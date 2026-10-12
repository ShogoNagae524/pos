"""パスコード・端末識別（Cookie）・PC3限定のチェック。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request, WebSocket

COOKIE_NAME = "gakusai_pos_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7

ROLES = {"reception": "PC1受付", "kitchen": "PC2厨房"}
ADMIN_LABEL = "PC3管理"

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# ---------------------------------------------------------------- passcode


def hash_passcode(passcode: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(passcode.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_passcode(passcode: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    salt = base64.b64decode(salt_b64)
    digest = hashlib.scrypt(passcode.encode(), salt=salt, n=2**14, r=8, p=1)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# ---------------------------------------------------------------- session cookie


@dataclass(frozen=True)
class Terminal:
    role: str  # reception / kitchen / admin
    tid: str  # 端末ごとのランダムID（ログ・接続状況用）

    @property
    def label(self) -> str:
        return ADMIN_LABEL if self.role == "admin" else ROLES[self.role]


def _sign(secret: str, passcode_hash: str, payload: str) -> str:
    # パスコードのハッシュも鍵に混ぜ、パスコード変更で既存の登録を無効にする
    key = (secret + passcode_hash).encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(secret: str, passcode_hash: str, role: str, tid: str) -> str:
    payload = f"{role}.{tid}"
    return f"{payload}.{_sign(secret, passcode_hash, payload)}"


def parse_token(secret: str, passcode_hash: str, token: str | None) -> Terminal | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    role, tid, sig = parts
    if role not in ROLES:
        return None
    if not hmac.compare_digest(sig, _sign(secret, passcode_hash, f"{role}.{tid}")):
        return None
    return Terminal(role, tid)


# ---------------------------------------------------------------- PC3 限定


def is_local(host: str | None, local_ips: set[str]) -> bool:
    """接続元がPC3自身（ループバックまたは自分のIP）かどうか。"""
    if not host:
        return False
    host = host.removeprefix("::ffff:")
    return host in LOOPBACK or host in local_ips


def _client_host(conn: Request | WebSocket) -> str | None:
    return conn.client.host if conn.client else None


def terminal_from(conn: Request | WebSocket) -> Terminal | None:
    """Cookieから端末を取り出す。PC3自身からならCookieがなくても管理端末とみなす。"""
    from .db import get_setting

    state = conn.app.state
    db = state.db
    term = parse_token(
        get_setting(db, "session_secret", ""),
        get_setting(db, "passcode_hash", ""),
        conn.cookies.get(COOKIE_NAME),
    )
    if term:
        return term
    if is_local(_client_host(conn), state.local_ips):
        return Terminal("admin", "pc3")
    return None


def require_terminal(request: Request) -> Terminal:
    term = terminal_from(request)
    if term is None:
        raise HTTPException(401, "端末が登録されていません。パスコードを入力してください")
    return term


def require_role(*roles: str):
    def dep(request: Request) -> Terminal:
        term = require_terminal(request)
        if term.role not in roles:
            raise HTTPException(403, "この端末では実行できない操作です")
        return term

    return dep


def require_admin(request: Request) -> Terminal:
    if not is_local(_client_host(request), request.app.state.local_ips):
        raise HTTPException(403, "管理機能はPC3からのみ使えます")
    return Terminal("admin", "pc3")
