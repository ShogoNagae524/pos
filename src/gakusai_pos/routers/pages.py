"""HTMLページ（Jinja2テンプレート）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from .. import services as sv
from ..auth import is_local, terminal_from
from ..db import public_settings

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    settings = public_settings(request.app.state.db)
    return templates.TemplateResponse(
        request, name, {"settings": settings, "v": __version__, **ctx}
    )


def _is_pc3(request: Request) -> bool:
    return is_local(request.client.host if request.client else None, request.app.state.local_ips)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    term = terminal_from(request)
    if term and term.role in ("reception", "kitchen") and not _is_pc3(request):
        return RedirectResponse(f"/{term.role}", status_code=303)
    return _render(request, "index.html", pc3=_is_pc3(request))


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, role: str = "reception"):
    if role not in ("reception", "kitchen"):
        role = "reception"
    return _render(request, "login.html", role=role)


@router.get("/reception", response_class=HTMLResponse)
async def reception(request: Request):
    term = terminal_from(request)
    if term is None or term.role != "reception":
        return RedirectResponse("/login?role=reception", status_code=303)
    return _render(request, "reception.html", term=term)


@router.get("/kitchen", response_class=HTMLResponse)
async def kitchen(request: Request):
    term = terminal_from(request)
    if term is None or term.role != "kitchen":
        return RedirectResponse("/login?role=kitchen", status_code=303)
    return _render(request, "kitchen.html", term=term)


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    if not _is_pc3(request):
        return HTMLResponse("管理画面はPC3（サーバーのPC）からのみ開けます。", status_code=403)
    return _render(request, "admin.html")


@router.get("/admin/print", response_class=HTMLResponse)
async def print_cards(request: Request):
    if not _is_pc3(request):
        return HTMLResponse("管理画面はPC3（サーバーのPC）からのみ開けます。", status_code=403)
    return _render(request, "print.html", products=sv.list_products(request.app.state.db))
