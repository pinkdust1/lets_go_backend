from datetime import timedelta

from app.config import settings
from app.utils import utcnow

from .conftest import (
    JPEG,
    ROUTE,
    auth,
    complete_ride,
    create_ride,
    go_online,
    login,
    make_admin,
    make_driver,
    make_passenger,
)

API = "/api/v1"


# ====================================================================== auth
def test_otp_registration_and_login(client):
    r = client.post(f"{API}/auth/otp/request", json={"phone": "8 (701) 111-11-11"})
    assert r.status_code == 200
    code = r.json()["debug_code"]
    assert len(code) == 6

    bad = client.post(f"{API}/auth/otp/verify", json={"phone": "+77011111111", "code": "000000"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "OTP_INVALID"

    r = client.post(f"{API}/auth/otp/verify", json={"phone": "+77011111111", "code": code, "name": "Анна"})
    body = r.json()
    assert r.status_code == 200 and body["is_new_user"] is True
    assert body["user"]["phone"] == "+77011111111"  # номер нормализован (8... -> +7...)
    assert body["user"]["role"] == "passenger"

    # код одноразовый
    again = client.post(f"{API}/auth/otp/verify", json={"phone": "+77011111111", "code": code})
    assert again.status_code == 400

    # повторный вход — не новый пользователь; токены работают
    data = login(client, "+77011111111")
    assert data["is_new_user"] is False
    me = client.get(f"{API}/users/me", headers=auth(data["access_token"]))
    assert me.json()["name"] == "Анна"

    # refresh
    r = client.post(f"{API}/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert r.status_code == 200 and client.get(f"{API}/users/me", headers=auth(r.json()["access_token"])).status_code == 200
    # access-токен нельзя использовать как refresh и наоборот
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": data["access_token"]}).status_code == 401
    assert client.get(f"{API}/users/me", headers=auth(data["refresh_token"])).status_code == 401
    assert client.get(f"{API}/users/me").status_code == 401


def test_otp_limits(client, monkeypatch):
    monkeypatch.setattr(settings, "otp_resend_seconds", 60)
    assert client.post(f"{API}/auth/otp/request", json={"phone": "+77011111111"}).status_code == 200
    r = client.post(f"{API}/auth/otp/request", json={"phone": "+77011111111"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "OTP_TOO_SOON"

    # перебор: после 5 неверных попыток даже верный код не принимается
    monkeypatch.setattr(settings, "otp_resend_seconds", 0)
    code = client.post(f"{API}/auth/otp/request", json={"phone": "+77033333333"}).json()["debug_code"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        client.post(f"{API}/auth/otp/verify", json={"phone": "+77033333333", "code": wrong})
    r = client.post(f"{API}/auth/otp/verify", json={"phone": "+77033333333", "code": code})
    assert r.status_code == 429 and r.json()["error"]["code"] == "OTP_ATTEMPTS_EXCEEDED"


def test_roles(client):
    make_passenger(client, "+77011111111")
    # номер пассажира нельзя «перерегистрировать» водителем
    code = client.post(f"{API}/auth/otp/request", json={"phone": "+77011111111"}).json()["debug_code"]
    r = client.post(f"{API}/auth/otp/verify", json={"phone": "+77011111111", "code": code, "role": "driver"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ROLE_MISMATCH"
    # admin самому себе назначить нельзя
    r = client.post(f"{API}/auth/otp/verify", json={"phone": "+77044444444", "code": "123456", "role": "admin"})
    assert r.status_code == 422

    admin, passenger = make_admin(client), make_passenger(client, "+77055555555")
    assert client.get(f"{API}/admin/stats", headers=admin["h"]).status_code == 200
    assert client.get(f"{API}/admin/stats", headers=passenger["h"]).status_code == 403
    assert client.get(f"{API}/drivers/me", headers=passenger["h"]).status_code == 403


# ====================================================================== водитель
def test_driver_onboarding_and_moderation(client):
    admin = make_admin(client)
    data = login(client, "+77022222222", "driver")
    h, uid = auth(data["access_token"]), data["user"]["id"]

    drv = client.get(f"{API}/drivers/me", headers=h).json()
    assert drv["status"] == "incomplete"
    assert sorted(drv["missing_documents"]) == ["driver_license", "id_card", "vehicle_registration"]

    # без одобрения на линию выйти нельзя
    assert client.post(f"{API}/drivers/me/status", headers=h, json={"is_online": True}).status_code == 403

    profile = {
        "full_name": "Иван Петров", "car_make": "Kia", "car_model": "Rio", "car_color": "серый",
        "car_year": 2019, "plate_number": "777 abc 02", "car_class": "economy",
    }
    r = client.put(f"{API}/drivers/me/profile", headers=h, json=profile)
    assert r.json()["plate_number"] == "777ABC02"  # нормализован
    assert r.json()["status"] == "incomplete"  # документов ещё нет
    assert client.put(f"{API}/drivers/me/profile", headers=h, json={**profile, "car_class": "spaceship"}).status_code == 422

    def upload(doc_type, content=JPEG, name="d.jpg"):
        return client.post(f"{API}/drivers/me/documents", headers=h, data={"doc_type": doc_type}, files={"file": (name, content, "image/jpeg")})

    # проверка по сигнатуре, а не по расширению/Content-Type
    r = upload("id_card", b"MZ\x90\x00 this is an exe", "evil.jpg")
    assert r.status_code == 415
    r = upload("id_card", JPEG + b"0" * (settings.max_upload_mb * 1024 * 1024))
    assert r.status_code == 413

    assert upload("driver_license").status_code == 201
    assert upload("vehicle_registration").status_code == 201
    assert client.get(f"{API}/drivers/me", headers=h).json()["status"] == "incomplete"  # нет 3-го
    assert upload("id_card").status_code == 201
    assert client.get(f"{API}/drivers/me", headers=h).json()["status"] == "pending"

    # очередь модерации
    pending = client.get(f"{API}/admin/drivers?status=pending", headers=admin["h"]).json()
    assert [d["user_id"] for d in pending] == [uid]
    docs = {d["doc_type"]: d["id"] for d in pending[0]["documents"]}

    # отклоняем один документ -> rejected, причина обязательна
    assert client.post(f"{API}/admin/documents/{docs['id_card']}/review", headers=admin["h"], json={"approved": False}).status_code == 422
    r = client.post(f"{API}/admin/documents/{docs['id_card']}/review", headers=admin["h"], json={"approved": False, "reason": "Нечитаемо"})
    assert r.json()["status"] == "rejected"
    rejected_doc = next(d for d in r.json()["documents"] if d["doc_type"] == "id_card")
    assert rejected_doc["reject_reason"] == "Нечитаемо"

    # перезагрузка -> снова pending (старый документ заменён, не добавлен)
    assert upload("id_card").status_code == 201
    drv = client.get(f"{API}/drivers/me", headers=h).json()
    assert drv["status"] == "pending" and len(drv["documents"]) == 3

    for d in drv["documents"]:
        r = client.post(f"{API}/admin/documents/{d['id']}/review", headers=admin["h"], json={"approved": True})
    assert r.json()["status"] == "approved"

    # файл документа: владелец и админ видят, посторонний водитель — нет
    other = make_driver(client, admin, phone="+77066666666", plate="999ZZZ01", online=False)
    doc_id = drv["documents"][0]["id"]
    assert client.get(f"{API}/drivers/documents/{doc_id}/file", headers=h).status_code == 200
    assert client.get(f"{API}/drivers/documents/{doc_id}/file", headers=admin["h"]).status_code == 200
    assert client.get(f"{API}/drivers/documents/{doc_id}/file", headers=other["h"]).status_code == 404

    # уникальность госномера
    r = client.put(f"{API}/drivers/me/profile", headers=other["h"], json={**profile, "plate_number": "777ABC02"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLATE_TAKEN"

    # выход на линию: нужна геопозиция
    assert client.post(f"{API}/drivers/me/status", headers=h, json={"is_online": True}).status_code == 409
    go_online(client, h)

    # блокировка сбрасывает онлайн и закрывает доступ; разблокировка возвращает approved
    r = client.post(f"{API}/admin/drivers/{uid}/block", headers=admin["h"], json={"reason": "Жалобы пассажиров"})
    assert r.json()["status"] == "blocked" and r.json()["is_online"] is False
    assert client.post(f"{API}/drivers/me/location", headers=h, json={"lat": 50, "lng": 70}).status_code == 403
    r = client.post(f"{API}/admin/drivers/{uid}/unblock", headers=admin["h"])
    assert r.json()["status"] == "approved"


# ====================================================================== заказ + промокод + рейтинг
def test_full_ride_with_promo_and_ratings(client):
    admin, passenger = make_admin(client), make_passenger(client)
    driver = make_driver(client, admin)

    r = client.post(f"{API}/admin/promo-codes", headers=admin["h"], json={
        "code": "welcome20", "discount_type": "percent", "value": 20, "max_discount": 300, "first_ride_only": True,
    })
    assert r.status_code == 201 and r.json()["code"] == "WELCOME20"

    est = client.post(f"{API}/rides/estimate", headers=passenger["h"], json={**ROUTE, "promo_code": "welcome20"}).json()
    items = {i["tariff_code"]: i for i in est["items"]}
    assert set(items) == {"economy", "comfort", "business"}
    eco = items["economy"]  # 300 + 100*5 + 15*12 = 980
    assert eco["price"] == 980 and eco["discount"] == 196 and eco["final_price"] == 784

    r = create_ride(client, passenger, promo_code="WELCOME20")
    assert r.status_code == 201, r.text
    ride = r.json()
    assert ride["status"] == "searching" and ride["final_price"] == 784 and ride["promo_code"] == "WELCOME20"
    assert ride["driver"] is None
    rid = ride["id"]

    # второй активный заказ создать нельзя
    assert create_ride(client, passenger).json()["error"]["code"] == "ACTIVE_RIDE_EXISTS"

    # водитель видит заказ без телефона пассажира
    avail = client.get(f"{API}/rides/available", headers=driver["h"]).json()
    assert [a["id"] for a in avail] == [rid] and "phone" not in avail[0]

    r = client.post(f"{API}/rides/{rid}/accept", headers=driver["h"])
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert r.json()["passenger"]["phone"] == "+77011111111"  # теперь можно позвонить
    # пассажир видит водителя, авто и его позицию
    seen = client.get(f"{API}/rides/{rid}", headers=passenger["h"]).json()
    assert seen["driver"]["plate_number"] == "123ABC01" and seen["driver"]["lat"] == 50.001
    assert client.get(f"{API}/rides/active", headers=passenger["h"]).json()["id"] == rid
    assert client.get(f"{API}/rides/active", headers=driver["h"]).json()["id"] == rid

    # нельзя перепрыгнуть статус
    assert client.post(f"{API}/rides/{rid}/start", headers=driver["h"]).status_code == 409
    assert client.post(f"{API}/rides/{rid}/complete", headers=driver["h"]).status_code == 409
    for step in ("arrived", "start"):
        assert client.post(f"{API}/rides/{rid}/{step}", headers=driver["h"]).status_code == 200
    # во время поездки отмена недоступна
    assert client.post(f"{API}/rides/{rid}/cancel", headers=passenger["h"]).status_code == 409
    # оценить до завершения нельзя
    assert client.post(f"{API}/rides/{rid}/rating", headers=passenger["h"], json={"score": 5}).json()["error"]["code"] == "RIDE_NOT_COMPLETED"

    done = client.post(f"{API}/rides/{rid}/complete", headers=driver["h"]).json()
    assert done["status"] == "completed"
    assert done["platform_fee"] == 784 * 15 // 100 and done["driver_earnings"] == 784 - 784 * 15 // 100
    assert client.get(f"{API}/rides/{rid}", headers=passenger["h"]).json()["platform_fee"] is None  # пассажиру не показываем
    assert client.get(f"{API}/rides/active", headers=passenger["h"]).json() is None
    assert client.get(f"{API}/rides/{rid}", headers=passenger["h"]).json()["passenger"]["phone"] is None  # заказ закрыт

    # рейтинги: обе стороны, повторно нельзя
    r = client.post(f"{API}/rides/{rid}/rating", headers=passenger["h"], json={"score": 5, "comment": "Отлично", "tags": ["чисто", "вежливо"]})
    assert r.status_code == 201
    assert client.post(f"{API}/rides/{rid}/rating", headers=passenger["h"], json={"score": 1}).json()["error"]["code"] == "ALREADY_RATED"
    assert client.post(f"{API}/rides/{rid}/rating", headers=driver["h"], json={"score": 4}).status_code == 201
    assert client.post(f"{API}/rides/{rid}/rating", headers=passenger["h"], json={"score": 6}).status_code == 422

    me_driver = client.get(f"{API}/ratings/me", headers=driver["h"]).json()
    assert me_driver["average"] == 5.0 and me_driver["count"] == 1
    assert me_driver["recent"][0]["tags"] == ["чисто", "вежливо"]
    assert client.get(f"{API}/ratings/me", headers=passenger["h"]).json()["average"] == 4.0
    assert client.get(f"{API}/rides/{rid}", headers=passenger["h"]).json()["my_rating"] == 5

    # среднее считается по всем поездкам: вторая поездка с оценкой 2 -> (5+2)/2 = 3.5
    rid2 = complete_ride(client, passenger, driver)
    client.post(f"{API}/rides/{rid2}/rating", headers=passenger["h"], json={"score": 2})
    s = client.get(f"{API}/ratings/me", headers=driver["h"]).json()
    assert s["average"] == 3.5 and s["count"] == 2 and s["distribution"] == {"1": 0, "2": 1, "3": 0, "4": 0, "5": 1}
    assert client.get(f"{API}/rides/{rid2}", headers=passenger["h"]).json()["driver"]["rating_avg"] == 3.5

    # «первая поездка» уже использована
    est = client.post(f"{API}/rides/estimate", headers=passenger["h"], json={**ROUTE, "promo_code": "WELCOME20"}).json()
    assert est["items"][0]["promo_error"] and est["items"][0]["discount"] == 0

    # история и заработок
    hist = client.get(f"{API}/rides/history", headers=passenger["h"]).json()
    assert [h["id"] for h in hist] == [rid2, rid]
    earn = client.get(f"{API}/drivers/me/earnings?days=7", headers=driver["h"]).json()
    assert earn["rides_count"] == 2 and earn["gross"] == 784 + 980 and earn["gross"] == earn["platform_fee"] + earn["net"]

    # окно оценки закрывается
    monkey_window = settings.rating_window_hours
    settings.rating_window_hours = 0
    try:
        rid3 = complete_ride(client, passenger, driver)
        r = client.post(f"{API}/rides/{rid3}/rating", headers=passenger["h"], json={"score": 5})
        assert r.status_code == 409 and r.json()["error"]["code"] == "RATING_WINDOW_CLOSED"
    finally:
        settings.rating_window_hours = monkey_window


# ====================================================================== промокоды: правила
def test_promo_rules(client):
    admin, passenger = make_admin(client), make_passenger(client)
    driver = make_driver(client, admin)

    def promo(**kw):
        body = {"discount_type": "fixed", "value": 100, **kw}
        r = client.post(f"{API}/admin/promo-codes", headers=admin["h"], json=body)
        assert r.status_code == 201, r.text

    def validate(code, amount=1000):
        return client.post(f"{API}/promos/validate", headers=passenger["h"], json={"code": code, "order_amount": amount}).json()

    promo(code="FIX100")
    promo(code="BIG", min_order=5000)
    promo(code="HALF", discount_type="percent", value=50, max_discount=200)
    promo(code="OLD", valid_until=(utcnow() - timedelta(days=1)).isoformat())
    promo(code="LATER", valid_from=(utcnow() + timedelta(days=1)).isoformat())
    promo(code="ONCE", max_uses=1, per_user_limit=5)
    promo(code="OFF", is_active=False)
    promo(code="FREE", discount_type="fixed", value=100000)  # скидка больше цены -> цена не уходит в минус

    assert validate("fix100") == {"valid": True, "code": "FIX100", "discount": 100, "final_price": 900, "error_code": None, "message": None}
    assert validate("HALF")["discount"] == 200  # потолок max_discount
    assert validate("HALF", 200)["discount"] == 100
    assert validate("FREE")["final_price"] == 0
    for code, err in [("BIG", "PROMO_MIN_ORDER"), ("OLD", "PROMO_EXPIRED"), ("LATER", "PROMO_EXPIRED"),
                      ("OFF", "PROMO_EXPIRED"), ("NOPE", "PROMO_NOT_FOUND")]:
        v = validate(code)
        assert v["valid"] is False and v["error_code"] == err, (code, v)

    # невалидный промокод при создании заказа — ошибка, заказ не создаётся
    r = create_ride(client, passenger, promo_code="OLD")
    assert r.status_code == 400 and r.json()["error"]["code"] == "PROMO_EXPIRED"

    # per_user_limit=1: после использования — нельзя; отмена возвращает промокод
    rid = create_ride(client, passenger, promo_code="FIX100").json()["id"]
    assert validate("FIX100")["error_code"] == "PROMO_ALREADY_USED"
    assert client.post(f"{API}/rides/{rid}/cancel", headers=passenger["h"], json={"reason": "передумал"}).json()["status"] == "cancelled"
    assert validate("FIX100")["valid"] is True  # вернулся

    # общий лимит max_uses=1 (другой пассажир)
    p2 = make_passenger(client, "+77077777777", "Второй")
    rid = create_ride(client, passenger, promo_code="ONCE").json()["id"]
    v = client.post(f"{API}/promos/validate", headers=p2["h"], json={"code": "ONCE", "order_amount": 1000}).json()
    assert v["error_code"] == "PROMO_EXHAUSTED"
    client.post(f"{API}/rides/{rid}/cancel", headers=passenger["h"])

    # счётчик использований в админке
    complete_ride(client, passenger, driver, promo_code="FIX100")
    used = {p["code"]: p["used_count"] for p in client.get(f"{API}/admin/promo-codes", headers=admin["h"]).json()}
    assert used["FIX100"] == 1 and used["ONCE"] == 0

    # деактивация через PATCH
    pid = next(p["id"] for p in client.get(f"{API}/admin/promo-codes", headers=admin["h"]).json() if p["code"] == "HALF")
    assert client.patch(f"{API}/admin/promo-codes/{pid}", headers=admin["h"], json={"is_active": False}).json()["is_active"] is False
    assert validate("HALF")["error_code"] == "PROMO_EXPIRED"

    # валидация создания: 150% и дубликат
    bad = client.post(f"{API}/admin/promo-codes", headers=admin["h"], json={"code": "X150", "discount_type": "percent", "value": 150})
    assert bad.status_code == 422
    dup = client.post(f"{API}/admin/promo-codes", headers=admin["h"], json={"code": "fix100", "discount_type": "fixed", "value": 1})
    assert dup.status_code == 409


def test_promotions_banners(client):
    admin, passenger = make_admin(client), make_passenger(client)
    now = utcnow()
    for title, start, end, active in [
        ("Активная", None, None, True),
        ("Прошла", now - timedelta(days=9), now - timedelta(days=1), True),
        ("Будет", now + timedelta(days=1), None, True),
        ("Выключена", None, None, False),
    ]:
        r = client.post(f"{API}/admin/promotions", headers=admin["h"], json={
            "title": title, "promo_code": "summer", "is_active": active,
            "starts_at": start.isoformat() if start else None, "ends_at": end.isoformat() if end else None,
        })
        assert r.status_code == 201
    shown = client.get(f"{API}/promotions", headers=passenger["h"]).json()
    assert [p["title"] for p in shown] == ["Активная"] and shown[0]["promo_code"] == "SUMMER"
    assert len(client.get(f"{API}/admin/promotions", headers=admin["h"]).json()) == 4
    assert client.get(f"{API}/promotions").status_code == 401


# ====================================================================== состояния и гонки
def test_accept_race_and_cancellation(client):
    admin, passenger = make_admin(client), make_passenger(client)
    d1 = make_driver(client, admin, "+77022222222", "111AAA01")
    d2 = make_driver(client, admin, "+77033333333", "222BBB01")

    rid = create_ride(client, passenger).json()["id"]
    assert client.post(f"{API}/rides/{rid}/accept", headers=d1["h"]).status_code == 200
    r = client.post(f"{API}/rides/{rid}/accept", headers=d2["h"])  # второй опоздал
    assert r.status_code == 409 and r.json()["error"]["code"] == "RIDE_NOT_AVAILABLE"
    # чужой водитель не может двигать статус и видеть заказ
    assert client.post(f"{API}/rides/{rid}/arrived", headers=d2["h"]).status_code == 404
    assert client.get(f"{API}/rides/{rid}", headers=d2["h"]).status_code == 404
    # водитель на заказе не может взять второй и уйти с линии
    assert client.post(f"{API}/rides/{rid}/accept", headers=d1["h"]).status_code == 409
    assert client.post(f"{API}/drivers/me/status", headers=d1["h"], json={"is_online": False}).json()["error"]["code"] == "ACTIVE_RIDE_EXISTS"

    # водитель отменяет -> заказ cancelled, пассажир может заказать снова
    r = client.post(f"{API}/rides/{rid}/cancel", headers=d1["h"], json={"reason": "Поломка"})
    assert r.json()["status"] == "cancelled" and r.json()["cancelled_by"] == "driver"
    assert create_ride(client, passenger).status_code == 201

    # отменённый заказ не виден в доступных; повторная отмена невозможна
    assert client.get(f"{API}/rides/available", headers=d1["h"]).json() != []
    assert client.post(f"{API}/rides/{rid}/cancel", headers=passenger["h"]).status_code == 409


def test_available_filters_and_car_class(client):
    admin, passenger = make_admin(client), make_passenger(client)
    eco = make_driver(client, admin, "+77022222222", "111AAA01", car_class="economy")
    biz = make_driver(client, admin, "+77033333333", "222BBB01", car_class="business")

    rid = create_ride(client, passenger, tariff_code="comfort").json()["id"]
    assert client.get(f"{API}/rides/available", headers=eco["h"]).json() == []  # эконом не берёт комфорт
    r = client.post(f"{API}/rides/{rid}/accept", headers=eco["h"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "CAR_CLASS_MISMATCH"
    assert [a["id"] for a in client.get(f"{API}/rides/available", headers=biz["h"]).json()] == [rid]  # бизнес берёт комфорт

    # далеко от водителя -> не показывается
    far = {"lat": 51.0, "lng": 71.0}
    client.post(f"{API}/drivers/me/location", headers=biz["h"], json=far)
    assert client.get(f"{API}/rides/available", headers=biz["h"]).json() == []

    # оффлайн-водитель
    client.post(f"{API}/drivers/me/status", headers=eco["h"], json={"is_online": False})
    assert client.get(f"{API}/rides/available", headers=eco["h"]).json()["error"]["code"] == "DRIVER_OFFLINE"


def test_route_validation(client):
    passenger = make_passenger(client)
    est = lambda **kw: client.post(f"{API}/rides/estimate", headers=passenger["h"], json={**ROUTE, **kw})
    assert est().status_code == 200
    assert est(distance_km=1.0).json()["error"]["code"] == "ROUTE_INVALID"  # меньше прямой (~4.3 км)
    assert est(duration_min=1).json()["error"]["code"] == "ROUTE_INVALID"  # 300 км/ч
    same = {"pickup": ROUTE["pickup"], "dropoff": ROUTE["pickup"]}
    assert est(**same).json()["error"]["code"] == "ROUTE_TOO_SHORT"
    assert est(pickup={"lat": 95, "lng": 0}).status_code == 422  # некорректная широта
    assert client.post(f"{API}/rides", headers=passenger["h"], json={**ROUTE, "tariff_code": "nope"}).json()["error"]["code"] == "TARIFF_NOT_FOUND"
    assert client.post(f"{API}/rides", headers=passenger["h"], json={**ROUTE, "tariff_code": "economy", "payment_method": "card"}).status_code == 422


def test_admin_tariff_update_changes_price(client):
    admin, passenger = make_admin(client), make_passenger(client)
    r = client.patch(f"{API}/admin/tariffs/economy", headers=admin["h"], json={"per_km": 200, "min_fare": 0})
    assert r.json()["per_km"] == 200
    eco = client.post(f"{API}/rides/estimate", headers=passenger["h"], json=ROUTE).json()["items"][0]
    assert eco["price"] == 300 + 200 * 5 + 15 * 12  # 1480
    client.patch(f"{API}/admin/tariffs/economy", headers=admin["h"], json={"is_active": False})
    codes = [i["tariff_code"] for i in client.post(f"{API}/rides/estimate", headers=passenger["h"], json=ROUTE).json()["items"]]
    assert "economy" not in codes
