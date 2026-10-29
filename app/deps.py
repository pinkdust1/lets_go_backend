from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import get_db
from .errors import AppError
from .models import DriverStatus, Role, User
from .security import decode_token

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise AppError(401, "UNAUTHORIZED", "Требуется авторизация")
    user = db.get(User, decode_token(creds.credentials, "access"))
    if user is None:
        raise AppError(401, "TOKEN_INVALID", "Недействительный токен")
    if not user.is_active:
        raise AppError(403, "ACCOUNT_DISABLED", "Аккаунт заблокирован")
    return user


def require_roles(*roles: Role):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise AppError(403, "FORBIDDEN", "Недостаточно прав")
        return user

    return checker


current_passenger = require_roles(Role.PASSENGER)
current_driver = require_roles(Role.DRIVER)
current_admin = require_roles(Role.ADMIN)


def approved_driver(user: User = Depends(current_driver)) -> User:
    profile = user.driver_profile
    if profile is None or profile.status != DriverStatus.APPROVED:
        raise AppError(403, "DRIVER_NOT_APPROVED", "Профиль водителя ещё не подтверждён")
    return user
