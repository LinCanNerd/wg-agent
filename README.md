<p align="center">
  <img src="banner.jpeg" alt="wg-agent: your flat-hunting sidekick for WG-Gesucht" width="100%">
</p>

A flat-hunting assistant that runs on your own machine. It watches your
[WG-Gesucht](https://www.wg-gesucht.de), [Kleinanzeigen](https://www.kleinanzeigen.de) and
[ImmoScout24](https://www.immobilienscout24.de) searches, uses an **LLM** (local or cloud) to score each
new ad and draft a personal first message in German or English, and sends you a card in **Telegram**.
**Nothing is ever sent until you tap ✅.**

> **Disclaimer:** this is an unofficial personal tool, not affiliated with WG-Gesucht, Kleinanzeigen or ImmoScout24.
> Automated access may be against the sites' terms of use. Use it at your own risk, keep the request volume
> low (the defaults are conservative) and never remove the human approval step.

## Features

- **Filters first:** company ads, swap offers, old, too expensive or unsuitable ads are dropped before they're opened.
- **Reads the whole ad:** all description tabs, coordinates, flatmates, and, with WG-Gesucht Plus, the applicant count.
- **Real commute times** by bike and public transport, via the free [Transitous](https://transitous.org) service
  (or Google).
- **Drafts:** a 0–10 score, scam signals, answers to the ad's questions, and an English translation of German
  drafts. By default your own fixed text is sent, plus one sentence about the ad.
- **Code words:** finds tests hidden in the ad ("write *Banane* in your message"), puts them in the message
  and checks again before you send.
- **No double messages:** skips reposts, the same flat on another site, and people you've already written to.
- **Reply tracking:** pings you when someone answers (WG-Gesucht, Kleinanzeigen) and logs everything in `applications.xlsx`.

| Site | Reads | Sends |
|---|---|---|
| WG-Gesucht | website, logged in | after you tap ✅ |
| Kleinanzeigen | website, logged in | after you tap ✅; a dry run until you set `sites.kleinanzeigen.dry_run: false`, because sending isn't verified live yet |
| ImmoScout24 | app API, no login | never: copy the draft, send it from the app, tap `📤 I sent it`. 🔒 on a card means only Plus members can write for now |

Immowelt isn't supported (captcha on the first page).

## How it works

```
search page ──► card filter ──► ad page ──► filters, repost & same-person check
                                                   │
          Telegram card ◄── LLM score + draft ◄── commute (Transitous)
                │
          you tap ✅ ──► the site's contact form ──► applications.xlsx ◄── inbox replies
```

## Setup

You need an always-on Linux or macOS computer with Python 3.10+, an [LLM](#choosing-an-llm), a Telegram bot
token (from [@BotFather](https://t.me/BotFather)), your Telegram user id (from
[@userinfobot](https://t.me/userinfobot)), and an account on each site you search.

```bash
git clone <this repo> wg-agent && cd wg-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium     # on ARM64, try without --with-deps if it fails

cp config.example.yaml config.yaml          # then fill in everything marked TODO, and pick an LLM
python bot.py --login                       # once: opens a browser window, log in to each site you search
python bot.py
```

In Telegram, open your bot and press **Start**. `--login` needs a display (`ssh -X` or VNC on a headless
server); skip it if you only search ImmoScout24.

**Test first:** keep `send.dry_run: true` and send `/test <ad url>` for a few ads. ✅ then fills in the contact
form and sends you a screenshot instead of sending. When you're happy, set `dry_run: false`.

## Choosing an LLM

Any OpenAI-compatible API works: set `llm.base_url`, `llm.model` and, for cloud providers, `llm.api_key` (or
`LLM_API_KEY`). `config.example.yaml` lists the URLs.

| | Local (Ollama, LM Studio, vLLM) | Cloud (OpenAI, Anthropic, Gemini, ...) |
|---|---|---|
| Hardware | A strong GPU or Apple Silicon | None beyond the always-on computer |
| Cost | Free | One or two requests per ad |
| Privacy | Stays on your machine | Your profile and the ads go to the provider |

In our tests `gemma4:31b` wrote the best German; `gemma4:26b` and `qwen3:30b-a3b-instruct-2507` are much faster
with a few slips; 8B models tend to invent facts about you.

## Configuration

Everything is in `config.yaml`; `config.example.yaml` documents every option. The main parts:

- **`me`:** who you are. The model only uses facts written here, so be complete and honest; if an ad asks
  something your profile doesn't answer, it may guess. With `mode: template` your own text is sent word for word, with a greeting
  and one sentence about the ad filled in; with `full` the model writes everything.
- **`search.searches`:** one entry per search URL, each with its own budget and minimum score.
- **`commute.destinations`:** places you travel to, with limits. `action: exclude` drops ads that are too far,
  `mark` only warns.
- **Another city:** set `city`, use its search URLs, and adjust `districts_exclude` and `commute.viewbox`.

## Telegram commands

| Command | |
|---|---|
| `/status` | what the agent is doing, page budget, database counts |
| `/check`, `/replies` | check the searches / inboxes now |
| `/test <url>` | score and draft any ad (ignores filters) |
| `/excel` | get `applications.xlsx` |
| `/summary` | the daily review right now |
| `/pause`, `/resume` | stop / restart polling |
| `/login_check` | is the browser still logged in to each site? |

## Run it as a service

`wg-agent.service` is a systemd **user** unit. It assumes the repo is in `~/wg-agent`; edit the paths if not.

```bash
mkdir -p ~/.config/systemd/user && cp wg-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now wg-agent
sudo loginctl enable-linger "$USER"    # keep it running after you log out
journalctl --user -u wg-agent -f
```

### Several people on one machine

Give each person a folder with their own `config.yaml` and Telegram bot; their database, spreadsheet and
logins stay in that folder.

```bash
mkdir -p ~/wg-agent-people/anna
cp ~/wg-agent/config.example.yaml ~/wg-agent-people/anna/config.yaml   # fill it in for Anna
cd ~/wg-agent-people/anna && ~/wg-agent/.venv/bin/python ~/wg-agent/bot.py --login   # with Anna's accounts
cd ~/wg-agent-people/anna && ~/wg-agent/.venv/bin/python ~/wg-agent/bot.py
```

As a service: `systemctl --user enable --now wg-agent@anna` (setup is in the comments of `wg-agent@.service`).
All agents share your IP address, so check each site less often (`every_n_polls: 2` or more).

## Staying polite to the sites

- Each site has its own budget of 40 page loads per hour and 650 per day, kept across restarts.
- Searches run every 2–3.5 min (every ~20 min at night) and open at most 3 ads per check.
- A captcha, HTTP 403/429/503, block page or suddenly empty search pauses that site for 15 min, then 1, 3 and
  12 h, and you get a Telegram note. No captcha solving, no anti-detection stealth.

## Troubleshooting

- **Nothing found:** the sites change their HTML now and then. The selectors are in `wg.py` and `kleinanzeigen.py`.
- **ImmoScout24 keeps failing:** the API may want a newer app version; set `sites.immoscout.user_agent`.
- **Logged out:** run `python bot.py --login` again (`--login kleinanzeigen` for just that site).
- **Send problems:** every send attempt leaves a screenshot in `screenshots/`.
- **Commute missing:** check the coordinates logged at startup, or set `lat`/`lng` yourself.

## Privacy

Your config, browser profile and session, database, spreadsheet and screenshots are personal and in
`.gitignore`; don't commit them. With a local LLM, nothing leaves your machine except requests to the flat
sites, Telegram and the routing service. With a cloud LLM, your `me:` profile and the ads also go to the provider.

## Credits

Ideas and selectors borrowed from [Fredy](https://github.com/orangecoding/fredy),
[Flathunter](https://github.com/flathunters/flathunter), MietRadar, jonasdieker, AykoSc, nickirk/immo,
isaksolheim/gesucht and Lauchturm.

## License

[MIT](LICENSE)
