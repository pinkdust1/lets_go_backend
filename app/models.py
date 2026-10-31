from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .database import Base
from .utils import utcnow


# ---------------------------------------------------------------- enums
class Role(StrEnum):
    PASSENGER = "passenger"
    DRIVER = "driver"
    ADMIN = "admin"


class DriverStatus(StrEnum):
    INCOMPLETE = "incomplete"  # не заполнен профиль или загружены не все документы
    PENDING = "pending"  # всё загружено, ждёт проверки админом
    APPROVED = "approved"  # можно выходить на линию
    REJECTED = "rejected"  # хотя бы один документ отклонён — нужно перезагрузить
    BLOCKED = "blocked"  # заблокирован админом


class DocType(StrEnum):
    DRIVER_LICENSE = "driver_license"  # водительское удостоверение
    VEHICLE_REGISTRATION = "vehicle_registration"  # свидетельство о регистрации ТС
    ID_CARD = "id_card"  # удостоверение личности


class DocStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RideStatus(StrEnum):
    SEARCHING = "searching"
    ACCEPTED = "accepted"
    ARRIVED = "arrived"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DiscountType(StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"


class PaymentMethod(StrEnum):
    CASH = "cash"
    CARD = "card"


# ---------------------------------------------------------------- helpers
class UTCDateTime(TypeDecorator):
    """В БД хранится naive UTC, в Python всегда приходит aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


def enum_col(enum_cls, **kw):
    return mapped_column(
        Enum(
            enum_cls,
            native_enum=False,
            length=24,
            values_callable=lambda e: [m.value for m in e],
            validate_strings=True,
        ),
        **kw,
    )


# ---------------------------------------------------------------- tables
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    role: Mapped[Role] = enum_col(Role)
    name: Mapped[Optional[str]] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    fcm_token: Mapped[Optional[str]] = mapped_column(String(255))
    # кэш рейтинга (пересчитывается при каждой новой оценке); null = оценок ещё нет
    rating_avg: Mapped[Optional[float]] = mapped_column(Float)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    driver_profile: Mapped[Optional["DriverProfile"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class OTPCode(Base):
    __tablename__ = "otp_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DriverProfile(Base):
    __tablename__ = "driver_profiles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    car_make: Mapped[Optional[str]] = mapped_column(String(50))
    car_model: Mapped[Optional[str]] = mapped_column(String(50))
    car_color: Mapped[Optional[str]] = mapped_column(String(30))
    car_year: Mapped[Optional[int]] = mapped_column(Integer)
    plate_number: Mapped[Optional[str]] = mapped_column(String(20), unique=True)
    car_class: Mapped[str] = mapped_column(String(20), default="economy")  # = Tariff.code
    status: Mapped[DriverStatus] = enum_col(DriverStatus, default=DriverStatus.INCOMPLETE)
    block_reason: Mapped[Optional[str]] = mapped_column(String(255))
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    lat: Mapped[Optional[float]] = mapped_column(Float)
    lng: Mapped[Optional[float]] = mapped_column(Float)
    location_updated_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="driver_profile")
    documents: Mapped[list["DriverDocument"]] = relationship(
        back_populates="driver", cascade="all, delete-orphan"
    )


class DriverDocument(Base):
    __tablename__ = "driver_documents"
    __table_args__ = (UniqueConstraint("driver_id", "doc_type", name="uq_driver_doctype"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("driver_profiles.user_id", ondelete="CASCADE"), index=True
    )
    doc_type: Mapped[DocType] = enum_col(DocType)
    file_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[DocStatus] = enum_col(DocStatus, default=DocStatus.PENDING)
    reject_reason: Mapped[Optional[str]] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    reviewed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))

    driver: Mapped[DriverProfile] = relationship(back_populates="documents")


class Tariff(Base):
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(50))
    rank: Mapped[int] = mapped_column(Integer, default=1)  # водитель класса N берёт тарифы с rank <= N
    base_fare: Mapped[int] = mapped_column(Integer)  # подача
    per_km: Mapped[int] = mapped_column(Integer)
    per_min: Mapped[int] = mapped_column(Integer)
    min_fare: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # всегда UPPER
    description: Mapped[Optional[str]] = mapped_column(String(255))
    discount_type: Mapped[DiscountType] = enum_col(DiscountType)
    value: Mapped[int] = mapped_column(Integer)  # % или фикс. сумма
    max_discount: Mapped[Optional[int]] = mapped_column(Integer)  # потолок для процентов
    min_order: Mapped[int] = mapped_column(Integer, default=0)
    max_uses: Mapped[Optional[int]] = mapped_column(Integer)  # общий лимит, null = без лимита
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    first_ride_only: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    valid_until: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PromoRedemption(Base):
    """Использование промокода. Создаётся вместе с заказом, удаляется при его отмене."""

    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id", ondelete="CASCADE"), unique=True)
    discount_amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Promotion(Base):
    """Акция — баннер/новость в приложении (может ссылаться на промокод)."""

    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[Optional[str]] = mapped_column(Text)
    image_url: Mapped[Optional[str]] = mapped_column(String(500))
    promo_code: Mapped[Optional[str]] = mapped_column(String(32))
    starts_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    ends_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Ride(Base):
    __tablename__ = "rides"
    __table_args__ = (Index("ix_rides_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    passenger_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    driver_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[RideStatus] = enum_col(RideStatus, default=RideStatus.SEARCHING)

    pickup_lat: Mapped[float] = mapped_column(Float)
    pickup_lng: Mapped[float] = mapped_column(Float)
    pickup_address: Mapped[Optional[str]] = mapped_column(String(255))
    dropoff_lat: Mapped[float] = mapped_column(Float)
    dropoff_lng: Mapped[float] = mapped_column(Float)
    dropoff_address: Mapped[Optional[str]] = mapped_column(String(255))
    distance_km: Mapped[float] = mapped_column(Float)  # посчитано клиентом (маршрут уже есть в приложении)
    duration_min: Mapped[float] = mapped_column(Float)

    tariff_code: Mapped[str] = mapped_column(String(20))
    base_price: Mapped[int] = mapped_column(Integer)  # цена до скидки
    discount: Mapped[int] = mapped_column(Integer, default=0)
    final_price: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    promo_code_id: Mapped[Optional[int]] = mapped_column(ForeignKey("promo_codes.id"))
    payment_method: Mapped[PaymentMethod] = enum_col(PaymentMethod, default=PaymentMethod.CASH)

    platform_fee: Mapped[Optional[int]] = mapped_column(Integer)  # заполняется при завершении
    driver_earnings: Mapped[Optional[int]] = mapped_column(Integer)

    cancelled_by: Mapped[Optional[Role]] = enum_col(Role, nullable=True)
    cancel_reason: Mapped[Optional[str]] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    arrived_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)

    passenger: Mapped[User] = relationship(foreign_keys=[passenger_id])
    driver: Mapped[Optional[User]] = relationship(foreign_keys=[driver_id])
    promo: Mapped[Optional[PromoCode]] = relationship()


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (
        UniqueConstraint("ride_id", "from_user_id", name="uq_rating_ride_author"),
        CheckConstraint("score >= 1 AND score <= 5", name="ck_rating_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ride_id: Mapped[int] = mapped_column(ForeignKey("rides.id", ondelete="CASCADE"), index=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[Optional[str]] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppSetting(Base):
    """Настройки, которые админ меняет из панели. Нет строки -> действует значение по умолчанию из .env."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(64))  # JSON-число
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
