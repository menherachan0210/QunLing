"""Small, idempotent container entry points; never import a local deployment."""

from __future__ import annotations

import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in {"init", "app", "wizard", "access"}:
        raise SystemExit("Usage: entrypoint.py init|app|wizard|access")
    os.umask(0o077)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    role = argv[0]
    if role == "init":
        from bootstrap.state import prepare

        prepare(
            Path(os.environ.get("QQBOT_STATE_DIR", "/var/lib/qqbot-setup")),
            Path(os.environ.get("QQBOT_NAPCAT_CONFIG_DIR", "/var/lib/napcat-config")),
        )
        print(
            "Deployment state prepared; existing credentials and QQ config preserved."
        )
        return
    if role == "access":
        os.execv(
            sys.executable,
            [sys.executable, "-u", "-m", "bootstrap.server", "--url"],
        )
    if role == "app":
        os.chdir(ROOT / "langbot")
        # Both Host and its automatically managed stdio Runtime use this cwd.
        # Do not pass --standalone-runtime or publish Runtime transport ports.
        os.environ.setdefault("QUNLING_DATA_DIR", str(ROOT / "langbot/data/qunling"))
        os.execv(
            sys.executable,
            [sys.executable, "-u", str(ROOT / "scripts/Run-LangBot.py")],
        )
    os.execv(sys.executable, [sys.executable, "-u", "-m", "bootstrap.server"])


if __name__ == "__main__":
    main()
