"""業務ロジック（商品・注文・集計・削除・CSV）。HTTPやWebSocketには依存しない。"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime

from .db import Tx, get_setting, log_event, now_str, set_setting, today_str

TICKET = 50  # 学内通貨1枚の金額

STATUS_LABEL = {
    "waiting": "未着手",
    "cooking": "調理中",
    "delivered": "受渡済",
    "cancelled": "取消",
}

# (現在, 次) -> 種類。前に進む操作と、1段階戻す操作（K-05）
TRANSITIONS = {
    ("waiting", "cooking"): "forward",
    ("cooking", "delivered"): "forward",
    ("cooking", "waiting"): "undo",
    ("delivered", "cooking"): "undo",
}


class ServiceError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def tickets(amount: int) -> int:
    return amount // TICKET


def _epoch_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


# ================================================================ products


def product_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "price": row["price"],
        "sort_order": row["sort_order"],
        "sold_out": bool(row["sold_out"]),
        "deleted": row["deleted_at"] is not None,
        "stock": row["stock"],
    }


def list_products(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM products WHERE deleted_at IS NULL ORDER BY sort_order, id"
    ).fetchall()
    return [product_dict(r) for r in rows]


def get_product(conn: sqlite3.Connection, product_id: int) -> dict:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None or row["deleted_at"] is not None:
        raise ServiceError(404, "商品が見つかりません")
    return product_dict(row)


def add_product(
    conn: sqlite3.Connection, name: str, price: int, stock: int | None, term_label: str
) -> dict:
    with Tx(conn):
        no = int(get_setting(conn, "next_product_no", "1"))
        # 削除済みも含めて既存のコードと重ならない番号を使う（再利用しない）
        while conn.execute("SELECT 1 FROM products WHERE code = ?", (f"M{no:03d}",)).fetchone():
            no += 1
        code = f"M{no:03d}"
        set_setting(conn, "next_product_no", str(no + 1))
        sort_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM products WHERE deleted_at IS NULL"
        ).fetchone()[0]
        sold_out = 1 if stock == 0 else 0
        cur = conn.execute(
            "INSERT INTO products (code, name, price, sort_order, sold_out, stock) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, name, price, sort_order, sold_out, stock),
        )
        product = product_dict(
            conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone()
        )
        log_event(conn, term_label, "product_added", product["id"], product)
    return product


def update_product(conn: sqlite3.Connection, product_id: int, patch, term_label: str) -> dict:
    with Tx(conn):
        before = get_product(conn, product_id)
        fields: dict = {}
        if patch.name is not None:
            fields["name"] = patch.name
        if patch.price is not None:
            fields["price"] = patch.price
        if patch.sort_order is not None:
            fields["sort_order"] = patch.sort_order
        if patch.sold_out is not None:
            fields["sold_out"] = int(patch.sold_out)
        if patch.clear_stock:
            fields["stock"] = None
        elif patch.stock is not None:
            fields["stock"] = patch.stock
            if patch.stock == 0:
                fields["sold_out"] = 1
            elif patch.sold_out is None and before["stock"] == 0:
                fields["sold_out"] = 0  # 在庫を補充したら売切を解除
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE products SET {sets} WHERE id = ?", (*fields.values(), product_id))
        after = get_product(conn, product_id)
        changes = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
        if changes:
            log_event(
                conn,
                term_label,
                "product_updated",
                product_id,
                {"code": after["code"], "name": after["name"], "changes": changes},
            )
    return after


def set_sold_out(
    conn: sqlite3.Connection, product_id: int, sold_out: bool, term_label: str, tid: str
) -> dict:
    with Tx(conn):
        get_product(conn, product_id)
        conn.execute("UPDATE products SET sold_out = ? WHERE id = ?", (int(sold_out), product_id))
        product = get_product(conn, product_id)
        log_event(
            conn,
            term_label,
            "sold_out",
            product_id,
            {"name": product["name"], "sold_out": sold_out},
            tid,
        )
    return product


def delete_product(conn: sqlite3.Connection, product_id: int, term_label: str) -> dict:
    """注文に使われていれば削除済みの印、一度も売れていなければ完全に削除する。"""
    with Tx(conn):
        product = get_product(conn, product_id)
        used = conn.execute(
            "SELECT 1 FROM order_items WHERE product_id = ? LIMIT 1", (product_id,)
        ).fetchone()
        if used:
            conn.execute("UPDATE products SET deleted_at = ? WHERE id = ?", (now_str(), product_id))
        else:
            conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        product["deleted"] = True
        log_event(
            conn,
            term_label,
            "product_deleted",
            product_id,
            {"code": product["code"], "name": product["name"], "hard": not used},
        )
    return product


# ================================================================ orders


def _orders_with_items(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    items: dict[int, list] = {i: [] for i in ids}
    marks = ",".join("?" * len(ids))
    for it in conn.execute(
        f"SELECT * FROM order_items WHERE order_id IN ({marks}) ORDER BY id", ids
    ):
        items[it["order_id"]].append(
            {
                "product_id": it["product_id"],
                "name": it["name"],
                "unit_price": it["unit_price"],
                "qty": it["qty"],
            }
        )
    out = []
    for r in rows:
        d = dict(r)
        d["is_test"] = bool(d["is_test"])
        d["tickets"] = tickets(d["total"])
        d["status_label"] = STATUS_LABEL[d["status"]]
        d["paid_ts"] = _epoch_ms(d["paid_at"])
        d["items"] = items[r["id"]]
        if d["delivered_at"]:
            d["wait_seconds"] = int(
                (
                    datetime.fromisoformat(d["delivered_at"]) - datetime.fromisoformat(d["paid_at"])
                ).total_seconds()
            )
        else:
            d["wait_seconds"] = None
        out.append(d)
    return out


def get_order(conn: sqlite3.Connection, order_id: int) -> dict:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise ServiceError(404, "注文が見つかりません")
    return _orders_with_items(conn, [row])[0]


def _next_call_no(conn: sqlite3.Connection, business_date: str, is_test: int) -> int:
    start = int(get_setting(conn, "call_no_start", "1") or 1)
    row = conn.execute(
        "SELECT MAX(call_no) FROM orders WHERE business_date = ? AND is_test = ?",
        (business_date, is_test),
    ).fetchone()
    return start if row[0] is None else max(row[0] + 1, start)


def create_order(
    conn: sqlite3.Connection,
    items: list,
    term_label: str,
    tid: str | None = None,
    client_uuid: str | None = None,
    source: str = "pos",
    business_date: str | None = None,
    call_no: int | None = None,
    paid_at: str | None = None,
    status: str = "waiting",
    is_test: bool | None = None,
) -> tuple[dict, bool, list[dict]]:
    """会計を1トランザクションで保存する。

    戻り値は (注文, 新規作成したか, 在庫・売切が変わった商品)。
    同じ client_uuid の再送信には、既存の注文をそのまま返す（R-25）。
    """
    if client_uuid:
        row = conn.execute("SELECT id FROM orders WHERE client_uuid = ?", (client_uuid,)).fetchone()
        if row:
            return get_order(conn, row["id"]), False, []

    # 同じ商品の行はまとめる
    qty_by_product: dict[int, int] = {}
    for it in items:
        qty_by_product[it.product_id] = qty_by_product.get(it.product_id, 0) + it.qty

    touched: list[int] = []
    with Tx(conn):
        lines = []
        for pid, qty in qty_by_product.items():
            row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise ServiceError(409, "削除された商品が含まれています", product_id=pid)
            if source == "pos" and row["sold_out"]:
                raise ServiceError(409, f"「{row['name']}」は売切です", product_id=pid)
            if row["stock"] is not None:
                if source == "pos" and row["stock"] < qty:
                    raise ServiceError(
                        409, f"「{row['name']}」の残りは{row['stock']}個です", product_id=pid
                    )
                new_stock = max(row["stock"] - qty, 0)
                conn.execute(
                    "UPDATE products SET stock = ?, sold_out = CASE WHEN ? = 0 THEN 1 "
                    "ELSE sold_out END WHERE id = ?",
                    (new_stock, new_stock, pid),
                )
                touched.append(pid)
            lines.append((row, qty))

        total = sum(row["price"] * qty for row, qty in lines)
        if is_test is None:
            is_test = get_setting(conn, "test_mode") == "1"
        is_test = int(is_test)
        business_date = business_date or today_str()
        if call_no is None:
            call_no = _next_call_no(conn, business_date, is_test)
        paid_at = paid_at or now_str()
        cur = conn.execute(
            "INSERT INTO orders (client_uuid, business_date, call_no, total, status, source, "
            "paid_at, delivered_at, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                client_uuid,
                business_date,
                call_no,
                total,
                status,
                source,
                paid_at,
                paid_at if status == "delivered" else None,
                is_test,
            ),
        )
        order_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO order_items (order_id, product_id, name, unit_price, qty) "
            "VALUES (?, ?, ?, ?, ?)",
            [(order_id, row["id"], row["name"], row["price"], qty) for row, qty in lines],
        )
        order = get_order(conn, order_id)
        log_event(
            conn,
            term_label,
            "manual_order" if source == "manual" else "order_created",
            order_id,
            _order_summary(order),
            tid,
        )
    products = [get_product(conn, pid) for pid in touched]
    return order, True, products


def _order_summary(order: dict) -> dict:
    return {
        "call_no": order["call_no"],
        "business_date": order["business_date"],
        "total": order["total"],
        "status": order["status"],
        "is_test": order["is_test"],
        "items": [f"{i['name']}×{i['qty']}" for i in order["items"]],
    }


def change_status(
    conn: sqlite3.Connection, order_id: int, current: str, to: str, term_label: str, tid: str
) -> dict:
    """状態変更。今の状態が送信側の認識と同じときだけ反映する（第9章）。"""
    kind = TRANSITIONS.get((current, to))
    if kind is None:
        raise ServiceError(400, f"{STATUS_LABEL[current]}から{STATUS_LABEL[to]}には変更できません")
    now = now_str()
    with Tx(conn):
        updates = {
            ("waiting", "cooking"): "status = 'cooking', started_at = :now",
            ("cooking", "delivered"): "status = 'delivered', delivered_at = :now",
            ("cooking", "waiting"): "status = 'waiting', started_at = NULL",
            ("delivered", "cooking"): "status = 'cooking', delivered_at = NULL",
        }[(current, to)]
        cur = conn.execute(
            f"UPDATE orders SET {updates} WHERE id = :id AND status = :current",
            {"now": now, "id": order_id, "current": current},
        )
        if cur.rowcount == 0:
            order = get_order(conn, order_id)  # 404 ならここで送出
            raise ServiceError(
                409,
                f"呼出番号{order['call_no']}はすでに「{order['status_label']}」です",
                order=order,
            )
        order = get_order(conn, order_id)
        log_event(
            conn,
            term_label,
            "status_undone" if kind == "undo" else "status_changed",
            order_id,
            {"call_no": order["call_no"], "from": current, "to": to},
            tid,
        )
    return order


def cancel_order(
    conn: sqlite3.Connection, order_id: int, current: str | None, term_label: str, tid: str
) -> tuple[dict, list[dict]]:
    touched: list[int] = []
    with Tx(conn):
        order = get_order(conn, order_id)
        if order["status"] not in ("waiting", "cooking") or (
            current is not None and current != order["status"]
        ):
            raise ServiceError(
                409,
                f"呼出番号{order['call_no']}は「{order['status_label']}」のため取消できません",
                order=order,
            )
        conn.execute(
            "UPDATE orders SET status = 'cancelled', cancelled_at = ? WHERE id = ? AND status = ?",
            (now_str(), order_id, order["status"]),
        )
        # 在庫上限を使っている商品は数を戻す
        for it in order["items"]:
            cur = conn.execute(
                "UPDATE products SET stock = stock + ? "
                "WHERE id = ? AND stock IS NOT NULL AND deleted_at IS NULL",
                (it["qty"], it["product_id"]),
            )
            if cur.rowcount:
                touched.append(it["product_id"])
        order = get_order(conn, order_id)
        log_event(conn, term_label, "order_cancelled", order_id, _order_summary(order), tid)
    return order, [get_product(conn, pid) for pid in touched]


def ack_cancel(conn: sqlite3.Connection, order_id: int, term_label: str, tid: str) -> dict:
    with Tx(conn):
        order = get_order(conn, order_id)
        if order["status"] != "cancelled":
            raise ServiceError(409, "取消された注文ではありません", order=order)
        if order["cancel_ack_at"] is None:
            conn.execute("UPDATE orders SET cancel_ack_at = ? WHERE id = ?", (now_str(), order_id))
            log_event(conn, term_label, "cancel_ack", order_id, {"call_no": order["call_no"]}, tid)
        order = get_order(conn, order_id)
    return order


def open_orders(conn: sqlite3.Connection) -> list[dict]:
    """受渡前の注文と、厨房がまだ確認していない取消注文（到着順）。"""
    rows = conn.execute(
        "SELECT * FROM orders WHERE status IN ('waiting', 'cooking') "
        "OR (status = 'cancelled' AND cancel_ack_at IS NULL) ORDER BY id"
    ).fetchall()
    return _orders_with_items(conn, rows)


def recent_delivered(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM orders WHERE status = 'delivered' ORDER BY delivered_at DESC, id DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    return _orders_with_items(conn, rows)


def orders_of_day(conn: sqlite3.Connection, date: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM orders WHERE business_date = ? ORDER BY id DESC LIMIT ?", (date, limit)
    ).fetchall()
    return _orders_with_items(conn, rows)


def search_orders(
    conn: sqlite3.Connection,
    date: str | None = None,
    status: str | None = None,
    q: str | None = None,
    test: str = "all",
    limit: int = 500,
) -> list[dict]:
    where, args = [], []
    if date:
        where.append("o.business_date = ?")
        args.append(date)
    if status:
        where.append("o.status = ?")
        args.append(status)
    if test == "test":
        where.append("o.is_test = 1")
    elif test == "real":
        where.append("o.is_test = 0")
    if q:
        q = q.strip()
        if q.isdigit():
            where.append("o.call_no = ?")
            args.append(int(q))
        else:
            where.append(
                "EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id AND i.name LIKE ?)"
            )
            args.append(f"%{q}%")
    sql = "SELECT o.* FROM orders o"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.id DESC LIMIT ?"
    rows = conn.execute(sql, (*args, limit)).fetchall()
    return _orders_with_items(conn, rows)


# ================================================================ delete (A-14)


def _purge_where(p) -> tuple[str, list]:
    marks = ",".join("?" * len(p.statuses))
    where = [f"status IN ({marks})"]
    args: list = list(p.statuses)
    if p.business_date:
        where.append("business_date = ?")
        args.append(p.business_date)
    if p.test == "test":
        where.append("is_test = 1")
    elif p.test == "real":
        where.append("is_test = 0")
    return " AND ".join(where), args


def purge_preview(conn: sqlite3.Connection, p) -> dict:
    where, args = _purge_where(p)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS amount FROM orders WHERE {where}",
        args,
    ).fetchone()
    return {"count": row["n"], "amount": row["amount"], "tickets": tickets(row["amount"])}


def purge_orders(conn: sqlite3.Connection, p, term_label: str) -> dict:
    with Tx(conn):
        where, args = _purge_where(p)
        rows = conn.execute(f"SELECT * FROM orders WHERE {where} ORDER BY id", args).fetchall()
        orders = _orders_with_items(conn, rows)
        if orders:
            ids = [o["id"] for o in orders]
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM orders WHERE id IN ({marks})", ids)
        amount = sum(o["total"] for o in orders)
        log_event(
            conn,
            term_label,
            "orders_purged",
            None,
            {
                "condition": p.model_dump(),
                "count": len(orders),
                "amount": amount,
                "orders": [{"id": o["id"], **_order_summary(o)} for o in orders],
            },
        )
    return {"count": len(orders), "amount": amount, "ids": [o["id"] for o in orders]}


def delete_order(conn: sqlite3.Connection, order_id: int, term_label: str) -> dict:
    with Tx(conn):
        order = get_order(conn, order_id)
        if order["status"] not in ("delivered", "cancelled"):
            raise ServiceError(409, "未着手・調理中の注文は削除できません", order=order)
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        log_event(
            conn, term_label, "order_deleted", order_id, {"id": order_id, **_order_summary(order)}
        )
    return order


# ================================================================ summary (A-08)


def summary(conn: sqlite3.Connection, date: str | None) -> dict:
    cond, args = "o.status != 'cancelled' AND o.is_test = 0", []
    if date:
        cond += " AND o.business_date = ?"
        args.append(date)
    tot = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS amount FROM orders o WHERE {cond}",
        args,
    ).fetchone()
    products = [
        {
            "product_id": r["product_id"],
            "name": r["name"],
            "qty": r["qty"],
            "amount": r["amount"],
            "tickets": tickets(r["amount"]),
        }
        for r in conn.execute(
            f"SELECT i.product_id, MAX(i.name) AS name, SUM(i.qty) AS qty, "
            f"SUM(i.qty * i.unit_price) AS amount FROM order_items i "
            f"JOIN orders o ON o.id = i.order_id WHERE {cond} "
            f"GROUP BY i.product_id ORDER BY amount DESC",
            args,
        )
    ]
    hourly = [
        {"hour": int(r["h"]), "orders": r["n"], "amount": r["amount"]}
        for r in conn.execute(
            f"SELECT substr(o.paid_at, 12, 2) AS h, COUNT(*) AS n, SUM(o.total) AS amount "
            f"FROM orders o WHERE {cond} GROUP BY h ORDER BY h",
            args,
        )
    ]
    wait = conn.execute(
        f"SELECT AVG((julianday(delivered_at) - julianday(paid_at)) * 86400) AS w, "
        f"COUNT(*) AS n FROM orders o WHERE {cond} AND o.status = 'delivered' "
        f"AND o.source = 'pos'",
        args,
    ).fetchone()
    date_cond, date_args = ("AND business_date = ?", [date]) if date else ("", [])
    cancelled = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders "
        f"WHERE status = 'cancelled' AND is_test = 0 {date_cond}",
        date_args,
    ).fetchone()
    test = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE is_test = 1 {date_cond}", date_args
    ).fetchone()
    days = [
        {
            "business_date": r["d"],
            "orders": r["n"],
            "amount": r["amount"],
            "tickets": tickets(r["amount"]),
        }
        for r in conn.execute(
            "SELECT business_date AS d, COUNT(*) AS n, SUM(total) AS amount FROM orders "
            "WHERE status != 'cancelled' AND is_test = 0 GROUP BY d ORDER BY d"
        )
    ]
    return {
        "business_date": date,
        "orders": tot["n"],
        "amount": tot["amount"],
        "tickets": tickets(tot["amount"]),
        "products": products,
        "hourly": hourly,
        "avg_wait_seconds": round(wait["w"]) if wait["w"] is not None else None,
        "delivered_count": wait["n"],
        "cancelled_count": cancelled[0],
        "cancelled_amount": cancelled[1],
        "test_count": test[0],
        "days": days,
        "dates": [
            r[0] for r in conn.execute("SELECT DISTINCT business_date FROM orders ORDER BY 1 DESC")
        ],
    }


# ================================================================ closing (A-09)


def record_closing(
    conn: sqlite3.Connection, date: str, counted: int, note: str, term_label: str
) -> dict:
    s = summary(conn, date)
    result = {
        "business_date": date,
        "expected_tickets": s["tickets"],
        "expected_amount": s["amount"],
        "counted_tickets": counted,
        "diff_tickets": counted - s["tickets"],
        "diff_amount": (counted - s["tickets"]) * TICKET,
        "open_orders": conn.execute(
            "SELECT COUNT(*) FROM orders WHERE business_date = ? "
            "AND status IN ('waiting', 'cooking') AND is_test = 0",
            (date,),
        ).fetchone()[0],
        "note": note,
    }
    with Tx(conn):
        log_event(conn, term_label, "closing", None, result)
    return result


def closings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT at, content FROM events WHERE kind = 'closing' ORDER BY id DESC"
    ).fetchall()
    return [{"at": r["at"], **json.loads(r["content"])} for r in rows]


# ================================================================ events (A-06)


def search_events(
    conn: sqlite3.Connection,
    terminal: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    after_id: int | None = None,
    limit: int = 300,
) -> list[dict]:
    where, args = [], []
    if terminal:
        where.append("terminal = ?")
        args.append(terminal)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if since:
        where.append("at >= ?")
        args.append(since)
    if until:
        where.append("at <= ?")
        args.append(until)
    if after_id:
        where.append("id > ?")
        args.append(after_id)
    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = conn.execute(sql, (*args, limit)).fetchall()
    return [{**dict(r), "content": json.loads(r["content"])} for r in rows]


# ================================================================ CSV (A-10)


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return "﻿" + buf.getvalue()  # Excelで文字化けしないようBOM付き


def export_csv(conn: sqlite3.Connection, kind: str) -> str:
    if kind == "orders":
        cols = [
            "id",
            "business_date",
            "call_no",
            "total",
            "status",
            "source",
            "paid_at",
            "started_at",
            "delivered_at",
            "cancelled_at",
            "is_test",
            "client_uuid",
        ]
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM orders ORDER BY id").fetchall()
        return _csv(cols + ["tickets"], [(*r, tickets(r["total"])) for r in rows])
    if kind == "items":
        rows = conn.execute(
            "SELECT i.id, i.order_id, o.business_date, o.call_no, o.status, o.is_test, "
            "i.product_id, i.name, i.unit_price, i.qty, i.unit_price * i.qty "
            "FROM order_items i JOIN orders o ON o.id = i.order_id ORDER BY i.id"
        ).fetchall()
        return _csv(
            [
                "id",
                "order_id",
                "business_date",
                "call_no",
                "status",
                "is_test",
                "product_id",
                "name",
                "unit_price",
                "qty",
                "subtotal",
            ],
            rows,
        )
    if kind == "events":
        rows = conn.execute(
            "SELECT id, at, terminal, terminal_id, kind, target_id, content FROM events ORDER BY id"
        ).fetchall()
        return _csv(["id", "at", "terminal", "terminal_id", "kind", "target_id", "content"], rows)
    raise ServiceError(404, "不明なCSVの種類です")
