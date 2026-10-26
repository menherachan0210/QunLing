"""Group console operations using the same write locks as automatic memory updates."""
from __future__ import annotations

import copy
import hashlib
import json


class ConsoleError(ValueError):
    pass


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class GroupConsole:
    MAX_ITEMS = 5000

    def __init__(self, store):
        self.store, self.plugin = store, store.plugin

    def profile_item(self, scope, key, raw):
        value = self.store._normalize_profile(raw)
        sender = key[len(f'pp:{scope}:'):] if key.startswith('pp:') else ''
        return dict(kind='profile', sender_id=sender, id=key, revision=revision(raw),
                    title=('成员 ' + sender) if sender else '群资料', updated_at=value['updated_at'],
                    value={field: value[field] for field in ('name', 'traits', 'preferences', 'notes')})

    @staticmethod
    def episode_item(collection, episode):
        return dict(kind='episode', collection_id=collection, id=episode['id'], revision=revision(episode),
                    title=episode.get('sender_name') or episode.get('sender_id') or '群事件',
                    updated_at=episode.get('timestamp', ''), status=episode.get('status', 'active'),
                    value=episode.get('content', ''), tags=episode.get('tags', []))

    async def records(self, scope, configs):
        items, profiles, episodes = [], [], []
        keys = await self.plugin.get_plugin_storage_keys()
        for key in sorted(key for key in keys if key == f'ps:{scope}' or key.startswith(f'pp:{scope}:')):
            raw = await self.store._read_json(key)
            if not self.store.has_profile_data(self.store._normalize_profile(raw)):
                continue
            items.append(self.profile_item(scope, key, raw))
            profiles.append(dict(key=key, profile=raw))
            if len(items) > self.MAX_ITEMS:
                raise ConsoleError('too_large')
        for collection in sorted(configs):
            offset = 0
            while True:
                result = await self.plugin.vector_list(collection_id=collection, filters={'user_key':scope}, limit=100, offset=offset)
                page = [self.store._episode_from_vector_item(item) for item in result.get('items', [])]
                total = result.get('total', -1)
                for episode in page:
                    items.append(self.episode_item(collection, episode))
                    episodes.append(dict(collection_id=collection, episode=episode))
                if len(items) > self.MAX_ITEMS:
                    raise ConsoleError('too_large')
                offset += len(page)
                if not page or len(page) < 100 or total >= 0 and offset >= total:
                    break
        return items, profiles, episodes

    async def handle(self, body):
        try:
            scope, action = body['scope_key'], body['action']
            configs = await self.store.get_kb_configs()
            if any(config.get('isolation', 'session') != 'session' for config in configs.values()):
                raise ConsoleError('shared')
            if action in {'list', 'export'}:
                items, profiles, episodes = await self.records(scope, configs)
                if action == 'export':
                    return dict(version=1, exported_at=self.store._now_timestamp(), profiles=profiles, episodes=episodes, count=len(items))
                query, kind, offset = body.get('query', '').casefold(), body.get('kind', 'all'), body.get('offset', 0)
                items = [item for item in items if (kind == 'all' or item['kind'] == kind)
                         and (not query or query in json.dumps([item['title'], item['value']], ensure_ascii=False).casefold())]
                return dict(items=items[offset:offset + 30], total=len(items), offset=offset,
                            next_offset=offset + 30 if offset + 30 < len(items) else None)
            if action not in {'save', 'delete'}:
                raise ConsoleError('invalid')
            if body.get('kind') == 'profile':
                return await self.store._writes.run(self.plugin, scope, lambda: self.mutate_profile(scope, body))
            collection = body.get('collection_id')
            if collection not in configs:
                raise ConsoleError('missing')
            return await self.store._writes.run(self.plugin, collection,
                                               lambda: self.mutate_episode(scope, collection, configs[collection], body))
        except ConsoleError as exc:
            return {'error': str(exc)}

    async def mutate_profile(self, scope, body):
        sender = body.get('sender_id', '')
        key = self.store._speaker_profile_key(scope, sender) if sender else self.store._session_profile_key(scope)
        raw = await self.store._read_json(key)
        if not raw or not self.store.has_profile_data(self.store._normalize_profile(raw)):
            raise ConsoleError('missing')
        if revision(raw) != body.get('revision'):
            raise ConsoleError('conflict')
        if body['action'] == 'delete':
            if sender:
                await self.store.clear_speaker_profile(scope, sender)
            else:
                await self.store.clear_session_profile(scope)
            return {'deleted': True}
        value = body.get('value')
        if not isinstance(value, dict) or set(value) != {'name', 'traits', 'preferences', 'notes'}:
            raise ConsoleError('invalid')
        for field, maximum in [('name', 200), ('notes', self.store._MAX_NOTES_LENGTH)]:
            if not isinstance(value[field], str) or len(value[field]) > maximum:
                raise ConsoleError('invalid')
        for field in ('traits', 'preferences'):
            if (not isinstance(value[field], list) or len(value[field]) > self.store._profile_field_limit(field)
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in value[field])):
                raise ConsoleError('invalid')
        profile = copy.deepcopy(self.store._normalize_profile(raw))
        profile.update(name=value['name'], notes=value['notes'])
        for field in ('traits', 'preferences'):
            if value[field] != profile[field]:
                self.store._clear_slot_group_current_values(profile, field, 'console_edit')
                profile['freeform_' + field] = value[field]
        saved = await self.store._save_profile_by_storage_key(key, profile, scope)
        return {'saved': True, 'item': self.profile_item(scope, key, saved)}

    async def mutate_episode(self, scope, collection, config, body):
        episode = await self.store.get_episode_by_id(collection, body['id'], scope)
        if not episode:
            raise ConsoleError('missing')
        if revision(episode) != body.get('revision'):
            raise ConsoleError('conflict')
        if body['action'] == 'delete':
            await self.store.delete_episode_by_id(collection, body['id'], scope)
            return {'deleted': True}
        content = body.get('value')
        if not isinstance(content, str) or not content.strip() or len(content) > 8000 or not config.get('embedding_model_uuid'):
            raise ConsoleError('invalid')
        metadata = dict(episode['metadata'], content=content, file_id=body['id'])
        vectors = await self.plugin.invoke_embedding(config['embedding_model_uuid'], [content])
        await self.store._writes.mutate(self.plugin, collection, lambda: self.plugin.vector_upsert(
            collection_id=collection, vectors=vectors, ids=[body['id']], metadata=[metadata], documents=[content]),
            'Console vector edit outcome unknown')
        return {'saved': True}
