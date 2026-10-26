"""Same-origin console proxy. The Host establishes bot, workspace and group ownership."""
from urllib.parse import urlencode

from bootstrap.api import SetupError
from bootstrap.proactive import _path
from qunling.console_memory import validate
from qunling.console_tasks import parse_filters


def tasks(manager, filters):
    return manager.api.call('GET', _path(manager).removesuffix('/proactive') + '/console/tasks?' + urlencode(parse_filters(filters)))


def memory(manager, values):
    validate(values)
    with manager.lock:
        if manager.update_inflight or manager.update_status()['busy'] or manager.pending:
            raise SetupError('配置或版本更新正在进行，请稍后管理记忆。')
        path = _path(manager).removesuffix('/proactive') + '/console/memory'
    return manager.api.call('POST', path, values)
