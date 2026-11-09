# Инструменты из Awesome REA для «Договорились»

Отбор из каталога [Awesome REA](https://github.com/reacher-z/Awesome-REA) (данные на 09.11.2026).
Взято только то, что помогает проверять **своё** приложение: бот, мини-приложение и сборку Capacitor.
Игры, прошивки, бинарники и протоколы железа не подходят.

## Мини-приложение и API (`web.py`, `webapp/index.html`)

| Инструмент | Зачем здесь |
|---|---|
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Агент открывает `/tg?demo` в Chrome, снимает трассу производительности, смотрит сеть и консоль. Быстрая проверка экранов из `DESIGN.md` §6 |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Повторяемые сценарии по снимку доступности: «добавить дело → отметить → отменить» на 320px и в тёмной теме |
| [mitmproxy](https://github.com/mitmproxy/mitmproxy) | Записать трафик мини-приложения и APK к своему серверу и посмотреть, что реально уходит в `/api/*` |
| [mitmproxy2swagger](https://github.com/alufers/mitmproxy2swagger) / [har-to-openapi](https://github.com/jonluca/har-to-openapi) | Из записи трафика (или HAR из DevTools) собрать OpenAPI на ~20 эндпоинтов `web.py` — документация API для мобильного клиента и тестов |
| [Burp Suite MCP Server](https://github.com/PortSwigger/mcp-server) | Повторить запросы с чужим `id` дела или проекта — регрессия на изоляцию пар и на дыру со входом из аудита (п. 4) |
| [Dembrandt](https://github.com/dembrandt/dembrandt) | Вытащить фактические цвета, шрифты и отступы из отрисованной страницы и сверить с `DESIGN.md`: ловит `#fff` и px мимо токенов |

## Android-сборка (`mobile/`, Capacitor)

| Инструмент | Зачем здесь |
|---|---|
| [JADX](https://github.com/skylot/jadx) / [Apktool](https://github.com/iBotPeaches/Apktool) | Распаковать собранный APK из Codemagic/GitHub Actions и убедиться, что в `assets/public` нет ключей, отладочных URL и лишних файлов |
| [APKiD](https://github.com/rednaga/APKiD) | Быстро проверить, чем собран и защищён APK |
| [MobSF](https://github.com/MobSF/Mobile-Security-Framework-MobSF) | Статический отчёт по манифесту: `debuggable`, `allowBackup`, cleartext-трафик, экспортированные компоненты |
| [apk-mitm](https://github.com/niklashigi/apk-mitm) / [Android Unpinner](https://github.com/mitmproxy/android-unpinner) | Подготовить свою сборку к записи HTTPS через mitmproxy |
| [objection](https://github.com/sensepost/objection) / [Grapefruit](https://github.com/ChiChou/grapefruit) | Посмотреть на устройстве, что приложение хранит локально (токен входа, кеш WebView) |

## Учиться

- [OWASP MASTG](https://mas.owasp.org/MASTG/) — чек-лист тестирования мобильных приложений; разделы про WebView и хранение данных прямо про Capacitor.

Мини-сценарий: `mitmproxy -w flows` → пройти приложение → `mitmproxy2swagger -i flows -o docs/openapi.yaml -p https://<amvera-домен>` →
положить спецификацию рядом с этим файлом и проверять её в тестах.
