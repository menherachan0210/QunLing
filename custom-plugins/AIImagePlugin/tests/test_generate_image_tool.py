"""Exercise Tool delivery and billing guards with SDK and generator fakes only."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


TOOL_PATH = Path(__file__).resolve().parents[1] / "components/tools/generate_image.py"


@pytest.fixture
def tool_env(monkeypatch, tmp_path):
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    class ImageGenerationError(Exception):
        pass

    class Tool:
        pass

    class Image:
        def __init__(self, *, base64, url):
            self.base64 = base64
            self.url = url

    class MessageChain(list):
        pass

    class Proxy:
        def __init__(self, *, query_id, query_uuid, plugin_runtime_handler):
            self.handler = plugin_runtime_handler
            self.ref = (query_id, query_uuid)
            self.query_uuid = query_uuid
            self.handler.refs.append(self.ref)

        async def get_query_vars(self):
            if self.handler.read_error:
                raise RuntimeError("private read failure")
            # Yield to expose an unprotected check/set race deterministically.
            state = dict(self.handler.variables.setdefault(self.ref, {}))
            from qunling.integration import CONTEXT_KEY, descriptor
            state.setdefault(CONTEXT_KEY, descriptor(self.handler.plugin, SimpleNamespace(
                bot_uuid="bot", launcher_type=getattr(self.handler, "launcher_type", "group"),
                launcher_id=getattr(self.handler, "launcher_id", "group"),
                sender_id=getattr(self.handler, "sender_id", "member")),
                self.query_uuid))
            await asyncio.sleep(0)
            return state

        async def set_query_var(self, key, value):
            self.handler.writes.append((key, value))
            if self.handler.write_error or (
                self.handler.final_write_error and value != "started"
            ):
                raise RuntimeError("private write failure")
            self.handler.variables.setdefault(self.ref, {})[key] = value
            await asyncio.sleep(0)

        async def get_bot_uuid(self):
            return "bot"

        async def reply(self, chain):
            self.handler.attempts.append(chain)
            if self.handler.send_hangs:
                await asyncio.Event().wait()
            if self.handler.send_error:
                raise RuntimeError("private delivery failure with token")
            self.handler.delivered.append(chain)

    context_state = SimpleNamespace(
        selected=SimpleNamespace(base64="data:image/png;base64/source", url=None),
        selected_refs=[
            SimpleNamespace(base64="data:image/png;base64/first", url=None),
            SimpleNamespace(base64="data:image/png;base64/second", url=None),
        ],
        error=None,
    )
    remember_output = AsyncMock()

    def query_claim_lock(plugin):
        if not hasattr(plugin, "_image_query_claim_lock"):
            plugin._image_query_claim_lock = asyncio.Lock()
        return plugin._image_query_claim_lock

    def select_image(plugin, variables):
        if context_state.error:
            raise context_state.error
        return context_state.selected

    def select_references(plugin, variables, query_id, query_uuid, refs, *, bot=None):
        assert bot == "bot"
        if context_state.error:
            raise context_state.error
        return context_state.selected_refs

    modules = {
        "langbot_plugin.api.definition.components.tool.tool": {"Tool": Tool},
        "langbot_plugin.api.entities.builtin.platform.message": {
            "Image": Image,
            "MessageChain": MessageChain,
        },
        "langbot_plugin.api.proxies.query_based_api": {"QueryBasedAPIProxy": Proxy},
        "main": {"ImageGenerationError": ImageGenerationError,
                 "image_data_url": AsyncMock(return_value="data:image/png;base64,c2F2ZWQgb3V0cHV0")},
        "pkg.image_context": {
            "query_claim_lock": query_claim_lock,
            "remember_output": remember_output,
            "select_image": select_image,
        },
        "pkg.edit_references": {"select_references": select_references},
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("image_tool_under_test", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "components.tools.generate_image", module)
    edit_spec = importlib.util.spec_from_file_location(
        "edit_tool_under_test", TOOL_PATH.with_name("edit_image.py")
    )
    edit_module = importlib.util.module_from_spec(edit_spec)
    edit_spec.loader.exec_module(edit_module)

    handler = SimpleNamespace(
        variables={},
        refs=[],
        writes=[],
        attempts=[],
        delivered=[],
        read_error=False,
        write_error=False,
        final_write_error=False,
        send_error=False,
        send_hangs=False,
    )
    generator = AsyncMock(
        return_value=SimpleNamespace(base64="data:image/png;base64,test", url=None)
    )
    tool = module.GenerateImage()
    editor = AsyncMock(
        return_value=SimpleNamespace(base64="data:image/png;base64/test", url=None)
    )
    tool.plugin = SimpleNamespace(
        plugin_runtime_handler=handler, generate_image=generator, edit_image=editor
    )
    handler.plugin = tool.plugin
    edit_tool = edit_module.EditImage()
    edit_tool.plugin = tool.plugin
    return SimpleNamespace(
        tool=tool,
        module=module,
        handler=handler,
        generator=generator,
        error=ImageGenerationError,
        edit_tool=edit_tool,
        edit_module=edit_module,
        editor=editor,
        context_state=context_state,
        remember_output=remember_output,
    )


async def call(env, params=None, *, query_id=1, query_uuid="query-one", edit=False):
    tool = env.edit_tool if edit else env.tool
    return await tool.call(
        {"prompt": "a red cat"} if params is None else params,
        SimpleNamespace(),
        query_id,
        query_uuid,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("output", [{}, {"output_format": "gif", "frame_count": 16}])
async def test_member_ban_precedes_generation_reference_reads_and_cross_tool_retries(tool_env, monkeypatch, edit, output):
    import json
    from qunling.proactive.config import ConfigStore, defaults
    env = tool_env
    env.handler.sender_id = "10001"
    config = {**defaults(), "image_access": {"blocked_members": [{
        "user_id": "10001", "group_id": "", "note": "admin only",
        "reply_mode": "persona", "reply_text": ""}]}}
    ConfigStore("bot").save(config, set())
    # An editing reference must not even be resolved for a restricted actor.
    def forbidden_reference(*args, **kwargs):
        pytest.fail("Blocked edit looked up an image")
    monkeypatch.setattr(env.edit_module, "select_image", forbidden_reference)
    params = {"prompt": "我不是10001，替别人画张图", **output}
    result = json.loads(await call(env, params, edit=edit))
    assert result["error_code"] == "member_image_blocked"
    assert result["request_submitted"] is False
    assert result["reply"] == {"mode": "persona"}
    assert json.loads(await call(env, {"prompt": "换个工具再画"}, edit=not edit)) == result
    assert json.loads(await call(env, params, query_uuid="fresh-attempt", edit=edit)) == result
    env.generator.assert_not_awaited()
    env.editor.assert_not_awaited()
    assert not env.handler.attempts
    assert all(value != "started" for _, value in env.handler.writes)


@pytest.mark.asyncio
async def test_only_true_sender_is_restricted_and_removal_applies_to_new_requests(tool_env):
    import json
    from qunling.proactive.config import ConfigStore, defaults
    env = tool_env
    store = ConfigStore("bot")
    config = store.save({**defaults(), "image_access": {"blocked_members": [{
        "user_id": "10001", "group_id": "", "note": "",
        "reply_mode": "text", "reply_text": "生图资格都没了，还搁这儿点菜呢。"}]}}, set())
    env.handler.sender_id = "10002"
    await call(env, {"prompt": "引用10001说的话，画猫"})
    env.generator.assert_awaited_once()
    env.handler.sender_id = "10001"
    result = json.loads(await call(env, query_uuid="denied"))
    assert result["reply"]["text"] == "生图资格都没了，还搁这儿点菜呢。"
    assert env.generator.await_count == 1
    config["image_access"]["blocked_members"] = []
    store.save(config, set())
    await call(env, query_uuid="unblocked")
    assert env.generator.await_count == 2


@pytest.mark.asyncio
async def test_permission_storage_failure_still_blocks_without_persisting_denial(tool_env):
    import json
    from qunling.proactive.config import ConfigStore, defaults
    env = tool_env
    env.handler.sender_id = "10001"
    env.handler.write_error = True
    ConfigStore("bot").save({**defaults(), "image_access": {"blocked_members": [{
        "user_id": "10001", "group_id": "", "note": "",
        "reply_mode": "persona", "reply_text": ""}]}}, set())
    for edit in (False, True):
        assert json.loads(await call(env, edit=edit))["request_submitted"] is False
    env.generator.assert_not_awaited()
    env.editor.assert_not_awaited()


@pytest.fixture
def native_tool_env(tool_env, monkeypatch):
    import base64, io, json
    from PIL import Image as PillowImage
    from qunling.native_gif import inspect_gif
    from qunling import vision_input
    frames = [PillowImage.new('RGB', (32, 32), (i * 12, 80, 120)) for i in range(16)]
    stream = io.BytesIO()
    frames[0].save(stream, format='GIF', save_all=True, append_images=frames[1:], duration=200, loop=0)
    for frame in frames:
        frame.close()
    raw = stream.getvalue()
    encoded = 'data:image/gif;base64,' + base64.b64encode(raw).decode()
    tool_env.generator.return_value = tool_env.editor.return_value = SimpleNamespace(base64=encoded, url=None)
    monkeypatch.setattr(sys.modules['main'], 'image_data_url', AsyncMock(return_value=encoded))
    # Exercise real frame parsing here; the isolated process/caps have dedicated tests.
    async def inspect(value):
        return inspect_gif(value)
    monkeypatch.setattr(vision_input, 'inspect_generated_gif', inspect)
    return tool_env, raw


@pytest.mark.asyncio
@pytest.mark.parametrize('edit', [False, True])
@pytest.mark.parametrize('deliver', [False, True])
async def test_native_gif_saves_and_delivers_original_bytes_once(native_tool_env, edit, deliver):
    import base64, json
    from qunling.assets import AssetStore, AssetError
    from qunling.scope import Scope
    env, raw = native_tool_env
    params = {'prompt': '猫挥手然后回到起始姿势', 'output_format': 'gif', 'deliver': deliver}
    result = json.loads(await call(env, params, edit=edit))
    assert result['status'] == ('sent' if deliver else 'created')
    assert result['delivery_status'] == ('sent' if deliver else 'not_requested')
    assert result['frame_count'] == 16 and result['duration_ms'] == 3200
    assert result['name'].endswith('.gif')
    # Retrieve through the same query-bound scope that was used for persistence.
    from qunling.integration import descriptor
    context = descriptor(env.tool.plugin, SimpleNamespace(bot_uuid='bot', launcher_type='group',
                         launcher_id='group', sender_id='member'), 'query-one')
    scope = Scope(**context['scope'])
    store = AssetStore.from_env()
    assert store.read(scope, result['asset_ref']) == raw
    with pytest.raises(AssetError):
        store.read(Scope(scope.workspace_id, scope.bot_id, 'group:other', scope.actor_id), result['asset_ref'])
    if deliver:
        assert base64.b64decode(env.handler.delivered[0][0].base64) == raw
        env.remember_output.assert_awaited_once()
    else:
        assert env.handler.delivered == []
    generator = env.editor if edit else env.generator
    generator.assert_awaited_once()
    assert generator.call_args.args[0] == params['prompt']
    assert generator.call_args.kwargs == {'output': {'output_format': 'gif'}}
    await call(env, params, edit=not edit)
    assert env.generator.await_count + env.editor.await_count == 1
    assert len(env.handler.attempts) == int(deliver)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['send', 'validation', 'storage'])
async def test_native_failure_keeps_claim_and_never_repeats_generation(native_tool_env, monkeypatch, failure):
    import json
    from qunling import vision_input
    from qunling.assets import AssetStore
    env, raw = native_tool_env
    if failure == 'send':
        env.handler.send_error = True
    elif failure == 'validation':
        monkeypatch.setattr(vision_input, 'inspect_generated_gif', AsyncMock(side_effect=ValueError('bad GIF')))
    else:
        def unavailable(*a, **kw):
            raise OSError('disk unavailable')
        monkeypatch.setattr(AssetStore, 'put', unavailable)
    params = {'prompt': '挥手', 'output_format': 'gif'}
    result = json.loads(await call(env, params))
    assert result['status'] == 'partial'
    assert ('asset_ref' in result) is (failure != 'storage')
    assert not env.handler.delivered
    await call(env, params, edit=True)
    assert env.generator.await_count + env.editor.await_count == 1
    assert len(env.handler.attempts) == int(failure == 'send')


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [
    {'gif_frames': ['a', 'b']}, {'output_format': 'video'}, {'output_format': []},
    {'frame_count': 16}, {'frame_duration_ms': 200}, {'output_format': 'gif', 'frame_count': True},
    {'output_format': 'gif', 'frame_count': 17}, {'output_format': 'gif', 'frame_duration_ms': 2001},
])
async def test_invalid_native_or_removed_options_never_charge(tool_env, options):
    await call(tool_env, {'prompt': 'wave', **options})
    tool_env.generator.assert_not_awaited()
    assert not tool_env.handler.writes


@pytest.mark.asyncio
async def test_native_gif_still_delivers_if_optional_state_update_fails(native_tool_env):
    import json
    env, _ = native_tool_env
    env.handler.final_write_error = True
    result = json.loads(await call(env, {'prompt': 'wave', 'output_format': 'gif'}))
    assert result['status'] == 'sent' and len(env.handler.delivered) == 1
    await call(env, {'prompt': 'wave', 'output_format': 'gif'}, edit=True)
    assert env.generator.await_count + env.editor.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"prompt": ""},
        {"prompt": " \n "},
        {"prompt": 42},
        {"prompt": "x" * 8001},
        {"prompt": "cat", "target_id": "unwanted"},
    ],
)
async def test_invalid_parameters_never_claim_generate_or_send(tool_env, params):
    env = tool_env
    assert await call(env, params) == env.module.INVALID_PROMPT
    env.generator.assert_not_awaited()
    assert env.handler.refs == env.handler.writes == env.handler.attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image",
    [
        SimpleNamespace(base64="data:image/png;base64,test", url=None),
        SimpleNamespace(base64=None, url="https://images.example/generated.png"),
    ],
)
async def test_sends_one_image_to_current_query_and_returns_only_status(
    tool_env, image
):
    env = tool_env
    env.generator.return_value = image
    result = await call(env, {"prompt": "  a red cat  "})
    assert result == env.module.SENT
    env.generator.assert_awaited_once_with("a red cat")
    assert env.handler.refs == [(1, "query-one")]
    assert len(env.handler.delivered) == 1
    assert len(env.handler.delivered[0]) == 1
    sent = env.handler.delivered[0][0]
    assert (sent.base64, sent.url) == ("c2F2ZWQgb3V0cHV0", "")
    assert "https://" not in result and "data:" not in result
    assert env.handler.variables[(1, "query-one")][env.module.QUERY_STATE_KEY] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_delivery_result_distinguishes_requested_traits_from_observed_output(tool_env, edit):
    env = tool_env
    prompt = "保留紫色短发和白色发饰，改成水彩头像。"
    result = await call(env, {"prompt": prompt}, edit=edit)

    # The provider received the requested traits; returning a generated image
    # still supplies no visual assessment of whether those traits survived.
    if edit:
        env.editor.assert_awaited_once_with(prompt, env.context_state.selected)
        env.generator.assert_not_awaited()
    else:
        env.generator.assert_awaited_once_with(prompt)
        env.editor.assert_not_awaited()
    assert len(env.handler.delivered) == 1
    assert "图片已生成并发送" in result
    assert "没有对成图做像素识别" in result
    assert "生成描述是本次要求" in result
    assert "交流表达采用当前群设置" in result
    assert "若此前确实识别过原图" in result
    assert "不要因本轮未识别成图而否认已经发图或此前看过原图" in result
    assert "紫色" not in result and "白色" not in result and "水彩" not in result
    assert "data:" not in result and "https://" not in result

    # A repeated tool call returns the same factual boundary without billing or
    # implying the different retry prompt was executed or visually observed.
    repeated = await call(env, {"prompt": "再保留红色帽子"}, edit=not edit)
    assert repeated == result
    assert env.generator.await_count + env.editor.await_count == 1
    assert len(env.handler.delivered) == 1


@pytest.mark.asyncio
async def test_prompt_boundary_and_legacy_query_id_are_supported(tool_env):
    env = tool_env
    assert await call(env, {"prompt": "x" * 8000}, query_uuid=None) == env.module.SENT
    env.generator.assert_awaited_once_with("x" * 8000)
    assert env.handler.refs == [(1, None)]


@pytest.mark.asyncio
async def test_claim_precedes_billable_generation_and_repeated_prompt_is_not_regenerated(
    tool_env,
):
    env = tool_env

    async def generate(prompt):
        assert (
            env.handler.variables[(1, "query-one")][env.module.QUERY_STATE_KEY]
            == "started"
        )
        return SimpleNamespace(base64="data:image/png;base64,test", url=None)

    env.generator.side_effect = generate
    assert await call(env) == env.module.SENT
    assert await call(env, {"prompt": "a different requested image"}) == env.module.SENT
    env.generator.assert_awaited_once()
    assert len(env.handler.attempts) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_calls_generate_and_deliver_once(tool_env):
    env = tool_env
    results = await asyncio.gather(*(call(env) for _ in range(6)))
    env.generator.assert_awaited_once()
    assert len(env.handler.attempts) == 1
    assert all(result in {env.module.SENT, env.module.GENERATING} for result in results)


@pytest.mark.asyncio
async def test_other_queries_do_not_share_billing_state(tool_env):
    env = tool_env
    assert await call(env, query_uuid="query-a") == env.module.SENT
    assert await call(env, query_uuid="query-b") == env.module.SENT
    assert env.generator.await_count == 2
    assert len(env.handler.delivered) == 2
    assert set(vars(env.tool)) == {"plugin"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read_error", "write_error"])
async def test_guard_failure_prevents_generation(tool_env, failure):
    env = tool_env
    setattr(env.handler, failure, True)
    assert await call(env) == env.module.GUARD_UNAVAILABLE
    env.generator.assert_not_awaited()
    assert env.handler.attempts == []


@pytest.mark.asyncio
async def test_safe_generation_error_is_reported_without_delivery_or_retry(tool_env):
    env = tool_env
    env.generator.side_effect = env.error("绘图服务暂不可用。")
    first = await call(env)
    second = await call(env)
    assert "绘图服务暂不可用" in first
    assert second == env.module.GENERATION_FAILED
    env.generator.assert_awaited_once()
    assert env.handler.attempts == []


@pytest.mark.asyncio
async def test_unexpected_error_and_empty_result_are_sanitized(tool_env):
    env = tool_env
    env.generator.side_effect = RuntimeError("private API token in provider error")
    assert await call(env) == env.module.GENERATION_FAILED
    env.generator.side_effect = None
    env.generator.return_value = SimpleNamespace(base64=None, url=None)
    assert "未返回可发送的图片" in await call(env, query_uuid="empty-result")
    assert env.handler.attempts == []


@pytest.mark.asyncio
async def test_delivery_failure_never_retries_or_regenerates(tool_env):
    env = tool_env
    env.handler.send_error = True
    result = await call(env)
    assert result.startswith(env.module.SEND_FAILED) and "asset_ref=asset_" in result
    assert await call(env) == result
    from qunling.assets import AssetStore
    from qunling.scope import Scope
    from qunling.integration import workspace_key
    scope = Scope(workspace_key(env.tool.plugin), "bot", "group:group", "member")
    ref = env.handler.variables[(1, "query-one")][env.module.QUERY_STATE_KEY + ".asset"]
    # A fresh store/process can recover the paid artifact after a QQ failure.
    store = AssetStore.from_env()
    assert store.read(scope, ref) == b"saved output"
    assert store.delivery(scope, "query-one", ref) == "unconfirmed"
    env.generator.assert_awaited_once()
    assert len(env.handler.attempts) == 1
    assert env.handler.delivered == []


@pytest.mark.asyncio
async def test_terminal_state_write_failure_keeps_pre_request_claim(tool_env):
    env = tool_env
    env.handler.final_write_error = True
    assert await call(env) == env.module.SENT
    assert await call(env) == env.module.GENERATING
    env.generator.assert_awaited_once()
    assert len(env.handler.attempts) == 1


@pytest.mark.asyncio
async def test_cancelled_generation_keeps_claim_to_prevent_second_charge(tool_env):
    env = tool_env
    env.generator.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await call(env)
    assert await call(env) == env.module.GENERATING
    env.generator.assert_awaited_once()
    assert env.handler.attempts == []


@pytest.mark.asyncio
async def test_new_component_reads_existing_host_claim(tool_env):
    env = tool_env
    assert await call(env) == env.module.SENT
    replacement = env.module.GenerateImage()
    replacement.plugin = env.tool.plugin
    env.tool = replacement
    assert await call(env) == env.module.SENT
    env.generator.assert_awaited_once()
    assert len(env.handler.attempts) == 1


@pytest.mark.asyncio
async def test_edit_saves_and_remembers_selected_image_before_delivery(tool_env):
    env = tool_env

    async def remember(plugin, api, image):
        assert len(env.handler.delivered) == 0
        assert env.handler.variables[(1, "query-one")][env.module.QUERY_STATE_KEY + ".asset"].startswith("asset_")
        assert image is env.editor.return_value

    env.remember_output.side_effect = remember
    assert (
        await call(env, {"prompt": "  change the background  "}, edit=True)
        == env.module.SENT
    )
    env.editor.assert_awaited_once_with(
        "change the background", env.context_state.selected
    )
    env.generator.assert_not_awaited()
    env.remember_output.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", ["没有可用的近期图片。", "本条消息包含多张图片。", "图片已过期。"]
)
async def test_edit_missing_ambiguous_or_expired_source_does_not_claim_or_bill(
    tool_env, message
):
    env = tool_env
    env.context_state.error = env.error(message)
    assert await call(env, edit=True) == message
    env.editor.assert_not_awaited()
    assert env.handler.writes == env.handler.attempts == []


@pytest.mark.asyncio
async def test_generate_and_edit_share_one_concurrent_billing_claim(tool_env):
    env = tool_env
    results = await asyncio.gather(
        call(env), call(env, edit=True), call(env), call(env, edit=True)
    )
    assert env.generator.await_count + env.editor.await_count == 1
    assert len(env.handler.attempts) == 1
    assert all(result in {env.module.SENT, env.module.GENERATING} for result in results)


@pytest.mark.asyncio
async def test_multi_edit_uses_selected_list_and_shares_generate_claim(tool_env):
    env = tool_env
    params = {"prompt": "交换衣服", "image_refs": ["edit_ref_first", "edit_ref_second"]}
    first = await call(env, params, edit=True)
    assert first == env.module.SENT
    env.editor.assert_awaited_once_with("交换衣服", env.context_state.selected_refs)
    assert await call(env) == env.module.SENT
    env.generator.assert_not_awaited()
    assert len(env.handler.delivered) == 1


@pytest.mark.asyncio
async def test_multi_edit_invalid_refs_do_not_claim_or_generate(tool_env):
    env = tool_env
    env.context_state.error = env.error("本轮参考图不属于当前问题")
    result = await call(env, {"prompt": "edit", "image_refs": ["img_1"]}, edit=True)
    assert "不属于" in result
    assert env.handler.writes == []
    env.editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_multi_edit_and_generate_issue_at_most_one_request(tool_env):
    env = tool_env
    params = {"prompt": "交换衣服", "image_refs": ["edit_ref_first", "edit_ref_second"]}
    results = await asyncio.gather(
        call(env, params, edit=True),
        call(env),
        call(env, params, edit=True),
    )
    assert env.editor.await_count + env.generator.await_count == 1
    assert len(env.handler.attempts) == 1
    assert all(result in {env.module.SENT, env.module.GENERATING} for result in results)


@pytest.mark.asyncio
async def test_edit_delivery_failure_keeps_output_without_regenerating_or_sending_twice(
    tool_env,
):
    env = tool_env
    env.handler.send_error = True
    result = await call(env, edit=True)
    assert result.startswith(env.module.SEND_FAILED) and "asset_ref=asset_" in result
    assert await call(env, edit=True) == result
    assert await call(env) == result
    env.editor.assert_awaited_once()
    env.generator.assert_not_awaited()
    env.remember_output.assert_awaited_once()
    assert len(env.handler.attempts) == 1


@pytest.mark.asyncio
async def test_edit_safe_and_unexpected_errors_never_send(tool_env):
    env = tool_env
    env.editor.side_effect = env.error("图片格式不受支持。")
    assert "图片格式不受支持" in await call(env, edit=True)
    env.editor.side_effect = RuntimeError("private upstream data")
    assert (
        await call(env, query_uuid="other-query", edit=True)
        == env.module.GENERATION_FAILED
    )
    assert env.handler.attempts == []
    env.remember_output.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_delivery_timeout_does_not_trigger_second_charge(
    tool_env, monkeypatch, edit
):
    env = tool_env
    env.handler.send_hangs = True
    monkeypatch.setattr(
        env.edit_module if edit else env.module, "REPLY_TIMEOUT_SECONDS", 0.01
    )
    result = await call(env, edit=edit)
    assert result.startswith(env.module.SEND_FAILED)
    assert await call(env, edit=edit) == result
    assert env.generator.await_count + env.editor.await_count == 1
    env.remember_output.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_generated_intermediate_returns_reusable_bytes_without_posting(tool_env, tmp_path, monkeypatch, edit):
    import base64, hashlib, io, json, time
    from PIL import Image as PillowImage
    from qunling.assets import AssetStore
    from qunling.scope import Scope
    env = tool_env
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path))
    binding = SimpleNamespace(instance_uuid="instance", workspace_uuid="workspace", placement_generation=1)
    env.tool.plugin.get_installation_binding = lambda: binding
    tenant = hashlib.sha256(json.dumps(["instance", "workspace"]).encode()).hexdigest()
    scope = Scope(tenant, "bot", "group:123", "member")
    context = {"version": 1, "scope": scope.as_dict(), "created": time.time(), "query_uuid": "query-one",
               "request_key": "query-one"}
    env.handler.variables[(1, "query-one")] = {"qunling.context.v1": context}
    out = io.BytesIO()
    PillowImage.new("RGB", (64, 64), "blue").save(out, format="PNG")
    encoded = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    env.generator.return_value = env.editor.return_value = SimpleNamespace(base64=encoded, url=None)
    monkeypatch.setattr(sys.modules["main"], "image_data_url", AsyncMock(return_value=encoded), raising=False)
    result = json.loads(await call(env, {"prompt": "蓝色素材", "deliver": False}, edit=edit))
    assert result["status"] == "created" and result["delivery_status"] == "not_requested"
    store = AssetStore.from_env()
    assert store.read(scope, result["asset_ref"]) == out.getvalue()
    store.require_visible(scope, result["asset_ref"], context)
    assert result["_qunling_task"]["can_continue"]
    assert env.handler.attempts == []
    assert "不要重新生图" in await call(env, {"prompt": "蓝色素材", "deliver": False}, edit=not edit)
    assert env.generator.await_count + env.editor.await_count == 1
