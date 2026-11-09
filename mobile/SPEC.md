---
id: mobile
type: module-design
status: active
title: Android и iOS (mobile/)
parent: architecture
depends-on: [webapp]
tags: [mobile, capacitor]
---

## Ответственность

Capacitor-обёртка (`ru.dogovorilis.app`) вокруг `/app` на сервере: правки `webapp/index.html` доходят до
приложений без обновления в сторах. `www/offline.html` — экран без сети.

## Граница

- Вход только через бота (`/auth/*`, `t.me/<бот>?start=login_<код>`); своих учётных записей нет.
- Ключи подписи и App Store Connect — только в Codemagic; в репозитории их быть не может.
- Тестовый APK собирает GitHub Actions (`mobile.yml`), релизы — `codemagic.yaml`.

## Проверка сборки

Распаковать APK (JADX/Apktool) и убедиться, что в `assets/public` нет секретов; см. `docs/TOOLS.md`.
