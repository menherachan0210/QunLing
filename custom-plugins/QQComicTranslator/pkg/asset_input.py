"""Adapt scoped image assets to the existing checkpointed comic archive pipeline."""
from __future__ import annotations

from dataclasses import dataclass
import io
import threading
import unicodedata
import warnings
import zipfile

from PIL import Image

from pkg.archive import (
    ArchiveError, MAX_ARCHIVE_BYTES, MAX_IMAGE_BYTES, MAX_PAGES, MAX_PIXELS,
    SUPPORTED_FORMATS, _natural_key,
)
from pkg.labelplus import render_labelplus
from qunling.assets import AssetStore, REF
from qunling.scope import Scope

_PREPARE_SLOTS = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class AssetInput:
    name: str
    raw: bytes
    # Mapping from trusted internal archive order to the original source names.
    page_names: tuple[str, ...] | None = None


def asset_parameters(params) -> list[str]:
    if not isinstance(params, dict) or "asset_refs" not in params or set(params) - {"asset_refs", "password"}:
        raise ArchiveError("只接受 asset_refs 和可选 password，不接受路径、URL、文件ID或收件人。")
    refs = params["asset_refs"]
    if (
        not isinstance(refs, list) or not 1 <= len(refs) <= MAX_PAGES
        or any(not isinstance(ref, str) or not REF.fullmatch(ref) for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        raise ArchiveError("请选择本轮可见的 1 至 100 个不重复 asset_ref。")
    return refs


def _archive_kind(raw: bytes) -> bool:
    return raw.startswith((
        b"PK\x03\x04", b"PK\x05\x06", b"Rar!\x1a\x07\x00",
        b"Rar!\x1a\x07\x01\x00", b"7z\xbc\xaf\x27\x1c",
    ))


def _image_format(raw: bytes) -> str:
    if not 0 < len(raw) <= MAX_IMAGE_BYTES:
        raise ArchiveError("单张漫画图片必须非空且不超过 20 MiB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw), formats=list(SUPPORTED_FORMATS)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                    raise ArchiveError("图片像素超过 4000 万或尺寸无效。")
                if getattr(image, "n_frames", 1) != 1:
                    raise ArchiveError("漫画翻译暂不支持动画或多帧图片。")
                image.load()  # Do not charge for earlier pages before discovering a bad input image.
                return image.format
    except ArchiveError:
        raise
    except Exception:
        raise ArchiveError("所选资产含损坏图片或不支持的格式；支持 PNG、JPEG、WEBP、BMP。") from None


def prepare_assets(store: AssetStore, scope: Scope, refs: list[str], context: dict) -> AssetInput:
    if not _PREPARE_SLOTS.acquire(timeout=5):
        raise ArchiveError("当前图片预检任务较多，请稍后再试。")
    try:
        return _prepare_assets(store, scope, refs, context)
    finally:
        _PREPARE_SLOTS.release()


def _prepare_assets(store: AssetStore, scope: Scope, refs: list[str], context: dict) -> AssetInput:
    # Validate metadata and the aggregate budget before reading any blob bytes.
    for ref in refs:
        store.require_visible(scope, ref, context)
    items = [store.get(scope, ref) for ref in refs]
    if sum(item["size"] for item in items) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("所选漫画资产总大小超过 40 MiB，请分批处理。")
    entries = [(item, store.read(scope, item["asset_ref"])) for item in items]
    if any(_archive_kind(raw) for _, raw in entries):
        if len(entries) != 1:
            raise ArchiveError("压缩包请单独选择；也可先提取需要的图片，再按多图翻译。")
        item, raw = entries[0]
        return AssetInput(item["name"], raw)
    entries.sort(key=lambda entry: _natural_key(entry[0]["name"]))
    names = tuple(item["name"] for item, _ in entries)
    identities = {unicodedata.normalize("NFC", name).casefold() for name in names}
    if len(identities) != len(names):
        raise ArchiveError("所选图片有同名或大小写/规范化冲突，LabelPlus 无法唯一匹配；请先改名。")
    render_labelplus([{"name": name, "result": None} for name in names])
    formats = [_image_format(raw) for _, raw in entries]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for index, ((item, raw), image_format) in enumerate(zip(entries, formats), 1):
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "BMP": "bmp"}[image_format]
            # Fixed metadata/order makes retries byte-identical. The original
            # filename remains in the archive digest even with a wrong extension.
            info = zipfile.ZipInfo(f"{index:04d}/{item['name']}.{extension}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, raw)
    raw = output.getvalue()
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("图片连同内部任务目录超过 40 MiB，请略减数量后重试。")
    name = names[0] if len(names) == 1 else "漫画图片合集.zip"
    return AssetInput(name, raw, names)
