"""Exercise installed read bridges and card conversion without QQ/network calls."""
from __future__ import annotations

import ast
import datetime
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "src/langbot/pkg/platform/adapters/aiocqhttp/"
PINNED_HOST = "502b38130f9878b405498b93e4203593573eb19b"
NAMES = ("onebot-group-information.patch", "onebot-forward-placeholder.patch")
SPEC = importlib.util.spec_from_file_location("group_information_patcher", ROOT / "scripts/Apply-RuntimePatches.py")
patcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = patcher
SPEC.loader.exec_module(patcher)

# Original 4.11.0 method/branch context, kept local so the basic publication
# regression does not require the ignored LangBot source checkout.
ADAPTER = """class AiocqhttpAdapter:
    async def call_platform_api(self, action: str, params: dict = {}) -> dict:
        handler = PLATFORM_API_MAP.get(action)
        if handler is None:
            raise NotSupportedError(f'call_platform_api:{action}')
        return await handler(self.bot, params)
"""
CONVERTER = """class AiocqhttpMessageConverter:
    @staticmethod
    async def target2yiri(message, message_id=-1, timestamp=None, bot=None):
        components = []
        for segment in aiocqhttp.Message(message):
            if segment.type == 'text':
                components.append(platform_message.Plain(text=segment.data.get('text', '')))
            elif segment.type == 'json':
                try:
                    raw = segment.data.get('data', {})
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    components.append(platform_message.Plain(text=str(raw)))
                except Exception:
                    components.append(platform_message.Plain(text='[收到一张JSON卡片]'))

            else:
                components.append(platform_message.Unknown(text=f'{segment.type}:{segment.data}'))

        return platform_message.MessageChain(components)
"""


def write(root, path, source):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def plans(root, names=NAMES):
    return [patcher.build_plan(name, root, (ROOT / "patches" / name).read_text(encoding="utf-8"), "host")
            for name in names]


def method(source, class_name, method_name, globals):
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    function = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name)
    function.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<installed QQ bridge>", "exec"), globals)
    return globals[method_name]


@pytest.fixture
def installed(tmp_path):
    write(tmp_path, PREFIX + "adapter.py", ADAPTER)
    write(tmp_path, PREFIX + "message_converter.py", CONVERTER)
    patcher.execute_plans(plans(tmp_path), apply=True)
    return tmp_path


def test_new_patches_preflight_together_and_roundtrip_without_partial_dependency(tmp_path):
    originals = {PREFIX + "adapter.py": ADAPTER, PREFIX + "message_converter.py": CONVERTER}
    for path, source in originals.items():
        write(tmp_path, path, source)
    first = plans(tmp_path)
    assert [plan.state for plan in first] == ["ready", "ready"]
    patcher.execute_plans(first, apply=False)
    assert {path: (tmp_path / path).read_text(encoding="utf-8") for path in originals} == originals
    patcher.execute_plans(first, apply=True)
    applied = {path: (tmp_path / path).read_text(encoding="utf-8") for path in originals}
    second = plans(tmp_path)
    assert [plan.state for plan in second] == ["already-applied", "already-applied"]
    patcher.execute_plans(second, apply=True)
    assert {path: (tmp_path / path).read_text(encoding="utf-8") for path in originals} == applied
    for plan in reversed(second):
        assert patcher.git_apply(tmp_path, plan.payload, "--reverse", "--check").returncode == 0
        assert patcher.git_apply(tmp_path, plan.payload, "--reverse").returncode == 0
    assert {path: (tmp_path / path).read_text(encoding="utf-8") for path in originals} == originals


def test_new_patches_are_required_host_installs():
    for name in NAMES:
        assert (name, "host", False) in patcher.PATCHES


