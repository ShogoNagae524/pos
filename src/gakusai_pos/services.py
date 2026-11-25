"""業務ロジック（商品・注文・集計・削除・CSV）。HTTPやWebSocketには依存しない。"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta

from .db import Tx, get_setting, log_event, now_str, set_setting, today_str

TICKET = 50  # 学内通貨1枚の金額

STATUS_LABEL = {
    "waiting": "準備中",
    "cooking": "準備中",  # 以前のDBの「調理中」。起動時に waiting へ移す
    "ready": "受渡可能",
    "delivered": "受渡済",
    "cancelled": "取消",
}
# 飲食・物販とも waiting を「準備中」と呼ぶ
GOODS_STATUS_LABEL = STATUS_LABEL

# 商品・注文の区分。物販は、準備が要らない商品だけなら会計した時点で受渡済にする
CATEGORY_LABEL = {"food": "飲食", "goods": "物販"}

# 区分 -> {(現在, 次) -> 種類}。前に進む操作と、1段階戻す操作（K-05）
TRANSITIONS = {
    # 飲食（PC2厨房）：準備中 → 受渡済
    "food": {
        ("waiting", "delivered"): "forward",
        ("delivered", "waiting"): "undo",
    },
    # 物販（PC4）：準備中 → 受渡可能 → 受渡済。PC5に準備中・受渡可能を表示する
    "goods": {
        ("waiting", "ready"): "forward",
        ("ready", "delivered"): "forward",
        ("ready", "waiting"): "undo",
        ("delivered", "ready"): "undo",
    },
}

STATUS_UPDATES = {
    ("waiting", "delivered"): "status = 'delivered', delivered_at = :now",
    ("delivered", "waiting"): "status = 'waiting', delivered_at = NULL",
    ("waiting", "ready"): "status = 'ready', ready_at = :now",
    ("ready", "delivered"): "status = 'delivered', delivered_at = :now",
    ("ready", "waiting"): "status = 'waiting', ready_at = NULL",
    ("delivered", "ready"): "status = 'ready', delivered_at = NULL",
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
        "category": row["category"],
        "needs_prep": bool(row["needs_prep"]),
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
    conn: sqlite3.Connection,
    name: str,
    price: int,
    stock: int | None,
    term_label: str,
    category: str = "food",
    needs_prep: bool = False,
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
            "INSERT INTO products "
            "(code, name, price, sort_order, sold_out, stock, category, needs_prep) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (code, name, price, sort_order, sold_out, stock, category, int(needs_prep)),
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
        if patch.category is not None:
            fields["category"] = patch.category
        if patch.needs_prep is not None:
            fields["needs_prep"] = int(patch.needs_prep)
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
        labels = GOODS_STATUS_LABEL if d["category"] == "goods" else STATUS_LABEL
        d["status_label"] = labels[d["status"]]
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


def _next_call_no(
    conn: sqlite3.Connection, business_date: str, is_test: int, category: str = "food"
) -> int:
    """飲食の呼出番号と物販の受付番号は別々に振る（物販で飲食の番号が飛ばないように）。"""
    start = int(get_setting(conn, "call_no_start", "1") or 1)
    row = conn.execute(
        "SELECT MAX(call_no) FROM orders WHERE business_date = ? AND is_test = ? AND category = ?",
        (business_date, is_test, category),
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
    category: str | None = None,
) -> tuple[dict, bool, list[dict]]:
    """会計を1トランザクションで保存する。

    戻り値は (注文, 新規作成したか, 在庫・売切が変わった商品)。
    同じ client_uuid の再送信には、既存の注文をそのまま返す（R-25）。
    category を指定すると、その区分の商品だけを受け付ける（PC1は飲食、PC4は物販）。
    飲食と物販は1つの注文に混ぜられない。物販の注文は、準備が要る商品（needs_prep）を
    含めば status のまま（POSからは準備中）、含まなければ会計時点で受渡済になる。
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
            if category is None:
                category = row["category"]
            if row["category"] != category:
                raise ServiceError(
                    409,
                    f"「{row['name']}」は{CATEGORY_LABEL[row['category']]}の商品です"
                    "（飲食はPC1受付、物販はPC4物販で会計してください）",
                    product_id=pid,
                )
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

        # 価格がマイナスの商品は割引券。割引券だけの会計と、合計がマイナスの会計は受けない
        if not any(row["price"] > 0 for row, _ in lines):
            raise ServiceError(409, "割引券だけでは会計できません。商品を入れてください")
        total = sum(row["price"] * qty for row, qty in lines)
        if total < 0:
            raise ServiceError(
                409, "割引が合計金額を超えています（おつりは出せません）。割引券を減らしてください"
            )
        if is_test is None:
            is_test = get_setting(conn, "test_mode") == "1"
        is_test = int(is_test)
        business_date = business_date or today_str()
        if category == "goods" and not any(row["needs_prep"] for row, _ in lines):
            status = "delivered"  # 準備が要らないので、その場で渡す
        if call_no is None:
            call_no = _next_call_no(conn, business_date, is_test, category)
        paid_at = paid_at or now_str()
        cur = conn.execute(
            "INSERT INTO orders (client_uuid, business_date, call_no, total, status, source, "
            "paid_at, delivered_at, is_test, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                category,
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
        "category": order["category"],
        "items": [f"{i['name']}×{i['qty']}" for i in order["items"]],
    }


