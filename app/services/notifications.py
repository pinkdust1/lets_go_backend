import logging

from ..models import User

log = logging.getLogger("taxi.push")


def notify(user: User | None, title: str, body: str) -> None:
    """Заглушка push-уведомлений.

    Здесь подключается FCM (firebase-admin) — токен устройства лежит в user.fcm_token.
    Пока приложение может опрашивать GET /rides/active, а push просто пишется в лог.
    """
    if user is None:
        return
    log.info("push -> user=%s token=%s | %s: %s", user.id, bool(user.fcm_token), title, body)
