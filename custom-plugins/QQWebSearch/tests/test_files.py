from __future__ import annotations

import asyncio
import hashlib
import io
import struct
from unittest.mock import AsyncMock
import zipfile

import pytest
from PIL import Image

from pkg import files
from pkg.errors import WebError
from pkg.network import Fetched


def sfnt(signature=b"\x00\x01\x00\x00"):
    header = signature + struct.pack(">HHHH", 1, 16, 0, 0)
    table = b"name" + struct.pack(">III", 0, 28, 4)
    return header + table + b"test"


def woff():
    header = (
        b"wOFF"
        + b"\x00\x01\x00\x00"
        + struct.pack(">IHHIHHIIIII", 68, 1, 0, 32, 1, 0, 0, 0, 0, 0, 0)
    )
    table = b"name" + struct.pack(">IIII", 64, 4, 4, 0)
    return header + table + b"test"


def font_collection():
    font = bytearray(sfnt())
    struct.pack_into(">I", font, 20, 44)
    return b"ttcf\x00\x01\x00\x00" + struct.pack(">II", 1, 16) + font


def woff2_header():
    header = bytearray(48)
    header[:8] = b"wOF2\x00\x01\x00\x00"
    struct.pack_into(">IHHII", header, 8, 52, 1, 0, 32, 2)
    return bytes(header) + b"\x05\x04\x01\x02"


def office(extension):
    output = io.BytesIO()
    prefix = {
        "docx": "word/document.xml",
        "xlsx": "xl/workbook.xml",
        "pptx": "ppt/presentation.xml",
    }[extension]
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(prefix, "<document/>")
    return output.getvalue()


def bitmap(format_name):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format=format_name)
    return output.getvalue()


@pytest.mark.asyncio
async def test_github_blob_font_is_fetched_as_raw_and_original_bytes_are_retained(
    monkeypatch,
):
    raw = sfnt()
    fetch = AsyncMock(
        return_value=Fetched(
            "https://raw.githubusercontent.com/owner/repo/main/fonts/font.ttf",
            raw,
            "application/octet-stream",
        )
    )
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    result = await files.download_file(
        "https://github.com/owner/repo/blob/main/fonts/font.ttf?raw=true#preview"
    )
    fetch.assert_awaited_once_with(
        "https://raw.githubusercontent.com/owner/repo/main/fonts/font.ttf",
        max_bytes=20 * 1024 * 1024,
        timeout=60,
    )
    assert result == {
        "data": raw,
        "name": "font.ttf",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_url": fetch.return_value.url,
        "content_type": "application/octet-stream",
    }


@pytest.mark.asyncio
async def test_final_redirect_url_and_rfc5987_filename_are_used(monkeypatch):
    response = Fetched(
        "https://cdn.example.test/final/fallback.ttf?sig=opaque",
        sfnt(),
        "font/ttf",
        "attachment; filename=\"fallback.ttf\"; filename*=UTF-8''%E4%B8%AD%E6%96%87.ttf",
    )
    monkeypatch.setattr(files, "fetch_public_file", AsyncMock(return_value=response))
    result = await files.download_file("https://example.test/download")
    assert result["name"] == "中文.ttf"
    assert result["source_url"] == response.url


@pytest.mark.asyncio
async def test_user_filename_overrides_disposition_and_url(monkeypatch):
    fetch = AsyncMock(
        return_value=Fetched(
            "https://example.test/download",
            sfnt(),
            "",
            "attachment; filename=server-name.ttf",
        )
    )
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    result = await files.download_file(
        "https://example.test/download", filename="我的字体.ttf"
    )
    assert result["name"] == "我的字体.ttf"


