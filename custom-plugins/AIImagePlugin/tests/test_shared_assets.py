import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from main import GeneratedImage, ImageGenerationError
from pkg.asset_input import select_assets
from qunling.assets import AssetError, AssetStore
from qunling.integration import CONTEXT_KEY, descriptor
from qunling.scope import Scope
from test_image_core import plugin_with_client, response, PAYLOAD


def gif():
    out = io.BytesIO()
    first, second = Image.new("RGB", (80, 80), "red"), Image.new("RGB", (80, 80), "blue")
    first.save(out, format="GIF", save_all=True, append_images=[second], duration=150, loop=0)
    return out.getvalue()


@pytest.mark.asyncio
async def test_edit_receives_sampled_motion_and_keeps_original_asset(monkeypatch, tmp_path):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    plugin, client = plugin_with_client(monkeypatch, response(b64_json=PAYLOAD))
    binding = SimpleNamespace(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1)
    monkeypatch.setattr(plugin, "get_installation_binding", lambda: binding)
    shared = descriptor(plugin, SimpleNamespace(bot_uuid="bot", launcher_type="group", launcher_id="123", sender_id="alice"), "query")
    api = SimpleNamespace(query_uuid="query", get_query_vars=AsyncMock(return_value={CONTEXT_KEY: shared}), get_bot_uuid=AsyncMock(return_value="bot"))
    scope, store = Scope(**shared["scope"]), AssetStore.from_env()
    raw = gif()
    item = store.put(scope, "retrieved.gif", raw, origin="web")
    store.grant(scope, shared["request_key"], item["asset_ref"])
    references = await select_assets(plugin, api, [item["asset_ref"]])
    await plugin.edit_image("保留动作并更换主角", references)
    client.post.assert_awaited_once()
    body = client.post.await_args.kwargs["body"]
    value = body["images"][0]["image_url"]
    assert value.startswith("data:image/png;base64,") and "关键帧" in body["prompt"]
    with Image.open(io.BytesIO(base64.b64decode(value.split(",")[1]))) as image:
        colors = image.convert("RGB").getdata()
        assert (255, 0, 0) in colors and (0, 0, 255) in colors
    assert store.read(scope, item["asset_ref"]) == raw
    shared["scope"] = Scope(scope.workspace_id, scope.bot_id, scope.conversation_id, "bob").as_dict()
    with pytest.raises(AssetError):
        await select_assets(plugin, api, [item["asset_ref"]])


@pytest.mark.asyncio
async def test_corrupt_gif_never_reaches_paid_edit(monkeypatch):
    plugin, client = plugin_with_client(monkeypatch, response(b64_json=PAYLOAD))
    raw = b"GIF89a broken"
    with pytest.raises(ImageGenerationError, match="尚未提交"):
        await plugin.edit_image("change character", GeneratedImage(base64=base64.b64encode(raw).decode()))
    client.post.assert_not_awaited()
    client.images.generate.assert_not_awaited()
