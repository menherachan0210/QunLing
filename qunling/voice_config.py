"""Private, bot/workspace-scoped audio settings; public views never contain keys."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading

from bootstrap.state import write_json
from qunling.assets import data_root

VOICES = ('marin', 'cedar', 'coral', 'alloy', 'ash', 'ballad', 'echo', 'sage', 'shimmer', 'verse')
DEFAULTS = {'revision': '0', 'enabled': False, 'receive': True, 'reply': True,
            'voice': 'marin', 'style': '自然、轻松地说中文，像和朋友聊天。',
            'keywords': '群灵', 'max_seconds': 120, 'api_key': ''}
_lock = threading.RLock()


class VoiceError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.status = code, status
        super().__init__(message)


class VoiceConfig:
    def __init__(self, workspace, bot):
        if not all(isinstance(v, str) and v for v in (workspace, bot)):
            raise ValueError('Voice configuration requires a workspace and bot')
        self.key = hashlib.sha256(json.dumps([workspace, bot]).encode()).hexdigest()
        self.path = data_root() / 'voice' / (self.key + '.json')

    def read(self):
        with _lock:
            if self.path.is_symlink() or self.path.parent.is_symlink():
                raise VoiceError('voice_config', '语音配置路径不可用。', 503)
            if not self.path.exists():
                return dict(DEFAULTS)
            try:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                self.validate(value)
                return value
            except (OSError, ValueError, TypeError):
                raise VoiceError('voice_config', '语音配置无法读取，请在管理页重新检查。', 503) from None

    @staticmethod
    def validate(value):
        if (not isinstance(value, dict) or set(value) != set(DEFAULTS)
                or not isinstance(value['revision'], str)
                or not re.fullmatch(r'0|[a-f0-9]{32}', value['revision'])
                or any(type(value[k]) is not bool for k in ('enabled', 'receive', 'reply'))
                or value['voice'] not in VOICES
                or type(value['max_seconds']) is not int or not 15 <= value['max_seconds'] <= 180):
            raise VoiceError('voice_config', '语音设置无效，请检查开关、音色和时长。')
        for key, maximum in (('style', 400), ('keywords', 500), ('api_key', 256)):
            text = value[key]
            if (not isinstance(text, str) or len(text) > maximum
                    or any(ord(c) < 32 and c not in '\n\t' or 0xD800 <= ord(c) <= 0xDFFF for c in text)):
                raise VoiceError('voice_config', '语音设置包含无效内容或超过长度限制。')
        if value['api_key'] and not re.fullmatch(r'sk-[A-Za-z0-9_-]{20,250}', value['api_key']):
            raise VoiceError('voice_config', '请填写有效的 OpenAI API Key。')
        if value['enabled'] and not value['api_key']:
            raise VoiceError('voice_config', '启用语音前请先保存官方 API Key。')
        if len(value['keywords'].splitlines()) > 30 or any(c in value['keywords'] for c in '<>'):
            raise VoiceError('voice_config', '专名提示最多 30 行，不支持尖括号。')

    @staticmethod
    def public(value):
        return {**{k: v for k, v in value.items() if k != 'api_key'}, 'api_key_saved': bool(value['api_key'])}

    def save(self, values):
        if (not isinstance(values, dict) or set(values) != set(DEFAULTS)
                or not isinstance(values.get('api_key'), str)):
            raise VoiceError('voice_config', '语音设置不完整，请刷新后重试。')
        with _lock:
            previous = self.read()
            if values['revision'] != previous['revision']:
                raise VoiceError('voice_conflict', '语音设置已被修改，请刷新后再保存。', 409)
            result = {**values, 'api_key': values['api_key'] or previous['api_key']}
            self.validate(result)
            if result != previous:
                result['revision'] = secrets.token_hex(16)
                write_json(self.path, result)
            return self.public(result)


def for_bot(bot):
    return VoiceConfig(str(bot.execution_context.workspace_uuid), str(bot.bot_entity.uuid))
