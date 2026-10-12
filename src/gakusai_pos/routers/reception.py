"""受付端末（PC1）のAPI：会計確定・取消。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from .. import services as sv
from ..auth import Terminal, require_role
from ..schemas import CancelIn, OrderIn
from .common import notify_order, notify_products

router = APIRouter()


@router.post("/api/orders")
async def create_order(
    body: OrderIn,
    request: Request,
    response: Response,
    term: Terminal = Depends(require_role("reception")),
):
    order, created, touched = sv.create_order(
        request.app.state.db, body.items, term.label, term.tid, client_uuid=body.client_uuid
    )
    if created:
        response.status_code = 201
        await notify_order(request.app, order, created=True)
        await notify_products(request.app, touched)
    return order


@router.post("/api/orders/{order_id}/cancel")
async def cancel_order(
    order_id: int,
    body: CancelIn,
    request: Request,
    term: Terminal = Depends(require_role("reception")),
):
    order, touched = sv.cancel_order(
        request.app.state.db, order_id, body.current, term.label, term.tid
    )
    await notify_order(request.app, order)
    await notify_products(request.app, touched)
    return order
