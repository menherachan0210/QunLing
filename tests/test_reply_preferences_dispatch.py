import time

import pytest

from test_qq_dispatch import managed, app, put
from test_qq_continuation_dispatch import observed
from qunling import reply_preferences as prefs, qq_dispatch
from langbot.pkg.pipeline.entities import ResultType


def save(app, mode='mention_only'):
    return prefs.save(prefs.key(app.scope, 'bot', '30000'), '20000', mode=mode,
        rule='仅在我询问时参与' if mode == 'conditional' else '没真正@不接话',
        expected_revision='0', source_message_id='old', source_time=time.time()-10)


@pytest.mark.asyncio
async def test_prefix_cannot_bypass_rule_and_real_mention_survives_trigger_stripping(managed):
    save(managed)
    assert await put(managed, '20000', '/ai 帮我查一下', group='30000', addressed=False) is None
    query = await put(managed, '20000', '正常回答', group='30000', addressed=True)
    assert query is not None
    assert (await managed.trigger.process(query, 'trigger')).result_type == ResultType.CONTINUE
    assert not any(getattr(p, 'type', None) == 'At' for p in query.message_chain)
    assert await qq_dispatch.delivery_allowed(query)
    other = await put(managed, '20001', '/ai 正常指令', group='30000', addressed=False)
    assert other is not None and await qq_dispatch.delivery_allowed(other)
    managed.adapter.reply_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_conditional_handoff_uses_judged_grant_during_admission_and_delivery(managed):
    save(managed, 'conditional')
    values = await observed(managed)
    query = await qq_dispatch.admit_continuation(managed, values, validate=lambda: True)
    assert query is not None and await qq_dispatch.delivery_allowed(query)
    query._qq_continuation.validate = lambda: False
    assert not await qq_dispatch.delivery_allowed(query)
    managed.adapter.reply_message.assert_not_awaited()
