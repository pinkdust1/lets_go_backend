from datetime import timedelta

from app.utils import utcnow

from .conftest import (
    ROUTE,
    auth,
    complete_ride,
    create_ride,
    login,
    make_admin,
    make_driver,
    make_passenger,
)

API = "/api/v1"


def seed(client):
    admin = make_admin(client)
    passenger = make_passenger(client, "+77011111111", "Анна")
    driver = make_driver(client, admin, "+77022222222", "111AAA01")
    return admin, passenger, driver


# ====================================================================== доступ
def test_admin_panel_requires_admin(client):
    admin, passenger, driver = seed(client)
    for path in ("/admin/dashboard", "/admin/users", "/admin/rides", "/admin/settings"):
        assert client.get(f"{API}{path}").status_code == 401
        assert client.get(f"{API}{path}", headers=passenger["h"]).status_code == 403
        assert client.get(f"{API}{path}", headers=driver["h"]).status_code == 403
        assert client.get(f"{API}{path}", headers=admin["h"]).status_code == 200
    # сама страница отдаётся, а данные без токена — нет
    assert client.get("/admin-panel/").status_code == 200


# ====================================================================== дашборд
def test_dashboard_numbers(client):
    admin, passenger, driver = seed(client)
    empty = client.get(f"{API}/admin/dashboard?days=7", headers=admin["h"]).json()
    assert empty["gross"]["value"] == 0 and empty["avg_check"] == 0 and empty["cancel_rate"] == 0
    assert len(empty["series"]) == 7 and empty["top_drivers"] == []

    complete_ride(client, passenger, driver)  # 980
    complete_ride(client, passenger, driver)  # 980
    rid = create_ride(client, passenger).json()["id"]
    client.post(f"{API}/rides/{rid}/cancel", headers=passenger["h"])
    create_ride(client, passenger)  # висит в поиске

    d = client.get(f"{API}/admin/dashboard?days=7", headers=admin["h"]).json()
    assert d["rides_created"]["value"] == 4
    assert d["rides_completed"]["value"] == 2
    assert d["gross"]["value"] == 1960 and d["avg_check"] == 980
    assert d["platform_fee"] == 2 * (980 * 15 // 100)
    assert d["cancel_rate"] == 33.3  # 1 отмена на 3 завершённых заказа
    assert d["searching_rides"] == 1 and d["active_rides"] == 0
    assert d["online_drivers"] == 1 and d["approved_drivers"] == 1
    assert d["total_passengers"] == 1 and d["total_drivers"] == 1
    assert d["new_passengers"] == 1 and d["new_drivers"] == 1
    today = d["series"][-1]
    assert today["rides"] == 4 and today["completed"] == 2 and today["cancelled"] == 1 and today["gross"] == 1960
    assert d["top_drivers"][0]["id"] == driver["id"] and d["top_drivers"][0]["rides"] == 2
    assert d["gross"]["previous"] == 0

    # часовой пояс: в UTC-12 «сегодня» может быть вчерашним днём, но сумма за период не теряется
    shifted = client.get(f"{API}/admin/dashboard?days=7&tz_offset_min=720", headers=admin["h"]).json()
    assert sum(p["gross"] for p in shifted["series"]) == 1960

    assert client.get(f"{API}/admin/dashboard?days=0", headers=admin["h"]).status_code == 422
    assert client.get(f"{API}/admin/dashboard?days=91", headers=admin["h"]).status_code == 422


# ====================================================================== пользователи
def test_users_list_filters_and_detail(client):
    admin, passenger, driver = seed(client)
    p2 = make_passenger(client, "+77033333333", "Борис")
    complete_ride(client, passenger, driver)

    r = client.get(f"{API}/admin/users", headers=admin["h"]).json()
    assert r["total"] == 4 and [u["id"] for u in r["items"]] == sorted((u["id"] for u in r["items"]), reverse=True)

    def users(**params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return client.get(f"{API}/admin/users?{qs}", headers=admin["h"]).json()

    assert users(role="driver")["total"] == 1
    assert users(role="passenger")["total"] == 2
    assert users(driver_status="approved")["items"][0]["id"] == driver["id"]
    assert users(driver_status="pending")["total"] == 0
    assert users(q="7022")["items"][0]["id"] == driver["id"]  # по цифрам номера
    assert users(q="%2B7%20(703)")["items"][0]["id"] == p2["id"]  # +7 (703)
    assert users(q="Борис")["items"][0]["id"] == p2["id"]
    assert users(q="%25")["total"] == 0  # % не работает как подстановка
    assert users(limit=2, offset=0)["total"] == 4 and len(users(limit=2, offset=3)["items"]) == 1

    by_id = {u["id"]: u for u in users()["items"]}
    assert by_id[passenger["id"]]["rides_count"] == 1 and by_id[driver["id"]]["rides_count"] == 1
    assert by_id[driver["id"]]["driver_status"] == "approved" and by_id[driver["id"]]["is_online"] is True

    det = client.get(f"{API}/admin/users/{driver['id']}", headers=admin["h"]).json()
    assert det["completed_rides"] == 1 and det["money_total"] == 980 - 980 * 15 // 100
    assert det["driver"]["plate_number"] == "111AAA01" and len(det["driver"]["documents"]) == 3
    assert len(det["recent_rides"]) == 1
    pdet = client.get(f"{API}/admin/users/{passenger['id']}", headers=admin["h"]).json()
    assert pdet["money_total"] == 980 and pdet["driver"] is None
    assert client.get(f"{API}/admin/users/9999", headers=admin["h"]).status_code == 404


def test_block_user(client):
    admin, passenger, driver = seed(client)
    url = lambda uid: f"{API}/admin/users/{uid}/active"

    assert client.post(url(admin["id"]), headers=admin["h"], json={"is_active": False}).status_code == 403

    # нельзя заблокировать, пока идёт заказ
    rid = create_ride(client, passenger).json()["id"]
    r = client.post(url(passenger["id"]), headers=admin["h"], json={"is_active": False})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ACTIVE_RIDE_EXISTS"
    client.post(f"{API}/rides/{rid}/cancel", headers=passenger["h"])

    r = client.post(url(passenger["id"]), headers=admin["h"], json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert client.get(f"{API}/users/me", headers=passenger["h"]).json()["error"]["code"] == "ACCOUNT_DISABLED"
    code = client.post(f"{API}/auth/otp/request", json={"phone": "+77011111111"}).json()["debug_code"]
    r = client.post(f"{API}/auth/otp/verify", json={"phone": "+77011111111", "code": code})
    assert r.status_code == 403  # заново войти тоже нельзя

    # блокировка водителя снимает его с линии
    assert client.post(url(driver["id"]), headers=admin["h"], json={"is_active": False}).json()["is_online"] is False

    client.post(url(passenger["id"]), headers=admin["h"], json={"is_active": True})
    assert client.get(f"{API}/users/me", headers=passenger["h"]).status_code == 200


# ====================================================================== заказы
def test_rides_list_search_and_cancel(client):
    admin, passenger, driver = seed(client)
    p2 = make_passenger(client, "+77033333333", "Борис")
    rid1 = complete_ride(client, passenger, driver)
    rid2 = create_ride(client, p2, promo_code=None).json()["id"]

    def rides(**params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return client.get(f"{API}/admin/rides?{qs}", headers=admin["h"]).json()

    all_rides = rides()
    assert all_rides["total"] == 2 and [r["id"] for r in all_rides["items"]] == [rid2, rid1]
    assert all_rides["items"][1]["driver"]["phone"] == "+77022222222"
    assert all_rides["items"][0]["driver"] is None

    assert [r["id"] for r in rides(status="completed")["items"]] == [rid1]
    assert [r["id"] for r in rides(status="searching")["items"]] == [rid2]
    assert [r["id"] for r in rides(q=f"%23{rid2}")["items"]] == [rid2]  # "#2" — номер заказа
    assert [r["id"] for r in rides(q="%23999")["items"]] == []
    # просто цифры — это телефон, а не номер заказа: "2" есть в номере водителя заказа 1, но не в номерах заказа 2
    assert {r["id"] for r in rides(q=rid2)["items"]} == {rid1}
    assert {r["id"] for r in rides(q="7011")["items"]} == {rid1}  # по телефону пассажира
    assert {r["id"] for r in rides(q="7022")["items"]} == {rid1}  # по телефону водителя
    assert {r["id"] for r in rides(q="Борис")["items"]} == {rid2}

    today = utcnow().date()
    assert rides(date_from=today)["total"] == 2
    assert rides(date_from=today + timedelta(days=1))["total"] == 0
    assert rides(date_to=today - timedelta(days=1))["total"] == 0
    assert rides(date_to=today)["total"] == 2

    det = client.get(f"{API}/admin/rides/{rid1}", headers=admin["h"]).json()
    assert det["driver_car"] == "Toyota Camry белый 123ABC01" or det["driver_car"].endswith("111AAA01")
    assert det["platform_fee"] == 980 * 15 // 100 and det["completed_at"] and det["ratings"] == []
    client.post(f"{API}/rides/{rid1}/rating", headers=passenger["h"], json={"score": 4, "comment": "ок"})
    det = client.get(f"{API}/admin/rides/{rid1}", headers=admin["h"]).json()
    assert det["ratings"] == [{"from_role": "passenger", "score": 4, "comment": "ок", "tags": []}]

    # принудительная отмена зависшего заказа
    r = client.post(f"{API}/admin/rides/{rid2}/cancel", headers=admin["h"], json={"reason": "Тест"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["cancel_reason"] == "Тест"
    assert r.json()["cancelled_by"] == "admin"
    assert client.get(f"{API}/rides/active", headers=p2["h"]).json() is None
    assert client.post(f"{API}/admin/rides/{rid2}/cancel", headers=admin["h"]).status_code == 409
    assert client.post(f"{API}/admin/rides/{rid1}/cancel", headers=admin["h"]).status_code == 409
    assert client.get(f"{API}/admin/rides/9999", headers=admin["h"]).status_code == 404


def test_admin_cancel_releases_promo_even_in_progress(client):
    admin, passenger, driver = seed(client)
    client.post(f"{API}/admin/promo-codes", headers=admin["h"], json={"code": "ONE", "discount_type": "fixed", "value": 100})
    rid = create_ride(client, passenger, promo_code="ONE").json()["id"]
    for step in ("accept", "arrived", "start"):
        client.post(f"{API}/rides/{rid}/{step}", headers=driver["h"])
    assert client.post(f"{API}/admin/rides/{rid}/cancel", headers=admin["h"]).json()["status"] == "cancelled"
    v = client.post(f"{API}/promos/validate", headers=passenger["h"], json={"code": "ONE", "order_amount": 1000}).json()
    assert v["valid"] is True


# ====================================================================== настройки
def test_settings_read_update_reset_and_effect(client):
    admin, passenger, driver = seed(client)
    s = {x["key"]: x for x in client.get(f"{API}/admin/settings", headers=admin["h"]).json()}
    assert set(s) == {"platform_commission_percent", "price_round_to", "driver_search_radius_km",
                      "max_ride_distance_km", "rating_window_hours"}
    assert s["platform_commission_percent"]["value"] == 15 and s["platform_commission_percent"]["is_custom"] is False

    r = client.patch(f"{API}/admin/settings", headers=admin["h"], json={"values": {"platform_commission_percent": 25}})
    assert r.status_code == 200
    s = {x["key"]: x for x in r.json()}
    assert s["platform_commission_percent"]["value"] == 25 and s["platform_commission_percent"]["is_custom"] is True
    assert s["platform_commission_percent"]["updated_by"]

    # новая комиссия применяется к следующей поездке
    rid = complete_ride(client, passenger, driver)
    assert client.get(f"{API}/admin/rides/{rid}", headers=admin["h"]).json()["platform_fee"] == 980 * 25 // 100

    # округление цены
    client.patch(f"{API}/admin/settings", headers=admin["h"], json={"values": {"price_round_to": 100}})
    eco = client.post(f"{API}/rides/estimate", headers=passenger["h"], json=ROUTE).json()["items"][0]
    assert eco["price"] == 1000

    # радиус поиска: заказ в ~0.1 км от водителя виден при радиусе 5 и не виден при 0.5 -> 0.01 недопустимо
    assert create_ride(client, passenger).status_code == 201
    assert len(client.get(f"{API}/rides/available", headers=driver["h"]).json()) == 1
    client.patch(f"{API}/admin/settings", headers=admin["h"], json={"values": {"driver_search_radius_km": 0.5}})
    far = {"lat": 50.5, "lng": 70.5}
    client.post(f"{API}/drivers/me/location", headers=driver["h"], json=far)
    assert client.get(f"{API}/rides/available", headers=driver["h"]).json() == []

    # максимальная длина
    p2 = make_passenger(client, "+77033333333", "Борис")
    client.patch(f"{API}/admin/settings", headers=admin["h"], json={"values": {"max_ride_distance_km": 4.5}})
    r = client.post(f"{API}/rides", headers=p2["h"], json={**ROUTE, "tariff_code": "economy"})  # 5 км
    assert r.status_code == 422 and r.json()["error"]["code"] == "ROUTE_TOO_LONG"

    # возврат к значению по умолчанию снимает переопределение
    r = client.patch(f"{API}/admin/settings", headers=admin["h"], json={"values": {"platform_commission_percent": 15}})
    assert {x["key"]: x for x in r.json()}["platform_commission_percent"]["is_custom"] is False


def test_settings_validation_is_atomic(client):
    admin = make_admin(client)
    patch = lambda values: client.patch(f"{API}/admin/settings", headers=admin["h"], json={"values": values})

    for bad in (
        {"platform_commission_percent": 101},
        {"platform_commission_percent": -1},
        {"platform_commission_percent": 12.5},  # int-настройка
        {"driver_search_radius_km": 0},
        {"rating_window_hours": 100000},
        {"nonexistent": 1},
    ):
        r = patch(bad)
        assert r.status_code == 422, bad
    assert patch({}).status_code == 422
    assert patch({"platform_commission_percent": "10"}).status_code == 422
    assert patch({"platform_commission_percent": True}).status_code == 422

    # одна неверная настройка отменяет всю пачку
    r = patch({"rating_window_hours": 48, "platform_commission_percent": 500})
    assert r.status_code == 422
    s = {x["key"]: x for x in client.get(f"{API}/admin/settings", headers=admin["h"]).json()}
    assert s["rating_window_hours"]["value"] == 72 and s["rating_window_hours"]["is_custom"] is False

    assert patch({"driver_search_radius_km": 7.5}).json()
    assert {x["key"]: x for x in client.get(f"{API}/admin/settings", headers=admin["h"]).json()}["driver_search_radius_km"]["value"] == 7.5
