import copy
import json
import sqlite3

import pytest

from qunling.embeddings import MODEL, REVISION
from qunling.memory_upgrade import collection_name, reindex_collection, upgrade


class Collection:
    def __init__(self, client, name, metadata=None):
        self.client, self.name, self.metadata = client, name, metadata
        self.records = {}

    def count(self):
        return len(self.records)

    def get(self, limit=None, offset=0, include=None):
        rows = list(self.records.items())[offset:offset+limit if limit is not None else None]
        return {"ids": [key for key, _ in rows], "metadatas": [row["metadata"] for _, row in rows],
                "documents": [row["document"] for _, row in rows]}

    def upsert(self, ids, embeddings, metadatas, documents=None):
        for index, key in enumerate(ids):
            self.records[key] = {"vector": embeddings[index], "metadata": copy.deepcopy(metadatas[index]),
                                 "document": documents[index] if documents else None}

    def delete(self, ids):
        for key in ids:
            self.records.pop(key, None)

    def modify(self, name):
        self.client.collections.pop(self.name)
        self.name = name
        self.client.collections[name] = self


class Client:
    def __init__(self):
        self.collections = {}

    def list_collections(self):
        return list(self.collections.values())

    def create_collection(self, name, metadata=None):
        assert name not in self.collections
        self.collections[name] = Collection(self, name, metadata)
        return self.collections[name]


def seed(client, name="memory"):
    old = client.create_collection(name)
    old.upsert(["a", "b"], [[1, 0], [0, 1]],
               [{"content": "喜欢猫", "user_key": "bot:group_1", "status": "active"},
                {"content": "喜欢狗", "user_key": "bot:group_2", "status": "superseded"}],
               ["喜欢猫", "喜欢狗"])
    return old


def test_reindex_preserves_ids_metadata_inactive_records_and_other_groups():
    client = Client()
    old = seed(client)
    before = copy.deepcopy(old.records)
    report = reindex_collection(client, "memory", lambda texts: [[1, 0, 0] for _ in texts])
    new = client.collections["memory"]
    assert new.metadata["qunling_embedding_revision"] == REVISION
    assert old.records == before and client.collections[report["backup"]] is old
    assert new.get() == old.get() and new.records["a"]["vector"] == [1, 0, 0]
    assert reindex_collection(client, "memory", lambda _: pytest.fail("reembedded")) == report


def test_embedding_failure_never_renames_or_changes_original():
    client = Client()
    old = seed(client)
    before = copy.deepcopy(old.records)
    def fail(_):
        raise RuntimeError("model unavailable")
    with pytest.raises(RuntimeError):
        reindex_collection(client, "memory", fail)
    assert client.collections["memory"] is old and old.records == before
    report = reindex_collection(client, "memory", lambda texts: [[1, 0, 0] for _ in texts])
    assert report["count"] == 2


def test_restart_between_two_renames_recovers_from_named_backup(monkeypatch):
    client = Client()
    old = seed(client)
    original = Collection.modify
    def interrupted(self, name):
        if name == "memory":
            raise RuntimeError("power loss")
        original(self, name)
    monkeypatch.setattr(Collection, "modify", interrupted)
    with pytest.raises(RuntimeError):
        reindex_collection(client, "memory", lambda texts: [[1, 0, 0] for _ in texts])
    assert "memory" not in client.collections and old.name.startswith("ql_old_")
    monkeypatch.setattr(Collection, "modify", original)
    reindex_collection(client, "memory", lambda texts: [[1, 0, 0] for _ in texts])
    assert client.collections["memory"].get() == old.get()


def test_database_switch_keeps_model_uuid_and_scope_and_ignores_custom_provider(tmp_path):
    path = tmp_path / "langbot.db"
    with sqlite3.connect(path) as db:
        db.executescript("""CREATE TABLE model_providers(uuid,workspace_uuid,name,requester);
            CREATE TABLE embedding_models(uuid,workspace_uuid,provider_uuid,name);
            CREATE TABLE workspaces(uuid,instance_uuid,source);
            CREATE TABLE knowledge_bases(uuid,workspace_uuid,knowledge_engine_plugin_id,
                creation_settings,embedding_dimension,collection_id,legacy_vector_collection);
            INSERT INTO workspaces VALUES ('w','i','local');
            INSERT INTO model_providers VALUES ('p','w','QQ Bot local embedding','chroma-embedding');
            INSERT INTO model_providers VALUES ('custom','w','My embedding','chroma-embedding');
            INSERT INTO embedding_models VALUES ('e','w','p','all-MiniLM-L6-v2');
            INSERT INTO embedding_models VALUES ('keep','w','custom','all-MiniLM-L6-v2');""")
        db.execute("INSERT INTO knowledge_bases VALUES (?,?,?,?,?,?,?)", ("k", "w", "langbot-team/LongTermMemory",
                   json.dumps({"embedding_model_uuid": "e", "isolation": "session"}), None, "k", 0))
    client = Client()
    physical = collection_name("i", "w", "k")
    seed(client, physical)
    assert upgrade(path, tmp_path / "chroma", client=client, embedding=lambda texts: [[1, 0, 0] for _ in texts])[0]["count"] == 2
    with sqlite3.connect(path) as db:
        assert dict(db.execute("SELECT uuid,name FROM embedding_models")) == {"e": MODEL, "keep": "all-MiniLM-L6-v2"}
        settings = json.loads(db.execute("SELECT creation_settings FROM knowledge_bases").fetchone()[0])
    assert settings["embedding_model_uuid"] == "e" and settings["isolation"] == "session"
    assert upgrade(path, tmp_path / "chroma", client=client) == []
