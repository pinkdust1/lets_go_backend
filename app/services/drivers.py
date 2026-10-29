import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..errors import AppError
from ..models import DocStatus, DocType, DriverProfile, DriverStatus, Tariff, User
from ..schemas import DocumentOut, DriverOut

REQUIRED_DOCS = list(DocType)  # все три документа обязательны


def refresh_driver_status(driver: DriverProfile) -> None:
    """Единственное место, где вычисляется статус водителя (кроме ручной блокировки)."""
    if driver.status == DriverStatus.BLOCKED:
        return
    docs = {d.doc_type: d for d in driver.documents}
    profile_ok = all([driver.car_make, driver.car_model, driver.car_color, driver.plate_number])

    if not profile_ok or any(t not in docs for t in REQUIRED_DOCS):
        driver.status = DriverStatus.INCOMPLETE
    elif any(d.status == DocStatus.REJECTED for d in docs.values()):
        driver.status = DriverStatus.REJECTED
    elif all(d.status == DocStatus.APPROVED for d in docs.values()):
        driver.status = DriverStatus.APPROVED
    else:
        driver.status = DriverStatus.PENDING

    if driver.status != DriverStatus.APPROVED:
        driver.is_online = False


def serialize_driver(user: User) -> DriverOut:
    p = user.driver_profile
    have = {d.doc_type for d in p.documents}
    return DriverOut(
        user_id=user.id,
        phone=user.phone,
        full_name=user.name,
        status=p.status,
        block_reason=p.block_reason,
        is_online=p.is_online,
        car_make=p.car_make,
        car_model=p.car_model,
        car_color=p.car_color,
        car_year=p.car_year,
        plate_number=p.plate_number,
        car_class=p.car_class,
        rating_avg=user.rating_avg,
        rating_count=user.rating_count,
        documents=[DocumentOut.model_validate(d) for d in p.documents],
        missing_documents=[t for t in REQUIRED_DOCS if t not in have],
    )


def ensure_tariff_exists(db: Session, code: str) -> None:
    if not db.scalar(select(Tariff.id).where(Tariff.code == code, Tariff.is_active)):
        raise AppError(422, "TARIFF_NOT_FOUND", f"Класс авто «{code}» не существует")


# ------------------------------------------------------------ файлы документов
def sniff_extension(data: bytes) -> str | None:
    """Тип файла определяем по сигнатуре, а не по Content-Type от клиента."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return "heic"
    return None


def save_document_file(user_id: int, data: bytes, ext: str) -> str:
    folder = Path(settings.upload_dir) / "drivers" / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.{ext}"
    path.write_bytes(data)
    return str(path)


def delete_file_quietly(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
