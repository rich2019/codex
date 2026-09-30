import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import secrets
import time
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from nacl.exceptions import CryptoError
from nacl.public import Box, PrivateKey, PublicKey
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

MYSQL_HOST = os.getenv('MYSQL_HOST', 'mysql')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'codex_console')
MYSQL_USER = os.getenv('MYSQL_USER', 'codex_app')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
DATABASE_URL = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}/{MYSQL_DATABASE}?charset=utf8mb4"
COOKIE_NAME = 'cc_session'
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'true').lower() == 'true'
BOOTSTRAP_TOKEN = os.getenv('BOOTSTRAP_TOKEN', '')
CODEX_STATUS_FILE = Path(os.getenv('CODEX_STATUS_FILE', '/state/codex_status.json'))
CODEX_BIN = os.getenv('CODEX_BIN', '/usr/local/bin/codex')
CODEX_HOME = Path(os.getenv('CODEX_HOME', '/home/codex/.codex'))
WORKSPACE = Path(os.getenv('CODEX_WORKSPACE', '/srv/codex/repo'))
WORKTREES = Path(os.getenv('CODEX_WORKTREES', '/srv/codex/worktrees'))
CRYPTO_KEY_FILE = Path(os.getenv('CODEX_CRYPTO_KEY_FILE', '/home/codex/.codex/console-crypto-key'))
ph = PasswordHasher()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=3, max_overflow=2, pool_recycle=1800)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default='user')
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_change_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class UserSession(Base):
    __tablename__ = 'user_sessions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

