"""Capture only Host-provided attachment metadata at the incoming message boundary."""
from __future__ import annotations

import base64
import binascii
import hashlib
import time
from types import SimpleNamespace

from qunling.assets import AssetError, AssetStore, MAX_ASSET_BYTES, image_name
from qunling.inbox import Inbox
from qunling.integration import descriptor, source_scope


def _remember(store, inbox, scope, component, number):
    kind = getattr(component, "type", "")
    inline = getattr(component, "base64", "") or ""
    data = {
        "name": getattr(component, "name", "") if kind == "File" else f"image-{number}.jpg",
        "size": getattr(component, "size", 0),
        "file_id": getattr(component, "id", "") if kind == "File" else "",
        "url": getattr(component, "url", "") or "",
    }
    if kind == "Image":
        identity = data["url"] or getattr(component, "image_id", "") or str(number)
        data["name"] = "qq-image-" + hashlib.sha256(identity.encode()).hexdigest()[:16] + ".image"
        data["media_type"] = "image"
    if inline:
        if not isinstance(inline, str) or len(inline) > ((MAX_ASSET_BYTES + 2) // 3) * 4 + 100:
            raise AssetError("size_limit", "内嵌附件过大。")
        if inline.startswith("data:"):
            inline = inline.split(";base64,", 1)[-1]
        try:
            raw = base64.b64decode(inline, validate=True)
        except (binascii.Error, ValueError):
            raise AssetError("invalid_attachment", "内嵌附件编码无效。") from None
        if kind == "Image":
            data["name"] = image_name(raw)
        item = store.put(scope, data["name"], raw)
        data.update(asset_ref=item["asset_ref"], size=item["size"])
    return inbox.add(scope, data)


def capture_message(plugin, source, query_uuid, *, captured_at=None):
    captured_at = time.time() if captured_at is None else captured_at
    store = AssetStore.from_env()
    inbox = Inbox(store)
    scope = source_scope(plugin, source)
    result = descriptor(plugin, source, query_uuid, captured_at=captured_at)
    direct, quoted = [], []
    unresolved = has_quote = False
    for component in getattr(source, "message_chain", ()) or ():
        kind = getattr(component, "type", "")
        if kind in {"File", "Image"}:
            direct.append(component)
        elif kind == "Quote":
            has_quote = True
            chat_kind = getattr(source.launcher_type, "value", source.launcher_type)
            group = getattr(component, "group_id", None)
            target = getattr(component, "target_id", None)
            cross_chat = group not in (None, "", 0, "0") and (
                chat_kind != "group" or str(group) != str(source.launcher_id)
            )
            if chat_kind == "person" and target not in (None, "", 0, "0"):
                cross_chat |= str(target) != str(source.launcher_id)
            origin = getattr(component, "origin", None)
            if not origin or cross_chat:
                unresolved = True
            else:
                quoted.extend(c for c in origin if getattr(c, "type", "") in {"File", "Image"})
    refs, warnings = [], []
    selected = direct or quoted
    for number, component in enumerate(selected[:20], 1):
        try:
            ref = _remember(store, inbox, scope, component, number)
            refs.append(ref)
            asset_ref = inbox.get(scope, ref).get("asset_ref")
            if asset_ref:
                store.grant(scope, result["request_key"], asset_ref)
        except AssetError as error:
            warnings.append(error.code)
    if len(selected) > 20:
        warnings.append("attachment_count_limit")
    result.update(inbox_refs=refs, unresolved_quote=(unresolved or (has_quote and not quoted)) and not direct)
    result["warnings"] = sorted(set(warnings))
    # Explicit refs are the preferred selection. Older inbox rows remain selectable,
    # bounded by this arrival timestamp; later group chatter cannot replace them.
    return result


def capture_group_upload(plugin, bot_uuid, group_id, sender_id, file, **execution_scope):
    if not isinstance(file, dict):
        return
    source = SimpleNamespace(bot_uuid=bot_uuid, launcher_type="group",
                             launcher_id=group_id, sender_id=sender_id, **execution_scope)
    component = SimpleNamespace(type="File", name=file.get("name", ""), size=file.get("size", 0),
                                id=file.get("id", ""), url=file.get("url", ""), base64="")
    store = AssetStore.from_env()
    _remember(store, Inbox(store), source_scope(plugin, source), component, 1)
