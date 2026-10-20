"""Download opaque attachment bytes without executing, unpacking, or saving them."""

from __future__ import annotations

from email.message import Message
from email.utils import collapse_rfc2231_value
import hashlib
import io
import re
import struct
import unicodedata
from urllib.parse import unquote, urldefrag, urlsplit, urlunsplit
import zipfile

from pkg.errors import WebError
from pkg.network import MAX_FILE_BYTES, fetch_public_file, validate_public_url

MAX_DOWNLOAD_BYTES = MAX_FILE_BYTES
FONT_EXTENSIONS = {"ttf", "otf", "ttc", "woff", "woff2"}
OFFICE_EXTENSIONS = {"docx", "xlsx", "pptx"}
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    prefix + str(number) for prefix in ("com", "lpt") for number in range(1, 10)
}


def safe_filename(value: str) -> str:
    if not isinstance(value, str):
        raise WebError("附件文件名无效。")
    value = unicodedata.normalize("NFKC", value)
    if (
        not value
        or value != value.strip()
        or len(value) > 180
        or len(value.encode("utf-8")) > 240
        or value.endswith(".")
        or value in {".", ".."}
        or any(char in value for char in '/\\:*?"<>|')
        or any(unicodedata.category(char).startswith("C") for char in value)
        or "[cq:" in value.lower()
        or value.split(".", 1)[0].rstrip(" .").casefold() in WINDOWS_RESERVED
    ):
        raise WebError("附件文件名必须是安全的单个文件名，不能包含路径或控制字符。")
    return value


def normalize_download_url(value: str) -> str:
    if not isinstance(value, str):
        raise WebError("附件地址无效。")
    url = validate_public_url(urldefrag(value)[0])
    parsed = urlsplit(url)
    if parsed.hostname.lower() != "github.com":
        return url
    parts = parsed.path.split("/")
    if len(parts) < 6 or parts[3] != "blob":
        return url
    owner, repository = parts[1:3]
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in (owner, repository)):
        raise WebError("GitHub 单文件地址无效。")
    # Preserve encoded non-ASCII paths while rejecting encoded separators and
    # traversal before crossing from GitHub HTML to its raw-content host.
    for segment in (owner, repository, *parts[4:]):
        try:
            decoded = unquote(segment, encoding="utf-8", errors="strict")
        except UnicodeError:
            raise WebError("GitHub 单文件地址编码无效。") from None
        if (
            not decoded
            or decoded in {".", ".."}
            or any(char in decoded for char in "/\\")
            or any(unicodedata.category(char).startswith("C") for char in decoded)
        ):
            raise WebError("GitHub 单文件地址路径无效。")
    return validate_public_url(
        urlunsplit(
            (
                "https",
                "raw.githubusercontent.com",
                "/".join(parts[:3] + parts[4:]),
                "",
                "",
            )
        )
    )


def _disposition_filename(header: str) -> str | None:
    if not header:
        return None
    if (
        not isinstance(header, str)
        or len(header) > 2048
        or any(ord(char) < 32 or ord(char) == 127 for char in header)
        or re.search(r"filename\*\d", header, re.IGNORECASE)
    ):
        raise WebError("附件文件名信息无效。")
    message = Message()
    message["Content-Disposition"] = header
    values = [
        value
        for name, value in message.get_params(
            header="content-disposition", unquote=True
        )[1:]
        if name.lower() == "filename"
    ]
    extended = [value for value in values if isinstance(value, tuple)]
    ordinary = [value for value in values if not isinstance(value, tuple)]
    if len(extended) > 1 or len(ordinary) > 1:
        raise WebError("附件文件名信息存在歧义。")
    if extended:
        charset = (extended[0][0] or "").lower()
        if charset not in {
            "utf-8",
            "utf8",
            "iso-8859-1",
            "latin-1",
            "ascii",
            "us-ascii",
        }:
            raise WebError("附件文件名编码不支持。")
        try:
            return collapse_rfc2231_value(extended[0], errors="strict")
        except (UnicodeError, LookupError):
            raise WebError("附件文件名编码无效。") from None
    return ordinary[0] if ordinary else None


