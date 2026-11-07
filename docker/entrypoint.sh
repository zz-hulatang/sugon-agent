#!/bin/bash
set -Eeuo pipefail

pids=()
cleanup() {
    trap - EXIT TERM INT
    if ((${#pids[@]})); then
        kill "${pids[@]}" 2>/dev/null || true
        wait "${pids[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 0' TERM INT

# Docker 重启会保留可写层，移除上次异常停止留下的显示锁。
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp &
pids+=("$!")
ready=false
for ((attempt = 0; attempt < 50; attempt++)); do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 0.1
done
if [[ "$ready" != true ]]; then
    echo "虚拟显示启动失败" >&2
    exit 1
fi

fluxbox >/dev/null 2>&1 &
pids+=("$!")
# VNC 仅在容器回环地址监听，外部通过 Compose 的本机 noVNC 端口连接。
x11vnc -display "$DISPLAY" -localhost -rfbport 5900 -nopw -forever -shared >/dev/null 2>&1 &
pids+=("$!")
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &
pids+=("$!")
"$@" &
pids+=("$!")

# 任一服务退出时关闭其余进程，使容器状态反映真实运行状态。
status=0
wait -n "${pids[@]}" || status=$?
if ((status == 0)); then
    status=1
fi
exit "$status"
