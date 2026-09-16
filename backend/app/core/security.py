"""认证与安全（N3：**仅密码**，TOTP 后置不启用）。

对齐设计 §14 M1-T1 / FR-8.1~8.6：
- **密码慢哈希**：``passlib`` 的 ``argon2id``（不可逆 + 加盐），**绝不落明文**；
  argon2 后端缺失时降级为 ``pbkdf2_sha256``（仍是慢哈希）；
- **会话管理**：``secrets.token_urlsafe`` 随机令牌，落 ``app_session``，带过期时间，
  登出即失效；
- **登录失败限速 / 临时锁定**：连续失败达阈值 → 锁定冷却；成功即清零；
- **SQL 全参数化**（本模块所有查询均用参数占位符，禁止拼接）；
- **TOTP 双因素**：``app_user.totp_secret`` 字段**保留但不启用**（M1 不实现校验，
  接入点预留在登录流程，见 ``verify_login`` 注释）；
- HTTPS / CSRF / CSP 由 ``config`` 开关与 ``main.py`` 中间件就绪。
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from passlib.context import CryptContext

from app.config import Settings
from app.config import settings as default_settings
from app.core.db import APP
from app.core.errors import Unauthorized, ValidationError
from app.core.logging import get_logger
from app.core.timeutil import now_bj_naive
from app.data.repository import Repository

log = get_logger(__name__)

# 优先 argon2id；后端不可用时降级 pbkdf2_sha256（仍为慢哈希）
_PWD_CTX = CryptContext(schemes=["argon2", "pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    """把明文密码哈希（加盐、慢哈希）。"""
    if not password or len(password) < 6:
        raise ValidationError("密码长度至少 6 位")
    return _PWD_CTX.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配（恒定时间）。"""
    if not password or not password_hash:
        return False
    try:
        return bool(_PWD_CTX.verify(password, password_hash))
    except Exception:  # noqa: BLE001 - 哈希格式非法等一律视为不匹配
        return False


