"""Offline, restartable upgrade of wizard-owned SQLite/Chroma memory indexes.

Run before Host/Runtime start. L1 profiles and episode metadata are never rewritten.
The previous collection is retained; an interrupted rename is completed on restart.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .embeddings import MODEL, REVISION, ChineseEmbedding


def collection_name(instance: str, workspace: str, kb: str) -> str:
    return "lb_" + uuid.uuid5(uuid.NAMESPACE_URL, f"langbot:knowledge-vector:{instance}:{workspace}:{kb}").hex


def _collections(client):
    return {item.name: item for item in client.list_collections()}


def _digest(collection) -> tuple[int, str]:
    """Order independent identity/content/ownership check, bounded page memory."""
    digest, count = 0, collection.count()
    for offset in range(0, count, 128):
        page = collection.get(limit=128, offset=offset, include=["metadatas", "documents"])
        for index, item_id in enumerate(page["ids"]):
            value = [item_id, page["metadatas"][index], page["documents"][index]]
            digest ^= int(hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest(), 16)
    return count, f"{digest:064x}"


def reindex_collection(client, name: str, embedding) -> dict:
    suffix = hashlib.sha256((name + REVISION).encode()).hexdigest()[:32]
    backup, shadow = "ql_old_" + suffix, "ql_build_" + suffix
    collections = _collections(client)
    current = collections.get(name)
    if current and (current.metadata or {}).get("qunling_embedding_revision") == REVISION:
        return {"collection": name, "count": current.count(), "backup": backup if backup in collections else None}
    if backup in collections:
        if current is not None:
            raise RuntimeError("Memory migration has conflicting old and active collections")
        source = collections[backup]
    else:
        source = current
    if source is None:
        client.create_collection(name, metadata={"qunling_embedding_revision": REVISION})
        return {"collection": name, "count": 0, "backup": None}
    expected = _digest(source)
    staging = collections.get(shadow)
    if staging is None:
        staging = client.create_collection(shadow, metadata={"qunling_embedding_revision": REVISION})
    # Staging is disposable until verified. Remove stale staged entries from an
    # earlier attempt whose old collection subsequently changed before restart.
    source_ids = set()
    for offset in range(0, expected[0], 64):
        page = source.get(limit=64, offset=offset, include=["metadatas", "documents"])
        source_ids.update(page["ids"])
        texts = []
        for meta in page["metadatas"]:
            content = (meta or {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Memory migration requires the original episode content")
            texts.append(content)
        if texts:
            vectors = embedding(texts)
            # Chroma disallows nulls inside a documents batch. Split to preserve
            # missing documents, rather than fabricating or losing their values.
            for has_document in (False, True):
                indices = [i for i, doc in enumerate(page["documents"]) if (doc is not None) == has_document]
                if not indices:
                    continue
                kwargs = {"ids": [page["ids"][i] for i in indices],
                          "embeddings": [vectors[i] for i in indices],
                          "metadatas": [page["metadatas"][i] for i in indices]}
                if has_document:
                    kwargs["documents"] = [page["documents"][i] for i in indices]
                staging.upsert(**kwargs)
    stale = set(staging.get(include=[])["ids"]) - source_ids
    for start in range(0, len(stale), 128):
        staging.delete(ids=sorted(stale)[start:start + 128])
    if _digest(staging) != expected or _digest(source) != expected:
        raise RuntimeError("Memory index verification failed; original index retained")
    if backup not in collections:
        source.modify(name=backup)
    # If the process exits between the renames, the named backup above is the
    # source on the next startup. No Host is allowed to run in this state.
    staging.modify(name=name)
    return {"collection": name, "count": expected[0], "backup": backup}


def upgrade(database: Path, chroma: Path, *, client=None, embedding=None) -> list[dict]:
    """Upgrade only the wizard's local MiniLM models and their scoped memories."""
    if not database.is_file():
        return []
    connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"embedding_models", "model_providers", "knowledge_bases", "workspaces"} <= tables:
            return []  # Fresh/pre-tenancy Host performs its own schema migration first.
        connection.execute("BEGIN IMMEDIATE")
        models = list(connection.execute("""SELECT e.uuid, e.workspace_uuid FROM embedding_models e
            JOIN model_providers p ON p.uuid=e.provider_uuid AND p.workspace_uuid=e.workspace_uuid
            WHERE e.name='all-MiniLM-L6-v2' AND p.name='QQ Bot local embedding'
            AND p.requester='chroma-embedding'"""))
        if not models:
            connection.rollback()
            return []
        workspaces = {r["uuid"]: dict(r) for r in connection.execute("SELECT uuid,instance_uuid,source FROM workspaces")}
        bases = [dict(r) for r in connection.execute("SELECT * FROM knowledge_bases")]
        plans = []
        for model in models:
            workspace = workspaces[model["workspace_uuid"]]
            if workspace["source"] != "local":
                raise RuntimeError("Automatic local memory upgrade cannot migrate a projected workspace")
            for kb in bases:
                settings = json.loads(kb["creation_settings"] or "{}")
                if settings.get("embedding_model_uuid") != model["uuid"]:
                    continue
                if (kb["workspace_uuid"] != model["workspace_uuid"]
                        or kb["knowledge_engine_plugin_id"] != "langbot-team/LongTermMemory"):
                    raise RuntimeError("Managed embedding is shared with an unsupported knowledge engine")
                physical = collection_name(workspace["instance_uuid"], workspace["uuid"], kb["uuid"])
                if kb.get("legacy_vector_collection"):
                    if len(workspaces) != 1 or not kb.get("collection_id"):
                        raise RuntimeError("Legacy collection is outside the single local workspace")
                    physical = kb["collection_id"]
                plans.append((kb, settings, physical))
        if client is None:
            import chromadb
            client = chromadb.PersistentClient(path=str(chroma))
        # Load once before touching any collection or database record.
        embedding = embedding or ChineseEmbedding()
        reports = [reindex_collection(client, physical, embedding) for _, _, physical in plans]
        for kb, settings, _ in plans:
            settings["embedding_revision"] = REVISION
            connection.execute("UPDATE knowledge_bases SET creation_settings=?, embedding_dimension=? WHERE uuid=? AND workspace_uuid=?",
                               (json.dumps(settings), 512, kb["uuid"], kb["workspace_uuid"]))
        for model in models:
            connection.execute("UPDATE embedding_models SET name=? WHERE uuid=? AND workspace_uuid=? AND name='all-MiniLM-L6-v2'",
                               (MODEL, model["uuid"], model["workspace_uuid"]))
        connection.commit()
        return reports
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def before_start(app_root: Path) -> list[dict]:
    """Respect deployment backend selection; never open a second storage backend."""
    import yaml
    config_path = app_root / "data/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    config = config or {}
    database = config.get("database", {})
    database_type = os.environ.get("DATABASE__USE", database.get("use", "sqlite"))
    vector_type = os.environ.get("VDB__USE", config.get("vdb", {}).get("use", "chroma"))
    if database_type != "sqlite" or vector_type != "chroma":
        return []
    path = Path(os.environ.get("DATABASE__SQLITE__PATH", database.get("sqlite", {}).get("path", "data/langbot.db")))
    return upgrade(path if path.is_absolute() else app_root / path, app_root / "data/chroma")
