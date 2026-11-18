#!/usr/bin/env bash
# 供本项目的本地准备和测试入口复用，不承载续期业务。
set -euo pipefail

task_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
cd "$task_root"
export HAX_DATA_UID HAX_DATA_GID
HAX_DATA_UID=0
HAX_DATA_GID=0
task_project="hax-local-$(printf '%s' "$task_root" | cksum | cut -d ' ' -f 1)"
task_container=
task_docker_pid=
compose() {
  docker compose --env-file /dev/null -p "$task_project" -f "$task_root/tests/docker/compose.yaml" "$@" <&0 &
  task_docker_pid=$!
  task_status=0
  wait "$task_docker_pid" || task_status=$?
  task_docker_pid=
  return "$task_status"
}

cancel_task() {
  trap - INT TERM
  if [ -n "$task_container" ]; then
    docker stop --timeout 15 "$task_container" >/dev/null 2>&1 || true
  fi
  if [ -n "$task_docker_pid" ]; then
    kill -INT "$task_docker_pid" 2>/dev/null || true
    wait "$task_docker_pid" 2>/dev/null || true
  fi
  exit 130
}
trap cancel_task INT TERM

require_docker() {
  command -v docker >/dev/null || { echo '需要安装并启动 Docker。' >&2; exit 1; }
  docker info >/dev/null 2>&1 || { echo '无法连接 Docker，请先启动 Docker。' >&2; exit 1; }
  docker compose version >/dev/null
}

private_directories() {
  # 校验实际仓库与索引，避免终端继承的 Git 位置覆盖这些检查。
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
  git check-ignore --quiet -- .local/secrets/accounts.json || {
    echo '.local 尚未被 Git 忽略，已停止。' >&2; exit 1;
  }
  if [ -n "$(git ls-files -- .local)" ]; then
    echo '.local 中存在已跟踪文件，已停止。' >&2; exit 1
  fi
  if ! grep -qxE '/?\.local/' .dockerignore || grep -qE '^[[:space:]]*!' .dockerignore; then
    echo 'Docker 排除规则未通过检查，已停止。' >&2; exit 1
  fi
  umask 077
  for task_directory in .local .local/secrets .local/browser .local/state .local/results; do
    if [ -L "$task_directory" ]; then
      echo '私有目录不能使用符号链接。' >&2; exit 1
    fi
    [ -d "$task_directory" ] || mkdir -m 700 "$task_directory"
    # ls 的权限字段在 macOS / Linux 上一致，不读取目录内容。
    task_mode=$(LC_ALL=C ls -ld "$task_directory")
    case "$task_mode" in
      drwx------*) ;;
      *) echo '私有目录权限必须为 0700，请核对后调整。' >&2; exit 1 ;;
    esac
  done
}

lock_private_data() {
  if ! mkdir .local/state/operation.lock 2>/dev/null; then
    echo '已有准备或实测任务；确认任务结束后再移除 .local/state/operation.lock。' >&2
    exit 1
  fi
  trap 'rmdir "$task_root/.local/state/operation.lock" 2>/dev/null || true' EXIT
}

detect_data_owner() {
  task_owner=$(docker run --rm --network none --platform linux/amd64 \
    --entrypoint stat \
    --mount "type=bind,source=$task_root/.local,target=/mnt/hax-data,readonly" \
    "${task_project}-runtime:authentication-v1" -c '%u:%g' /mnt/hax-data)
  if [[ ! "$task_owner" =~ ^[0-9]+:[0-9]+$ ]]; then
    echo '无法核实容器挂载后的私有目录所有者。' >&2; exit 1
  fi
  IFS=: read -r HAX_DATA_UID HAX_DATA_GID <<< "$task_owner"
  export HAX_DATA_UID HAX_DATA_GID
}
