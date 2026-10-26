"""Frozen media follow-ups reach actual vision without reading live traffic."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from qunling.group_batch import publish
from qunling.group_knowledge import recall
from qunling.integration import workspace_key
from pkg.tools import read_context, read_images
from pkg.group_evidence import publish_result
from test_context_tools import fixture


def media(item, key, sender, offset, bot=False):
    return NS(bot_id=item.bot, group_id=item.group, sender_id=sender, sender_name='成员',
        message_id=key, timestamp=item.timestamp + offset, text='对比这组图',
        reply_to=None, is_bot=bot, has_media=True)


def raw(item, key, sender):
    return {'message_id': key, 'group_id': item.group, 'sender': {'user_id': sender},
            'message': [{'type': 'image', 'data': {'url': 'https://example.test/' + key + '.png'}}]}


@pytest.mark.asyncio
async def test_later_batch_image_and_own_image_reach_pixels_and_shared_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    plugin, _, _, item = fixture()
    workspace = workspace_key(plugin)
    assert publish(workspace, item.bot, item.group, item.sender, item.anchor,
        [media(item, '501', item.sender, 2), media(item, '502', item.bot, -2, True)], '999')
    async def api(bot, function, request):
        if request['action'] == 'get_group_recent_context':
            return {'messages': []}
        key = str(request['params']['message_id'])
        return raw(item, key, item.sender if key == '501' else '999')
    plugin.call_platform_api = AsyncMock(side_effect=api)
    page = await read_context(plugin, item, {})
    refs = [ref for row in page['messages'] for ref in row['image_refs']]
    assert len(refs) == 2 and page['batch_images_unavailable'] == 0
    calls = plugin.call_platform_api.await_count
    cached = await read_context(plugin, item, {})
    assert {k: v for k, v in cached.items() if k != 'io'} == {k: v for k, v in page.items() if k != 'io'}
    assert page['io']['cache'] == 'miss' and cached['io']['cache'] == 'hit'
    assert cached['io']['downloaded_bytes'] == 0
    assert plugin.call_platform_api.await_count == calls
    load = AsyncMock(return_value='data:image/jpeg;base64,AA==')
    monkeypatch.setattr('pkg.tools.load_image', load)
    plugin.invoke_llm = AsyncMock(return_value=NS(content='两张图的帽子颜色不同。', tool_calls=[]))
    result = await read_images(plugin, item, {'image_refs': refs, 'question': '比较帽子'})
    assert load.await_count == 2 and result['status'] == 'ok'
    await publish_result(plugin, item, 'images', result)
    evidence = recall(workspace_id=workspace, bot_id=item.bot, group_id=item.group,
                      question='帽子颜色', related_message_ids=('502',))
    assert evidence and evidence[0]['source'] == 'image_analysis'
    assert '颜色不同' in evidence[0]['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['group', 'sender', 'message', 'private_url', 'failure'])
async def test_bad_resolution_never_supplies_pixels_or_private_error(tmp_path, monkeypatch, change):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    plugin, _, _, item = fixture()
    assert publish(workspace_key(plugin), item.bot, item.group, item.sender, item.anchor,
                   [media(item, '501', item.sender, 1)], '999')
    value = raw(item, '501', item.sender)
    if change == 'group': value['group_id'] = '9999'
    if change == 'sender': value['sender']['user_id'] = '9999'
    if change == 'message': value['message_id'] = '9999'
    if change == 'private_url': value['message'][0]['data']['url'] = 'http://127.0.0.1/private'
    plugin.call_platform_api = AsyncMock(side_effect=[{'messages': []},
        RuntimeError('private secret') if change == 'failure' else value])
    page = await read_context(plugin, item, {})
    assert not page['messages'] and page['batch_images_unavailable'] == 1
    assert 'private' not in str(page) and not item.image_urls
