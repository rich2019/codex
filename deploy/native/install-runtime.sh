#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" != 0 ]]; then
    echo "Run as root" >&2
    exit 1
fi

PYTHON_VERSION=3.11.16
PYTHON_SHA256=91bcdebfdde239a003ae93738a7fce0f9230fee5c4bc2b86f6e6e8c6f98aabe8
NODE_VERSION=22.23.3
NODE_SHA256=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de
CODEX_VERSION=0.159.2
RUNTIME_ROOT=/opt/codex-runtime
PYTHON_PREFIX="$RUNTIME_ROOT/python-$PYTHON_VERSION"
NODE_PREFIX="$RUNTIME_ROOT/node-v$NODE_VERSION"
CODEX_PREFIX="$RUNTIME_ROOT/codex-cli"
APP_ROOT=/opt/codex-backend
HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:7890}"
HTTP_PROXY="${HTTP_PROXY:-$HTTPS_PROXY}"
export HTTPS_PROXY HTTP_PROXY https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY"

temp_dir="$(mktemp -d /var/tmp/codex-runtime.XXXXXX)"
trap 'rm -rf "$temp_dir"' EXIT
install -d -m 0755 "$RUNTIME_ROOT"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git xz-utils pkg-config \
    libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
    libffi-dev liblzma-dev libncursesw5-dev libgdbm-dev uuid-dev

if [[ ! -x "$PYTHON_PREFIX/bin/python3.11" ]] || [[ "$("$PYTHON_PREFIX/bin/python3.11" --version 2>&1)" != "Python $PYTHON_VERSION" ]]; then
    python_archive="$temp_dir/Python-$PYTHON_VERSION.tar.xz"
    curl --fail --silent --show-error --location --proxy "$HTTPS_PROXY" \
        "https://www.python.org/ftp/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tar.xz" \
        --output "$python_archive"
    echo "$PYTHON_SHA256  $python_archive" | sha256sum --check --status
    tar -xJf "$python_archive" -C "$temp_dir"
    (
        cd "$temp_dir/Python-$PYTHON_VERSION"
        ./configure --prefix="$PYTHON_PREFIX" --with-ensurepip=install
        make -j2
        make altinstall
    )
fi

if [[ ! -x "$NODE_PREFIX/bin/node" ]] || [[ "$("$NODE_PREFIX/bin/node" --version)" != "v$NODE_VERSION" ]]; then
    node_archive="$temp_dir/node-v$NODE_VERSION-linux-x64.tar.xz"
    curl --fail --silent --show-error --location --proxy "$HTTPS_PROXY" \
        "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" \
        --output "$node_archive"
    echo "$NODE_SHA256  $node_archive" | sha256sum --check --status
    install -d -m 0755 "$NODE_PREFIX"
    tar -xJf "$node_archive" --strip-components=1 -C "$NODE_PREFIX"
fi

install -d -m 0755 "$CODEX_PREFIX"
export PATH="$NODE_PREFIX/bin:$PATH"
export npm_config_registry=https://registry.npmjs.org
export npm_config_proxy="$HTTP_PROXY"
export npm_config_https_proxy="$HTTPS_PROXY"
npm install --global --prefix "$CODEX_PREFIX" "@openai/codex@$CODEX_VERSION"

install -d -m 0755 "$APP_ROOT"
if [[ ! -x "$APP_ROOT/venv/bin/python" ]]; then
    "$PYTHON_PREFIX/bin/python3.11" -m venv "$APP_ROOT/venv"
fi
export PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
export PIP_TRUSTED_HOST=mirrors.aliyun.com
"$APP_ROOT/venv/bin/python" -m pip install --no-cache-dir -r "$(dirname "$0")/../../backend/requirements.host.lock"
"$APP_ROOT/venv/bin/python" -m pip check

chown -R root:root "$RUNTIME_ROOT" "$APP_ROOT/venv"
chmod -R a+rX "$RUNTIME_ROOT" "$APP_ROOT/venv"
"$NODE_PREFIX/bin/node" --version
"$CODEX_PREFIX/bin/codex" --version
"$APP_ROOT/venv/bin/python" --version
