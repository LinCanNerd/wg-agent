<p align="center">
  <img src="banner.jpeg" alt="wg-agent: your flat-hunting sidekick for WG-Gesucht" width="100%">
</p>

A flat-hunting assistant that runs on your own machine. It watches your
[WG-Gesucht](https://www.wg-gesucht.de) and [Kleinanzeigen](https://www.kleinanzeigen.de) searches, uses an
**LLM** (on your own machine or a cloud API) to score each new ad and write a personal first message in
the ad's language, and sends you a card in **Telegram**. **Nothing is ever sent until you tap ✅.**

> **Disclaimer:** this is an unofficial personal tool, not affiliated with WG-Gesucht or Kleinanzeigen.
> Automated access may be against the sites' terms of use. Use it at your own risk, keep the request volume
> low (the defaults are conservative) and never remove the human approval step.

## Features

- **Fast:** checks your searches every 2–3.5 minutes. With WG-Gesucht Plus, logged in, you also get
  Plus's head start on new ads.
- **Two sites:** WG-Gesucht, and Kleinanzeigen's "Auf Zeit & WG" and "Mietwohnungen" categories. Each
  site has its own page budget, so one site pushing back doesn't stop the other. ImmoScout24 and
  Immowelt aren't supported: they show a captcha or block automated browsers right away.
- **Cheap filtering first:** paid, partner and company ads, swap offers (Tauschangebote), old ads,
  ads over budget and ads for the other gender only are dropped from the search page, without opening them.
- **Reads the whole ad:** all description tabs (where code words hide), exact coordinates,
  flatmates, who they're looking for, the advertiser's "member since" date and, with Plus, the number
  of applicants.
- **Real commute times** by bike and public transport to the places you choose, via the free
  [Transitous](https://transitous.org) routing service (Google Distance Matrix is optional).
  Ads that are too far away are dropped or flagged.
- **No double messages:** skips reposts of the same flat, the same flat advertised on the other site
  (same text, or same street, size and rent), and advertisers you've already written to.
- **LLM drafting:** scores the ad 0–10, spots scam signals and code words ("start your message with
  *Banane*"), answers questions the ad asks, and writes the message in German or English: casual
  "du" for WGs, polite "Sie" for studios. By default it sends your own fixed text and only adds a
  sentence about the ad. German drafts come with an English translation for you.
- **Telegram approval:** photo album + card with commute, flatmates, score, warnings, notes and the
  draft. Buttons: `✅ Send` `❌ Skip` `✏️ Edit` `🔁 Rewrite` `🔗 Ad` `🗺 Map` `🚲 Route`.
- **Safe sending:** goes straight to the contact form, refuses if you already wrote to that ad or
  person, and confirms the message actually went out. Sending on Kleinanzeigen stays a dry run until
  you switch it on (`sites.kleinanzeigen.dry_run`), because it hasn't been verified on the live site yet.
- **Reply tracking:** checks your inboxes, pings you in Telegram when someone answers, and
  keeps an `applications.xlsx` log of everything you sent (`/excel`).

## How it works

```
search page ──► card filter ──► ad page ──► filters, repost & same-person check
                                                   │
          Telegram card ◄── LLM score + draft ◄── commute (Transitous)
                │
          you tap ✅ ──► the site's contact form ──► applications.xlsx ◄── inbox replies
```

| File | What it does |
|---|---|
| `bot.py` | Telegram bot, main loop, filters, SQLite storage |
| `sites.py` | What all sites share: one persistent, logged-in Chromium (Playwright) and the `Site` interface |
| `wg.py` | WG-Gesucht: search, ad page, contact form and inbox selectors |
| `kleinanzeigen.py` | Kleinanzeigen: search, ad page (both 2026 layouts), contact form and inbox |
| `llm.py` | Prompt, scoring, drafting, scam signals (any OpenAI-compatible API) |
| `commute.py` | Bike and public transport times via Transitous / Nominatim (or Google) |
| `guard.py` | Request budget and block cooldowns per site, saved across restarts |
| `tracker.py` | `applications.xlsx` export and matching inbox replies to sent ads |

## Requirements

- An always-on computer with Python 3.10+ (Linux or macOS): a home server, a Raspberry Pi-class
  board or a small VPS. It runs the Telegram bot and a headless Chromium; no GPU is needed if you
  use a cloud LLM.
- An LLM: a local one or a cloud API key. See [Choosing an LLM](#choosing-an-llm).
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and your Telegram user id
  (from [@userinfobot](https://t.me/userinfobot)).
- A WG-Gesucht account (Plus is optional but helps), and a Kleinanzeigen account if you search there.

## Setup

```bash
git clone <this repo> wg-agent && cd wg-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium     # on ARM64, try without --with-deps if it fails

cp config.example.yaml config.yaml          # then fill in everything marked TODO, and pick an LLM
python bot.py --login                       # once: opens a browser window, log in to each site you search
python bot.py
```

In Telegram, open your bot and press **Start**. `--login` needs a display; on a headless server use
`ssh -X` or VNC. `python bot.py --login kleinanzeigen` logs in to one site only.

## Choosing an LLM

The agent talks to any OpenAI-compatible chat API, so you set `llm.base_url`, `llm.model` and, for
cloud providers, `llm.api_key` (or the `LLM_API_KEY` environment variable). `config.example.yaml`
lists the URLs.

| | Local | Cloud |
|---|---|---|
| Examples | Ollama, LM Studio, vLLM | OpenAI, Anthropic, Google Gemini, Mistral, Groq, OpenRouter |
| Hardware | A strong GPU or Apple Silicon | None beyond the always-on computer |
| Cost | Free | Pay per use (one or two requests per ad you see) |
| Privacy | Everything stays on your machine | Your profile and the ads are sent to the provider |

German quality matters more than speed, because you read every draft anyway. Local results from our
tests: `gemma4:31b` wrote the best German; `gemma4:26b` and `qwen3:30b-a3b-instruct-2507` are much
faster with a few slips; small 8B models tend to invent facts about you. With Ollama, run
`ollama pull <model>` first.
Whatever you pick, try it with `/test` before turning off dry run.

## Configuration

Everything lives in `config.yaml`; `config.example.yaml` documents every option. The most important parts:

- **`me:`** is who you are. The model only uses facts written here, so be complete and honest. If an
  ad asks something your profile doesn't answer (smoker? pets?), the model may guess.
  `preferences` tells it how to score; `message_guidelines` tells it how to write.
- **`search.searches`:** one entry per search URL (WG-Gesucht or Kleinanzeigen), each with its own
  budget and minimum score.
- **`sites:`** per-site settings: `dry_run` and that site's own page budget.
- **`commute.destinations`:** the places you travel to, with limits. `action: exclude` drops ads that
  are too far; `mark` only adds a warning.
- **`me.mode`:**
  - `template` (what `config.example.yaml` uses): your own fixed text (`me.templates`, one per WG/studio and German/English)
    is sent word for word. The model only fills in the greeting and one plain sentence about the ad,
    plus answers to questions the ad asks and a code word if there is one. It sees your text so it
    doesn't repeat it, and sentences where it invents where you live are dropped.
  - `full`: the model writes the whole message, following `message_guidelines`. More varied, but it
    tends to sound like an AI.
  In both modes a code word found in the ad is always kept; a greeting-style one ("Servus Corps RP!")
  replaces the greeting.
- **Other cities:** set `city`, use that city's search URLs, and adjust `districts_exclude` and
  `commute.viewbox`.

## First test

Keep `send.dry_run: true` and send `/test <ad url>` for a few ads. Pressing ✅ then fills in the
contact form and sends you a screenshot instead of sending. When you're happy with the drafts, set
`dry_run: false`. Kleinanzeigen has its own switch, `sites.kleinanzeigen.dry_run`: check a dry-run
screenshot there first.

## Telegram commands

| Command | |
|---|---|
| `/status` | what the agent is doing, page budget, database counts |
| `/check` | check the searches now |
| `/replies` | check the inbox now |
| `/excel` | get `applications.xlsx` |
| `/pause`, `/resume` | stop / restart polling |
| `/test <url>` | score and draft any ad (ignores filters) |
| `/login_check` | is the browser still logged in to each site? |

## Staying polite to the sites

`guard.py` makes sure the agent stays well below anything that looks like scraping:

- Every page load counts against that site's budget of 40 per hour and 650 per day, saved to disk so
  restarts don't reset it. Normal use is about 30 per hour per site.
- Searches run at random intervals (2–3.5 min, every ~20 min at night). At most 3 ads are opened per
  check, freshest first.
- A captcha, HTTP 403/429/503, the terms-of-use block page, or a search that suddenly comes back empty
  pauses that site for 15 min, then 1 h, 3 h and 12 h for repeat blocks, and you get a Telegram note.
  There is no captcha solving and no anti-detection stealth.
- Images and fonts are never downloaded; Telegram fetches the photos itself.
- Routing requests (Transitous / OpenStreetMap) are throttled to 1 per second and cached.

## Run it as a service

`wg-agent.service` is a systemd **user** unit (no root needed). It assumes the repo is in `~/wg-agent`;
edit the paths if not.

```bash
mkdir -p ~/.config/systemd/user && cp wg-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now wg-agent
sudo loginctl enable-linger "$USER"    # keep it running after you log out
journalctl --user -u wg-agent -f
```

## Troubleshooting

- **0 listings parsed / nothing found:** the sites change their HTML now and then. The selectors are
  in `wg.py` and `kleinanzeigen.py` (`CARDS_JS`, `DETAIL_JS`, `INBOX_JS`, `send_message`).
- **Logged out:** WG-Gesucht needs a session-only cookie; the agent saves it in `browser-session.json`
  and restores it on start. If the server-side session expires, run `python bot.py --login` again
  (or `--login kleinanzeigen` for just that site).
- **Send problems:** a screenshot of every send attempt lands in `screenshots/`.
- **Commute missing:** check the destination coordinates logged at startup, or set `lat`/`lng` yourself.

## Privacy

Your `config.yaml`, the browser profile and session, the database, the spreadsheet and the screenshots
are personal and are all in `.gitignore`. Don't commit them. With a local LLM nothing leaves your machine
except the requests to the flat sites, Telegram and the routing service; with a cloud LLM, your `me:` profile and the text
of each ad are also sent to that provider.

## Credits

Ideas and selectors borrowed from other open-source WG-Gesucht and flat-hunting projects:

- **[Fredy](https://github.com/orangecoding/fredy):** current page markup, skipping paid cards,
  coordinates from the map, Transitous commute and its etiquette, repost fingerprints, scam signals.
- **[Flathunter](https://github.com/flathunters/flathunter):** commute filtering, photo-first Telegram cards.
- **MietRadar:** reading all description tabs, embedded-question prompts, template mode, inbox reply
  tracking, visible-captcha detection.
- **jonasdieker, AykoSc, nickirk/immo:** the `#message_timestamp` "already contacted" check,
  send-form selectors, removing overlays.
- **isaksolheim/gesucht:** handling code words.
- **Lauchturm:** WG-Gesucht's terms-of-use block page as a block signal.

## License

[MIT](LICENSE)
