"""Authenticated operator proxy; the Host owns the request metadata store."""
import re
from urllib.parse import urlencode

from bootstrap.api import SetupError
from qunling.request_journal import parse_filters


def requests(manager, values):
    filters = parse_filters(values)
    bot_uuid = manager.state.read(default={}).get('bot_uuid')
    if not isinstance(bot_uuid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', bot_uuid):
        return dict(available=False, requests=[])
    try:
        return manager.api.call('GET', '/api/v1/platform/bots/' + bot_uuid + '/requests?' + urlencode(filters))
    except SetupError:
        raise SetupError('请求记录暂时无法读取，请稍后刷新。') from None
