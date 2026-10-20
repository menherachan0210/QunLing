"""Paid synthetic ZIP/7z integration check through local chat; never sends to QQ."""

import asyncio
import argparse
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import zipfile

from PIL import Image, ImageDraw, ImageFont
from websockets.asyncio.client import connect

from local_api import LocalAPI, STATE


def synthetic_archive(format_name="zip", seven_zip_path=None):
    output = io.BytesIO()
    fonts = [
        os.environ.get("QQBOT_TEST_FONT", ""),
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    font_path = next((path for path in fonts if path and Path(path).is_file()), None)
    if font_path is None:
        raise RuntimeError(
            "CJK test font missing; install Noto CJK or set QQBOT_TEST_FONT."
        )
    font = ImageFont.truetype(font_path, 46)
    pages = []
    for index, texts in enumerate(
        [("おはよう！", "公園に行こう。"), ("WAIT FOR ME!", "THANK YOU.")], 1
    ):
        picture = Image.new("RGB", (1000, 1400), "white")
        draw = ImageDraw.Draw(picture)
        draw.rectangle((35, 35, 965, 1365), outline="black", width=5)
        for box, text in zip([(120, 140, 820, 400), (220, 850, 920, 1110)], texts):
            draw.ellipse(box, outline="black", width=5)
            x0, y0, x1, y1 = box
            draw.text(
                ((x0 + x1) / 2, (y0 + y1) / 2),
                text,
                fill="black",
                font=font,
                anchor="mm",
            )
        payload = io.BytesIO()
        picture.save(payload, format="PNG")
        pages.append((f"{index:03}.png", payload.getvalue()))
        picture.save(STATE / f"comic-fixture-{index}.png")
    if format_name == "7z":
        if not seven_zip_path or not Path(seven_zip_path).is_file():
            raise RuntimeError("Configure the native 7-Zip path first")
        with tempfile.TemporaryDirectory(prefix="comic-verification-") as folder:
            folder = Path(folder)
            for name, payload in pages:
                (folder / name).write_bytes(payload)
            result = subprocess.run(
                [
                    seven_zip_path,
                    "a",
                    "-t7z",
                    "-ms=on",
                    str(folder / "synthetic.7z"),
                    "002.png",
                    "001.png",
                ],
                cwd=folder,
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                raise RuntimeError("Synthetic 7z fixture creation failed")
            return (folder / "synthetic.7z").read_bytes()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # Deliberately reversed insertion order tests natural page ordering.
        for name, payload in reversed(pages):
            archive.writestr(name, payload)
    return output.getvalue()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["zip", "7z"], default="zip")
    args = parser.parse_args()
    api = LocalAPI()
    state = json.loads((STATE / "setup-state.json").read_text(encoding="utf-8"))
    available = api.call(
        "GET", "/api/v1/tools?pipeline_uuid=" + state["pipeline_uuid"]
    )["tools"]
    assert "start_comic_translation" in {tool["name"] for tool in available}, (
        "Install/configure comic plugin first"
    )
    pipeline = api.call("GET", "/api/v1/pipelines/" + state["pipeline_uuid"])[
        "pipeline"
    ]
    url = (
        "ws://127.0.0.1:5300/api/v1/pipelines/"
        + state["pipeline_uuid"]
        + "/ws/connect?session_type=person"
    )
    report = {
        "fixture": "two synthetic Japanese/English comic pages",
        "qq_messages_sent_by_test": False,
        "steps": [],
        "archive_format": args.format,
    }
    received_files = []

    def save():
        (STATE / "comic-translation-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (STATE / ("comic-translation-" + args.format + "-result.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async with connect(
        url, origin="http://127.0.0.1:5300", proxy=None, max_size=16 * 1024 * 1024
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "authenticate",
                    "token": api.http.headers["Authorization"][7:],
                    "workspace_uuid": pipeline["workspace_uuid"],
                }
            )
        )
        connected = json.loads(await socket.recv())
        assert connected.get("type") == "connected"

        async def ask(label, prompt, attachment=None):
            print("Testing " + label + "...", flush=True)
            chain = [{"type": "Plain", "text": prompt}]
            if attachment:
                chain.append(attachment)
            await socket.send(
                json.dumps({"type": "message", "stream": True, "message": chain})
            )
            started = time.monotonic()
            seen_response = False
            response = ""
            while time.monotonic() - started < 180:
                try:
                    frame = json.loads(await asyncio.wait_for(socket.recv(), 1))
                except TimeoutError:
                    if seen_response:
                        running = api.call(
                            "GET",
                            "/api/v1/monitoring/executions?botId=websocket-proxy-bot&status=running&status=queued&limit=100",
                        )["items"]
                        if not any(
                            connected["connection_id"] in str(item) for item in running
                        ):
                            break
                    continue
                if frame.get("type") == "error":
                    raise RuntimeError("Local chat returned an error")
                if frame.get("type") in ("response", "stream_chunk"):
                    seen_response = True
                    data = frame.get("data", {})
                    response = data.get("content") or response
                    for component in data.get("message_chain", []):
                        if component.get("type") == "File" and component.get("base64"):
                            raw = base64.b64decode(
                                component["base64"].split(",", 1)[-1], validate=True
                            )
                            received_files.append(raw)
                            (STATE / "comic-generated-LabelPlus.txt").write_bytes(raw)
            else:
                raise RuntimeError(label + " exceeded local check deadline")
            recent = api.call(
                "GET",
                "/api/v1/monitoring/executions?botId=websocket-proxy-bot&limit=50",
            )["items"]
            execution = next(
                (
                    item
                    for item in recent
                    if item.get("session_id")
                    == "person_websocket_" + connected["connection_id"]
                    and item.get("input_preview", "").startswith(prompt[:50])
                ),
                None,
            )
            calls = []
            if execution:
                calls = api.call(
                    "GET",
                    "/api/v1/monitoring/executions/"
                    + execution["source"]
                    + "/"
                    + execution["id"]
                    + "?section=tool_calls",
                ).get("tool_calls", [])
            results = []
            for call in calls:
                try:
                    result = json.loads(call.get("result", "{}"))
                    if isinstance(result, dict):
                        results.append(result)
                except (TypeError, ValueError):
                    pass
            report["steps"].append(
                {
                    "label": label,
                    "tools": [call["tool_name"] for call in calls],
                    "results": results,
                    "response": response,
                    "seconds": round(time.monotonic() - started, 1),
                }
            )
            save()
            return results

        raw = synthetic_archive(args.format, state.get("comic_archive_path"))
        name = "synthetic-comic-" + uuid.uuid4().hex[:8] + "." + args.format
        attachment = {
            "type": "File",
            "id": "local-test-" + uuid.uuid4().hex,
            "name": name,
            "size": len(raw),
            "base64": base64.b64encode(raw).decode("ascii"),
        }
        results = await ask(
            "start",
            "请把这个压缩包中的两页漫画都翻译成简体中文，按LabelPlus格式生成TXT文件。",
            attachment,
        )
        assert any(item.get("status") in {"queued", "running"} for item in results), (
            "Comic start did not create a job"
        )
        for attempt in range(20):
            await asyncio.sleep(10)
            results = await ask(
                "status-" + str(attempt), "查询我刚才的漫画翻译进度，只查询，不重试。"
            )
            statuses = [item.get("status") for item in results]
            if "completed" in statuses:
                break
            if any(
                status in {"failed", "partial", "interrupted", "error", "missing"}
                for status in statuses
            ):
                raise RuntimeError(
                    "Synthetic translation did not complete; inspect local report"
                )
        else:
            raise RuntimeError("Synthetic translation deadline exceeded")
        # The web debug bot has no QQ recipient. This fresh query verifies actual
        # File serialization/retrieval without attempting to impersonate a QQ chat.
        await ask("result", "请把刚才翻译完成的LabelPlus TXT作为文件发送给我。")
        assert received_files, "No TXT attachment received"
        text = received_files[-1].decode("utf-8-sig")
        assert text.startswith("1,0\r\n")
        assert text.index(">>>>>>>>[001.png]<<<<<<<<") < text.index(
            ">>>>>>>>[002.png]<<<<<<<<"
        )
        assert "公园" in text and "谢" in text and "未完成" not in text
        report["passed"] = True
        report["output_bytes"] = len(received_files[-1])
        report["output_pages"] = 2
        save()
        await ask("cleanup", "请删除本次合成漫画测试的翻译任务缓存。")
    print(
        f"Synthetic {args.format} translation and actual LabelPlus TXT attachment passed.",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
