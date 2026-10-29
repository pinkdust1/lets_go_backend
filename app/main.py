from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from .config import settings
from .database import Base, SessionLocal, engine
from .errors import AppError, app_error_handler
from .models import Tariff
from .routers import admin, auth, drivers, promos, ratings, rides

# Стартовые тарифы — заглушки, настройте под свой город через PATCH /admin/tariffs/{code}
DEFAULT_TARIFFS = [
    dict(code="economy", name="Эконом", rank=1, base_fare=300, per_km=100, per_min=15, min_fare=600),
    dict(code="comfort", name="Комфорт", rank=2, base_fare=400, per_km=130, per_min=20, min_fare=800),
    dict(code="business", name="Бизнес", rank=3, base_fare=600, per_km=180, per_min=30, min_fare=1200),
]


def seed_tariffs() -> None:
    with SessionLocal() as db:
        if db.scalar(select(Tariff.id).limit(1)) is None:
            db.add_all(Tariff(**t) for t in DEFAULT_TARIFFS)
            db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Для продакшена вместо create_all используйте миграции Alembic.
    Base.metadata.create_all(engine)
    seed_tariffs()
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)

API_PREFIX = "/api/v1"
for r in (auth.router, drivers.router, rides.router, promos.router, ratings.router, admin.router):
    app.include_router(r, prefix=API_PREFIX)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}
