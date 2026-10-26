import asyncio
import copy
import json

import pytest

from store.console import GroupConsole
from store.memory_store import MemoryStore


class Plugin:
    def __init__(self):
        self.storage, self.records, self.embeddings = {}, {}, []

    async def get_plugin_storage_keys(self):
        return list(self.storage)

    async def get_plugin_storage(self, key):
        if key not in self.storage:
            raise KeyError(key)
        return self.storage[key]

    async def set_plugin_storage(self, key, data):
        self.storage[key] = data

    async def invoke_embedding(self, model, texts):
        self.embeddings.append(texts)
        return [[1.0] for _ in texts]

    def filtered(self, collection, filters):
        conditions = filters.get('$and', [filters]) if filters else []
        return [copy.deepcopy(row) for (kb, _), row in self.records.items() if kb == collection
                and all(all(row['metadata'].get(key) == value for key, value in part.items()) for part in conditions)]

    async def vector_list(self, collection_id, filters=None, limit=20, offset=0):
        rows = self.filtered(collection_id, filters)
        return {'items': rows[offset:offset + limit], 'total':len(rows)}

    async def vector_upsert(self, collection_id, ids, metadata, documents, **kwargs):
        for identifier, meta, content in zip(ids, metadata, documents):
            self.records[(collection_id, identifier)] = dict(id=identifier, metadata=copy.deepcopy(meta), document=content)

    async def vector_delete(self, collection_id, filters=None, **kwargs):
        rows = self.filtered(collection_id, filters)
        for row in rows:
            self.records.pop((collection_id, row['id']))
        return len(rows)


@pytest.fixture
def memory():
    plugin = Plugin()
    plugin.storage['kb_configs'] = json.dumps({'kb': {'isolation':'session','embedding_model_uuid':'embedding'}}).encode()
    for group in ['123', '1234']:
        for key in [f'ps:bot:group_{group}', f'pp:bot:group_{group}:456']:
            plugin.storage[key] = json.dumps({'name': '成员', 'traits':['喜欢画图'], 'preferences':[], 'notes':'备注'}).encode()
        identifier = 'ep' + group
        plugin.records[('kb', identifier)] = dict(id=identifier, document='旧事件', metadata={
            'content':'旧事件','file_id':identifier,'user_key':f'bot:group_{group}','status':'active'})
    return GroupConsole(MemoryStore(plugin)), plugin


async def listing(console, **extra):
    return await console.handle(dict(scope_key='bot:group_123', action='list', **extra))


@pytest.mark.asyncio
async def test_list_and_export_are_scoped_searchable_and_do_not_embed(memory):
    console, plugin = memory
    data = await listing(console)
    assert data['total'] == 3
    assert all('1234' not in row['id'] for row in data['items'])
    assert len((await listing(console, query='画图', kind='profile'))['items']) == 2
    exported = await console.handle(dict(scope_key='bot:group_123', action='export'))
    assert exported['count'] == 3 and len(exported['profiles']) == 2
    assert not plugin.embeddings


@pytest.mark.asyncio
async def test_profile_edit_preserves_other_fields_and_conflict_does_not_overwrite(memory):
    console, plugin = memory
    row = next(item for item in (await listing(console))['items'] if item['kind'] == 'profile')
    body = dict(scope_key='bot:group_123',action='save',kind='profile',sender_id=row['sender_id'],revision=row['revision'],
                value={**row['value'], 'notes':'新的备注'})
    assert (await console.handle(body))['saved']
    assert (await console.handle(body)) == {'error':'conflict'}
    current = json.loads(plugin.storage[row['id']])
    assert current['notes'] == '新的备注' and current['traits'] == ['喜欢画图']
    other = json.loads(plugin.storage['ps:bot:group_1234'])
    assert other['notes'] == '备注'


@pytest.mark.asyncio
async def test_episode_edit_reembeds_and_scoped_delete_cannot_touch_other_group(memory):
    console, plugin = memory
    row = next(item for item in (await listing(console))['items'] if item['kind'] == 'episode')
    body = dict(scope_key='bot:group_123', action='save', kind='episode', collection_id='kb', id=row['id'],revision=row['revision'],value='新的事件')
    assert (await console.handle(body))['saved']
    assert plugin.embeddings == [['新的事件']]
    assert plugin.records[('kb', row['id'])]['metadata']['user_key'] == 'bot:group_123'
    assert (await console.handle({**body, 'id':'ep1234', 'action':'delete'})) == {'error':'missing'}
    updated = next(item for item in (await listing(console))['items'] if item['kind'] == 'episode')
    assert (await console.handle({**body, 'action':'delete','revision':updated['revision']}))['deleted']
    assert ('kb','ep1234') in plugin.records and ('kb',row['id']) not in plugin.records


@pytest.mark.asyncio
async def test_concurrent_operator_edits_share_automatic_write_lock(memory):
    console, plugin = memory
    row = next(item for item in (await listing(console))['items'] if item['kind'] == 'profile')
    body = dict(scope_key='bot:group_123',action='save',kind='profile',sender_id=row['sender_id'],revision=row['revision'])
    results = await asyncio.gather(*(console.handle({**body,'value':{**row['value'],'notes':str(index)}}) for index in range(2)))
    assert sum(result.get('saved',False) for result in results) == 1
    assert {'error':'conflict'} in results


@pytest.mark.asyncio
async def test_shared_storage_and_oversized_content_fail_closed(memory):
    console, plugin = memory
    row = next(item for item in (await listing(console))['items'] if item['kind'] == 'episode')
    body = dict(scope_key='bot:group_123',action='save',kind='episode',collection_id='kb',id=row['id'],revision=row['revision'],value='a' * 8001)
    assert (await console.handle(body)) == {'error':'invalid'}
    plugin.storage['kb_configs'] = json.dumps({'kb':{'isolation':'bot'}}).encode()
    assert (await listing(console)) == {'error':'shared'}
