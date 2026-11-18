#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=tests/docker/common.sh
source "$(dirname "$0")/../docker/common.sh"
require_docker
private_directories
lock_private_data
compose build prepare
detect_data_owner
task_container="${task_project}-prepare"
if [ "${1:-}" = --check ]; then
  compose run --rm --no-deps -T --name "$task_container" prepare prepare "$@"
else
  compose run --rm --no-deps --service-ports --name "$task_container" prepare prepare "$@"
fi
