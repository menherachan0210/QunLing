"""Run fixed file operations with bounded resources and scoped artifact outputs."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from qunling.assets import AssetError, AssetStore, safe_name
from qunling.scope import Scope

OPERATIONS = {
    "inspect": ({"asset_ref"}, {"asset_ref"}),
    "read": ({"asset_ref", "selector"}, {"asset_ref"}),
    "search": ({"asset_ref", "query", "selector", "limit"}, {"asset_ref", "query"}),
    "extract": ({"asset_ref", "member", "filename"}, {"asset_ref", "member"}),
    "create": ({"format", "payload", "filename"}, {"format", "payload", "filename"}),
}


def validate_operation(operation, args):
    if operation not in OPERATIONS or not isinstance(args, dict):
        raise AssetError("invalid_operation", "未知文件操作。")
    allowed, required = OPERATIONS[operation]
    if set(args) - allowed or required - set(args):
        raise AssetError("invalid_parameters", "文件操作参数不完整或含不支持的字段。")
    try:
        raw = json.dumps(args, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise AssetError("invalid_parameters", "文件参数需要有限的 JSON 数据。") from None
    if len(raw) > 512 * 1024:
        raise AssetError("input_limit", "单次生成或处理参数不能超过 512 KiB，请分批处理。")
    if operation == "create":
        name = safe_name(args["filename"])
        if args["format"] not in {"txt", "md", "csv", "json", "docx", "xlsx", "pptx", "zip"}:
            raise AssetError("unsupported_export", "暂不支持该生成格式。")
        if not name.lower().endswith("." + args["format"]):
            raise AssetError("invalid_name", "文件扩展名应与生成格式一致。")


async def run_operation(store: AssetStore, scope: Scope, operation: str, args: dict) -> dict:
    validate_operation(operation, args)
    args = dict(args)
    with tempfile.TemporaryDirectory(prefix="qunling-file-") as temporary:
        root = Path(temporary)
        request = {"operation": operation, "args": args}
        if operation != "create":
            item = store.get(scope, args["asset_ref"])
            # Prefix prevents collision with fixed protocol filenames.
            name = "source" + Path(safe_name(item["name"])).suffix[:32]
            (root / name).write_bytes(store.read(scope, args["asset_ref"]))
            request["input_name"] = name
        elif args["format"] == "zip":
            # Public schema accepts asset refs; the worker receives only bytes.
            payload = args["payload"]
            if isinstance(payload, dict) and isinstance(payload.get("files"), list):
                entries, total = [], 0
                for entry in payload["files"]:
                    if isinstance(entry, dict) and set(entry) == {"name", "asset_ref"}:
                        content = store.read(scope, entry["asset_ref"])
                        total += len(content)
                        if total > 20 * 1024 * 1024:
                            raise AssetError("output_limit", "打包文件总量不能超过 20 MiB。")
                        entry = {"name": entry["name"], "base64": base64.b64encode(content).decode()}
                    entries.append(entry)
                args["payload"] = {**payload, "files": entries}
        (root / "request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
        }}
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).with_name("_process_worker.py")),
            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **options,
        )
        try:
            async with asyncio.timeout(65):
                await process.wait()
        except BaseException:
            if process.returncode is None:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            await process.wait()
            raise
        result_path = root / "result.json"
        if not result_path.is_file() or result_path.stat().st_size > 128 * 1024:
            raise AssetError("processing_failed", "文件处理达到资源限制，未得到完整结果。")
        try:
            response = json.loads(result_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise AssetError("processing_failed", "文件处理未返回有效结果。") from None
        if not response.get("ok"):
            raise AssetError(response.get("error_code", "processing_failed"), response.get("error", "文件处理失败。"))
        result = response["result"]
        if operation in {"create", "extract"}:
            output = root / "output"
            if output.is_symlink() or not output.is_file() or output.stat().st_size > 20 * 1024 * 1024:
                raise AssetError("output_limit", "生成文件超过传输上限或不可用。")
            filename = args.get("filename")
            if operation == "extract" and not filename:
                filename = args["member"].replace("\\", "/").rsplit("/", 1)[-1]
            item = store.put(scope, safe_name(filename), output.read_bytes(),
                             origin="generated" if operation == "create" else "archive")
            result = {**result, "artifact": item, "result_refs": [item["asset_ref"]]}
        return {"status": "ok", **result}
