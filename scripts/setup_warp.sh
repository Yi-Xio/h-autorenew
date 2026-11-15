#!/usr/bin/env bash
# 仅安装当前续期流程使用的官方 WARP client，不输出出口 IP。
# 安装方式参考 https://pkg.cloudflareclient.com/ 与原 WARP Action 的 client 模式。
set -euo pipefail

install_warp() {
  sudo apt-get -y update || return 1
  curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg |
    sudo gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg || return 1
  distro=$(lsb_release -cs) || return 1
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $distro main" |
    sudo tee /etc/apt/sources.list.d/cloudflare-client.list || return 1
  sudo apt-get update || return 1
  sudo apt-get install -y cloudflare-warp || return 1
  sudo warp-cli --accept-tos registration new || return 1
  sudo warp-cli --accept-tos mode warp+doh || return 1
  sudo warp-cli --accept-tos tunnel ip add-range ::0/0 || return 1
  sudo warp-cli --accept-tos connect || return 1
  for _attempt in {1..12}; do
    if curl -4 -fsS --max-time 5 https://www.cloudflare.com/cdn-cgi/trace | grep -qE '^warp=(on|plus)$'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if (install_warp) >/dev/null 2>&1; then
  echo 'stage=warp result=connected'
else
  echo 'stage=warp result=failed category=warp_setup_failed'
  exit 1
fi