def change_status(
    conn: sqlite3.Connection,
    order_id: int,
    current: str,
    to: str,
    term_label: str,
    tid: str,
    category: str = "food",
) -> dict:
    """状態変更。今の状態が送信側の認識と同じときだけ反映する（第9章）。

    category は操作する端末の区分（PC2厨房は food、PC4物販は goods）。
    """
    labels = GOODS_STATUS_LABEL if category == "goods" else STATUS_LABEL
    kind = TRANSITIONS[category].get((current, to))
    if kind is None:
        raise ServiceError(400, f"{labels[current]}から{labels[to]}には変更できません")
    now = now_str()
    with Tx(conn):
        order = get_order(conn, order_id)  # 404 ならここで送出
        if order["category"] != category:
            raise ServiceError(
                403, f"{CATEGORY_LABEL[order['category']]}の注文はこの端末では変更できません"
            )
        # 準備の工程を通らなかった物販の注文（その場で渡したもの）は受渡可能に戻せない
        extra = " AND ready_at IS NOT NULL" if (current, to) == ("delivered", "ready") else ""
        cur = conn.execute(
            f"UPDATE orders SET {STATUS_UPDATES[(current, to)]} "
            f"WHERE id = :id AND status = :current{extra}",
            {"now": now, "id": order_id, "current": current},
        )
        if cur.rowcount == 0:
            order = get_order(conn, order_id)
            no_label = "受付番号" if category == "goods" else "呼出番号"
            msg = (
                f"{no_label}{order['call_no']}は準備の工程がない注文です"
                if order["status"] == current
                else f"{no_label}{order['call_no']}はすでに「{order['status_label']}」です"
            )
            raise ServiceError(409, msg, order=order)
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
    conn: sqlite3.Connection,
    order_id: int,
    current: str | None,
    term_label: str,
    tid: str,
    category: str | None = None,
) -> tuple[dict, list[dict]]:
    """取消。飲食は受渡前だけ、物販は準備中・受渡可能・受渡済（＝返品）を取り消せる。

    category を指定すると、その区分の注文だけを取り消せる。
    """
    touched: list[int] = []
    with Tx(conn):
        order = get_order(conn, order_id)
        goods = order["category"] == "goods"
        no_label = "受付番号" if goods else "呼出番号"
        if category is not None and order["category"] != category:
            raise ServiceError(
                403, f"{CATEGORY_LABEL[order['category']]}の注文はこの端末では取消できません"
            )
        cancellable = ("waiting", "ready", "delivered") if goods else ("waiting",)
        if order["status"] not in cancellable or (
            current is not None and current != order["status"]
        ):
            raise ServiceError(
                409,
                f"{no_label}{order['call_no']}は「{order['status_label']}」のため取消できません",
                order=order,
            )
        now = now_str()
        # 物販には厨房の「確認」がないので、取消と同時に確認済みにする
        conn.execute(
            "UPDATE orders SET status = 'cancelled', cancelled_at = ?, cancel_ack_at = ? "
            "WHERE id = ? AND status = ?",
            (now, now if goods else None, order_id, order["status"]),
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


def open_orders(conn: sqlite3.Connection, category: str = "food") -> list[dict]:
    """受渡前の注文（到着順）。

    飲食は厨房に出す準備中と未確認の取消。物販はPC4・PC5に出す準備中・受渡可能。
    """
    if category == "goods":
        where = "category = 'goods' AND status IN ('waiting', 'ready')"
    else:
        where = (
            "category = 'food' AND (status = 'waiting' "
            "OR (status = 'cancelled' AND cancel_ack_at IS NULL))"
        )
    rows = conn.execute(f"SELECT * FROM orders WHERE {where} ORDER BY id").fetchall()
    return _orders_with_items(conn, rows)


def recent_delivered(
    conn: sqlite3.Connection, limit: int = 10, category: str = "food"
) -> list[dict]:
    """最近受け渡した注文（押し間違いの戻し用）。物販は準備の工程を通ったものだけ。"""
    where = "category = 'food'"
    if category == "goods":
        where = "category = 'goods' AND ready_at IS NOT NULL"
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status = 'delivered' AND {where} "
        "ORDER BY delivered_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return _orders_with_items(conn, rows)


def orders_of_day(
    conn: sqlite3.Connection, date: str, limit: int = 200, category: str | None = None
) -> list[dict]:
    sql, args = "SELECT * FROM orders WHERE business_date = ?", [date]
    if category:
        sql += " AND category = ?"
        args.append(category)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return _orders_with_items(conn, rows)


def search_orders(
    conn: sqlite3.Connection,
    date: str | None = None,
    status: str | None = None,
    q: str | None = None,
    test: str = "all",
    limit: int = 500,
    category: str | None = None,
) -> list[dict]:
    where, args = [], []
    if date:
        where.append("o.business_date = ?")
        args.append(date)
    if category:
        where.append("o.category = ?")
        args.append(category)
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
            raise ServiceError(409, "受渡前の注文は削除できません", order=order)
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        log_event(
            conn, term_label, "order_deleted", order_id, {"id": order_id, **_order_summary(order)}
        )
    return order


# ================================================================ summary (A-08)


def summary(conn: sqlite3.Connection, date: str | None, category: str | None = None) -> dict:
    """売上集計。category（food / goods）を指定するとその区分だけを集計する。"""
    base, base_args = "1 = 1", []
    if category:
        base += " AND category = ?"
        base_args.append(category)
    date_cond, date_args = base, list(base_args)
    if date:
        date_cond += " AND business_date = ?"
        date_args.append(date)
    cond = f"status != 'cancelled' AND is_test = 0 AND {date_cond}"
    args = date_args
    tot = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS amount FROM orders WHERE {cond}",
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
            f"JOIN (SELECT id FROM orders WHERE {cond}) o ON o.id = i.order_id "
            f"GROUP BY i.product_id ORDER BY amount DESC",
            args,
        )
    ]
    hourly = [
        {"hour": int(r["h"]), "orders": r["n"], "amount": r["amount"]}
        for r in conn.execute(
            f"SELECT substr(paid_at, 12, 2) AS h, COUNT(*) AS n, SUM(total) AS amount "
            f"FROM orders WHERE {cond} GROUP BY h ORDER BY h",
            args,
        )
    ]
    # 待ち時間は厨房を通る飲食の注文だけで見る（物販は会計＝受渡なので0秒になる）
    wait = conn.execute(
        f"SELECT AVG((julianday(delivered_at) - julianday(paid_at)) * 86400) AS w, "
        f"COUNT(*) AS n FROM orders WHERE {cond} AND status = 'delivered' "
        f"AND source = 'pos' AND category = 'food'",
        args,
    ).fetchone()
    cancelled = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders "
        f"WHERE status = 'cancelled' AND is_test = 0 AND {date_cond}",
        date_args,
    ).fetchone()
    test = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE is_test = 1 AND {date_cond}", date_args
    ).fetchone()
    days = [
        {
            "business_date": r["d"],
            "orders": r["n"],
            "amount": r["amount"],
            "tickets": tickets(r["amount"]),
        }
        for r in conn.execute(
            f"SELECT business_date AS d, COUNT(*) AS n, SUM(total) AS amount FROM orders "
            f"WHERE status != 'cancelled' AND is_test = 0 AND {base} GROUP BY d ORDER BY d",
            base_args,
        )
    ]
    # 区分別（飲食・物販）。レジ締めで金券を別々に数えるとき用
    by_cat = {
        r["category"]: r
        for r in conn.execute(
            "SELECT category, COUNT(*) AS n, COALESCE(SUM(total), 0) AS amount FROM orders "
            "WHERE status != 'cancelled' AND is_test = 0"
            + (" AND business_date = ?" if date else "")
            + " GROUP BY category",
            [date] if date else [],
        )
    }
    categories = [
        {
            "category": c,
            "label": label,
            "orders": by_cat[c]["n"] if c in by_cat else 0,
            "amount": by_cat[c]["amount"] if c in by_cat else 0,
            "tickets": tickets(by_cat[c]["amount"]) if c in by_cat else 0,
        }
        for c, label in CATEGORY_LABEL.items()
    ]
    return {
        "business_date": date,
        "category": category,
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
        "categories": categories,
        "days": days,
        "dates": [
            r[0] for r in conn.execute("SELECT DISTINCT business_date FROM orders ORDER BY 1 DESC")
        ],
    }


