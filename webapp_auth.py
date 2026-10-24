"""Verify Telegram mini-app launch data:
secret = HMAC_SHA256(key="WebAppData", msg=bot_token); hash = hex(HMAC_SHA256(secret, data_check_string))."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_SECONDS = 24 * 3600


def verify(init_data: str, bot_token: str, now: float | None = None) -> dict | None:
    """Return the launch user ({"id": ..., ...}) if init_data is authentic and fresh, else None."""
    if not init_data or not bot_token:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
        user = json.loads(fields.get("user", "{}"))
    except ValueError:
        return None
    if (now or time.time()) - auth_date > MAX_AGE_SECONDS or "id" not in user:
        return None
    return user