@pytest.mark.asyncio
async def test_url_leaf_is_decoded_without_treating_query_as_filename(monkeypatch):
    fetch = AsyncMock(
        return_value=Fetched(
            "https://example.test/%E4%B8%AD%E6%96%87.txt?name=evil.exe",
            b"content",
            "text/plain",
        )
    )
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    assert (await files.download_file("https://example.test/start"))[
        "name"
    ] == "中文.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "../file.ttf",
        r"..\file.ttf",
        "/absolute.ttf",
        r"C:\file.ttf",
        "C:file.ttf",
        "font.ttf:stream",
        "font\n.ttf",
        "font\x00.ttf",
        "font\u202e.ttf",
        "font.ttf ",
        "font.ttf.",
        "CON.ttf",
        "CON .ttf",
        "LPT1.txt",
        "ＣＯＮ.txt",
        "[CQ:image,file=abc].txt",
        "",
        12,
        "中" * 100 + ".txt",
    ],
)
async def test_unsafe_explicit_names_fail_before_downloading(monkeypatch, name):
    fetch = AsyncMock()
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    with pytest.raises(WebError):
        await files.download_file("https://example.test/file", name)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    [
        'attachment; filename="../font.ttf"',
        "attachment; filename*=UTF-8''..%2Ffont.ttf",
        "attachment; filename*=UTF-8''font%00.ttf",
        "attachment; filename*=UTF-8''%FF.ttf",
        "attachment; filename*=unknown''font.ttf",
        "attachment; filename=one.ttf; filename=two.ttf",
        "attachment; filename*0*=UTF-8''font; filename*1*=.ttf",
    ],
)
async def test_untrusted_disposition_cannot_supply_unsafe_or_ambiguous_name(
    monkeypatch, disposition
):
    monkeypatch.setattr(
        files,
        "fetch_public_file",
        AsyncMock(
            return_value=Fetched(
                "https://example.test/safe.ttf", sfnt(), "", disposition
            )
        ),
    )
    with pytest.raises(WebError):
        await files.download_file("https://example.test/download")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/font.ttf",
        "http://127.0.0.1/font.ttf",
        "https://name:password@example.test/font.ttf",
        "https://github.com/owner/repo/blob/main/../font.ttf",
        "https://github.com/owner/repo/blob/main/%2e%2e/font.ttf",
        "https://github.com/owner/repo/blob/main/a%2Fb.ttf",
        "https://github.com/owner/repo/blob/main/font%00.ttf",
        "https://github.com/owner/repo/blob/main/",
    ],
)
async def test_unsafe_urls_are_rejected_without_network(monkeypatch, url):
    fetch = AsyncMock()
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    with pytest.raises(WebError):
        await files.download_file(url)
    fetch.assert_not_awaited()


def test_only_actual_github_blob_host_is_rewritten():
    original = "https://github.com.example.test/owner/repo/blob/main/font.ttf"
    assert files.normalize_download_url(original) == original
    raw = "https://raw.githubusercontent.com/owner/repo/main/font.ttf"
    assert files.normalize_download_url(raw) == raw
    branch = "https://github.com/owner/repo/blob/release/v1/fonts/%E4%B8%AD.ttf"
    assert (
        files.normalize_download_url(branch)
        == "https://raw.githubusercontent.com/owner/repo/release/v1/fonts/%E4%B8%AD.ttf"
    )


@pytest.mark.parametrize(
    "extension,raw",
    [
        ("ttf", sfnt()),
        ("otf", sfnt(b"OTTO")),
        ("woff", woff()),
        ("woff2", woff2_header()),
        ("ttc", font_collection()),
        ("txt", "中文文本".encode()),
        ("txt", "中文文本".encode("gb18030")),
        ("txt", "中文文本".encode("utf-16")),
        ("txt", "中文文本".encode("utf-32")),
        ("md", b"# document\n"),
        ("csv", b"a,b\n1,2\n"),
        ("json", b'{"name":"font"}'),
        ("pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"),
        ("rar", b"Rar!\x1a\x07\x00" + b"\0" * 20),
        ("7z", b"7z\xbc\xaf\x27\x1c" + b"\0" * 26),
    ],
)
def test_supported_type_headers_and_text_encodings(extension, raw):
    files.validate_file_body(raw, "sample." + extension, "application/octet-stream")


