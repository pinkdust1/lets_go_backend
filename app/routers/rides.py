from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import approved_driver, current_passenger, get_current_user
from ..errors import AppError
from ..models import (
    PromoRedemption,
    Rating,
    Ride,
    RideStatus,
    Role,
    Tariff,
    User,
)
from ..schemas import (
    AvailableRideOut,
    CancelIn,
    DriverBrief,
    EstimateItem,
    EstimateOut,
    PartyBrief,
    Point,
    RideCreateIn,
    RideOut,
    RouteIn,
)
from ..services import promos
from ..services.notifications import notify
from ..services.pricing import calc_price, validate_route
from ..utils import haversine_km, utcnow

router = APIRouter(prefix="/rides", tags=["rides"])

ACTIVE = (RideStatus.SEARCHING, RideStatus.ACCEPTED, RideStatus.ARRIVED, RideStatus.IN_PROGRESS)
# телефон собеседника виден только пока заказ идёт
CONTACT_VISIBLE = (RideStatus.ACCEPTED, RideStatus.ARRIVED, RideStatus.IN_PROGRESS)


# ------------------------------------------------------------------ сериализация
def _party(user: User, show_phone: bool) -> PartyBrief:
    return PartyBrief(
        id=user.id,
        name=user.name,
        phone=user.phone if show_phone else None,
        rating_avg=user.rating_avg,
        rating_count=user.rating_count,
    )


def to_ride_out(db: Session, ride: Ride, viewer: User) -> RideOut:
    show_phone = ride.status in CONTACT_VISIBLE
    driver = None
    if ride.driver is not None:
        p = ride.driver.driver_profile
        driver = DriverBrief(
            **_party(ride.driver, show_phone).model_dump(),
            car_make=p.car_make,
            car_model=p.car_model,
            car_color=p.car_color,
            plate_number=p.plate_number,
            lat=p.lat if ride.status in CONTACT_VISIBLE else None,
            lng=p.lng if ride.status in CONTACT_VISIBLE else None,
        )
    my_rating = db.scalar(
        select(Rating.score).where(Rating.ride_id == ride.id, Rating.from_user_id == viewer.id)
    )
    is_driver = viewer.id == ride.driver_id
    return RideOut(
        id=ride.id,
        status=ride.status,
        pickup=Point(lat=ride.pickup_lat, lng=ride.pickup_lng, address=ride.pickup_address),
        dropoff=Point(lat=ride.dropoff_lat, lng=ride.dropoff_lng, address=ride.dropoff_address),
        distance_km=ride.distance_km,
        duration_min=ride.duration_min,
        tariff_code=ride.tariff_code,
        base_price=ride.base_price,
        discount=ride.discount,
        final_price=ride.final_price,
        currency=ride.currency,
        promo_code=ride.promo.code if ride.promo else None,
        payment_method=ride.payment_method,
        passenger=_party(ride.passenger, show_phone),
        driver=driver,
        platform_fee=ride.platform_fee if is_driver else None,
        driver_earnings=ride.driver_earnings if is_driver else None,
        cancelled_by=ride.cancelled_by,
        cancel_reason=ride.cancel_reason,
        my_rating=my_rating,
        created_at=ride.created_at,
        accepted_at=ride.accepted_at,
        arrived_at=ride.arrived_at,
        started_at=ride.started_at,
        completed_at=ride.completed_at,
        cancelled_at=ride.cancelled_at,
    )


def _get_ride_for_participant(db: Session, ride_id: int, user: User) -> Ride:
    ride = db.get(Ride, ride_id)
    # чужие заказы не раскрываем: для всех посторонних это просто 404
    if ride is None or user.id not in (ride.passenger_id, ride.driver_id):
        raise AppError(404, "RIDE_NOT_FOUND", "Заказ не найден")
    return ride


def _active_ride_count(db: Session, user: User) -> int:
    column = Ride.driver_id if user.role == Role.DRIVER else Ride.passenger_id
    return db.scalar(
        select(func.count()).select_from(Ride).where(column == user.id, Ride.status.in_(ACTIVE))
    )


