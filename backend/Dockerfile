FROM node:22-bookworm-slim
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources && npm config set registry https://registry.npmmirror.com && apt-get update && apt-get install -y --no-install-recommends python3 passwd python3-venv python3-pip git ca-certificates tini && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --no-cache-dir -r /tmp/requirements.txt && npm install -g @openai/codex@latest
RUN mkdir -p /home/codex
WORKDIR /app
COPY main.py worker.py ./
RUN mkdir -p /state /srv/codex/worktrees && chown -R 10001:10001 /app /home/codex /state /srv/codex
USER 10001:10001
ENV HOME=/home/codex CODEX_HOME=/home/codex/.codex CODEX_BIN=/usr/local/bin/codex
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
