"""SQLite接続・スキーマ・設定・バックアップ。"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY,
    code       TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    price      INTEGER NOT NULL CHECK (price > 0 AND price % 50 = 0),
    sort_order INTEGER NOT NULL DEFAULT 0,
    sold_out   INTEGER NOT NULL DEFAULT 0 CHECK (sold_out IN (0, 1)),
    deleted_at TEXT,
    stock      INTEGER CHECK (stock IS NULL OR stock >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_uuid   TEXT    UNIQUE,
    business_date TEXT    NOT NULL,
    call_no       INTEGER NOT NULL,
    total         INTEGER NOT NULL CHECK (total > 0 AND total % 50 = 0),
    status        TEXT    NOT NULL
                  CHECK (status IN ('waiting', 'cooking', 'delivered', 'cancelled')),
    source        TEXT    NOT NULL DEFAULT 'pos' CHECK (source IN ('pos', 'manual')),
    paid_at       TEXT    NOT NULL,
    started_at    TEXT,
    delivered_at  TEXT,
    cancelled_at  TEXT,
    cancel_ack_at TEXT,   -- 厨房が取消表示を「確認」した時刻（K-07）
    is_test       INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders (business_date);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

CREATE TABLE IF NOT EXISTS order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products (id),
    name       TEXT    NOT NULL,
    unit_price INTEGER NOT NULL CHECK (unit_price > 0 AND unit_price % 50 = 0),
    qty        INTEGER NOT NULL CHECK (qty >= 1)
);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items (order_id);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT    NOT NULL,
    terminal    TEXT    NOT NULL,   -- PC1受付 / PC2厨房 / PC3管理 / システム
    terminal_id TEXT,
    kind        TEXT    NOT NULL,
    target_id   INTEGER,
    content     TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);
"""

DB_FILENAME = "pos.db"


def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now().date().isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class Tx:
    """BEGIN IMMEDIATE ～ COMMIT/ROLLBACK をまとめるコンテキストマネージャ。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "shop_name": "",
    "day1": "",
    "day2": "",
    "call_no_start": "1",
    "test_mode": "0",
    "usb_backup_dir": "",
    "next_product_no": "1",
    "passcode_changed": "0",
}

# 端末へ配ってよい設定（パスコードなどは含めない）
PUBLIC_SETTINGS = ("shop_name", "day1", "day2", "call_no_start", "test_mode")


def init_db(conn: sqlite3.Connection) -> str | None:
    """スキーマを作り、必須の設定に初期値を入れる。

    初回（パスコード未設定）のときだけ、生成した初期パスコードを返す。
    """
    from .auth import hash_passcode

    conn.executescript(SCHEMA)
    initial_passcode = None
    with Tx(conn):
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now_str()),
            )
        if get_setting(conn, "session_secret") is None:
            set_setting(conn, "session_secret", secrets.token_hex(32))
        if get_setting(conn, "passcode_hash") is None:
            initial_passcode = f"{secrets.randbelow(1_000_000):06d}"
            set_setting(conn, "passcode_hash", hash_passcode(initial_passcode))
    return initial_passcode


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now_str()),
    )


def public_settings(conn: sqlite3.Connection) -> dict:
    s = {k: get_setting(conn, k, DEFAULT_SETTINGS.get(k, "")) for k in PUBLIC_SETTINGS}
    s["test_mode"] = s["test_mode"] == "1"
    s["call_no_start"] = int(s["call_no_start"] or 1)
    s["today"] = today_str()
    s["day_label"] = day_label(s, s["today"])
    return s


def day_label(settings: dict, date: str) -> str:
    if date and date == settings.get("day1"):
        return "1日目"
    if date and date == settings.get("day2"):
        return "2日目"
    return ""


# ---------------------------------------------------------------- events


def log_event(
    conn: sqlite3.Connection,
    terminal: str,
    kind: str,
    target_id: int | None = None,
    content: dict | None = None,
    terminal_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (at, terminal, terminal_id, kind, target_id, content) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            now_str(),
            terminal,
            terminal_id,
            kind,
            target_id,
            json.dumps(content or {}, ensure_ascii=False),
        ),
    )


# ---------------------------------------------------------------- backup


def backup_to(conn: sqlite3.Connection, dest: Path) -> Path:
    """SQLiteのバックアップAPIで dest へ書き出す（ファイルの単純コピーはしない）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    target = sqlite3.connect(tmp)
    try:
        conn.backup(target)
    finally:
        target.close()
    tmp.replace(dest)
    return dest


def backup_filename() -> str:
    return f"pos-{datetime.now():%Y%m%d-%H%M%S}.db"


def prune_backups(folder: Path, keep: int = 300) -> None:
    files = sorted(folder.glob("pos-*.db"))
    for f in files[:-keep]:
        try:
            f.unlink()
        except OSError:
            pass


def run_backup(conn: sqlite3.Connection, data_dir: Path) -> dict:
    """data/backups/ と、設定されていればUSBメモリのフォルダへバックアップする。"""
    name = backup_filename()
    result: dict = {"at": now_str(), "path": None, "usb_path": None, "usb_error": None}
    local_dir = data_dir / "backups"
    result["path"] = str(backup_to(conn, local_dir / name))
    prune_backups(local_dir)
    usb = (get_setting(conn, "usb_backup_dir") or "").strip()
    if usb:
        usb_dir = Path(usb)
        if not usb_dir.exists():
            result["usb_error"] = f"フォルダが見つかりません: {usb}"
        else:
            try:
                result["usb_path"] = str(backup_to(conn, usb_dir / "gakusai-pos" / name))
                prune_backups(usb_dir / "gakusai-pos")
            except OSError as e:
                result["usb_error"] = f"書き込めません: {e}"
    return result
