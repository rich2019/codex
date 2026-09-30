#!/usr/bin/env bash
set -euo pipefail

timeout_seconds="${1:-120}"
for port in 3306 7890; do
    ready=0
    for _ in $(seq 1 "$timeout_seconds"); do
        if timeout 2 bash -c "</dev/tcp/127.0.0.1/$port" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 1
    done
    if [[ "$ready" != 1 ]]; then
        echo "Timed out waiting for 127.0.0.1:$port" >&2
        exit 1
    fi
done
