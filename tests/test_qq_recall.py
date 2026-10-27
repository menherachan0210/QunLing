import asyncio
from types import SimpleNamespace as NS

import pytest

from qunling import qq_recall as recall


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    result = NS(bot_account_id='10000')
    result._runtime = NS(adapter=result, bot_entity=NS(adapter='onebot-v11', uuid='bot'),
        execution_context=NS(instance_uuid='instance', workspace_uuid='workspace', placement_generation=1), ap=NS())
    return result


def notice(adapter, mid='-123', group='30000', **extra):
    return {'post_type': 'notice', 'notice_type': 'group_recall', 'self_id': adapter.bot_account_id,
            'group_id': group, 'message_id': mid, 'user_id': '20000', 'operator_id': '20001', **extra}


def query(adapter, mid='-123', group='30000', kind='group'):
    return NS(adapter=adapter, launcher_type=kind, launcher_id=group, variables={},
              message_event=NS(message_id=mid), sender_id='20000')


@pytest.mark.parametrize('kind', ['group', 'person'])
def test_recall_is_scoped_to_bot_placement_and_conversation(adapter, kind):
    q = query(adapter, kind=kind)
    payload = notice(adapter, user_id='30000', notice_type='friend_recall' if kind == 'person' else 'group_recall')
    recall.observe_native(adapter, payload)
    assert recall.query_recalled(q)
    assert not recall.query_recalled(query(adapter, mid='new', kind=kind))
    assert not recall.query_recalled(query(adapter, group='other', kind=kind))
    assert not recall.query_recalled(query(adapter, kind='person' if kind == 'group' else 'group'))
    adapter._runtime.execution_context.placement_generation += 1
    assert not recall.query_recalled(q)


@pytest.mark.parametrize('extra', [
    {'self_id': 'wrong-account'}, {'post_type': 'message'}, {'notice_type': 'notify'},
    {'message_id': None}, {'message_id': True}, {'group_id': None},
])
def test_text_or_invalid_notifications_cannot_withdraw_messages(adapter, extra):
    recall.observe_native(adapter, notice(adapter, **extra))
    assert not recall.query_recalled(query(adapter))


@pytest.mark.asyncio
async def test_recall_cancels_inflight_reply_but_same_text_new_message_can_reply(adapter):
    entered, waiting = asyncio.Event(), asyncio.Event()
    delivered = []
    async def reply(mid):
        q = query(adapter, mid=mid)
        with recall.protect(adapter, recall.query_keys(q), query=q):
            entered.set()
            await waiting.wait()
            delivered.append(mid)
    old = asyncio.create_task(reply('-123'))
    await entered.wait()
    recall.observe_native(adapter, notice(adapter))
    with pytest.raises(asyncio.CancelledError):
        await old
    waiting.set()
    await reply('new-message')
    assert delivered == ['new-message']


@pytest.mark.asyncio
async def test_ignored_cancellation_still_cannot_send_after_tombstone_eviction(adapter, monkeypatch):
    entered, resume = asyncio.Event(), asyncio.Event()
    delivered = []
    async def work():
        q = query(adapter)
        with recall.protect(adapter, recall.query_keys(q), query=q):
            entered.set()
            try:
                await resume.wait()
            except asyncio.CancelledError:
                await resume.wait()  # Simulate a cancellation-resistant provider.
            recall.before_action(adapter, 'send_group_msg', {})
            delivered.append('must not send')
    task = asyncio.create_task(work())
    await entered.wait()
    monkeypatch.setattr(recall, 'MAX_RECALLED', 1)
    recall.observe_native(adapter, notice(adapter))
    await asyncio.sleep(0)
    recall.observe_native(adapter, notice(adapter, mid='second'))
    resume.set()
    with pytest.raises(recall.MessageRecalled):
        await task
    assert not delivered


@pytest.mark.asyncio
async def test_another_members_message_in_same_group_is_not_cancelled(adapter):
    q = query(adapter, mid='live-message')
    with recall.protect(adapter, recall.query_keys(q)):
        recall.observe_native(adapter, notice(adapter))
        recall.check_current()
    assert not recall.query_recalled(q)


def test_merged_ingress_checks_all_original_message_ids(adapter):
    q = query(adapter, mid='first')
    q.variables[recall.SOURCE_IDS] = ['first', 'second']
    recall.observe_native(adapter, notice(adapter, mid='second'))
    assert recall.query_recalled(q)
    assert not recall.query_recalled(query(adapter, mid='first'))


def test_detached_plugin_reply_segment_is_checked_without_blocking_plain_text(adapter):
    recall.observe_native(adapter, notice(adapter))
    with pytest.raises(recall.MessageRecalled):
        recall.before_action(adapter, 'send_group_msg', {'group_id': 30000,
            'message': [{'type': 'reply', 'data': {'id': -123}}]})
    recall.before_action(adapter, 'send_group_msg', {'group_id': 30000,
        'message': [{'type': 'text', 'data': {'text': '撤回消息 -123'}}]})
    recall.before_action(adapter, 'send_group_msg', {'group_id': 30001,
        'message': [{'type': 'reply', 'data': {'id': -123}}]})
