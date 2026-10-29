from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_admin
from ..errors import AppError
from ..models import (
    DocStatus,
    DriverDocument,
    DriverProfile,
    DriverStatus,
    PromoCode,
    PromoRedemption,
    Promotion,
    Ride,
    Role,
    Tariff,
    User,
)
from ..schemas import (
    BlockIn,
    DocumentReviewIn,
    DriverOut,
    PromoCodeIn,
    PromoCodeOut,
    PromoCodeUpdateIn,
    PromotionIn,
    PromotionOut,
    PromotionUpdateIn,
    TariffOut,
    TariffUpdateIn,
)
from ..services import drivers as drivers_svc
from ..services.notifications import notify
from ..utils import utcnow

router = APIRouter(prefix="/admin", tags=["admin"])


# ------------------------------------------------------------------ водители
@router.get("/drivers", response_model=list[DriverOut])
def list_drivers(
    status: DriverStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Очередь модерации: ?status=pending."""
    q = select(User).join(DriverProfile, DriverProfile.user_id == User.id)
    if status:
        q = q.where(DriverProfile.status == status)
    users = db.scalars(q.order_by(User.id).limit(limit).offset(offset)).all()
    return [drivers_svc.serialize_driver(u) for u in users]


def _get_driver(db: Session, driver_id: int) -> User:
    user = db.get(User, driver_id)
    if user is None or user.driver_profile is None:
        raise AppError(404, "DRIVER_NOT_FOUND", "Водитель не найден")
    return user


@router.get("/drivers/{driver_id}", response_model=DriverOut)
def get_driver(driver_id: int, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    return drivers_svc.serialize_driver(_get_driver(db, driver_id))


@router.post("/documents/{doc_id}/review", response_model=DriverOut)
def review_document(
    doc_id: int,
    body: DocumentReviewIn,
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    """Одобрить/отклонить документ. Когда одобрены все 3 — водитель получает статус approved."""
    doc = db.get(DriverDocument, doc_id)
    if doc is None:
        raise AppError(404, "DOCUMENT_NOT_FOUND", "Документ не найден")
    doc.status = DocStatus.APPROVED if body.approved else DocStatus.REJECTED
    doc.reject_reason = None if body.approved else body.reason
    doc.reviewed_at = utcnow()
    doc.reviewed_by = admin.id

    profile = doc.driver
    was = profile.status
    drivers_svc.refresh_driver_status(profile)
    db.commit()

    user = db.get(User, profile.user_id)
    if profile.status != was:
        if profile.status == DriverStatus.APPROVED:
            notify(user, "Профиль подтверждён", "Можно выходить на линию")
        elif profile.status == DriverStatus.REJECTED:
            notify(user, "Документ отклонён", body.reason or "Перезагрузите документ")
    return drivers_svc.serialize_driver(user)


@router.post("/drivers/{driver_id}/block", response_model=DriverOut)
def block_driver(
    driver_id: int, body: BlockIn, _: User = Depends(current_admin), db: Session = Depends(get_db)
):
    user = _get_driver(db, driver_id)
    p = user.driver_profile
    p.status, p.block_reason, p.is_online = DriverStatus.BLOCKED, body.reason, False
    db.commit()
    return drivers_svc.serialize_driver(user)


@router.post("/drivers/{driver_id}/unblock", response_model=DriverOut)
def unblock_driver(driver_id: int, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    user = _get_driver(db, driver_id)
    p = user.driver_profile
    p.block_reason = None
    p.status = DriverStatus.INCOMPLETE  # заглушка, ниже статус пересчитается по документам
    drivers_svc.refresh_driver_status(p)
    db.commit()
    return drivers_svc.serialize_driver(user)


# ------------------------------------------------------------------ промокоды
def _promo_out(db: Session, promo: PromoCode, used: int | None = None) -> PromoCodeOut:
    if used is None:
        used = db.scalar(
            select(func.count()).select_from(PromoRedemption).where(PromoRedemption.promo_id == promo.id)
        )
    out = PromoCodeOut.model_validate(promo)
    out.used_count = used
    return out


@router.post("/promo-codes", response_model=PromoCodeOut, status_code=201)
def create_promo(body: PromoCodeIn, _: User = Depends(current_admin), db: Session = Depends(get_db)):
    data = body.model_dump()
    data["code"] = body.code.upper()
    promo = PromoCode(**data)
    db.add(promo)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "PROMO_EXISTS", "Промокод с таким кодом уже есть")
    return _promo_out(db, promo, 0)


@router.get("/promo-codes", response_model=list[PromoCodeOut])
def list_promos(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    used = dict(
        db.execute(
            select(PromoRedemption.promo_id, func.count()).group_by(PromoRedemption.promo_id)
        ).all()
    )
    promos = db.scalars(select(PromoCode).order_by(PromoCode.id.desc())).all()
    return [_promo_out(db, p, used.get(p.id, 0)) for p in promos]


@router.patch("/promo-codes/{promo_id}", response_model=PromoCodeOut)
def update_promo(
    promo_id: int,
    body: PromoCodeUpdateIn,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    promo = db.get(PromoCode, promo_id)
    if promo is None:
        raise AppError(404, "PROMO_NOT_FOUND", "Промокод не найден")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(promo, field, value)
    db.commit()
    return _promo_out(db, promo)


# ------------------------------------------------------------------ акции
@router.post("/promotions", response_model=PromotionOut, status_code=201)
def create_promotion(
    body: PromotionIn, _: User = Depends(current_admin), db: Session = Depends(get_db)
):
    item = Promotion(**body.model_dump())
    if item.promo_code:
        item.promo_code = item.promo_code.upper()
    db.add(item)
    db.commit()
    return item


@router.get("/promotions", response_model=list[PromotionOut])
def list_promotions(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    return db.scalars(select(Promotion).order_by(Promotion.id.desc())).all()


@router.patch("/promotions/{promotion_id}", response_model=PromotionOut)
def update_promotion(
    promotion_id: int,
    body: PromotionUpdateIn,
    _: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    item = db.get(Promotion, promotion_id)
    if item is None:
        raise AppError(404, "PROMOTION_NOT_FOUND", "Акция не найдена")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value.upper() if field == "promo_code" and value else value)
    db.commit()
    return item


@router.delete("/promotions/{promotion_id}", status_code=204)
def delete_promotion(
    promotion_id: int, _: User = Depends(current_admin), db: Session = Depends(get_db)
):
    item = db.get(Promotion, promotion_id)
    if item is None:
        raise AppError(404, "PROMOTION_NOT_FOUND", "Акция не найдена")
    db.delete(item)
    db.commit()


# ------------------------------------------------------------------ тарифы
@router.get("/tariffs", response_model=list[TariffOut])
def list_tariffs(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    return db.scalars(select(Tariff).order_by(Tariff.rank)).all()


@router.patch("/tariffs/{code}", response_model=TariffOut)
def update_tariff(
    code: str, body: TariffUpdateIn, _: User = Depends(current_admin), db: Session = Depends(get_db)
):
    tariff = db.scalar(select(Tariff).where(Tariff.code == code))
    if tariff is None:
        raise AppError(404, "TARIFF_NOT_FOUND", "Тариф не найден")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(tariff, field, value)
    db.commit()
    return tariff


# ------------------------------------------------------------------ статистика
@router.get("/stats")
def stats(_: User = Depends(current_admin), db: Session = Depends(get_db)):
    def grouped(col):
        return {str(k.value if hasattr(k, "value") else k): n for k, n in db.execute(
            select(col, func.count()).group_by(col)
        ).all()}

    return {
        "users_by_role": grouped(User.role),
        "drivers_by_status": grouped(DriverProfile.status),
        "rides_by_status": grouped(Ride.status),
        "online_drivers": db.scalar(
            select(func.count()).select_from(DriverProfile).where(DriverProfile.is_online)
        ),
        "passengers": db.scalar(
            select(func.count()).select_from(User).where(User.role == Role.PASSENGER)
        ),
    }
