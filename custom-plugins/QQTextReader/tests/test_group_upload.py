"""Exercise the real SDK event wire and Runner lifecycle without external I/O."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from langbot_plugin.api.definition.components.manifest import ComponentManifest
from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.entities.builtin.platform.events import PlatformSpecificEvent
from langbot_plugin.api.entities.builtin.runner.context import RunnerContext
from langbot_plugin.api.proxies.invocation import bind_invocation
from langbot_plugin.entities.io.context import InstallationBinding


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qq_text_group_upload_under_test", ROOT / "components/runners/group_upload.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NoHostAPI:
    def __getattr__(self, name):
        pytest.fail(f"The upload recorder must not call Host APIs: {name}")


class RecorderPlugin(BasePlugin):
    def __init__(self, handler):
        super().__init__()
        self.plugin_runtime_handler = handler
        self.captures = []

    def capture_group_upload(self, *args, **kwargs):
        self.captures.append((args, kwargs))


def context(
    *, event_changes=None, data_changes=None, metadata_changes=None, source="platform"
):
    data = {
        "group_id": 111,
        "user_id": "222",
        "file": {
            "id": "fixture-file-id",
            "name": "fixture.txt",
            "size": 11000,
            "busid": 102,
            "url": "https://example.invalid/private-download",
            "content": "private-file-body",
        },
        "instance_uuid": "untrusted-instance",
        "workspace_uuid": "untrusted-workspace",
        "placement_generation": 99,
    }
    data.update(data_changes or {})
    event = PlatformSpecificEvent(
        bot_uuid="fixture-bot",
        adapter_name="onebot-v11",
        action="notice.group_upload",
        data=data,
        source_platform_object={"private_raw": "must-not-cross-wire"},
    )
    event = event.model_copy(update=event_changes or {})
    metadata = {"bot_id": "fixture-bot", "workspace_id": "fixture-workspace"}
    metadata.update(metadata_changes or {})
    payload = {
        "run_id": "fixture-run",
        "trigger": {"type": "platform.specific", "source": source},
        "event": {
            "event_id": "fixture-event",
            "event_type": "platform.specific",
            "source": source,
            "data": event.model_dump(mode="json", exclude={"source_platform_object"}),
        },
        "input": {"text": "notice.group_upload", "contents": [], "attachments": []},
        "delivery": {"surface": "platform", "automatic_reply": False},
        "resources": {},
        "runtime": {"metadata": metadata},
    }
    restored = RunnerContext.model_validate(json.loads(json.dumps(payload)))
    assert restored.platform_event.source_platform_object is None
    return restored


def binding():
    return InstallationBinding(
        instance_uuid="fixture-instance",
        workspace_uuid="fixture-workspace",
        installation_uuid="fixture-installation",
        placement_generation=3,
        runtime_revision=2,
        artifact_digest="a" * 64,
    )


async def invoke(ctx, scoped=True):
    handler = object()
    plugin = RecorderPlugin(handler)
    runner = MODULE.GroupUpload()
    runner.plugin = plugin
    runner.bind_runtime(
        plugin_runtime_handler=handler, plugin_identity="local/QQTextReader"
    )
    runner.get_run_api = lambda active_ctx: NoHostAPI()
    with bind_invocation(handler, config={}, binding=binding() if scoped else None):
        results = [result async for result in runner.invoke(ctx)]
    assert [result.type for result in results] == ["run.completed"]
    wire = json.dumps([result.model_dump() for result in results])
    for value in ("private-", "fixture-file-id", "fixture.txt", "https://"):
        assert value not in wire
    assert ctx._api is None
    return plugin.captures


@pytest.mark.asyncio
async def test_sdk_invocation_captures_only_whitelisted_metadata_with_trusted_scope():
    captures = await invoke(context())
    assert captures == [
        (
            (
                "fixture-bot",
                "111",
                "222",
                {
                    "id": "fixture-file-id",
                    "name": "fixture.txt",
                    "size": 11000,
                    "busid": 102,
                },
            ),
            {
                "instance_uuid": "fixture-instance",
                "workspace_uuid": "fixture-workspace",
                "placement_generation": 3,
            },
        )
    ]


@pytest.mark.asyncio
async def test_legacy_dedicated_scope_uses_only_host_workspace_metadata():
    captures = await invoke(context(), scoped=False)
    assert captures[0][1] == {
        "instance_uuid": None,
        "workspace_uuid": "fixture-workspace",
        "placement_generation": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "api"},
        {"event_changes": {"adapter_name": "other-platform"}},
        {"event_changes": {"action": "notice.group_increase"}},
        {"event_changes": {"bot_uuid": ""}},
        {"data_changes": {"group_id": True}},
        {"data_changes": {"user_id": "0"}},
        {"data_changes": {"group_id": "９９９"}},
        {"data_changes": {"file": None}},
        {"data_changes": {"file": {"id": "fixture", "name": "x" * 256}}},
        {"data_changes": {"file": {"id": "x" * 4097, "name": "x.txt"}}},
        {"data_changes": {"file": {"id": "fixture", "name": "x.txt\n"}}},
        {"data_changes": {"file": {"id": "fixture", "name": "x.txt", "size": -1}}},
        {"data_changes": {"file": {"id": "fixture", "name": "x.txt", "busid": True}}},
        {"metadata_changes": {"bot_id": "different-bot"}},
        {"metadata_changes": {"workspace_id": "different-workspace"}},
    ],
)
async def test_unrelated_or_invalid_uploads_are_ignored_without_api_calls(kwargs):
    assert await invoke(context(**kwargs)) == []


@pytest.mark.asyncio
async def test_optional_file_size_and_busid_are_not_invented():
    captures = await invoke(
        context(data_changes={"file": {"id": "fixture", "name": "x.txt"}})
    )
    assert captures[0][0][3] == {"id": "fixture", "name": "x.txt"}


@pytest.mark.asyncio
async def test_file_metadata_accepts_host_and_core_length_limits():
    file = {"id": "x" * 4096, "name": "x" * 251 + ".txt"}
    captures = await invoke(context(data_changes={"file": file}))
    assert captures[0][0][3] == file


def test_component_manifest_uses_event_runner_contract():
    relative = "components/runners/group_upload.yaml"
    raw = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    manifest = ComponentManifest(
        owner="local/QQTextReader", manifest=raw, rel_path=relative
    )
    assert manifest.kind == "Runner"
    assert manifest.metadata.name == "group_upload"
    assert manifest.spec["usages"] == ["event"]
    assert manifest.spec["events"] == ["platform.specific"]
    assert manifest.spec["config"] == []
