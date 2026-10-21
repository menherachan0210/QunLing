"""Bounded QQ foreground scheduling; all identities come from the host Query.

Ordinary receive events use a small separate queue. Actual tool execution parks
the owning foreground lease; it never creates extra model execution slots.
"""
from __future__ import annotations

import asyncio
from collections import deque, OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import time
import uuid

_bypass = ContextVar("qq_admission_bypass", default=False)
_notice = ContextVar("qq_control_notice", default=False)


def is_control_notice():
    return _notice.get()


def _attr(obj, name, value):
    object.__setattr__(obj, name, value)


def _scope(query):
    from langbot.pkg.pipeline.pool import get_query_execution_context
    ctx = get_query_execution_context(query)
    return (ctx.instance_uuid, ctx.workspace_uuid, ctx.placement_generation,
            query.bot_uuid, str(query.launcher_id), str(query.sender_id))


def _complete(query):
    from qunling.proactive.host import completed
    completed(query.adapter, query.message_event)


def protected(query):
    return isinstance(getattr(query, "_qq_task", None), Task)


def _explicit_raw(query):
    """Only trusted mentions/default command prefix warrant ingress errors."""
    from langbot_plugin.api.entities.builtin.platform.message import At, Plain
    chain = query.message_chain
    return (any(isinstance(part, At) and str(part.target) == str(query.adapter.bot_account_id) for part in chain)
            or any(isinstance(part, Plain) and part.text.lstrip().startswith("/ai") for part in chain))


def _service(ap):
    service = getattr(ap, "_qq_dispatch", None)
    if service is None:
        service = Dispatch(ap)
        ap._qq_dispatch = service
    return service


def _managed_app(pool, arguments):
    if not os.environ.get("QUNLING_DATA_DIR"):
        return None
    runtime = getattr(arguments["adapter"], "_runtime", None)
    ap = getattr(runtime, "ap", None)
    if (ap is not None and getattr(ap, "query_pool", None) is pool and
        getattr(getattr(runtime, "bot_entity", None), "adapter", None) == "onebot-v11" and
        getattr(arguments["launcher_type"], "value", None) == "group"):
        return ap
    return None


async def forward_ingress(ap, arguments):
    """QQ group aggregation is disabled; avoid its unbounded preparatory I/O."""
    if _managed_app(ap.query_pool, arguments) is None:
        return False
    names = ("bot_uuid", "launcher_type", "launcher_id", "sender_id", "message_event", "message_chain",
             "adapter", "pipeline_uuid", "routed_by_rule", "variables", "execution_context")
    kwargs = {key: arguments[key] for key in names}
    from langbot.pkg.telemetry import trace
    ingress = trace.current()
    if ingress is not None:
        kwargs["variables"] = {**(kwargs["variables"] or {}), "_ingress_event_id": ingress.event_id}
    await ap.query_pool.add_query(**kwargs)
    return True


async def _authorized_notice(ap, query):
    from langbot.pkg.pipeline.pool import get_query_execution_context
    from langbot.pkg.core.task_boundary import run_in_workspace_uow
    ctx = get_query_execution_context(query)
    runtime = getattr(query.adapter, "_runtime", None)
    active = getattr(runtime, "execution_context", None)
    if (runtime is None or active is None or runtime.ap is not ap or runtime.adapter is not query.adapter or
        runtime.enable is not True or runtime.bot_entity.enable is not True or
        runtime.bot_entity.uuid != query.bot_uuid or
        (ctx.instance_uuid, ctx.workspace_uuid, ctx.placement_generation) !=
        (active.instance_uuid, active.workspace_uuid, active.placement_generation)):
        raise RuntimeError("QQ notification authorization changed")

    async def check():
        binding = await ap.workspace_service.get_execution_binding(ctx.workspace_uuid,
            expected_generation=ctx.placement_generation)
        info = await ap.bot_service.get_bot(ctx, query.bot_uuid, include_secret=False)
        current = await ap.platform_mgr.get_bot_by_uuid(ctx, query.bot_uuid)
        if (binding.instance_uuid != ctx.instance_uuid or not info or info.get("enable") is not True or
            info.get("adapter") != "onebot-v11" or current is not runtime):
            raise RuntimeError("QQ notification authorization was revoked")
    await run_in_workspace_uow(ap, ctx.workspace_uuid, check)