class Task(Base):
    __tablename__ = 'tasks'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default='queued', index=True)
    codex_session_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resume_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    final_output: Mapped[str] = mapped_column(Text, default='')
    final_diff: Mapped[str] = mapped_column(Text, default='')
    error: Mapped[str] = mapped_column(Text, default='')
    worktree_path: Mapped[str] = mapped_column(String(512), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TaskEvent(Base):
    __tablename__ = 'task_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('tasks.id', ondelete='CASCADE'), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

app = FastAPI(title='Codex Console', docs_url=None, redoc_url=None)

MODEL_CATALOG_TTL = 300
model_catalog_lock = threading.Lock()
model_catalog_cache: dict | None = None
model_catalog_cached_at = 0.0

VALIDATION_LABELS = {
    'password': '密码',
    'new_password': '新密码',
    'current_password': '当前密码',
    'token': '初始化令牌',
    'username': '用户名',
    'role': '角色',
    'prompt': '任务描述',
    'secret': '加密认证信息',
}

CRYPTO_CHALLENGE_TTL = 120
crypto_lock = threading.Lock()
crypto_private_key: PrivateKey | None = None
crypto_challenges: dict[str, float] = {}

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')

def _unb64(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        raise HTTPException(400, f'{field}格式不正确')

def crypto_key() -> PrivateKey:
    global crypto_private_key
    with crypto_lock:
        if crypto_private_key is not None:
            return crypto_private_key
        CRYPTO_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = CRYPTO_KEY_FILE.read_bytes()
            private = PrivateKey(raw)
        except FileNotFoundError:
            private = PrivateKey.generate()
            temporary = CRYPTO_KEY_FILE.with_name(CRYPTO_KEY_FILE.name + '.tmp')
            temporary.write_bytes(bytes(private))
            os.chmod(temporary, 0o600)
            os.replace(temporary, CRYPTO_KEY_FILE)
        except Exception as exc:
            raise RuntimeError(f'无法读取 Codex 控制台加密密钥: {exc}')
        os.chmod(CRYPTO_KEY_FILE, 0o600)
        crypto_private_key = private
        return private

def crypto_key_id(private: PrivateKey) -> str:
    return hashlib.sha256(bytes(private.public_key)).hexdigest()[:16]

def issue_crypto_challenge() -> dict:
    private = crypto_key()
    now = time.time()
    challenge_id = secrets.token_urlsafe(32)
    with crypto_lock:
        for key, expires_at in list(crypto_challenges.items()):
            if expires_at <= now:
                crypto_challenges.pop(key, None)
        crypto_challenges[challenge_id] = now + CRYPTO_CHALLENGE_TTL
    return {
        'challenge_id': challenge_id,
        'key_id': crypto_key_id(private),
        'public_key': _b64(bytes(private.public_key)),
        'expires_in': CRYPTO_CHALLENGE_TTL,
    }

def decrypt_secret(secret: 'SecretEnvelope') -> dict:
    private = crypto_key()
    if secret.key_id != crypto_key_id(private):
        raise HTTPException(400, '加密密钥已更新，请重试')
    now = time.time()
    with crypto_lock:
        expires_at = crypto_challenges.get(secret.challenge_id)
        if not expires_at or expires_at <= now:
            crypto_challenges.pop(secret.challenge_id, None)
            raise HTTPException(409, '加密挑战已过期或已使用，请重试')
    ephemeral = _unb64(secret.ephemeral_public_key, '临时公钥')
    nonce = _unb64(secret.nonce, '加密随机数')
    ciphertext = _unb64(secret.ciphertext, '密文')
    if len(ephemeral) != PrivateKey.SIZE or len(nonce) != Box.NONCE_SIZE:
        raise HTTPException(400, '加密参数长度不正确')
    try:
        plaintext = Box(private, PublicKey(ephemeral)).decrypt(ciphertext, nonce)
        payload = json.loads(plaintext.decode('utf-8'))
    except (CryptoError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, '加密认证信息无法解密，请重试')
    if not isinstance(payload, dict) or payload.get('challenge_id') != secret.challenge_id:
        raise HTTPException(400, '加密挑战校验失败，请重试')
    with crypto_lock:
        expires_at = crypto_challenges.get(secret.challenge_id)
        if not expires_at or expires_at <= time.time():
            crypto_challenges.pop(secret.challenge_id, None)
            raise HTTPException(409, '加密挑战已过期或已使用，请重试')
        crypto_challenges.pop(secret.challenge_id, None)
    return payload

def secret_text(payload: dict, key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HTTPException(422, f'{label}不能为空')
    return value

def validation_error_message(exc: RequestValidationError) -> str:
    messages = []
    for item in exc.errors():
        location = item.get('loc') or []
        field = str(location[-1]) if location else ''
        label = VALIDATION_LABELS.get(field, field or '请求参数')
        error_type = item.get('type', '')
        if error_type in ('missing', 'string_too_short'):
            message = '不能为空'
        elif error_type in ('string_too_long', 'too_long'):
            message = '过长'
        elif error_type == 'string_pattern_mismatch':
            message = '格式不正确'
        else:
            message = str(item.get('msg') or '参数不合法')
        messages.append(f'{label}{message}')
    return '；'.join(messages) or '请求参数不合法'

@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={'detail': validation_error_message(exc)})

def db_dep() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def user_json(u: User) -> dict:
    return {'id': u.id, 'username': u.username, 'role': u.role, 'active': u.active, 'password_change_required': u.password_change_required, 'created_at': u.created_at.isoformat() + 'Z'}

def task_json(t: Task) -> dict:
    return {'id': t.id, 'user_id': t.user_id, 'prompt': t.prompt, 'model': t.model, 'reasoning_effort': t.reasoning_effort, 'status': t.status, 'codex_session_id': t.codex_session_id, 'final_output': t.final_output, 'error': t.error, 'created_at': t.created_at.isoformat() + 'Z', 'updated_at': t.updated_at.isoformat() + 'Z'}

def current_user(request: Request, db: Session = Depends(db_dep)) -> User:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise HTTPException(401, '请先登录')
    row = db.scalar(select(UserSession).where(UserSession.token_hash == hashlib.sha256(raw.encode()).hexdigest(), UserSession.expires_at > datetime.utcnow()))
    if not row:
        raise HTTPException(401, '登录已过期，请重新登录')
    u = db.get(User, row.user_id)
    if not u or not u.active:
        raise HTTPException(401, '账号已停用')
    request.state.session_row = row
    return u

def require_ready_user(u: User = Depends(current_user)) -> User:
    if u.password_change_required:
        raise HTTPException(403, '请先修改临时密码')
    return u

def admin_user(u: User = Depends(require_ready_user)) -> User:
    if u.role != 'admin':
        raise HTTPException(403, '需要管理员权限')
    return u

def verify_csrf(request: Request, x_csrf_token: str | None):
    row = getattr(request.state, 'session_row', None)
    if not row or not x_csrf_token or not secrets.compare_digest(row.csrf_token, x_csrf_token):
        raise HTTPException(403, 'CSRF 校验失败')

class SecretEnvelope(BaseModel):
    key_id: str = Field(min_length=8, max_length=64)
    challenge_id: str = Field(min_length=20, max_length=128)
    ephemeral_public_key: str = Field(min_length=32, max_length=128)
    nonce: str = Field(min_length=24, max_length=128)
    ciphertext: str = Field(min_length=16, max_length=200000)

class BootstrapIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r'^[a-zA-Z0-9_.-]+$')
    secret: SecretEnvelope

class LoginIn(BaseModel):
    username: str
    secret: SecretEnvelope

class PasswordIn(BaseModel):
    secret: SecretEnvelope

class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r'^[a-zA-Z0-9_.-]+$')
    role: str = 'user'

class TaskIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=16)


def ensure_task_settings_columns(db_engine=engine):
    """Add nullable task settings to existing MySQL databases, safely across API/worker startup."""
    lock_name = 'codex_console_task_settings_migration'
    with db_engine.connect() as connection:
        locked = False
        try:
            if connection.dialect.name == 'mysql':
                locked = connection.execute(
                    text('SELECT GET_LOCK(:name, 30)'), {'name': lock_name}
                ).scalar() == 1
                if not locked:
                    raise RuntimeError('Timed out waiting for task schema migration lock')
            columns = {column['name'] for column in inspect(connection).get_columns('tasks')}
            if 'model' not in columns:
                connection.execute(text('ALTER TABLE tasks ADD COLUMN model VARCHAR(128) NULL'))
            if 'reasoning_effort' not in columns:
                connection.execute(text('ALTER TABLE tasks ADD COLUMN reasoning_effort VARCHAR(16) NULL'))
            connection.commit()
        finally:
            if locked:
                connection.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': lock_name})
                connection.commit()


def _configured_codex_model() -> str | None:
    config_path = CODEX_HOME / 'config.toml'
    try:
        config = tomllib.loads(config_path.read_text(encoding='utf-8'))
        model = config.get('model')
        return model if isinstance(model, str) and model else None
    except Exception:
        return None


def get_codex_model_catalog() -> dict:
    """Return a safe, minimal view of models advertised by the installed Codex CLI."""
    global model_catalog_cache, model_catalog_cached_at
    now = time.monotonic()
    with model_catalog_lock:
        if model_catalog_cache and now - model_catalog_cached_at < MODEL_CATALOG_TTL:
            return dict(model_catalog_cache)

        raw_catalog = None
        catalog_error = ''
        for args in ([CODEX_BIN, 'debug', 'models'], [CODEX_BIN, 'debug', 'models', '--bundled']):
            try:
                result = subprocess.run(args, capture_output=True, text=True, timeout=20, env=os.environ)
                if result.returncode == 0:
                    candidate = json.loads(result.stdout)
                    if isinstance(candidate, dict) and isinstance(candidate.get('models'), list):
                        raw_catalog = candidate
                        break
                catalog_error = 'Codex 模型目录不可用'
            except Exception:
                catalog_error = 'Codex 模型目录不可用'

        if raw_catalog is None:
            if model_catalog_cache:
                stale = dict(model_catalog_cache)
                stale['stale'] = True
                return stale
            return {'available': False, 'default_model': None, 'models': [], 'message': catalog_error or 'Codex 模型目录不可用'}

        models = []
        for entry in raw_catalog['models']:
            if not isinstance(entry, dict) or entry.get('visibility') != 'list' or entry.get('supported_in_api') is not True:
                continue
            model_id = entry.get('slug')
            if not isinstance(model_id, str) or not model_id:
                continue
            levels = entry.get('supported_reasoning_levels') or []
            efforts = []
            for level in levels:
                effort = level.get('effort') if isinstance(level, dict) else level
                if isinstance(effort, str) and effort not in efforts:
                    efforts.append(effort)
            models.append({
                'id': model_id,
                'name': str(entry.get('display_name') or model_id),
                'default_effort': entry.get('default_reasoning_level') if isinstance(entry.get('default_reasoning_level'), str) else None,
                'reasoning_efforts': efforts,
            })

        configured_default = _configured_codex_model()
        model_ids = {entry['id'] for entry in models}
        catalog = {
            'available': True,
            'default_model': configured_default if configured_default in model_ids else None,
            'models': models,
            'stale': False,
        }
        model_catalog_cache = catalog
        model_catalog_cached_at = time.monotonic()
        return dict(catalog)


