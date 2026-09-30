# Light host-native services

The Light host runs the API, worker/Codex CLI, and read-only status agent as systemd services. MySQL and Mihomo remain in Docker and publish only to `127.0.0.1`; the ECS frontend remains in Docker. API and worker share dedicated non-root UID/GID `10001`; the API has read-only project/worktree access while the worker can write task worktrees.

## Install

1. Back up `/opt/codex-backend/.env`, the MySQL volume, `codex-backend_codex_auth`, Mihomo state, and `/srv/codex`; confirm there are no active tasks and stop the Docker API, worker, and status-agent.
2. Update `compose.light.yml` and recreate only MySQL/Mihomo so their host ports bind to loopback. Keep their existing data volumes.
3. Run `deploy/native/install-runtime.sh` as root. It verifies Python 3.11.16 and Node 22.23.3 archives, creates the isolated venv, and installs Codex CLI 0.159.2.
4. Run `deploy/native/install-light-services.sh` as root. It copies (does not remove) the existing Codex home, renders root-only environment files, installs and applies the peer ACL, and enables the service units without starting API/worker/monitor.
5. Start the systemd status-agent, API, and worker units; validate API, login, model catalog, and worker startup. ECS Nginx continues to forward to the same Light private address.

For ECS, install the Python stdlib status agent with `deploy/native/install-ecs-monitor.sh`. It binds only to `10.0.7.126:9107` and reads the root-only token from `/opt/codex-console/.env.ecs`.

## Access boundaries

The API and worker run as UID/GID `10001` (`codex-console`). Systemd makes the project and worktrees read-only to the API and writable only to the worker; uploads and the Codex home are the only API-writable data locations. The worker keeps Codex's `workspace-write` sandbox. The monitor runs as `codex-monitor`, has no Docker socket or sudo access, and returns fixed metrics/service health only.

MySQL is reachable by host services at `127.0.0.1:3306`; Mihomo is reachable at `127.0.0.1:7890`. The API listens on port `8000`; the persistent host ACL allows inbound traffic only from ECS `10.0.7.126`. Status agents bind to their private IPs on port `9107`.

## Rollback

Stop the native systemd services, copy any updated `.codex` contents back into the preserved Docker auth volume, restore the saved full Light Compose file, and start the Docker API/worker/status-agent. MySQL and worktrees use the same persistent data paths; do not restore an older database over newer writes.
