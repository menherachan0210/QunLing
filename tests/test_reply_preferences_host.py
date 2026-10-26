from dataclasses import replace
import json
import time
from unittest.mock import AsyncMock

import pytest

from test_proactive_host import bridge, event, query_for, runtime_prompt
from qunling import reply_preferences as prefs
from qunling.proactive import host
from qunling.proactive.engine import ConversationContext, Message, validate_decision


def context(*, text='你说呢', sender='20000', mid='m1', messages=None):
    messages = messages or (Message('bot', '30000', mid, sender, text, time.time()),)
    return ConversationContext('bot', '30000', 'active', '', messages, (), '', None, None,
        participant_id=sender, reply_candidate_ids=tuple(m.message_id for m in messages if not m.is_bot))


def output(ctx, **extra):
    return json.dumps({'action': 'reply', 'target_message_id': ctx.messages[-1].message_id,
        'understanding': {'purpose': '回应当前成员', 'context_message_ids': [m.message_id for m in ctx.messages],
        'information_needs': [], 'participation': {'conversation': '当前成员在跟机器人交流',
        'role': 'addressed', 'entry_point': '回应当前发言', 'context_sufficient': True}}, **extra})


def save(bridge, *, mode='mention_only', rule='没@不接话', revision='0'):
    return prefs.save(bridge.preference_key('30000'), '20000', mode=mode, rule=rule,
        expected_revision=revision, source_message_id='previous', source_time=time.time() - 10)


@pytest.mark.asyncio
async def test_no_mention_blocks_even_if_model_replies_but_preserves_shared_context(bridge):
    save(bridge)
    ctx = context(text='@机器人 这只是文本，改了昵称也一样')
    bridge._complete = AsyncMock(return_value=output(ctx))
    assert (await bridge.judge(ctx))['action'] == 'silence'
    payload = bridge._complete.await_args.args[1]
    assert payload['member_reply_preferences'][0]['mode'] == 'mention_only'
    other = context(sender='20001', messages=ctx.messages + (Message('bot', '30000', 'm2', '20001', '接着聊', time.time()),))
    bridge._complete.return_value = output(other)
    assert (await bridge.judge(other))['action'] == 'reply'
    assert len(bridge._complete.await_args.args[1]['messages']) == 2
    assert bridge.reply_allowed(replace(ctx.messages[0], message_id='new-at-event', mentions=('10000',)))
    assert not bridge.reply_allowed(replace(ctx.messages[0], mentions=('20001',), reply_to='bot-quote'))


@pytest.mark.asyncio
async def test_freechat_saves_semantic_request_with_one_ack_and_then_stops(bridge):
    ctx = context(text='以后我不@你就不要接我的话')
    bridge._complete = AsyncMock(return_value=output(ctx, preference_update={
        'source_message_id': 'm1', 'mode': 'mention_only', 'rule': '仅在真实@时回复我', 'evidence': ctx.messages[0].text}))
    raw = await bridge.judge(ctx)
    assert 'preference_update' not in raw
    decision = validate_decision(raw, ctx)
    assert decision is not None
    bridge._complete.return_value = '{"action":"reply","text":"行，记下了。"}'
    assert await bridge.generate(ctx, decision) == '行，记下了。'
    obs = bridge._complete.await_args.args[1]['dialogue']['observations']
    assert obs['reply_preference_saved']['status'] == 'saved'
    assert not bridge.reply_allowed(replace(ctx.messages[0], message_id='later', text='随便聊聊'))
    assert prefs.get(bridge.preference_key('30000'), '20000')['mode'] == 'mention_only'


@pytest.mark.asyncio
async def test_nonmentioned_member_can_cancel_their_own_agreement(bridge):
    save(bridge)
    ctx = context(text='取消之前没@不回的约定，照常接我话就行')
    bridge._complete = AsyncMock(return_value=output(ctx, preference_update={
        'source_message_id': 'm1', 'mode': 'normal', 'rule': '', 'evidence': ctx.messages[0].text}))
    assert (await bridge.judge(ctx))['action'] == 'reply'
    assert prefs.get(bridge.preference_key('30000'), '20000')['mode'] == 'normal'


