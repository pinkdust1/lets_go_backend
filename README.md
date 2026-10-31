# Taxi API (FastAPI)

Бэкенд мобильного приложения такси: вход по телефону (OTP), регистрация водителя с 3 документами и модерацией,
заказы, промокоды, акции, рейтинги.

## Запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # поправьте SECRET_KEY, ADMIN_PHONES
uvicorn app.main:app --reload
```

Swagger: http://localhost:8000/docs · **Админ-панель: http://localhost:8000/admin-panel/** · Тесты: `pytest`

В dev при `DEBUG_OTP=true` код из `/auth/otp/request` возвращается в поле `debug_code` (SMS пишутся в лог).
Все эндпоинты — под префиксом `/api/v1`. Ошибки приходят как `{"error": {"code": "...", "message": "..."}}` —
по `code` приложение может локализовать текст.

## Как это работает

**Роли:** `passenger`, `driver`, `admin`. Роль выбирается при первой регистрации (`role` в `/auth/otp/verify`),
админами становятся номера из `ADMIN_PHONES`. Один номер — одна роль.

**Водитель.** `PUT /drivers/me/profile` (анкета + авто) → `POST /drivers/me/documents` ×3 → админ проверяет
каждый документ → когда одобрены все 3, статус `approved` и можно выходить на линию.
Статусы: `incomplete → pending → approved | rejected`, а также `blocked`. Загрузка файла проверяется по сигнатуре
(JPG/PNG/WEBP/HEIC/PDF, до 10 МБ); файлы отдаются только владельцу и админу.

**Заказ:** `searching → accepted → arrived → in_progress → completed` (+ `cancelled`).
Принятие заказа атомарное — при одновременном нажатии двумя водителями заказ получит только один.
Цена фиксируется на сервере при создании. Промокод резервируется с заказом и возвращается при отмене.

**Рейтинг:** после `completed` каждая сторона оценивает другую 1–5 (+ комментарий и теги), одна оценка на заказ,
окно `RATING_WINDOW_HOURS` (72 ч). Среднее пересчитывается из таблицы оценок.

## Эндпоинты

| Раздел | Эндпоинты |
|---|---|
| Авторизация | `POST /auth/otp/request`, `POST /auth/otp/verify`, `POST /auth/refresh` |
| Профиль | `GET/PATCH /users/me`, `PUT /users/me/device-token` (FCM) |
| Водитель | `GET /drivers/me`, `PUT /drivers/me/profile`, `POST /drivers/me/documents`, `GET /drivers/documents/{id}/file`, `POST /drivers/me/status`, `POST /drivers/me/location`, `GET /drivers/me/earnings` |
| Заказы | `POST /rides/estimate`, `POST /rides`, `GET /rides/active`, `GET /rides/history`, `GET /rides/{id}`, `POST /rides/{id}/cancel` |
| Заказы (водитель) | `GET /rides/available`, `POST /rides/{id}/accept`, `/arrived`, `/start`, `/complete` |
| Промо | `POST /promos/validate`, `GET /promotions` |
| Рейтинг | `POST /rides/{id}/rating`, `GET /ratings/me` |
| Админ | `GET /admin/drivers`, `POST /admin/documents/{id}/review`, `POST /admin/drivers/{id}/block\|unblock`, `/admin/promo-codes`, `/admin/promotions`, `/admin/tariffs`, `GET /admin/stats` |

## Админ-панель

Веб-интерфейс на `/admin-panel/` отдаётся самим бэкендом (один HTML-файл `app/static/admin/index.html`, без сборки
и внешних библиотек). Вход — по SMS-коду с номера из `ADMIN_PHONES`; остальные роли панель не пускает.

- **Дашборд:** выручка, поездки, средний чек, доля отмен, новые пользователи за 7/30/90 дней со сравнением с прошлым
  периодом; графики по дням; сколько водителей на линии и активных заказов; топ водителей; плашка «ждут проверки документов».
  Дни считаются в часовом поясе браузера админа. Обновляется каждые 30 секунд.
- **Пользователи:** поиск по имени/номеру, фильтры по роли, статусу водителя и доступу. В карточке: статистика, авто,
  **просмотр и модерация 3 документов**, блокировка водителя (с причиной) и аккаунта, последние заказы.
- **Заказы:** фильтры по статусу и датам, поиск (`#42` — номер заказа, цифры — фрагмент телефона, текст — имя),
  карточка со всеми деталями, оценками и хронологией, **принудительная отмена** зависшего заказа
  (промокод при этом возвращается пассажиру).
- **Настройки** (меняются на лету, хранятся в БД, значения из `.env` — по умолчанию): комиссия платформы, округление цены,
  радиус поиска заказов, максимальная длина поездки, время на оценку. Применяются только к новым действиям.

Новые эндпоинты (`/api/v1/admin/...`): `GET /dashboard`, `GET /users`, `GET /users/{id}`, `POST /users/{id}/active`,
`GET /rides`, `GET /rides/{id}`, `POST /rides/{id}/cancel`, `GET|PATCH /settings`.

Ограничения: поиск по имени без учёта регистра кириллицы работает только в PostgreSQL (в SQLite — только для латиницы);
администраторов блокировать нельзя; заблокировать пользователя с активным заказом нельзя, пока заказ не отменён.

## Допущения — проверьте под ваше приложение

- **3 документа** = водительское удостоверение, свидетельство о регистрации ТС, удостоверение личности
  (`DocType` в `app/models.py` — поменяйте под ваш список).
- **Маршрут считает клиент**: `/rides/estimate` и `/rides` принимают точки, `distance_km`, `duration_min`.
  Сервер лишь отсекает нереалистичные значения. Для защиты от подделки цены считайте маршрут на сервере (OSRM/Directions).
- **Тарифы и валюта** (`CURRENCY=KZT`) — заглушки. Меняются через `PATCH /admin/tariffs/{code}` и `.env`.
- Оплата только наличными, комиссия платформы `PLATFORM_COMMISSION_PERCENT` фиксируется в заказе при завершении.

## Что осталось для продакшена

1. SMS-провайдер: реализуйте `SMSSender` в `app/services/otp.py` (сейчас пишет в лог).
2. Push (FCM): заполнить `app/services/notifications.py` (токены уже сохраняются). Пока приложение может опрашивать `GET /rides/active`.
3. PostgreSQL (`DATABASE_URL`, `pip install "psycopg[binary]"`) и миграции Alembic вместо `create_all`.
4. Поиск водителей: сейчас перебор открытых заказов по радиусу — при росте нагрузки PostGIS/geohash.
5. Онлайн-оплата карт, WebSocket для трекинга машины, rate-limit на уровне прокси.
