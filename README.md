# Codex Console

Self-hosted web console for running Codex tasks against a configured Git workspace.

## Deployment

1. Copy `.env.example` to `.env` and set unique secrets.
2. Keep the Git workspace outside this repository, at `/srv/codex/repo`.
3. Start with `docker compose -f compose.yml up -d --build`.
4. Open the HTTP console and use `BOOTSTRAP_TOKEN` to create the first administrator.
5. The administrator can start ChatGPT Plus device login from the console; no API key is required.

The checked-in HTTP configuration is intended for a private/single-user deployment. When a domain and HTTPS are added, replace it with the TLS configuration and set `COOKIE_SECURE=true`.

## Data and secrets

Never commit `.env`, `state/`, Codex authorization data, MySQL volumes, or real source repositories. The compose file mounts those resources separately at runtime.
