from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    DiscountType,
    DocStatus,
    DocType,
    DriverStatus,
    PaymentMethod,
    RideStatus,
    Role,
)
from .utils import normalize_phone

Phone = Annotated[str, AfterValidator(normalize_phone)]
Lat = Annotated[float, Field(ge=-90, le=90)]
Lng = Annotated[float, Field(ge=-180, le=180)]


# ------------------------------------------------------------------ auth
class OTPRequestIn(BaseModel):
    phone: Phone


class OTPRequestOut(BaseModel):
    expires_in: int
    debug_code: str | None = None  # только при DEBUG_OTP=true


class OTPVerifyIn(BaseModel):
    phone: Phone
    code: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")
    # Нужна только при первой регистрации; у существующего пользователя можно не передавать.
    role: Role | None = None
    name: str | None = Field(None, min_length=2, max_length=100)

    @field_validator("role")
    @classmethod
    def _no_admin_self_register(cls, v):
        if v == Role.ADMIN:
            raise ValueError("Роль admin назначается только через ADMIN_PHONES")
        return v


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: str
    role: Role
    name: str | None
    rating_avg: float | None
    rating_count: int
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class VerifyOut(TokenOut):
    user: UserOut
    is_new_user: bool


class UserUpdateIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class DeviceTokenIn(BaseModel):
    fcm_token: str = Field(min_length=10, max_length=255)


# ------------------------------------------------------------------ driver
class DriverProfileIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    car_make: str = Field(min_length=1, max_length=50)
    car_model: str = Field(min_length=1, max_length=50)
    car_color: str = Field(min_length=2, max_length=30)
    car_year: int = Field(ge=1980, le=2100)
    plate_number: str = Field(min_length=3, max_length=20)
    car_class: str = Field("economy", max_length=20)

    @field_validator("plate_number")
    @classmethod
    def _plate(cls, v: str) -> str:
        return v.replace(" ", "").upper()


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    doc_type: DocType
    status: DocStatus
    reject_reason: str | None
    uploaded_at: datetime
    reviewed_at: datetime | None


class DriverOut(BaseModel):
    user_id: int
    phone: str
    full_name: str | None
    status: DriverStatus
    block_reason: str | None
    is_online: bool
    car_make: str | None
    car_model: str | None
    car_color: str | None
    car_year: int | None
    plate_number: str | None
    car_class: str
    rating_avg: float | None
    rating_count: int
    documents: list[DocumentOut]
    missing_documents: list[DocType]


class OnlineIn(BaseModel):
    is_online: bool


class LocationIn(BaseModel):
    lat: Lat
    lng: Lng


class EarningsOut(BaseModel):
    days: int
    rides_count: int
    gross: int
    platform_fee: int
    net: int
    currency: str


# ------------------------------------------------------------------ rides
class Point(BaseModel):
    lat: Lat
    lng: Lng
    address: str | None = Field(None, max_length=255)


class RouteIn(BaseModel):
    """Маршрут уже рассчитан на клиенте — присылаем точки, расстояние и время."""

    pickup: Point
    dropoff: Point
    distance_km: float = Field(gt=0, le=1000)
    duration_min: float = Field(gt=0, le=1440)
    promo_code: str | None = Field(None, max_length=32)


class EstimateItem(BaseModel):
    tariff_code: str
    tariff_name: str
    price: int
    discount: int
    final_price: int
    promo_error: str | None = None


class EstimateOut(BaseModel):
    distance_km: float
    duration_min: float
    currency: str
    items: list[EstimateItem]


class RideCreateIn(RouteIn):
    tariff_code: str = Field(max_length=20)
    payment_method: PaymentMethod = PaymentMethod.CASH


class CancelIn(BaseModel):
    reason: str | None = Field(None, max_length=255)


class PartyBrief(BaseModel):
    id: int
    name: str | None
    phone: str | None  # виден только пока заказ активен (чтобы можно было позвонить)
    rating_avg: float | None
    rating_count: int


class DriverBrief(PartyBrief):
    car_make: str | None
    car_model: str | None
    car_color: str | None
    plate_number: str | None
    lat: float | None
    lng: float | None


