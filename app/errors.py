from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Бизнес-ошибка. Клиент получает {"error": {"code": ..., "message": ...}}.

    code — стабильный машинный код (по нему приложение может локализовать текст),
    message — готовое сообщение на русском.
    """

    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
