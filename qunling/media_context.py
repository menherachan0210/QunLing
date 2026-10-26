"""Small attachment provenance records; never URLs, pixels or inferred content."""
KINDS = {"Image": "image", "File": "file", "Voice": "audio", "Audio": "audio",
         "Video": "video", "Forward": "forward"}


def capture(chain, message_id, sender_id):
    records = []
    for item in chain:
        kind = getattr(item, "type", None)
        if kind in KINDS:
            records.append(dict(kind=KINDS[kind], source="message",
                                message_id=str(message_id), sender_id=str(sender_id)))
        elif kind == "Quote" and getattr(item, "id", None):
            for quoted in getattr(item, "origin", ()) or ():
                quoted_kind = getattr(quoted, "type", None)
                if quoted_kind in KINDS:
                    records.append(dict(kind=KINDS[quoted_kind], source="quote",
                        message_id=str(item.id), sender_id=str(getattr(item, "sender_id", "") or "")))
    return normalize(records)


def normalize(records):
    result = []
    for record in records[:8] if isinstance(records, (list, tuple)) else ():
        if not isinstance(record, dict) or record.get("kind") not in set(KINDS.values()) | {"unknown"}:
            continue
        if record.get("source") not in {"message", "quote"}:
            continue
        if any(not isinstance(record.get(key), str) or len(record[key]) > 160
               or any(ord(c) < 32 for c in record[key]) for key in ("message_id", "sender_id")):
            continue
        result.append({key: record[key] for key in ("kind", "source", "message_id", "sender_id")})
    return tuple(result)


def describe(item):
    records = [record for record in normalize(item.get("attachments", ()))
               if (record["source"] == "message" and record["message_id"] == item.get("message_id")
                   and record["sender_id"] == item.get("sender_id"))
               or (record["source"] == "quote" and record["message_id"] == item.get("reply_to"))]
    if not records and item.get("has_media"):
        records = [dict(kind="unknown", source="message", message_id=item["message_id"],
                        sender_id=item["sender_id"])]
    return records
