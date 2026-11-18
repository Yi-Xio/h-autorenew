FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive UV_PYTHON_INSTALL_DIR=/opt/python
COPY --from=uv /uv /usr/local/bin/uv
RUN rm -f /etc/apt/apt.conf.d/docker-clean && \
    printf '%s\n' 'Acquire::Retries "3";' 'Acquire::http::Timeout "30";' \
      'Acquire::https::Timeout "30";' > /etc/apt/apt.conf.d/80hax-network
RUN --mount=type=cache,id=hax-noble-apt,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=hax-noble-lists,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates
RUN sed -i 's|http://archive.ubuntu.com|https://archive.ubuntu.com|g; s|http://security.ubuntu.com|https://security.ubuntu.com|g' \
      /etc/apt/sources.list.d/ubuntu.sources
RUN --mount=type=cache,id=hax-noble-apt,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=hax-noble-lists,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg git sudo procps iproute2 iptables dbus \
    xvfb xauth openbox x11vnc novnc websockify ffmpeg unzip fonts-noto-cjk \
    libnss3 libatk-bridge2.0-0 libgbm1 libasound2t64
RUN curl -fsSL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 60 \
      https://dl.google.com/linux/linux_signing_key.pub | \
      gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list && \
    curl -fsSL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 60 \
      https://pkg.cloudflareclient.com/pubkey.gpg | \
      gpg --dearmor -o /usr/share/keyrings/cloudflare-warp.gpg && \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp.gpg] https://pkg.cloudflareclient.com/ noble main' > /etc/apt/sources.list.d/cloudflare-client.list
ARG CHROME_VERSION=157.0.8103.78-1
ARG WARP_VERSION=2026.8.2115.0
RUN --mount=type=cache,id=hax-noble-apt,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=hax-noble-lists,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    task_sha256=$(apt-cache show "google-chrome-stable=${CHROME_VERSION}" | awk '/^SHA256:/ {print $2; exit}') && \
    test -n "$task_sha256" && \
    task_chrome="/var/cache/apt/archives/google-chrome-stable_${CHROME_VERSION}_amd64.deb" && \
    if ! echo "$task_sha256  $task_chrome" | sha256sum -c - 2>/dev/null; then \
      curl -fsSL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 600 \
        "https://dl-ssl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}_amd64.deb" \
        -o "$task_chrome.partial" && \
      echo "$task_sha256  $task_chrome.partial" | sha256sum -c - && \
      mv "$task_chrome.partial" "$task_chrome" || exit 1; \
    fi && \
    test "$(dpkg-deb --field "$task_chrome" Version)" = "$CHROME_VERSION" && \
    apt-get install -y --no-install-recommends google-chrome-stable=${CHROME_VERSION} cloudflare-warp=${WARP_VERSION}
RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install 3.13.18 && uv venv --python 3.13.18 /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp/hax-home
COPY requirements.txt /opt/requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv pip install --python /opt/venv/bin/python -r /opt/requirements.txt
WORKDIR /opt/hax
COPY . /opt/hax
RUN test ! -e /opt/hax/.local && test ! -e /opt/hax/.git
ENTRYPOINT ["bash", "/opt/hax/tests/docker/entrypoint.sh"]
