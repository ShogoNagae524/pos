"""HTMLページ（Jinja2テンプレート）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from .. import services as sv
from ..auth import is_local, terminal_from
from ..db import get_setting, public_settings

router = APIRouter()
# お客様向けの事前注文ページに載せるサークルのInstagram
INSTAGRAM_URL = "https://www.instagram.com/monodzukurisakuru?xtok=YzZweGwxMnR0Nmt3&utm_source=qr"
PKG_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=PKG_DIR / "templates")


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
    if term and term.role in ("reception", "kitchen", "goods", "pickup") and not _is_pc3(request):
        return RedirectResponse(f"/{term.role}", status_code=303)
    return _render(request, "index.html", pc3=_is_pc3(request))


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, role: str = "reception"):
    if role not in ("reception", "kitchen", "goods", "pickup"):
        role = "reception"
    return _render(request, "login.html", role=role)


@router.get("/reception", response_class=HTMLResponse)
async def reception(request: Request):
    term = terminal_from(request)
    if term is None or term.role != "reception":
        return RedirectResponse("/login?role=reception", status_code=303)
    return _render(request, "reception.html", term=term)


@router.get("/goods", response_class=HTMLResponse)
async def goods(request: Request):
    # 物販（PC4）は受付と同じ画面。調理の工程がないので会計した時点で受渡済になる
    term = terminal_from(request)
    if term is None or term.role != "goods":
        return RedirectResponse("/login?role=goods", status_code=303)
    return _render(request, "reception.html", term=term)


@router.get("/pickup", response_class=HTMLResponse)
async def pickup(request: Request):
    # 物販の受渡表示（PC5）。準備中・受渡可能の受付番号を表示するだけ
    term = terminal_from(request)
    if term is None or term.role != "pickup":
        return RedirectResponse("/login?role=pickup", status_code=303)
    return _render(request, "pickup.html", term=term)


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


@router.get("/admin/preorder", response_class=HTMLResponse)
async def preorder_page(request: Request, download: bool = False):
    """お客様向けの事前注文ページ（1枚きりのHTML）を書き出す。

    サーバーとは通信しないページなので、GitHub Pages など別の場所に置いて案内する。
    お客様をPOSのサーバーにアクセスさせないための仕組み。
    """
    if not _is_pc3(request):
        return HTMLResponse("管理画面はPC3（サーバーのPC）からのみ開けます。", status_code=403)
    # QRに入れるのは商品コードと個数だけ。価格は画面の表示と、受付での価格変更の確認に使う
    menu = [
        {"code": p["code"], "name": p["name"], "price": p["price"]}
        for p in sv.list_products(request.app.state.db)
        if p["category"] == "food" and p["price"] > 0  # 割引券は受付で受け取って入れる
    ]
    now = datetime.now()
    static = PKG_DIR / "static"
    res = _render(
        request,
        "preorder.html",
        menu=menu,
        exported_at=now.strftime("%Y-%m-%d %H:%M"),
        instagram_url=INSTAGRAM_URL,
        menu_stamp=now.strftime("%Y%m%d%H%M%S"),
        qrcode_js=(static / "vendor" / "qrcode.js").read_text(encoding="utf-8"),
        preorder_code_js=(static / "preorder_code.js").read_text(encoding="utf-8"),
    )
    if download:
        res.headers["Content-Disposition"] = 'attachment; filename="preorder.html"'
    return res


@router.get("/admin/preorder/poster", response_class=HTMLResponse)
async def preorder_poster(request: Request):
    """事前注文の案内ポスター（A4）。QRは設定した公開URL。"""
    if not _is_pc3(request):
        return HTMLResponse("管理画面はPC3（サーバーのPC）からのみ開けます。", status_code=403)
    url = get_setting(request.app.state.db, "preorder_url", "")
    return _render(request, "preorder_poster.html", url=url)
