from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Rating, User


def recompute_user_rating(db: Session, user: User) -> None:
    """Пересчёт кэша из таблицы оценок (а не инкрементом) — значение не может «поплыть»."""
    avg, count = db.execute(
        select(func.avg(Rating.score), func.count(Rating.id)).where(Rating.to_user_id == user.id)
    ).one()
    user.rating_count = count or 0
    user.rating_avg = round(float(avg), 2) if count else None


def distribution(db: Session, user_id: int) -> dict[int, int]:
    rows = db.execute(
        select(Rating.score, func.count(Rating.id))
        .where(Rating.to_user_id == user_id)
        .group_by(Rating.score)
    ).all()
    result = {s: 0 for s in range(1, 6)}
    result.update({score: n for score, n in rows})
    return result
