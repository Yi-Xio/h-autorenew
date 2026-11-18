#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
用法
  run.sh renew [--force] [--account-identifier 标识符]
  run.sh check-auth [--account-identifier 标识符]

renew       按生产规则判断续期或跳过
--force     跳过本地到期判断，实测真实续期
check-auth  单独检查保存的登录凭据

默认处理所有已配置账号。--account-identifier 可重复指定。
EOF
}

case "${1-}" in
  ''|-h|--help) usage; exit 0 ;;
  renew|check-auth)
    case "${2-}" in -h|--help) usage; exit 0 ;; esac
    ;;
  *) usage >&2; exit 2 ;;
esac

# shellcheck source=tests/docker/common.sh
source "$(dirname "$0")/../docker/common.sh"
require_docker
private_directories
lock_private_data
compose build live
detect_data_owner
# 通用回归由 tests/regression/run.sh 独立运行。
task_container="${task_project}-live"
compose run --rm --no-deps -T --name "$task_container" live live "$@"
