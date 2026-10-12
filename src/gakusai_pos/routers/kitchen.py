"""厨房端末（PC2）のAPI：状態変更・取消の確認・売切切替。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import services as sv
from ..auth import Terminal, require_role
from ..schemas import SoldOutIn, StatusIn
from .common import notify_order, notify_products

router = APIRouter()


@router.post("/api/orders/{order_id}/status")
async def change_status(
    order_id: int,
    body: StatusIn,
    request: Request,
    term: Terminal = Depends(require_role("kitchen")),
):
    order = sv.change_status(
        request.app.state.db, order_id, body.current, body.to, term.label, term.tid
    )
    await notify_order(request.app, order)
    return order


@router.post("/api/orders/{order_id}/ack")
async def ack_cancel(
    order_id: int, request: Request, term: Terminal = Depends(require_role("kitchen"))
):
    order = sv.ack_cancel(request.app.state.db, order_id, term.label, term.tid)
    await notify_order(request.app, order)
    return order


@router.post("/api/products/{product_id}/sold-out")
async def sold_out(
    product_id: int,
    body: SoldOutIn,
    request: Request,
    term: Terminal = Depends(require_role("kitchen")),
):
    product = sv.set_sold_out(request.app.state.db, product_id, body.sold_out, term.label, term.tid)
    await notify_products(request.app, [product])
    return product
