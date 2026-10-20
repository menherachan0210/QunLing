"""Configure the installed local image plugin from the existing sub2api provider."""

import json
from local_api import LocalAPI, STATE

api = LocalAPI()
state_path = STATE / 'setup-state.json'
state = json.loads(state_path.read_text(encoding='utf-8'))
provider = api.call('GET', '/api/v1/provider/providers/' + state['provider_uuid'] + '?include_secret=true')['provider']
if not provider.get('api_keys'):
    raise RuntimeError('Set the sub2api provider API key in LangBot first')
api.call('PUT', '/api/v1/plugins/langbot-team/AIImagePlugin/config', {
    'api_base_url': provider['base_url'], 'openai_api_key': provider['api_keys'][0],
    'model_name': 'gpt-image-2.5-sunburst', 'image_size': '1024x1024',
    'quality': 'auto', 'request_timeout_seconds': 300,
})
pipeline = api.call('GET', '/api/v1/pipelines/' + state['pipeline_uuid'])['pipeline']
config = pipeline['config']
runner = config['ai']['runner_config']['plugin:langbot-team/LocalAgent/default']
# Leave enough time for image processing plus model decisions and delivery.
runner['timeout'] = max(600, int(runner.get('timeout', 300)))
instructions = (
    '当用户明确要求生成新图或修改图片时，按意图调用图片生成或编辑工具，不要求固定口令。'
    '仅讨论或描述图片时不要生成图片；修改目标不明确时请用户指定或引用图片。'
    '图片工具成功后已经直接发送图片，不要编造图片链接或重复发送。'
)
system = next((m for m in runner['prompt'] if m['role'] == 'system'), None)
if system is None:
    runner['prompt'].insert(0, {'role': 'system', 'content': instructions})
elif '当用户明确要求生成新图或修改图片时' not in system['content']:
    system['content'] += '\n' + instructions
api.call('PUT', '/api/v1/pipelines/' + state['pipeline_uuid'], {'config': config})
state['image_model'] = 'gpt-image-2.5-sunburst'
state['image_plugin'] = 'langbot-team/AIImagePlugin'
state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
print('Image plugin configured with the existing sub2api key; no credentials were printed.')
