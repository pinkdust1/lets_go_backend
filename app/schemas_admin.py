from datetime import datetime

from pydantic import BaseModel, Field, StrictFloat, StrictInt

from .models import PaymentMethod, RideStatus, Role, DriverStatus
from .schemas import DriverOut


# ------------------------------------------------------------------ dashboard
class DashboardKpi(BaseModel):
    value: float
    previous: float  # то же за предыдущий период такой же длины — для динамики


class DashboardSeriesPoint(BaseModel):
    date: str  # YYYY-MM-DD в часовом поясе админа
    rides: int  # создано заказов
    completed: int
    cancelled: int
    gross: int  # выручка по завершённым
    new_users: int


class TopDriver(BaseModel):
    id: int
    name: str | None
    phone: str
    rides: int
    earnings: int
    rating_avg: float | None


class DashboardOut(BaseModel):
    days: int
    currency: str
    rides_created: DashboardKpi
    rides_completed: DashboardKpi
    gross: DashboardKpi
    platform_fee: int
    discounts: int
    avg_check: int
    cancel_rate: float  # % отменённых среди завершённых + отменённых
    new_passengers: int
    new_drivers: int
    online_drivers: int
    active_rides: int
    searching_rides: int
    pending_drivers: int
    total_passengers: int
    total_drivers: int
    approved_drivers: int
    series: list[DashboardSeriesPoint]
    top_drivers: list[TopDriver]


# ------------------------------------------------------------------ users
class AdminUserRow(BaseModel):
    id: int
    phone: str
    name: str | None
    role: Role
    is_active: bool
    created_at: datetime
    rating_avg: float | None
    rating_count: int
    rides_count: int  # завершённых поездок
    driver_status: DriverStatus | None
    is_online: bool | None


class AdminUserList(BaseModel):
    items: list[AdminUserRow]
    total: int


class AdminParty(BaseModel):
    id: int
    name: str | None
    phone: str


class AdminRideRow(BaseModel):
    id: int
    status: RideStatus
    created_at: datetime
    passenger: AdminParty
    driver: AdminParty | None
    pickup_address: str | None
    dropoff_address: str | None
    tariff_code: str
    distance_km: float
    final_price: int
    currency: str
    payment_method: PaymentMethod
    promo_code: str | None


class AdminRideList(BaseModel):
    items: list[AdminRideRow]
    total: int


class AdminRatingRow(BaseModel):
    from_role: str  # passenger | driver
    score: int
    comment: str | None
    tags: list[str]


class AdminRideDetail(AdminRideRow):
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    duration_min: float
    base_price: int
    discount: int
    platform_fee: int | None
    driver_earnings: int | None
    driver_car: str | None
    accepted_at: datetime | None
    arrived_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancelled_by: Role | None
    cancel_reason: str | None
    ratings: list[AdminRatingRow]


class AdminUserDetail(AdminUserRow):
    completed_rides: int
    cancelled_rides: int
    money_total: int  # потрачено (пассажир) / заработано (водитель)
    currency: str
    driver: DriverOut | None
    recent_rides: list[AdminRideRow]


class ActiveIn(BaseModel):
    is_active: bool


class AdminCancelIn(BaseModel):
    reason: str | None = Field(None, max_length=255)


# ------------------------------------------------------------------ settings
class SettingOut(BaseModel):
    key: str
    label: str
    description: str
    group: str
    kind: str
    unit: str
    minimum: float
    maximum: float
    value: float
    default: float
    is_custom: bool
    updated_at: datetime | None
    updated_by: str | None


class SettingsUpdateIn(BaseModel):
    # Strict: строку "10" или true молча в число не превращаем
    values: dict[str, StrictInt | StrictFloat] = Field(min_length=1)