@dataclass(eq=False)
class Task:
    query: object
    owner: tuple
    sequence: int = 0
    created: float = field(default_factory=time.monotonic)
    phase: str = "queued"
    changed: float = field(default_factory=time.monotonic)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    runner: asyncio.Task | None = None
    watcher: asyncio.Task | None = None
    foreground: asyncio.Semaphore | None = None
    owns_slot: bool = False
    tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_tasks: set = field(default_factory=set)
    terminal: str = ""
    notices: set = field(default_factory=set)


class Dispatch:
    queue_timeout = 90.0
    run_timeout = 600.0
    active_timeout = 120.0
    feedback_after = 15.0
    observation_timeout = 5.0
    observation_barrier_timeout = 15.0
    observation_limit = 32
    parked_limit = 2
    tool_queue_timeout = 180.0
    tool_timeout = 180.0

    def __init__(self, ap):
        self.ap = ap
        self.tasks = {}
        self.observations = deque()
        self.observer = None
        self.observing = None
        self.observation_dropped = 0
        self.observation_failed = 0
        self.admission_failed = 0
        self.observation_changed = asyncio.Event()
        self.tools = asyncio.Semaphore(1)
        self.parked = 0
        self.sequence = 0
        self.retired = OrderedDict()
        self.preparing = {}

    def retire(self, task):
        self.tasks.pop(task.query.query_uuid, None)
        self.retired[task.query.query_uuid] = task.owner
        while len(self.retired) > 256:
            self.retired.popitem(last=False)

    async def notice(self, query, text):
        from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
        token = _notice.set(True)
        try:
            # A delivery error has an ambiguous outcome. Never retry it.
            async with asyncio.timeout(5):
                await _authorized_notice(self.ap, query)
                await query.adapter.reply_message(
                    message_source=query.message_event, message=MessageChain([Plain(text=text)]),
                    quote_origin=True)
        except Exception:
            self.ap.logger.warning("QQ task notification delivery failed (not retried)")
        finally:
            _notice.reset(token)

    async def once(self, task, key, text):
        if key not in task.notices:
            task.notices.add(key)
            await self.notice(task.query, text)

    def observe(self, query, pipeline):
        completion = asyncio.Event()
        if len(self.observations) >= self.observation_limit:
            _, _, dropped = self.observations.popleft()
            dropped.set()
            self.observation_dropped += 1
        self.observations.append((query, pipeline, completion))
        self.observation_changed.set()
        if self.observer is None or self.observer.done():
            self.observer = asyncio.create_task(self._observe(), name="qq-receive-observer")

    async def _observe(self):
        from langbot_plugin.api.entities import events
        pool = self.ap.query_pool
        while self.observations:
            self.observation_changed.clear()
            def blocked(candidate):
                owner = _scope(candidate[0])
                sequence = candidate[0]._qq_receive_sequence
                return (any(other == owner and earlier < sequence for earlier, other in self.preparing.items())
                        or any(task.owner == owner and not task.done.is_set() and task.sequence < sequence
                               for task in self.tasks.values()))
            item = next((candidate for candidate in self.observations if not blocked(candidate)), None)
            if item is None:
                await self.observation_changed.wait()
                continue
            self.observations.remove(item)
            query, pipeline, completion = item
            self.observing = item
            ctx = getattr(query, "_execution_context")
            # These temporary entries authorize query-scoped plugin APIs. They
            # are never foreground tasks and never decrement foreground counts.
            async with pool.condition:
                query.query_id = pool.query_id_counter
                pool.query_id_counter += 1
                pool.cached_queries[(ctx.workspace_uuid, query.query_uuid)] = query
                pool.legacy_query_index[(ctx.workspace_uuid, query.query_id)] = query.query_uuid
            try:
                event = events.GroupMessageReceived(
                    query=query, launcher_type="group", launcher_id=query.launcher_id,
                    sender_id=query.sender_id, message_event=query.message_event,
                    message_chain=query.message_chain)
                async with asyncio.timeout(self.observation_timeout):
                    await pipeline._assert_execution_active(query)
                    await self.ap.plugin_connector.emit_event(event, pipeline.bound_plugins)
            except Exception:
                self.observation_failed += 1
            finally:
                async with pool.condition:
                    pool.cached_queries.pop((ctx.workspace_uuid, query.query_uuid), None)
                    pool.legacy_query_index.pop((ctx.workspace_uuid, query.query_id), None)
                    pool.condition.notify_all()
                completion.set()
                self.observing = None

    async def barrier(self, query):
        owner = _scope(query)
        # Only older observations of this member are awaited. Prioritize them
        # ahead of unrelated traffic, without delaying the ingress coroutine.
        def earlier(item):
            return (_scope(item[0]) == owner and
                    getattr(item[0], "_qq_receive_sequence") <= query._qq_task.sequence)
        pending = [item for item in self.observations if earlier(item)]
        other = [item for item in self.observations if not earlier(item)]
        self.observations = deque(pending + other)
        if self.observing and _scope(self.observing[0]) == owner:
            pending.insert(0, self.observing)
        if pending:
            async with asyncio.timeout(self.observation_barrier_timeout):
                for _, _, completed in pending:
                    await completed.wait()

    def register(self, query, task):
        task.query = query
        _attr(query, "_qq_task", task)
        self.tasks[query.query_uuid] = task
        task.watcher = asyncio.create_task(self.watch(task), name="qq-task-watch")

    async def watch(self, task):
        try:
            while not task.done.is_set():
                try:
                    await asyncio.wait_for(task.done.wait(), min(self.feedback_after, 1.0))
                    break
                except TimeoutError:
                    pass
                now = time.monotonic()
                if task.phase == "queued" and now - task.created >= self.queue_timeout:
                    await self.stop(task, "queue_timeout")
                    break
                if task.phase in {"running", "resuming"} and now - task.changed >= self.active_timeout:
                    await self.stop(task, "execution_timeout")
                    break
                if now - task.created >= self.feedback_after:
                    message = ("请求已收到，正在排队；可 @ 我发送“任务状态”或“取消任务”。"
                               if task.phase == "queued" else
                               "请求仍在处理，尚未完成；可 @ 我发送“任务状态”或“取消任务”。")
                    await self.once(task, "waiting", message)
        except asyncio.CancelledError:
            pass

    async def stop(self, task, reason, notify=True):
        if task.done.is_set():
            return
        task.terminal = reason
        task.done.set()
        for child in tuple(task.tool_tasks):
            if child is not asyncio.current_task():
                child.cancel()
        if task.runner is not None and task.runner is not asyncio.current_task():
            task.runner.cancel()
        await self.ap.query_pool.remove_query(task.query)
        async with self.ap.query_pool.condition:
            self.ap.query_pool.condition.notify_all()
        self.retire(task)
        self.observation_changed.set()
        _complete(task.query)
        texts = {
            "cancelled": "已取消当前请求的继续处理。已提交的后台任务可能仍在运行；已发送的内容不会撤回，也不会自动重发。",
            "queue_timeout": "这次请求排队超时，已停止等待，没有开始执行。请稍后重新发起。",
            "execution_timeout": "这次请求处理超时，已停止继续处理。已提交的后台任务可能仍在运行，已产生的结果保留，不会自动重试或重复发送。",
        }
        if notify:
            await self.once(task, "terminal", texts[reason])

    async def control(self, query, command):
        own = [task for task in self.tasks.values() if task.owner == _scope(query) and not task.done.is_set()]
        if command == "取消任务":
            if own:
                for task in own:
                    await self.stop(task, "cancelled", notify=False)
                await self.notice(query, "已取消你在本群请求的继续处理。已提交的后台任务可能仍在运行；已发送的内容不会撤回，也不会自动重发。")
            else:
                await self.notice(query, "你在这个群里没有尚未完成的请求。")
        else:
            phases = {"queued": "排队", "running": "处理", "tool_wait": "等待工具", "tool": "工具处理", "resuming": "等待继续"}
            descriptions = [f"{phases.get(t.phase, '处理')}中（已等待 {int(time.monotonic()-t.created)} 秒）" for t in own]
            await self.notice(query, "你的请求：" + "；".join(descriptions) if descriptions else "你在这个群里没有尚未完成的请求。")

    async def close(self):
        tasks = [t.watcher for t in self.tasks.values() if t.watcher]
        if self.observer:
            tasks.append(self.observer)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def intercept_add(pool, arguments):
    """Called before any foreground queue admission; returns handled/result."""
    if _bypass.get() or not os.environ.get("QUNLING_DATA_DIR"):
        return False, None
    ap = _managed_app(pool, arguments)
    if ap is None:
        return False, None
    from langbot_plugin.api.entities.builtin.pipeline.query import Query
    from langbot.pkg.pipeline.entities import ResultType
    from langbot.pkg.pipeline.resprule.resprule import GroupRespondRuleCheckStage

    names = ("bot_uuid", "launcher_type", "launcher_id", "sender_id", "message_event", "message_chain",
             "adapter", "pipeline_uuid", "routed_by_rule", "variables", "execution_context")
    kwargs = {key: arguments[key] for key in names}
    kwargs["variables"] = dict(kwargs.get("variables") or {})
    received_at = time.monotonic()
    service = _service(ap)
    service.sequence += 1
    sequence = service.sequence
    query = Query(**{key: value for key, value in kwargs.items() if key not in {"execution_context", "routed_by_rule"}},
                  query_id=-1, resp_messages=[], resp_message_chain=[])
    _attr(query, "_qq_received_at", received_at)
    _attr(query, "_qq_receive_sequence", sequence)
    query.variables = {**(kwargs.get("variables") or {}), "_routed_by_rule": kwargs["routed_by_rule"]}
    if len(service.preparing) >= 64:
        service.admission_failed += 1
        _attr(query, "_execution_context", kwargs["execution_context"])
        if _explicit_raw(query):
            await service.notice(query, "当前受理请求较多，这次尚未开始处理，请稍后再试。")
        _complete(query)
        return True, None
    runtime_scope = getattr(kwargs["adapter"]._runtime, "execution_context", None)
    if runtime_scope is not None:
        service.preparing[sequence] = (runtime_scope.instance_uuid, runtime_scope.workspace_uuid,
            runtime_scope.placement_generation, query.bot_uuid, str(query.launcher_id), str(query.sender_id))
    try:
        async with asyncio.timeout(5):
            context = await pool.resolve_execution_context(kwargs["execution_context"],
                bot_uuid=kwargs["bot_uuid"], pipeline_uuid=kwargs["pipeline_uuid"], query_uuid=str(uuid.uuid4()))
            for key in ("instance_uuid", "workspace_uuid", "placement_generation", "query_uuid"):
                _attr(query, key, getattr(context, key))
            _attr(query, "_execution_context", context)
            pipeline = await ap.pipeline_mgr.get_pipeline_by_uuid(context, query.pipeline_uuid)
            if pipeline is None:
                raise RuntimeError("pipeline unavailable")
            query.pipeline_config = pipeline.pipeline_entity.config
            check = next((c.inst for c in pipeline.stage_containers
                          if isinstance(c.inst, GroupRespondRuleCheckStage)), None)
            matched = True
            replacement = query.message_chain.model_copy(deep=True)
            if check is not None:
                original = query.message_chain
                query.message_chain = replacement
                matched = (await check.process(query, "qq-admission")).result_type == ResultType.CONTINUE
                replacement = query.message_chain
                query.message_chain = original
    except Exception:
        # Do not turn an ingress/config failure into silent loss of a mention.
        service.preparing.pop(sequence, None)
        service.observation_changed.set()
        service.admission_failed += 1
        if _explicit_raw(query):
            await service.notice(query, "暂时无法受理这次请求，请稍后重试。")
        _complete(query)
        return True, None
    except BaseException:
        service.preparing.pop(sequence, None)
        service.observation_changed.set()
        raise
    if not matched:
        service.preparing.pop(sequence, None)
        service.observe(query, pipeline)
        _complete(query)
        return True, None
    command = str(replacement).strip()
    if command in {"任务状态", "取消任务"}:
        try:
            await service.control(query, command)
            _complete(query)
        finally:
            service.preparing.pop(sequence, None)
            service.observation_changed.set()
        return True, None
    task = Task(query=query, owner=_scope(query), created=received_at, sequence=sequence)
    token = _bypass.set(True)
    try:
        # Admission and registration contain no await after pool.add_query
        # returns: consumer cannot see a half-registered explicit request.
        admitted = await pool.add_query(**kwargs)
        _attr(admitted, "_qq_trigger_replacement", replacement)
        service.register(admitted, task)
        return True, admitted
    except Exception:
        await service.notice(query, "当前请求已排满，这次尚未开始执行，请稍后再试。")
        _complete(query)
        return True, None
    finally:
        _bypass.reset(token)
        service.preparing.pop(sequence, None)
        service.observation_changed.set()


