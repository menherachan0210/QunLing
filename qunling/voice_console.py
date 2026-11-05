"""Authenticated administration and bounded, non-delivering voice previews."""
import asyncio
import base64
import time

from qunling.voice_config import VOICES, VoiceError, for_bot
from qunling.voice_client import synthesize, transcribe
from qunling.voice_context import delivery
from qunling.proactive.config import ConfigStore


async def manage(ap, context, bot_uuid, action, values=None):
    from qunling.proactive.host import _authorized_bot
    bot = await _authorized_bot(ap, context, bot_uuid)
    store = for_bot(bot)
    chat_store = ConfigStore(str(bot.bot_entity.uuid))
    chats = await asyncio.to_thread(chat_store.read)
    if not chat_store.read_valid:
        raise VoiceError('voice_config', '聊天设置暂不可读，请稍后重试。', 503)
    if action == 'read':
        config = store.public(await asyncio.to_thread(store.read))
    elif action == 'save':
        config = await asyncio.to_thread(store.save, values)
    elif action == 'test':
        if (not isinstance(values, dict) or set(values) - {'group_id', 'chat_revision'} != {'revision', 'text'}
                or not isinstance(values['text'], str) or not 1 <= len(values['text'].strip()) <= 100):
            raise VoiceError('voice_config', '试听文字需要 1～100 字，请先保存设置。')
        group = values.get('group_id', '')
        if not isinstance(group, str) or (group and group not in chats['groups']):
            raise VoiceError('voice_config', '请选择当前机器人已配置的群聊。')
        if (group or 'chat_revision' in values) and values.get('chat_revision') != chats['revision']:
            raise VoiceError('voice_conflict', '聊天人格已变化，请刷新后再试听。', 409)
        if group:
            groups = await bot.adapter.get_group_list()
            if group not in {str(g.id) for g in groups}:
                raise VoiceError('voice_config', '机器人已不在所选群聊中，请刷新群列表。')
        config = await asyncio.to_thread(store.read)
        if values['revision'] != config['revision']:
            raise VoiceError('voice_conflict', '设置已变化，请刷新后再试听。', 409)
        if not config['api_key']:
            raise VoiceError('voice_config', '请先保存官方语音 API Key。')
        if getattr(bot, '_qunling_voice_preview', False):
            raise VoiceError('voice_busy', '已有语音试听进行中，请等待完成。', 409)
        bot._qunling_voice_preview = True
        try:
            async with asyncio.timeout(38):
                started = time.monotonic()
                audio, spoken = await synthesize(config, values['text'], persona=delivery(chats, group))
                generated = time.monotonic() - started
                started = time.monotonic()
                name = chats['groups'][group]['display_name'] if group else ''
                transcript = await transcribe(config, audio, keywords=(name,))
                return {'audio_base64': base64.b64encode(audio).decode(), 'mime': 'audio/wav',
                        'spoken_text': spoken, 'transcript': transcript, 'ai_generated': True,
                        'speech_seconds': round(generated, 2), 'transcription_seconds': round(time.monotonic() - started, 2)}
        except TimeoutError:
            raise VoiceError('voice_timeout', '语音试听超时，本次没有自动重试。', 504) from None
        finally:
            bot._qunling_voice_preview = False
    else:
        raise VoiceError('voice_config', '未知语音操作。')
    return {'config': config, 'voices': list(VOICES), 'chat_revision': chats['revision'],
            'contexts': [{'group_id': gid, 'display_name': policy['display_name']} for gid, policy in chats['groups'].items()],
            'models': {'recognition': 'gpt-transcribe',
            'speech': 'gpt-realtime-2.1-mini'}, 'limits': {'input_mib': 12, 'reply_characters': 600, 'reply_seconds': 60}}