def validate_task_settings(model: str | None, reasoning_effort: str | None):
    if model is None and reasoning_effort is None:
        return
    catalog = get_codex_model_catalog()
    if not catalog.get('available'):
        raise HTTPException(503, 'Codex 模型目录暂不可用，请使用 Codex 默认设置后重试')
    selected_id = model or catalog.get('default_model')
    selected = next((item for item in catalog['models'] if item['id'] == selected_id), None)
    if model is not None and selected is None:
        raise HTTPException(422, '所选模型不在当前 Codex 模型目录中')
    if reasoning_effort is not None:
        if selected is None:
            raise HTTPException(422, '请先选择具体模型，再设置推理强度')
        if reasoning_effort not in selected['reasoning_efforts']:
            raise HTTPException(422, '该模型不支持所选推理强度')

failed_logins: dict[str, list[float]] = {}
login_lock = threading.Lock()
login_proc = None
login_state = {'running': False, 'authenticated': False, 'device_url': '', 'user_code': '', 'message': ''}
ANSI_ESCAPE_RE = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
DEVICE_CODE_RE = re.compile(r'(?<![A-Z0-9])([A-Z0-9]{4,8})\s*-\s*([A-Z0-9]{4,8})(?![A-Z0-9])', re.IGNORECASE)

def _device_login_reader(proc):
    global login_state, login_proc
    try:
        for line in proc.stdout:
            text = ANSI_ESCAPE_RE.sub('', line).replace('\r', '').strip()
            url = re.search(r'https?://[^\s]+', text)
            code = DEVICE_CODE_RE.search(text)
            with login_lock:
                if url:
                    login_state['device_url'] = url.group(0).rstrip('.,')
                if code:
                    login_state['user_code'] = f'{code.group(1).upper()}-{code.group(2).upper()}'
                    login_state['message'] = '请在授权页面输入上方代码'
                elif url:
                    login_state['message'] = '正在等待账号授权'
        rc = proc.wait()
        with login_lock:
            login_state['running'] = False
            login_state['authenticated'] = rc == 0
            login_state['message'] = 'ChatGPT 登录已完成' if rc == 0 else '登录流程已结束，请重试'
    except Exception:
        with login_lock:
            login_state['running'] = False
            login_state['message'] = '读取登录状态失败'


@app.on_event('startup')
def startup():
    for _ in range(30):
        try:
            Base.metadata.create_all(engine)
            ensure_task_settings_columns()
            crypto_key()
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError('MySQL did not become ready')

@app.get('/api/health')
def health(db: Session = Depends(db_dep)):
    try:
        db.execute(select(func.now()))
        return {'ok': True, 'database': 'connected'}
    except Exception:
        raise HTTPException(503, 'database unavailable')

@app.get('/api/setup/status')
def setup_status(db: Session = Depends(db_dep)):
    return {'needs_admin': db.scalar(select(func.count(User.id))) == 0}

@app.get('/api/auth/crypto/challenge')
def crypto_challenge():
    return issue_crypto_challenge()