async def acquire_slot(controller, query):
    task = getattr(query, "_qq_task", None)
    if task is None:
        await controller.semaphore.acquire()
        return True
    remaining = task.created + _service(controller.ap).queue_timeout - time.monotonic()
    if task.done.is_set() or remaining <= 0:
        return False
    acquire = asyncio.create_task(controller.semaphore.acquire())
    cancelled = asyncio.create_task(task.done.wait())
    try:
        completed, _ = await asyncio.wait({acquire, cancelled}, timeout=remaining,
                                          return_when=asyncio.FIRST_COMPLETED)
        if acquire not in completed or task.done.is_set():
            if acquire.done() and not acquire.cancelled() and acquire.result():
                controller.semaphore.release()
            else:
                acquire.cancel()
                await asyncio.gather(acquire, return_exceptions=True)
            if not task.done.is_set():
                await _service(controller.ap).stop(task, "queue_timeout")
            return False
    except BaseException:
        if acquire.done() and not acquire.cancelled() and acquire.result():
            controller.semaphore.release()
        else:
            acquire.cancel()
            await asyncio.gather(acquire, return_exceptions=True)
        raise
    finally:
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
    task.foreground = controller.semaphore
    task.owns_slot = True
    if task.done.is_set():
        release_slot(controller, query)
        return False
    return True


