import os
import sys
import tempfile
from pathlib import Path

# Окружение должно быть выставлено ДО импорта приложения (настройки читаются при импорте).
os.environ.update(
    DATABASE_URL="sqlite://",
    ENVIRONMENT="dev",
    SECRET_KEY="test-secret-key-test-secret-key-test-secret",
    DEBUG_OTP="true",
    OTP_RESEND_SECONDS="0",
    ADMIN_PHONES="+77010000000",
    UPLOAD_DIR=tempfile.mkdtemp(prefix="taxi_uploads_"),
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_PHONE = "+77010000000"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64

PICKUP = {"lat": 50.0, "lng": 70.0, "address": "Точка А"}
DROPOFF = {"lat": 50.03, "lng": 70.05, "address": "Точка Б"}  # по прямой ~4.3 км
ROUTE = {"pickup": PICKUP, "dropoff": DROPOFF, "distance_km": 5.0, "duration_min": 12}


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)  # чистая БД для каждого теста
    with TestClient(app) as c:  # lifespan: create_all + тарифы
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def login(client, phone: str, role: str | None = None, name: str | None = None) -> dict:
    code = client.post("/api/v1/auth/otp/request", json={"phone": phone}).json()["debug_code"]
    body = {"phone": phone, "code": code}
    if role:
        body["role"] = role
    if name:
        body["name"] = name
    r = client.post("/api/v1/auth/otp/verify", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def make_passenger(client, phone="+77011111111", name="Пассажир") -> dict:
    data = login(client, phone, "passenger", name)
    return {"h": auth(data["access_token"]), "id": data["user"]["id"]}


def make_admin(client) -> dict:
    data = login(client, ADMIN_PHONE)
    assert data["user"]["role"] == "admin"
    return {"h": auth(data["access_token"]), "id": data["user"]["id"]}


def make_driver(client, admin, phone="+77022222222", plate="123ABC01", online=True, car_class="economy"):
    data = login(client, phone, "driver")
    h, uid = auth(data["access_token"]), data["user"]["id"]
    r = client.put(
        "/api/v1/drivers/me/profile",
        headers=h,
        json={
            "full_name": "Водитель Тестов",
            "car_make": "Toyota",
            "car_model": "Camry",
            "car_color": "белый",
            "car_year": 2020,
            "plate_number": plate,
            "car_class": car_class,
        },
    )
    assert r.status_code == 200, r.text
    for doc_type in ("driver_license", "vehicle_registration", "id_card"):
        r = client.post(
            "/api/v1/drivers/me/documents",
            headers=h,
            data={"doc_type": doc_type},
            files={"file": ("doc.jpg", JPEG, "image/jpeg")},
        )
        assert r.status_code == 201, r.text
    drv = client.get(f"/api/v1/admin/drivers/{uid}", headers=admin["h"]).json()
    for d in drv["documents"]:
        r = client.post(
            f"/api/v1/admin/documents/{d['id']}/review", headers=admin["h"], json={"approved": True}
        )
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    if online:
        go_online(client, h)
    return {"h": h, "id": uid}


def go_online(client, h, lat=50.001, lng=70.001):
    assert client.post("/api/v1/drivers/me/location", headers=h, json={"lat": lat, "lng": lng}).status_code == 204
    r = client.post("/api/v1/drivers/me/status", headers=h, json={"is_online": True})
    assert r.status_code == 200, r.text


def create_ride(client, passenger, **extra):
    return client.post(
        "/api/v1/rides", headers=passenger["h"], json={**ROUTE, "tariff_code": "economy", **extra}
    )


def complete_ride(client, passenger, driver, **extra) -> int:
    """Создаёт и проводит заказ до статуса completed, возвращает ride_id."""
    r = create_ride(client, passenger, **extra)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    for step in ("accept", "arrived", "start", "complete"):
        r = client.post(f"/api/v1/rides/{rid}/{step}", headers=driver["h"])
        assert r.status_code == 200, (step, r.text)
    return rid
