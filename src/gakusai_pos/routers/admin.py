"""管理端末（PC3）の画面用API。PC3自身からのアクセスだけを受け付ける。"""

from __future__ import annotations

import io
from datetime import datetime

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import services as sv
from ..auth import ADMIN_LABEL, hash_passcode, require_admin
from ..db import (
    DEFAULT_SETTINGS,
    Tx,
    get_setting,
    log_event,
    now_str,
    public_settings,
    run_backup,
    set_setting,
    today_str,
)
from ..schemas import ClosingIn, ManualOrderIn, ProductIn, ProductPatch, PurgeIn, SettingsIn
from .common import notify_order, notify_products, notify_settings

router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])

ADMIN = ADMIN_LABEL


def _db(request: Request):
    return request.app.state.db


def server_urls(app) -> list[str]:
    st = app.state
    return [f"{st.scheme}://{ip}:{st.port}/" for ip in st.local_ip_list]


@router.get("/info")
async def info(request: Request):
    db = _db(request)
    st = request.app.state
    return {
        "urls": server_urls(request.app),
        "passcode_changed": get_setting(db, "passcode_changed") == "1",
        "terminals": st.hub.terminal_list(),
        "backup": st.backup_status,
        "settings": public_settings(db),
        "now": now_str(),
    }


@router.get("/qr.png")
async def qr_png(data: str, box: int = 10):
    if len(data) > 300:
        raise HTTPException(400, "データが長すぎます")
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=max(2, min(box, 20)), border=2
    )
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return Response(
        buf.getvalue(), media_type="image/png", headers={"Cache-Control": "max-age=3600"}
    )


# ---------------------------------------------------------------- products


@router.get("/products")
async def products(request: Request):
    return sv.list_products(_db(request))


@router.post("/products", status_code=201)
async def add_product(body: ProductIn, request: Request):
    product = sv.add_product(_db(request), body.name, body.price, body.stock, ADMIN)
    await notify_products(request.app, [product])
    return product


@router.patch("/products/{product_id}")
async def update_product(product_id: int, body: ProductPatch, request: Request):
    product = sv.update_product(_db(request), product_id, body, ADMIN)
    await notify_products(request.app, [product])
    return product


@router.delete("/products/{product_id}")
async def delete_product(product_id: int, request: Request):
    product = sv.delete_product(_db(request), product_id, ADMIN)
    await notify_products(request.app, [product])
    return product


# ---------------------------------------------------------------- settings


@router.get("/settings")
async def get_settings(request: Request):
    db = _db(request)
    s = public_settings(db)
    s["usb_backup_dir"] = get_setting(db, "usb_backup_dir", "")
    s["passcode_changed"] = get_setting(db, "passcode_changed") == "1"
    return s


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request):
    db = _db(request)
    changed: dict = {}
    with Tx(db):
        for key in ("shop_name", "day1", "day2", "usb_backup_dir"):
            value = getattr(body, key)
            if value is None:
                continue
            value = value.strip()
            if value != get_setting(db, key, DEFAULT_SETTINGS[key]):
                set_setting(db, key, value)
                changed[key] = value
        if body.call_no_start is not None and str(body.call_no_start) != get_setting(
            db, "call_no_start"
        ):
            set_setting(db, "call_no_start", str(body.call_no_start))
            changed["call_no_start"] = body.call_no_start
        if body.test_mode is not None and body.test_mode != (get_setting(db, "test_mode") == "1"):
            set_setting(db, "test_mode", "1" if body.test_mode else "0")
            changed["test_mode"] = body.test_mode
        if body.passcode:
            set_setting(db, "passcode_hash", hash_passcode(body.passcode))
            set_setting(db, "passcode_changed", "1")
            changed["passcode"] = "（変更）"
        if changed:
            log_event(db, ADMIN, "settings_updated", None, changed)
    if changed:
        await notify_settings(request.app)
    return await get_settings(request)


# ---------------------------------------------------------------- events


@router.get("/events")
async def events(
    request: Request,
    terminal: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    after_id: int | None = None,
    limit: int = 300,
):
    return sv.search_events(
        _db(request),
        terminal or None,
        kind or None,
        since or None,
        until or None,
        after_id,
        min(limit, 2000),
    )


# ---------------------------------------------------------------- orders


@router.get("/orders")
async def orders(
    request: Request,
    date: str | None = None,
    status: str | None = None,
    q: str | None = None,
    test: str = "all",
):
    return sv.search_orders(_db(request), date or None, status or None, q or None, test)


@router.post("/orders", status_code=201)
async def manual_order(body: ManualOrderIn, request: Request):
    date = body.business_date or today_str()
    paid_at = None
    if body.paid_time:
        paid_at = datetime.fromisoformat(f"{date}T{body.paid_time}:00").isoformat()
    order, _, touched = sv.create_order(
        _db(request),
        body.items,
        ADMIN,
        "pc3",
        source="manual",
        business_date=date,
        call_no=body.call_no,
        paid_at=paid_at,
        status=body.status,
        is_test=False,
    )
    await notify_order(request.app, order, created=True)
    await notify_products(request.app, touched)
    return order


@router.delete("/orders/{order_id}")
async def delete_order(order_id: int, request: Request):
    order = sv.delete_order(_db(request), order_id, ADMIN)
    await notify_order(request.app, {**order, "deleted": True})
    return {"deleted": 1}


@router.post("/orders/purge-preview")
async def purge_preview(body: PurgeIn, request: Request):
    return sv.purge_preview(_db(request), body)


@router.post("/orders/purge")
async def purge(body: PurgeIn, request: Request):
    result = sv.purge_orders(_db(request), body, ADMIN)
    for oid in result["ids"]:
        await request.app.state.hub.broadcast("order_updated", order={"id": oid, "deleted": True})
    return {"count": result["count"], "amount": result["amount"]}


# ---------------------------------------------------------------- summary / closing


@router.get("/summary")
async def summary(request: Request, date: str | None = None):
    return sv.summary(_db(request), date or None)


@router.post("/closing")
async def closing(body: ClosingIn, request: Request):
    return sv.record_closing(
        _db(request), body.business_date, body.counted_tickets, body.note, ADMIN
    )


@router.get("/closings")
async def closings(request: Request):
    return sv.closings(_db(request))


# ---------------------------------------------------------------- CSV / backup


@router.get("/export/{kind}.csv")
async def export(kind: str, request: Request):
    text = sv.export_csv(_db(request), kind)
    filename = f"{kind}-{datetime.now():%Y%m%d-%H%M}.csv"
    return Response(
        text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup")
async def backup(request: Request):
    st = request.app.state
    result = run_backup(st.db, st.data_dir)
    st.backup_status["last_manual"] = result
    with Tx(st.db):
        log_event(st.db, ADMIN, "backup", None, result)
    return result
