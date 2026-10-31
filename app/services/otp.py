import hmac
import logging
import secrets
from datetime import timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..errors import AppError
from ..models import OTPCode
from ..security import hash_otp
from ..utils import utcnow

log = logging.getLogger("taxi.otp")


class SMSSender(Protocol):
    def send(self, phone: str, text: str) -> None: ...


class ConsoleSMSSender:
    """Заглушка: пишет SMS в лог. В проде замените на клиент вашего SMS-провайдера."""

    def send(self, phone: str, text: str) -> None:
        log.warning("SMS -> %s: %s", phone, text)


sms_sender: SMSSender = ConsoleSMSSender()


def request_otp(db: Session, phone: str) -> str:
    now = utcnow()
    last = db.scalar(
        select(OTPCode).where(OTPCode.phone == phone).order_by(OTPCode.created_at.desc()).limit(1)
    )
    if last and (now - last.created_at).total_seconds() < settings.otp_resend_seconds:
        wait = settings.otp_resend_seconds - int((now - last.created_at).total_seconds())
        raise AppError(429, "OTP_TOO_SOON", f"Повторно запросить код можно через {wait} сек.")

    code = settings.dev_fixed_otp or f"{secrets.randbelow(10**6):06d}"
    db.add(
        OTPCode(
            phone=phone,
            code_hash=hash_otp(phone, code),
            expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
        )
    )
    db.commit()
    sms_sender.send(phone, f"Ваш код входа: {code}")
    return code


def verify_otp(db: Session, phone: str, code: str) -> None:
    otp = db.scalar(
        select(OTPCode)
        .where(OTPCode.phone == phone, OTPCode.used.is_(False))
        .order_by(OTPCode.created_at.desc())
        .limit(1)
    )
    if otp is None or otp.expires_at < utcnow():
        raise AppError(400, "OTP_INVALID", "Код не найден или истёк, запросите новый")
    if otp.attempts >= settings.otp_max_attempts:
        raise AppError(429, "OTP_ATTEMPTS_EXCEEDED", "Слишком много попыток, запросите новый код")

    otp.attempts += 1
    if not hmac.compare_digest(otp.code_hash, hash_otp(phone, code)):
        db.commit()  # фиксируем попытку, иначе перебор кода не ограничен
        raise AppError(400, "OTP_INVALID", "Неверный код")
    otp.used = True
    db.commit()
