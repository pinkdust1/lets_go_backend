from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..errors import AppError
from ..models import DriverProfile, Role, User
from ..schemas import (
    DeviceTokenIn,
    OTPRequestIn,
    OTPRequestOut,
    OTPVerifyIn,
    RefreshIn,
    TokenOut,
    UserOut,
    UserUpdateIn,
    VerifyOut,
)
from ..security import create_access_token, create_refresh_token, decode_token
from ..services import otp as otp_service

router = APIRouter(tags=["auth"])


def _tokens(user: User) -> dict:
    return {
        "access_token": create_access_token(user.id),
        "refresh_token": create_refresh_token(user.id),
        "expires_in": settings.access_token_minutes * 60,
    }


@router.post("/auth/otp/request", response_model=OTPRequestOut)
def request_code(body: OTPRequestIn, db: Session = Depends(get_db)):
    code = otp_service.request_otp(db, body.phone)
    return OTPRequestOut(
        expires_in=settings.otp_ttl_seconds,
        debug_code=code if settings.debug_otp else None,
    )


@router.post("/auth/otp/verify", response_model=VerifyOut)
def verify_code(body: OTPVerifyIn, db: Session = Depends(get_db)):
    otp_service.verify_otp(db, body.phone, body.code)

    is_admin_phone = body.phone in settings.admin_phone_list
    user = db.scalar(select(User).where(User.phone == body.phone))
    is_new = user is None

    if user is None:
        role = Role.ADMIN if is_admin_phone else (body.role or Role.PASSENGER)
        user = User(phone=body.phone, role=role, name=body.name)
        db.add(user)
        db.flush()
        if role == Role.DRIVER:
            db.add(DriverProfile(user_id=user.id))
    else:
        if is_admin_phone and user.role != Role.ADMIN:
            user.role = Role.ADMIN
        elif body.role and user.role not in (body.role, Role.ADMIN):
            raise AppError(
                409,
                "ROLE_MISMATCH",
                "Этот номер уже зарегистрирован как "
                + ("водитель" if user.role == Role.DRIVER else "пассажир"),
            )
        if body.name and not user.name:
            user.name = body.name

    if not user.is_active:
        raise AppError(403, "ACCOUNT_DISABLED", "Аккаунт заблокирован")
    db.commit()
    return VerifyOut(**_tokens(user), user=UserOut.model_validate(user), is_new_user=is_new)


@router.post("/auth/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    user = db.get(User, decode_token(body.refresh_token, "refresh"))
    if user is None or not user.is_active:
        raise AppError(401, "TOKEN_INVALID", "Недействительный токен")
    return TokenOut(**_tokens(user))


# ----------------------------------------------------------------- профиль
@router.get("/users/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/users/me", response_model=UserOut)
def update_me(
    body: UserUpdateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    user.name = body.name
    db.commit()
    return user


@router.put("/users/me/device-token", status_code=204)
def set_device_token(
    body: DeviceTokenIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    user.fcm_token = body.fcm_token
    db.commit()
