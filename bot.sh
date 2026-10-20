#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$task_root"
action="${1:-up}"
case "$action" in up|down|status|logs|url) ;; *)
  echo "Usage: bash bot.sh [up|down|status|logs|url]" >&2; exit 2 ;;
esac
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Engine with Compose v2, then run again." >&2
  exit 1
fi
docker compose version >/dev/null
compose=(docker compose -f "$task_root/deploy/compose/compose.yaml")
case "$action" in
  up)
    engine="$(docker info --format '{{.OSType}}/{{.Architecture}}')"
    case "$engine" in linux/x86_64|linux/amd64) ;; *)
      echo "This release requires a Linux amd64 Docker engine; found: $engine" >&2; exit 1 ;;
    esac
    "${compose[@]}" up -d --build
    echo "Open this private setup link (use SSH forwarding on a server):"
    "${compose[@]}" run --rm --no-deps wizard access
    ;;
  down) "${compose[@]}" down ;;
  status) "${compose[@]}" ps ;;
  logs) "${compose[@]}" logs --tail=100 app wizard napcat ;;
  url) "${compose[@]}" run --rm --no-deps wizard access ;;
esac
