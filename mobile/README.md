# Договорились: Android и iOS

The apps are a Capacitor shell around the same web app as the Telegram mini-app (`/app` on the server).
Sign-in goes through the Telegram bot (`/auth/*`, `t.me/<bot>?start=login_<code>`), so a user gets the same account,
couple, tasks and chat as in Telegram. Changes to `webapp/index.html` reach the apps without a store update.

- `capacitor.config.json`: app id `ru.dogovorilis.app`, server URL.
- `assets/`: icon and splash sources; `npm run assets` regenerates every size.
- Test APK: GitHub Actions «Android test build» on every push to `mobile/`.
- Store builds: `codemagic.yaml` at the repo root.
