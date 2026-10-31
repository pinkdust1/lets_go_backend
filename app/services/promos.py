from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import DiscountType, PromoCode, PromoRedemption, Ride, RideStatus
from ..utils import utcnow


def find_promo(db: Session, code: str) -> PromoCode:
    promo = db.scalar(select(PromoCode).where(PromoCode.code == code.strip().upper()))
    if promo is None:
        raise AppError(404, "PROMO_NOT_FOUND", "Промокод не найден")
    return promo


def calc_discount(promo: PromoCode, price: int) -> int:
    if promo.discount_type == DiscountType.PERCENT:
        discount = price * promo.value // 100
    else:
        discount = promo.value
    if promo.max_discount is not None:
        discount = min(discount, promo.max_discount)
    return max(0, min(discount, price))


def check_and_calculate(db: Session, promo: PromoCode, user_id: int, price: int) -> int:
    """Проверяет все условия промокода и возвращает размер скидки (или бросает AppError)."""
    now = utcnow()
    if (
        not promo.is_active
        or (promo.valid_from and now < promo.valid_from)
        or (promo.valid_until and now > promo.valid_until)
    ):
        raise AppError(400, "PROMO_EXPIRED", "Промокод недействителен или срок его действия истёк")

    if price < promo.min_order:
        raise AppError(
            400,
            "PROMO_MIN_ORDER",
            f"Промокод действует для заказов от {promo.min_order}",
        )

    if promo.max_uses is not None:
        total = db.scalar(
            select(func.count()).select_from(PromoRedemption).where(PromoRedemption.promo_id == promo.id)
        )
        if total >= promo.max_uses:
            raise AppError(400, "PROMO_EXHAUSTED", "Лимит использований промокода исчерпан")

    used_by_user = db.scalar(
        select(func.count())
        .select_from(PromoRedemption)
        .where(PromoRedemption.promo_id == promo.id, PromoRedemption.user_id == user_id)
    )
    if used_by_user >= promo.per_user_limit:
        raise AppError(400, "PROMO_ALREADY_USED", "Вы уже использовали этот промокод")

    if promo.first_ride_only:
        rides = db.scalar(
            select(func.count())
            .select_from(Ride)
            .where(Ride.passenger_id == user_id, Ride.status != RideStatus.CANCELLED)
        )
        if rides > 0:
            raise AppError(400, "PROMO_FIRST_RIDE_ONLY", "Промокод действует только на первую поездку")

    return calc_discount(promo, price)


def try_discount(
    db: Session, code: str | None, user_id: int, price: int
) -> tuple[PromoCode | None, int, AppError | None]:
    """Мягкая проверка для расчёта стоимости: ошибка возвращается, а не выбрасывается."""
    if not code:
        return None, 0, None
    try:
        promo = find_promo(db, code)
        return promo, check_and_calculate(db, promo, user_id, price), None
    except AppError as e:
        return None, 0, e