def release_slot(controller, query):
    task = getattr(query, "_qq_task", None)
    if task is None:
        controller.semaphore.release()
    elif task.owns_slot:
        task.owns_slot = False
        task.foreground.release()


def begin_processing(query):
    task = getattr(query, "_qq_task", None)
    if task is not None:
        task.runner = asyncio.current_task()
        task.phase, task.changed = "running", time.monotonic()


async def preflight(query, operation):
    if protected(query):
        return await asyncio.wait_for(operation, 5)
    return await operation


async def run_pipeline(ap, query, pipeline):
    task = getattr(query, "_qq_task", None)
    if task is None:
        return await pipeline.run(query)
    service = _service(ap)
    task.runner = asyncio.current_task()
    task.phase, task.changed = "running", time.monotonic()
    try:
        if task.done.is_set():
            return
        async with asyncio.timeout(service.run_timeout):
            await service.barrier(query)
            await pipeline.run(query)
        if query.variables.get("_monitoring_has_error") and not getattr(query, "_qq_delivery_confirmed", False):
            message = ("这次回复未能确认发送成功，已停止；不会自动重发。" if
                       getattr(query, "_qq_delivery_attempted", False) else
                       "这次请求处理失败，未获得可发送的结果；不会自动重复执行。")
            await service.once(task, "terminal", message)
    except TimeoutError:
        await service.stop(task, "execution_timeout")
    except asyncio.CancelledError:
        # A control cancellation already sent its single final notification.
        if not task.terminal:
            raise
    except Exception:
        await service.once(task, "terminal", "这次请求处理失败，已停止；不会自动重复执行或发送。")
        raise
    finally:
        task.done.set()
        if task.watcher and not task.terminal:
            task.watcher.cancel()
        for child in tuple(task.tool_tasks):
            child.cancel()
        service.retire(task)
        service.observation_changed.set()


