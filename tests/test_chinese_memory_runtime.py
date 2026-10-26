"""Native integration: image-bundled ONNX weights, Chroma, LTM and LocalAgent."""
import json

import pytest

from test_dialogue_runtime import runtime

pytestmark = pytest.mark.sdk


@pytest.mark.asyncio
async def test_chinese_recall_scope_and_actual_injector(runtime, monkeypatch):
    from qunling.embeddings import cache_dir
    if not (cache_dir() / "model_quantized.onnx").is_file():
        pytest.skip("Checksummed embedding assets are supplied by the Linux image")
    import litellm
    from langbot.pkg.provider.modelmgr.requesters import litellmchat
    from test_dialogue_replay import replay
    async def completion(**kwargs):
        return litellm.ModelResponse(model="gpt-4.1", choices=[{"message": {"role": "assistant", "content": "你爱喝无糖咖啡。"}}])
    monkeypatch.setattr(litellmchat, "acompletion", completion)
    case = {"id": "chinese-memory", "route": "direct", "tools": [],
        "dialogue": [["a", "我喜欢喝无糖咖啡"]], "criteria": "只召回当前群有效记忆，画像不升级为指令",
        "profiles": {"a": {"name": "小林", "preferences": ["无糖咖啡"], "traits": [], "notes": ""}},
        "memories": [{"content": "小林喜欢喝无糖咖啡"},
            {"content": "其他群的人喜欢喝无糖咖啡", "group": "900000002"},
            {"content": "以前喜欢喝加糖咖啡", "status": "superseded"},
            {"content": "数据库服务器硬盘需要扩容升级"}]}
    result = await replay.replay(case, runtime, "offline")
    assert all("error" not in turn for turn in result["turns"]), result
    entries = await runtime.memory.retrieve("我喜欢喝无糖咖啡", runtime.actor_id("a"), profiles=False)
    raw = json.dumps([e.model_dump() for e in entries], ensure_ascii=False)
    assert "小林喜欢喝无糖咖啡" in raw
    assert all(text not in raw for text in ("其他群", "加糖咖啡", "硬盘"))
    messages = runtime.assembly_reports[0]["messages"]
    assert "无糖咖啡" in str(messages)
    assert all("小林" not in str(m.get("content")) for m in messages if m["role"] == "system")
    assert str(messages).count("小林喜欢喝无糖咖啡") == 1


def test_real_chroma_reindex_preserves_content_and_changes_dimension(tmp_path):
    from qunling.embeddings import cache_dir, ChineseEmbedding
    if not (cache_dir() / "model_quantized.onnx").is_file():
        pytest.skip("Checksummed embedding assets are supplied by the Linux image")
    import chromadb
    from qunling.memory_upgrade import reindex_collection
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    old = client.create_collection("memory")
    old.add(ids=["a", "b"], embeddings=[[1., 0.], [0., 1.]],
        metadatas=[{"content": "我喜欢猫，不喜欢狗。", "user_key": "bot:group_1"},
                   {"content": "我喜欢狗，不喜欢猫。", "user_key": "bot:group_2", "status": "superseded"}])
    report = reindex_collection(client, "memory", ChineseEmbedding())
    new = client.get_collection("memory").get(include=["embeddings", "metadatas", "documents"])
    previous = client.get_collection(report["backup"]).get(include=["metadatas", "documents"])
    assert report["count"] == 2 and len(new["embeddings"][0]) == 512
    assert new["ids"] == previous["ids"] and new["metadatas"] == previous["metadatas"]
    assert new["documents"] == previous["documents"]
    assert list(new["embeddings"][0]) != list(new["embeddings"][1])