@app.post('/api/setup/bootstrap')
def bootstrap(data: BootstrapIn, db: Session = Depends(db_dep)):
    payload = decrypt_secret(data.secret)
    token = secret_text(payload, 'token', '初始化令牌')
    password = secret_text(payload, 'password', '密码')
    if not BOOTSTRAP_TOKEN or not secrets.compare_digest(token, BOOTSTRAP_TOKEN):
        raise HTTPException(403, '初始化令牌错误')
    if db.scalar(select(func.count(User.id))) != 0:
        raise HTTPException(409, '管理员已初始化')
    u = User(username=data.username, password_hash=ph.hash(password), role='admin')
    db.add(u)
    db.commit()
    return {'ok': True}

@app.post('/api/auth/login')
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(db_dep)):
    payload = decrypt_secret(data.secret)
    password = secret_text(payload, 'password', '密码')
    ip = request.headers.get('x-real-ip', request.client.host if request.client else 'unknown')
    key = f'{ip}:{data.username.lower()}'
    now = time.time()
    attempts = [t for t in failed_logins.get(key, []) if now - t < 600]
    if len(attempts) >= 8:
        raise HTTPException(429, '尝试次数过多，请 10 分钟后再试')
    u = db.scalar(select(User).where(User.username == data.username))
    ok = False
    if u and u.active:
        try:
            ok = ph.verify(u.password_hash, password)
        except VerifyMismatchError:
            ok = False
    if not ok:
        attempts.append(now)
        failed_logins[key] = attempts
        raise HTTPException(401, '用户名或密码错误')
    failed_logins.pop(key, None)
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    row = UserSession(user_id=u.id, token_hash=hashlib.sha256(raw.encode()).hexdigest(), csrf_token=csrf, expires_at=datetime.utcnow() + timedelta(hours=12))
    db.add(row)
    db.commit()
    response.set_cookie(COOKIE_NAME, raw, httponly=True, secure=COOKIE_SECURE, samesite='strict', max_age=43200, path='/')
    return {'user': user_json(u), 'csrf_token': csrf}

@app.post('/api/auth/logout')
def logout(request: Request, response: Response, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_dep), u: User = Depends(current_user)):
    verify_csrf(request, x_csrf_token)
    row = request.state.session_row
    db.delete(row)
    db.commit()
    response.delete_cookie(COOKIE_NAME, path='/')
    return {'ok': True}

@app.get('/api/auth/me')
def me(request: Request, u: User = Depends(current_user)):
    return {'user': user_json(u), 'csrf_token': request.state.session_row.csrf_token}

@app.post('/api/auth/password')
def change_password(data: PasswordIn, request: Request, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_dep), u: User = Depends(current_user)):
    verify_csrf(request, x_csrf_token)
    payload = decrypt_secret(data.secret)
    current_password = secret_text(payload, 'current_password', '当前密码') if not u.password_change_required else ''
    new_password = secret_text(payload, 'new_password', '新密码')
    if not u.password_change_required:
        try:
            if not ph.verify(u.password_hash, current_password):
                raise HTTPException(400, '当前密码不正确')
        except VerifyMismatchError:
            raise HTTPException(400, '当前密码不正确')
    u.password_hash = ph.hash(new_password)
    u.password_change_required = False
    db.commit()
    return {'ok': True, 'user': user_json(u)}

