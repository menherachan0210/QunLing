"""Voice integration contracts with synthetic audio; no live QQ or paid API calls."""
import asyncio
import base64
from copy import deepcopy
import io
import json
import shutil
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import wave

import aiohttp
import httpx
import pytest
from langbot_plugin.api.entities.builtin.platform.message import At, MessageChain, Plain, Quote, Voice

from qunling import voice, voice_client, voice_console
from qunling.voice_config import DEFAULTS, VoiceConfig, VoiceError, for_bot
from qunling.proactive.config import ConfigStore
from test_proactive_host import bridge, event, admin_context, query_for
from test_qq_actions import service
from test_public_setup_server import endpoint

pytestmark = pytest.mark.usefixtures('available_file_resources')
KEY = 'sk-' + 'offline-test-key-' * 3


def wav(seconds=1, rate=16000):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b'\x00\x01' * int(seconds * rate))
    return stream.getvalue()


@pytest.fixture
def enabled(bridge, monkeypatch):
    for_bot(bridge.bot).save({**DEFAULTS, 'enabled': True, 'api_key': KEY})
    bridge.bot.adapter.bot.call_action = AsyncMock(return_value={'base64': base64.b64encode(wav()).decode()})
    monkeypatch.setattr(voice_client, 'transcribe', AsyncMock(return_value='今晚一起打游戏吗'))
    monkeypatch.setattr(voice_client, 'synthesize', AsyncMock(return_value=(wav(), '来啦')))
    monkeypatch.setattr(voice_console, 'synthesize', voice_client.synthesize)
    monkeypatch.setattr(voice_console, 'transcribe', voice_client.transcribe)
    return bridge


def incoming(*, explicit=False, message_id='1001', group='30000'):
    value = event(explicit=explicit, message_id=message_id, group=group)
    value.message_chain = MessageChain(([At(target='10000')] if explicit else []) + [Voice(voice_id='native-record')])
    return value


def test_private_config_is_scoped_and_concurrent_writes_do_not_lose_keys(tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    store = VoiceConfig('workspace', 'bot')
    saved = store.save({**DEFAULTS, 'enabled': True, 'api_key': KEY})
    assert saved['api_key_saved'] and KEY not in json.dumps(saved)
    assert VoiceConfig('other', 'bot').read() == DEFAULTS
    assert VoiceConfig('workspace', 'other').read() == DEFAULTS
    assert store.read()['api_key'] == KEY
    edit = {k: v for k, v in saved.items() if k != 'api_key_saved'}
    edit.update(api_key='', voice='cedar')
    store.save(edit)
    assert store.read()['api_key'] == KEY
    with pytest.raises(VoiceError, match='已被修改'):
        store.save(edit)
    for value in (None, [], False, 1):
        with pytest.raises(VoiceError):
            store.save({**store.read(), 'api_key': value})


@pytest.mark.parametrize('change', [{'max_seconds': True}, {'max_seconds': 181}, {'voice': '../path'},
    {'style': 'x' * 401}, {'api_key': 'bad'}, {'keywords': '\n'.join(['hint']*31)}, {'enabled': 'yes'}, {'base_url': 'https://other.invalid'}])
def test_config_rejects_unsafe_or_invalid_settings(change, tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    with pytest.raises(VoiceError):
        VoiceConfig('w', 'b').save({**DEFAULTS, **change})


def test_audio_validates_actual_duration_without_silent_truncation():
    assert voice_client.normalize_audio(wav(2), 15) == wav(2)
    with pytest.raises(VoiceError, match='超过'):
        voice_client.normalize_audio(wav(16), 15)
    with pytest.raises(VoiceError):
        voice_client.normalize_audio(b'#EXTM3U\nhttps://localhost/private', 15)
    with pytest.raises(VoiceError):
        voice_client.normalize_audio(b'RIFF' + b'X' * voice_client.MAX_INPUT_BYTES, 15)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='Real codec round trip runs in the Linux image')
def test_native_compressed_transfer_decodes_with_the_installed_codec():
    encoded = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-threads', '1', '-f', 'wav', '-i', 'pipe:0',
        '-threads', '1', '-f', 'mp3', 'pipe:1'], input=wav(2), capture_output=True, timeout=15, check=True).stdout
    assert len(encoded) < len(wav(2))
    decoded = voice_client.normalize_audio(encoded, 15)
    with wave.open(io.BytesIO(decoded)) as w:
        assert 1.9 <= w.getnframes() / w.getframerate() <= 2.3


