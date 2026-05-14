from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    wb_api_token: str = Field(..., alias="WB_API_TOKEN")
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_ids: str = Field("", alias="TELEGRAM_ALLOWED_USER_IDS")

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.telegram_allowed_user_ids.strip():
            return set()
        return {int(x) for x in self.telegram_allowed_user_ids.split(",") if x.strip()}


settings = Settings()
