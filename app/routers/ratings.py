from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..errors import AppError
from ..models import Rating, Ride, RideStatus, User
from ..schemas import RatingIn, RatingOut, RatingSummaryOut
from ..services import app_settings
from ..services.ratings import distribution, recompute_user_rating
from ..utils import utcnow

router = APIRouter(tags=["ratings"])


@router.post("/rides/{ride_id}/rating", response_model=RatingOut, status_code=201)
def rate_ride(
    ride_id: int, body: RatingIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Пассажир оценивает водителя, водитель — пассажира. Одна оценка на заказ от каждой стороны."""
    ride = db.get(Ride, ride_id)
    if ride is None or user.id not in (ride.passenger_id, ride.driver_id):
        raise AppError(404, "RIDE_NOT_FOUND", "Заказ не найден")
    if ride.status != RideStatus.COMPLETED:
        raise AppError(409, "RIDE_NOT_COMPLETED", "Оценить можно только завершённую поездку")
    if utcnow() - ride.completed_at > timedelta(hours=app_settings.get(db, "rating_window_hours")):
        raise AppError(409, "RATING_WINDOW_CLOSED", "Время для оценки этой поездки истекло")

    target_id = ride.driver_id if user.id == ride.passenger_id else ride.passenger_id
    rating = Rating(
        ride_id=ride.id,
        from_user_id=user.id,
        to_user_id=target_id,
        score=body.score,
        comment=body.comment,
        tags=body.tags,
    )
    db.add(rating)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "ALREADY_RATED", "Вы уже оценили эту поездку")

    recompute_user_rating(db, db.get(User, target_id))
    db.commit()
    return rating


@router.get("/ratings/me", response_model=RatingSummaryOut)
def my_rating(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Мой рейтинг: среднее, распределение по звёздам и последние 20 отзывов."""
    recent = db.scalars(
        select(Rating)
        .where(Rating.to_user_id == user.id)
        .order_by(Rating.id.desc())
        .limit(20)
    ).all()
    return RatingSummaryOut(
        average=user.rating_avg,
        count=user.rating_count,
        distribution=distribution(db, user.id),
        recent=[RatingOut.model_validate(r) for r in recent],
    )
