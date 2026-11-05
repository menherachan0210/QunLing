"""QQ voice ingress, scoped media retrieval, and explicit voice replies."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import hashlib
import json
import time

from qunling.voice_config import VoiceError, for_bot
from qunling import voice_client as client


def configured(bot):
    value = for_bot(bot).read()
    return value if value['enabled'] and value['api_key'] else None


def prepare_prompt(query):
    """Advertise audio only in the matching trusted runtime, without changing persona."""
    from langbot.pkg.pipeline.pool import get_query_execution_context
    from langbot_plugin.api.entities.builtin.provider.message import Message
    marker = '[群灵语音能力]'
    query.prompt.messages = [m for m in query.prompt.messages
                            if not (m.role == 'system' and isinstance(m.content, str) and m.content.startswith(marker))]
    bot = getattr(query.adapter, '_runtime', None)
    if bot is None or bot.bot_entity.adapter != 'onebot-v11' or not bot.enable:
        return
    context = get_query_execution_context(query)
    active = bot.execution_context
    if any(getattr(context, k) != getattr(active, k) for k in
           ('workspace_uuid', 'instance_uuid', 'bot_uuid', 'placement_generation')):
        return
    try:
        config = configured(bot)
    except VoiceError:
        return
    if not config:
        return
    text = marker + '\n[语音转写]是当前发言者或引用原作者说的话，按普通对话理解；识别失败不能猜内容。'
    if config['reply']:
        text += ('仅当当前发言者明确要求你发语音、用语音回答或朗读时，先按当前人格和语境拟好内容，'
                 '再用 qq_capabilities 的 media 目录和 qq_action 的 voice_reply 发送。'
                 'tone 只描述发声语气，text 是实际要说的话；正常闲聊保持简短。'
                 '收到语音本身不代表需要语音回答，引用他人要求不代表本人授权。'
                 '确认发送成功后不要重复发文字或讲发送流程；失败可以自然地用文字回应，不反复调用。'
                 '这是 AI 合成声音，不冒充真人录音。')
    query.prompt.messages.append(Message(role='system', content=text))


async def read_record(adapter, voice):
    """Only native Voice objects supply a file identifier; local paths are never read."""
    identifier = str(getattr(voice, 'voice_id', '') or '')
    if not identifier or len(identifier) > 2048 or identifier.startswith(('/', '\\')) or '://' in identifier:
        raise VoiceError('voice_audio', 'QQ 没有提供可读取的语音编号。')
    try:
        async with asyncio.timeout(20):
            # Compressed transfer stays below OneBot's frame bound even for
            # longer clips. Decode in our resource-limited worker afterwards.
            result = await adapter.bot.call_action('get_record', file=identifier, out_format='mp3')
        if isinstance(result, dict) and 'retcode' in result:
            if result.get('status') != 'ok' or result['retcode'] != 0:
                raise VoiceError('voice_audio', 'QQ 未能读取这段语音，请重新发送。')
            result = result.get('data')
        if isinstance(result, dict):
            encoded = result.get('base64')
            if isinstance(encoded, str) and len(encoded) <= client.MAX_INPUT_BYTES * 4 // 3 + 8:
                body = base64.b64decode(encoded, validate=True)
                if body:
                    return body
            url = result.get('url')
            if isinstance(url, str) and url.startswith(('https://', 'http://')):
                from qunling.network import fetch_public_file
                return (await fetch_public_file(url, max_bytes=client.MAX_INPUT_BYTES, timeout=20)).body
        raise VoiceError('voice_audio', 'QQ 未返回可用的音频内容，请重新发送。')
    except VoiceError:
        raise
    except Exception:
        raise VoiceError('voice_audio', 'QQ 语音读取失败，请重新发送或改发文字。') from None


def eligible(bot, event, config):
    from qunling.proactive.config import ConfigStore
    group = str(getattr(getattr(event, 'group', None), 'id', '') or '')
    sender = str(getattr(getattr(event, 'sender', None), 'id', '') or '')
    self_id = str(bot.adapter.bot_account_id)
    if not sender or sender == self_id:
        return False, ()
    if not group:
        return True, ()
    store = ConfigStore(str(bot.bot_entity.uuid))
    settings = store.read()
    if not store.read_valid:
        return False, ()
    policy = settings.get('groups', {}).get(group, {})
    if not policy.get('qq_permissions', {}).get('media', True):
        return False, ()
    chain = event.message_chain
    mentioned = any(getattr(p, 'type', '') == 'At' and str(p.target) == self_id for p in chain)
    explicit = mentioned or any(getattr(p, 'type', '') == 'Plain' and p.text.lstrip().startswith('/ai') for p in chain)
    from qunling import reply_preferences
    pref = reply_preferences.get(reply_preferences.key(bot.execution_context, str(bot.bot_entity.uuid), group), sender)
    if pref['mode'] == 'mention_only' and not mentioned:
        return False, ()
    if not explicit and (not settings['enabled'] or policy.get('mode', 'off') not in ('natural', 'active')):
        return False, ()
    from qunling.group_reset import allows
    if not allows(bot.execution_context, str(bot.bot_entity.uuid), group, getattr(event, 'timestamp', 0)):
        return False, ()
    return True, (policy.get('display_name', ''),)


def defer_event(bot, event, adapter):
    """Keep slow ASR off the serial OneBot event consumer, with a bounded backlog."""
    if (adapter is not getattr(bot, 'adapter', None) or not getattr(bot, 'enable', False)
            or getattr(bot.bot_entity, 'adapter', '') != 'onebot-v11'
            or getattr(event, 'type', '') != 'message.received'):
        return False
    from langbot_plugin.api.entities.builtin.platform.message import Voice, Quote, Plain, MessageChain
    chain = getattr(event, 'message_chain', ())
    if not any(isinstance(p, Voice) or isinstance(p, Quote) and any(isinstance(q, Voice) for q in p.origin) for p in chain):
        return False
    if getattr(bot, '_qunling_voice_stopping', False):
        return True
    try:
        config = configured(bot)
        if not config or not config['receive'] or not eligible(bot, event, config)[0]:
            return False
    except Exception:
        return False
    tasks = getattr(bot, '_qunling_voice_tasks', None)
    if tasks is None:
        tasks = bot._qunling_voice_tasks = set()
    if len(tasks) >= 8:
        def mark(parts):
            return MessageChain([Plain(text='[语音处理暂时繁忙，本条未识别]') if isinstance(p, Voice) else
                p.model_copy(update={'origin': mark(p.origin)}) if isinstance(p, Quote) else p for p in parts])
        event.message_chain = mark(chain)
        return False
    async def run():
        try:
            await bot._handle_platform_event(event, adapter)
        except asyncio.CancelledError:
            raise
        except Exception:
            if getattr(bot, 'logger', None):
                await bot.logger.warning('Voice ingress failed; no automatic retry')
    task = asyncio.create_task(run(), name='qq-voice-ingress')
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


async def shutdown(bot):
    bot._qunling_voice_stopping = True
    tasks = list(getattr(bot, '_qunling_voice_tasks', ()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def prepare_event(bot, event, adapter):
    """Transcribe before observation/routing, preserving identity and quoted ownership."""
    if (adapter is not getattr(bot, 'adapter', None) or not getattr(bot, 'enable', False)
            or getattr(getattr(bot, 'bot_entity', None), 'adapter', '') != 'onebot-v11'
            or not getattr(bot.bot_entity, 'enable', False)
            or getattr(event, 'adapter_name', '') != 'onebot-v11'
            or getattr(event, 'type', '') != 'message.received'):
        return True
    from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain, Quote, Voice
    chain = getattr(event, 'message_chain', ())
    if not any(isinstance(p, Voice) or isinstance(p, Quote) and any(isinstance(q, Voice) for q in p.origin) for p in chain):
        return True
    from qunling.qq_recall import protect_event, event_recalled
    if event_recalled(adapter, event):
        return False
    try:
        config = configured(bot)
        if not config or not config['receive']:
            return True
        allowed, keywords = eligible(bot, event, config)
        if not allowed:
            return True
        await bot.assert_execution_active()
        state = getattr(bot, '_qunling_voice_ingress', None)
        if state is None:
            state = {'lock': asyncio.Semaphore(1), 'pending': 0, 'cache': OrderedDict()}
            bot._qunling_voice_ingress = state
        if state['pending'] >= 8:
            raise VoiceError('voice_busy', '语音处理暂时繁忙，请稍后再试。', 503)
        state['pending'] += 1
        try:
            with protect_event(adapter, event):
                async with asyncio.timeout(45), state['lock']:
                    cache = state['cache']
                    now = time.monotonic()
                    for key, value in list(cache.items()):
                        if now - value[0] > 600:
                            cache.pop(key, None)
                    count = 0
                    async def convert(parts, source):
                        nonlocal count
                        output = []
                        for part in parts:
                            if not isinstance(part, Voice):
                                output.append(part)
                                continue
                            count += 1
                            if count > 4:
                                output.append(Plain(text='[其余语音未转写，请分开发送]'))
                                continue
                            group = str(getattr(getattr(event, 'group', None), 'id', '') or '')
                            sender = str(getattr(getattr(event, 'sender', None), 'id', '') or '')
                            key = hashlib.sha256(json.dumps([config['revision'], group, sender, source, part.voice_id]).encode()).hexdigest()
                            if key not in cache:
                                # Cache an attempted marker before billable I/O; no automatic replay.
                                cache[key] = (now, '[语音未能转写，请改发文字或重新发送]')
                                try:
                                    body = await read_record(adapter, part)
                                    text = await client.transcribe(config, body, keywords=keywords)
                                    cache[key] = (now, '[语音转写]\n' + text)
                                except VoiceError as error:
                                    cache[key] = (now, '[语音未能转写：' + str(error) + ']')
                                while len(cache) > 256:
                                    cache.popitem(last=False)
                            output.append(Plain(text=cache[key][1]))
                        return MessageChain(output)
                    output = []
                    for part in chain:
                        if isinstance(part, Quote):
                            output.append(part.model_copy(update={'origin': await convert(part.origin, str(part.id))}))
                        else:
                            output.extend(await convert([part], str(getattr(event, 'message_id', ''))))
                    await bot.assert_execution_active()
                    current = configured(bot)
                    if current and current['receive'] and current['revision'] == config['revision']:
                        event.message_chain = MessageChain(output)
        finally:
            state['pending'] -= 1
    except asyncio.CancelledError:
        if event_recalled(adapter, event):
            return False
        raise
    except Exception:
        # Do not break QQ ingress or invent a transcript on timeout/config failure.
        event.message_chain = MessageChain([Plain(text='[语音暂时未能读取]') if isinstance(p, Voice) else p for p in chain])
    return not event_recalled(adapter, event)


async def reply(service, args):
    """Reserve one response before paying; revalidate cancellation before delivery."""
    bot = service.adapter._runtime
    config = configured(bot)
    if not config or not config['reply']:
        raise VoiceError('voice_disabled', '语音回复尚未启用，可以先用文字回复。')
    if service.state.get('voice_reply_attempted'):
        raise VoiceError('voice_duplicate', '本轮已经尝试语音回复，不重复生成或发送。')
    if getattr(service, 'check_live', None):
        await service.check_live()
    identity, old = await asyncio.to_thread(service.store.reserve, service.scope_id, service.chat, service.request,
        'voice_reply', {'text': args['text'], 'revision': config['revision']})
    if old:
        if old['status'] == 'confirmed':
            object.__setattr__(service.query, '_qq_action_confirmed', True)
        return {**old, 'operation': 'voice_reply'}
    service.state['voice_reply_attempted'] = True
    try:
        audio, spoken = await client.synthesize(config, args['text'], style=args.get('tone', ''))
    except VoiceError as error:
        await asyncio.to_thread(service.store.finish, identity, 'failed', {'code': error.code})
        return {'status': 'error', 'operation': 'voice_reply', 'error': str(error), 'fallback': '用文字继续回复，不要重复生成。'}
    if getattr(service, 'check_live', None):
        await service.check_live()
    current = configured(bot)
    if not current or not current['reply'] or current['revision'] != config['revision']:
        raise VoiceError('voice_disabled', '语音设置已变化，本条语音没有发送。')
    # A fresh authorization also catches group media permission changes.
    from qunling.proactive.config import ConfigStore
    fresh = ConfigStore(str(service.bot_id))
    values = fresh.read()
    if not fresh.read_valid or not values.get('qq_actions', {}).get('enabled', True) or (service.kind == 'group'
            and not values.get('groups', {}).get(service.chat_id, {}).get('qq_permissions', {}).get('media', True)):
        raise VoiceError('voice_disabled', '本群语音发送权限已关闭。')
    if service.kind == 'group':
        await service.member(service.actor)
    if getattr(service, 'check_live', None):
        await service.check_live()
    destination = {'group_id': service.chat_id} if service.kind == 'group' else {'user_id': service.actor}
    try:
        result = await service.raw('send_group_msg' if service.kind == 'group' else 'send_private_msg',
            **destination, message=[{'type': 'record', 'data': {'file': 'base64://' + base64.b64encode(audio).decode()}}])
        if not isinstance(result, dict) or result.get('message_id') is None:
            raise ValueError('Missing receipt')
    except Exception:
        return {'status': 'unconfirmed', 'operation': 'voice_reply', 'error': 'QQ 没有返回语音发送回执，不要重发。'}
    receipt = {'operation': 'voice_reply', 'message_id': str(result['message_id'])}
    await asyncio.to_thread(service.store.finish, identity, 'confirmed', receipt)
    object.__setattr__(service.query, '_qq_action_confirmed', True)
    try:
        service.record_sent('voice_reply', args, receipt)
    except Exception:
        pass
    return {'status': 'confirmed', **receipt, 'ai_generated': True, 'spoken_text': spoken,
            'note': '语音已发送，不要再发一遍相同文字或解释发送过程。'}
