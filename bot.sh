#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$task_root"
action="${1:-up}"
case "$action" in up|rebuild|down|status|logs|url) ;; *)
  echo "Usage: bash bot.sh [up|rebuild|down|status|logs|url]" >&2; exit 2 ;;
esac
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Engine with Compose v2, then run again." >&2
  exit 1
fi
docker compose version >/dev/null
compose=(docker compose -f "$task_root/deploy/compose/compose.yaml")
project="${COMPOSE_PROJECT_NAME:-qq-ai-bot}"
has_updates() { docker volume inspect "${project}_update-data" >/dev/null 2>&1; }
guard_update() {
  if has_updates; then "${compose[@]}" run --rm --no-deps updater guard; fi
}
case "$action" in
  up|rebuild)
    engine="$(docker info --format '{{.OSType}}/{{.Architecture}}')"
    case "$engine" in linux/x86_64|linux/amd64) ;; *)
      echo "This release requires a Linux amd64 Docker engine; found: $engine" >&2; exit 1 ;;
    esac
    guard_update
    resumed=false
    if [[ "$action" == up ]] && has_updates; then
      if "${compose[@]}" run --rm --no-deps updater resume; then
        resumed=true
      else
        result=$?
        [[ "$result" == 3 ]] || exit "$result"
      fi
    fi
    if [[ "$resumed" == false ]]; then
      QQBOT_REVISION="$(git -C "$task_root" rev-parse HEAD 2>/dev/null || echo unknown)"
      if [[ -n "$(git -C "$task_root" status --porcelain 2>/dev/null || true)" ]]; then
        QQBOT_REVISION=unknown
      fi
      export QQBOT_REVISION
      "${compose[@]}" up -d --build
      "${compose[@]}" run --rm --no-deps updater adopt
    fi
    echo "Open this private setup link (use SSH forwarding on a server):"
    "${compose[@]}" run --rm --no-deps wizard access
    ;;
  down) guard_update; "${compose[@]}" down ;;
  status) "${compose[@]}" ps ;;
  logs) "${compose[@]}" logs --tail=100 app wizard napcat ;;
  url) "${compose[@]}" run --rm --no-deps wizard access ;;
esac
