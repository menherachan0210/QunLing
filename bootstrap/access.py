"""Persistent HTTPS entry configuration, independent of a client-side tunnel."""
from __future__ import annotations

import argparse
from ipaddress import ip_address
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from bootstrap.state import State


class Access:
    def __init__(self, value=None):
        self.origin = ""
        self.hostname = ""
        self.proxies = set()
        if not value:
            return
        if not isinstance(value, dict) or set(value) != {"origin", "trusted_proxies"}:
            raise ValueError("公网访问配置格式无效。")
        origin = value["origin"]
        if not isinstance(origin, str):
            raise ValueError("请提供 HTTPS 管理网址。")
        parts = urlsplit(origin)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment
                or not re.fullmatch(r"[a-zA-Z0-9.\-:\[\]]+", parts.netloc)
                or parts.port is not None and not 1 <= parts.port <= 65535):
            raise ValueError("管理网址必须是独立的 HTTPS 地址，不含口令、路径或查询参数。")
        proxies = value["trusted_proxies"]
        if not isinstance(proxies, list) or len(proxies) > 8 or any(not isinstance(p, str) for p in proxies):
            raise ValueError("代理地址无效。")
        self.proxies = {str(ip_address(peer)) for peer in proxies}
        self.origin = "https://" + parts.netloc.lower()
        self.hostname = parts.hostname.lower()

    @classmethod
    def read(cls, state):
        return cls(state.read("public-access.json", {}))

    def client(self, peer, headers):
        if peer not in self.proxies:
            return peer
        try:
            return str(ip_address(headers.get("X-Real-IP", "")))
        except ValueError:
            return peer

    def valid_origin(self, host, origin):
        if self.hostname and urlsplit("//" + host).hostname == self.hostname:
            return origin == self.origin and "https://" + host.lower() == self.origin
        return origin in ("http://" + host, "https://" + host)


def main():
    parser = argparse.ArgumentParser(description="Configure the server's permanent HTTPS management entry.")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--proxy", action="append", default=[])
    options = parser.parse_args()
    access = Access({"origin": options.origin, "trusted_proxies": options.proxy})
    state = State(os.environ.get("QQBOT_STATE_DIR", str(Path(__file__).resolve().parents[1] / ".local/public-setup")))
    state.write({"origin": access.origin, "trusted_proxies": sorted(access.proxies)}, "public-access.json")
    print("HTTPS 管理入口已保存；重启管理服务后生效：" + access.origin)


if __name__ == "__main__":
    main()
