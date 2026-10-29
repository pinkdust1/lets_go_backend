import hashlib
import hmac
from datetime import timedelta

import jwt

from .config import settings
from .errors import AppError
from .utils import utcnow

ALGORITHM = "HS256"


def hash_otp(phone: str, code: str) -> str:
    return hmac.new(
        settings.secret_key.encode(), f"{phone}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def create_token(user_id: int, token_type: str, lifetime: timedelta) -> str:
    now = utcnow()
    payload = {"sub": str(user_id), "type": token_type, "iat": now, "exp": now + lifetime}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str, expected_type: str) -> int:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AppError(401, "TOKEN_EXPIRED", "Сессия истекла, войдите заново")
    except jwt.PyJWTError:
        raise AppError(401, "TOKEN_INVALID", "Недействительный токен")
    if payload.get("type") != expected_type:
        raise AppError(401, "TOKEN_INVALID", "Недействительный токен")
    return int(payload["sub"])


def create_access_token(user_id: int) -> str:
    return create_token(user_id, "access", timedelta(minutes=settings.access_token_minutes))


def create_refresh_token(user_id: int) -> str:
    return create_token(user_id, "refresh", timedelta(days=settings.refresh_token_days))
