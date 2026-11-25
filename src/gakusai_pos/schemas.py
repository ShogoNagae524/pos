"""入出力の型（Pydantic）。"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

Status = Literal["waiting", "cooking", "ready", "delivered", "cancelled"]
Category = Literal["food", "goods"]


def _check_price(v: int) -> int:
    # マイナスは割引券（例 -50 = 50円引き）
    if v == 0 or v % 50 != 0:
        raise ValueError("価格は50円の倍数で入力してください（割引券はマイナス。0円は不可）")
    return v


class SessionIn(BaseModel):
    passcode: str = Field(min_length=1, max_length=64)
    role: Literal["reception", "kitchen", "goods", "pickup"]


class OrderItemIn(BaseModel):
    product_id: int
    qty: int = Field(ge=1, le=99)


class OrderIn(BaseModel):
    client_uuid: str = Field(min_length=8, max_length=64)
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)


class StatusIn(BaseModel):
    current: Status  # 送信側が認識している現在の状態
    to: Status


class CancelIn(BaseModel):
    current: Status | None = None


class SoldOutIn(BaseModel):
    sold_out: bool


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    price: int
    stock: int | None = Field(default=None, ge=0)
    category: Category = "food"
    needs_prep: bool = False  # 物販で、その場で作ってから渡す（待ち時間あり）

    _price = field_validator("price")(_check_price)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("商品名を入力してください")
        return v


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    price: int | None = None
    sort_order: int | None = None
    sold_out: bool | None = None
    stock: int | None = Field(default=None, ge=0)
    clear_stock: bool = False  # true で在庫上限を解除（NULL）
    category: Category | None = None
    needs_prep: bool | None = None

    @field_validator("price")
    @classmethod
    def _price(cls, v: int | None) -> int | None:
        return None if v is None else _check_price(v)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("商品名を入力してください")
        return v


class SettingsIn(BaseModel):
    shop_name: str | None = Field(default=None, max_length=60)
    day1: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    day2: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    call_no_start: int | None = Field(default=None, ge=0, le=99999)
    test_mode: bool | None = None
    usb_backup_dir: str | None = Field(default=None, max_length=500)
    passcode: str | None = Field(default=None, min_length=4, max_length=64)
    preorder_url: str | None = Field(default=None, max_length=300)

    @field_validator("preorder_url")
    @classmethod
    def _preorder_url(cls, v: str | None) -> str | None:
        # 空欄は「未設定」。お客様のスマホで開く公開ページなので https のURLだけを受け付ける
        if v is None:
            return None
        v = v.strip()
        if not v:
            return ""
        parts = urlsplit(v)
        if parts.scheme != "https" or not parts.hostname or any(c.isspace() for c in v):
            raise ValueError("事前注文ページのURLは https:// で始まるURLを入力してください")
        return v


class ManualOrderIn(BaseModel):
    items: list[OrderItemIn] = Field(min_length=1)
    business_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    call_no: int | None = Field(default=None, ge=0)
    paid_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    status: Literal["waiting", "delivered"] = "delivered"


class PurgeIn(BaseModel):
    business_date: str | None = None
    statuses: list[Literal["delivered", "cancelled"]] = Field(
        default_factory=lambda: ["delivered", "cancelled"], min_length=1
    )
    test: Literal["all", "test", "real"] = "all"


class ClosingIn(BaseModel):
    business_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    counted_tickets: int = Field(ge=0)
    note: str = Field(default="", max_length=200)
    category: Category | None = None  # 指定するとその区分（飲食・物販）だけで照合
