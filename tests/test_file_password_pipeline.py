"""Exercise password transport and actual scoped output without QQ/model calls."""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.sdk

from qunling.assets import AssetError, AssetStore
from qunling.jobs import JobService
from qunling.processing import run_operation, validate_operation
from qunling.scope import Scope

# Same synthetic member as the core decoder fixture; password fixture-pass.
ZIP_CRYPTO = (
    "UEsDBBQAAQAAAENPVV36aGugJAAAABgAAAAIAAAAbm90ZS50eHRECedNTDXf0ZwLtKbRan6o"
    "fm1MRkbSG38J8ym1IqJOo7tpxxBQSwECPwAUAAEAAABDT1Vd+mhroCQAAAAYAAAACAAk"
    "AAAAAAAAACAAAAAAAAAAbm90ZS50eHQKACAAAAAAAAEAGAD8zAOd/2DdAQAAAAAAAAAAAAAA"
    "AAAAAABQSwUGAAAAAAEAAQBaAAAASgAAAAAA"
)
SCOPE = Scope("fixture-workspace", "fixture-bot", "group:fixture", "fixture-user")


@pytest.fixture
def files(tmp_path):
    store = AssetStore(tmp_path / "assets")
    item = store.put(SCOPE, "encrypted.zip", base64.b64decode(ZIP_CRYPTO))
    return store, item["asset_ref"]


@pytest.mark.asyncio
async def test_encrypted_worker_password_retry_and_extract(files, monkeypatch):
    store, ref = files
    spawn = asyncio.create_subprocess_exec
    requests = []

    async def checked_spawn(*command, **kwargs):
        request = (Path(kwargs["cwd"]) / "request.json").read_text(encoding="utf-8")
        assert "fixture-pass" not in request
        assert "fixture-pass" not in repr(command) + repr(kwargs.get("env"))
        requests.append(json.loads(request))
        return await spawn(*command, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", checked_spawn)
    for password, code in [(None, "archive_password_required"), ("wrong", "archive_password_invalid")]:
        args = {"asset_ref": ref, **({"password": password} if password is not None else {})}
        with pytest.raises(AssetError) as caught:
            await run_operation(store, SCOPE, "inspect", args)
        assert caught.value.code == code
    result = await run_operation(store, SCOPE, "read", {
        "asset_ref": ref, "password": "fixture-pass", "selector": {"member": "note.txt"},
    })
    assert result["sections"][0]["text"] == "attachment password test"
    result = await run_operation(store, SCOPE, "extract", {
        "asset_ref": ref, "password": "fixture-pass", "member": "note.txt",
    })
    assert store.read(SCOPE, result["result_refs"][0]) == b"attachment password test"
    assert requests[-1]["password_supplied"] is True
    assert "password" not in requests[-1]["args"]


def test_passwords_cannot_enter_persistent_job_payloads(tmp_path, files):
    _, ref = files
    jobs = JobService(tmp_path / "jobs-root")
    with pytest.raises(AssetError) as caught:
        jobs.submit(SCOPE, "inspect", {"asset_ref": ref, "password": "fixture-pass"}, "request")
    assert caught.value.code == "ephemeral_input_required"
    assert not list(jobs.jobs.rglob("*.json"))
    assert not jobs.list(SCOPE)


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_password_tool_extract_delivers_scoped_real_bytes(files, tmp_path, monkeypatch, confirmed):
    store, ref = files
    monkeypatch.setenv("QUNLING_DATA_DIR", str(tmp_path / "unneeded-jobs"))
    source = Path(__file__).resolve().parents[1] / "custom-plugins/QQTextReader/pkg/file_tools.py"
    spec = importlib.util.spec_from_file_location("password_file_tools", source)
    tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tools)
    sent = []

    async def reply(chain, **kwargs):
        sent.append(chain)
        if not confirmed:
            raise TimeoutError("simulated QQ receipt timeout")

    context = {"created": time.time() + 1, "request_key": "password-query"}
    result = await tools.workspace(None, SimpleNamespace(reply=reply), context, SCOPE, store, {
        "operation": "extract", "arguments": {
            "asset_ref": ref, "member": "note.txt", "password": "fixture-pass",
        }, "deliver": True,
    })
    assert result["delivery"]["status"] == ("sent" if confirmed else "unconfirmed")
    assert store.read(SCOPE, result["result_refs"][0]) == b"attachment password test"
    if not confirmed:
        assert result["status"] == "partial"
        assert store.delivery(SCOPE, context["request_key"], result["result_refs"][0]) == "unconfirmed"
    assert base64.b64decode(sent[0].root[0].base64) == b"attachment password test"
    assert "fixture-pass" not in json.dumps(result)
    assert not (tmp_path / "unneeded-jobs").exists()
    other = Scope("fixture-workspace", "fixture-bot", "group:fixture", "other-user")
    with pytest.raises(AssetError):
        await tools.workspace(None, SimpleNamespace(reply=reply), context, other, store, {
            "operation": "read", "arguments": {"asset_ref": ref, "password": "fixture-pass"},
        })
    assert len(sent) == 1


@pytest.mark.parametrize("password", [None, 1, "", "bad\npass", "\ud800", "x" * 1025])
def test_password_validation_before_worker_creation(password):
    with pytest.raises(AssetError) as caught:
        validate_operation("inspect", {"asset_ref": "asset_" + "a" * 32, "password": password})
    assert caught.value.code == "invalid_parameters"
