#!/usr/bin/env bash
set -euo pipefail
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
