# wg-agent: notes for Claude Code

WG-Gesucht flat-hunting agent. It polls WG-Gesucht searches, a local LLM scores each ad and drafts a
message in the ad's language (German or English), and the user approves every message in Telegram
before it's sent. Nothing is ever sent without the user tapping ✅. See README.md for the user-facing docs.

## Architecture
- `bot.py`: Telegram bot (python-telegram-bot v21) and the main loop. Self-scheduling `tick()` →
  `_poll()` → card filter (`hard_filter`) → queue → `process()` (details → filters → repost and
  same-person check → commute → LLM → card). Also `check_replies()` (inbox), `/excel`, `/test`.
- `wg.py`: Playwright persistent Chromium profile (`browser-profile/`, logged in via `--login`).
  `CARDS_JS`, `DETAIL_JS` and `INBOX_JS` are the selectors. `send_message()` goes straight to
  `/nachricht-senden/...`, checks `#message_timestamp`, and confirms success through the
  `api.php?action=conversations` response. Session-only cookies (PHPSESSID) are saved to
  `browser-session.json` on stop and restored on start; login is checked on `/nachrichten.html`,
  never the homepage (it looks the same logged in or out).
- `guard.py`: request budget (pages per hour and per day) and escalating block cooldowns, saved to SQLite.
- `commute.py`: Transitous `/api/v1/plan` (bike + transit) from the ad's `map_config` coordinates;
  Nominatim geocoding; optional Google Distance Matrix; cached.
- `llm.py`: OpenAI-compatible client for a local server or a cloud provider (JSON mode, optional
  `/no_think`, key from config or `LLM_API_KEY`), the prompt, scam regexes, and full/template message modes.
- `tracker.py`: `applications.xlsx` export and matching inbox conversations to sent ads.
- State: `wg.sqlite` (listings with status queued/filtered/pending/sent/replied/..., plus a kv table).
- Config: `config.yaml` (copy of `config.example.yaml`). The user's own facts live under `me:`.

## Rules for working on this
- Never send real messages to WG-Gesucht while testing; keep `send.dry_run: true` unless the user says otherwise.
- Keep request volume low; every page load must go through `WG._goto` (RateGuard).
- Don't add captcha solving or anti-detection stealth; on a block, pause and notify instead.
- Never commit `config.yaml`, `CLAUDE.local.md`, `browser-profile/`, `browser-session.json`, `wg.sqlite`,
  `applications.xlsx` or `screenshots/` (personal data, session cookies). They are in `.gitignore`.
- New config keys need a default in code, so existing `config.yaml` files keep working.
- Format and lint with `uvx ruff format . && uvx ruff check .` (settings in `pyproject.toml`).

## Personal context
A private `CLAUDE.local.md` (gitignored) may hold the user's own situation and setup notes.
