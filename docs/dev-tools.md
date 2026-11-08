# Инструменты проверки мини-приложения (из каталога Awesome REA)

Подобраны в каталоге [Awesome REA](https://github.com/reacher-z/Awesome-REA), раздел Website. С ними агент (Claude Code) сам открывает планер, проверяет экраны и стили, а тесты не дают отправить пользователю секреты.

## Запуск без Telegram

Демо-режим работает целиком в браузере, сервер и ключи не нужны:

```bash
python3 -m http.server 8080 -b 127.0.0.1 -d webapp
# открыть http://127.0.0.1:8080/index.html?demo
```

## MCP-серверы для Claude Code (`.mcp.json`)

| Сервер | Для чего |
|---|---|
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | агент проходит экраны (дела, календарь, покупки, проекты, сезон) в вьюпорте телефона 390×844, заполняет шторки, делает снимки |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | ошибки в консоли, сетевые запросы, трассы производительности; отправка URL в CrUX и статистика выключены |
| [Dembrandt MCP](https://github.com/dembrandt/dembrandt) | извлекает из отрисованной страницы цвета, шрифты, отступы, радиусы, компоненты и контраст WCAG |

Примеры запросов агенту:
- «Открой http://127.0.0.1:8080/index.html?demo, пройди все вкладки в светлой и тёмной теме и сравни со `screenshots/redesign/`»;
- «Проверь экран на ширине 320 px: ничего не обрезано, кнопки не наезжают»;
- «Сними дизайн-токены и покажи цвета и размеры шрифтов, которых нет в CSS-переменных `:root`».

Без установки MCP то же самое из терминала: `npx dembrandt "http://127.0.0.1:8080/index.html?demo" --mobile --wcag --save-output` (результат в `output/`, он в `.gitignore`).

## Тест «что получает пользователь»: `tests/test_frontend_audit.py`

Проверяет `webapp/`, `mobile/www/` и `mobile/capacitor.config.json`, то есть всё, что уходит в браузер и в приложение:
- нет source maps (по ним исходники восстанавливаются одной командой — sourcemapper, shuji);
- нет строк в формате токена бота, ключа Yandex Cloud, IAM-токена, `sk-…`, AWS, приватных ключей;
- нет реальных значений секретов из `.env` и окружения (`BOT_TOKEN`, `YANDEX_API_KEY`…);
- серверные настройки (`BOT_TOKEN`, `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`, `ADMIN_IDS`) не упоминаются во фронтенде;
- сторонние скрипты грузятся только по HTTPS и только с одобренных хостов (`telegram.org`, шрифты Google). Новый хост добавляется в список в тесте осознанно;
- мобильная оболочка ходит только по HTTPS (`cleartext: false`).

Тест работает только на stdlib и идёт вместе с остальными в `python -m pytest -q`.
