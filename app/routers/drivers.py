from datetime import timedelta
from typing import Annotated
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import approved_driver, current_driver, get_current_user
from ..errors import AppError
from ..models import DocStatus, DocType, DriverDocument, DriverStatus, Ride, RideStatus, Role, User
from ..schemas import (
    DocumentOut,
    DriverOut,
    DriverProfileIn,
    EarningsOut,
    LocationIn,
    OnlineIn,
)
from ..services import drivers as svc
from ..utils import utcnow

router = APIRouter(prefix="/drivers", tags=["drivers"])


@router.get("/me", response_model=DriverOut)
def my_profile(user: User = Depends(current_driver)):
    return svc.serialize_driver(user)


@router.put("/me/profile", response_model=DriverOut)
def save_profile(
    body: DriverProfileIn, user: User = Depends(current_driver), db: Session = Depends(get_db)
):
    """Анкета водителя и автомобиль. Можно вызывать повторно для исправления данных."""
    p = user.driver_profile
    if p.status == DriverStatus.BLOCKED:
        raise AppError(403, "DRIVER_BLOCKED", "Профиль заблокирован")
    svc.ensure_tariff_exists(db, body.car_class)

    user.name = body.full_name
    p.car_make, p.car_model = body.car_make, body.car_model
    p.car_color, p.car_year = body.car_color, body.car_year
    p.plate_number, p.car_class = body.plate_number, body.car_class
    svc.refresh_driver_status(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "PLATE_TAKEN", "Автомобиль с таким номером уже зарегистрирован")
    return svc.serialize_driver(user)


@router.post("/me/documents", response_model=DocumentOut, status_code=201)
def upload_document(
    doc_type: Annotated[DocType, Form()],
    file: Annotated[UploadFile, File()],
    user: User = Depends(current_driver),
    db: Session = Depends(get_db),
):
    """Загрузка одного из 3 документов (jpg/png/webp/heic/pdf, до MAX_UPLOAD_MB).

    Повторная загрузка того же типа заменяет файл и отправляет документ на перепроверку.
    """
    p = user.driver_profile
    if p.status == DriverStatus.BLOCKED:
        raise AppError(403, "DRIVER_BLOCKED", "Профиль заблокирован")

    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise AppError(413, "FILE_TOO_LARGE", f"Файл больше {settings.max_upload_mb} МБ")
    ext = svc.sniff_extension(data)
    if ext is None:
        raise AppError(415, "FILE_TYPE_UNSUPPORTED", "Допустимы JPG, PNG, WEBP, HEIC и PDF")

    path = svc.save_document_file(user.id, data, ext)
    doc = next((d for d in p.documents if d.doc_type == doc_type), None)
    if doc:
        svc.delete_file_quietly(doc.file_path)
        doc.file_path = path
        doc.status = DocStatus.PENDING
        doc.reject_reason = None
        doc.reviewed_at = None
        doc.reviewed_by = None
        doc.uploaded_at = utcnow()
    else:
        doc = DriverDocument(driver_id=user.id, doc_type=doc_type, file_path=path)
        db.add(doc)
        p.documents.append(doc)

    svc.refresh_driver_status(p)
    db.commit()
    return doc


@router.get("/documents/{doc_id}/file")
def download_document(
    doc_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Файл документа отдаётся только владельцу и админам (статик-раздачи нет)."""
    doc = db.get(DriverDocument, doc_id)
    if doc is None or (user.role != Role.ADMIN and doc.driver_id != user.id):
        raise AppError(404, "DOCUMENT_NOT_FOUND", "Документ не найден")
    if not Path(doc.file_path).exists():
        raise AppError(404, "FILE_MISSING", "Файл не найден")
    return FileResponse(doc.file_path)


@router.post("/me/status", response_model=DriverOut)
def set_online(
    body: OnlineIn, user: User = Depends(approved_driver), db: Session = Depends(get_db)
):
    p = user.driver_profile
    if body.is_online:
        if p.lat is None:
            raise AppError(409, "NO_LOCATION", "Сначала отправьте текущее местоположение")
        p.is_online = True
    else:
        busy = db.scalar(
            select(func.count())
            .select_from(Ride)
            .where(
                Ride.driver_id == user.id,
                Ride.status.in_([RideStatus.ACCEPTED, RideStatus.ARRIVED, RideStatus.IN_PROGRESS]),
            )
        )
        if busy:
            raise AppError(409, "ACTIVE_RIDE_EXISTS", "Нельзя уйти с линии во время заказа")
        p.is_online = False
    db.commit()
    return svc.serialize_driver(user)


@router.post("/me/location", status_code=204)
def update_location(
    body: LocationIn, user: User = Depends(approved_driver), db: Session = Depends(get_db)
):
    """Приложение водителя вызывает раз в 5–10 секунд, пока он на линии."""
    p = user.driver_profile
    p.lat, p.lng, p.location_updated_at = body.lat, body.lng, utcnow()
    db.commit()


@router.get("/me/earnings", response_model=EarningsOut)
def earnings(days: int = 7, user: User = Depends(current_driver), db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    since = utcnow() - timedelta(days=days)
    count, gross, fee, net = db.execute(
        select(
            func.count(Ride.id),
            func.coalesce(func.sum(Ride.final_price), 0),
            func.coalesce(func.sum(Ride.platform_fee), 0),
            func.coalesce(func.sum(Ride.driver_earnings), 0),
        ).where(
            Ride.driver_id == user.id,
            Ride.status == RideStatus.COMPLETED,
            Ride.completed_at >= since,
        )
    ).one()
    return EarningsOut(
        days=days,
        rides_count=count,
        gross=int(gross),
        platform_fee=int(fee),
        net=int(net),
        currency=settings.currency,
    )
