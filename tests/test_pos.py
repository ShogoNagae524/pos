import uuid

from conftest import order
from gakusai_pos.db import today_str


def uid():
    return str(uuid.uuid4())


# ------------------------------------------------------------ 金額（50円単位）


def test_price_must_be_multiple_of_50(admin):
    for bad in (0, -50, 120, 99):
        r = admin.post("/admin/api/products", json={"name": "x", "price": bad})
        assert r.status_code == 422, bad
    assert admin.post("/admin/api/products", json={"name": "x", "price": 250}).status_code == 201


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
    assert _status(kitchen, o["id"], "waiting", "cooking").json()["status"] == "cooking"
    d = _status(kitchen, o["id"], "cooking", "delivered").json()
    assert d["status"] == "delivered" and d["delivered_at"]
    assert kitchen.get("/api/orders?status=open").json() == []
    assert kitchen.get("/api/orders?status=delivered").json()[0]["id"] == o["id"]


def test_invalid_transitions_rejected(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    assert _status(kitchen, o["id"], "waiting", "delivered").status_code == 400
    assert _status(kitchen, o["id"], "waiting", "cancelled").status_code == 400
    assert _status(kitchen, o["id"], "delivered", "waiting").status_code == 400


def test_stale_state_is_rejected(kitchen, reception, menu):
    """厨房で「調理開始」の瞬間に受付で取消されていたら拒否して最新の状態を返す。"""
    o = order(reception, [(menu[0]["id"], 1)]).json()
    assert (
        reception.post(f"/api/orders/{o['id']}/cancel", json={"current": "waiting"}).status_code
        == 200
    )
    r = _status(kitchen, o["id"], "waiting", "cooking")
    assert r.status_code == 409
    assert r.json()["order"]["status"] == "cancelled"


def test_undo_one_step(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    _status(kitchen, o["id"], "waiting", "cooking")
    _status(kitchen, o["id"], "cooking", "delivered")
    back = _status(kitchen, o["id"], "delivered", "cooking").json()
    assert back["status"] == "cooking" and back["delivered_at"] is None
    back = _status(kitchen, o["id"], "cooking", "waiting").json()
    assert back["status"] == "waiting" and back["started_at"] is None


def test_cancel_shows_until_ack(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    _status(kitchen, o["id"], "waiting", "cooking")
    reception.post(f"/api/orders/{o['id']}/cancel", json={"current": "cooking"})
    open_ = kitchen.get("/api/orders?status=open").json()
    assert [x["status"] for x in open_] == ["cancelled"]
    kitchen.post(f"/api/orders/{o['id']}/ack")
    assert kitchen.get("/api/orders?status=open").json() == []


def test_cannot_cancel_delivered(kitchen, reception, menu):
    o = order(reception, [(menu[0]["id"], 1)]).json()
    _status(kitchen, o["id"], "waiting", "cooking")
    _status(kitchen, o["id"], "cooking", "delivered")
    assert reception.post(f"/api/orders/{o['id']}/cancel", json={}).status_code == 409


def test_fifo_order(kitchen, reception, menu):
    ids = [order(reception, [(menu[2]["id"], 1)], uid()).json()["id"] for _ in range(3)]
    _status(kitchen, ids[1], "waiting", "cooking")
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
    assert _status(reception, o["id"], "waiting", "cooking").status_code == 403
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
    _status(kitchen, a["id"], "waiting", "cooking")
    _status(kitchen, a["id"], "cooking", "delivered")

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
    # 未着手は削除できない
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
    _status(kitchen, o["id"], "waiting", "cooking")
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
    assert "M001" in admin.get("/admin/print").text
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
