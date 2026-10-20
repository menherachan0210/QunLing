import asyncio
import base64
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from pkg import jobs
from pkg.jobs import JobError, JobManager, scope_key
from pkg.labelplus import TranslationError


def archive_bytes(count=2):
    buf, img = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(img, format="PNG")
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(count):
            z.writestr(f"{i + 1:03}.png", img.getvalue())
    return buf.getvalue()


class Plugin:
    def get_config(self):
        return {}

    def __init__(self):
        self.storage = {}
        self.sends = []
        self.fail_send = False

    async def set_plugin_storage(self, key, value):
        self.storage[key] = value

    async def get_plugin_storage(self, key):
        return self.storage.get(key, b"")

    async def get_plugin_storage_keys(self):
        return list(self.storage)

    async def delete_plugin_storage(self, key):
        self.storage.pop(key, None)

    def spawn_background(self, factory, timeout):
        assert timeout == 7200
        return asyncio.create_task(factory())

    async def send_message(self, bot, kind, target, chain):
        self.sends.append((bot, kind, target, chain))
        if self.fail_send:
            raise RuntimeError("private upstream exception")


@pytest.fixture
def env(monkeypatch):
    calls = []

    async def translate(plugin, model, prepared, previous):
        calls.append(prepared.index)
        return {
            "status": "ok",
            "labels": [{"x": 0.5, "y": 0.4, "group": 1, "text": "合成测试中文译文。"}],
        }

    monkeypatch.setattr(jobs, "translate_page", translate)
    plugin = Plugin()
    session = SimpleNamespace(
        bot_uuid="bot", chat_type="group", chat_id="123", sender_id="456"
    )
    return plugin, JobManager(plugin), session, calls


async def start(manager, session, count=2):
    return await manager.start(
        session, SimpleNamespace(name="合成.zip"), archive_bytes(count), "vision-model"
    )


@pytest.mark.asyncio
async def test_background_output_preserves_sender_target_and_no_duplicate_billing(env):
    plugin, manager, session, calls = env
    result = await start(manager, session)
    assert result["status"] == "queued"
    await manager.tasks[scope_key(session)]
    assert calls == [0, 1]
    bot, kind, target, chain = plugin.sends[0]
    assert (bot, kind, target) == ("bot", "group", "123")
    text = base64.b64decode(chain[0].base64).decode("utf-8-sig")
    assert "合成测试中文译文" in text and "[001.png]" in text
    assert not any(".zip." in key for key in plugin.storage)
    assert (await start(manager, session))["status"] == "completed"
    assert calls == [0, 1] and len(plugin.sends) == 1
    other = SimpleNamespace(**{**vars(session), "sender_id": "789"})
    assert (await manager.operate(other, "status"))["status"] == "missing"


@pytest.mark.asyncio
async def test_partial_resume_only_failed_pages(env, monkeypatch):
    plugin, manager, session, calls = env
    original = jobs.translate_page

    async def fail_one(plugin, model, prepared, previous):
        if prepared.index == 1:
            raise TranslationError("该页翻译超时；未自动重试。")
        return await original(plugin, model, prepared, previous)

    monkeypatch.setattr(jobs, "translate_page", fail_one)
    await start(manager, session)
    await manager.tasks[scope_key(session)]
    assert (await manager.operate(session, "status"))["status"] == "partial"
    assert "待校对" in plugin.sends[0][3][0].name
    assert "本页未完成翻译" in base64.b64decode(plugin.sends[0][3][0].base64).decode(
        "utf-8-sig"
    )
    monkeypatch.setattr(jobs, "translate_page", original)
    await manager.operate(session, "resume")
    await manager.tasks[scope_key(session)]
    assert calls == [0, 1]
    assert (await manager.operate(session, "status"))["status"] == "completed"