# ================================================================ closing (A-09)


def record_closing(
    conn: sqlite3.Connection,
    date: str,
    counted: int,
    note: str,
    term_label: str,
    category: str | None = None,
) -> dict:
    """レジ締め。category を指定すると、その区分（飲食・物販）の金券だけで照合する。"""
    s = summary(conn, date, category)
    result = {
        "business_date": date,
        "category": category,
        "expected_tickets": s["tickets"],
        "expected_amount": s["amount"],
        "counted_tickets": counted,
        "diff_tickets": counted - s["tickets"],
        "diff_amount": (counted - s["tickets"]) * TICKET,
        "open_orders": conn.execute(
            "SELECT COUNT(*) FROM orders WHERE business_date = ? "
            "AND status IN ('waiting', 'cooking', 'ready') AND is_test = 0",
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
            "ready_at",
            "delivered_at",
            "cancelled_at",
            "is_test",
            "client_uuid",
            "category",
        ]
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM orders ORDER BY id").fetchall()
        return _csv(cols + ["tickets"], [(*r, tickets(r["total"])) for r in rows])
    if kind == "items":
        rows = conn.execute(
            "SELECT i.id, i.order_id, o.business_date, o.call_no, o.status, o.is_test, "
            "o.category, i.product_id, i.name, i.unit_price, i.qty, i.unit_price * i.qty "
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
                "category",
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


# ---------------------------------------------------------------- 売上グラフ

BUCKET_MINUTES = 30
MAX_SERIES_BUCKETS = 14 * 24 * 2  # 14日分（30分ごと）まで


def _floor_bucket(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % BUCKET_MINUTES, second=0, microsecond=0)


def sales_series(
    conn: sqlite3.Connection,
    since: str,
    until: str,
    category: str | None = None,
    trim: bool = False,
) -> dict:
    """since 以上 until 未満の会計時刻の売上を30分ごとに集計する（売上グラフ用）。

    取消・テスト注文は除く（売上集計と同じ）。cumulative は期間の始めからの累計。
    trim=True なら日ごとに、最初の売上の前と最後の売上の後を切り落とす（営業日をまたいでも夜中を出さない）。
    """
    try:
        start = datetime.fromisoformat(since)
        end = datetime.fromisoformat(until)
    except ValueError as e:
        raise ServiceError(400, "期間の日時の形式が正しくありません") from e
    if start >= end:
        raise ServiceError(400, "期間の「から」は「まで」より前にしてください")
    start = _floor_bucket(start)
    if (end - start) > timedelta(minutes=BUCKET_MINUTES * MAX_SERIES_BUCKETS):
        raise ServiceError(400, "期間は14日以内にしてください")

    cond = "status != 'cancelled' AND is_test = 0 AND paid_at >= ? AND paid_at < ?"
    args: list = [start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")]
    if category:
        cond += " AND category = ?"
        args.append(category)
    step = timedelta(minutes=BUCKET_MINUTES)
    sums: dict[datetime, list[int]] = {}
    for r in conn.execute(f"SELECT paid_at, total FROM orders WHERE {cond}", args):
        b = _floor_bucket(datetime.fromisoformat(r["paid_at"]))
        acc = sums.setdefault(b, [0, 0])
        acc[0] += 1
        acc[1] += r["total"]

    # 出す30分の並び。trim なら日ごとに、最初と最後の売上の間だけにする（夜中は出さない）
    if not trim:
        spans = [(start, end)]
    else:
        by_day: dict = {}
        for b in sums:
            lo, hi = by_day.get(b.date(), (b, b))
            by_day[b.date()] = (min(lo, b), max(hi, b))
        spans = [(lo, hi + step) for _, (lo, hi) in sorted(by_day.items())]

    buckets = []
    cumulative = 0
    for first, last in spans:
        t = first
        while t < last:
            n, amount = sums.get(t, (0, 0))
            cumulative += amount
            e = min(t + step, end)
            buckets.append(
                {
                    "start": t.isoformat(timespec="minutes"),
                    "end": e.isoformat(timespec="minutes"),
                    "orders": n,
                    "amount": amount,
                    "tickets": tickets(amount),
                    "cumulative": cumulative,
                }
            )
            t += step
    total_orders = sum(b["orders"] for b in buckets)
    return {
        "since": start.isoformat(timespec="minutes"),
        "until": end.isoformat(timespec="minutes"),
        "bucket_minutes": BUCKET_MINUTES,
        "buckets": buckets,
        "orders": total_orders,
        "amount": cumulative,
        "tickets": tickets(cumulative),
    }
