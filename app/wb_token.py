"""Runtime-managed WB API token, persisted to a gitignored file."""
from __future__ import annotations

from pathlib import Path

from app.config import settings

TOKEN_PATH = Path("data/wb_token.txt")


class NoWBTokenError(RuntimeError):
    pass


def get_wb_token() -> str:
    """Token saved at runtime takes precedence; .env is a fallback for bootstrapping."""
    if TOKEN_PATH.exists():
        text = TOKEN_PATH.read_text().strip()
        if text:
            return text
    fallback = (settings.wb_api_token or "").strip()
    if not fallback:
        raise NoWBTokenError(
            "WB-токен не задан. Нажмите «🔑 WB-токен» и пришлите токен следующим сообщением."
        )
    return fallback


def save_wb_token(token: str) -> None:
    token = token.strip()
    if not token:
        raise ValueError("empty token")
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)


def has_user_token() -> bool:
    return TOKEN_PATH.exists() and bool(TOKEN_PATH.read_text().strip())