def _sfnt(raw: bytes, offset: int = 0, *, magic: bytes | None = None) -> bool:
    if len(raw) < offset + 12:
        return False
    signature = raw[offset : offset + 4]
    if signature not in ({magic} if magic else {b"\x00\x01\x00\x00", b"true", b"OTTO"}):
        return False
    count = int.from_bytes(raw[offset + 4 : offset + 6], "big")
    if not 1 <= count <= 4096 or offset + 12 + count * 16 > len(raw):
        return False
    for index in range(count):
        entry = offset + 12 + index * 16
        start, size = struct.unpack_from(">II", raw, entry + 8)
        if start > len(raw) or size > len(raw) - start:
            return False
    return True


def _font_matches(raw: bytes, extension: str) -> bool:
    if extension in {"ttf", "otf"}:
        return _sfnt(raw, magic=b"OTTO" if extension == "otf" else None) and (
            extension != "ttf" or raw[:4] in {b"\x00\x01\x00\x00", b"true"}
        )
    if extension == "ttc":
        if len(raw) < 16 or raw[:8] not in {
            b"ttcf\x00\x01\x00\x00",
            b"ttcf\x00\x02\x00\x00",
        }:
            return False
        count = int.from_bytes(raw[8:12], "big")
        return (
            1 <= count <= 4096
            and 12 + 4 * count <= len(raw)
            and all(
                _sfnt(raw, int.from_bytes(raw[12 + 4 * i : 16 + 4 * i], "big"))
                for i in range(count)
            )
        )
    minimum = 44 if extension == "woff" else 48
    signature = b"wOFF" if extension == "woff" else b"wOF2"
    if len(raw) < minimum or raw[:4] != signature:
        return False
    size, count, reserved = struct.unpack_from(">IHH", raw, 8)
    if size != len(raw) or not 1 <= count <= 4096 or reserved:
        return False
    if extension == "woff":
        if 44 + count * 20 > len(raw):
            return False
        for index in range(count):
            offset, compressed, original = struct.unpack_from(
                ">III", raw, 48 + index * 20
            )
            if (
                not 0 < compressed <= original
                or offset > len(raw)
                or compressed > len(raw) - offset
            ):
                return False
        return True
    compressed = int.from_bytes(raw[20:24], "big")
    return 0 < compressed <= len(raw) - 48


def _is_html(raw: bytes) -> bool:
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        head = raw[:4096].decode("utf-32", errors="ignore").encode("utf-8")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        head = raw[:4096].decode("utf-16", errors="ignore").encode("utf-8")
    else:
        head = raw[:4096]
    head = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return bool(
        re.match(
            rb"(?:<!--.*?-->\s*)*(?:<\?xml[^>]*>\s*)?"
            rb"(?:<!doctype\s+html|<html\b|<head\b|<body\b|<script\b)",
            head,
            flags=re.DOTALL,
        )
    )