def _wrong_state(expected: str) -> AppError:
    return AppError(409, "INVALID_RIDE_STATE", f"Действие недоступно: заказ должен быть в статусе «{expected}»")


# ------------------------------------------------------------------ пассажир
@router.post("/estimate", response_model=EstimateOut)
def estimate(
    body: RouteIn, user: User = Depends(current_passenger), db: Session = Depends(get_db)
):
    """Стоимость по всем тарифам (с учётом промокода, если передан)."""
    validate_route(body)
    tariffs = db.scalars(select(Tariff).where(Tariff.is_active).order_by(Tariff.rank)).all()
    items = []
    for t in tariffs:
        price = calc_price(t, body.distance_km, body.duration_min)
        _, discount, err = promos.try_discount(db, body.promo_code, user.id, price)
        items.append(
            EstimateItem(
                tariff_code=t.code,
                tariff_name=t.name,
                price=price,
                discount=discount,
                final_price=price - discount,
                promo_error=err.message if err else None,
            )
        )
    return EstimateOut(
        distance_km=body.distance_km,
        duration_min=body.duration_min,
        currency=settings.currency,
        items=items,
    )


@router.post("", response_model=RideOut, status_code=201)
def create_ride(
    body: RideCreateIn, user: User = Depends(current_passenger), db: Session = Depends(get_db)
):
    if body.payment_method.value != "cash":
        raise AppError(422, "PAYMENT_NOT_SUPPORTED", "Пока доступна только оплата наличными")
    validate_route(body)
    if _active_ride_count(db, user):
        raise AppError(409, "ACTIVE_RIDE_EXISTS", "У вас уже есть активный заказ")

    tariff = db.scalar(select(Tariff).where(Tariff.code == body.tariff_code, Tariff.is_active))
    if tariff is None:
        raise AppError(404, "TARIFF_NOT_FOUND", "Тариф не найден")

    price = calc_price(tariff, body.distance_km, body.duration_min)
    promo, discount = None, 0
    if body.promo_code:  # при создании заказа невалидный промокод — это ошибка, а не «мягкое» предупреждение
        promo = promos.find_promo(db, body.promo_code)
        discount = promos.check_and_calculate(db, promo, user.id, price)

    ride = Ride(
        passenger_id=user.id,
        pickup_lat=body.pickup.lat,
        pickup_lng=body.pickup.lng,
        pickup_address=body.pickup.address,
        dropoff_lat=body.dropoff.lat,
        dropoff_lng=body.dropoff.lng,
        dropoff_address=body.dropoff.address,
        distance_km=body.distance_km,
        duration_min=body.duration_min,
        tariff_code=tariff.code,
        base_price=price,
        discount=discount,
        final_price=price - discount,
        currency=settings.currency,
        promo_code_id=promo.id if promo else None,
        payment_method=body.payment_method,
    )
    db.add(ride)
    db.flush()
    if promo:
        db.add(
            PromoRedemption(
                promo_id=promo.id, user_id=user.id, ride_id=ride.id, discount_amount=discount
            )
        )
    db.commit()
    return to_ride_out(db, ride, user)


