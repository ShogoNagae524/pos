"""全端末で使うAPI（端末登録・商品一覧・注文一覧・設定）と通知の補助。"""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response

from .. import services as sv
from ..auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    ROLES,
    Terminal,
    make_token,
    require_terminal,
    verify_passcode,
)
from ..db import Tx, get_setting, log_event, public_settings
from ..schemas import SessionIn

router = APIRouter()


def db_of(request: Request):
    return request.app.state.db


# ---------------------------------------------------------------- 通知


async def notify_order(app: FastAPI, order: dict, created: bool = False) -> None:
    await app.state.hub.broadcast("order_created" if created else "order_updated", order=order)


async def notify_products(app: FastAPI, products: list[dict]) -> None:
    for p in products:
        await app.state.hub.broadcast("product_updated", product=p)


async def notify_settings(app: FastAPI) -> None:
    await app.state.hub.broadcast("settings_updated", settings=public_settings(app.state.db))


# ---------------------------------------------------------------- API


@router.post("/api/session")
async def register_session(body: SessionIn, request: Request, response: Response):
    db = db_of(request)
    if not verify_passcode(body.passcode, get_setting(db, "passcode_hash")):
        await asyncio.sleep(1)  # 総当たりを遅らせる
        raise HTTPException(401, "パスコードが違います")
    tid = secrets.token_hex(3)
    token = make_token(
        get_setting(db, "session_secret", ""),
        get_setting(db, "passcode_hash", ""),
        body.role,
        tid,
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    with Tx(db):
        log_event(
            db,
            ROLES[body.role],
            "session_registered",
            None,
            {"ip": request.client.host if request.client else None},
            tid,
        )
    return {"role": body.role, "label": ROLES[body.role], "tid": tid}


@router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request, term: Terminal = Depends(require_terminal)):
    return {
        "role": term.role,
        "label": term.label,
        "tid": term.tid,
        "settings": public_settings(db_of(request)),
    }


@router.get("/api/settings")
async def settings(request: Request, term: Terminal = Depends(require_terminal)):
    return public_settings(db_of(request))


@router.get("/api/products")
async def products(request: Request, term: Terminal = Depends(require_terminal)):
    return sv.list_products(db_of(request))


@router.get("/api/orders")
async def orders(
    request: Request,
    status: str = "open",
    date: str | None = None,
    limit: int = 10,
    term: Terminal = Depends(require_terminal),
):
    db = db_of(request)
    if status == "open":
        return sv.open_orders(db)
    if status == "delivered":
        return sv.recent_delivered(db, min(limit, 50))
    if status == "day":
        from ..db import today_str

        return sv.orders_of_day(db, date or today_str())
    raise HTTPException(400, "status は open / delivered / day のいずれかです")