@pytest.mark.parametrize(
    "extension,format_name",
    [
        ("png", "PNG"),
        ("jpg", "JPEG"),
        ("gif", "GIF"),
        ("webp", "WEBP"),
        ("bmp", "BMP"),
        ("tiff", "TIFF"),
    ],
)
def test_common_image_files_are_kept_as_attachments(extension, format_name):
    files.validate_file_body(
        bitmap(format_name), "sample." + extension, "image/" + extension
    )


@pytest.mark.parametrize("extension", ["docx", "xlsx", "pptx"])
def test_office_container_is_checked_without_reading_or_extracting_entries(
    monkeypatch, extension
):
    raw = office(extension)

    def forbidden(*args, **kwargs):
        pytest.fail("attachment validation must not read or extract an archive entry")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    files.validate_file_body(raw, "sample." + extension, "application/octet-stream")
    wrong = office("xlsx" if extension == "docx" else "docx")
    with pytest.raises(WebError):
        files.validate_file_body(wrong, "sample." + extension, "")


def test_archive_attachments_do_not_unpack_or_execute(monkeypatch):
    raw = office("docx")

    def forbidden(*args, **kwargs):
        pytest.fail("archive transfer must not execute or unpack anything")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    files.validate_file_body(raw, "archive.zip", "application/zip")
    files.validate_file_body(raw, "archive.cbz", "application/zip")


@pytest.mark.parametrize(
    "extension,raw",
    [
        ("ttf", b"\0\1\0\0"),
        ("otf", sfnt()),
        ("woff", b"wOFF" + b"\0" * 40),
        ("woff2", b"wOF2" + b"\0" * 44),
        ("ttc", b"ttcf" + b"\0" * 20),
        ("zip", b"PK\x03\x04"),
        ("rar", b"Rar!\x1a\x07\x00"),
        ("7z", b"7z\xbc\xaf\x27\x1c"),
        ("pdf", b"%PDF-1.7\nmissing end marker"),
        ("png", b"\x89PNG\r\n\x1a\n"),
        ("ttf", b"MZ" + b"\0" * 128),
        ("ttf", b'{"error":"login required"}'),
    ],
)
def test_fake_or_truncated_known_types_fail(extension, raw):
    with pytest.raises(WebError):
        files.validate_file_body(raw, "sample." + extension, "application/octet-stream")


def test_font_table_bounds_are_checked_before_transfer():
    raw = bytearray(sfnt())
    struct.pack_into(">I", raw, 20, 10_000)
    with pytest.raises(WebError):
        files.validate_file_body(bytes(raw), "sample.ttf", "font/ttf")
    raw = bytearray(woff())
    struct.pack_into(">I", raw, 8, len(raw) + 1)
    with pytest.raises(WebError):
        files.validate_file_body(bytes(raw), "sample.woff", "font/woff")


@pytest.mark.parametrize(
    "raw,content_type",
    [
        (b"<!DOCTYPE html><html>Sign in</html>", "application/octet-stream"),
        (b"\xef\xbb\xbf \n<!-- login -->\n<html>login</html>", ""),
        ("<html>登录</html>".encode("utf-16"), ""),
        ("<html>登录</html>".encode("utf-32"), ""),
        (b"<script>login()</script>", "text/plain"),
        (b"not even html", "text/html; charset=utf-8"),
        (b"<html/>", "application/xhtml+xml"),
    ],
)
def test_web_and_login_pages_cannot_be_renamed_to_text_or_fonts(raw, content_type):
    for name in ("font.ttf", "document.txt"):
        with pytest.raises(WebError, match="网页或登录页"):
            files.validate_file_body(raw, name, content_type)


