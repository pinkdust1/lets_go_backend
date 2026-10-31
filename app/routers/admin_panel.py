"""API для веб-админки: дашборд, пользователи, заказы, настройки."""

import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..config import settings as env
from ..database import get_db
from ..deps import current_admin
from ..errors import AppError
from ..models import (
    DriverProfile,
    DriverStatus,
    PromoRedemption,
    Rating,
    Ride,
    RideStatus,
    Role,
    User,
)
from ..schemas_admin import (
    ActiveIn,
    AdminCancelIn,
    AdminParty,
    AdminRatingRow,
    AdminRideDetail,
    AdminRideList,
    AdminRideRow,
    AdminUserDetail,
    AdminUserList,
    AdminUserRow,
    DashboardKpi,
    DashboardOut,
    DashboardSeriesPoint,
    SettingOut,
    SettingsUpdateIn,
    TopDriver,
)
from ..services import app_settings
from ..services import drivers as drivers_svc
from ..services.notifications import notify
from ..utils import utcnow

router = APIRouter(prefix="/admin", tags=["admin-panel"])

LIVE = (RideStatus.ACCEPTED, RideStatus.ARRIVED, RideStatus.IN_PROGRESS)
ACTIVE = (RideStatus.SEARCHING, *LIVE)


def _count(db: Session, model, *conds) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*conds)) or 0


