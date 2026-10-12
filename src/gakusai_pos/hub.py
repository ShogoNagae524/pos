"""WebSocketでの変更通知と、端末の接続状況（A-11）。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

from .auth import Terminal

PING_INTERVAL = 10  # 秒。生存確認の間隔
DEAD_AFTER = 30  # 秒。応答がなければ切断とみなす


@dataclass
class TerminalInfo:
    role: str
    label: str
    tid: str
    ip: str | None = None
    connections: int = 0
    last_seen: float = field(default_factory=time.time)
    connected_at: float | None = None

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "label": self.label,
            "tid": self.tid,
            "ip": self.ip,
            "connected": self.connections > 0,
            "last_seen": int(self.last_seen * 1000),
        }


class Hub:
    def __init__(self) -> None:
        self.clients: dict[WebSocket, Terminal] = {}
        self.last_pong: dict[WebSocket, float] = {}
        self.terminals: dict[str, TerminalInfo] = {}

    def _key(self, term: Terminal) -> str:
        return f"{term.role}:{term.tid}"

    def connect(self, ws: WebSocket, term: Terminal) -> bool:
        """接続を登録する。端末として初めての接続なら True。"""
        self.clients[ws] = term
        self.last_pong[ws] = time.time()
        info = self.terminals.get(self._key(term))
        if info is None:
            info = TerminalInfo(term.role, term.label, term.tid)
            self.terminals[self._key(term)] = info
        info.ip = ws.client.host if ws.client else None
        info.connections += 1
        info.last_seen = time.time()
        if info.connections == 1:
            info.connected_at = time.time()
            return True
        return False

    def disconnect(self, ws: WebSocket) -> bool:
        """接続を外す。端末の最後の接続だったら True。"""
        term = self.clients.pop(ws, None)
        self.last_pong.pop(ws, None)
        if term is None:
            return False
        info = self.terminals.get(self._key(term))
        if info is None:
            return False
        info.connections = max(info.connections - 1, 0)
        return info.connections == 0

    def touch(self, ws: WebSocket) -> None:
        now = time.time()
        self.last_pong[ws] = now
        term = self.clients.get(ws)
        if term:
            info = self.terminals.get(self._key(term))
            if info:
                info.last_seen = now

    def terminal_list(self) -> list[dict]:
        return [
            t.as_dict()
            for t in sorted(self.terminals.values(), key=lambda t: (t.role, t.tid))
            if t.role != "admin"
        ]

    async def _send(self, ws: WebSocket, text: str) -> None:
        try:
            await ws.send_text(text)
        except Exception:
            self.disconnect(ws)

    async def broadcast(self, type_: str, **payload) -> None:
        text = json.dumps({"type": type_, **payload}, ensure_ascii=False)
        await asyncio.gather(*(self._send(ws, text) for ws in list(self.clients)))

    async def ping_all(self) -> list[WebSocket]:
        """生存確認を送り、応答のない接続を返す。"""
        now = time.time()
        dead = [ws for ws, t in self.last_pong.items() if now - t > DEAD_AFTER]
        text = json.dumps({"type": "ping", "now": int(now * 1000)})
        await asyncio.gather(*(self._send(ws, text) for ws in list(self.clients) if ws not in dead))
        return dead
