"""Voice message → text with Yandex SpeechKit (synchronous recognition, up to 30 s and 1 MB).

Telegram voice messages are already OGG/Opus, the format SpeechKit takes as is.
"""
import logging

import aiohttp

from config import config

URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
MAX_SECONDS = 30
MAX_BYTES = 1024 * 1024
log = logging.getLogger(__name__)


async def recognize(audio: bytes) -> str:
    if not config.ai_enabled:
        raise RuntimeError("SpeechKit is not configured")
    params = {"lang": "ru-RU", "folderId": config.yandex_folder_id, "format": "oggopus"}
    headers = {"Authorization": f"Api-Key {config.yandex_api_key}"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(URL, params=params, headers=headers, data=audio) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                log.warning("speechkit %s: %s", r.status, body.get("error_code"))
                raise RuntimeError(f"SpeechKit {r.status}")
    return (body.get("result") or "").strip()
