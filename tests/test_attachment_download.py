import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from qunling import attachment_download as transfer


class Response:
    def __init__(self, body, *, status=200, headers=None, error=None):
        self.body, self.status, self.headers, self.error = body, status, headers or {}, error
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        yield self.body
        if self.error:
            raise self.error


def transport(monkeypatch, responses):
    calls = []

    class Session:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['auto_decompress'] is False
            self.headers = kwargs['headers']
            assert not any(k.lower() in {'authorization', 'cookie'} for k in self.headers)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, *, allow_redirects):
            assert allow_redirects is False
            calls.append((url, self.headers))
            return responses.pop(0)

    monkeypatch.setattr(transfer.aiohttp, 'TCPConnector', lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(transfer.aiohttp, 'ClientSession', Session)
    return calls


@pytest.mark.asyncio
async def test_broken_connection_resumes_exact_bytes_without_duplicate_download(monkeypatch, tmp_path):
    calls = transport(monkeypatch, [
        Response(b'abc', headers={'Content-Length': '6', 'ETag': '"same"'}, error=aiohttp.ClientPayloadError()),
        Response(b'def', status=206, headers={'Content-Length': '3', 'Content-Range': 'bytes 3-5/6', 'ETag': '"same"'}),
    ])
    path = tmp_path / 'private.part'
    updates = []
    async def progress(received, total):
        updates.append((received, total))
    await transfer.download_file('https://public.example/file', path, expected_size=6, progress=progress)
    assert path.read_bytes() == b'abcdef'
    assert calls[1][1]['Range'] == 'bytes=3-'
    assert calls[1][1]['If-Range'] == '"same"'
    assert updates[-1] == (6, 6)
    assert 'https' not in path.with_suffix('.json').read_text()
    await transfer.download_file('https://public.example/new-ticket', path, expected_size=6)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_interrupted_transfer_can_resume_in_a_later_call(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer, 'MAX_ATTEMPTS', 1)
    calls = transport(monkeypatch, [Response(b'abc', headers={'Content-Length': '6', 'Last-Modified': 'date'},
        error=TimeoutError()), Response(b'def', status=206,
        headers={'Content-Range': 'bytes 3-5/6', 'Last-Modified': 'date', 'Content-Length': '3'})])
    path = tmp_path / 'resume.part'
    with pytest.raises(transfer.DownloadError, match='保留'):
        await transfer.download_file('https://public.example/file', path, expected_size=6)
    assert path.read_bytes() == b'abc'
    await transfer.download_file('https://public.example/fresh-ticket', path, expected_size=6)
    assert path.read_bytes() == b'abcdef' and calls[1][1]['Range'] == 'bytes=3-'


@pytest.mark.asyncio
async def test_range_ignored_overwrites_instead_of_appending(monkeypatch, tmp_path):
    transport(monkeypatch, [Response(b'old', headers={'Content-Length': '6', 'ETag': '"old"'}, error=TimeoutError()),
        Response(b'newnew', headers={'Content-Length': '6', 'ETag': '"new"'})])
    path = tmp_path / 'changed.part'
    await transfer.download_file('https://public.example/file', path, expected_size=6)
    assert path.read_bytes() == b'newnew'


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [
    {'Content-Range': 'bytes 2-5/6', 'ETag': '"same"'},
    {'Content-Range': 'bytes 3-5/6', 'ETag': '"changed"'},
    {'Content-Range': 'bytes 3-6/7', 'ETag': '"same"'},
])
async def test_mismatched_range_never_concatenates(monkeypatch, tmp_path, headers):
    transport(monkeypatch, [Response(b'abc', headers={'Content-Length': '6', 'ETag': '"same"'}, error=TimeoutError()),
        Response(b'bad', status=206, headers=headers)])
    path = tmp_path / 'unsafe.part'
    with pytest.raises(transfer.DownloadError, match='未拼接'):
        await transfer.download_file('https://public.example/file', path, expected_size=6)
    assert path.read_bytes() == b'abc'


@pytest.mark.asyncio
async def test_private_redirect_size_limit_and_cancellation(monkeypatch, tmp_path):
    calls = transport(monkeypatch, [Response(b'', status=302, headers={'Location': 'http://127.0.0.1/secret'})])
    with pytest.raises(transfer.DownloadError):
        await transfer.download_file('https://public.example/file', tmp_path / 'redirect.part')
    assert len(calls) == 1
    transport(monkeypatch, [Response(b'toolarge', headers={'Content-Length': '3'})])
    with pytest.raises(transfer.DownloadError):
        await transfer.download_file('https://public.example/file', tmp_path / 'large.part', expected_size=3)
    transport(monkeypatch, [Response(b'abc', headers={'Content-Length': '6', 'ETag': '"same"'}, error=asyncio.CancelledError())])
    path = tmp_path / 'cancel.part'
    with pytest.raises(asyncio.CancelledError):
        await transfer.download_file('https://public.example/file', path, expected_size=6)
    assert path.read_bytes() == b'abc'


def test_download_deadline_accepts_slow_24_mib_without_raising_size_limit():
    assert transfer.budget(25124189) >= 400
    assert transfer.budget(40 * 1024 * 1024) <= 720
    assert transfer.MAX_BYTES == 40 * 1024 * 1024


@pytest.mark.asyncio
async def test_replacement_is_truncated_before_new_validator_is_checkpointed(monkeypatch, tmp_path):
    transport(monkeypatch, [Response(b'newnew', headers={'Content-Length': '6', 'ETag': '"new"'})])
    path = tmp_path / 'replace.part'
    path.write_bytes(b'old')
    transfer._save(path.with_suffix('.json'), {'total': 6, 'validator': '"old"', 'validator_key': 'ETag'})
    save = transfer._save

    def crash_after_checkpoint(meta, state):
        save(meta, state)
        raise asyncio.CancelledError()

    monkeypatch.setattr(transfer, '_save', crash_after_checkpoint)
    with pytest.raises(asyncio.CancelledError):
        await transfer.download_file('https://public.example/file', path, expected_size=6)
    assert path.read_bytes() == b''


@pytest.mark.asyncio
@pytest.mark.parametrize('matching', [True, False])
async def test_qq_cdn_without_etag_uses_immutable_upload_and_verified_overlap(monkeypatch, tmp_path, matching):
    prefix = b'a' * 70000
    content = b'a' * 65536 + b'end' if matching else b'changed' + b'a' * (65536 - 7) + b'end'
    calls = transport(monkeypatch, [
        Response(prefix, headers={'Content-Length': '70003'}, error=TimeoutError()),
        Response(content, status=206, headers={'Content-Range': 'bytes 4464-70002/70003', 'Content-Length': '65539'}),
    ])
    path = tmp_path / 'qq.part'
    if matching:
        await transfer.download_file('https://tjc-download.ftn.qq.com/file', path, expected_size=70003,
                                     immutable_id='trusted-qq-upload-id')
        assert path.read_bytes() == prefix + b'end'
    else:
        with pytest.raises(transfer.DownloadError, match='校验'):
            await transfer.download_file('https://tjc-download.ftn.qq.com/file', path, expected_size=70003,
                                         immutable_id='trusted-qq-upload-id')
        assert path.read_bytes() == prefix
    assert calls[1][1]['Range'] == 'bytes=4464-'