class LoginThrottle:
    """登录失败限速 / 临时锁定（进程内；单实例部署）。

    Args:
        max_attempts: 连续失败阈值。
        lock_seconds: 触发锁定后的冷却秒数。
    """

    def __init__(self, max_attempts: int = 5, lock_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.lock_seconds = lock_seconds
        self._fail: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_locked(self, key: str) -> tuple[bool, int]:
        """返回 ``(是否锁定, 剩余秒数)``。"""
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            remain = int(until - time.time())
            if remain > 0:
                return True, remain
            if until and remain <= 0:
                self._locked_until.pop(key, None)
                self._fail.pop(key, None)
            return False, 0

    def record_failure(self, key: str) -> tuple[bool, int]:
        """记录一次失败；返回 ``(是否因此被锁定, 剩余秒数)``。"""
        with self._lock:
            n = self._fail.get(key, 0) + 1
            self._fail[key] = n
            if n >= self.max_attempts:
                self._locked_until[key] = time.time() + self.lock_seconds
                self._fail[key] = 0
                return True, self.lock_seconds
            return False, self.max_attempts - n

    def reset(self, key: str) -> None:
        """登录成功后清零。"""
        with self._lock:
            self._fail.pop(key, None)
            self._locked_until.pop(key, None)


class SecurityService:
    """认证服务（用户 / 会话 / 限速）。

    Args:
        repository: 仓储（app 库）。
        config: 配置。
    """

    def __init__(self, repository: Repository | None = None, config: Settings | None = None) -> None:
        self._cfg = config or default_settings
        self._repo: Repository = repository or Repository()
        self.throttle = LoginThrottle(self._cfg.login_max_attempts, self._cfg.login_lock_seconds)

    # ─────────────── 用户 ───────────────
    def get_user(self, username: str) -> dict[str, Any] | None:
        """查询用户。"""
        con = self._repo.manager.get_read(APP)
        cur = con.execute(
            "SELECT username, password_hash, totp_secret, created_at FROM app_user WHERE username = ?",
            [username],
        )
        cols = [c[0] for c in cur.description]
        row = cur.fetchone()
        return None if row is None else dict(zip(cols, row, strict=False))

    def create_user(self, username: str, password: str, *, totp_secret: str | None = None) -> None:
        """创建用户（密码慢哈希入库）。"""
        if not username:
            raise ValidationError("用户名不能为空")
        # totp_secret 预留但 M1 不启用（N3）
        with self._repo.manager.acquire_write(APP) as con:
            con.execute(
                "INSERT OR REPLACE INTO app_user (username, password_hash, totp_secret, created_at) "
                "VALUES (?, ?, ?, ?)",
                [username, hash_password(password), totp_secret, now_bj_naive()],
            )
        log.info("user_created", username=username)

    def ensure_default_user(self) -> None:
        """首启引导：若库中无任何用户，则用配置的账号/密码建一个。"""
        con = self._repo.manager.get_read(APP)
        row = con.execute("SELECT COUNT(*) FROM app_user").fetchone()
        if row and int(row[0]) > 0:
            return
        self.create_user(self._cfg.admin_username, self._cfg.admin_password)
        log.warning(
            "default_user_bootstrapped",
            username=self._cfg.admin_username,
            hint="生产环境请立即修改默认密码",
        )

    # ─────────────── 登录 ───────────────
    def verify_login(self, username: str, password: str) -> tuple[str, datetime]:
        """校验账号密码，返回 ``(会话令牌, 过期时间)``；失败抛 ``Unauthorized``。

        N3：TOTP 双因素**未启用**——此处为预留接入点（若未来启用，
        在校验密码通过后追加一次 TOTP 校验）。
        """
        locked, remain = self.throttle.is_locked(username)
        if locked:
            raise Unauthorized(f"登录失败次数过多，请 {remain} 秒后重试", code=429)

        user = self.get_user(username)
        # 恒定时间：即使用户不存在也执行一次哈希校验，避免用户名枚举
        password_hash = user["password_hash"] if user else _PWD_CTX.hash("__nonexistent__")
        ok = verify_password(password, password_hash) and user is not None
        if not ok:
            just_locked, left = self.throttle.record_failure(username)
            if just_locked:
                raise Unauthorized("登录失败次数过多，账号已临时锁定", code=429)
            raise Unauthorized(f"用户名或密码错误（剩余尝试 {left} 次）")

        self.throttle.reset(username)
        token, expires_at = self.create_session(username)
        log.info("login_success", username=username)
        return token, expires_at

    # ─────────────── 会话 ───────────────
    def create_session(self, username: str) -> tuple[str, datetime]:
        """创建会话，返回 ``(token, expires_at)``。"""
        token = secrets.token_urlsafe(32)
        ttl = timedelta(hours=self._cfg.session_ttl_hours)
        expires_at = now_bj_naive() + ttl
        with self._repo.manager.acquire_write(APP) as con:
            con.execute(
                "INSERT OR REPLACE INTO app_session (token, username, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                [token, username, expires_at, now_bj_naive()],
            )
        return token, expires_at

    def resolve_session(self, token: str) -> str | None:
        """由令牌解析用户名；无效/过期返回 None（并顺带清理过期）。"""
        if not token:
            return None
        con = self._repo.manager.get_read(APP)
        row = con.execute(
            "SELECT username, expires_at FROM app_session WHERE token = ?", [token]
        ).fetchone()
        if row is None:
            return None
        username, expires_at = row[0], row[1]
        if expires_at is not None and _as_naive(expires_at) <= now_bj_naive():
            self.revoke_session(token)
            return None
        return str(username)

    def revoke_session(self, token: str) -> None:
        """使会话立即失效（登出）。"""
        if not token:
            return
        with self._repo.manager.acquire_write(APP) as con:
            con.execute("DELETE FROM app_session WHERE token = ?", [token])

    def purge_expired(self) -> int:
        """清理过期会话，返回删除条数。"""
        with self._repo.manager.acquire_write(APP) as con:
            before = con.execute("SELECT COUNT(*) FROM app_session").fetchone()
            con.execute("DELETE FROM app_session WHERE expires_at <= ?", [now_bj_naive()])
            after = con.execute("SELECT COUNT(*) FROM app_session").fetchone()
        removed = int((before[0] if before else 0) - (after[0] if after else 0))
        if removed:
            log.info("sessions_purged", removed=removed)
        return removed


def _as_naive(value: Any) -> datetime:
    """把 DuckDB 返回的时间戳归一化为 naive datetime。"""
    import pandas as pd

    ts = pd.Timestamp(value)
    return ts.to_pydatetime().replace(tzinfo=None)


def constant_time_eq(a: str, b: str) -> bool:
    """恒定时间字符串比较（用于令牌/签名比对）。"""
    return hmac.compare_digest(a or "", b or "")


__all__ = [
    "hash_password",
    "verify_password",
    "LoginThrottle",
    "SecurityService",
    "constant_time_eq",
]
