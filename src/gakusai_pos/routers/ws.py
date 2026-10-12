"""WebSocket（wss）：変更通知の配信と生存確認。"""

from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import terminal_from
from ..db import Tx, log_event

router = APIRouter()


@router.websocket("/ws")
async def websocket(ws: WebSocket):
    term = terminal_from(ws)
    if term is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    app = ws.app
    hub = app.state.hub
    db = app.state.db
    ip = ws.client.host if ws.client else None
    if hub.connect(ws, term) and term.role != "admin":
        with Tx(db):
            log_event(db, term.label, "terminal_connected", None, {"ip": ip}, term.tid)
    try:
        await ws.send_text(json.dumps({"type": "hello", "role": term.role}))
        while True:
            text = await ws.receive_text()
            hub.touch(ws)
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if hub.disconnect(ws) and term.role != "admin":
            with Tx(db):
                log_event(db, term.label, "terminal_disconnected", None, {"ip": ip}, term.tid)
