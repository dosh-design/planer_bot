import os
from dataclasses import dataclass
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    # Pasted from a phone the token often brings a stray space or line break along; the token itself has none.
    bot_token: str = "".join(os.environ.get("BOT_TOKEN", "").split())
    brand: str = env("BRAND", "Обещал — сделал")
    data_dir: Path = Path(env("DATA_DIR", "/data"))
    timezone: str = env("TZ_NAME", "Europe/Moscow")
    # Yandex AI Studio: the text model turns a phrase into tasks, SpeechKit turns a voice message into text.
    # The API key needs the roles ai.languageModels.user and ai.speechkit-stt.user.
    yandex_api_key: str = env("YANDEX_API_KEY", "")
    yandex_folder_id: str = env("YANDEX_FOLDER_ID", "")
    yandex_text_model: str = env("YANDEX_TEXT_MODEL", "yandexgpt-lite/latest")
    # Local time of the morning digest and of the gentle nudges about old promises.
    morning_hour: int = int(env("MORNING_HOUR", "10") or 10)
    # A promise without a date gets its first nudge after this many days, then again every NUDGE_EVERY days.
    nudge_after_days: int = int(env("NUDGE_AFTER_DAYS", "3") or 3)
    nudge_every_days: int = int(env("NUDGE_EVERY_DAYS", "3") or 3)
    # Public HTTPS address of this app on the hosting, e.g. https://family-user.amvera.io — turns on the mini-app.
    webapp_url: str = env("WEBAPP_URL", "").rstrip("/")
    port: int = int(env("PORT", "80") or 80)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.yandex_api_key and self.yandex_folder_id)


config = Config()