class RideOut(BaseModel):
    id: int
    status: RideStatus
    pickup: Point
    dropoff: Point
    distance_km: float
    duration_min: float
    tariff_code: str
    base_price: int
    discount: int
    final_price: int
    currency: str
    promo_code: str | None
    payment_method: PaymentMethod
    passenger: PartyBrief
    driver: DriverBrief | None
    platform_fee: int | None  # только для водителя
    driver_earnings: int | None  # только для водителя
    cancelled_by: Role | None
    cancel_reason: str | None
    my_rating: int | None  # оценка, которую текущий пользователь уже поставил
    created_at: datetime
    accepted_at: datetime | None
    arrived_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None


class AvailableRideOut(BaseModel):
    id: int
    pickup: Point
    dropoff: Point
    distance_km: float
    duration_min: float
    tariff_code: str
    final_price: int
    currency: str
    payment_method: PaymentMethod
    passenger_name: str | None
    passenger_rating_avg: float | None
    distance_to_pickup_km: float
    created_at: datetime


# ------------------------------------------------------------------ promo
class PromoValidateIn(BaseModel):
    code: str = Field(max_length=32)
    order_amount: int = Field(gt=0)


class PromoValidateOut(BaseModel):
    valid: bool
    code: str
    discount: int = 0
    final_price: int | None = None
    error_code: str | None = None
    message: str | None = None


class PromoCodeIn(BaseModel):
    code: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = Field(None, max_length=255)
    discount_type: DiscountType
    value: int = Field(gt=0)
    max_discount: int | None = Field(None, gt=0)
    min_order: int = Field(0, ge=0)
    max_uses: int | None = Field(None, gt=0)
    per_user_limit: int = Field(1, gt=0)
    first_ride_only: bool = False
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.discount_type == DiscountType.PERCENT and self.value > 100:
            raise ValueError("Процент скидки не может быть больше 100")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until должен быть позже valid_from")
        return self


class PromoCodeUpdateIn(BaseModel):
    description: str | None = Field(None, max_length=255)
    max_discount: int | None = Field(None, gt=0)
    min_order: int | None = Field(None, ge=0)
    max_uses: int | None = Field(None, gt=0)
    per_user_limit: int | None = Field(None, gt=0)
    first_ride_only: bool | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool | None = None


class PromoCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    description: str | None
    discount_type: DiscountType
    value: int
    max_discount: int | None
    min_order: int
    max_uses: int | None
    per_user_limit: int
    first_ride_only: bool
    valid_from: datetime | None
    valid_until: datetime | None
    is_active: bool
    used_count: int = 0


class PromotionIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = None
    image_url: str | None = Field(None, max_length=500)
    promo_code: str | None = Field(None, max_length=32)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool = True
    sort_order: int = 0


class PromotionUpdateIn(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = None
    image_url: str | None = Field(None, max_length=500)
    promo_code: str | None = Field(None, max_length=32)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class PromotionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    image_url: str | None
    promo_code: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    is_active: bool
    sort_order: int


# ------------------------------------------------------------------ ratings
class RatingIn(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(None, max_length=500)
    tags: list[Annotated[str, Field(max_length=32)]] = Field(default_factory=list, max_length=5)


class RatingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ride_id: int
    score: int
    comment: str | None
    tags: list[str]
    created_at: datetime


class RatingSummaryOut(BaseModel):
    average: float | None
    count: int
    distribution: dict[int, int]  # {1: n, 2: n, ... 5: n}
    recent: list[RatingOut]


# ------------------------------------------------------------------ admin
class DocumentReviewIn(BaseModel):
    approved: bool
    reason: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _reason_required(self):
        if not self.approved and not self.reason:
            raise ValueError("Укажите причину отклонения")
        return self


class BlockIn(BaseModel):
    reason: str = Field(min_length=3, max_length=255)


class TariffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    rank: int
    base_fare: int
    per_km: int
    per_min: int
    min_fare: int
    is_active: bool


class TariffUpdateIn(BaseModel):
    name: str | None = Field(None, max_length=50)
    base_fare: int | None = Field(None, ge=0)
    per_km: int | None = Field(None, ge=0)
    per_min: int | None = Field(None, ge=0)
    min_fare: int | None = Field(None, ge=0)
    is_active: bool | None = None
