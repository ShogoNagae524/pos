import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from conftest import order
from gakusai_pos.db import today_str


def uid():
    return str(uuid.uuid4())


# ------------------------------------------------------------ 金額（50円単位）


def test_price_must_be_multiple_of_50(admin):
    for bad in (0, -120, 120, 99):
        r = admin.post("/admin/api/products", json={"name": "x", "price": bad})
        assert r.status_code == 422, bad
    assert admin.post("/admin/api/products", json={"name": "x", "price": 250}).status_code == 201
    # マイナスは割引券として登録できる
    assert admin.post("/admin/api/products", json={"name": "券", "price": -50}).status_code == 201


# ------------------------------------------------------------ 割引券（マイナス価格の商品）


@pytest.fixture
def coupon(admin):
    r = admin.post("/admin/api/products", json={"name": "100円引き券", "price": -100})
    assert r.status_code == 201, r.text
    return r.json()


def test_coupon_reduces_total(admin, reception, kitchen, menu, coupon):
    yaki = menu[0]["id"]
    o = order(reception, [(yaki, 1), (coupon["id"], 1)], uid()).json()
    assert o["total"] == 200 and o["tickets"] == 4
    # 価格を変えても過去の注文の値引き額は変わらない
    admin.patch(f"/admin/api/products/{coupon['id']}", json={"price": -50})
    lines = {i["name"]: i for i in kitchen.get("/api/orders?status=open").json()[0]["items"]}
    assert lines["100円引き券"]["unit_price"] == -100
    # 売上集計では割引券がマイナスの行として出る
    s = admin.get(f"/admin/api/summary?date={today_str()}").json()
    assert s["amount"] == 200
    by = {p["name"]: p for p in s["products"]}
    assert by["100円引き券"]["amount"] == -100


def test_coupon_can_make_total_zero(reception, menu, coupon):
    o = order(reception, [(menu[2]["id"], 1), (coupon["id"], 1)], uid())  # 100 - 100
    assert o.status_code == 201, o.text
    assert o.json()["total"] == 0 and o.json()["tickets"] == 0


def test_coupon_only_or_negative_total_rejected(reception, menu, coupon):
    r = order(reception, [(coupon["id"], 1)], uid())
    assert r.status_code == 409 and "割引券だけ" in r.json()["detail"]
    r = order(reception, [(menu[2]["id"], 1), (coupon["id"], 2)], uid())  # 100 - 200
    assert r.status_code == 409 and "おつり" in r.json()["detail"]
    assert reception.get("/api/orders?status=day").json() == []


def test_coupon_not_on_preorder_page(admin, menu, coupon):
    import re

    html = admin.get("/admin/preorder").text
    names = [m["name"] for m in json.loads(re.search(r"const MENU = (.*);", html).group(1))]
    assert names == ["やきそば", "フランク", "ジュース"]
    assert "割引券をお持ちの方" in html


def test_migrates_old_price_constraints(tmp_path):
    """割引券（マイナス価格・0円の会計）を入れられない古い制約のDBを作り直す。"""
    import sqlite3

    from gakusai_pos.app import create_app
    from gakusai_pos.db import TABLES

    data = tmp_path / "old"
    data.mkdir()
    conn = sqlite3.connect(data / "pos.db")
    old = {
        "products": ("price <> 0 AND", "price > 0 AND"),
        "orders": ("total >= 0 AND", "total > 0 AND"),
        "order_items": ("unit_price <> 0 AND", "unit_price > 0 AND"),
    }
    for name, (new, prev) in old.items():
        conn.execute(TABLES[name].format(name=name).replace(new, prev))
    conn.execute("INSERT INTO products (code, name, price) VALUES ('M001', 'やきそば', 300)")
    conn.execute(
        "INSERT INTO orders (business_date, call_no, total, status, paid_at) "
        "VALUES ('2026-11-01', 1, 300, 'delivered', '2026-11-01 10:00:00')"
    )
    conn.execute(
        "INSERT INTO order_items (order_id, product_id, name, unit_price, qty) "
        "VALUES (1, 1, 'やきそば', 300, 1)"
    )
    conn.commit()
    conn.close()
    app = create_app(data, background=False)
    try:
        db = app.state.db
        for name in old:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
            assert old[name][1] not in sql, name
        assert db.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute("INSERT INTO products (code, name, price) VALUES ('M002', '券', -50)")
    finally:
        app.state.db.close()


def test_price_update_validated(admin, menu):
    pid = menu[0]["id"]
    assert admin.patch(f"/admin/api/products/{pid}", json={"price": 330}).status_code == 422
    assert admin.patch(f"/admin/api/products/{pid}", json={"price": 350}).json()["price"] == 350


def test_order_total_and_tickets(reception, menu):
    r = order(reception, [(menu[0]["id"], 1), (menu[1]["id"], 2), (menu[0]["id"], 1)])
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["total"] == 300 * 2 + 150 * 2
    assert o["tickets"] == 18
    assert o["status"] == "waiting"
    assert o["call_no"] == 1
    assert {(i["name"], i["qty"]) for i in o["items"]} == {("やきそば", 2), ("フランク", 2)}