@pytest.mark.asyncio
async def test_restart_keeps_progress_but_never_auto_resumes(env, monkeypatch):
    plugin, manager, session, calls = env
    entered = asyncio.Event()
    original = jobs.translate_page

    async def block(plugin, model, prepared, previous):
        if prepared.index == 1:
            entered.set()
            await asyncio.Event().wait()
        return await original(plugin, model, prepared, previous)

    monkeypatch.setattr(jobs, "translate_page", block)
    await start(manager, session)
    await entered.wait()
    await manager.operate(session, "cancel")
    # A crash may leave running on disk. A new manager treats that as interrupted.
    job = manager.jobs[scope_key(session)]
    job["status"] = "running"
    await manager.save(job)
    fresh = JobManager(plugin)
    result = await fresh.operate(session, "status")
    assert result["status"] == "interrupted" and result["completed_pages"] == 1
    assert not fresh.tasks and calls == [0]
    monkeypatch.setattr(jobs, "translate_page", original)
    await fresh.operate(session, "resume")
    await fresh.tasks[scope_key(session)]
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_delivery_failure_does_not_retry_until_explicit_result(env):
    plugin, manager, session, calls = env
    plugin.fail_send = True
    await start(manager, session)
    await manager.tasks[scope_key(session)]
    assert (await manager.operate(session, "status"))["delivery"] == "unknown"
    assert len(plugin.sends) == 1
    assert "private" not in json.dumps(
        manager.snapshot(manager.jobs[scope_key(session)])
    )
    plugin.fail_send = False
    await manager.operate(session, "result")
    assert len(plugin.sends) == 2 and calls == [0, 1]
    assert (await manager.operate(session, "status"))["error"] == ""


@pytest.mark.asyncio
async def test_three_failures_stop_billing_and_mark_remaining(env, monkeypatch):
    plugin, manager, session, calls = env

    async def fail(plugin, model, prepared, previous):
        calls.append(prepared.index)
        raise TranslationError("请求失败")

    monkeypatch.setattr(jobs, "translate_page", fail)
    await start(manager, session, 5)
    await manager.tasks[scope_key(session)]
    result = await manager.operate(session, "status")
    assert result["failed_pages"] == 3 and result["pending_pages"] == 2
    assert calls == [0, 1, 2]
    text = base64.b64decode(plugin.sends[0][3][0].base64).decode("utf-8-sig")
    assert text.count("本页未完成翻译") == 5


@pytest.mark.asyncio
async def test_delete_only_current_scope(env):
    plugin, manager, session, _ = env
    await start(manager, session)
    await manager.tasks[scope_key(session)]
    assert (await manager.operate(session, "delete"))["status"] == "deleted"
    assert not plugin.storage


@pytest.mark.asyncio
async def test_only_two_active_jobs(env, monkeypatch):
    plugin, manager, session, _ = env

    async def block(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "translate_page", block)
    await start(manager, session)
    other = SimpleNamespace(**{**vars(session), "sender_id": "789"})
    await start(manager, other)
    third = SimpleNamespace(**{**vars(session), "sender_id": "999"})
    with pytest.raises(JobError):
        await start(manager, third)
    await manager.operate(session, "cancel")
    await manager.operate(other, "cancel")


@pytest.mark.asyncio
async def test_cached_jobs_expire_and_first_missing_key_does_not_read(env):
    plugin, manager, session, _ = env
    original_get = plugin.get_plugin_storage

    async def strict_get(key):
        if key not in plugin.storage:
            raise RuntimeError("Storage key missing")
        return await original_get(key)

    plugin.get_plugin_storage = strict_get
    assert (await manager.operate(session, "status"))["status"] == "missing"
    await start(manager, session)
    await manager.tasks[scope_key(session)]
    manager.jobs[scope_key(session)]["created"] -= jobs.RETENTION + 1
    assert (await manager.operate(session, "status"))["status"] == "missing"
    assert not plugin.storage


@pytest.mark.asyncio
async def test_archive_cleanup_failure_keeps_output_available(env):
    plugin, manager, session, _ = env
    original_delete = plugin.delete_plugin_storage

    async def fail_archive_cleanup(key):
        if ".zip." in key:
            raise RuntimeError("storage offline")
        await original_delete(key)

    plugin.delete_plugin_storage = fail_archive_cleanup
    await start(manager, session)
    await manager.tasks[scope_key(session)]
    result = await manager.operate(session, "status")
    assert result["status"] == "completed" and result["delivery"] == "sent"
    assert len(plugin.sends) == 1


@pytest.mark.asyncio
async def test_failed_chunk_write_cleans_full_intended_set(env):
    plugin, manager, session, _ = env
    original_set = plugin.set_plugin_storage

    async def failed_write(key, value):
        if ".zip." in key:
            raise RuntimeError("interrupted upload")
        await original_set(key, value)

    plugin.set_plugin_storage = failed_write
    with pytest.raises(JobError):
        await start(manager, session)
    assert not plugin.storage and not manager.tasks
