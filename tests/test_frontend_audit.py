"""What the browser and the phone app receive: no source maps, no secrets, scripts only from known hosts.

Idea from the Awesome REA catalog (Website section): webcrack, sourcemapper and shuji restore code and
sources from whatever is shipped, so everything in webapp/ and mobile/www/ is public. Stdlib only.
Run alone: python -m pytest tests/test_frontend_audit.py
"""
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = [ROOT / "webapp", ROOT / "mobile" / "www", ROOT / "mobile" / "capacitor.config.json"]
TEXT = {".html", ".js", ".css", ".json", ".webmanifest", ".svg", ".txt", ".map"}

SECRET_PATTERNS = {
    "Telegram bot token": re.compile(r"\b\d{8,10}:AA[0-9A-Za-z_-]{33}\b"),
    "Yandex Cloud API key": re.compile(r"\bAQVN[0-9A-Za-z_-]{30,}\b"),
    "IAM token": re.compile(r"\bt1\.[0-9A-Za-z_-]{40,}"),
    "sk-… API key": re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
}
SERVER_ONLY_ENV = ("BOT_TOKEN", "YANDEX_API_KEY", "YANDEX_FOLDER_ID", "ADMIN_IDS")
# Third-party code the pages may load; anything else must be reviewed and added here on purpose.
ALLOWED_SCRIPT_HOSTS = {"telegram.org"}
ALLOWED_STYLE_HOSTS = {"fonts.googleapis.com", "fonts.gstatic.com"}


def shipped_files():
    for p in SHIPPED:
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (f for f in sorted(p.rglob("*")) if f.is_file())


def shipped_text():
    for f in shipped_files():
        if f.suffix.lower() in TEXT:
            yield f, f.read_text(encoding="utf-8", errors="replace")


def env_secret_values():
    """Values of secret-looking keys from a local .env (if any) and the current environment."""
    pairs = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$", line)
            if m:
                pairs[m.group(1)] = m.group(2).strip("'\"")
    return {k: v for k, v in pairs.items()
            if re.search(r"TOKEN|SECRET|API_KEY|PASSWORD", k) and len(v) >= 12}


def test_frontend_files_exist():
    names = {f.relative_to(ROOT).as_posix() for f in shipped_files()}
    assert "webapp/index.html" in names and "mobile/www/index.html" in names


def test_no_source_maps():
    bad = [f.relative_to(ROOT).as_posix() for f in shipped_files() if f.suffix == ".map"]
    bad += [f.relative_to(ROOT).as_posix() for f, t in shipped_text() if "sourceMappingURL=" in t]
    assert not bad, f"source maps ship the sources: {bad}"


def test_no_secret_formats():
    found = [(f.relative_to(ROOT).as_posix(), name) for f, t in shipped_text()
             for name, rx in SECRET_PATTERNS.items() if rx.search(t)]
    assert not found, f"secret-looking strings in shipped files (rotate them): {found}"


def test_no_env_secret_values():
    secrets = env_secret_values()
    found = [(f.relative_to(ROOT).as_posix(), key) for f, t in shipped_text()
             for key, value in secrets.items() if value in t]
    assert not found, f"real secret values in shipped files (rotate them): {found}"


def test_server_env_names_not_in_frontend():
    found = [(f.relative_to(ROOT).as_posix(), n) for f, t in shipped_text() for n in SERVER_ONLY_ENV
             if re.search(rf"\b{n}\b", t)]
    assert not found, f"server-only settings referenced in the frontend: {found}"


@pytest.mark.parametrize("page", ["webapp/index.html", "webapp/privacy.html", "mobile/www/index.html",
                                  "mobile/www/offline.html"])
def test_third_party_code_only_from_known_hosts(page):
    path = ROOT / page
    if not path.is_file():
        pytest.skip(f"{page} not present")
    html = path.read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", html)
    styles = re.findall(r"<link[^>]*\bhref=\"(https?://[^\"]+)\"", html)
    for url in scripts:
        if url.startswith(("http://", "https://", "//")):
            assert url.startswith("https://"), f"{page}: script over plain HTTP: {url}"
            host = re.sub(r"^https://([^/]+).*", r"\1", url)
            assert host in ALLOWED_SCRIPT_HOSTS, f"{page}: script from unreviewed host {host}"
    for url in styles:
        assert url.startswith("https://"), f"{page}: stylesheet over plain HTTP: {url}"
        host = re.sub(r"^https://([^/]+).*", r"\1", url)
        assert host in ALLOWED_STYLE_HOSTS | ALLOWED_SCRIPT_HOSTS, f"{page}: resource from unreviewed host {host}"


def test_mobile_shell_https_only():
    cfg = json.loads((ROOT / "mobile" / "capacitor.config.json").read_text(encoding="utf-8"))
    server = cfg.get("server", {})
    assert server.get("url", "").startswith("https://")
    assert server.get("cleartext") is False
