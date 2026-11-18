#!/usr/bin/env bash
set -euo pipefail
umask 077
mkdir -p "$HOME"
task_mode=${1:-}
shift || true
task_child=
cleanup() {
  trap - EXIT INT TERM
  if [ -n "$task_child" ]; then
    kill -TERM "$task_child" 2>/dev/null || true
    wait "$task_child" 2>/dev/null || true
  fi
  if [ "$(id -u)" = 0 ]; then
    warp-cli --accept-tos disconnect >/dev/null 2>&1 || true
    for task_path in /mnt/hax-state /mnt/hax-results; do
      if [ -d "$task_path" ]; then
        find "$task_path" -xdev -type d -exec chmod 700 {} +
        find "$task_path" -xdev -type f -exec chmod 600 {} +
        chown -R "${HAX_DATA_UID}:${HAX_DATA_GID}" "$task_path"
      fi
    done
  fi
  rm -rf /run/hax-private /tmp/DrissionPage "$HOME/.local/share/DrissionPage"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

case "$task_mode" in
  regression)
    export HAX_BROWSER_TESTS=1
    python -m tests.regression.run &
    ;;
  prepare)
    if [ "${1:-}" != --check ]; then
      export DISPLAY=:99
      Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp >/dev/null 2>&1 &
      python - <<'PY'
import socket
import time
for attempt in range(100):
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect('/tmp/.X11-unix/X99')
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit('容器桌面启动失败。')
PY
      openbox >/dev/null 2>&1 &
      x11vnc -display :99 -localhost -rfbport 5900 -nopw -forever -shared >/dev/null 2>&1 &
      websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
      echo "浏览器桌面：http://127.0.0.1:${HAX_DESKTOP_PORT:-6080}/vnc.html?autoconnect=true&resize=scale"
    fi
    python -m tests.live.prepare "$@" <&0 &
    ;;
  live)
    mkdir -p /run/dbus
    dbus-daemon --system --fork >/dev/null 2>&1
    warp-svc >/dev/null 2>&1 &
    python -m tests.live.run "$@" &
    ;;
  *) echo '未知容器入口。' >&2; exit 2 ;;
esac
task_child=$!
wait "$task_child"
task_child=