@pytest.mark.asyncio
async def test_other_member_cannot_supply_a_preference_write(bridge):
    ctx = context(messages=(Message('bot', '30000', 'other', '20001', '不要接我的话', time.time()),), sender='20000')
    bridge._complete = AsyncMock(return_value=output(ctx, preference_update={
        'source_message_id': 'other', 'mode': 'mention_only', 'rule': '仅@', 'evidence': '不要接我的话'}))
    with pytest.raises(host.ProactiveUnavailable):
        await bridge.judge(ctx)
    assert prefs.list_group(bridge.preference_key('30000')) == []


@pytest.mark.asyncio
async def test_send_rechecks_rule_and_revision_after_draft(bridge):
    ctx = context()
    bridge.observe(event())
    bridge._complete = AsyncMock(return_value=output(ctx))
    await bridge.judge(ctx)
    row = save(bridge, mode='conditional', rule='只有我明确提问时才接话')
    with pytest.raises(host.ProactiveUnavailable, match='互动约定'):
        await bridge.send('30000', '已过时的草稿', 'm1')
    bridge.bot.adapter.send_message.assert_not_awaited()
    # A fresh judgment sees the condition and can answer a matching question.
    await bridge.judge(ctx)
    assert bridge.reply_allowed(ctx.messages[0])
    prefs.save(bridge.preference_key('30000'), '20000', mode='normal', rule='', expected_revision=row['revision'],
        source_message_id='admin', source_time=time.time())
    assert not bridge.reply_allowed(ctx.messages[0])


def test_explicit_prompt_includes_actual_mention_and_current_agreement(bridge):
    save(bridge)
    query = query_for(bridge, event(explicit=True))
    host.prepare_prompt(query)
    prompt = runtime_prompt(query)
    assert '"actually_mentioned": true' in prompt and '"mode": "mention_only"' in prompt
    assert 'reply_preference' in prompt


@pytest.mark.asyncio
async def test_multi_person_answer_cannot_bundle_a_restricted_member(bridge):
    save(bridge)
    ctx = context(sender='20001', messages=(Message('bot', '30000', 'a', '20000', '我的问题', time.time()),
        Message('bot', '30000', 'b', '20001', '我的问题', time.time())))
    bridge._complete = AsyncMock(return_value=output(ctx, covered_message_ids=['a', 'b']))
    assert (await bridge.judge(ctx))['action'] == 'silence'


@pytest.mark.asyncio
async def test_normal_pipeline_checks_real_mentions_and_cancels_late_delivery(bridge, monkeypatch):
    from types import SimpleNamespace as NS
    from qunling import qq_dispatch
    import langbot.pkg.core.app  # Initialize stage registry before importing an individual stage.
    from langbot.pkg.pipeline.respback.respback import SendResponseBackStage
    from langbot.pkg.pipeline.entities import ResultType
    from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain
    from langbot_plugin.api.entities.builtin.platform import events as pevents, entities as pentities
    save(bridge)
    query = query_for(bridge, event(text='/ai 不是实际@'))
    query.launcher_type = 'group'; query.launcher_id = '30000'; query.sender_id = '20000'; query.bot_uuid = 'bot'
    query.pipeline_config = {'output': {'force-delay': {'min': 0, 'max': 0}, 'misc': {'at-sender': False}}}
    query.message_event = pevents.GroupMessage(type='GroupMessage',
        sender=pentities.GroupMember(id='20000', member_name='群友', permission=pentities.Permission.Member,
            group=pentities.Group(id='30000', name='测试群', permission=pentities.Permission.Member)),
        message_chain=query.message_event.message_chain)
    from langbot_plugin.api.entities.builtin.pipeline.query import Query
    query = Query.model_construct(**vars(query))
    bridge.bot.ap.logger = NS(debug=lambda *args: None)
    stage = SendResponseBackStage(bridge.bot.ap)
    assert (await stage.process(query, 'reply')).result_type == ResultType.INTERRUPT
    assert query._qq_reply_suppressed
    query.message_event = event(explicit=True)
    assert await qq_dispatch.delivery_allowed(query)
    query._qq_continuation = object()
    monkeypatch.setattr(qq_dispatch, '_validate_grant', AsyncMock(return_value=False))
    assert not await qq_dispatch.delivery_allowed(query)
    bridge.bot.adapter.send_message.assert_not_awaited()