@pytest.mark.asyncio
async def test_empty_and_oversized_file_fail(monkeypatch):
    for body, url in [
        (b"", "https://example.test/empty.txt"),
        (b"x" * (20 * 1024 * 1024 + 1), "https://example.test/large.txt"),
    ]:
        monkeypatch.setattr(
            files, "fetch_public_file", AsyncMock(return_value=Fetched(url, body, ""))
        )
        with pytest.raises(WebError):
            await files.download_file(url)


@pytest.mark.asyncio
async def test_downloader_errors_are_redacted_without_retry_and_cancellation_propagates(
    monkeypatch, caplog
):
    fetch = AsyncMock(side_effect=OSError("https://private.example/?secret=never-log"))
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    with pytest.raises(WebError) as caught:
        await files.download_file("https://example.test/file.txt")
    fetch.assert_awaited_once()
    assert "never-log" not in str(caught.value) + caplog.text
    monkeypatch.setattr(
        files, "fetch_public_file", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await files.download_file("https://example.test/file.txt")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,body",
    [
        ("program.exe", b"MZ" + b"\0" * 100),
        ("script.ps1", b"Write-Output 'example'"),
        ("source.py", b"print('example')"),
        ("package.deb", b"!<arch>\nopaque package"),
        ("model.safetensors", b"\x00\xffopaque"),
        ("binary.txt", b"\0\xffopaque"),
        ("unknown.custom", b"\0\xffopaque"),
        ("LICENSE", b"license text"),
    ],
)
async def test_arbitrary_software_source_and_binary_are_inert_attachments(
    monkeypatch, name, body
):
    fetch = AsyncMock(
        return_value=Fetched(
            "https://example.test/" + name, body, "application/x-unknown"
        )
    )
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    result = await files.download_file("https://example.test/" + name)
    assert result["name"] == name
    assert result["data"] == body
    assert result["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_missing_url_basename_has_safe_fallback_without_extension_guess(
    monkeypatch,
):
    fetch = AsyncMock(
        return_value=Fetched(
            "https://example.test/", b"\0opaque binary", "application/octet-stream"
        )
    )
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    assert (await files.download_file("https://example.test/"))[
        "name"
    ] == "download.bin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,filename,mime",
    [
        ("https://example.test/page.html", None, "text/html"),
        ("https://example.test/page.htm", None, "text/html; charset=utf-8"),
        (
            "https://example.test/download",
            "保存的页面.html",
            "application/octet-stream",
        ),
    ],
)
async def test_explicit_html_download_is_allowed_as_an_attachment(
    monkeypatch, url, filename, mime
):
    raw = b"<!doctype html><html><body>Example page</body></html>"
    fetch = AsyncMock(return_value=Fetched(url, raw, mime))
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    result = await files.download_file(url, filename)
    assert result["data"] == raw
    assert result["name"].endswith((".html", ".htm"))


@pytest.mark.asyncio
async def test_redirect_to_login_html_cannot_turn_binary_download_into_html_attachment(
    monkeypatch,
):
    fetch = AsyncMock(
        return_value=Fetched(
            "https://example.test/login.html",
            b"<html>Sign in</html>",
            "text/html",
            "attachment; filename=login.html",
        )
    )
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    with pytest.raises(WebError, match="网页或登录页"):
        await files.download_file("https://example.test/package.exe")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [int(12.3 * 1024 * 1024), 20 * 1024 * 1024])
async def test_large_opaque_attachment_is_retained_up_to_twenty_mib(monkeypatch, size):
    raw = b"\0\xff" + b"x" * (size - 2)
    fetch = AsyncMock(
        return_value=Fetched(
            "https://example.test/file.custom", raw, "application/x-custom"
        )
    )
    monkeypatch.setattr(files, "fetch_public_file", fetch)
    result = await files.download_file("https://example.test/file.custom")
    assert result["data"] is raw
    assert result["size"] == size
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    fetch.assert_awaited_once_with(
        "https://example.test/file.custom", max_bytes=20 * 1024 * 1024, timeout=60
    )
