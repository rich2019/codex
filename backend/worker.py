import json
import os
import selectors
import signal
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, func
from main import SessionLocal, Task, TaskEvent, Upload, TaskAttachment, WORKSPACE, WORKTREES, UPLOAD_ROOT, CODEX_STATUS_FILE, Base, engine, ensure_task_settings_columns

CODEX_BIN = os.getenv('CODEX_BIN', '/usr/local/bin/codex')
MAX_RUNTIME = int(os.getenv('MAX_TASK_SECONDS', '7200'))
POLL = float(os.getenv('WORKER_POLL_SECONDS', '2'))


def write_status():
    status = {'authenticated': False, 'message': 'Codex CLI 未登录'}
    try:
        p = subprocess.run([CODEX_BIN, 'login', 'status'], capture_output=True, text=True, timeout=15, env=os.environ)
        msg = (p.stdout + p.stderr).strip()[:500]
        status = {'authenticated': p.returncode == 0, 'message': msg if p.returncode == 0 else '请在服务器完成 Codex ChatGPT 登录', 'checked_at': datetime.utcnow().isoformat() + 'Z'}
    except Exception as exc:
        status['message'] = f'Codex CLI 检查失败: {exc}'
    if not (WORKSPACE / '.git').exists():
        status['workspace_git'] = False
        status['message'] = 'Codex 已可登录，但尚未配置 Git 工作区' if status.get('authenticated') else status['message']
    else:
        status['workspace_git'] = True
    CODEX_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CODEX_STATUS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(status, ensure_ascii=False))
    os.replace(tmp, CODEX_STATUS_FILE)
    heartbeat = CODEX_STATUS_FILE.parent / 'worker.heartbeat'
    heartbeat.touch()
    os.chmod(heartbeat, 0o644)
    return status


def add_event(task_id, event_type, payload):
    with SessionLocal() as db:
        seq = (db.scalar(select(func.max(TaskEvent.seq)).where(TaskEvent.task_id == task_id)) or 0) + 1
        db.add(TaskEvent(task_id=task_id, seq=seq, event_type=event_type, payload=json.dumps({'type': event_type, 'data': payload}, ensure_ascii=False, default=str)))
        db.commit()


def get_task(task_id):
    with SessionLocal() as db:
        return db.get(Task, task_id)


def update_task(task_id, **values):
    with SessionLocal() as db:
        t = db.get(Task, task_id)
        if t:
            for k, v in values.items():
                setattr(t, k, v)
            db.commit()


def prepare_worktree(task):
    if task.worktree_path:
        path = Path(task.worktree_path)
        if path.exists() and (path / '.git').exists():
            materialize_attachments(task.id, path)
            return path
    base = WORKSPACE.resolve()
    if not (base / '.git').exists():
        raise RuntimeError(f'配置的工作区不是 Git 仓库: {base}')
    WORKTREES.mkdir(parents=True, exist_ok=True)
    path = (WORKTREES / task.id).resolve()
    if WORKTREES.resolve() not in path.parents:
        raise RuntimeError('非法工作树路径')
    if path.exists():
        raise RuntimeError(f'工作树目录已存在但不是有效 Git 工作树: {path}')
    p = subprocess.run(['git', '-C', str(base), 'worktree', 'add', '--detach', str(path), 'HEAD'], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2000:])
    update_task(task.id, worktree_path=str(path))
    materialize_attachments(task.id, path)
    return path


def materialize_attachments(task_id, worktree):
    with SessionLocal() as db:
        rows = db.execute(select(TaskAttachment, Upload).join(Upload, TaskAttachment.upload_id == Upload.id).where(TaskAttachment.task_id == task_id)).all()
    if not rows:
        return
    attachment_dir = (worktree / 'attachments').resolve()
    if worktree.resolve() not in attachment_dir.parents or (worktree / 'attachments').is_symlink():
        raise RuntimeError('非法附件目录')
    attachment_dir.mkdir(parents=True, exist_ok=True)
    for attachment, upload in rows:
        source = UPLOAD_ROOT / str(upload.user_id) / upload.stored_name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f'附件暂存文件不存在: {upload.original_name}')
        clean = Path(attachment.original_name.replace('\\', '/')).name
        clean = ''.join(ch for ch in clean if ch.isprintable() and ch not in '/\\').strip(' .')[:150] or 'file'
        target = attachment_dir / f'{upload.id[:8]}-{clean}'
        if target.is_symlink():
            raise RuntimeError('附件目标路径不能是符号链接')
        if not target.exists():
            shutil.copyfile(source, target)
        source.unlink(missing_ok=True)


def collect_diff(path):
    subprocess.run(['git', '-C', str(path), 'add', '--intent-to-add', '--', '.'], capture_output=True, text=True, timeout=60)
    p = subprocess.run(['git', '-C', str(path), 'diff', '--no-ext-diff', '--binary', 'HEAD'], capture_output=True, text=True, timeout=60)
    diff = p.stdout if p.returncode == 0 else (p.stderr or '')
    return diff[:500000]