async def fail_cleanup(ap, query):
    """Controller exits before pipeline.run still settle the managed task."""
    task = getattr(query, "_qq_task", None)
    if task is None or task.done.is_set():
        return
    service = _service(ap)
    task.done.set()
    task.terminal = "failed"
    service.retire(task)
    service.observation_changed.set()
    if task.watcher:
        task.watcher.cancel()
    try:
        from langbot.pkg.pipeline.pool import get_query_execution_context
        ctx = get_query_execution_context(query)
        async with asyncio.timeout(5):
            binding = await ap.workspace_service.get_execution_binding(ctx.workspace_uuid,
                expected_generation=ctx.placement_generation)
        if binding.instance_uuid == ctx.instance_uuid:
            await service.once(task, "terminal", "这次请求未能开始处理，已停止等待，请稍后重新发起。")
    except Exception:
        pass  # Do not deliver using a revoked workspace binding.
    _complete(query)


async def invoke_tool(ap, query, invoke, name=""):
    """Wrap authorized host tool execution (plugin, MCP, skill, native)."""
    service = getattr(ap, "_qq_dispatch", None)
    task = None if service is None else service.tasks.get(getattr(query, "query_uuid", ""))
    if task is None:
        if protected(query) or (service and getattr(query, "query_uuid", "") in service.retired):
            raise RuntimeError("The owning QQ request has finished or was cancelled")
        return await invoke()
    attached = getattr(query, "_qq_task", None)
    if attached is not None and attached is not task:
        raise RuntimeError("QQ task identity does not match the active request")
    if _scope(query) != task.owner or task.done.is_set():
        raise RuntimeError("The owning QQ request is no longer active")
    # Serial per request, including SDK parallel tool calls. A run must never
    # resume its model while one of its other tools still holds the lease.
    async with task.tool_lock:
        if task.done.is_set():
            raise RuntimeError("The owning QQ request was cancelled")
        if service.parked >= service.parked_limit:
            raise RuntimeError("Long-task capacity is full; do not automatically retry this tool")
        service.parked += 1
        current = asyncio.current_task()
        task.tool_tasks.add(current)
        task.phase, task.changed = "tool_wait", time.monotonic()
        if task.owns_slot:
            task.owns_slot = False
            task.foreground.release()
        try:
            async with asyncio.timeout(service.tool_queue_timeout):
                await service.tools.acquire()
            try:
                task.phase, task.changed = "tool", time.monotonic()
                timeout = 420 if name in {"generate_image", "edit_image"} else service.tool_timeout
                async with asyncio.timeout(timeout):
                    return await invoke()
            finally:
                service.tools.release()
        finally:
            try:
                if not task.done.is_set():
                    task.phase, task.changed = "resuming", time.monotonic()
                    await task.foreground.acquire()
                    if task.done.is_set():
                        task.foreground.release()
                    else:
                        task.owns_slot = True
                        task.phase, task.changed = "running", time.monotonic()
            finally:
                task.tool_tasks.discard(current)
                service.parked -= 1


