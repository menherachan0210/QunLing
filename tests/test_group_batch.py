"""Only frozen same-scope image identities cross into the group reader."""
from types import SimpleNamespace as NS
import time

import pytest

from qunling import group_batch as batch
from qunling.group_knowledge import workspace_id


KEY = (workspace_id('instance', 'workspace'), 'bot', '1001', '201', '-990')


def picture(key='501', **changes):
    return NS(**(dict(bot_id='bot', group_id='1001', sender_id='201', sender_name='成员',
        message_id=key, timestamp=time.time(), text='', reply_to=None, is_bot=False, has_media=True) | changes))


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv('QUNLING_DATA_DIR', str(tmp_path))


def test_snapshot_contains_only_media_and_preserves_bot_receipt_identity():
    messages = [picture(), picture('502', is_bot=True, sender_id='bot'),
                picture('503', has_media=False), picture('504', group_id='elsewhere')]
    assert batch.publish(*KEY, messages, '999')
    rows = batch.recall(*KEY)
    assert [r['message_id'] for r in rows] == ['501', '502']
    assert rows[-1]['sender_id'] == '999'
    messages.append(picture('505'))
    assert batch.recall(*KEY) == rows


@pytest.mark.parametrize('index,value', [(0, '0' * 64), (1, 'other'), (2, '2001'), (3, '202'), (4, '-991')])
def test_another_scope_sender_or_request_cannot_read_snapshot(index, value):
    assert batch.publish(*KEY, [picture()], '999')
    other = list(KEY)
    other[index] = value
    assert batch.recall(*other) == []


def test_ttl_and_image_count_are_bounded(monkeypatch):
    now = time.time()
    monkeypatch.setattr(batch, 'time', NS(time=lambda: now))
    assert batch.publish(*KEY, [picture(str(i + 1)) for i in range(20)], '999')
    assert len(batch.recall(*KEY)) == batch.MAX_IMAGES
    monkeypatch.setattr(batch, 'time', NS(time=lambda: now + batch.TTL))
    assert batch.recall(*KEY) == []
