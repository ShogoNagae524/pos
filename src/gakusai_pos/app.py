"""FastAPIアプリの組み立て。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import DB_FILENAME, connect, init_db, run_backup
from .hub import PING_INTERVAL, Hub
from .routers import admin, common, kitchen, pages, reception, ws
from .services import ServiceError
from .tls import local_ips

log = logging.getLogger("gakusai_pos")

BACKUP_INTERVAL = 5 * 60  # 秒

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _ping_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL)
        dead = await app.state.hub.ping_all()
        for sock in dead:
            # 閉じると受信待ちが終わり、ws.py 側で切断として記録される
            with contextlib.suppress(Exception):
                await sock.close()


async def _backup_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        st = app.state
        try:
            st.backup_status["last_auto"] = run_backup(st.db, st.data_dir)
            st.backup_status["last_error"] = None
        except Exception as e:  # バックアップ失敗で営業を止めない
            log.exception("自動バックアップに失敗")
            st.backup_status["last_error"] = str(e)


def create_app(
    data_dir: Path,
    port: int = 8443,
    scheme: str = "https",
    background: bool = True,
) -> FastAPI:
    data_dir = Path(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if background:
            tasks = [asyncio.create_task(_ping_loop(app)), asyncio.create_task(_backup_loop(app))]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            app.state.db.close()

    app = FastAPI(title="学祭飲食POS", lifespan=lifespan, docs_url=None, redoc_url=None)

    db = connect(data_dir / DB_FILENAME)
    app.state.initial_passcode = init_db(db)
    app.state.db = db
    app.state.data_dir = data_dir
    app.state.port = port
    app.state.scheme = scheme
    app.state.local_ip_list = local_ips()
    app.state.local_ips = set(app.state.local_ip_list)
    app.state.hub = Hub()
    app.state.backup_status = {
        "last_auto": None,
        "last_manual": None,
        "last_error": None,
        "interval_seconds": BACKUP_INTERVAL,
    }

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        return JSONResponse({"detail": exc.message, **exc.extra}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors():
            msg = str(e.get("msg", ""))
            msgs.append(msg.removeprefix("Value error, "))
        return JSONResponse(
            {"detail": " / ".join(msgs) or "入力が正しくありません"}, status_code=422
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for r in (pages, common, reception, kitchen, admin, ws):
        app.include_router(r.router)
    return app
