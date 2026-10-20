#!/bin/sh
# Foreground Linux entry point; let systemd own process lifetime and logging.
set -eu
TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TASK_APP="$TASK_ROOT/langbot"
TASK_PYTHON="$TASK_APP/.venv/bin/python"
if [ ! -x "$TASK_PYTHON" ]; then
    echo "Create the Linux Python environment first; see deploy/linux/README.md." >&2
    exit 1
fi
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export ANONYMIZED_TELEMETRY=False
export PATH="$TASK_APP/.venv/bin:$PATH"
export QUNLING_DATA_DIR="${QUNLING_DATA_DIR:-$TASK_APP/data/qunling}"
umask 077
cd "$TASK_APP"
"$TASK_PYTHON" "$TASK_ROOT/scripts/Setup-FileRuntime.py"
exec "$TASK_PYTHON" -u "$TASK_ROOT/scripts/Run-LangBot.py"
