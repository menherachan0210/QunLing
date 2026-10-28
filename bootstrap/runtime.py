"""Small, allowlisted operator controls for the deployment-owned pipeline."""
from copy import deepcopy
import hashlib
import json
import re
import shutil
import time

from bootstrap.api import ProactiveAPIError, SetupError
from bootstrap.configure import RUNNER, PLUGINS, plugin_identity

LEVELS = {'provider_default', 'disabled', 'enabled', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}


def _pipeline(manager):
    record = manager.state.read(default={})
    identifier = record.get('pipeline_uuid', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', identifier):
        raise SetupError('请先完成模型配置。')
    path = '/api/v1/pipelines/' + identifier
    pipeline = manager.api.call('GET', path)['pipeline']
    ai = pipeline['config'].get('ai', {})
    if ai.get('runner', {}).get('id') != RUNNER:
        raise SetupError('当前流水线使用其他运行器，暂不支持在这里修改。')
    runner = ai.get('runner_config', {}).get(RUNNER, {})
    primary = runner.get('model', {}).get('primary')
    models = {role: (ai.get('qunling_' + role + '_model') or primary) for role in ('decision', 'reconcile')}
    models['chat'] = primary
    if any(not isinstance(v, str) or not v for v in models.values()):
        raise SetupError('流水线尚未设置聊天模型。')
    revision = hashlib.sha256(json.dumps(pipeline['config'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    levels = runner.get('model', {}).get('reasoning', {})
    result = {'revision': revision, 'tool_iterations': runner.get('max-tool-iterations', 24),
              'request_timeout': runner.get('timeout', 600),
              'reasoning': {role: levels.get(model, 'provider_default') for role, model in models.items()}}
    # Only role relationships are public. No raw pipeline, prompts or credentials.
    return path, pipeline, models, result


def settings(manager):
    with manager.lock:
        _, _, models, config = _pipeline(manager)
        return {'config': config, 'shared_roles': [[role for role, value in models.items() if value == model]
                    for model in dict.fromkeys(models.values())]}


def save(manager, values):
    if (not isinstance(values, dict) or set(values) != {'revision', 'tool_iterations', 'request_timeout', 'reasoning'}
            or not isinstance(values['revision'], str) or not re.fullmatch('[a-f0-9]{64}', values['revision'])
            or type(values['tool_iterations']) is not int or not 1 <= values['tool_iterations'] <= 64
            or type(values['request_timeout']) is not int or not 30 <= values['request_timeout'] <= 1200
            or not isinstance(values['reasoning'], dict) or set(values['reasoning']) != {'chat', 'decision', 'reconcile'}
            or any(not isinstance(v, str) or v not in LEVELS for v in values['reasoning'].values())):
        raise SetupError('高级参数无效，请检查范围与推理强度。')
    with manager.lock:
        if manager.pending or manager.update_inflight or manager.update_status()['busy']:
            raise SetupError('配置或版本更新正在进行，请稍后重试。')
        path, pipeline, models, previous = _pipeline(manager)
        if previous['revision'] != values['revision']:
            raise ProactiveAPIError('高级参数已在其他页面变化，请重新载入后编辑。', 409)
        selected = {}
        for role, model in models.items():
            level = values['reasoning'][role]
            if model in selected and selected[model] != level:
                raise SetupError('共用同一模型的角色需要选择相同的推理强度。')
            selected[model] = level
        config = deepcopy(pipeline['config'])
        runner = config['ai']['runner_config'][RUNNER]
        runner['max-tool-iterations'] = values['tool_iterations']
        runner['timeout'] = values['request_timeout']
        levels = runner['model'].setdefault('reasoning', {})
        levels.update(selected)
        manager.api.call('PUT', path, {'config': config})
        confirmed = settings(manager)
        if any(confirmed['config'][key] != values[key] for key in ('tool_iterations', 'request_timeout', 'reasoning')):
            raise SetupError('高级参数保存结果未确认，请重新读取后核对。')
        return confirmed


def health(manager):
    total, used, free = shutil.disk_usage(manager.state.directory)
    try:
        plugins = manager.api.call('GET', '/api/v1/plugins').get('plugins', [])
        available = True
    except SetupError:
        plugins, available = [], False
    rows = []
    for item in plugins[:100]:
        identity = plugin_identity(item)
        manifest = item.get('manifest', {}).get('manifest', {})
        metadata = manifest.get('metadata', {}) if isinstance(manifest, dict) else {}
        rows.append({'id': identity, 'managed': identity in PLUGINS, 'enabled': item.get('enabled') is not False,
                     'initialized': item.get('status') == 'initialized',
                     'version': str(metadata.get('version', ''))[:60]})
    return {'storage': {'total': total, 'used': used, 'free': free, 'sampled_at': time.time()},
            'plugins': rows, 'plugins_available': available}
