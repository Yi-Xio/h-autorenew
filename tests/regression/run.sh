#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=tests/docker/common.sh
source "$(dirname "$0")/../docker/common.sh"
require_docker
compose build regression
task_container="${task_project}-regression"
compose run --rm --no-deps -T --name "$task_container" regression