async def next_query(controller):
    """Resolve sessions outside the pool mutex; one bad DB lookup is bounded."""
    pool = controller.ap.query_pool
    async with pool:
        snapshot = list(pool.queries)
    for query in snapshot:
        try:
            session = await asyncio.wait_for(controller.ap.sess_mgr.get_session(query), 3)
        except Exception:
            await fail_cleanup(controller.ap, query)
            await pool.remove_query(query)
            async with pool.condition:
                pool.condition.notify_all()
            controller.ap.logger.warning("Session lookup failed; request removed without retry")
            continue
        if session._semaphore.locked():
            # All group steering is already prohibited by the orchestrator.
            # Avoid its redundant DB lookup, especially under a pool mutex.
            if getattr(query.launcher_type, "value", None) != "group":
                try:
                    claimed = await asyncio.wait_for(controller._try_claim_steering_before_session_slot(query), 3)
                except TimeoutError:
                    claimed = False
                if claimed:
                    await pool.remove_query(query)
            continue
        async with pool:
            if not any(item is query for item in pool.queries) or session._semaphore.locked():
                continue
            await session._semaphore.acquire()  # known available; no suspended I/O
            pool.mark_query_running_locked(query)
            return query, session
    async with pool.condition:
        if any(not any(item is old for old in snapshot) for item in pool.queries):
            return None, None
        # Bounded wait also covers a completion notification racing this scan.
        try:
            await asyncio.wait_for(pool.condition.wait(), 1)
        except TimeoutError:
            pass
    return None, None
