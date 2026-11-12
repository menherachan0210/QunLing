from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling.qq_mentions import normalize


NAME = '（这是AI）七濑胡桃天下第一'


def text(value):
    return {'type': 'text', 'data': {'text': value}}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    client = NS(connection=object(), get_group_member_info=AsyncMock(return_value={
        'group_id': 123, 'user_id': 90000, 'card': NAME, 'nickname': '哈哈哈'}))
    adapter = NS(bot_account_id='90000', bot=client)
    payload = {'post_type': 'message', 'message_type': 'group', 'self_id': 90000,
               'group_id': 123, 'user_id': 456, 'message_id': 789,
               'message': [{'type': 'image', 'data': {'file': 'first'}},
                           {'type': 'image', 'data': {'file': 'second'}}]}
    return adapter, payload


@pytest.mark.asyncio
@pytest.mark.parametrize('position', ['prefix', 'suffix'])
async def test_real_incident_shape_restores_mention_without_changing_actor_or_images(env, position):
    adapter, payload = env
    if position == 'prefix':
        payload['message'] = [text('@' + NAME), text(' '), *payload['message'], text('\n按要求改图')]
    else:
        payload['message'].append(text('\n按要求改图！！ @' + NAME))
    original = deepcopy(payload)
    result = await normalize(adapter, payload)
    assert payload == original
    assert result['user_id'] == 456 and result['message_id'] == 789
    assert [p for p in result['message'] if p['type'] == 'image'] == [
        p for p in original['message'] if p['type'] == 'image']
    assert [p['data']['qq'] for p in result['message'] if p['type'] == 'at'] == ['90000']
    assert result['_qunling_mention_source'] == 'qq_display_name'
    assert '按要求改图' in ''.join(p['data'].get('text', '') for p in result['message'])


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['@别的成员 改图', '有人提到@' + NAME, '讨论 @' + NAME + ' 的名字',
                                  '他说“@' + NAME + '”', '@七濑 改图', '@' + NAME + '假名 改图'])
async def test_partial_names_embedded_mentions_and_quotations_do_not_trigger(env, value):
    adapter, payload = env
    payload['message'].append(text(value))
    assert await normalize(adapter, payload) is payload


@pytest.mark.asyncio
async def test_native_mentions_quotes_and_plain_chatter_are_not_reinterpreted(env):
    adapter, payload = env
    payload['message'] += [{'type': 'at', 'data': {'qq': '33333'}}, text('@' + NAME)]
    assert await normalize(adapter, payload) is payload
    adapter.bot.get_group_member_info.assert_not_awaited()
    payload['message'] = [text('@' + NAME + ' 看看')]
    assert await normalize(adapter, payload) is payload
    payload['message'] = [{'type': 'reply', 'data': {'id': 'quote', 'text': '@' + NAME}},
                          {'type': 'image', 'data': {'file': 'first'}}, text('看看这个')]
    assert await normalize(adapter, payload) is payload


@pytest.mark.asyncio
async def test_verified_names_are_cached_per_group_and_connection(env):
    adapter, payload = env
    payload['message'].append(text('@' + NAME))
    await normalize(adapter, payload)
    await normalize(adapter, payload)
    assert adapter.bot.get_group_member_info.await_count == 1
    adapter.bot.connection = object()
    await normalize(adapter, payload)
    assert adapter.bot.get_group_member_info.await_count == 2


@pytest.mark.asyncio
async def test_wrong_identity_or_failed_lookup_never_fabricates_mention(env):
    adapter, payload = env
    payload['message'].append(text('@' + NAME))
    adapter.bot.get_group_member_info.return_value['user_id'] = 12345
    assert await normalize(adapter, payload) is payload
    adapter.bot.get_group_member_info.side_effect = TimeoutError()
    assert await normalize(adapter, payload) is payload


@pytest.mark.asyncio
async def test_recovered_two_image_message_reaches_explicit_host_routing(env):
    from langbot.pkg.platform.adapters.aiocqhttp.message_converter import AiocqhttpMessageConverter
    from qunling.proactive.host import _is_explicit
    from qunling.reply_preferences import permits
    adapter, payload = env
    payload['message'].append(text('按要求换头 @' + NAME))
    normalized = await normalize(adapter, payload)
    chain = await AiocqhttpMessageConverter.target2yiri(normalized['message'], bot=adapter.bot)
    assert len([p for p in chain if p.type == 'Image']) == 2
    assert _is_explicit(chain, '90000')
    assert permits({'mode': 'mention_only'}, actual_mention=any(p.type == 'At' and str(p.target) == '90000' for p in chain))
