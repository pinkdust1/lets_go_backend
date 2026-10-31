from ..config import settings
from ..errors import AppError
from ..models import Tariff
from ..schemas import RouteIn
from ..utils import haversine_km


def calc_price(
    tariff: Tariff, distance_km: float, duration_min: float, round_to: int | None = None
) -> int:
    raw = tariff.base_fare + tariff.per_km * distance_km + tariff.per_min * duration_min
    raw = max(raw, tariff.min_fare)
    step = round_to or settings.price_round_to
    return int(round(raw / step)) * step


def validate_route(route: RouteIn, max_distance_km: float | None = None) -> None:
    """Расстояние и время присылает клиент, поэтому проверяем их на адекватность.

    Это не защита от целенаправленного обмана (для неё нужен расчёт маршрута на сервере,
    например через OSRM/Google Directions), но отсекает ошибки клиента и грубые подделки.
    """
    straight = haversine_km(
        route.pickup.lat, route.pickup.lng, route.dropoff.lat, route.dropoff.lng
    )
    if straight < 0.05:
        raise AppError(422, "ROUTE_TOO_SHORT", "Точки посадки и назначения совпадают")
    if route.distance_km < straight * 0.9:
        raise AppError(422, "ROUTE_INVALID", "Расстояние меньше прямой между точками")
    if route.distance_km > (max_distance_km or settings.max_ride_distance_km):
        raise AppError(422, "ROUTE_TOO_LONG", "Слишком длинный маршрут")
    # средняя скорость выше 150 км/ч или ниже 2 км/ч — явная ошибка в данных
    speed = route.distance_km / (route.duration_min / 60)
    if not 2 <= speed <= 150:
        raise AppError(422, "ROUTE_INVALID", "Некорректное время в пути")