def validate_file_body(
    raw: bytes, name: str, content_type: str, *, allow_html: bool = False
) -> None:
    if not isinstance(raw, bytes) or not raw:
        raise WebError("附件内容为空或无效。")
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise WebError("单个附件超过 20 MiB，无法通过当前 QQ 通道发送。")
    if not allow_html and (
        content_type.split(";", 1)[0].strip().lower()
        in {"text/html", "application/xhtml+xml"}
        or _is_html(raw)
    ):
        raise WebError(
            "该地址返回的是网页或登录页，未取得可发送的附件。请提供公开文件直链。"
        )
    extension = name.rsplit(".", 1)[-1].casefold()
    valid = True
    if extension in FONT_EXTENSIONS:
        valid = _font_matches(raw, extension)
    elif extension == "pdf":
        valid = (
            bool(re.match(rb"%PDF-(?:1\.[0-9]|2\.0)[\r\n]", raw))
            and b"%%EOF" in raw[-2048:]
        )
    elif extension in {"zip", "cbz"} | OFFICE_EXTENSIONS:
        valid = len(raw) >= 22 and raw.startswith((b"PK\x03\x04", b"PK\x05\x06"))
        if valid and extension in OFFICE_EXTENSIONS:
            # Read only the ZIP central directory, never extract or decompress.
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    names = archive.namelist()
                prefix = {"docx": "word/", "xlsx": "xl/", "pptx": "ppt/"}[extension]
                valid = "[Content_Types].xml" in names and any(
                    item.startswith(prefix) for item in names
                )
            except (ValueError, OSError, zipfile.BadZipFile):
                valid = False
    elif extension in {"rar", "cbr"}:
        valid = (len(raw) >= 14 and raw.startswith(b"Rar!\x1a\x07\x00")) or (
            len(raw) >= 16 and raw.startswith(b"Rar!\x1a\x07\x01\x00")
        )
    elif extension in {"7z", "cb7"}:
        valid = len(raw) >= 32 and raw.startswith(b"7z\xbc\xaf\x27\x1c")
    elif extension == "png":
        valid = (
            len(raw) >= 45
            and raw.startswith(b"\x89PNG\r\n\x1a\n")
            and raw[12:16] == b"IHDR"
        )
    elif extension in {"jpg", "jpeg"}:
        valid = (
            len(raw) >= 8
            and raw.startswith(b"\xff\xd8\xff")
            and b"\xff\xd9" in raw[-32:]
        )
    elif extension == "gif":
        valid = len(raw) >= 14 and raw[:6] in {b"GIF87a", b"GIF89a"}
    elif extension == "webp":
        valid = len(raw) >= 20 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    elif extension == "bmp":
        valid = len(raw) >= 26 and raw[:2] == b"BM"
    elif extension in {"tif", "tiff"}:
        valid = len(raw) >= 8 and raw[:4] in {b"II*\x00", b"MM\x00*"}
    if not valid:
        raise WebError("附件内容与文件类型不符、格式不支持或文件不完整，未发送。")


async def download_file(url: str, filename: str | None = None) -> dict:
    """Return validated bytes and a safe leaf filename for the current-query store."""
    try:
        name = safe_filename(filename) if filename is not None else None
        source = normalize_download_url(url)
        fetched = await fetch_public_file(
            source, max_bytes=MAX_DOWNLOAD_BYTES, timeout=60
        )
        if name is None:
            proposed = _disposition_filename(fetched.content_disposition)
            if proposed is None:
                try:
                    proposed = (
                        unquote(
                            urlsplit(fetched.url).path.rsplit("/", 1)[-1],
                            encoding="utf-8",
                            errors="strict",
                        )
                        or "download.bin"
                    )
                except UnicodeError:
                    raise WebError("附件文件名编码无效。") from None
            name = safe_filename(proposed)
        html_name = name.rsplit(".", 1)[-1].lower() in {"html", "htm"}
        explicit_html = filename is not None and html_name
        original_html = urlsplit(source).path.rsplit(".", 1)[-1].lower() in {
            "html",
            "htm",
        }
        html_mime = fetched.content_type.split(";", 1)[0].strip().lower() in {
            "text/html",
            "application/xhtml+xml",
        }
        validate_file_body(
            fetched.body,
            name,
            fetched.content_type,
            allow_html=explicit_html or (html_name and original_html and html_mime),
        )
        return {
            "data": fetched.body,
            "name": name,
            "size": len(fetched.body),
            "sha256": hashlib.sha256(fetched.body).hexdigest(),
            "source_url": fetched.url,
            "content_type": fetched.content_type,
        }
    except WebError:
        raise
    except Exception:
        raise WebError("附件下载或校验失败，未自动重试。") from None
