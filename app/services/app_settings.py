"""Настройки, редактируемые из админ-панели.

Значение берётся из таблицы app_settings, а если строки нет — из .env (config.Settings).
Читаем из БД при каждом обращении (запрос по первичному ключу), поэтому изменения сразу
видны всем воркерам без перезапуска.
"""

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import settings
from ..errors import AppError
from ..models import AppSetting, User
from ..utils import utcnow


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    description: str
    kind: str  # "int" | "float"
    minimum: float
    maximum: float
    unit: str
    group: str


DEFS: list[SettingDef] = [
    SettingDef(
        "platform_commission_percent", "Комиссия платформы",
        "Доля от стоимости поездки, которую удерживает сервис. Фиксируется в заказе при завершении.",
        "int", 0, 100, "%", "Деньги",
    ),
    SettingDef(
        "price_round_to", "Округление цены",
        "Стоимость поездки округляется до кратной этому шагу (в валюте приложения).",
        "int", 1, 1000, "", "Деньги",
    ),
    SettingDef(
        "driver_search_radius_km", "Радиус поиска заказов",
        "На каком расстоянии от водителя ему показываются новые заказы.",
        "float", 0.5, 100, "км", "Заказы",
    ),
    SettingDef(
        "max_ride_distance_km", "Максимальная длина поездки",
        "Заказы с маршрутом длиннее этого значения не принимаются.",
        "float", 1, 2000, "км", "Заказы",
    ),
    SettingDef(
        "rating_window_hours", "Время на оценку поездки",
        "Сколько часов после завершения поездки можно поставить оценку.",
        "int", 1, 720, "ч", "Рейтинг",
    ),
]
BY_KEY = {d.key: d for d in DEFS}


def default_value(key: str):
    return getattr(settings, key)


def _cast(d: SettingDef, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AppError(422, "SETTING_INVALID", f"«{d.label}»: нужно число")
    if d.kind == "int":
        if float(value) != int(value):
            raise AppError(422, "SETTING_INVALID", f"«{d.label}»: нужно целое число")
        value = int(value)
    else:
        value = float(value)
    if not d.minimum <= value <= d.maximum:
        raise AppError(
            422, "SETTING_INVALID",
            f"«{d.label}»: допустимо от {d.minimum:g} до {d.maximum:g}",
        )
    return value


def get(db: Session, key: str):
    row = db.get(AppSetting, key)
    if row is None:
        return default_value(key)
    try:
        return _cast(BY_KEY[key], json.loads(row.value))
    except (ValueError, AppError):  # испорченное значение в БД не должно ронять заказы
        return default_value(key)


def list_all(db: Session) -> list[dict]:
    out = []
    for d in DEFS:
        row = db.get(AppSetting, d.key)
        editor = db.get(User, row.updated_by) if row and row.updated_by else None
        out.append(
            dict(
                key=d.key, label=d.label, description=d.description, group=d.group,
                kind=d.kind, unit=d.unit, minimum=d.minimum, maximum=d.maximum,
                value=get(db, d.key), default=default_value(d.key), is_custom=row is not None,
                updated_at=row.updated_at if row else None,
                updated_by=(editor.name or editor.phone) if editor else None,
            )
        )
    return out


def update(db: Session, values: dict, admin: User) -> None:
    unknown = [k for k in values if k not in BY_KEY]
    if unknown:
        raise AppError(422, "SETTING_UNKNOWN", f"Неизвестная настройка: {unknown[0]}")
    cleaned = {k: _cast(BY_KEY[k], v) for k, v in values.items()}  # сначала проверяем все

    for key, value in cleaned.items():
        row = db.get(AppSetting, key)
        if value == default_value(key):  # вернули значение по умолчанию -> убираем переопределение
            if row:
                db.delete(row)
            continue
        if row is None:
            row = AppSetting(key=key, value=json.dumps(value))
            db.add(row)
        row.value = json.dumps(value)
        row.updated_at = utcnow()
        row.updated_by = admin.id
    db.commit()
