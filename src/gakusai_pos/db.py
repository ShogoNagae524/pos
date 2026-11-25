"""SQLite接続・スキーマ・設定・バックアップ。"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
{products}

{orders}
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders (business_date);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

{order_items}
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items (order_id);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT    NOT NULL,
    terminal    TEXT    NOT NULL,   -- PC1受付 / PC2厨房 / PC3管理 / PC4物販 / システム（PC5は表示のみ）
    terminal_id TEXT,
    kind        TEXT    NOT NULL,
    target_id   INTEGER,
    content     TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);
"""

# 価格がマイナスの商品は割引券（例 -50 = 50円引き）。0円の商品は作れない
PRODUCTS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id         INTEGER PRIMARY KEY,
    code       TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    price      INTEGER NOT NULL CHECK (price <> 0 AND price % 50 = 0),
    sort_order INTEGER NOT NULL DEFAULT 0,
    sold_out   INTEGER NOT NULL DEFAULT 0 CHECK (sold_out IN (0, 1)),
    deleted_at TEXT,
    stock      INTEGER CHECK (stock IS NULL OR stock >= 0),
    category   TEXT    NOT NULL DEFAULT 'food' CHECK (category IN ('food', 'goods')),
    -- 物販で、その場で作ってから渡す商品（3Dプリントなど）。注文は準備中から始まりPC5に出る
    needs_prep INTEGER NOT NULL DEFAULT 0 CHECK (needs_prep IN (0, 1))
);
"""

ORDER_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products (id),
    name       TEXT    NOT NULL,
    unit_price INTEGER NOT NULL CHECK (unit_price <> 0 AND unit_price % 50 = 0),
    qty        INTEGER NOT NULL CHECK (qty >= 1)
);
"""

# 「準備中」は waiting、物販の「受渡可能」は ready を使う（ready は物販だけ）。
# cooking は以前の飲食の「調理中」で、今は使わない（起動時に waiting へ移す）
ORDERS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_uuid   TEXT    UNIQUE,
    business_date TEXT    NOT NULL,
    call_no       INTEGER NOT NULL,
    -- 割引券で0円になる会計もある（マイナスにはしない。おつりは出さない）
    total         INTEGER NOT NULL CHECK (total >= 0 AND total % 50 = 0),
    status        TEXT    NOT NULL
                  CHECK (status IN ('waiting', 'cooking', 'ready', 'delivered', 'cancelled')),
    source        TEXT    NOT NULL DEFAULT 'pos' CHECK (source IN ('pos', 'manual')),
    paid_at       TEXT    NOT NULL,
    started_at    TEXT,
    ready_at      TEXT,   -- 物販で「受渡可能」にした時刻
    delivered_at  TEXT,
    cancelled_at  TEXT,
    cancel_ack_at TEXT,   -- 厨房が取消表示を「確認」した時刻（K-07）
    is_test       INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1)),
    -- food: 飲食（PC1→PC2厨房） / goods: 物販（PC4。準備が要らなければ会計時点で受渡済）
    category      TEXT    NOT NULL DEFAULT 'food' CHECK (category IN ('food', 'goods'))
);
"""

TABLES = {
    "products": PRODUCTS_TABLE,
    "orders": ORDERS_TABLE,
    "order_items": ORDER_ITEMS_TABLE,
}
for _name, _sql in TABLES.items():
    SCHEMA = SCHEMA.replace("{" + _name + "}", _sql.format(name=_name).strip())

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
    "preorder_url": "",  # 事前注文ページを置いた公開URL（案内用QRの中身）
}

# 端末へ配ってよい設定（パスコードなどは含めない）
PUBLIC_SETTINGS = ("shop_name", "day1", "day2", "call_no_start", "test_mode")


def init_db(conn: sqlite3.Connection) -> str | None:
    """スキーマを作り、必須の設定に初期値を入れる。

    初回（パスコード未設定）のときだけ、生成した初期パスコードを返す。
    """
    from .auth import hash_passcode

    conn.executescript(SCHEMA)
    _migrate(conn)
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


def _migrate(conn: sqlite3.Connection) -> None:
    """以前の版で作ったDBを今のスキーマに合わせる（物販の区分・準備中・受渡可能）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
    if "category" not in cols:
        conn.execute(
            "ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT 'food' "
            "CHECK (category IN ('food', 'goods'))"
        )
    if "needs_prep" not in cols:
        conn.execute(
            "ALTER TABLE products ADD COLUMN needs_prep INTEGER NOT NULL DEFAULT 0 "
            "CHECK (needs_prep IN (0, 1))"
        )
    # 制約を変えた表は作り直す（ready の追加、割引券のためのマイナス価格・0円の会計）
    for name, needs in (
        ("products", lambda s: "price > 0" in s),
        ("orders", lambda s: "'ready'" not in s or "total > 0" in s),
        ("order_items", lambda s: "unit_price > 0" in s),
    ):
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        if row and needs(row[0]):
            _rebuild_table(conn, name)
    # 飲食の「未着手」「調理中」は「準備中」（waiting）にまとめた。残っている調理中を移す
    with Tx(conn):
        conn.execute("UPDATE orders SET status = 'waiting' WHERE status = 'cooking'")


TABLE_INDEXES = {
    "products": [],
    "orders": [
        "CREATE INDEX IF NOT EXISTS idx_orders_date ON orders (business_date)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status)",
    ],
    "order_items": ["CREATE INDEX IF NOT EXISTS idx_items_order ON order_items (order_id)"],
}


def _rebuild_table(conn: sqlite3.Connection, name: str) -> None:
    """表を今の定義で作り直してデータを移す（SQLiteは制約を変更できない）。"""
    old = [r["name"] for r in conn.execute(f"PRAGMA table_info({name})")]
    conn.execute("PRAGMA foreign_keys=OFF")  # 外部キーでつながった行を保ったまま入れ替える
    try:
        with Tx(conn):
            conn.execute(TABLES[name].format(name=f"{name}_new"))
            new = {r["name"] for r in conn.execute(f"PRAGMA table_info({name}_new)")}
            cols = ", ".join(c for c in old if c in new)
            conn.execute(f"INSERT INTO {name}_new ({cols}) SELECT {cols} FROM {name}")
            conn.execute(f"DROP TABLE {name}")
            conn.execute(f"ALTER TABLE {name}_new RENAME TO {name}")
            for sql in TABLE_INDEXES[name]:
                conn.execute(sql)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


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