# ====================================================================== dashboard
@router.get("/dashboard", response_model=DashboardOut)
def dashboard(
    days: int = Query(7, ge=1, le=90),
    tz_offset_min: int = Query(0, ge=-840, le=840, description="Смещение часового пояса админа от UTC, минут"),
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Сводка за последние `days` дней (включая сегодня) и сравнение с предыдущим периодом.

    Дни считаются в часовом поясе админа (tz_offset_min), а не в UTC.
    """
    off = timedelta(minutes=tz_offset_min)
    today = (utcnow() + off).date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, time.min, tzinfo=timezone.utc) - off
    prev_start = start - timedelta(days=days)

    def local_day(dt: datetime) -> date:
        return (dt + off).date()

    series = {
        first + timedelta(days=i): dict(rides=0, completed=0, cancelled=0, gross=0, new_users=0)
        for i in range(days)
    }
    prev = dict(created=0, completed=0, gross=0)
    cur = dict(completed=0, cancelled=0, gross=0, fee=0, discounts=0)

    for created_at, in db.execute(select(Ride.created_at).where(Ride.created_at >= prev_start)):
        if created_at >= start:
            series[local_day(created_at)]["rides"] += 1
        else:
            prev["created"] += 1

    completed_rows = db.execute(
        select(Ride.completed_at, Ride.final_price, Ride.platform_fee, Ride.discount).where(
            Ride.status == RideStatus.COMPLETED, Ride.completed_at >= prev_start
        )
    )
    for completed_at, price, fee, discount in completed_rows:
        if completed_at >= start:
            day = series[local_day(completed_at)]
            day["completed"] += 1
            day["gross"] += price
            cur["completed"] += 1
            cur["gross"] += price
            cur["fee"] += fee or 0
            cur["discounts"] += discount
        else:
            prev["completed"] += 1
            prev["gross"] += price

    for cancelled_at, in db.execute(
        select(Ride.cancelled_at).where(Ride.status == RideStatus.CANCELLED, Ride.cancelled_at >= start)
    ):
        series[local_day(cancelled_at)]["cancelled"] += 1
        cur["cancelled"] += 1

    new_pass = new_drv = 0
    for created_at, role in db.execute(
        select(User.created_at, User.role).where(
            User.created_at >= start, User.role.in_([Role.PASSENGER, Role.DRIVER])
        )
    ):
        series[local_day(created_at)]["new_users"] += 1
        if role == Role.PASSENGER:
            new_pass += 1
        else:
            new_drv += 1

    finished = cur["completed"] + cur["cancelled"]
    top = db.execute(
        select(
            Ride.driver_id,
            func.count(Ride.id),
            func.coalesce(func.sum(Ride.driver_earnings), 0),
        )
        .where(Ride.status == RideStatus.COMPLETED, Ride.completed_at >= start)
        .group_by(Ride.driver_id)
        .order_by(func.sum(Ride.driver_earnings).desc())
        .limit(5)
    ).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([t[0] for t in top])))}

    return DashboardOut(
        days=days,
        currency=env.currency,
        rides_created=DashboardKpi(
            value=sum(d["rides"] for d in series.values()), previous=prev["created"]
        ),
        rides_completed=DashboardKpi(value=cur["completed"], previous=prev["completed"]),
        gross=DashboardKpi(value=cur["gross"], previous=prev["gross"]),
        platform_fee=cur["fee"],
        discounts=cur["discounts"],
        avg_check=cur["gross"] // cur["completed"] if cur["completed"] else 0,
        cancel_rate=round(cur["cancelled"] * 100 / finished, 1) if finished else 0.0,
        new_passengers=new_pass,
        new_drivers=new_drv,
        online_drivers=_count(db, DriverProfile, DriverProfile.is_online),
        active_rides=_count(db, Ride, Ride.status.in_(LIVE)),
        searching_rides=_count(db, Ride, Ride.status == RideStatus.SEARCHING),
        pending_drivers=_count(db, DriverProfile, DriverProfile.status == DriverStatus.PENDING),
        total_passengers=_count(db, User, User.role == Role.PASSENGER),
        total_drivers=_count(db, User, User.role == Role.DRIVER),
        approved_drivers=_count(db, DriverProfile, DriverProfile.status == DriverStatus.APPROVED),
        series=[DashboardSeriesPoint(date=d.isoformat(), **v) for d, v in series.items()],
        top_drivers=[
            TopDriver(
                id=driver_id,
                name=users[driver_id].name,
                phone=users[driver_id].phone,
                rides=n,
                earnings=int(earned),
                rating_avg=users[driver_id].rating_avg,
            )
            for driver_id, n, earned in top
            if driver_id in users
        ],
    )


# ====================================================================== users
def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def _user_conds(q: str | None):
    """Поиск по номеру (цифры) или по имени. Без регистронезависимого поиска кириллицы в SQLite —
    в PostgreSQL ilike работает полноценно."""
    if not q or not q.strip():
        return []
    q = q.strip()
    digits = re.sub(r"\D", "", q)
    if digits and not re.search(r"[^\d\s()+\-]", q):
        return [User.phone.like(_like(digits), escape="\\")]
    return [User.name.ilike(_like(q), escape="\\")]


def _user_rows(db: Session, users: list[User]) -> list[AdminUserRow]:
    ids = [u.id for u in users]
    as_passenger = dict(
        db.execute(
            select(Ride.passenger_id, func.count())
            .where(Ride.passenger_id.in_(ids), Ride.status == RideStatus.COMPLETED)
            .group_by(Ride.passenger_id)
        ).all()
    )
    as_driver = dict(
        db.execute(
            select(Ride.driver_id, func.count())
            .where(Ride.driver_id.in_(ids), Ride.status == RideStatus.COMPLETED)
            .group_by(Ride.driver_id)
        ).all()
    )
    rows = []
    for u in users:
        p = u.driver_profile
        rows.append(
            AdminUserRow(
                id=u.id, phone=u.phone, name=u.name, role=u.role, is_active=u.is_active,
                created_at=u.created_at, rating_avg=u.rating_avg, rating_count=u.rating_count,
                rides_count=as_passenger.get(u.id, 0) + as_driver.get(u.id, 0),
                driver_status=p.status if p else None,
                is_online=p.is_online if p else None,
            )
        )
    return rows


@router.get("/users", response_model=AdminUserList)
def list_users(
    role: Role | None = None,
    is_active: bool | None = None,
    driver_status: DriverStatus | None = None,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    conds = _user_conds(q)
    if role:
        conds.append(User.role == role)
    if is_active is not None:
        conds.append(User.is_active == is_active)
    base = select(User)
    count = select(func.count()).select_from(User)
    if driver_status:
        base = base.join(DriverProfile, DriverProfile.user_id == User.id)
        count = count.join(DriverProfile, DriverProfile.user_id == User.id)
        conds.append(DriverProfile.status == driver_status)
    total = db.scalar(count.where(*conds)) or 0
    users = db.scalars(base.where(*conds).order_by(User.id.desc()).limit(limit).offset(offset)).all()
    return AdminUserList(items=_user_rows(db, list(users)), total=total)


def _party(u: User | None) -> AdminParty | None:
    return AdminParty(id=u.id, name=u.name, phone=u.phone) if u else None


def _ride_row(r: Ride) -> AdminRideRow:
    return AdminRideRow(
        id=r.id, status=r.status, created_at=r.created_at,
        passenger=_party(r.passenger), driver=_party(r.driver),
        pickup_address=r.pickup_address, dropoff_address=r.dropoff_address,
        tariff_code=r.tariff_code, distance_km=r.distance_km, final_price=r.final_price,
        currency=r.currency, payment_method=r.payment_method,
        promo_code=r.promo.code if r.promo else None,
    )


RIDE_LOAD = (joinedload(Ride.passenger), joinedload(Ride.driver), joinedload(Ride.promo))


@router.get("/users/{user_id}", response_model=AdminUserDetail)
def user_detail(user_id: int, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise AppError(404, "USER_NOT_FOUND", "Пользователь не найден")
    is_driver = user.role == Role.DRIVER
    mine = Ride.driver_id == user.id if is_driver else Ride.passenger_id == user.id
    completed, money = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Ride.driver_earnings if is_driver else Ride.final_price), 0),
        ).where(mine, Ride.status == RideStatus.COMPLETED)
    ).one()
    recent = db.scalars(
        select(Ride)
        .options(*RIDE_LOAD)
        .where(or_(Ride.passenger_id == user.id, Ride.driver_id == user.id))
        .order_by(Ride.id.desc())
        .limit(10)
    ).unique().all()
    row = _user_rows(db, [user])[0]
    return AdminUserDetail(
        **row.model_dump(),
        completed_rides=completed,
        cancelled_rides=_count(db, Ride, mine, Ride.status == RideStatus.CANCELLED),
        money_total=int(money),
        currency=env.currency,
        driver=drivers_svc.serialize_driver(user) if user.driver_profile else None,
        recent_rides=[_ride_row(r) for r in recent],
    )


@router.post("/users/{user_id}/active", response_model=AdminUserRow)
def set_user_active(
    user_id: int,
    body: ActiveIn,
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Блокировка/разблокировка аккаунта: заблокированный не может пользоваться ни одним эндпоинтом."""
    user = db.get(User, user_id)
    if user is None:
        raise AppError(404, "USER_NOT_FOUND", "Пользователь не найден")
    if user.role == Role.ADMIN:
        raise AppError(403, "FORBIDDEN", "Администраторов блокировать нельзя")
    if not body.is_active:
        column = Ride.driver_id if user.role == Role.DRIVER else Ride.passenger_id
        if _count(db, Ride, column == user.id, Ride.status.in_(ACTIVE)):
            raise AppError(
                409, "ACTIVE_RIDE_EXISTS", "У пользователя есть активный заказ — сначала отмените его"
            )
        if user.driver_profile:
            user.driver_profile.is_online = False
    user.is_active = body.is_active
    db.commit()
    return _user_rows(db, [user])[0]


# ====================================================================== rides
def _ride_conds(
    status: RideStatus | None, q: str | None, date_from: date | None, date_to: date | None, tz: int
):
    conds = []
    if status:
        conds.append(Ride.status == status)
    if q and q.strip():
        q = q.strip()
        digits = re.sub(r"\D", "", q)
        by_number = re.fullmatch(r"[#№]\s*(\d{1,9})", q)
        if by_number:  # "#42" — номер заказа
            conds.append(Ride.id == int(by_number.group(1)))
        elif digits and not re.search(r"[^\d\s()+\-]", q):  # цифры — фрагмент телефона
            phone_ids = select(User.id).where(User.phone.like(_like(digits), escape="\\"))
            conds.append(or_(Ride.passenger_id.in_(phone_ids), Ride.driver_id.in_(phone_ids)))
        else:
            name_ids = select(User.id).where(User.name.ilike(_like(q), escape="\\"))
            conds.append(or_(Ride.passenger_id.in_(name_ids), Ride.driver_id.in_(name_ids)))
    off = timedelta(minutes=tz)
    if date_from:
        conds.append(Ride.created_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc) - off)
    if date_to:
        conds.append(
            Ride.created_at
            < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc) - off
        )
    return conds


