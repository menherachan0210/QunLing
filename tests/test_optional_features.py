import json
import pytest
from bootstrap.configure import settings_from_input, public_settings, role_settings
from bootstrap.api import SetupError
from test_public_config import FakeLangBot, run_configure, row, settings


def test_chat_only_does_not_need_optional_plugins_or_embedding_resources():
    api = FakeLangBot()
    api.plugins = [p for p in api.plugins if p['manifest']['manifest']['metadata']['name'] == 'LocalAgent']
    record = run_configure(api, settings(image_model='', features=[]))
    pipeline = row(api, '/api/v1/pipelines', record['pipeline_uuid'])
    assert pipeline['extensions_preferences']['plugins'] == [{'author':'langbot-team', 'name':'LocalAgent'}]
    assert not api.records['/api/v1/knowledge/bases']
    assert not api.records['/api/v1/agents']
    assert not any('/plugins/' in path and method == 'PUT' for method, path, _ in api.calls)


def test_independent_endpoints_never_receive_chat_credential_and_remain_private():
    previous = settings(vision_base_url='https://vision.example/v1', vision_api_key='synthetic-vision',
                        image_base_url='https://image.example/v1', image_api_key='synthetic-image')
    saved = settings_from_input({'vision_api_key':'', 'image_api_key':''}, previous)
    assert role_settings(saved, 'vision')['api_key'] == 'synthetic-vision'
    api = FakeLangBot()
    record = run_configure(api, saved)
    model = row(api, '/api/v1/provider/models/llm', record['vision_model_uuid'])
    provider = row(api, '/api/v1/provider/providers', model['provider_uuid'])
    assert provider['base_url'] == 'https://vision.example/v1'
    assert provider['api_keys'] == ['synthetic-vision']
    assert api.configs['langbot-team/AIImagePlugin']['openai_api_key'] == 'synthetic-image'
    public = json.dumps(public_settings(saved))
    assert not any(secret in public for secret in ['synthetic-vision', 'synthetic-image', saved['api_key']])
    with pytest.raises(SetupError, match='自己的 API Key'):
        settings_from_input({'vision_base_url':'https://another.example/v1', 'vision_api_key':''}, previous)
    with pytest.raises(SetupError, match='自己的 API Key'):
        settings_from_input({'vision_base_url':'https://another.example/v1'}, previous)
    inherited = settings_from_input({'vision_base_url':''}, previous)
    assert role_settings(inherited, 'vision')['api_key'] == previous['api_key']


def test_deselection_preserves_memory_data_and_unrelated_operator_extensions():
    api = FakeLangBot()
    record = run_configure(api)
    pipeline = row(api, '/api/v1/pipelines', record['pipeline_uuid'])
    pipeline['extensions_preferences']['plugins'].append({'author':'operator','name':'custom'})
    run_configure(api, settings(features=[]), record)
    assert api.memory_records and len(api.records['/api/v1/knowledge/bases']) == 1
    assert pipeline['extensions_preferences']['plugins'] == [
        {'author':'operator','name':'custom'}, {'author':'langbot-team','name':'LocalAgent'}]
    assert row(api, '/api/v1/platform/bots', record['bot_uuid'])['plugin_processors'] == []


def test_changing_judge_model_does_not_rename_the_shared_chat_model():
    api = FakeLangBot()
    record = run_configure(api, settings(features=[]))
    chat = record['chat_model_uuid']
    updated = run_configure(api, settings(features=[], decision_model='fast-judge'), record)
    assert updated['chat_model_uuid'] == chat and updated['decision_model_uuid'] != chat
    assert row(api, '/api/v1/provider/models/llm', chat)['name'] == 'chat-example'


def test_migration_from_all_plugins_preserves_existing_third_party_tools():
    api = FakeLangBot()
    record = run_configure(api)
    pipeline = row(api, '/api/v1/pipelines', record['pipeline_uuid'])
    pipeline['extensions_preferences'] = {'enable_all_plugins': True, 'plugins': []}
    api.plugins.append({'manifest': {'manifest': {'metadata': {'author': 'operator', 'name': 'custom'}}},
                        'status': 'initialized', 'enabled': True})
    run_configure(api, settings(features=[]), record)
    assert pipeline['extensions_preferences']['plugins'] == [
        {'author': 'operator', 'name': 'custom'}, {'author': 'langbot-team', 'name': 'LocalAgent'}]


def test_fresh_setup_replaces_upstream_defaults_but_restart_preserves_user_budget():
    from bootstrap.configure import RUNNER
    class WithUpstreamDefaults(FakeLangBot):
        def call(self, method, path, data=None):
            result = super().call(method, path, data)
            if method == 'POST' and path == '/api/v1/pipelines':
                pipeline = row(self, path, result['uuid'])
                pipeline['config']['ai']['runner_config'][RUNNER] = {
                    'max-tool-iterations': 10, 'box-enabled': True, 'timeout': 180}
            return result
    api = WithUpstreamDefaults()
    record = run_configure(api)
    runner = row(api, '/api/v1/pipelines', record['pipeline_uuid'])['config']['ai']['runner_config'][RUNNER]
    assert runner['max-tool-iterations'] == 24 and runner['timeout'] == 600
    assert runner['box-enabled'] is False
    runner['max-tool-iterations'], runner['timeout'] = 9, 123
    run_configure(api, record=record)
    assert runner['max-tool-iterations'] == 9 and runner['timeout'] == 123


@pytest.mark.parametrize('mode', ['shared', 'independent'])
def test_legacy_production_tool_settings_are_migrated_without_losing_preferences(mode):
    values = settings(image_size='768x1280', image_quality='high', image_timeout_seconds=210,
                      search_timeout_seconds=75, search_base_url='https://search.example/v1',
                      search_api_key='synthetic-search')
    values['search_provider'] = mode
    migrated = settings_from_input(values)
    assert 'search_provider' not in migrated
    assert role_settings(migrated, 'search')['base_url'] == (
        'https://search.example/v1' if mode == 'independent' else values['base_url'])
    api = FakeLangBot()
    run_configure(api, migrated)
    image = api.configs['langbot-team/AIImagePlugin']
    assert (image['image_size'], image['quality'], image['request_timeout_seconds']) == ('768x1280', 'high', 210)
    assert api.configs['local/QQWebSearch']['request_timeout_seconds'] == 75
    assert settings_from_input({'chat_model':'updated'}, migrated)['image_quality'] == 'high'
