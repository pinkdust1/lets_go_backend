from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .utils import normalize_phone

DEFAULT_SECRET = "change-me-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Taxi API"
    environment: str = "dev"  # dev | prod
    database_url: str = "sqlite:///./taxi.db"

    # --- auth ---
    secret_key: str = DEFAULT_SECRET
    access_token_minutes: int = 60
    refresh_token_days: int = 30
    otp_ttl_seconds: int = 300
    otp_resend_seconds: int = 60
    otp_max_attempts: int = 5
    debug_otp: bool = False  # вернуть код в ответе (только dev)
    dev_fixed_otp: str | None = None  # всегда один и тот же код (только dev)
    admin_phones: str = ""  # через запятую

    # --- файлы ---
    upload_dir: str = "uploads"
    max_upload_mb: int = 10

    # --- деньги / заказы ---
    currency: str = "KZT"
    price_round_to: int = 10  # округление цены до N единиц валюты
    platform_commission_percent: int = 15
    driver_search_radius_km: float = 5.0
    max_ride_distance_km: float = 300.0
    rating_window_hours: int = 72  # сколько после поездки можно поставить оценку

    @property
    def admin_phone_list(self) -> list[str]:
        return [normalize_phone(p) for p in self.admin_phones.split(",") if p.strip()]

    @model_validator(mode="after")
    def _check_prod(self):
        if self.environment == "prod":
            if self.secret_key == DEFAULT_SECRET or len(self.secret_key) < 32:
                raise ValueError("В prod нужен собственный SECRET_KEY (>= 32 символов)")
            if self.debug_otp or self.dev_fixed_otp:
                raise ValueError("DEBUG_OTP / DEV_FIXED_OTP запрещены в prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
