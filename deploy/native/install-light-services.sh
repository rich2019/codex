#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" != 0 ]]; then
    echo "Run as root" >&2
    exit 1
fi

APP_ROOT=/opt/codex-backend
CODEX_HOME=/home/codex-console/.codex
AUTH_VOLUME=codex-backend_codex_auth
UNIT_DIR=/etc/systemd/system

if ! getent group 10001 >/dev/null; then
    groupadd --gid 10001 codex-console
elif [[ "$(getent group 10001 | cut -d: -f1)" != codex-console ]]; then
    echo "GID 10001 is already assigned to another group" >&2
    exit 1
fi
if ! getent passwd 10001 >/dev/null; then
    useradd --uid 10001 --gid 10001 --home-dir /home/codex-console \
        --create-home --shell /usr/sbin/nologin codex-console
elif [[ "$(getent passwd 10001 | cut -d: -f1)" != codex-console ]]; then
    echo "UID 10001 is already assigned to another user" >&2
    exit 1
fi

if ! getent group codex-monitor >/dev/null; then
    groupadd --system codex-monitor
fi
if ! getent passwd codex-monitor >/dev/null; then
    useradd --system --gid codex-monitor --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin codex-monitor
fi

install -d -o root -g root -m 0750 /etc/codex-console
install -d -o codex-console -g codex-console -m 0700 "$CODEX_HOME"
install -d -o codex-console -g codex-console -m 0755 /var/lib/codex-console/state
install -d -o codex-console -g codex-console -m 0750 /srv/codex/uploads
install -d -o root -g root -m 0755 /usr/local/lib/codex-console

auth_mount="$(docker volume inspect --format '{{.Mountpoint}}' "$AUTH_VOLUME")"
if [[ ! -f "$CODEX_HOME/console-crypto-key" ]]; then
    if [[ ! -d "$auth_mount" ]]; then
        echo "Codex authorization volume was not found: $AUTH_VOLUME" >&2
        exit 1
    fi
    tar -C "$auth_mount" -cpf - . | tar -C "$CODEX_HOME" -xpf -
fi
chown -R codex-console:codex-console "$CODEX_HOME"
chmod 0700 "$CODEX_HOME"
if [[ -f "$CODEX_HOME/console-crypto-key" ]]; then
    chmod 0600 "$CODEX_HOME/console-crypto-key"
fi

if [[ -d "$APP_ROOT/state" ]]; then
    cp -an "$APP_ROOT/state"/. /var/lib/codex-console/state/
fi
chown -R codex-console:codex-console /var/lib/codex-console/state
chmod 0755 /var/lib/codex-console/state
if [[ -f /var/lib/codex-console/state/worker.heartbeat ]]; then
    chmod 0644 /var/lib/codex-console/state/worker.heartbeat
fi

install -m 0755 "$APP_ROOT/deploy/native/wait-for-infra.sh" /usr/local/sbin/codex-console-wait-infra
install -m 0755 "$APP_ROOT/deploy/native/light-peer-acl.sh" /usr/local/sbin/codex-console-peer-acl
install -m 0644 "$APP_ROOT/deploy/systemd/codex-console-peer-acl.service" "$UNIT_DIR/"
install -m 0644 "$APP_ROOT/deploy/systemd/codex-console-api.service" "$UNIT_DIR/"
install -m 0644 "$APP_ROOT/deploy/systemd/codex-console-worker.service" "$UNIT_DIR/"
install -m 0644 "$APP_ROOT/deploy/systemd/codex-status-agent.service" "$UNIT_DIR/"
install -m 0644 "$APP_ROOT/backend/status_agent.py" /usr/local/lib/codex-console/status_agent.py
chmod 0644 /usr/local/lib/codex-console/status_agent.py

"$APP_ROOT/venv/bin/python" "$APP_ROOT/deploy/native/render-env-files.py" \
    --role light --source-env "$APP_ROOT/.env" --destination /etc/codex-console

systemctl daemon-reload
systemctl enable codex-console-peer-acl.service codex-console-api.service \
    codex-console-worker.service codex-status-agent.service
systemctl restart codex-console-peer-acl.service
echo "Light host users, Codex home, environment files, ACL, and systemd units are prepared."
echo "Stop the Docker API/worker/status-agent before starting the host-native services."