# ------------------------------------------------------------------ общие (пассажир и водитель)
@router.get("/active", response_model=RideOut | None)
def active_ride(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Текущий заказ или null — вызывать при старте приложения, чтобы восстановить экран."""
    if user.role not in (Role.PASSENGER, Role.DRIVER):
        raise AppError(403, "FORBIDDEN", "Недостаточно прав")
    column = Ride.driver_id if user.role == Role.DRIVER else Ride.passenger_id
    ride = db.scalar(
        select(Ride).where(column == user.id, Ride.status.in_(ACTIVE)).order_by(Ride.id.desc())
    )
    return to_ride_out(db, ride, user) if ride else None


@router.get("/history", response_model=list[RideOut])
def history(
    status: RideStatus | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in (Role.PASSENGER, Role.DRIVER):
        raise AppError(403, "FORBIDDEN", "Недостаточно прав")
    column = Ride.driver_id if user.role == Role.DRIVER else Ride.passenger_id
    q = select(Ride).where(column == user.id)
    if status:
        q = q.where(Ride.status == status)
    rides = db.scalars(q.order_by(Ride.id.desc()).limit(limit).offset(offset)).all()
    return [to_ride_out(db, r, user) for r in rides]


# ------------------------------------------------------------------ водитель
@router.get("/available", response_model=list[AvailableRideOut])
def available_rides(driver: User = Depends(approved_driver), db: Session = Depends(get_db)):
    """Заказы в радиусе DRIVER_SEARCH_RADIUS_KM от водителя, ближайшие первыми.

    Поиск по радиусу сделан простым перебором открытых заказов — для старта этого достаточно.
    При росте нагрузки: PostGIS / geohash-индекс или рассылка заказов водителям по push/WebSocket.
    """
    p = driver.driver_profile
    if not p.is_online:
        raise AppError(409, "DRIVER_OFFLINE", "Выйдите на линию, чтобы видеть заказы")
    if p.lat is None or p.lng is None:
        raise AppError(409, "NO_LOCATION", "Сначала отправьте текущее местоположение")

    ranks = {t.code: t.rank for t in db.scalars(select(Tariff))}
    my_rank = ranks.get(p.car_class, 1)
    rides = db.scalars(
        select(Ride).where(Ride.status == RideStatus.SEARCHING).order_by(Ride.created_at).limit(500)
    ).all()

    result = []
    for r in rides:
        dist = haversine_km(p.lat, p.lng, r.pickup_lat, r.pickup_lng)
        if dist > settings.driver_search_radius_km or ranks.get(r.tariff_code, 99) > my_rank:
            continue
        result.append(
            AvailableRideOut(
                id=r.id,
                pickup=Point(lat=r.pickup_lat, lng=r.pickup_lng, address=r.pickup_address),
                dropoff=Point(lat=r.dropoff_lat, lng=r.dropoff_lng, address=r.dropoff_address),
                distance_km=r.distance_km,
                duration_min=r.duration_min,
                tariff_code=r.tariff_code,
                final_price=r.final_price,
                currency=r.currency,
                payment_method=r.payment_method,
                passenger_name=r.passenger.name,
                passenger_rating_avg=r.passenger.rating_avg,
                distance_to_pickup_km=round(dist, 2),
                created_at=r.created_at,
            )
        )
    return sorted(result, key=lambda x: x.distance_to_pickup_km)


@router.post("/{ride_id}/accept", response_model=RideOut)
def accept(ride_id: int, driver: User = Depends(approved_driver), db: Session = Depends(get_db)):
    p = driver.driver_profile
    if not p.is_online:
        raise AppError(409, "DRIVER_OFFLINE", "Выйдите на линию, чтобы принимать заказы")
    if _active_ride_count(db, driver):
        raise AppError(409, "ACTIVE_RIDE_EXISTS", "Сначала завершите текущий заказ")

    ride = db.get(Ride, ride_id)
    if ride is None:
        raise AppError(404, "RIDE_NOT_FOUND", "Заказ не найден")
    ranks = {t.code: t.rank for t in db.scalars(select(Tariff))}
    if ranks.get(ride.tariff_code, 99) > ranks.get(p.car_class, 1):
        raise AppError(403, "CAR_CLASS_MISMATCH", "Ваш автомобиль не подходит под тариф заказа")

    # Атомарный захват: UPDATE ... WHERE status='searching'. Если два водителя нажали
    # одновременно, rowcount=1 получит только один из них.
    res = db.execute(
        update(Ride)
        .where(Ride.id == ride_id, Ride.status == RideStatus.SEARCHING)
        .values(status=RideStatus.ACCEPTED, driver_id=driver.id, accepted_at=utcnow())
    )
    if res.rowcount == 0:
        db.rollback()
        raise AppError(409, "RIDE_NOT_AVAILABLE", "Заказ уже принят другим водителем или отменён")
    db.commit()
    db.refresh(ride)
    notify(ride.passenger, "Водитель найден", f"{driver.name or 'Водитель'} едет к вам")
    return to_ride_out(db, ride, driver)


def _driver_transition(
    db: Session, ride_id: int, driver: User, expected: RideStatus, new: RideStatus, ts_field: str
) -> Ride:
    ride = _get_ride_for_participant(db, ride_id, driver)
    if ride.driver_id != driver.id:
        raise AppError(404, "RIDE_NOT_FOUND", "Заказ не найден")
    if ride.status != expected:
        raise _wrong_state(expected.value)
    ride.status = new
    setattr(ride, ts_field, utcnow())
    return ride


@router.post("/{ride_id}/arrived", response_model=RideOut)
def arrived(ride_id: int, driver: User = Depends(approved_driver), db: Session = Depends(get_db)):
    ride = _driver_transition(db, ride_id, driver, RideStatus.ACCEPTED, RideStatus.ARRIVED, "arrived_at")
    db.commit()
    notify(ride.passenger, "Водитель приехал", "Выходите, вас ожидают")
    return to_ride_out(db, ride, driver)


@router.post("/{ride_id}/start", response_model=RideOut)
def start(ride_id: int, driver: User = Depends(approved_driver), db: Session = Depends(get_db)):
    ride = _driver_transition(db, ride_id, driver, RideStatus.ARRIVED, RideStatus.IN_PROGRESS, "started_at")
    db.commit()
    return to_ride_out(db, ride, driver)


@router.post("/{ride_id}/complete", response_model=RideOut)
def complete(ride_id: int, driver: User = Depends(approved_driver), db: Session = Depends(get_db)):
    ride = _driver_transition(
        db, ride_id, driver, RideStatus.IN_PROGRESS, RideStatus.COMPLETED, "completed_at"
    )
    ride.platform_fee = ride.final_price * settings.platform_commission_percent // 100
    ride.driver_earnings = ride.final_price - ride.platform_fee
    db.commit()
    notify(ride.passenger, "Поездка завершена", "Оцените поездку")
    return to_ride_out(db, ride, driver)


# ------------------------------------------------------------------ отмена
@router.post("/{ride_id}/cancel", response_model=RideOut)
def cancel(
    ride_id: int,
    body: CancelIn | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Пассажир: пока поиск / водитель едет / ждёт. Водитель: после принятия, до начала поездки.

    Во время самой поездки (in_progress) отмена недоступна.
    Использованный промокод при отмене возвращается пассажиру.
    """
    ride = _get_ride_for_participant(db, ride_id, user)
    allowed = (
        (RideStatus.SEARCHING, RideStatus.ACCEPTED, RideStatus.ARRIVED)
        if user.id == ride.passenger_id
        else (RideStatus.ACCEPTED, RideStatus.ARRIVED)
    )
    if ride.status not in allowed:
        raise AppError(409, "INVALID_RIDE_STATE", "Заказ в этом статусе отменить нельзя")

    other = ride.driver if user.id == ride.passenger_id else ride.passenger
    ride.status = RideStatus.CANCELLED
    ride.cancelled_by = user.role
    ride.cancel_reason = body.reason if body else None
    ride.cancelled_at = utcnow()
    for redemption in db.scalars(select(PromoRedemption).where(PromoRedemption.ride_id == ride.id)):
        db.delete(redemption)
    db.commit()
    notify(other, "Заказ отменён", ride.cancel_reason or "Заказ был отменён")
    return to_ride_out(db, ride, user)


# ------------------------------------------------------------------ один заказ (в конце: {ride_id} шире статических путей)
@router.get("/{ride_id}", response_model=RideOut)
def get_ride(ride_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return to_ride_out(db, _get_ride_for_participant(db, ride_id, user), user)
