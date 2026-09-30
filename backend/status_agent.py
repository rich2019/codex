import json
import os
import secrets
import socket
import time
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TOKEN = os.environ['STATUS_AGENT_TOKEN']
SERVICES = json.loads(os.getenv('STATUS_SERVICES', '{}'))
HOST_ROOT = os.getenv('STATUS_HOST_ROOT', '/host-root')
BIND_ADDRESS = os.getenv('STATUS_BIND_ADDRESS', '0.0.0.0')
PORT = int(os.getenv('PORT', '9107'))
internal_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cpu_counters():
    values = open('/proc/stat', encoding='ascii').readline().split()[1:]
    numbers = [int(value) for value in values]
    return sum(numbers), sum(numbers[3:])


def cpu_percent():
    first = cpu_counters()
    time.sleep(0.12)
    second = cpu_counters()
    total = second[0] - first[0]
    idle = second[1] - first[1]
    return round(max(0, min(100, 100 * (total - idle) / total)), 1) if total else 0


def memory():
    info = {}
    with open('/proc/meminfo', encoding='ascii') as handle:
        for line in handle:
            key, value = line.split(':', 1)
            if key in ('MemTotal', 'MemAvailable'):
                info[key] = int(value.split()[0]) * 1024
    total = info.get('MemTotal', 0)
    available = info.get('MemAvailable', 0)
    return {'total_bytes': total, 'available_bytes': available, 'used_percent': round(100 * (total - available) / total, 1) if total else None}


def collect():
    services = {}
    for name, url in SERVICES.items():
        try:
            if url.startswith('tcp://'):
                host_port = url.removeprefix('tcp://')
                host, port = host_port.rsplit(':', 1)
                with socket.create_connection((host, int(port)), timeout=2):
                    pass
                services[name] = {'healthy': True}
            elif url.startswith('file://'):
                stamp = Path(url.removeprefix('file://')).stat().st_mtime
                services[name] = {'healthy': time.time() - stamp < 60}
            else:
                with internal_opener.open(url, timeout=2) as response:
                    services[name] = {'healthy': 200 <= response.status < 300}
        except Exception:
            services[name] = {'healthy': False}
    load = os.getloadavg()
    with open('/proc/uptime', encoding='ascii') as handle:
        uptime = int(float(handle.read().split()[0]))
    disk = os.statvfs(HOST_ROOT)
    disk_total = disk.f_blocks * disk.f_frsize
    disk_free = disk.f_bavail * disk.f_frsize
    return {
        'cpu_percent': cpu_percent(),
        'memory': memory(),
        'disk': {'total_bytes': disk_total, 'available_bytes': disk_free, 'used_percent': round(100 * (disk_total - disk_free) / disk_total, 1) if disk_total else None},
        'load_average': [round(value, 2) for value in load],
        'uptime_seconds': uptime,
        'services': services,
        'sampled_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/healthz':
            payload = {'ok': True}
        elif self.path == '/status':
            supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
            if not secrets.compare_digest(supplied, TOKEN):
                self.send_error(401)
                return
            payload = collect()
        else:
            self.send_error(404)
            return
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        return


ThreadingHTTPServer((BIND_ADDRESS, PORT), Handler).serve_forever()
