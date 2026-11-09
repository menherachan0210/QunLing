"""Disposable image decoder; never inherits provider credentials or network clients."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    from qunling._process_worker import limits
    memory_mib = int(sys.argv[1]) if len(sys.argv) >= 2 else 384
    for_edit = len(sys.argv) == 3 and sys.argv[2] == "edit"
    inspect_gif = len(sys.argv) == 3 and sys.argv[2] == "gif"
    if memory_mib not in (128, 256, 384) or len(sys.argv) > 3 or (len(sys.argv) == 3 and not (for_edit or inspect_gif)):
        raise ValueError("Invalid decoder budget")
    handle = limits(memory_bytes=memory_mib * 1024 * 1024, cpu_seconds=10,
                    file_bytes=(10 if for_edit else 2) * 1024 * 1024, max_processes=1)
    from qunling.vision_input import animation_data_url, MAX_BYTES, MAX_PREVIEW_BYTES

    root = Path.cwd()
    source = root / "input.txt"
    result = {"ok": False}
    try:
        if source.stat().st_size > 4 * ((MAX_BYTES + 2) // 3) + 128:
            raise ValueError("Image exceeds input budget")
        value = source.read_text(encoding="ascii")
        if inspect_gif:
            from qunling.native_gif import inspect_gif as inspect
            result = {"ok": True, "metadata": inspect(value)}
            (root / "result.json").write_text(json.dumps(result), encoding="ascii")
            return handle
        normalized, animated = animation_data_url(value, for_edit=for_edit)
        converted = normalized != value
        if converted:
            preview = base64.b64decode(normalized.partition(",")[2], validate=True)
            limit = MAX_BYTES if for_edit and not animated else MAX_PREVIEW_BYTES
            if len(preview) > limit:
                raise ValueError("Preview exceeds output budget")
            (root / "preview.png").write_bytes(preview)
        result = {"ok": True, "animated": animated, "converted": converted}
    except (ValueError, OSError, MemoryError):
        pass  # Neither image data nor signed references belong in diagnostics.
    (root / "result.json").write_text(json.dumps(result), encoding="ascii")
    return handle  # Keep the Windows Job handle alive through the entire decode.


if __name__ == "__main__":
    main()
