"""Authenticated administration and bounded, non-delivering voice previews."""
import asyncio
import base64
import time

from qunling.voice_config import VOICES, VoiceError, for_bot
from qunling.voice_client import synthesize, transcribe


async def manage(ap, context, bot_uuid, action, values=None):
    from qunling.proactive.host import _authorized_bot
    bot = await _authorized_bot(ap, context, bot_uuid)
    store = for_bot(bot)
    if action == 'read':
        config = store.public(await asyncio.to_thread(store.read))
    elif action == 'save':
        config = await asyncio.to_thread(store.save, values)
    elif action == 'test':
        if (not isinstance(values, dict) or set(values) != {'revision', 'text'}
                or not isinstance(values['text'], str) or not 1 <= len(values['text'].strip()) <= 100):
            raise VoiceError('voice_config', '试听文字需要 1～100 字，请先保存设置。')
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
                audio, spoken = await synthesize(config, values['text'])
                generated = time.monotonic() - started
                started = time.monotonic()
                transcript = await transcribe(config, audio)
                return {'audio_base64': base64.b64encode(audio).decode(), 'mime': 'audio/wav',
                        'spoken_text': spoken, 'transcript': transcript, 'ai_generated': True,
                        'speech_seconds': round(generated, 2), 'transcription_seconds': round(time.monotonic() - started, 2)}
        except TimeoutError:
            raise VoiceError('voice_timeout', '语音试听超时，本次没有自动重试。', 504) from None
        finally:
            bot._qunling_voice_preview = False
    else:
        raise VoiceError('voice_config', '未知语音操作。')
    return {'config': config, 'voices': list(VOICES), 'models': {'recognition': 'gpt-transcribe',
            'speech': 'gpt-realtime-2.1-mini'}, 'limits': {'input_mib': 12, 'reply_characters': 600, 'reply_seconds': 60}}