@app.post('/api/admin/codex/login/start')
def start_codex_login(request: Request, x_csrf_token: str | None = Header(default=None), u: User = Depends(admin_user)):
    global login_proc, login_state
    verify_csrf(request, x_csrf_token)
    with login_lock:
        if login_state.get('running') and login_proc and login_proc.poll() is None:
            return dict(login_state)
        login_state = {'running': True, 'authenticated': False, 'device_url': '', 'user_code': '', 'message': '正在启动 ChatGPT 设备登录'}
        try:
            login_proc = subprocess.Popen(['/usr/local/bin/codex', 'login', '--device-auth'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=os.environ)
        except Exception as exc:
            login_state = {'running': False, 'authenticated': False, 'device_url': '', 'user_code': '', 'message': f'无法启动 Codex 登录: {str(exc)[:180]}'}
            raise HTTPException(503, login_state['message'])
        threading.Thread(target=_device_login_reader, args=(login_proc,), daemon=True).start()
        return dict(login_state)

@app.get('/api/admin/codex/login/status')
def codex_login_status(u: User = Depends(admin_user)):
    global login_state
    try:
        p = subprocess.run(['/usr/local/bin/codex', 'login', 'status'], capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            with login_lock:
                login_state.update({'running': False, 'authenticated': True, 'message': 'ChatGPT 登录有效'})
    except Exception:
        pass
    with login_lock:
        return dict(login_state)

@app.post('/api/admin/codex/login/cancel')
def cancel_codex_login(request: Request, x_csrf_token: str | None = Header(default=None), u: User = Depends(admin_user)):
    global login_state, login_proc
    verify_csrf(request, x_csrf_token)
    with login_lock:
        if login_proc and login_proc.poll() is None:
            login_proc.terminate()
        login_state.update({'running': False, 'user_code': '', 'device_url': '', 'message': '登录已取消'})
        return dict(login_state)

@app.get('/api/admin/users')
def list_users(u: User = Depends(admin_user), db: Session = Depends(db_dep)):
    return [user_json(x) for x in db.scalars(select(User).order_by(User.id)).all()]

@app.post('/api/admin/users')
def create_user(data: UserCreateIn, request: Request, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_dep), u: User = Depends(admin_user)):
    verify_csrf(request, x_csrf_token)
    if data.role not in ('admin', 'user'):
        raise HTTPException(400, 'role 必须是 admin 或 user')
    if db.scalar(select(User).where(User.username == data.username)):
        raise HTTPException(409, '用户名已存在')
    temporary = secrets.token_urlsafe(15)
    target = User(username=data.username, password_hash=ph.hash(temporary), role=data.role, password_change_required=True)
    db.add(target)
    db.commit()
    return {'user': user_json(target), 'temporary_password': temporary}

@app.patch('/api/admin/users/{user_id}')
def update_user(user_id: int, request: Request, active: bool, role: str | None = None, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_dep), u: User = Depends(admin_user)):
    verify_csrf(request, x_csrf_token)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, '用户不存在')
    new_role = role or target.role
    if new_role not in ('admin', 'user'):
        raise HTTPException(400, 'role 必须是 admin 或 user')
    if target.id == u.id and (not active or new_role != 'admin'):
        raise HTTPException(400, '不能停用或降级当前管理员')
    if target.role == 'admin' and (not active or new_role != 'admin'):
        admins = db.scalar(select(func.count(User.id)).where(User.role == 'admin', User.active == True)) or 0
        if admins <= 1:
            raise HTTPException(400, '至少保留一个启用的管理员')
    target.active = active
    target.role = new_role
    db.commit()
    if not active:
        db.query(UserSession).filter(UserSession.user_id == target.id).delete()
        db.commit()
    return user_json(target)

@app.post('/api/admin/users/{user_id}/reset-password')
def reset_password(user_id: int, request: Request, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_dep), u: User = Depends(admin_user)):
    verify_csrf(request, x_csrf_token)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, '用户不存在')
    temporary = secrets.token_urlsafe(15)
    target.password_hash = ph.hash(temporary)
    target.password_change_required = True
    db.query(UserSession).filter(UserSession.user_id == target.id).delete()
    db.commit()
    return {'temporary_password': temporary}

def task_allowed(t: Task, u: User):
    if u.role != 'admin' and t.user_id != u.id:
        raise HTTPException(404, '任务不存在')
    return t

def codex_status() -> dict:
    try:
        return json.loads(CODEX_STATUS_FILE.read_text())
    except Exception:
        return {'authenticated': False, 'message': 'Codex worker 尚未就绪'}

@app.get('/api/system/status')
def system_status(u: User = Depends(require_ready_user)):
    return {'codex': codex_status(), 'workspace_git': (WORKSPACE / '.git').exists(), 'workspace': str(WORKSPACE)}

@app.get('/api/codex/models')
def codex_models(u: User = Depends(require_ready_user)):
    return get_codex_model_catalog()

