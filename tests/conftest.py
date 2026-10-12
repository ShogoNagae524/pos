import pytest
from fastapi.testclient import TestClient

from gakusai_pos.app import create_app

BASE = "https://testserver"
REMOTE = ("192.0.2.10", 50000)  # PC1・PC2（Wi-Fi上の別の端末）
LOCAL = ("127.0.0.1", 50000)  # PC3自身


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "data", background=False)
    app.state.local_ips = set()  # テスト環境のIPに左右されないようにする
    app.state.local_ip_list = ["192.0.2.1"]
    yield app
    app.state.db.close()


@pytest.fixture
def admin(app):
    with TestClient(app, base_url=BASE, client=LOCAL) as c:
        yield c


def _terminal(app, role):
    c = TestClient(app, base_url=BASE, client=REMOTE)
    r = c.post("/api/session", json={"passcode": app.state.initial_passcode, "role": role})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def reception(app):
    with _terminal(app, "reception") as c:
        yield c


@pytest.fixture
def kitchen(app):
    with _terminal(app, "kitchen") as c:
        yield c


@pytest.fixture
def stranger(app):
    with TestClient(app, base_url=BASE, client=REMOTE) as c:
        yield c


@pytest.fixture
def menu(admin):
    """やきそば300円・フランク150円・ジュース100円"""
    items = [("やきそば", 300), ("フランク", 150), ("ジュース", 100)]
    out = []
    for name, price in items:
        r = admin.post("/admin/api/products", json={"name": name, "price": price})
        assert r.status_code == 201, r.text
        out.append(r.json())
    return out


def order(client, items, uuid="00000000-0000-4000-8000-000000000001"):
    return client.post(
        "/api/orders",
        json={"client_uuid": uuid, "items": [{"product_id": p, "qty": q} for p, q in items]},
    )