def parse_line(task_id, raw):
    line = raw.decode('utf-8', errors='replace').strip()
    if not line:
        return
    try:
        item = json.loads(line)
    except Exception:
        add_event(task_id, 'log', {'text': line[:4000]})
        return
    with SessionLocal() as db:
        t = db.get(Task, task_id)
        if t and item.get('type') == 'thread.started':
            t.codex_session_id = item.get('thread_id') or t.codex_session_id
            db.commit()
        if t and item.get('type') == 'item.completed':
            it = item.get('item') or {}
            if it.get('type') in ('agent_message', 'agentMessage'):
                t.final_output = (t.final_output + '\n' + str(it.get('text', ''))).strip()
                db.commit()
    add_event(task_id, item.get('type', 'event'), item)


def task_command(task):
    command = [CODEX_BIN, 'exec']
    if task.resume_requested and task.codex_session_id:
        command.extend(['resume', task.codex_session_id])
    if task.model:
        command.extend(['--model', task.model])
    if task.reasoning_effort:
        command.extend(['--config', f'model_reasoning_effort="{task.reasoning_effort}"'])
    prompt = task.prompt
    with SessionLocal() as db:
        attached = db.execute(select(TaskAttachment, Upload).join(Upload, TaskAttachment.upload_id == Upload.id).where(TaskAttachment.task_id == task.id)).all()
        snapshot_event = db.scalar(select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.event_type == 'diagnostics.snapshot').order_by(TaskEvent.seq.desc()).limit(1))
    if attached:
        names = []
        for attachment, upload in attached:
            clean = Path(attachment.original_name.replace(chr(92), '/')).name
            names.append(f"attachments/{upload.id[:8]}-{clean}")
        prompt += '\n\n用户附加的文件位于工作区下的 attachments/ 目录，可读取并按要求修改：' + '、'.join(names)
    if snapshot_event:
        prompt += '\n\n管理员请求附加的服务器只读状态快照：\n' + json.dumps(json.loads(snapshot_event.payload).get('data', {}), ensure_ascii=False)
    command.extend(['--json', '--sandbox', 'workspace-write', prompt])
    return command


def cleanup_staged_uploads():
    cutoff = datetime.utcnow() - timedelta(days=30)
    with SessionLocal() as db:
        rows = db.scalars(select(Upload).where(Upload.attached_task_id.is_(None), Upload.created_at < cutoff)).all()
        for row in rows:
            (UPLOAD_ROOT / str(row.user_id) / row.stored_name).unlink(missing_ok=True)
            db.delete(row)
        db.commit()


def run_task(task):
    add_event(task.id, 'worker.started', {'task_id': task.id})
    started = time.time()
    proc = None
    try:
        path = prepare_worktree(task)
        command = task_command(task)
        env = os.environ.copy()
        env['HOME'] = '/home/codex'
        env['CODEX_HOME'] = '/home/codex/.codex'
        proc = subprocess.Popen(command, cwd=path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        buf = b''
        eof = False
        while True:
            with SessionLocal() as db:
                current = db.get(Task, task.id)
                cancel = current and current.status == 'cancel_requested'
            if cancel:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if time.time() - started > MAX_RUNTIME:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise TimeoutError('任务超过运行时限，已终止')
            for key, _ in sel.select(timeout=0.5):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    eof = True
                    try:
                        sel.unregister(key.fileobj)
                    except Exception:
                        pass
                    continue
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    parse_line(task.id, line)
            if proc.poll() is not None and (eof or not sel.get_map()):
                break
        if buf:
            parse_line(task.id, buf)
        if cancel:
            update_task(task.id, status='cancelled', error='由用户取消', final_diff=collect_diff(path))
            add_event(task.id, 'turn.cancelled', {})
        else:
            code = proc.wait()
            diff = collect_diff(path)
            if code == 0:
                update_task(task.id, status='succeeded', final_diff=diff, error='')
                add_event(task.id, 'turn.completed', {'exit_code': code})
            else:
                update_task(task.id, status='failed', final_diff=diff, error=f'Codex 退出码 {code}')
                add_event(task.id, 'turn.failed', {'exit_code': code})
    except Exception as exc:
        if proc and proc.poll() is None:
            proc.kill()
        update_task(task.id, status='failed', error=str(exc)[:4000])
        add_event(task.id, 'turn.failed', {'error': str(exc)[:4000]})
    finally:
        write_status()


def main():
    Base.metadata.create_all(engine)
    ensure_task_settings_columns()
    with SessionLocal() as db:
        running = db.scalars(select(Task).where(Task.status.in_(['running', 'cancel_requested']))).all()
        for t in running:
            t.status = 'interrupted'
            t.error = 'Worker restarted while task was running'
        db.commit()
    last_check = 0
    last_upload_cleanup = 0
    while True:
        if time.time() - last_check > 30:
            write_status()
            last_check = time.time()
        if time.time() - last_upload_cleanup > 3600:
            cleanup_staged_uploads()
            last_upload_cleanup = time.time()
        selected = None
        with SessionLocal() as db:
            t = db.scalar(select(Task).where(Task.status == 'queued').order_by(Task.created_at).limit(1).with_for_update(skip_locked=True))
            if t:
                t.status = 'running'
                t.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(t)
                selected = t
        if selected:
            run_task(selected)
        else:
            time.sleep(POLL)

if __name__ == '__main__':
    main()
