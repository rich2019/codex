#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" != 0 ]]; then
    echo "Run as root" >&2
    exit 1
fi

APP_ROOT=/opt/codex-console/release
UNIT_DIR=/etc/systemd/system

if ! getent group codex-monitor >/dev/null; then
    groupadd --system codex-monitor
fi
if ! getent passwd codex-monitor >/dev/null; then
    useradd --system --gid codex-monitor --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin codex-monitor
fi

install -d -o root -g root -m 0750 /etc/codex-console
install -d -o root -g root -m 0755 /usr/local/lib/codex-console
install -m 0644 "$APP_ROOT/backend/status_agent.py" /usr/local/lib/codex-console/status_agent.py
install -m 0644 "$APP_ROOT/deploy/systemd/codex-status-agent.service" "$UNIT_DIR/"

/usr/bin/python3 "$APP_ROOT/deploy/native/render-env-files.py" \
    --role ecs \
    --source-env /opt/codex-console/.env.ecs \
    --destination /etc/codex-console

systemctl daemon-reload
systemctl enable codex-status-agent.service
echo "ECS host status-agent is installed and enabled but not started."