@pytest.mark.asyncio
async def test_voice_console_http_proxy_requires_auth_and_keeps_errors_public(endpoint, monkeypatch):
    from bootstrap import console
    http, url, _ = endpoint
    calls = []
    monkeypatch.setattr(console, 'voice', lambda *a, **kw: calls.append((a, kw)) or {'config':{'api_key_saved':True}})
    assert http.get(url + '/api/voice').status_code == 401
    assert http.post(url + '/api/voice/test', json={'text':'hello'}).status_code == 403
    assert not calls
    headers = {'Authorization':'Bearer offline-test-token'}
    assert http.get(url + '/api/voice', headers=headers).json() == {'config':{'api_key_saved':True}}
    assert http.post(url + '/api/voice/test', headers={**headers, 'Origin':url}, json={'text':'hello'}).status_code == 200
    assert calls[-1][1]['test'] is True


@pytest.mark.asyncio
async def test_transcribe_only_calls_official_audio_and_does_not_forward_private_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    calls = []
    def respond(request):
        calls.append(request)
        assert str(request.url) == 'https://api.openai.com/v1/audio/transcriptions'
        assert request.headers['Authorization'] == 'Bearer ' + KEY
        assert b'gpt-transcribe' in request.content and b'keywords[]' in request.content
        assert '群灵'.encode() in request.content
        assert b'private-persona' not in request.content
        return httpx.Response(200, json={'text': '今晚一起打游戏吗'})
    real = httpx.AsyncClient
    def fake(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return real(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', fake)
    assert await voice_client.transcribe({**DEFAULTS, 'api_key': KEY}, wav()) == '今晚一起打游戏吗'
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,code', [(401, 'voice_auth'), (429, 'voice_quota'), (302, 'voice_unavailable'), (500, 'voice_unavailable')])
async def test_asr_failure_is_not_retried_or_leaked(status, code, tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, text=KEY + ' internal provider details', headers={'Location':'https://other.invalid'})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(VoiceError) as error:
        await voice_client.transcribe({**DEFAULTS, 'api_key': KEY}, wav())
    assert error.value.code == code and KEY not in str(error.value) and len(calls) == 1


class Socket:
    def __init__(self, events): self.events, self.sent, self.closed = events, [], False
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    async def send_json(self, data): self.sent.append(data)
    async def __aiter__(self):
        for item in self.events: yield NS(type=aiohttp.WSMsgType.TEXT, data=json.dumps(item))


@pytest.mark.asyncio
async def test_realtime_uses_current_audio_protocol_and_bounded_pcm(monkeypatch):
    pcm = b'\x01\x00' * 24000
    ws = Socket([{'type':'session.updated'}, {'type':'response.output_audio.delta','delta':base64.b64encode(pcm).decode()},
                 {'type':'response.output_audio_transcript.done','transcript':'来啦'}, {'type':'response.done','response':{'status':'completed'}}])
    class Client:
        def __init__(self, **kwargs): assert kwargs['trust_env'] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def ws_connect(self, url, **kwargs):
            assert url == 'wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini'
            assert kwargs['headers']['Authorization'] == 'Bearer ' + KEY
            return ws
    monkeypatch.setattr(aiohttp, 'ClientSession', Client)
    audio, text = await voice_client.synthesize({**DEFAULTS, 'api_key': KEY}, '来啦', style='轻松')
    with wave.open(io.BytesIO(audio)) as w:
        assert w.getframerate() == 24000 and w.readframes(24000) == pcm
    assert text == '来啦' and ws.closed
    assert ws.sent[0]['session']['audio']['output']['voice'] == 'marin'
    assert ws.sent[1]['response']['conversation'] == 'none'
    ws.events[-1]['response']['status'] = 'failed'
    with pytest.raises(VoiceError):
        await voice_client.synthesize({**DEFAULTS, 'api_key': KEY}, '来啦')


@pytest.mark.asyncio
async def test_incoming_voice_becomes_scoped_text_and_duplicate_ingress_does_not_rebill(enabled):
    value = incoming()
    original_sender = value.sender
    assert await voice.prepare_event(enabled.bot, value, enabled.bot.adapter)
    assert value.sender is original_sender and value.group.id == '30000' and value.message_id == '1001'
    assert str(value.message_chain) == '[语音转写]\n今晚一起打游戏吗'
    duplicate = incoming()
    assert await voice.prepare_event(enabled.bot, duplicate, enabled.bot.adapter)
    voice_client.transcribe.assert_awaited_once()
    assert str(duplicate.message_chain) == str(value.message_chain)
    assert enabled.bot.adapter.bot.call_action.await_args.kwargs == {'file':'native-record','out_format':'mp3'}


@pytest.mark.asyncio
async def test_slow_audio_does_not_block_text_ingress_and_shutdown_cancels_backlog(enabled):
    bot = enabled.bot
    started = asyncio.Event()
    async def slow(*args):
        started.set()
        await asyncio.Event().wait()
    bot._handle_platform_event = AsyncMock(side_effect=slow)
    assert voice.defer_event(bot, incoming(), bot.adapter) is True
    await asyncio.wait_for(started.wait(), 1)
    assert voice.defer_event(bot, event('文字消息照常进入'), bot.adapter) is False
    for i in range(7):
        assert voice.defer_event(bot, incoming(message_id=str(i+2)), bot.adapter)
    busy = incoming(message_id='9')
    assert voice.defer_event(bot, busy, bot.adapter) is False
    assert '暂时繁忙' in str(busy.message_chain)
    assert len(bot._qunling_voice_tasks) == 8
    await voice.shutdown(bot)
    assert not bot._qunling_voice_tasks


@pytest.mark.asyncio
async def test_quote_transcription_preserves_original_author(enabled):
    value = event(explicit=True)
    value.message_chain = MessageChain([At(target='10000'), Quote(id='82', sender_id='20001', target_id='30000', group_id='30000',
        origin=MessageChain([Voice(voice_id='quoted-record')])) , Plain(text='他说什么')])
    assert await voice.prepare_event(enabled.bot, value, enabled.bot.adapter)
    quote = value.message_chain[1]
    assert quote.sender_id == '20001' and quote.id == '82'
    assert '[语音转写]' in str(quote.origin)
    assert str(value.message_chain[-1]) == '他说什么'


@pytest.mark.asyncio
async def test_disabled_groups_and_member_mention_only_do_not_spend(enabled):
    from qunling import reply_preferences
    bot = enabled.bot
    value = incoming(group='30001')
    assert await voice.prepare_event(bot, value, bot.adapter)
    voice_client.transcribe.assert_not_awaited()
    pref_key = reply_preferences.key(bot.execution_context, 'bot', '30000')
    pref = reply_preferences.get(pref_key, '20000')
    reply_preferences.save(pref_key, '20000', mode='mention_only', rule='仅实际 @ 时回复', expected_revision=pref['revision'],
                           source_message_id='7', source_time=1)
    assert await voice.prepare_event(bot, incoming(), bot.adapter)
    voice_client.transcribe.assert_not_awaited()
    assert await voice.prepare_event(bot, incoming(explicit=True), bot.adapter)
    voice_client.transcribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_recall_during_transcription_cancels_and_does_not_enter_chat(enabled, monkeypatch):
    from qunling.qq_recall import observe_native
    started = asyncio.Event()
    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(voice_client, 'transcribe', slow)
    task = asyncio.create_task(voice.prepare_event(enabled.bot, incoming(), enabled.bot.adapter))
    await asyncio.wait_for(started.wait(), 2)
    observe_native(enabled.bot.adapter, {'post_type':'notice', 'notice_type':'group_recall', 'self_id':10000,
        'group_id':30000, 'message_id':1001, 'user_id':20000})
    assert await asyncio.wait_for(task, 2) is False
    assert enabled.bot._qunling_voice_ingress['pending'] == 0


@pytest.mark.asyncio
async def test_read_record_does_not_read_files_or_follow_native_errors(enabled):
    with pytest.raises(VoiceError):
        await voice.read_record(enabled.bot.adapter, Voice(voice_id='/etc/passwd'))
    enabled.bot.adapter.bot.call_action.assert_not_awaited()
    enabled.bot.adapter.bot.call_action.return_value = {'status':'failed','retcode':1,'data':{'base64':base64.b64encode(wav()).decode()}}
    with pytest.raises(VoiceError):
        await voice.read_record(enabled.bot.adapter, Voice(voice_id='native'))


@pytest.mark.asyncio
async def test_reply_sends_native_audio_to_current_chat_once_and_records_receipt(service, enabled):
    service.adapter._runtime = enabled.bot
    service.check_live = AsyncMock()
    result = await service.execute('voice_reply', {'text':'来啦','tone':'轻松'})
    assert result['status'] == 'confirmed' and result['ai_generated']
    sent = [call for call in service.calls if call[0] == 'send_group_msg']
    assert len(sent) == 1 and sent[0][1]['group_id'] == '300'
    assert base64.b64decode(sent[0][1]['message'][0]['data']['file'].removeprefix('base64://')) == wav()
    assert service.query._qq_action_confirmed is True
    with pytest.raises(VoiceError):
        await service.execute('voice_reply', {'text':'来啦'})
    voice_client.synthesize.assert_awaited_once()
    assert (await service.catalog('media'))['media']['voice_reply_configured']


@pytest.mark.asyncio
async def test_recall_or_disable_during_speech_prevents_send(service, enabled, monkeypatch):
    service.adapter._runtime = enabled.bot
    async def change(*args, **kw):
        store = for_bot(enabled.bot)
        store.save({**store.read(), 'reply': False})
        return wav(), '来啦'
    monkeypatch.setattr(voice_client, 'synthesize', change)
    with pytest.raises(VoiceError):
        await service.execute('voice_reply', {'text':'来啦'})
    assert not any(call[0].startswith('send_') for call in service.calls)


@pytest.mark.asyncio
async def test_failed_synthesis_never_sends_or_retries(service, enabled):
    service.adapter._runtime = enabled.bot
    voice_client.synthesize.side_effect = VoiceError('voice_quota', '额度不足')
    result = await service.execute('voice_reply', {'text':'来啦'})
    assert result['status'] == 'error' and '文字' in result['fallback']
    with pytest.raises(VoiceError):
        await service.execute('voice_reply', {'text':'换一段话'})
    assert not any(call[0].startswith('send_') for call in service.calls)
    voice_client.synthesize.assert_awaited_once()


@pytest.mark.asyncio
async def test_console_requires_workspace_and_permission_and_preview_does_not_send(enabled):
    from langbot.pkg.api.http.authz import PermissionDeniedError
    from qunling.proactive.host import ProactiveUnavailable
    bot = enabled.bot
    for ctx in (admin_context('viewer'), admin_context(workspace='other')):
        with pytest.raises((PermissionDeniedError, ProactiveUnavailable)):
            await voice_console.manage(bot.ap, ctx, 'bot', 'read')
    public = await voice_console.manage(bot.ap, admin_context(), 'bot', 'read')
    assert public['config']['api_key_saved'] and KEY not in json.dumps(public)
    result = await voice_console.manage(bot.ap, admin_context(), 'bot', 'test',
        {'revision':public['config']['revision'], 'text':'来啦'})
    assert base64.b64decode(result['audio_base64']) == wav() and result['ai_generated']
    bot.adapter.bot.call_action.assert_not_awaited()
    with pytest.raises(VoiceError):
        await voice_console.manage(bot.ap, admin_context(), 'bot', 'test', {'revision':'stale','text':'来啦'})


def test_voice_prompt_preserves_persona_and_describes_explicit_only_voice(enabled):
    query = query_for(enabled, event())
    voice.prepare_prompt(query)
    voice.prepare_prompt(query)
    assert query.prompt.messages[0].content == '保留的原始系统提示'
    assert len([m for m in query.prompt.messages if m.content.startswith('[群灵语音能力]')]) == 1
    prompt = query.prompt.messages[-1].content
    assert '当前人格' in prompt and '收到语音本身不代表' in prompt and 'voice_reply' in prompt
    assert KEY not in prompt


@pytest.mark.asyncio
async def test_host_ingress_stops_before_observation_when_voice_was_recalled(enabled, monkeypatch):
    from langbot.pkg.platform.botmgr import RuntimeBot
    from qunling.proactive import host
    prepare = AsyncMock(return_value=False)
    observed = []
    monkeypatch.setattr(voice, 'prepare_event', prepare)
    monkeypatch.setattr(host, 'observe', lambda *args: observed.append(args))
    value = incoming()
    await RuntimeBot._handle_platform_event(enabled.bot, value, enabled.bot.adapter)
    prepare.assert_awaited_once_with(enabled.bot, value, enabled.bot.adapter)
    assert not observed