@app.post('/api/tasks')
def create_task(data: TaskIn, request: Request, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_dep), u: User = Depends(require_ready_user)):
    verify_csrf(request, x_csrf_token)
    validate_task_settings(data.model, data.reasoning_effort)
    if not (WORKSPACE / '.git').exists():
        raise HTTPException(409, '尚未配置 Git 工作区，请先将目标仓库放到服务器工作区')
    status = codex_status()
    if not status.get('authenticated'):
        raise HTTPException(503, status.get('message', '请先在服务器完成 Codex ChatGPT 登录'))
    t = Task(id=str(uuid.uuid4()), user_id=u.id, prompt=data.prompt.strip(), model=data.model, reasoning_effort=data.reasoning_effort)
    db.add(t)
    db.commit()
    return task_json(t)

@app.get('/api/tasks')
def list_tasks(u: User = Depends(require_ready_user), db: Session = Depends(db_dep)):
    q = select(Task).order_by(Task.created_at.desc()).limit(200)
    if u.role != 'admin':
        q = q.where(Task.user_id == u.id)
    return [task_json(t) for t in db.scalars(q).all()]

@app.get('/api/tasks/{task_id}')
def get_task(task_id: str, u: User = Depends(require_ready_user), db: Session = Depends(db_dep)):
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, '任务不存在')
    task_allowed(t, u)
    events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.seq).limit(500)).all()
    return {**task_json(t), 'events': [{'seq': e.seq, 'event_type': e.event_type, 'payload': json.loads(e.payload), 'created_at': e.created_at.isoformat() + 'Z'} for e in events], 'diff': t.final_diff}

@app.get('/api/tasks/{task_id}/events')
async def stream_events(task_id: str, after: int = 0, u: User = Depends(require_ready_user)):
    with SessionLocal() as db:
        t = db.get(Task, task_id)
        if not t:
            raise HTTPException(404, '任务不存在')
        task_allowed(t, u)
    async def gen():
        last = after
        while True:
            with SessionLocal() as db:
                rows = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.seq > last).order_by(TaskEvent.seq).limit(100)).all()
                state = db.get(Task, task_id)
                for e in rows:
                    last = e.seq
                    yield f'id: {e.seq}\ndata: {e.payload}\n\n'
                if state and state.status in ('succeeded', 'failed', 'cancelled', 'interrupted') and not rows:
                    yield 'event: end\ndata: {}\n\n'
                    break
            yield ': keepalive\n\n'
            import asyncio
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.post('/api/tasks/{task_id}/cancel')
def cancel_task(task_id: str, request: Request, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_dep), u: User = Depends(require_ready_user)):
    verify_csrf(request, x_csrf_token)
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, '任务不存在')
    task_allowed(t, u)
    if t.status == 'queued':
        t.status = 'cancelled'
    elif t.status == 'running':
        t.status = 'cancel_requested'
    db.commit()
    return task_json(t)

@app.post('/api/tasks/{task_id}/resume')
def resume_task(task_id: str, data: TaskIn, request: Request, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_dep), u: User = Depends(require_ready_user)):
    verify_csrf(request, x_csrf_token)
    old = db.get(Task, task_id)
    if not old:
        raise HTTPException(404, '任务不存在')
    task_allowed(old, u)
    if old.status not in ('succeeded', 'failed', 'interrupted', 'cancelled') or not old.codex_session_id:
        raise HTTPException(409, '该任务没有可继续的 Codex 会话')
    model = data.model if 'model' in data.model_fields_set else old.model
    reasoning_effort = data.reasoning_effort if 'reasoning_effort' in data.model_fields_set else old.reasoning_effort
    validate_task_settings(model, reasoning_effort)
    t = Task(id=str(uuid.uuid4()), user_id=u.id, prompt=data.prompt.strip(), model=model, reasoning_effort=reasoning_effort, codex_session_id=old.codex_session_id, resume_requested=True, worktree_path=old.worktree_path)
    db.add(t)
    db.commit()
    return task_json(t)

@app.get('/api/tasks/{task_id}/diff')
def task_diff(task_id: str, u: User = Depends(require_ready_user), db: Session = Depends(db_dep)):
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, '任务不存在')
    task_allowed(t, u)
    return {'diff': t.final_diff}
