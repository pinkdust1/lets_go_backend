from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_passenger, get_current_user
from ..models import Promotion, User
from ..schemas import PromoValidateIn, PromoValidateOut, PromotionOut
from ..services import promos
from ..utils import utcnow

router = APIRouter(tags=["promos"])


@router.post("/promos/validate", response_model=PromoValidateOut)
def validate_promo(
    body: PromoValidateIn, user: User = Depends(current_passenger), db: Session = Depends(get_db)
):
    """Проверка промокода на экране ввода. Всегда 200: результат в поле `valid`."""
    code = body.code.strip().upper()
    promo, discount, err = promos.try_discount(db, code, user.id, body.order_amount)
    if err:
        return PromoValidateOut(valid=False, code=code, error_code=err.code, message=err.message)
    return PromoValidateOut(
        valid=True, code=code, discount=discount, final_price=body.order_amount - discount
    )


@router.get("/promotions", response_model=list[PromotionOut])
def active_promotions(_: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Активные акции для баннеров в приложении."""
    now = utcnow()
    return db.scalars(
        select(Promotion)
        .where(
            Promotion.is_active,
            or_(Promotion.starts_at.is_(None), Promotion.starts_at <= now),
            or_(Promotion.ends_at.is_(None), Promotion.ends_at >= now),
        )
        .order_by(Promotion.sort_order, Promotion.id.desc())
    ).all()
