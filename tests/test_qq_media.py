"""Media chains retain conversation ownership; models and HTTP are isolated."""
import asyncio
import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
from PIL import Image
import pytest

pytestmark = pytest.mark.usefixtures('available_file_resources')

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "langbot/src").is_dir():
    sys.path.insert(0, str(ROOT / "langbot/src"))
from langbot_plugin.api.entities.builtin.provider.message import Message
from qunling.assets import AssetStore, AssetError
from qunling.scope import Scope
from qunling import qq_media as media
from test_qq_actions import service


def picture():
    output = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def assets(service, tmp_path, monkeypatch):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path / "data"))
    return AssetStore.from_env()


@pytest.mark.asyncio
async def test_import_then_analyze_send_and_file_upload_share_real_assets(service, assets, monkeypatch):
    blob = picture()
    service.state["media"]["qqmedia_17_0"] = {"kind": "image", "data": {"url": "https://example.com/a.png"}, "message_id": "17"}
    fetch = AsyncMock(return_value=NS(body=blob))
    monkeypatch.setattr(media, "fetch_public_file", fetch)
    imported = await service.execute("import_media", {"media_ref": "qqmedia_17_0"})
    assert assets.read(service.asset_scope, imported["asset_ref"]) == blob
    assert (await service.execute("import_media", {"media_ref": "qqmedia_17_0"}))["cached"]
    fetch.assert_awaited_once()
    invoke = AsyncMock(return_value=Message(role="assistant", content="看见红色画面。"))
    monkeypatch.setattr(media, "model", AsyncMock(return_value=NS(provider=NS(invoke_llm=invoke))))
    analysis = await service.execute("analyze_images", {"asset_refs": [imported["asset_ref"]], "question": "有什么？"})
    assert analysis["analysis"] == "看见红色画面。"
    content = invoke.call_args.kwargs["messages"][0].content
    assert any(c.type == "image_base64" and base64.b64decode(c.image_base64) == blob for c in content)
    assert (await service.execute("send_media", {"asset_ref": imported["asset_ref"], "kind": "image"}))["status"] == "confirmed"
    assert (await service.execute("upload_file", {"asset_ref": imported["asset_ref"]}))["status"] == "confirmed"
    payload = service.calls[-1][1]
    assert payload["group_id"] == "300" and base64.b64decode(payload["file"].removeprefix("base64://")) == blob


@pytest.mark.asyncio
async def test_media_cannot_read_foreign_or_new_ungranted_assets(service, assets):
    other = Scope(service.asset_scope.workspace_id, "bot", "group:other", "101")
    foreign = assets.put(other, "image.png", picture())
    with pytest.raises(AssetError):
        await service.execute("send_media", {"asset_ref": foreign["asset_ref"], "kind": "image"})
    new = assets.put(service.asset_scope, "image.png", picture())
    with pytest.raises(AssetError):
        await service.execute("send_media", {"asset_ref": new["asset_ref"], "kind": "image"})
    assert not service.calls or all(c[0] == "get_group_member_info" for c in service.calls)


@pytest.mark.asyncio
async def test_audio_conversion_and_transcription_use_configured_provider(service, assets, monkeypatch):
    service.state["media"]["qqmedia_17_1"] = {"kind": "record", "data": {"file": "voice.silk"}, "message_id": "17"}
    raw = service.raw
    async def converted(action, **kwargs):
        if action == "get_record":
            assert kwargs == {"file": "voice.silk", "out_format": "wav"}
            return {"base64": base64.b64encode(b"RIFF\x00\x00\x00\x00WAVE" + b"data").decode()}
        return await raw(action, **kwargs)
    service.raw = converted
    item = await service.execute("import_media", {"media_ref": "qqmedia_17_1"})
    assert item["name"].endswith(".wav")
    with pytest.raises(ValueError, match="管理页"):
        await service.execute("transcribe", {"asset_ref": item["asset_ref"]})
    service.config["qq_actions"]["transcription_model"] = "test-asr"
    provider = NS(provider_entity=NS(base_url="https://model.invalid/v1"), token_mgr=NS(get_token=lambda: "test-key"))
    monkeypatch.setattr(media, "model", AsyncMock(return_value=NS(provider=provider)))
    requests = []
    def response(request):
        requests.append(request)
        assert str(request.url) == "https://model.invalid/v1/audio/transcriptions"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert b"test-asr" in request.content and b"RIFF" in request.content
        return httpx.Response(200, json={"text": "你好"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(response)))
    result = await service.execute("transcribe", {"asset_ref": item["asset_ref"]})
    assert result == {"status": "ok", "transcript": "你好", "truncated": False}
    with pytest.raises(ValueError, match="不自动重复"):
        await service.execute("transcribe", {"asset_ref": item["asset_ref"]})
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_video_exports_frames_with_grants_for_following_tools(service, assets, monkeypatch):
    video = media.publish(service, "clip.mp4", b"\x00\x00\x00\x18ftypisom")
    seen = []
    def frames(body, count, seconds):
        seen.append((count, seconds))
        return [picture(), picture()]
    monkeypatch.setattr(media, "ffmpeg_frames", frames)
    result = await service.execute("video_frames", {"asset_ref": video["asset_ref"], "frames": 2, "seconds": 10})
    assert seen == [(2, 10)] and result["sampled"] is True
    for item in result["frames"]:
        assets.require_visible(service.asset_scope, item["asset_ref"], {"created": service.state["created"], "request_key": service.request})
        assert assets.read(service.asset_scope, item["asset_ref"]) == picture()


@pytest.mark.asyncio
async def test_model_resolution_uses_host_pipeline_and_workspace(service, monkeypatch):
    from langbot.pkg.core import task_boundary
    scope = NS(workspace_uuid="workspace")
    runtime = NS(execution_context=scope, ap=NS(model_mgr=NS(get_model_by_uuid=AsyncMock(return_value="model"))))
    service.adapter._runtime = runtime
    service.query.pipeline_config = {"ai": {"runner": {"runner_id": "local/LocalAgent", "runner_config": {"local/LocalAgent": {"model": {"primary": "model-id"}}}}}}
    from langbot.pkg.agent.runner.config_resolver import RunnerConfigResolver
    monkeypatch.setattr(RunnerConfigResolver, "resolve_runner_id", lambda _: "agent")
    monkeypatch.setattr(RunnerConfigResolver, "resolve_runner_config", lambda *_: {"model": {"primary": "model-id"}})
    async def uow(ap, workspace, invoke):
        assert workspace == "workspace"
        return await invoke()
    monkeypatch.setattr(task_boundary, "run_in_workspace_uow", uow)
    assert await media.model(service) == "model"
    runtime.ap.model_mgr.get_model_by_uuid.assert_awaited_once_with(scope, "model-id")


def test_real_short_video_includes_first_frame(tmp_path):
    executable = shutil.which("ffmpeg")
    if not executable:
        pytest.skip("FFmpeg is tested in the Linux production image")
    clip = tmp_path / "clip.mp4"
    subprocess.run([executable, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32:r=10:d=1",
                    "-c:v", "mpeg4", str(clip)], check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = media.ffmpeg_frames(clip.read_bytes(), 4, 30)
    assert 2 <= len(result) <= 4
    with Image.open(io.BytesIO(result[0])) as image:
        assert image.width <= 768 and image.height <= 768


def test_non_video_input_cannot_be_used_as_ffmpeg_playlist(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    with pytest.raises(ValueError, match="容器"):
        media.ffmpeg_frames(b"#EXTM3U\nhttp://127.0.0.1/secret", 6, 30)
