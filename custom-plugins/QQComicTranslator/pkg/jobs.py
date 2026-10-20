"""Bounded, checkpointed comic jobs. All destinations come from captured events."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid

from langbot_plugin.api.entities.builtin.platform.message import (
    File,
    MessageChain,
    Plain,
)

from pkg.archive import ArchiveError, open_archive
from pkg.labelplus import TranslationError, output_name, render_labelplus
from pkg.translator import translate_page

PREFIX = "qqcomic.v1."
CHUNK_SIZE = 512 * 1024
RETENTION = 7 * 86400
MAX_JOBS = 16
MAX_ACTIVE = 2
MAX_ARCHIVE_STORAGE = 320 * 1024 * 1024


def scope_key(session):
    values = [
        getattr(session, field)
        for field in ("bot_uuid", "chat_type", "chat_id", "sender_id")
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


class JobError(Exception):
    pass


class JobManager:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()
        self.jobs = {}
        self.tasks = {}

    async def save(self, job):
        job["updated"] = time.time()
        await self.plugin.set_plugin_storage(
            PREFIX + job["scope"] + ".json",
            json.dumps(job, ensure_ascii=False).encode(),
        )

    async def load(self, scope):
        if scope in self.jobs:
            cached = self.jobs[scope]
            if time.time() - cached["created"] > RETENTION:
                await self.delete(cached)
                return None
            return cached
        try:
            key = PREFIX + scope + ".json"
            if key not in await self.plugin.get_plugin_storage_keys():
                return None
            raw = await self.plugin.get_plugin_storage(key)
        except Exception:
            raise JobError("暂时无法读取已保存的翻译任务，请稍后重试。") from None
        if not raw:
            return None
        try:
            job = json.loads(raw)
            if job["scope"] != scope or len(job["pages"]) > 100:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise JobError("翻译任务记录不可用，请重新上传压缩包。") from None
        if time.time() - job["created"] > RETENTION:
            await self.delete(job)
            return None
        if job["status"] in {"queued", "running"}:
            job["status"] = "interrupted"
            job["error"] = (
                "服务重启或任务中断；已完成页保留，明确要求继续后再处理未完成页。"
            )
        if job.get("delivery") == "sending":
            job["delivery"] = "unknown"
        self.jobs[scope] = job
        return job

    async def delete_archive(self, job):
        for i in range(job.get("chunks", 0)):
            await self.plugin.delete_plugin_storage(PREFIX + job["id"] + f".zip.{i}")
        job["chunks"] = 0
        job["archive_bytes"] = 0

    async def delete(self, job):
        await self.delete_archive(job)
        await self.plugin.delete_plugin_storage(PREFIX + job["scope"] + ".json")
        self.jobs.pop(job["scope"], None)

    async def inventory(self):
        keys = await self.plugin.get_plugin_storage_keys()
        scopes = [
            key[len(PREFIX) : -5]
            for key in keys
            if key.startswith(PREFIX) and key.endswith(".json")
        ]
        jobs = []
        for scope in scopes:
            job = await self.load(scope)
            if job:
                jobs.append(job)
        return jobs

    def snapshot(self, job):
        if job is None:
            return {
                "status": "missing",
                "message": "本会话中没有你的近期漫画翻译任务。",
            }
        completed = sum(page.get("result") is not None for page in job["pages"])
        failed = sum(bool(page.get("error")) for page in job["pages"])
        return {
            "status": job["status"],
            "archive": job["name"],
            "total_pages": len(job["pages"]),
            "completed_pages": completed,
            "failed_pages": failed,
            "pending_pages": len(job["pages"]) - completed - failed,
            "format": "LabelPlus",
            "delivery": job.get("delivery", "pending"),
            "error": "\n".join(
                filter(None, [job.get("error", ""), job.get("delivery_error", "")])
            ),
            "message": "任务在后台运行，完成后会回传 TXT。可随时询问翻译进度或取消。"
            if job["status"] in {"queued", "running"}
            else "",
        }

    def launch(self, job):
        # The local SDK supplies a fresh, revocable installation-level lifetime.
        # Never use a finished QueryBasedAPIProxy in this task.
        task = self.plugin.spawn_background(lambda: self.run(job), timeout=7200)
        self.tasks[job["scope"]] = task
        task.add_done_callback(
            lambda done: self.tasks.pop(job["scope"], None)
            if self.tasks.get(job["scope"]) is done
            else None
        )

    async def start(self, session, attachment, raw, model_uuid, *, page_names=None):
        scope = scope_key(session)
        digest = hashlib.sha256(raw).hexdigest()
        async with self.lock:
            existing = await self.load(scope)
            if scope in self.tasks:
                return self.snapshot(existing)
            if existing and existing.get("digest") == digest:
                result = self.snapshot(existing)
                result["message"] = (
                    "此压缩包已有任务，不重复计费；可查询结果，或明确要求继续未完成页。"
                )
                return result
            if sum(not task.done() for task in self.tasks.values()) >= MAX_ACTIVE:
                raise JobError("已有两个漫画翻译任务运行，请稍后再试。")
            jobs = await self.inventory()
            if not existing and len(jobs) >= MAX_JOBS:
                raise JobError("近期漫画任务已达到存储上限，请清理任务或等待七天到期。")
            stored = sum(
                item.get("archive_bytes", 0) for item in jobs if item is not existing
            )
            if stored + len(raw) > MAX_ARCHIVE_STORAGE:
                raise JobError("漫画任务压缩包缓存已满，请删除不再需要的任务后重试。")
            with await asyncio.to_thread(
                open_archive, raw, self.plugin.get_config().get("seven_zip_path")
            ) as archive:
                if page_names is not None and (
                    not isinstance(page_names, (list, tuple))
                    or len(page_names) != len(archive.pages)
                ):
                    raise JobError("漫画图片与原文件名的对应关系无效，未开始翻译。")
                pages = [
                    {
                        "index": page.index,
                        "name": page_names[page.index] if page_names is not None else page.basename,
                        "result": None,
                        "error": "",
                    }
                    for page in archive.pages
                ]
            render_labelplus(pages)  # Reject incompatible names before model usage.
            if existing:
                await self.delete(existing)
            target = {
                field: getattr(session, field)
                for field in ("bot_uuid", "chat_type", "chat_id", "sender_id")
            }
            job = {
                "id": uuid.uuid4().hex,
                "scope": scope,
                "target": target,
                "name": attachment.name,
                "digest": digest,
                "created": time.time(),
                "status": "queued",
                "pages": pages,
                "model_uuid": model_uuid,
                "chunks": (len(raw) + CHUNK_SIZE - 1) // CHUNK_SIZE,
                "archive_bytes": len(raw),
                "delivery": "pending",
                "delivery_error": "",
                "error": "",
            }
            try:
                # Record the full intended chunk set first so even an interrupted
                # upload has an owner and can be removed after restart.
                await self.save(job)
                for i, offset in enumerate(range(0, len(raw), CHUNK_SIZE)):
                    await self.plugin.set_plugin_storage(
                        PREFIX + job["id"] + f".zip.{i}",
                        raw[offset : offset + CHUNK_SIZE],
                    )
                self.jobs[scope] = job
                self.launch(job)
            except Exception:
                await self.delete(job)
                raise JobError("翻译任务创建失败，未开始翻译。") from None
            return self.snapshot(job)

    async def raw_archive(self, job):
        if not job.get("chunks"):
            raise JobError("压缩包缓存已清理，请重新上传。")
        chunks = [
            await self.plugin.get_plugin_storage(PREFIX + job["id"] + f".zip.{i}")
            for i in range(job["chunks"])
        ]
        raw = b"".join(chunks)
        if (
            len(raw) != job["archive_bytes"]
            or hashlib.sha256(raw).hexdigest() != job["digest"]
        ):
            raise JobError("压缩包缓存不完整，请重新上传。")
        return raw

    async def run(self, job):
        try:
            job["status"] = "running"
            await self.save(job)
            raw = await self.raw_archive(job)
            consecutive_failures = 0
            previous = ""
            with await asyncio.to_thread(
                open_archive, raw, self.plugin.get_config().get("seven_zip_path")
            ) as archive:
                for page in job["pages"]:
                    if page.get("result") is not None:
                        previous = "\n".join(
                            item["text"] for item in page["result"]["labels"]
                        )[-2000:]
                        continue
                    try:
                        prepared = await asyncio.to_thread(
                            archive.prepare_page, page["index"]
                        )
                        page["result"] = await translate_page(
                            self.plugin, job["model_uuid"], prepared, previous
                        )
                        page["error"] = ""
                        previous = "\n".join(
                            item["text"] for item in page["result"]["labels"]
                        )[-2000:]
                        consecutive_failures = 0
                    except (ArchiveError, TranslationError) as exc:
                        page["error"] = str(exc)
                        consecutive_failures += 1
                    await self.save(job)
                    if consecutive_failures >= 3:
                        job["error"] = (
                            "连续三页处理失败，已停止后续请求；可检查服务后继续。"
                        )
                        break
            incomplete = any(page.get("result") is None for page in job["pages"])
            job["status"] = "partial" if incomplete else "completed"
            render_labelplus(job["pages"])
            await self.save(job)
            # Completed pages never need to be billed again. Keep failed jobs' ZIP
            # for an explicit resume, but remove successful archives promptly.
            if not incomplete:
                try:
                    await self.delete_archive(job)
                    await self.save(job)
                except Exception:
                    # Cache cleanup is independent of a valid paid translation.
                    job["error"] = (
                        "翻译已完成；临时压缩包清理稍后重试，可正常取回结果。"
                    )
            await self.deliver(job)
        except asyncio.CancelledError:
            job["status"] = "interrupted"
            job["error"] = "任务已停止，完成页已保留；继续处理需要明确请求。"
            try:
                await self.save(job)
            except Exception:
                pass
            raise
        except Exception as exc:
            if job["status"] not in {"completed", "partial"}:
                job["status"] = "failed"
            job["error"] = (
                str(exc)
                if isinstance(exc, (JobError, TranslationError, ArchiveError))
                else "任务处理失败，完成页已保留。"
            )
            try:
                await self.save(job)
                await self.send_notice(
                    job, "漫画翻译未全部完成，已保留进度。请询问翻译进度查看状态。"
                )
            except Exception:
                pass

    async def send_notice(self, job, text):
        target = job["target"]
        await self.plugin.send_message(
            target["bot_uuid"],
            target["chat_type"],
            target["chat_id"],
            MessageChain([Plain(text=text)]),
        )

    async def deliver(self, job, *, query_api=None, explicit=False):
        if job["status"] not in {"completed", "partial"}:
            return
        if job.get("delivery") == "sending":
            return
        if job.get("delivery") in {"sent", "unknown"} and not explicit:
            return
        raw = render_labelplus(job["pages"])
        partial = job["status"] != "completed"
        chain = MessageChain(
            [
                File(
                    name=output_name(job["name"], partial),
                    base64=base64.b64encode(raw).decode("ascii"),
                )
            ]
        )
        job["delivery"] = "sending"
        await self.save(job)
        try:
            if query_api is not None:
                await query_api.reply(chain, quote_origin=False)
            else:
                target = job["target"]
                await self.plugin.send_message(
                    target["bot_uuid"], target["chat_type"], target["chat_id"], chain
                )
        except Exception:
            job["delivery"] = "unknown"
            job["delivery_error"] = (
                "TXT 发送未能确认；为避免重复发送未自动重试，可明确要求重发结果。"
            )
        else:
            job["delivery"] = "sent"
            job["delivery_error"] = ""
        await self.save(job)
        if partial and query_api is None and job["delivery"] == "sent":
            try:
                await self.send_notice(
                    job,
                    "这份 LabelPlus TXT 含未完成页，已在对应图片下标注。可询问进度或要求继续未完成页。",
                )
            except Exception:
                pass

    async def operate(self, session, action, query_api=None):
        scope = scope_key(session)
        async with self.lock:
            job = await self.load(scope)
            if not job:
                return self.snapshot(None)
            task = self.tasks.get(scope)
            if action == "cancel":
                if task and not task.done():
                    task.cancel()
                    # The worker checkpoints outside this manager lock.
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                return self.snapshot(job)
            if action == "delete":
                if task and not task.done():
                    raise JobError("请先取消运行中的翻译任务，再删除缓存。")
                await self.delete(job)
                return {
                    "status": "deleted",
                    "message": "本会话中你的漫画翻译缓存已删除。",
                }
            if action == "resume":
                if task and not task.done():
                    return self.snapshot(job)
                if job["status"] == "completed":
                    return self.snapshot(job)
                if sum(not task.done() for task in self.tasks.values()) >= MAX_ACTIVE:
                    raise JobError("已有两个漫画翻译任务运行，请稍后再试。")
                if not job.get("chunks"):
                    raise JobError("压缩包缓存已清理，请重新上传。")
                job.update(
                    status="queued", error="", delivery="pending", delivery_error=""
                )
                for page in job["pages"]:
                    if page.get("result") is None:
                        page["error"] = ""
                await self.save(job)
                self.launch(job)
            if action == "result":
                await self.deliver(job, query_api=query_api, explicit=True)
            return self.snapshot(job)
