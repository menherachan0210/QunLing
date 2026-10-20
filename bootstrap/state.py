"""Private, atomic deployment state. Never ship this directory in a release."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import threading


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + secrets.token_hex(5) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(state_dir, napcat_config_dir):
    state_dir, napcat_config_dir = Path(state_dir), Path(napcat_config_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    napcat_config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    webui_path = napcat_config_dir / "webui.json"
    existing_webui = read_json(webui_path, {})
    path = state_dir / "secrets.json"
    values = read_json(path)
    if values is None:
        values = {
            "setup_token": secrets.token_urlsafe(32),
            "napcat_token": existing_webui.get("token") or secrets.token_urlsafe(32),
            "onebot_token": secrets.token_urlsafe(32),
        }
        write_json(path, values)
    elif (
        existing_webui.get("token")
        and values["napcat_token"] != existing_webui["token"]
    ):
        # An administrator may rotate NapCat's token without recreating volumes.
        values["napcat_token"] = existing_webui["token"]
        write_json(path, values)
    if not existing_webui:
        write_json(
            webui_path,
            {
                "host": "0.0.0.0",
                "port": 6099,
                "token": values["napcat_token"],
                "loginRate": 10,
                "disableWebUI": False,
            },
        )
    elif not existing_webui.get("token"):
        write_json(webui_path, {**existing_webui, "token": values["napcat_token"]})
    if not (napcat_config_dir / "onebot11.json").exists():
        write_json(
            napcat_config_dir / "onebot11.json",
            {
                "network": {
                    "httpServers": [],
                    "httpClients": [],
                    "websocketServers": [],
                    "websocketClients": [],
                },
                "musicSignUrl": "",
                "enableLocalFile2Url": False,
                "parseMultMsg": False,
            },
        )
    return values


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()

    def read(self, name="setup.json", default=None):
        with self.lock:
            return read_json(self.directory / name, default)

    def write(self, value, name="setup.json"):
        with self.lock:
            write_json(self.directory / name, value)
