"""Disposable image decoder; never inherits provider credentials or network clients."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    from qunling._process_worker import limits
    memory_mib = int(sys.argv[1]) if len(sys.argv) == 2 else 384
    if memory_mib not in (128, 256, 384) or len(sys.argv) > 2:
        raise ValueError("Invalid decoder budget")
    handle = limits(memory_bytes=memory_mib * 1024 * 1024, cpu_seconds=10,
                    file_bytes=2 * 1024 * 1024, max_processes=1)
    from qunling.vision_input import animation_data_url, MAX_BYTES

    root = Path.cwd()
    source = root / "input.txt"
    result = {"ok": False}
    try:
        if source.stat().st_size > 4 * ((MAX_BYTES + 2) // 3) + 128:
            raise ValueError("Image exceeds input budget")
        normalized, animated = animation_data_url(source.read_text(encoding="ascii"))
        if animated:
            preview = base64.b64decode(normalized.partition(",")[2], validate=True)
            if len(preview) > 2 * 1024 * 1024:
                raise ValueError("Preview exceeds output budget")
            (root / "preview.png").write_bytes(preview)
        result = {"ok": True, "animated": animated}
    except (ValueError, OSError, MemoryError):
        pass  # Neither image data nor signed references belong in diagnostics.
    (root / "result.json").write_text(json.dumps(result), encoding="ascii")
    return handle  # Keep the Windows Job handle alive through the entire decode.


if __name__ == "__main__":
    main()