@router.get("/rides", response_model=AdminRideList)
def list_rides(
    status: RideStatus | None = None,
    q: str | None = Query(None, max_length=100, description="#номер заказа, фрагмент телефона или имя"),
    date_from: date | None = None,
    date_to: date | None = None,
    tz_offset_min: int = Query(0, ge=-840, le=840),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    conds = _ride_conds(status, q, date_from, date_to, tz_offset_min)
    total = _count(db, Ride, *conds)
    rides = db.scalars(
        select(Ride).options(*RIDE_LOAD).where(*conds).order_by(Ride.id.desc()).limit(limit).offset(offset)
    ).unique().all()
    return AdminRideList(items=[_ride_row(r) for r in rides], total=total)


def _ride_detail(db: Session, ride: Ride) -> AdminRideDetail:
    ratings = db.scalars(select(Rating).where(Rating.ride_id == ride.id).order_by(Rating.id)).all()
    car = None
    if ride.driver and ride.driver.driver_profile:
        p = ride.driver.driver_profile
        car = " ".join(x for x in (p.car_make, p.car_model, p.car_color, p.plate_number) if x) or None
    return AdminRideDetail(
        **_ride_row(ride).model_dump(),
        pickup_lat=ride.pickup_lat, pickup_lng=ride.pickup_lng,
        dropoff_lat=ride.dropoff_lat, dropoff_lng=ride.dropoff_lng,
        duration_min=ride.duration_min, base_price=ride.base_price, discount=ride.discount,
        platform_fee=ride.platform_fee, driver_earnings=ride.driver_earnings, driver_car=car,
        accepted_at=ride.accepted_at, arrived_at=ride.arrived_at, started_at=ride.started_at,
        completed_at=ride.completed_at, cancelled_at=ride.cancelled_at,
        cancelled_by=ride.cancelled_by, cancel_reason=ride.cancel_reason,
        ratings=[
            AdminRatingRow(
                from_role="passenger" if r.from_user_id == ride.passenger_id else "driver",
                score=r.score, comment=r.comment, tags=r.tags or [],
            )
            for r in ratings
        ],
    )


def _get_ride(db: Session, ride_id: int) -> Ride:
    ride = db.get(Ride, ride_id)
    if ride is None:
        raise AppError(404, "RIDE_NOT_FOUND", "Заказ не найден")
    return ride


@router.get("/rides/{ride_id}", response_model=AdminRideDetail)
def ride_detail(ride_id: int, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    return _ride_detail(db, _get_ride(db, ride_id))


@router.post("/rides/{ride_id}/cancel", response_model=AdminRideDetail)
def cancel_ride(
    ride_id: int,
    body: AdminCancelIn | None = None,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Принудительная отмена «зависшего» заказа в любом активном статусе (в т.ч. во время поездки)."""
    ride = _get_ride(db, ride_id)
    if ride.status not in ACTIVE:
        raise AppError(409, "INVALID_RIDE_STATE", "Завершённый или отменённый заказ изменить нельзя")
    ride.status = RideStatus.CANCELLED
    ride.cancelled_by = Role.ADMIN
    ride.cancel_reason = (body.reason if body else None) or "Отменён администратором"
    ride.cancelled_at = utcnow()
    for redemption in db.scalars(select(PromoRedemption).where(PromoRedemption.ride_id == ride.id)):
        db.delete(redemption)  # промокод возвращается пассажиру
    db.commit()
    notify(ride.passenger, "Заказ отменён", ride.cancel_reason)
    notify(ride.driver, "Заказ отменён", ride.cancel_reason)
    return _ride_detail(db, ride)


# ====================================================================== settings
@router.get("/settings", response_model=list[SettingOut])
def get_settings(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    return app_settings.list_all(db)


@router.patch("/settings", response_model=list[SettingOut])
def update_settings(
    body: SettingsUpdateIn, admin: User = Depends(current_admin), db: Session = Depends(get_db)
):
    """Меняет сразу несколько настроек (все значения проверяются до записи).

    Значение, равное умолчанию из .env, снимает переопределение. Новые значения применяются
    сразу и только к новым действиям: уже созданные заказы не пересчитываются.
    """
    app_settings.update(db, body.values, admin)
    return app_settings.list_all(db)
