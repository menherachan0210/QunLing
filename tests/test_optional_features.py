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
