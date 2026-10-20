"""Configure the local QQ bot after the official plugins have been installed."""

import json
from chat_style import styled_prompts
from local_api import LocalAPI, STATE

api = LocalAPI()
state_path = STATE / 'setup-state.json'
state = json.loads(state_path.read_text(encoding='utf-8'))


def save():
    state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')


providers = api.call('GET', '/api/v1/provider/providers')['providers']
embedding_provider = next((p for p in providers if p['name'] == 'Local memory embedding'), None)
embedding_provider_uuid = embedding_provider['uuid'] if embedding_provider else api.call(
    'POST', '/api/v1/provider/providers', {
        'name': 'Local memory embedding', 'requester': 'chroma-embedding', 'base_url': '', 'api_keys': [],
    },
)['uuid']
embedding_models = api.call('GET', '/api/v1/provider/models/embedding')['models']
embedding_model = next((m for m in embedding_models if m['provider_uuid'] == embedding_provider_uuid), None)
embedding_uuid = embedding_model['uuid'] if embedding_model else api.call(
    'POST', '/api/v1/provider/models/embedding', {
        'name': 'all-MiniLM-L6-v2', 'provider_uuid': embedding_provider_uuid, 'extra_args': {},
    },
)['uuid']
state['embedding_uuid'] = embedding_uuid
save()

bases = api.call('GET', '/api/v1/knowledge/bases')['bases']
memory = next((b for b in bases if b['name'] == 'QQ long-term memory'), None)
memory_uuid = memory['uuid'] if memory else api.call('POST', '/api/v1/knowledge/bases', {
    'name': 'QQ long-term memory',
    'description': 'Persisted memory, isolated per group or private chat.',
    'knowledge_engine_plugin_id': 'langbot-team/LongTermMemory',
    'creation_settings': {
        'embedding_model_uuid': embedding_uuid, 'isolation': 'session',
        'recency_half_life_days': 30, 'auto_recall_top_k': 3,
        'retrieval_strategy': 'auto', 'exact_match_boost': True,
        'candidate_extraction_enabled': False, 'candidate_auto_apply': False,
    },
    'retrieval_settings': {'top_k': 5},
})['uuid']
state['memory_uuid'] = memory_uuid
save()

pipeline = api.call('GET', '/api/v1/pipelines/' + state['pipeline_uuid'])['pipeline']
config = pipeline['config']
config['trigger']['group-respond-rules'] = {'at': True, 'prefix': ['/ai'], 'regexp': [], 'random': 0.0}
runner_id = 'plugin:langbot-team/LocalAgent/default'
config['ai']['runner']['id'] = runner_id
runner = config['ai']['runner_config'][runner_id]
runner['model'] = {'primary': state['models']['gpt-6.1-sol'], 'fallbacks': [], 'reasoning': {}}
runner['knowledge-bases'] = [memory_uuid]
runner['box-enabled'] = False
runner['max-tool-iterations'] = 24
runner['prompt'] = [{'role': 'system', 'content': (
    '你是一个中文 QQ 助手，回答自然、清楚，默认简洁。可以根据用户发送的图片回答问题。'
    '区分当前说话人与其他群成员，不把别人的信息归到当前用户。'
    '用户明确要求记住，或明确提供长期有效的偏好、身份信息时，使用可用记忆工具保存；'
    '相关时检索已有记忆，允许用户更正和删除。不要保存密钥、密码、验证码；'
    '不要把猜测或图片识别中的不确定结论保存成事实。'
    '当用户明确要求生成新图或修改图片时，按意图调用图片生成或编辑工具，不要求固定口令。'
    '仅讨论或描述图片时不要生成图片；修改目标不明确时请用户指定或引用图片。'
    '图片工具成功后已经直接发送图片，不要编造图片链接或重复发送。'
)}]
runner['prompt'] = styled_prompts(runner['prompt'])
api.call('PUT', '/api/v1/pipelines/' + state['pipeline_uuid'], {'config': config})

bots = api.call('GET', '/api/v1/platform/bots')['bots']
bot = next((b for b in bots if b['name'] == 'QQ AI'), None)
if bot:
    bot_uuid = bot['uuid']
else:
    bot_uuid = api.call('POST', '/api/v1/platform/bots', {
        'name': 'QQ AI', 'description': 'Reply when mentioned or when a message starts with /ai.',
        'adapter': 'onebot-v11',
        'adapter_config': {
            'connection_mode': 'reverse_ws',
            'access-token': (STATE / 'onebot-token.txt').read_text(encoding='utf-8').strip(),
            'self_id': '',
        },
        'enable': False,
        'event_bindings': [{
            'event_pattern': 'message.received', 'target_type': 'pipeline',
            'target_uuid': state['pipeline_uuid'], 'filters': [], 'enabled': True,
        }],
        'plugin_processors': [],
    })['uuid']
state['bot_uuid'] = bot_uuid
state['onebot_url'] = f'ws://127.0.0.1:5300/bots/{bot_uuid}/onebot/v11/ws'
save()
print('Configured local memory, pipeline, and QQ bot. Existing bot enable state was preserved.')
print('NapCat WebSocket URL: ' + state['onebot_url'])