@pytest.mark.asyncio
async def test_installed_adapter_routes_group_reads_before_legacy_and_preserves_other_apis(installed, monkeypatch):
    from qunling import qq_group_api
    read = AsyncMock(return_value={"safe": True})
    monkeypatch.setattr(qq_group_api, "read_group_api", read)
    legacy_read = AsyncMock(side_effect=AssertionError("legacy must not read new group endpoints"))
    status = AsyncMock(return_value={"online": True})
    mapping = {name: legacy_read for name in qq_group_api.READ_ACTIONS}
    mapping["get_status"] = status
    namespace = {"PLATFORM_API_MAP": mapping, "NotSupportedError": ValueError}
    call = method((installed / (PREFIX + "adapter.py")).read_text(encoding="utf-8"),
                  "AiocqhttpAdapter", "call_platform_api", namespace)
    self = SimpleNamespace(bot=object())
    for action in qq_group_api.READ_ACTIONS:
        params = {"group_id": "200", "file_id": "selected", "busid": 102}
        assert await call(self, action, params) == {"safe": True}
        read.assert_awaited_with(self.bot, action, params)
    legacy_read.assert_not_awaited()
    assert await call(self, "get_status", {}) == {"online": True}
    status.assert_awaited_once_with(self.bot, {})
    with pytest.raises(ValueError, match="call_platform_api:unknown"):
        await call(self, "unknown", {})


def converter(source):
    namespace = {
        "aiocqhttp": SimpleNamespace(Message=lambda data: [SimpleNamespace(type=row["type"], data=row["data"]) for row in data]),
        "platform_message": SimpleNamespace(
            Plain=lambda **kw: SimpleNamespace(type="Plain", **kw),
            Unknown=lambda **kw: SimpleNamespace(type="Unknown", **kw),
            Source=lambda **kw: SimpleNamespace(type="Source", **kw), MessageChain=list),
        "json": json, "datetime": datetime,
    }
    return method(source, "AiocqhttpMessageConverter", "target2yiri", namespace)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forward", "json"])
async def test_installed_converter_uses_readable_card_placeholder_without_fetching_or_leaking_ids(installed, kind):
    data = {"id": "sensitive-resource-id", "content": [{"sender": {"user_id": 123}}]}
    if kind == "json":
        data = {"data": json.dumps({"app": "com.tencent.multimsg", "meta": {"detail": {"resid": "sensitive-resource-id"}}})}
    call = converter((installed / (PREFIX + "message_converter.py")).read_text(encoding="utf-8"))
    bot = SimpleNamespace(call_action=AsyncMock(), get_msg=AsyncMock())
    result = await call([{"type": kind, "data": data}], bot=bot)
    assert len(result) == 1 and result[0].type == "Plain"
    assert "read_forwarded_messages" in result[0].text
    assert "sensitive-resource-id" not in result[0].text
    bot.call_action.assert_not_awaited()
    bot.get_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_pinned_sources_apply_with_existing_image_patch_and_execute_actual_converter(tmp_path):
    host = ROOT / "langbot"
    paths = [PREFIX + "adapter.py", PREFIX + "message_converter.py",
             "src/langbot/pkg/pipeline/preproc/preproc.py"]
    originals = {}
    for path in paths:
        result = subprocess.run(["git", "-C", str(host), "show", f"{PINNED_HOST}:{path}"],
                                capture_output=True, timeout=10)
        if result.returncode:
            pytest.skip("Optional pinned LangBot checkout is not installed")
        originals[path] = result.stdout.decode("utf-8")
        write(tmp_path, path, originals[path])
    names = ("onebot-image-vision.patch", *NAMES)
    first = plans(tmp_path, names)
    assert all(plan.state == "ready" for plan in first)
    patcher.execute_plans(first, apply=True)
    assert all(plan.state == "already-applied" for plan in plans(tmp_path, names))
    actual_converter = converter((tmp_path / (PREFIX + "message_converter.py")).read_text(encoding="utf-8"))
    result = await actual_converter([{"type": "forward", "data": {"id": "private-id"}}])
    assert [row.type for row in result] == ["Source", "Plain"]
    assert "read_forwarded_messages" in result[1].text and "private-id" not in result[1].text
    for plan in reversed(plans(tmp_path, names)):
        assert patcher.git_apply(tmp_path, plan.payload, "--reverse").returncode == 0
    assert {path: (tmp_path / path).read_text(encoding="utf-8") for path in originals} == originals
