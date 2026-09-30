#!/usr/bin/env python3
import argparse
import os
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(('"', "'")) and value.endswith(value[0]):
            value = value[1:-1]
        values[key] = value
    return values


def quote(value: str) -> str:
    if '\n' in value or '\r' in value:
        raise ValueError('Environment values must not contain newlines')
    if "'" not in value:
        return "'" + value + "'"
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def write_env(path: Path, values: dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ''.join(f'{key}={quote(value)}\n' for key, value in values.items())
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    if os.name == 'posix' and os.geteuid() == 0:
        os.chown(path, 0, 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=('light', 'ecs'), required=True)
    parser.add_argument('--source-env', type=Path, required=True)
    parser.add_argument('--destination', type=Path, default=Path('/etc/codex-console'))
    args = parser.parse_args()
    source = read_env(args.source_env)
    token = source.get('STATUS_AGENT_TOKEN')
    if not token:
        raise SystemExit('STATUS_AGENT_TOKEN is missing from the source environment')

    if args.role == 'ecs':
        agent_services = '{"frontend":"http://127.0.0.1:9999/"}'
        bind_address = '10.0.7.126'
    else:
        required = ('MYSQL_DATABASE', 'MYSQL_USER', 'MYSQL_PASSWORD', 'BOOTSTRAP_TOKEN')
        missing = [key for key in required if not source.get(key)]
        if missing:
            raise SystemExit('Missing application environment keys: ' + ', '.join(missing))
        agent_services = '{"api":"http://127.0.0.1:8000/api/health","mysql":"tcp://127.0.0.1:3306","mihomo":"tcp://127.0.0.1:7890","worker":"file:///var/lib/codex-console/state/worker.heartbeat"}'
        bind_address = '172.24.53.149'
        common = {
            'MYSQL_HOST': '127.0.0.1',
            'MYSQL_DATABASE': source.get('MYSQL_DATABASE', 'codex_console'),
            'MYSQL_USER': source.get('MYSQL_USER', 'codex_app'),
            'MYSQL_PASSWORD': source['MYSQL_PASSWORD'],
            'HTTP_PROXY': 'http://127.0.0.1:7890',
            'HTTPS_PROXY': 'http://127.0.0.1:7890',
            'ALL_PROXY': 'http://127.0.0.1:7890',
            'NO_PROXY': 'localhost,127.0.0.1,::1,10.0.7.126,172.24.53.149',
            'HOME': '/home/codex-console',
            'CODEX_HOME': '/home/codex-console/.codex',
            'CODEX_BIN': '/opt/codex-runtime/codex-cli/bin/codex',
            'CODEX_STATUS_FILE': '/var/lib/codex-console/state/codex_status.json',
            'CODEX_WORKSPACE': '/srv/codex/projects/rich2019-codex',
            'CODEX_WORKTREES': '/srv/codex/worktrees',
            'CODEX_UPLOADS': '/srv/codex/uploads',
            'CODEX_CRYPTO_KEY_FILE': '/home/codex-console/.codex/console-crypto-key',
        }
        api = {
            **common,
            'BOOTSTRAP_TOKEN': source['BOOTSTRAP_TOKEN'],
            'COOKIE_SECURE': 'false',
            'STATUS_AGENT_TOKEN': token,
            'STATUS_AGENTS': '{"ecs":"http://10.0.7.126:9107","light":"http://172.24.53.149:9107"}',
        }
        worker = {**common, 'MAX_TASK_SECONDS': '7200'}
        write_env(args.destination / 'api.env', api)
        write_env(args.destination / 'worker.env', worker)

    write_env(args.destination / 'status-agent.env', {
        'STATUS_AGENT_TOKEN': token,
        'STATUS_SERVICES': agent_services,
        'STATUS_BIND_ADDRESS': bind_address,
        'STATUS_HOST_ROOT': '/',
        'PORT': '9107',
    })
    os.chmod(args.destination, 0o750)
    if os.name == 'posix' and os.geteuid() == 0:
        os.chown(args.destination, 0, 0)


if __name__ == '__main__':
    main()
