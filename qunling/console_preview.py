"""Bounded administrator trial conversations; no dispatch, sends or memory writes."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import re
import time

from qunling.proactive.config import validate as validate_config

TRIAL_CONTEXT = '当前是管理台中的一对一试聊：输入的成员发言都直接对机器人说，按 addressed 或 continuing 理解参与关系。人格、消息含义和资料需求照常判断；试聊不执行工具或保存设置。'


def validate(value):
    if not isinstance(value, dict) or value.get('action') not in {'start', 'status', 'cancel'}:
        raise ValueError('无效的试聊操作。')
    required = {'action', 'group_id', 'request_id'}
    if value['action'] == 'start':
        required |= {'config', 'messages', 'member_id', 'use_memory'}
    if set(value) != required:
        raise ValueError('试聊参数不完整。')
    if not isinstance(value['group_id'], str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value['group_id']):
        raise ValueError('请选择有效的群。')
    if not isinstance(value['request_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', value['request_id']):
        raise ValueError('试聊编号无效。')
    if value['action'] == 'start':
        value = {**value, 'config': validate_config(value['config'])}
        if value['group_id'] not in value['config']['groups']:
            raise ValueError('试聊缺少当前群的设置。')
        messages = value['messages']
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 24
                or any(not isinstance(row, dict) or set(row) != {'role', 'text'}
                       or row['role'] not in {'user', 'assistant'} or not isinstance(row['text'], str)
                       or not 1 <= len(row['text'].strip()) <= 3000 for row in messages)
                or messages[-1]['role'] != 'user' or messages[0]['role'] != 'user'
                or sum(len(row['text']) for row in messages) > 24000):
            raise ValueError('试聊最多保留 24 条消息，单条不超过 3000 字。')
        if type(value['use_memory']) is not bool or not isinstance(value['member_id'], str):
            raise ValueError('试聊记忆选项无效。')
        if value['use_memory'] and not re.fullmatch(r'[1-9][0-9]{0,19}', value['member_id']):
            raise ValueError('读取记忆时需填写本群成员 QQ 号。')
    return value


async def run_trial(bot, values):
    from qunling.proactive.host import HostBridge, _bridge, JUDGE_PROMPT, GENERATE_PROMPT
    from qunling.proactive.engine import ConversationContext, Message, validate_decision, reply_character_limit
    from qunling.proactive.structured import decode_object, plain_draft
    from qunling.group_persona import group_persona_prompt, selected_examples
    from qunling.dialogue import build_frame
    from qunling.dialogue_review import review_text, reference_history
    from qunling import reply_preferences
    from qunling.performance import ModelTimings
    from qunling.group_memory import recall_group_memory

    live = _bridge(bot, create=True)
    if live is None:
        raise ValueError('当前机器人尚未就绪。')
    # A separate bridge never creates an engine, event ingress or persistence.
    bridge = HostBridge.__new__(HostBridge)
    bridge.bot, bridge._model = bot, live._model
    bridge.decision_timeout, bridge.reply_timeout = live.decision_timeout, live.reply_timeout
    bridge.performance = ModelTimings()
    stages = []
    async def complete(system, content, *, stage='generate'):
        started = time.monotonic()
        try:
            return await HostBridge._complete_raw(bridge, system, content, stage=stage)
        finally:
            stages.append({'stage': stage, 'seconds': round(time.monotonic() - started, 3)})
    bridge._complete = complete
    config, group = values['config'], values['group_id']
    policy = config['groups'][group]
    actor = values['member_id'] if values['use_memory'] else 'console-trial-member'
    bot_id, account = str(bot.bot_entity.uuid), str(bot.adapter.bot_account_id)
    stamp = time.time()
    messages = tuple(Message(bot_id, group, f'trial-{index}', account if row['role'] == 'assistant' else actor,
                             row['text'], stamp - len(values['messages']) + index,
                             sender_name=policy['display_name'] if row['role'] == 'assistant' else '试聊成员',
                             is_bot=row['role'] == 'assistant') for index, row in enumerate(values['messages']))
    target = messages[-1]
    previous = next((row for row in reversed(messages[:-1]) if row.is_bot), None)
    ctx = ConversationContext(bot_id, group, policy['mode'] if policy['mode'] != 'off' else 'quiet',
        policy['persona'], messages, (target.message_id,) if previous else (),
        previous.text if previous else '', previous.message_id if previous else None, previous.timestamp if previous else None,
        display_name=policy['display_name'], addressed_candidate_ids=(target.message_id,),
        reply_candidate_ids=(target.message_id,), participant_id=actor,
        default_style=config.get('default_style', ''), examples=tuple(policy.get('examples', [])),
        default_examples=tuple(config.get('default_examples', [])))
    style = group_persona_prompt(ctx.display_name, ctx.persona, ctx.default_style,
                                examples=ctx.examples, default_examples=ctx.default_examples)
    preference = (reply_preferences.get(live.preference_key(group), actor) if values['use_memory'] else
                  {'mode': 'normal', 'rule': '', 'revision': '0', 'sender_id': actor})
    profiles = await recall_group_memory(bot, group, actor, target.text, mode='profiles') if values['use_memory'] else {}
    raw = await complete(JUDGE_PROMPT + '\n\n' + reply_preferences.JUDGE_PROTOCOL + '\n\n' + style + '\n\n' + TRIAL_CONTEXT,
        {**{k: v for k, v in asdict(ctx).items() if k not in {'persona','examples','default_style','default_examples'}},
         'bot_account_id': account, 'known_profiles': [profiles], 'member_reply_preferences': [preference]}, stage='judge')
    choice = decode_object(raw)
    decision = validate_decision(choice, ctx)
    if decision is None:
        raise ValueError('模型没有返回有效的参与判断，请重试。')
    if decision.action in {'silence', 'wait'}:
        return {'reply': '', 'outcome': decision.action, 'stages': stages, 'memory': profiles, 'persona': style}
    if decision.action in {'research', 'task_request', 'tool_followup'}:
        return {'reply': '', 'outcome': 'requires_tools', 'stages': stages, 'memory': profiles, 'persona': style}
    memories = (await recall_group_memory(bot, group, actor, target.text, mode='episodes')
                if values['use_memory'] and decision.understanding and 'memory' in decision.understanding.information_needs else {})
    observations = {'profiles': profiles, 'memory': memories, 'web_knowledge': [], 'shared_knowledge': [], 'reply_preference': preference}
    frame = build_frame(bot_id=bot_id, group_id=group, target=target, messages=messages,
                        understanding=decision.understanding, observations=observations)
    limit = reply_character_limit(ctx, decision)
    system = GENERATE_PROMPT + '\n\n' + group_persona_prompt(ctx.display_name, ctx.persona, ctx.default_style,
        examples=ctx.examples, default_examples=ctx.default_examples, include_examples=False)
    system += '\n' + reply_preferences.PROTOCOL + '\n仅 reply_preference_saved 回执证明本轮约定已保存；没有回执不能声称已修改。\n' + TRIAL_CONTEXT
    raw = await complete(system, {'dialogue': frame,
        'examples': selected_examples(ctx.persona, ctx.examples, ctx.default_examples),
        'output': {'reply_character_limit': limit, 'tools_available': False, 'read_only_handoff_available': False}})
    try:
        output = decode_object(raw)
    except ValueError:
        draft = plain_draft(raw)
        if not draft:
            raise ValueError('回复格式无效，请重试。') from None
    else:
        if output.get('action') in {'research', 'silence'}:
            return {'reply': '', 'outcome': 'requires_tools' if output['action'] == 'research' else 'silence',
                    'stages': stages, 'memory': observations, 'persona': style}
        draft = output.get('text') if output.get('action') == 'reply' else None
        if not isinstance(draft, str) or not draft.strip() or len(draft) > limit:
            raise ValueError('回复格式无效，请重试。')
    frame['reference_history'] = reference_history(frame, messages, bot_id=bot_id)
    reply = await review_text(bridge, style=style, frame=frame, draft=draft, limit=limit)
    return {'reply': reply, 'outcome': 'reply', 'stages': stages, 'memory': observations, 'persona': style,
            'review_changed': reply != draft, 'response_mode': getattr(decision.understanding, 'response_mode', None)}


async def manage(ap, context, bot_uuid, values):
    from langbot.pkg.core.task_boundary import create_detached_task
    from qunling.proactive.host import _authorized_bot, list_groups
    from qunling.group_reset_host import MemoryResetError
    values = validate(values)
    bot = await _authorized_bot(ap, context, bot_uuid)
    owner = (context.workspace_uuid, bot_uuid, values['group_id'])
    jobs = getattr(ap, '_qunling_console_trials', None)
    if jobs is None:
        jobs = ap._qunling_console_trials = {}
    now = time.monotonic()
    for key, item in list(jobs.items()):
        if item['task'].done() and now - item['created'] > 600:
            jobs.pop(key)
    key = (*owner, values['request_id'])
    job = jobs.get(key)
    if values['action'] != 'start':
        if job is None:
            raise ValueError('试聊记录已过期，请重新开始。')
        if values['action'] == 'cancel' and not job['task'].done():
            job['public'].update(status='cancelled')
            job['task'].cancel()
        return deepcopy(job['public'])
    digest = hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if job:
        if job['digest'] != digest:
            raise ValueError('该试聊编号已被使用。')
        return deepcopy(job['public'])
    joined = await list_groups(ap, context, bot_uuid)
    if not any(row['group_id'] == values['group_id'] for row in joined['groups']):
        raise ValueError('只能试聊当前机器人已加入的群。')
    # Recheck after awaiting membership; this is the sole admission point.
    if key in jobs:
        if jobs[key]['digest'] != digest:
            raise ValueError('该试聊编号已被使用。')
        return deepcopy(jobs[key]['public'])
    if any(k[:2] == owner[:2] and not j['task'].done() for k, j in jobs.items()):
        raise MemoryResetError('preview_busy', '已有试聊进行中，请等待或取消。', 409)
    if len(jobs) >= 32:
        completed = [k for k, item in jobs.items() if item['task'].done()]
        if not completed:
            raise MemoryResetError('preview_busy', '试聊服务繁忙，请稍后重试。', 409)
        jobs.pop(min(completed, key=lambda k: jobs[k]['created']))
    public = {'request_id': values['request_id'], 'group_id': values['group_id'], 'status': 'running'}
    async def work():
        try:
            async with asyncio.timeout(110):
                result = await run_trial(bot, deepcopy(values))
            public.update(result, status='completed')
        except asyncio.CancelledError:
            public.update(status='cancelled')
        except Exception:
            public.update(status='failed', error='试聊未完成，请检查模型连接后重试。')
    coroutine = work()
    task = create_detached_task(coroutine, workspace_uuid=context.workspace_uuid)
    task.add_done_callback(lambda completed: coroutine.close())
    jobs[key] = {'public': public, 'digest': digest, 'created': now, 'task': task}
    return deepcopy(public)