def test_price_change_does_not_change_past_orders(admin, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    admin.patch(f"/admin/api/products/{menu[0]['id']}", json={"price": 500})
    again = admin.get("/admin/api/orders").json()
    assert again[0]["id"] == o["id"]
    assert again[0]["total"] == 300
    assert again[0]["items"][0]["unit_price"] == 300


def test_product_codes_auto_and_not_reused(admin):
    a = admin.post("/admin/api/products", json={"name": "a", "price": 50}).json()
    b = admin.post("/admin/api/products", json={"name": "b", "price": 50}).json()
    assert (a["code"], b["code"]) == ("M001", "M002")
    assert b["sort_order"] > a["sort_order"]
    admin.delete(f"/admin/api/products/{b['id']}")
    c = admin.post("/admin/api/products", json={"name": "c", "price": 50}).json()
    assert c["code"] == "M003"


def test_starts_with_no_products(admin):
    assert admin.get("/api/products").json() == []


# ------------------------------------------------------------ 呼出番号


def test_call_numbers_sequential_from_start(admin, reception, menu):
    admin.put("/admin/api/settings", json={"call_no_start": 101})
    nos = [order(reception, [(menu[2]["id"], 1)], uid()).json()["call_no"] for _ in range(3)]
    assert nos == [101, 102, 103]


def test_call_number_resets_per_business_date(app, admin, reception, menu):
    order(reception, [(menu[2]["id"], 1)], uid())
    app.state.db.execute("UPDATE orders SET business_date = '2000-01-01'")
    assert order(reception, [(menu[2]["id"], 1)], uid()).json()["call_no"] == 1


# ------------------------------------------------------------ 二重送信の防止


def test_duplicate_submission_is_one_order(reception, kitchen, menu):
    u = uid()
    r1 = order(reception, [(menu[0]["id"], 1)], u)
    r2 = order(reception, [(menu[0]["id"], 1)], u)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert len(kitchen.get("/api/orders?status=open").json()) == 1


# ------------------------------------------------------------ 状態遷移


def _status(kitchen, oid, current, to):
    return kitchen.post(f"/api/orders/{oid}/status", json={"current": current, "to": to})


def test_normal_flow(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    assert o["status"] == "waiting" and o["status_label"] == "準備中"
    d = _status(kitchen, o["id"], "waiting", "delivered").json()
    assert d["status"] == "delivered" and d["delivered_at"]
    assert kitchen.get("/api/orders?status=open").json() == []
    assert kitchen.get("/api/orders?status=delivered").json()[0]["id"] == o["id"]


def test_invalid_transitions_rejected(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    # 「調理中」はなくなった
    assert _status(kitchen, o["id"], "waiting", "cooking").status_code == 400
    assert _status(kitchen, o["id"], "waiting", "ready").status_code == 400
    assert _status(kitchen, o["id"], "waiting", "cancelled").status_code == 400
    assert _status(kitchen, o["id"], "delivered", "waiting").status_code == 409


def test_stale_state_is_rejected(kitchen, reception, menu):
    """厨房で「受渡完了」の瞬間に受付で取消されていたら拒否して最新の状態を返す。"""
    o = order(reception, [(menu[0]["id"], 1)]).json()
    assert (
        reception.post(f"/api/orders/{o['id']}/cancel", json={"current": "waiting"}).status_code
        == 200
    )
    r = _status(kitchen, o["id"], "waiting", "delivered")
    assert r.status_code == 409
    assert r.json()["order"]["status"] == "cancelled"


def test_undo_one_step(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    _status(kitchen, o["id"], "waiting", "delivered")
    back = _status(kitchen, o["id"], "delivered", "waiting").json()
    assert back["status"] == "waiting" and back["delivered_at"] is None
    assert [x["id"] for x in kitchen.get("/api/orders?status=open").json()] == [o["id"]]


def test_cancel_shows_until_ack(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    reception.post(f"/api/orders/{o['id']}/cancel", json={"current": "waiting"})
    open_ = kitchen.get("/api/orders?status=open").json()
    assert [x["status"] for x in open_] == ["cancelled"]
    kitchen.post(f"/api/orders/{o['id']}/ack")
    assert kitchen.get("/api/orders?status=open").json() == []


def test_cannot_cancel_delivered(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    _status(kitchen, o["id"], "waiting", "delivered")
    assert reception.post(f"/api/orders/{o['id']}/cancel", json={}).status_code == 409


def test_fifo_order(kitchen, reception, menu):
    ids = [order(reception, [(menu[2]["id"], 1)], uid()).json()["id"] for _ in range(3)]
    _status(kitchen, ids[0], "waiting", "delivered")
    _status(kitchen, ids[0], "delivered", "waiting")
    assert [o["id"] for o in kitchen.get("/api/orders?status=open").json()] == ids


# ------------------------------------------------------------ 売切・在庫


def test_sold_out_rejects_order(kitchen, reception, menu):
    pid = menu[0]["id"]
    assert kitchen.post(f"/api/products/{pid}/sold-out", json={"sold_out": True}).json()["sold_out"]
    r = order(reception, [(pid, 1)])
    assert r.status_code == 409
    assert "売切" in r.json()["detail"]


def test_stock_limit_auto_sold_out(admin, reception, menu):
    pid = menu[0]["id"]
    admin.patch(f"/admin/api/products/{pid}", json={"stock": 3})
    assert order(reception, [(pid, 4)], uid()).status_code == 409
    o = order(reception, [(pid, 3)], uid()).json()
    p = next(x for x in admin.get("/api/products").json() if x["id"] == pid)
    assert p["stock"] == 0 and p["sold_out"]
    reception.post(f"/api/orders/{o['id']}/cancel", json={})
    p = next(x for x in admin.get("/api/products").json() if x["id"] == pid)
    assert p["stock"] == 3


def test_delete_product_soft_when_used(admin, reception, menu):
    used, unused = menu[0]["id"], menu[1]["id"]
    order(reception, [(used, 1)])
    admin.delete(f"/admin/api/products/{used}")
    admin.delete(f"/admin/api/products/{unused}")
    ids = [p["id"] for p in admin.get("/api/products").json()]
    assert used not in ids and unused not in ids
    db = admin.app.state.db
    assert db.execute("SELECT deleted_at FROM products WHERE id = ?", (used,)).fetchone()[0]
    assert db.execute("SELECT 1 FROM products WHERE id = ?", (unused,)).fetchone() is None


# ------------------------------------------------------------ 認証・PC3限定


def test_unauthenticated_access_denied(stranger, menu):
    assert stranger.get("/api/products").status_code == 401
    assert order(stranger, [(menu[0]["id"], 1)]).status_code == 401
    r = stranger.get("/reception", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_wrong_passcode(app, stranger):
    r = stranger.post("/api/session", json={"passcode": "wrong!", "role": "reception"})
    assert r.status_code == 401


def test_admin_api_rejected_from_other_hosts(reception, kitchen, stranger):
    for c in (reception, kitchen, stranger):
        assert c.get("/admin/api/products").status_code == 403
        assert c.post("/admin/api/products", json={"name": "x", "price": 50}).status_code == 403
        assert c.get("/admin/api/summary").status_code == 403
        assert c.get("/admin/api/export/orders.csv").status_code == 403
        assert c.get("/admin").status_code == 403


def test_roles_are_enforced(reception, kitchen, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    assert order(kitchen, [(menu[0]["id"], 1)], uid()).status_code == 403
    assert _status(reception, o["id"], "waiting", "delivered").status_code == 403
    assert kitchen.post(f"/api/orders/{o['id']}/cancel", json={}).status_code == 403


def test_passcode_change_invalidates_sessions(admin, reception):
    assert reception.get("/api/products").status_code == 200
    admin.put("/admin/api/settings", json={"passcode": "9876"})
    assert reception.get("/api/products").status_code == 401
    r = reception.post("/api/session", json={"passcode": "9876", "role": "reception"})
    assert r.status_code == 200
    assert reception.get("/api/products").status_code == 200


def test_passcode_stored_hashed(app):
    stored = app.state.db.execute(
        "SELECT value FROM settings WHERE key = 'passcode_hash'"
    ).fetchone()[0]
    assert app.state.initial_passcode not in stored
    assert stored.startswith("scrypt$")


# ------------------------------------------------------------ 売上集計・レジ締め


def test_summary_excludes_cancelled_and_test(admin, reception, kitchen, menu):
    yaki, frank = menu[0]["id"], menu[1]["id"]
    a = order(reception, [(yaki, 2)], uid()).json()  # 600
    order(reception, [(frank, 1)], uid())  # 150
    c = order(reception, [(yaki, 1)], uid()).json()  # 取消
    reception.post(f"/api/orders/{c['id']}/cancel", json={})
    admin.put("/admin/api/settings", json={"test_mode": True})
    t = order(reception, [(yaki, 5)], uid()).json()
    assert t["is_test"]
    admin.put("/admin/api/settings", json={"test_mode": False})
    _status(kitchen, a["id"], "waiting", "delivered")

    s = admin.get(f"/admin/api/summary?date={today_str()}").json()
    assert s["orders"] == 2
    assert s["amount"] == 750
    assert s["tickets"] == 15
    assert s["cancelled_count"] == 1
    assert s["test_count"] == 1
    by = {p["name"]: p for p in s["products"]}
    assert by["やきそば"]["qty"] == 2 and by["やきそば"]["amount"] == 600
    assert by["フランク"]["tickets"] == 3
    assert sum(h["orders"] for h in s["hourly"]) == 2
    assert s["avg_wait_seconds"] is not None

    r = admin.post(
        "/admin/api/closing", json={"business_date": today_str(), "counted_tickets": 14}
    ).json()
    assert r["expected_tickets"] == 15
    assert r["diff_tickets"] == -1
    assert admin.get("/admin/api/closings").json()[0]["counted_tickets"] == 14


def test_manual_order(admin, menu):
    r = admin.post(
        "/admin/api/orders",
        json={
            "items": [{"product_id": menu[0]["id"], "qty": 2}],
            "business_date": today_str(),
            "paid_time": "11:30",
            "call_no": 55,
        },
    )
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["source"] == "manual" and o["status"] == "delivered" and o["call_no"] == 55
    assert o["paid_at"].endswith("11:30:00")
    assert admin.get(f"/admin/api/summary?date={today_str()}").json()["amount"] == 600


# ------------------------------------------------------------ 注文データの削除（A-14）


def test_delete_orders(admin, reception, kitchen, menu):
    a = order(reception, [(menu[0]["id"], 1)], uid()).json()
    b = order(reception, [(menu[1]["id"], 1)], uid()).json()
    reception.post(f"/api/orders/{b['id']}/cancel", json={})
    # 準備中は削除できない
    assert admin.delete(f"/admin/api/orders/{a['id']}").status_code == 409
    pv = admin.post("/admin/api/orders/purge-preview", json={}).json()
    assert pv == {"count": 1, "amount": 150, "tickets": 3}
    assert admin.post("/admin/api/orders/purge", json={}).json()["count"] == 1
    ids = [o["id"] for o in admin.get("/admin/api/orders").json()]
    assert ids == [a["id"]]
    ev = admin.get("/admin/api/events?kind=orders_purged").json()[0]
    assert ev["content"]["orders"][0]["call_no"] == b["call_no"]


# ------------------------------------------------------------ ログ・CSV・設定・バックアップ


def test_events_logged_with_terminal(admin, reception, kitchen, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    _status(kitchen, o["id"], "waiting", "delivered")
    kinds = {(e["terminal"], e["kind"]) for e in admin.get("/admin/api/events").json()}
    assert ("PC1受付", "order_created") in kinds
    assert ("PC2厨房", "status_changed") in kinds
    assert ("PC3管理", "product_added") in kinds
    only = admin.get("/admin/api/events?terminal=PC2厨房").json()
    assert only and all(e["terminal"] == "PC2厨房" for e in only)


def test_csv_export(admin, reception, menu):
    order(reception, [(menu[0]["id"], 2)])
    for kind in ("orders", "items", "events"):
        r = admin.get(f"/admin/api/export/{kind}.csv")
        assert r.status_code == 200
        assert r.text.startswith("﻿")
    assert "やきそば" in admin.get("/admin/api/export/items.csv").text


def test_settings_roundtrip(admin, reception):
    s = admin.put(
        "/admin/api/settings",
        json={
            "shop_name": "焼きそば屋",
            "day1": "2026-11-01",
            "day2": "2026-11-02",
        },
    ).json()
    assert s["shop_name"] == "焼きそば屋" and s["day2"] == "2026-11-02"
    pub = reception.get("/api/settings").json()
    assert pub["shop_name"] == "焼きそば屋"
    assert "passcode_hash" not in pub and "session_secret" not in pub


def test_backup(admin, app, menu):
    r = admin.post("/admin/api/backup").json()
    import sqlite3
    from pathlib import Path

    assert Path(r["path"]).exists()
    conn = sqlite3.connect(r["path"])
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 3
    conn.close()


# ------------------------------------------------------------ 画面・WebSocket


def test_pages_render(admin, reception, kitchen, menu):
    assert "受付" in reception.get("/reception").text
    assert "厨房" in kitchen.get("/kitchen").text
    assert "管理" in admin.get("/admin").text
    assert admin.get("/admin/print").status_code == 404  # 商品のQRカードは廃止
    assert admin.get("/admin/api/qr.png?data=M001").headers["content-type"] == "image/png"


def test_websocket_notifies_new_order(kitchen, reception, menu):
    with kitchen.websocket_connect("wss://testserver/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        o = order(reception, [(menu[0]["id"], 1)]).json()
        msg = ws.receive_json()
        assert msg["type"] == "order_created"
        assert msg["order"]["id"] == o["id"]


def test_websocket_requires_auth(stranger):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), stranger.websocket_connect("/ws") as ws:
        ws.receive_json()


# ------------------------------------------------------------ 物販（PC4）


def test_goods_order_is_delivered_immediately(goods, kitchen, goods_menu):
    r = order(goods, [(goods_menu[0]["id"], 1), (goods_menu[1]["id"], 2)])
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["category"] == "goods"
    assert o["status"] == "delivered"
    assert o["delivered_at"] == o["paid_at"]
    assert o["total"] == 1700 and o["tickets"] == 34
    # 厨房には出ない
    assert kitchen.get("/api/orders?status=open").json() == []
    assert kitchen.get("/api/orders?status=delivered").json() == []
    assert _status(kitchen, o["id"], "delivered", "waiting").status_code == 403


def test_goods_and_food_terminals_take_only_their_products(reception, goods, menu, goods_menu):
    assert order(goods, [(menu[0]["id"], 1)], uid()).status_code == 409
    assert order(reception, [(goods_menu[0]["id"], 1)], uid()).status_code == 409
    # 1つの注文に混ぜられない
    assert order(goods, [(goods_menu[0]["id"], 1), (menu[0]["id"], 1)], uid()).status_code == 409


def test_goods_numbers_separate_from_food(reception, goods, menu, goods_menu):
    f1 = order(reception, [(menu[0]["id"], 1)], uid()).json()
    g1 = order(goods, [(goods_menu[1]["id"], 1)], uid()).json()
    f2 = order(reception, [(menu[0]["id"], 1)], uid()).json()
    g2 = order(goods, [(goods_menu[1]["id"], 1)], uid()).json()
    assert (f1["call_no"], f2["call_no"]) == (1, 2)
    assert (g1["call_no"], g2["call_no"]) == (1, 2)


def test_goods_history_and_return(admin, reception, goods, kitchen, menu, goods_menu):
    pid = goods_menu[0]["id"]
    admin.patch(f"/admin/api/products/{pid}", json={"stock": 2})
    order(reception, [(menu[0]["id"], 1)], uid())
    g = order(goods, [(pid, 2)], uid()).json()
    p = next(x for x in goods.get("/api/products").json() if x["id"] == pid)
    assert p["stock"] == 0 and p["sold_out"]
    # 履歴は自分の区分だけ
    assert [o["id"] for o in goods.get("/api/orders?status=day").json()] == [g["id"]]
    assert g["id"] not in [o["id"] for o in reception.get("/api/orders?status=day").json()]
    # PC1からは物販を取り消せない。PC4は受渡済を返品として取り消せる
    assert reception.post(f"/api/orders/{g['id']}/cancel", json={}).status_code == 403
    r = goods.post(f"/api/orders/{g['id']}/cancel", json={"current": "delivered"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled" and r.json()["cancel_ack_at"]
    p = next(x for x in goods.get("/api/products").json() if x["id"] == pid)
    assert p["stock"] == 2
    # 厨房に「取消」は出ない
    assert kitchen.get("/api/orders?status=open").json()[0]["category"] == "food"
    assert len(kitchen.get("/api/orders?status=open").json()) == 1


def test_kitchen_cannot_toggle_goods(kitchen, goods, goods_menu):
    pid = goods_menu[0]["id"]
    assert kitchen.post(f"/api/products/{pid}/sold-out", json={"sold_out": True}).status_code == 403
    # 物販に「調理中」はない
    assert _status(goods, 1, "waiting", "cooking").status_code == 400


def test_summary_and_closing_by_category(admin, reception, goods, kitchen, menu, goods_menu):
    o = order(reception, [(menu[0]["id"], 1)], uid()).json()  # 300円
    _status(kitchen, o["id"], "waiting", "delivered")
    order(goods, [(goods_menu[0]["id"], 1)], uid())  # 1500円
    s = admin.get("/admin/api/summary").json()
    assert s["amount"] == 1800
    assert {c["category"]: c["amount"] for c in s["categories"]} == {"food": 300, "goods": 1500}
    assert s["delivered_count"] == 1  # 待ち時間は飲食だけで見る
    assert admin.get("/admin/api/summary?category=goods").json()["amount"] == 1500
    assert admin.get("/admin/api/summary?category=food").json()["amount"] == 300
    r = admin.post(
        "/admin/api/closing",
        json={"business_date": today_str(), "counted_tickets": 30, "category": "goods"},
    ).json()
    assert r["expected_tickets"] == 30 and r["diff_tickets"] == 0 and r["category"] == "goods"


def test_goods_page_and_login(goods, stranger):
    page = goods.get("/goods")
    assert page.status_code == 200
    assert 'data-role="goods"' in page.text and "受付番号" in page.text
    r = stranger.get("/goods", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?role=goods"
    assert goods.get("/reception", follow_redirects=False).status_code == 303


def test_migrates_db_without_category(tmp_path):
    import sqlite3

    from gakusai_pos.app import create_app

    data = tmp_path / "old"
    data.mkdir()
    conn = sqlite3.connect(data / "pos.db")
    conn.executescript(
        "CREATE TABLE products (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, "
        "name TEXT NOT NULL, price INTEGER NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0, "
        "sold_out INTEGER NOT NULL DEFAULT 0, deleted_at TEXT, stock INTEGER);"
        "INSERT INTO products (code, name, price) VALUES ('M001', 'やきそば', 300);"
    )
    conn.close()
    app = create_app(data, background=False)
    try:
        row = app.state.db.execute("SELECT category FROM products").fetchone()
        assert row["category"] == "food"
    finally:
        app.state.db.close()


# ------------------------------------------------------------ 物販の準備・受渡（PC4・PC5）


def test_goods_with_prep_starts_waiting(goods, pickup, kitchen, goods_menu):
    printed = goods_menu[2]
    assert printed["needs_prep"] is True and goods_menu[0]["needs_prep"] is False
    o = order(goods, [(printed["id"], 1), (goods_menu[1]["id"], 1)]).json()
    assert o["status"] == "waiting" and o["status_label"] == "準備中"
    assert o["delivered_at"] is None
    # PC4・PC5の一覧には出るが、厨房には出ない
    assert [x["id"] for x in pickup.get("/api/orders?status=open").json()] == [o["id"]]
    assert [x["id"] for x in goods.get("/api/orders?status=open").json()] == [o["id"]]
    assert kitchen.get("/api/orders?status=open").json() == []


def test_goods_prep_flow_and_undo(goods, pickup, goods_menu):
    o = order(goods, [(goods_menu[2]["id"], 1)]).json()
    r = _status(goods, o["id"], "waiting", "ready")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ready" and r.json()["ready_at"]
    assert pickup.get("/api/orders?status=open").json()[0]["status_label"] == "受渡可能"
    assert _status(goods, o["id"], "ready", "waiting").json()["status"] == "waiting"
    _status(goods, o["id"], "waiting", "ready")
    d = _status(goods, o["id"], "ready", "delivered").json()
    assert d["status"] == "delivered" and d["wait_seconds"] is not None
    assert pickup.get("/api/orders?status=open").json() == []
    assert [x["id"] for x in goods.get("/api/orders?status=delivered").json()] == [o["id"]]
    # 受渡完了の押し間違いは戻せる
    assert _status(goods, o["id"], "delivered", "ready").json()["status"] == "ready"
    # 古い状態からの操作は拒否
    assert _status(goods, o["id"], "waiting", "ready").status_code == 409


def test_instant_goods_cannot_go_back_to_ready(goods, goods_menu):
    o = order(goods, [(goods_menu[0]["id"], 1)]).json()
    assert o["status"] == "delivered"
    assert _status(goods, o["id"], "delivered", "ready").status_code == 409
    assert goods.get("/api/orders?status=delivered").json() == []


def test_pickup_is_display_only(pickup, goods, kitchen, reception, menu, goods_menu):
    o = order(goods, [(goods_menu[2]["id"], 1)]).json()
    assert order(pickup, [(goods_menu[0]["id"], 1)], uid()).status_code == 403
    assert _status(pickup, o["id"], "waiting", "ready").status_code == 403
    assert pickup.post(f"/api/orders/{o['id']}/cancel", json={}).status_code == 403
    # 厨房は物販の準備状態を変えられない、PC4は飲食の状態を変えられない
    assert _status(kitchen, o["id"], "waiting", "delivered").status_code == 403
    f = order(reception, [(menu[0]["id"], 1)], uid()).json()
    assert _status(goods, f["id"], "waiting", "ready").status_code == 403


def test_goods_cancel_while_preparing(admin, goods, pickup, goods_menu):
    pid = goods_menu[2]["id"]
    admin.patch(f"/admin/api/products/{pid}", json={"stock": 1})
    o = order(goods, [(pid, 1)]).json()
    _status(goods, o["id"], "waiting", "ready")
    r = goods.post(f"/api/orders/{o['id']}/cancel", json={"current": "ready"})
    assert r.status_code == 200, r.text
    assert pickup.get("/api/orders?status=open").json() == []
    p = next(x for x in goods.get("/api/products").json() if x["id"] == pid)
    assert p["stock"] == 1


def test_needs_prep_can_be_changed(admin, goods, goods_menu):
    pid = goods_menu[0]["id"]
    assert admin.patch(f"/admin/api/products/{pid}", json={"needs_prep": True}).json()["needs_prep"]
    assert order(goods, [(pid, 1)], uid()).json()["status"] == "waiting"


def test_pickup_page(pickup, stranger):
    page = pickup.get("/pickup")
    assert page.status_code == 200
    assert "準備中" in page.text and "受け渡し可能" in page.text
    r = stranger.get("/pickup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?role=pickup"


def test_migrates_orders_status_check(tmp_path):
    import sqlite3

    from gakusai_pos.app import create_app

    data = tmp_path / "old"
    data.mkdir()
    conn = sqlite3.connect(data / "pos.db")
    conn.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, client_uuid TEXT UNIQUE, "
        "business_date TEXT NOT NULL, call_no INTEGER NOT NULL, total INTEGER NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('waiting', 'cooking', 'delivered', 'cancelled')), "
        "source TEXT NOT NULL DEFAULT 'pos', paid_at TEXT NOT NULL, started_at TEXT, "
        "delivered_at TEXT, cancelled_at TEXT, cancel_ack_at TEXT, "
        "is_test INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO orders (business_date, call_no, total, status, paid_at) "
        "VALUES ('2026-10-01', 7, 300, 'delivered', '2026-10-01T10:00:00');"
        "INSERT INTO orders (business_date, call_no, total, status, paid_at, started_at) "
        "VALUES ('2026-10-01', 8, 300, 'cooking', '2026-10-01T10:01:00', '2026-10-01T10:02:00');"
    )
    conn.close()
    app = create_app(data, background=False)
    try:
        db = app.state.db
        row = db.execute("SELECT call_no, category, ready_at FROM orders").fetchone()
        assert (row["call_no"], row["category"], row["ready_at"]) == (7, "food", None)
        # 以前の「調理中」は「準備中」に移る
        st = db.execute("SELECT status FROM orders WHERE call_no = 8").fetchone()["status"]
        assert st == "waiting"
        db.execute("UPDATE orders SET status = 'ready'")  # 制約に ready が入っている
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        app.state.db.close()


# ---------------------------------------------------------------- 事前注文ページ


def test_preorder_page_export(admin, menu, goods_menu):
    r = admin.get("/admin/preorder?download=1")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    html = r.text
    # 飲食の商品だけが載り、物販は載らない
    # メニューはJSONで埋め込まれる（日本語は \uXXXX になる）
    for p in menu:
        assert p["code"] in html and json.dumps(p["name"])[1:-1] in html
    for p in goods_menu:
        assert json.dumps(p["name"])[1:-1] not in html
    # QRの生成に必要なものを同梱し、サーバーとは通信しない（URL・APIを含まない）
    assert "var qrcode = function" in html
    assert "GPOS1/" in html
    assert "/api/" not in html
    assert "192.0.2.1" not in html
    assert "<script src=" not in html
    # サークルのInstagramへのリンク（URLの & はHTMLでは &amp; になる）
    assert (
        'href="https://www.instagram.com/monodzukurisakuru?xtok=YzZweGwxMnR0Nmt3&amp;utm_source=qr"'
        in html
    )
    # プレビュー（ダウンロードではない）
    assert "content-disposition" not in admin.get("/admin/preorder").headers


def test_preorder_page_admin_only(reception, stranger):
    assert reception.get("/admin/preorder").status_code == 403
    assert stranger.get("/admin/preorder").status_code == 403


def test_preorder_menu_is_escaped(admin):
    r = admin.post("/admin/api/products", json={"name": "</script><b>x", "price": 100})
    assert r.status_code == 201
    html = admin.get("/admin/preorder").text
    assert "</script><b>x" not in html


def test_reception_loads_preorder_script(reception):
    html = reception.get("/reception").text
    assert "/static/preorder_code.js" in html
    assert "preorder-note" in html


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node がない")
def test_preorder_code_format(tmp_path):
    """事前注文QRの形式（static/preorder_code.js）をNode.jsで確かめる。"""
    js = Path(__file__).resolve().parent.parent / "src/gakusai_pos/static/preorder_code.js"
    script = f"""
const P = require({json.dumps(str(js))});
const out = {{}};
const s = P.build("K7Q2XM", 450, [{{code: "M001", qty: 1}}, {{code: "m002", qty: 2}}]);
out.built = s;
out.ok = P.parse(s);
out.lower = P.parse(s.toLowerCase());
out.merged = P.parse("GPOS1/ABCD/T0/M001*2+M001*3");
out.bad = [
  "GPOS1/ABCD/T100/",
  "GPOS1/ABCD/T100/M001*0",
  "GPOS1/ABCD/T100/M001*21",
  "GPOS1/ABCD/T100/M001*1+<b>*1",
  "GPOS1/AB/T100/M001*1",
  "GPOS1/ABCD/T-1/M001*1",
  "M001",
  "GPOS1/ABCD/T100/" + Array(21).fill("M001*1").join("+"),
  "GPOS1/ABCD/T100/M001*1" + "+M002*1".repeat(200),
].map((x) => P.parse(x));
out.isPre = [P.isPreorder("gpos1/x"), P.isPreorder("M001")];
const id = P.newId();
out.id = /^[A-HJ-NP-Z2-9]{{6}}$/.test(id);
console.log(JSON.stringify(out));
"""
    path = tmp_path / "check.js"
    path.write_text(script, encoding="utf-8")
    r = subprocess.run([NODE, str(path)], capture_output=True, text=True, check=True)
    out = json.loads(r.stdout)
    assert out["built"] == "GPOS1/K7Q2XM/T450/M001*1+M002*2"
    assert out["ok"] == {
        "id": "K7Q2XM",
        "total": 450,
        "lines": [{"code": "M001", "qty": 1}, {"code": "M002", "qty": 2}],
    }
    assert out["lower"] == out["ok"]
    assert out["merged"]["lines"] == [{"code": "M001", "qty": 5}]
    assert out["bad"] == [None] * 9
    assert out["isPre"] == [True, False]
    assert out["id"] is True


def test_preorder_url_setting_and_qr(admin, reception):
    # 未設定のうちはQRを作らない
    assert admin.get("/admin/api/preorder-qr.png").status_code == 404
    assert "URLが設定されていません" in admin.get("/admin/preorder/poster").text
    # https 以外・空白入りは受け付けない
    for bad in (
        "http://example.com/",
        "javascript:alert(1)",
        "example.com",
        "https://ex ample.com/",
    ):
        r = admin.put("/admin/api/settings", json={"preorder_url": bad})
        assert r.status_code == 422, bad
    url = "https://example.github.io/preorder/?a=1&b=<x>"
    r = admin.put("/admin/api/settings", json={"preorder_url": f"  {url} "})
    assert r.status_code == 200
    assert r.json()["preorder_url"] == url
    # 他の設定を保存しても消えない
    admin.put("/admin/api/settings", json={"shop_name": "模擬店"})
    assert admin.get("/admin/api/settings").json()["preorder_url"] == url
    # 端末へ配る設定には含めない
    assert "preorder_url" not in reception.get("/api/settings").json()

    r = admin.get("/admin/api/preorder-qr.png?download=1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert 'filename="preorder-qr.png"' in r.headers["content-disposition"]
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    poster = admin.get("/admin/preorder/poster").text
    assert "/admin/api/preorder-qr.png" in poster
    assert "&lt;x&gt;" in poster and "<x>" not in poster  # URLはエスケープして表示

    # 空欄で解除
    assert admin.put("/admin/api/settings", json={"preorder_url": ""}).json()["preorder_url"] == ""
    assert admin.get("/admin/api/preorder-qr.png").status_code == 404


def test_preorder_qr_and_poster_admin_only(reception, stranger):
    for c in (reception, stranger):
        assert c.get("/admin/api/preorder-qr.png").status_code == 403
        assert c.get("/admin/preorder/poster").status_code == 403


# ---------------------------------------------------------------- 売上グラフ


def _manual(admin, product_id, qty, date, hhmm, status="delivered"):
    r = admin.post(
        "/admin/api/orders",
        json={
            "items": [{"product_id": product_id, "qty": qty}],
            "business_date": date,
            "paid_time": hhmm,
            "status": status,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_sales_series_buckets_and_cumulative(admin, menu):
    d = "2026-11-03"
    _manual(admin, menu[0]["id"], 1, d, "10:05")  # 300
    _manual(admin, menu[1]["id"], 2, d, "10:29")  # 300
    _manual(admin, menu[2]["id"], 1, d, "10:30")  # 100（次の30分）
    _manual(admin, menu[0]["id"], 2, d, "11:45")  # 600
    r = admin.get(f"/admin/api/sales-series?since={d}T10:00&until={d}T12:00")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["bucket_minutes"] == 30
    assert [b["start"] for b in s["buckets"]] == [
        f"{d}T10:00",
        f"{d}T10:30",
        f"{d}T11:00",
        f"{d}T11:30",
    ]
    assert [b["amount"] for b in s["buckets"]] == [600, 100, 0, 600]
    assert [b["orders"] for b in s["buckets"]] == [2, 1, 0, 1]
    assert [b["cumulative"] for b in s["buckets"]] == [600, 700, 700, 1300]
    assert s["amount"] == 1300 and s["orders"] == 4 and s["tickets"] == 26
    # 期間の外は含めない（まで は含まない）
    s = admin.get(f"/admin/api/sales-series?since={d}T10:30&until={d}T11:45").json()
    assert s["amount"] == 100
    assert s["buckets"][-1]["end"] == f"{d}T11:45"


def test_sales_series_trim_and_category(admin, menu, goods_menu):
    d = "2026-11-03"
    _manual(admin, menu[0]["id"], 1, d, "10:10")
    _manual(admin, goods_menu[1]["id"], 1, d, "13:40")
    _manual(admin, menu[1]["id"], 1, d, "12:00", status="waiting")  # 準備中も売上に入る
    base = f"/admin/api/sales-series?since={d}T00:00&until=2026-11-04T00:00"
    # trim で売上のある 10:00〜14:00 だけになる
    s = admin.get(base + "&trim=true").json()
    assert s["buckets"][0]["start"] == f"{d}T10:00"
    assert s["buckets"][-1]["end"] == f"{d}T14:00"
    # 2日にまたがっても、日ごとに売上のある時間帯だけ（夜中は出さない）。累計は続く
    _manual(admin, menu[0]["id"], 2, "2026-11-04", "09:15")
    s2 = admin.get(
        f"/admin/api/sales-series?since={d}T00:00&until=2026-11-05T00:00&trim=true"
    ).json()
    starts = [b["start"] for b in s2["buckets"]]
    assert starts[0] == f"{d}T10:00" and starts[-1] == "2026-11-04T09:00"
    assert f"{d}T14:00" not in starts and "2026-11-04T08:30" not in starts
    assert len(starts) == 8 + 1
    assert s2["buckets"][-1]["cumulative"] == s2["amount"] == 300 + 100 + 150 + 600
    # trim しなければ1日分（48本）
    assert len(admin.get(base).json()["buckets"]) == 48
    # 区分で絞り込む
    assert admin.get(base + "&trim=true&category=goods").json()["amount"] == 100
    # 売上がなければ空
    assert (
        admin.get(
            "/admin/api/sales-series?since=2026-01-01T00:00&until=2026-01-02T00:00&trim=true"
        ).json()["buckets"]
        == []
    )


def test_sales_series_excludes_cancelled_and_test(admin, reception, menu):
    from datetime import datetime, timedelta

    now = datetime.now()
    since = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    until = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    url = f"/admin/api/sales-series?since={since}&until={until}"
    o = order(reception, [(menu[0]["id"], 1)], uid()).json()
    assert admin.get(url).json()["amount"] == 300
    reception.post(f"/api/orders/{o['id']}/cancel", json={"current": "waiting"})
    assert admin.get(url).json()["amount"] == 0
    admin.put("/admin/api/settings", json={"test_mode": True})
    order(reception, [(menu[1]["id"], 1)], uid())
    assert admin.get(url).json()["amount"] == 0
    admin.put("/admin/api/settings", json={"test_mode": False})
    order(reception, [(menu[2]["id"], 1)], uid())
    assert admin.get(url).json()["amount"] == 100


def test_sales_series_validation_and_admin_only(admin, reception, stranger):
    bad = [
        "since=2026-11-03T12:00&until=2026-11-03T10:00",  # 逆
        "since=2026-11-01T00:00&until=2026-11-30T00:00",  # 14日超
    ]
    for q in bad:
        assert admin.get(f"/admin/api/sales-series?{q}").status_code == 400, q
    assert admin.get("/admin/api/sales-series?since=abc&until=def").status_code == 422
    assert (
        admin.get(
            "/admin/api/sales-series?since=2026-11-03T00:00&until=2026-11-04T00:00&category=x"
        ).status_code
        == 422
    )
    q = "since=2026-11-03T00:00&until=2026-11-04T00:00"
    assert reception.get(f"/admin/api/sales-series?{q}").status_code == 403
    assert stranger.get(f"/admin/api/sales-series?{q}").status_code == 403


def test_admin_page_has_charts(admin):
    html = admin.get("/admin").text
    assert 'data-tab="charts"' in html and "/static/charts.js" in html
