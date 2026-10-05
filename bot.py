"""Flat-ad watcher (WG-Gesucht, Kleinanzeigen, ImmoScout24) + Telegram approval bot.

Usage:
    python bot.py --login             # once: log in to every site you search, in a visible browser
    python bot.py --login kleinanzeigen   # only one site
    python bot.py                     # run the agent
"""

import argparse
import asyncio
import html
import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from commute import Commute
from guard import BudgetExceeded, CoolingDown, RateGuard
from immoscout import ImmoScout
from kleinanzeigen import Kleinanzeigen
from llm import LLM, detect_language, first_name, has_number
from sites import Blocked, Browser, Listing, LoggedOut, Site, interactive_login, is_commercial
from tracker import looks_like_mine, match_conversation, write_excel
from wg import WGGesucht

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("agent")

CFG: dict = {}
SITE_TYPES = (WGGesucht, Kleinanzeigen, ImmoScout)
SITES: dict[str, Site] = {}  # every supported site; only the ones with a search are polled


# ---------------- storage ----------------
class DB:
    def __init__(self, path="wg.sqlite"):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute(
            """CREATE TABLE IF NOT EXISTS listings(
                 id TEXT PRIMARY KEY, url TEXT, title TEXT, data TEXT, status TEXT,
                 note TEXT, lang TEXT, score INTEGER, draft TEXT, created REAL)"""
        )
        self.c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
        for col in (
            "fp TEXT",
            "sent_at REAL",
            "reply_at REAL",
            "reply_text TEXT",
            "conv_url TEXT",
            "poster_id TEXT",
            "site TEXT",  # NULL in rows from before Kleinanzeigen: WG-Gesucht
        ):
            try:
                self.c.execute(f"ALTER TABLE listings ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.c.commit()

    def count_search(self, site, kind):
        return self.c.execute(
            "SELECT COUNT(*) FROM listings WHERE COALESCE(site, 'wg-gesucht')=? AND data LIKE ?",
            (site, f'%"kind": "{kind}"%'),
        ).fetchone()[0]

    def seen(self, lid):
        return self.c.execute("SELECT 1 FROM listings WHERE id=?", (lid,)).fetchone() is not None

    def add(self, l: Listing, status="new"):
        self.c.execute(
            "INSERT OR IGNORE INTO listings(id,url,title,data,status,created,site) VALUES(?,?,?,?,?,?,?)",
            (l.id, l.url, l.title, json.dumps(l.to_dict()), status, time.time(), l.site),
        )
        self.c.commit()

    def update(self, lid, **f):
        if "data" in f and isinstance(f["data"], Listing):
            f["data"] = json.dumps(f["data"].to_dict())
        cols = ", ".join(f"{k}=?" for k in f)
        self.c.execute(f"UPDATE listings SET {cols} WHERE id=?", (*f.values(), lid))
        self.c.commit()

    def get(self, lid):
        return self.c.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()

    def queued(self, n):
        rows = self.c.execute("SELECT * FROM listings WHERE status='queued'").fetchall()
        # freshest ad first (by WG-Gesucht's "Online: x min"), not by id – ids aren't chronological
        rows.sort(key=lambda r: (json.loads(r["data"]).get("online_min") or 10**6, -r["created"]))
        return rows[:n]

    def expire_queue(self, hours):
        self.c.execute(
            "UPDATE listings SET status='stale' WHERE status='queued' AND created < ?", (time.time() - hours * 3600,)
        )
        self.c.commit()

    def repost_of(self, fp, lid):
        """Same flat re-uploaded under a new id (Fredy/jonasdieker idea)."""
        r = self.c.execute(
            "SELECT id, status FROM listings WHERE fp=? AND id<>? AND created > ? "
            "AND status IN ('pending','sent','skipped','low_score','filtered','already') LIMIT 1",
            (fp, lid, time.time() - 30 * 86400),
        ).fetchone()
        return r

    def copy_on_other_site(self, l: Listing):
        """The same flat, already handled as an ad on another site in the last 30 days: (row, why) or None."""
        rows = self.c.execute(
            "SELECT id, status, data, COALESCE(site, 'wg-gesucht') AS site FROM listings "
            "WHERE COALESCE(site, 'wg-gesucht') <> ? AND created > ? "
            "AND status IN ('pending','sent','replied','skipped','low_score','already')",
            (l.site, time.time() - 30 * 86400),
        ).fetchall()
        for r in rows:
            if why := same_flat(l, Listing.from_dict(json.loads(r["data"]))):
                return r, why
        return None

    def _poster_id_ok(self, pid):
        """An advertiser id seen with more than 3 different names isn't one person's id (a parser mix-up,
        like reading my own id from the logged-in page): don't block ads over it."""
        names = {
            json.loads(r[0]).get("poster")
            for r in self.c.execute("SELECT data FROM listings WHERE poster_id=?", (pid,))
        }
        if len(names) > 3:
            log.warning(
                "advertiser id %s has %d different names: ignoring it for the same-person check", pid, len(names)
            )
            return False
        return True

    def same_person(self, l: Listing):
        """Have I already written to this advertiser (any of their ads)?"""
        if l.poster_id and self._poster_id_ok(l.poster_id):
            r = self.c.execute(
                "SELECT id, title FROM listings WHERE poster_id=? AND id<>? AND status IN ('sent','replied','already')",
                (l.poster_id, l.id),
            ).fetchone()
            if r:
                return r
        if l.poster and len(l.poster) > 3:  # fallback without id: same name + same street
            street = (l.address or l.street or "").split()[0:1]
            for r in self.c.execute(
                "SELECT id, title, data FROM listings WHERE id<>? AND status IN ('sent','replied','already')", (l.id,)
            ):
                d = json.loads(r["data"])
                if (
                    d.get("poster") == l.poster
                    and street
                    and (d.get("address") or d.get("street") or "").split()[0:1] == street
                ):
                    return r
        return None

    def sent_rows(self, site=None):
        rows = self.c.execute("SELECT * FROM listings WHERE status IN ('sent','replied','already')").fetchall()
        return [r for r in rows if site is None or (r["site"] or "wg-gesucht") == site]

    def kv_get(self, k, default=None):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v)))
        self.c.commit()

    def stats(self):
        return dict(self.c.execute("SELECT status, COUNT(*) FROM listings GROUP BY status").fetchall())


def fingerprint(l: Listing) -> str:
    street = re.sub(r"[^a-zäöüß]", "", (l.address or l.street or "").lower().split("\n")[0])[:25]
    fp = f"{l.kind}|{street}|{l.size}|{round((l.rent or 0) / 25)}"
    if not l.street and l.poster_id:  # only the postcode area is known: too vague without the advertiser
        fp += f"|{l.poster_id}"
    return fp


def _street_name(l: Listing) -> str:
    """'Plinganserstr. 12' and 'Plinganserstraße 12,' -> 'plinganserstraße'."""
    s = re.split(r"\d", (l.street or "").lower())[0]
    s = re.sub(r"[^a-zäöüß]", "", re.sub(r"str\.|strasse", "straße", s))
    return s if len(s) > 4 else ""


def _postcode(l: Listing) -> str:
    m = re.search(r"\b(\d{5})\b", f"{l.address} {l.card_text[:300]}")
    return m.group(1) if m else ""


def _text_overlap(a: str, b: str) -> float | None:
    """Share of the shorter ad text's 3-word phrases that the other one has too; None if too short to tell."""

    def phrases(t):
        w = re.findall(r"[a-zäöüß0-9]+", t.lower())
        return {" ".join(w[i : i + 3]) for i in range(len(w) - 2)}

    pa, pb = phrases(a), phrases(b)
    if min(len(pa), len(pb)) < 12:
        return None
    return len(pa & pb) / min(len(pa), len(pb))


def same_flat(a: Listing, b: Listing) -> str | None:
    """Is b the same flat as a, advertised on another site? Returns why, or None. Both need details.
    Agencies reuse their text for different flats, so the text alone isn't enough: size or rent must match."""
    size_ok = bool(a.size and b.size and abs(a.size - b.size) <= 1)
    rent_ok = bool(a.rent and b.rent and abs(a.rent - b.rent) <= max(30, 0.05 * max(a.rent, b.rent)))
    text = _text_overlap(a.description, b.description)
    if text is not None and text >= 0.6 and (size_ok or rent_ok):
        return f"same ad text ({text:.0%})"
    street = _street_name(a)
    if street and street == _street_name(b) and size_ok and rent_ok:
        return "same street, size and rent"
    pc = _postcode(a)
    if pc and pc == _postcode(b) and size_ok and rent_ok and text is not None and text >= 0.2:
        return f"same postcode, size and rent, similar text ({text:.0%})"
    return None


# ---------------- filtering ----------------
def _d(s):
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s), fmt).date()
        except (TypeError, ValueError):
            pass
    return None


def site_for_url(url) -> Site:
    for site in SITES.values():
        if site.handles(url):
            return site
    raise ValueError(f"not a supported site ({', '.join(s.label for s in SITES.values())}): {url}")


def active_sites() -> list[Site]:
    """Sites with at least one search in config.yaml."""
    names = dict.fromkeys(site_for_url(sc["url"]).name for sc in CFG["search"]["searches"])
    return [SITES[n] for n in names]


def site_dry_run(site: Site) -> bool:
    """sites.<name>.dry_run wins; a site whose sending isn't verified yet stays a dry run by default."""
    own = (CFG.get("sites") or {}).get(site.name) or {}
    if "dry_run" in own:
        return bool(own["dry_run"])
    return True if not site.send_verified else CFG.get("send", {}).get("dry_run", True)


def search_cfg(l: Listing):
    """The search an ad came from: same site and kind, else the same kind on another site."""
    same_kind = [sc for sc in CFG["search"]["searches"] if sc.get("kind", "room") == l.kind]
    for sc in same_kind:
        if site_for_url(sc["url"]).name == l.site:
            return sc
    return same_kind[0] if same_kind else {}


def hard_filter(l: Listing, extra_text="") -> str | None:
    s = {**CFG["search"], **search_cfg(l)}
    if s.get("max_rent") and l.rent and l.rent > s["max_rent"]:
        return f"rent {l.rent} €"
    if s.get("min_size") and l.size and l.size < s["min_size"]:
        return f"size {l.size} m²"
    if s.get("max_ad_age_hours") and l.online_min and l.online_min > s["max_ad_age_hours"] * 60:
        return f"old ad ({l.online})"
    if l.exchange:
        return "Tauschangebot"
    if is_commercial(l.poster):  # names known only from the ad page (ImmoScout search results have none)
        return f"commercial provider ({l.poster})"
    hay = f"{l.title} {l.card_text} {extra_text}".lower()
    for kw in s.get("keywords_exclude") or []:
        if kw.lower() in hay:
            return f"keyword '{kw}'"
    loc = f"{l.district} {l.address} {l.card_text}".lower()
    inc = s.get("districts_include") or []
    if inc and not any(d.lower() in loc for d in inc):
        return f"district '{l.district}'"
    for d in s.get("districts_exclude") or []:
        if d.lower() in loc:
            return f"district '{d}'"
    g = (CFG["me"].get("gender") or "").lower()
    w = (l.wanted or "").lower()
    if l.kind == "room" and g in ("m", "w"):
        only_f = re.search(
            r"(^|[;,]\s*)mitbewohnerin gesucht|^frau(en)? |nur (für )?frauen|female only|only (girls|women|female)",
            w + " " + hay,
        )
        only_m = re.search(
            r"(^|[;,]\s*)mitbewohner gesucht|^mann |^männer |nur (für )?männer|male only|only (guys|men|male)",
            w + " " + hay,
        )
        if g == "m" and only_f and "oder" not in w:
            return "looking for women only"
        if g == "w" and only_m and "oder" not in w:
            return "looking for men only"
    af, lim = _d(l.available_from), _d(s.get("available_from_before"))
    if af and lim and af > lim:
        return f"free from {l.available_from}"
    mdm = s.get("min_duration_months") or 0
    to = _d(l.available_to)
    if mdm and af and to and (to - af).days < mdm * 30:
        return f"only {l.available_from}–{l.available_to}"
    return None


# ---------------- telegram UI ----------------
def link_buttons(l: Listing) -> list:
    links = [InlineKeyboardButton(f"🔗 {SITES[l.site].label if l.site in SITES else 'Ad'}", url=l.url)]
    if l.lat:
        links.append(
            InlineKeyboardButton("🗺 Map", url=f"https://www.google.com/maps/search/?api=1&query={l.lat},{l.lng}")
        )
        dest = (CFG.get("commute") or {}).get("destinations") or []
        if dest:
            links.append(
                InlineKeyboardButton(
                    "🚲 Route",
                    url=f"https://www.google.com/maps/dir/?api=1&origin={l.lat},{l.lng}"
                    f"&destination={html.escape(dest[0].get('query', ''))}&travelmode=bicycling",
                )
            )
    return links


def links_keyboard(l: Listing):
    """What stays on a card once it's sent or skipped: the ad, map and route links, no action buttons."""
    return InlineKeyboardMarkup([link_buttons(l)])


def by_hand(l: Listing) -> bool:
    """A site the agent can't send on (ImmoScout24): you send in its app and tap "I sent it"."""
    return l.site in SITES and SITES[l.site].send_by_hand


def keyboard(lid, l: Listing | None = None):
    send = (
        InlineKeyboardButton("📤 I sent it", callback_data=f"sent:{lid}")
        if l and by_hand(l)
        else InlineKeyboardButton("✅ Send", callback_data=f"send:{lid}")
    )
    rows = [
        [send, InlineKeyboardButton("❌ Skip", callback_data=f"skip:{lid}")],
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{lid}"),
            InlineKeyboardButton("🔁 Rewrite", callback_data=f"rewrite:{lid}"),
        ],
    ]
    if l:
        rows.append(link_buttons(l))
    return InlineKeyboardMarkup(rows)


def card_text(l: Listing, r, meta, with_translation=True):
    e = html.escape
    kind = "🏢 STUDIO" if l.kind == "studio" else "🏠 WG"
    if len(active_sites()) > 1 and l.site in SITES:
        kind += f" · {SITES[l.site].label}"
    lines = [
        f"{kind} <b>{e(l.title or 'Angebot')}</b>",
        f"💶 {l.rent or '?'} € · 📐 {l.size or '?'} m² · 📍 {e(l.address or (l.street + ', ' + l.district))}",
        f"📅 {l.available_from or '?'} → {l.available_to or 'unbefristet'} · 🗣 {(r['lang'] or 'de').upper()}"
        + (f" · ⏱ {e(l.online.replace('Online: ', ''))}" if l.online else ""),
    ]
    who = " · ".join(
        filter(
            None,
            [
                l.flatmates,
                l.wanted,
                f"by {first_name(l.poster) or l.poster}" if l.poster else "",
                f"member since {l.member_since}" if l.member_since else "",
            ],
        )
    )
    if who:
        lines.append(f"👥 {e(who)}")
    if l.commute_text:
        lines.append(f"🧭 {e(l.commute_text)}")
    if l.applicants is not None:
        lines.append(f"📨 {l.applicants} applicants so far")
    if l.contact_note:
        lines.append(f"🔒 {e(l.contact_note)}")
    if r["score"] is not None:
        lines.append(f"⭐ {r['score']}/10 — {e(meta.get('reasons') or '')}")
    for w in meta.get("warnings") or []:
        lines.append(f"⚠️ {e(w)}")
    if meta.get("red_flags"):
        lines.append(f"🚩 {e(meta['red_flags'])}")
    if kw := meta.get("keyword"):  # re-checked on every card, so a manual edit can't silently drop it
        draft = (r["draft"] or "").strip()
        at_start = meta.get("keyword_at_start")
        sentence = next((s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", draft) if kw.lower() in s.lower()), "")
        if not sentence:
            status = "⛔ MISSING from the draft"
        elif meta.get("keyword_subject"):  # no subject line on the sites: alone on the first line instead
            status = (
                "✅ as the first line (instead of a subject)"
                if draft.split("\n")[0].strip() == kw
                else ("⚠️ should be alone on the first line")
            )
        elif at_start and not draft.lower().startswith(kw.lower()):
            status = "⚠️ not the first word"
        elif sentence.lower().strip(" .,!?") == kw.lower():
            status = "⚠️ stands alone, not in a sentence"
        else:
            status = "✅"
        if not meta.get("traps") and status.startswith("✅"):  # the model found one, the text search didn't
            status += " (not sure: the ad has no clear request to write it)"
        lines.append(f"🔑 Code word <b>{e(kw)}</b> ({'must be the first word' if at_start else 'anywhere'}) {status}")
        if sentence:
            lines.append(f"      ↳ <i>{e(sentence[:300])}</i>")
    if traps := meta.get("traps"):  # straight from the ad text, so a test the model overlooked still shows
        lines.append(
            "🪤 The ad says:" if kw else "🪤 Possible test in the ad, and the model found no code word. Check:"
        )
        lines += [f"      <i>{e(t[:200])}</i>" for t in traps]
    if (phone := CFG["me"].get("whatsapp")) and not has_number(r["draft"] or "", phone):
        lines.append("⚠️ Your WhatsApp number is missing from the draft (or a digit is wrong)")
    if meta.get("questions"):
        lines.append(f"❓ Answered: {e(meta['questions'])}")
    if meta.get("notes"):
        lines.append("\n<b>Notes:</b>\n" + "\n".join(f"📝 {e(n)}" for n in meta["notes"]))
    draft = (r["draft"] or "").strip() or "(no draft: the model rejected this ad. Use 🔁 Rewrite to get one anyway)"
    if by_hand(l):
        lines.append(
            f"\n✍️ <b>Send this one yourself:</b> tap the draft to copy it, open the ad with 🔗 "
            f"{SITES[l.site].label}, write to the advertiser there, then tap 📤 I sent it."
        )
    lines.append(f"\n<b>Draft:</b>\n<pre>{e(draft[:3000])}</pre>")
    if with_translation and meta.get("translation"):
        lines.append(
            f"<b>🇬🇧 In English (just for you, not sent):</b>\n"
            f"<blockquote expandable>{e(meta['translation'][:3000])}</blockquote>"
        )
    return "\n".join(lines)


def tg_len(text_html):
    """Length Telegram counts against its 4096 limit: text after the HTML tags are parsed."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text_html)))


async def post_card(bot, lid, with_photo=True):
    r = DB_.get(lid)
    l = Listing.from_dict(json.loads(r["data"]))
    meta = json.loads(r["note"]) if (r["note"] or "").startswith("{") else {}
    tg = CFG.get("telegram", {})
    chat = tg["chat_id"]
    reply_to = None
    photos = (l.images or ([l.image] if l.image else []))[: min(tg.get("max_photos", 10), 10)]  # album max 10
    if with_photo and photos and tg.get("photos", True):
        # Telegram fetches the images itself – costs nothing from our WG-Gesucht budget.
        # One bad URL fails the whole album, so fall back to the main photo alone.
        for attempt in ([photos] if len(photos) > 1 else []) + [photos[:1]]:
            try:
                if len(attempt) > 1:
                    msgs = await bot.send_media_group(
                        chat, [InputMediaPhoto(u) for u in attempt], disable_notification=True
                    )
                    reply_to = msgs[0].message_id
                else:
                    reply_to = (await bot.send_photo(chat, attempt[0], disable_notification=True)).message_id
                break
            except Exception as ex:
                log.warning("sending %d photo(s) failed: %s", len(attempt), ex)
    text = card_text(l, r, meta)
    # long draft + translation: the translation goes in its own message
    separate = bool(meta.get("translation")) and tg_len(text) > 4000
    if separate:
        text = card_text(l, r, meta, with_translation=False)
    card = await bot.send_message(
        chat,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard(lid, l),
        disable_web_page_preview=True,
        reply_to_message_id=reply_to,
    )
    if separate:
        await bot.send_message(
            chat,
            f"<b>🇬🇧 In English (just for you, not sent):</b>\n"
            f"<blockquote expandable>{html.escape(meta['translation'][:3500])}</blockquote>",
            parse_mode=ParseMode.HTML,
            reply_to_message_id=card.message_id,
            disable_notification=True,
        )


async def english_version(text, keyword=""):
    """Translation of a German draft for the card; empty for English drafts or if the model fails."""
    if not text or detect_language(text) != "de":
        return ""
    try:
        return await LLM_.translate(text, keyword or "")
    except Exception:
        log.exception("translation failed")
        return ""


async def notify(bot, text, important=False):
    """Agent updates. With telegram.quiet they wait for the daily summary; `important` ones (you have to act,
    or you asked) are always sent right away."""
    if CFG["telegram"].get("quiet") and not important:
        log.info("for the daily summary: %s", text)
        events = [ev for ev in DB_.kv_get("events", []) if ev["t"] > time.time() - 7 * 86400]
        DB_.kv_set("events", events + [{"t": time.time(), "text": text}])
        return
    await bot.send_message(CFG["telegram"]["chat_id"], text, disable_web_page_preview=True)


FILTER_REASONS = [  # how hard_filter / commute notes start -> words for the daily summary
    (r"^rent", "too expensive"),
    (r"^size", "too small"),
    (r"^old ad", "online too long"),
    (r"^Tauschangebot", "swap offer"),
    (r"^keyword", "excluded word"),
    (r"^district", "outside the city"),
    (r"only$", "wrong gender"),
    (r"^free from", "free too late"),
    (r"^only ", "sublet too short"),
    (r"^too far from", "too far from work"),
    (r"^commercial", "commercial provider"),
]


def summary_text(hours=24):
    since = time.time() - hours * 3600
    rows = DB_.c.execute(
        "SELECT status, score, note, COALESCE(site, 'wg-gesucht') AS site FROM listings "
        "WHERE created > ? AND status NOT IN ('preexisting', 'test')",
        (since,),
    ).fetchall()
    by, per_site = {}, {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
        per_site[r["site"]] = per_site.get(r["site"], 0) + 1
    reasons = {}
    for r in rows:
        if r["status"] == "filtered":
            label = next((lbl for pat, lbl in FILTER_REASONS if re.search(pat, r["note"] or "")), "other")
            reasons[label] = reasons.get(label, 0) + 1
    cards = [r for r in rows if r["status"] in ("pending", "sent", "skipped", "already", "replied")]
    sent = DB_.c.execute("SELECT COUNT(*) FROM listings WHERE sent_at > ?", (since,)).fetchone()[0]
    replies = DB_.c.execute("SELECT COUNT(*) FROM listings WHERE reply_at > ?", (since,)).fetchone()[0]
    sent_all = len(DB_.sent_rows())
    replied_all = DB_.c.execute("SELECT COUNT(*) FROM listings WHERE reply_at IS NOT NULL").fetchone()[0]
    waiting = DB_.c.execute("SELECT COUNT(*) FROM listings WHERE status='pending'").fetchone()[0]
    events = [ev for ev in DB_.kv_get("events", []) if ev["t"] > since]
    best = max((r["score"] for r in cards if r["score"] is not None), default=None)

    top = ", ".join(f"{n} {lbl}" for lbl, n in sorted(reasons.items(), key=lambda x: -x[1])[:4])
    sites = active_sites()
    split = ", ".join(f"{s.label} {per_site.get(s.name, 0)}" for s in sites)
    lines = [
        f"📋 <b>Daily review</b> ({datetime.now():%a %d %b}, last {hours} h)",
        f"• New ads: {len(rows)}" + (f" ({split})" if len(sites) > 1 else ""),
        f"• Filtered out automatically: {by.get('filtered', 0)}" + (f" ({top})" if top else ""),
        f"• Reposts / same flat on another site / already contacted: "
        f"{by.get('repost', 0) + by.get('duplicate', 0) + by.get('same_person', 0)}",
        f"• Below your minimum score: {by.get('low_score', 0)}",
        f"• 🏠 Cards sent to you: {len(cards)}" + (f" (best {best}/10)" if best is not None else ""),
        f"• ✅ Messages sent: {sent} (total {sent_all})",
        f"• 💬 Replies: {replies} (total {replied_all})",
        f"• ⏳ Cards waiting for your decision: {waiting}",
    ]
    if by.get("stale") or by.get("error") or by.get("gone"):
        lines.append(
            f"• Not processed: {by.get('stale', 0)} too old in the queue, {by.get('error', 0)} errors, "
            f"{by.get('gone', 0)} taken offline first"
        )
    lines += [f"• {s.label} {html.escape(s.guard.status())}" for s in sites]
    if events:
        lines.append(f"• Agent notes ({len(events)}):")
        lines += [f"   – {datetime.fromtimestamp(ev['t']):%H:%M} {html.escape(ev['text'][:160])}" for ev in events[-5:]]
    return "\n".join(lines)


async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_message(
        CFG["telegram"]["chat_id"], summary_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


# ---------------- core pipeline ----------------
async def process(bot, l: Listing, force=False):
    """Details -> filters -> commute -> LLM -> Telegram card. force=True skips filters (for /test)."""
    try:
        await SITES[l.site].fetch_details(l)
    except LookupError as e:
        DB_.update(l.id, status="gone", note=str(e))
        if force:
            await notify(bot, f"Couldn't read the ad: {e}", important=True)  # you asked with /test
        return
    fp = fingerprint(l)
    DB_.update(l.id, data=l, title=l.title, fp=fp, poster_id=l.poster_id or None)
    if not force:
        if why := hard_filter(l, l.description):
            DB_.update(l.id, status="filtered", note=why)
            log.info("filtered %s: %s", l.id, why)
            return
        if prev := DB_.same_person(l):
            DB_.update(l.id, status="same_person", note=f"already wrote to this person (ad {prev['id']})")
            log.info("same person %s (ad %s)", l.id, prev["id"])
            return
        if prev := DB_.repost_of(fp, l.id):
            DB_.update(l.id, status="repost", note=f"repost of {prev['id']} ({prev['status']})")
            log.info("repost %s of %s", l.id, prev["id"])
            return
        if dup := DB_.copy_on_other_site(l):  # e.g. the same room on WG-Gesucht and Kleinanzeigen
            prev, why = dup
            where = SITES[prev["site"]].label if prev["site"] in SITES else prev["site"]
            DB_.update(l.id, status="duplicate", note=f"same flat as {where} ad {prev['id']} ({prev['status']}): {why}")
            log.info("duplicate %s of %s ad %s: %s", l.id, where, prev["id"], why)
            return

    res, l.commute_text = await COMMUTE_.for_listing(l)
    l.commute = res
    DB_.update(l.id, data=l)
    why, warnings = COMMUTE_.check(res)
    if l.poster and l.poster in (DB_.kv_get("inbox_names", []) or []):
        warnings.append(f"you already have a chat with someone called {l.poster} – check it's not the same person")
    if why and not force:
        DB_.update(l.id, status="filtered", note=why)
        log.info("filtered %s: %s", l.id, why)
        return

    lang = CFG.get("language", "auto")
    if lang not in ("de", "en"):
        lang = detect_language(l.title + "\n" + l.description)
    try:
        out = await LLM_.evaluate(l, lang, COMMUTE_.prompt_text(res))
    except Exception as e:
        log.exception("LLM failed")
        DB_.update(l.id, status="error", note=str(e)[:500])
        await notify(bot, f"⚠️ New ad but drafting failed ({e.__class__.__name__}): {l.url}")
        return
    if out.get("commute_ok") is False and res:
        warnings.append("the model thinks the commute isn't feasible")
    keys = ("reasons", "red_flags", "keyword", "keyword_at_start", "keyword_subject", "traps", "questions", "notes")
    meta = {k: out.get(k) for k in keys}
    meta["warnings"] = warnings
    DB_.update(l.id, lang=lang, score=out["score"], draft=out["message"], note=json.dumps(meta))
    min_score = search_cfg(l).get("min_score", CFG["search"].get("min_score", 0))
    if not force and out["score"] < min_score:
        DB_.update(l.id, status="low_score")
        log.info("low score %s: %s (%s)", l.id, out["score"], out["reasons"])
        return
    meta["translation"] = await english_version(out["message"], meta.get("keyword"))  # only for ads you'll see
    DB_.update(l.id, status="pending", note=json.dumps(meta))
    await post_card(bot, l.id)


def next_delay():
    p = CFG["poll"]
    n0, n1 = p.get("night_hours", [1, 7])
    night = n0 <= datetime.now().hour < n1
    base = p.get("night_interval_seconds", 900) if night else p.get("interval_seconds", 120)
    d = base + random.uniform(0, p.get("jitter_seconds", 0))
    cooling = [s.guard.cooling_left() for s in active_sites()]
    if cooling and all(cooling):  # every site is cooling down: wait for the first to finish
        d = max(d, min(cooling) + random.uniform(30, 120))
    return d


async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    """Self-scheduling loop: irregular intervals, slower at night, waits out cooldowns."""
    try:
        await poll_once(ctx)
    except Exception:
        log.exception("poll crashed")
    finally:
        ctx.job_queue.run_once(tick, next_delay(), name="poll")


async def on_block(bot, site: Site, why):
    wait = site.guard.strike()
    await notify(
        bot,
        f"🛑 {site.label} pushed back ({why}). Cooling down {wait // 60} min "
        f"(strike {site.guard.strikes}). Nothing to do; I'll resume by myself.",
    )


async def on_budget(bot, st, site: Site):
    hour = datetime.now().strftime("%Y-%m-%d %H")
    log.warning("%s page budget reached: %s", site.label, site.guard.status())
    if st["budget_warned"].get(site.name) != hour:
        st["budget_warned"][site.name] = hour
        await notify(
            bot,
            f"⏳ Hit my own safety budget for {site.label} ({site.guard.status()}). "
            "Skipping its checks until it frees up.",
        )


async def site_trouble(bot, st, site: Site, e) -> bool:
    """Handles a block / budget / cooldown of one site; True if it was one (the site sits out this poll)."""
    if isinstance(e, Blocked):
        await on_block(bot, site, e)
    elif isinstance(e, BudgetExceeded):
        await on_budget(bot, st, site)
    elif not isinstance(e, CoolingDown):
        return False
    return True


async def poll_once(ctx: ContextTypes.DEFAULT_TYPE):
    st = ctx.bot_data
    if st["paused"] or st["poll_lock"].locked():
        return
    async with st["poll_lock"]:
        await _poll(ctx, st)


async def _poll(ctx, st):
    p = CFG["poll"]
    st["n"] += 1
    resting = set()  # sites that pushed back, ran out of budget or are cooling down: skipped this poll
    due = [s for s in CFG["search"]["searches"] if (st["n"] - 1) % max(1, int(s.get("every_n_polls", 1))) == 0]
    for i, sc in enumerate(due):
        site = site_for_url(sc["url"])
        if site.name in resting or site.guard.cooling_left():
            resting.add(site.name)
            continue
        if i:
            await asyncio.sleep(random.uniform(8, 20))
        kind = sc.get("kind", "room")
        key = f"{site.name}/{kind}"
        url = site.prepare_search_url(sc["url"], sc.get("max_rent"), CFG["search"].get("min_size"))
        try:
            listings = await site.search(url, kind)
        except Exception as e:
            if await site_trouble(ctx.bot, st, site, e):
                resting.add(site.name)
                continue
            log.exception("%s search failed", site.label)
            st["fails"][key] = st["fails"].get(key, 0) + 1
            if st["fails"][key] == 3:
                await notify(ctx.bot, f"⚠️ {site.label} search failing repeatedly: {e}")
            continue
        st["fails"][key] = 0
        # Soft-block detection: a search that normally has results suddenly returns none, twice.
        prev = st["counts"].get(key, 0)
        if not listings and prev >= 5:
            st["zeros"][key] = st["zeros"].get(key, 0) + 1
            if st["zeros"][key] >= 2:
                st["zeros"][key] = 0
                await on_block(ctx.bot, site, "search suddenly returns nothing")
                resting.add(site.name)
            continue
        st["zeros"][key] = 0
        st["counts"][key] = len(listings)
        st["last_poll"] = datetime.now().strftime("%H:%M:%S")

        new = [l for l in listings if not DB_.seen(l.id)]
        if not st["seeded"].get(key) and p.get("skip_existing_on_start") and DB_.count_search(site.name, kind) == 0:
            for l in new:
                DB_.add(l, status="preexisting")
            log.info("marked %d existing %s listings as seen", len(new), key)
            new = []
        st["seeded"][key] = True
        for l in new:
            DB_.add(l, status="queued")
            if why := hard_filter(l):  # cheap check on the card, no extra page load
                DB_.update(l.id, status="filtered", note=why)

    # Open at most N ads per poll (freshest first); the rest wait for the next poll.
    DB_.expire_queue(hours=24)
    for r in DB_.queued(int(p.get("max_details_per_poll", 3))):
        l = Listing.from_dict(json.loads(r["data"]))
        site = SITES[l.site]
        if site.name in resting or site.guard.cooling_left():
            continue
        await asyncio.sleep(random.uniform(6, 15))
        try:
            await process(ctx.bot, l)
        except Exception as e:
            if await site_trouble(ctx.bot, st, site, e):
                DB_.update(l.id, status="queued")  # retry later
                resting.add(site.name)
                continue
            log.exception("process failed")
            DB_.update(l.id, status="error", note=str(e)[:500])


# ---------------- replies ----------------
async def inbox_tick(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await check_replies(ctx.bot)
    except Exception:
        log.exception("reply check failed")
    finally:
        r = CFG.get("replies", {})
        n0, n1 = CFG["poll"].get("night_hours", [1, 7])
        mins = r.get("night_check_minutes", 60) if n0 <= datetime.now().hour < n1 else r.get("check_minutes", 12)
        ctx.job_queue.run_once(inbox_tick, mins * 60 + random.uniform(0, 120), name="inbox")


async def check_replies(bot):
    """Read the inbox of every site I've sent messages on."""
    if ctx_paused(bot):
        return
    for site in SITES.values():
        sent = DB_.sent_rows(site.name)
        if not sent or site.send_by_hand or site.guard.cooling_left():
            continue
        try:
            await check_site_replies(bot, site, sent)
        except LoggedOut:
            await notify(
                bot,
                f"⚠️ Can't read your {site.label} inbox: logged out. Run python bot.py --login {site.name}.",
                important=True,
            )
        except (Blocked, BudgetExceeded, CoolingDown) as e:
            await site_trouble(bot, APP.bot_data, site, e)


async def check_site_replies(bot, site: Site, sent):
    convs = await site.inbox()
    # WG-Gesucht keeps the key it had before there were other sites
    baseline = "inbox_baseline" if site.name == "wg-gesucht" else f"inbox_baseline:{site.name}"
    first_run = not DB_.kv_get(baseline, False)
    names = set(DB_.kv_get("inbox_names", []) or [])
    names.update(c["name"] for c in convs if c.get("name"))
    DB_.kv_set("inbox_names", sorted(names))
    drafts = [r["draft"] or "" for r in sent]
    changed = False
    for c in convs:
        key = c["key"]
        sig = f"{c['when']}|{c['text'][-200:]}"
        if DB_.kv_get(key) == sig:
            continue
        DB_.kv_set(key, sig)
        mine = c.get("mine")
        if mine or (mine is None and looks_like_mine(c["text"], drafts)):
            continue  # the newest message in that chat is my own
        if c.get("ad_id"):  # the site says which ad the chat is about
            rows = [r for r in sent if r["id"] == c["ad_id"]]
        else:
            rows = match_conversation(c, sent)
            if len(rows) != 1:  # ambiguous / unknown: open the chat once to read the ad id
                ad_id = await site.conversation_ad_id(c["href"])
                rows = [r for r in sent if r["id"] == ad_id] if ad_id else []
        preview = re.sub(r"\s+", " ", c["text"]).strip()[:300]
        if rows:
            r = rows[0]
            if r["status"] != "replied":
                DB_.update(r["id"], status="replied", reply_at=time.time(), reply_text=preview, conv_url=c["href"])
                changed = True
            else:
                DB_.update(r["id"], reply_text=preview, conv_url=c["href"])
                changed = True
        if first_run:
            continue  # first run only learns what's already there
        title = rows[0]["title"] if rows else c["title"]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open chat", url=c["href"])]])
        await bot.send_message(
            CFG["telegram"]["chat_id"],
            f"💬 <b>{html.escape(c['name'] or 'Someone')}</b> replied on {site.label}"
            f"{' about ' + html.escape(title) if title else ''}\n\n<i>{html.escape(preview)}</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    if first_run:
        DB_.kv_set(baseline, True)
    if changed:
        write_excel(DB_.c, EXCEL)


def ctx_paused(bot):
    return APP.bot_data.get("paused", False)


# ---------------- handlers ----------------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat_id != CFG["telegram"]["chat_id"]:
        return
    action, lid = q.data.split(":", 1)
    r = DB_.get(lid)
    if not r:
        await q.answer("Unknown listing")
        return
    l = Listing.from_dict(json.loads(r["data"]))

    if action == "skip":
        DB_.update(lid, status="skipped")
        await q.answer("Skipped")
        await q.edit_message_reply_markup(links_keyboard(l))
    elif action == "sent":  # sent by hand in the site's app
        if r["status"] in ("sent", "replied", "already"):
            await q.answer("Already logged")
            return
        DB_.update(lid, status="sent", sent_at=time.time(), poster_id=l.poster_id or None)
        await q.answer("Logged as sent")
        await q.edit_message_reply_markup(links_keyboard(l))
        try:
            write_excel(DB_.c, EXCEL)
        except Exception:
            log.exception("excel export failed")
        await q.message.reply_text(
            f"📤 Logged in the applications sheet (/excel). I can't read your {SITES[l.site].label} inbox, "
            "so replies there won't show up here."
        )
    elif action in ("edit", "rewrite"):
        await q.answer()
        ctx.user_data["pending"] = (action, lid)
        prompt = (
            "Send me the full new message text."
            if action == "edit"
            else "What should I change? (e.g. 'shorter', 'mention I can visit Saturday'). Send '.' for a fresh version."
        )
        await q.message.reply_text(prompt)
    elif action == "send":
        if r["status"] in ("sent", "already"):
            await q.answer("Already sent")
            return
        if prev := DB_.same_person(l):
            await q.answer("Already contacted this person", show_alert=True)
            await q.message.reply_text(
                f"⛔ Not sent: you already wrote to {l.poster or 'this advertiser'} "
                f"about another ad ({prev['title'] or prev['id']})."
            )
            DB_.update(lid, status="same_person")
            await q.edit_message_reply_markup(links_keyboard(l))
            return
        await q.answer("Sending…")
        await q.edit_message_reply_markup(links_keyboard(l))  # no second ✅ while it sends; links stay
        site = SITES[l.site]
        dry = site_dry_run(site)
        try:
            status, shot, info = await site.send_message(l, r["draft"], dry_run=dry)
        except LoggedOut:
            status, shot, info = "failed", None, f"Logged out of {site.label}. Run `python bot.py --login {site.name}`."
        except CoolingDown as e:
            status, shot, info = (
                "failed",
                None,
                f"Not sending right now: {e} after a block. Try again later or send manually.",
            )
        except Blocked as e:
            await on_block(ctx.bot, site, e)
            status, shot, info = "failed", None, f"{site.label} blocked the send page."
        except Exception as e:
            log.exception("send failed")
            status, shot, info = "failed", None, f"Error: {e}"
        if status in ("sent", "already"):
            DB_.update(lid, status=status, sent_at=time.time(), poster_id=l.poster_id or None)
            try:
                write_excel(DB_.c, EXCEL)
            except Exception:
                log.exception("excel export failed")
        caption = f"{info}\n{l.url}"
        if status == "failed":
            caption = "❌ " + caption + "\nThe draft is in the card above – you can copy it and send manually."
            await q.message.edit_reply_markup(keyboard(lid, l))
        elif status == "dry_run":  # keep the card usable: after switching dry_run off, ✅ sends it for real
            own = "dry_run" in ((CFG.get("sites") or {}).get(site.name) or {}) or not site.send_verified
            setting = f"sites.{site.name}.dry_run" if own else "send.dry_run"
            caption = "🧪 " + caption + f"\nTo really send it: set {setting}: false and restart, then press ✅ again."
            await q.message.edit_reply_markup(keyboard(lid, l))
        if shot and Path(shot).exists():
            with open(shot, "rb") as f:
                await ctx.bot.send_photo(q.message.chat_id, f, caption=caption[:1000])
        else:
            await q.message.reply_text(caption, disable_web_page_preview=True)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pending = ctx.user_data.pop("pending", None)
    if not pending:
        await update.message.reply_text("Send /help for commands, or paste an ad URL with /test <url>.")
        return
    action, lid = pending
    r = DB_.get(lid)
    text = update.message.text
    if action == "edit":
        draft = text
    else:
        await update.message.reply_text("✍️ Rewriting…")
        l = Listing.from_dict(json.loads(r["data"]))
        draft = await LLM_.rewrite(l, r["lang"] or "de", r["draft"], text, COMMUTE_.prompt_text(l.commute))
    meta = json.loads(r["note"]) if (r["note"] or "").startswith("{") else {}
    meta["translation"] = await english_version(draft, meta.get("keyword"))  # keep it in sync with the draft
    DB_.update(lid, draft=draft, note=json.dumps(meta))
    await post_card(ctx.bot, lid, with_photo=False)


async def cmd_excel(update: Update, ctx):
    path = write_excel(DB_.c, EXCEL)
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename=Path(path).name, caption=f"{len(DB_.sent_rows())} applications")


async def cmd_replies(update: Update, ctx):
    await update.message.reply_text("📬 Checking inbox…")
    try:
        await check_replies(ctx.bot)
        await update.message.reply_text("Done.")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


async def cmd_help(update: Update, ctx):
    await update.message.reply_text(
        "/status – what I'm doing\n/check – poll now\n/replies – check inbox now\n"
        "/excel – get the applications spreadsheet\n/pause, /resume\n"
        "/test <ad url> – score + draft any ad (ignores filters)\n/login_check – am I logged in?\n"
        "/summary – the daily review right now"
    )


async def cmd_summary(update: Update, ctx):
    await update.message.reply_text(summary_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_status(update: Update, ctx):
    st = ctx.bot_data
    sites = "\n".join(
        f"{s.label}: {s.guard.status()} · " + ("you send by hand" if s.send_by_hand else f"dry run: {site_dry_run(s)}")
        for s in active_sites()
    )
    await update.message.reply_text(
        f"{'⏸ paused' if st['paused'] else '▶️ running'}\n"
        f"Last poll: {st.get('last_poll', '–')} ({st.get('counts', {})} ads on page)\n"
        f"Next check in ~{int(next_delay() // 60)} min\n{sites}\n"
        f"Commute: {'on (' + COMMUTE_.provider + ')' if COMMUTE_.enabled else 'off'}\n"
        f"DB: {DB_.stats()}"
    )


async def cmd_pause(update: Update, ctx):
    ctx.bot_data["paused"] = True
    await update.message.reply_text("⏸ Paused")


async def cmd_resume(update: Update, ctx):
    ctx.bot_data["paused"] = False
    await update.message.reply_text("▶️ Resumed")


async def cmd_check(update: Update, ctx):
    cooling = [s.guard.cooling_left() for s in active_sites()]
    if all(cooling):
        await update.message.reply_text(f"🧊 Cooling down after a block ({min(cooling) // 60} min left), not checking.")
        return
    await update.message.reply_text("🔎 Checking now…")
    await poll_once(ctx)


async def login_lines() -> tuple[list[str], bool]:
    """One line per searched site (1 page load each), and whether all of them are logged in."""
    lines, all_ok = [], True
    for s in active_sites():
        if s.send_by_hand:
            lines.append(f"ℹ️ {s.label}: no login needed (you send from its app)")
            continue
        try:
            ok = await s.is_logged_in()
            lines.append(
                f"✅ {s.label}: logged in" if ok else f"⚠️ {s.label}: NOT logged in – python bot.py --login {s.name}"
            )
        except Exception as e:
            ok = True  # can't tell; don't raise the alarm for that
            lines.append(f"❔ {s.label}: login check failed ({e.__class__.__name__})")
        all_ok = all_ok and ok
    return lines, all_ok


async def cmd_login_check(update: Update, ctx):
    lines, _ = await login_lines()
    await update.message.reply_text("\n".join(lines))


async def cmd_test(update: Update, ctx):
    if not ctx.args:
        await update.message.reply_text(f"Usage: /test <ad url> ({', '.join(s.label for s in SITES.values())})")
        return
    try:
        site = site_for_url(ctx.args[0])
    except ValueError as e:
        await update.message.reply_text(str(e))
        return
    l = site.listing_from_url(ctx.args[0])
    DB_.add(l, status="test")
    await update.message.reply_text("⏳ Reading ad, checking commute, drafting…")
    try:
        await process(ctx.bot, l, force=True)
    except Blocked as e:
        await on_block(ctx.bot, site, e)
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")


# ---------------- main ----------------
async def post_init(app: Application):
    await COMMUTE_.setup()  # Chromium starts on first use (the login check below, for the browser sites)
    app.bot_data.update(
        paused=False, fails={}, n=0, counts={}, zeros={}, seeded={}, budget_warned={}, poll_lock=asyncio.Lock()
    )
    first = random.uniform(30, 90)
    app.job_queue.run_once(tick, first, name="poll")
    if CFG.get("replies", {}).get("enabled", True):
        app.job_queue.run_once(inbox_tick, first + random.uniform(60, 180), name="inbox")
    if hhmm := CFG["telegram"].get("daily_summary"):
        h, m = map(int, str(hhmm).split(":"))
        app.job_queue.run_daily(daily_summary, dtime(h, m, tzinfo=ZoneInfo("Europe/Berlin")), name="summary")
    # 1 page load per site; sending (and WG-Gesucht Plus's head start) need the login
    lines, logged_in = await login_lines()
    await notify(
        app.bot,
        f"🤖 Flat agent started – first check in {int(first)}s. "
        f"Commute check: {'on' if COMMUTE_.enabled else 'OFF'}.\n" + "\n".join(lines) + "\n/help",
        important=not logged_in,  # quiet mode: only bother you if you need to log in again
    )


async def post_shutdown(app: Application):
    await BROWSER.stop()


def setup_sites(headless, block_resources):
    """One shared browser; every supported site with its own page budget (sites.<name> can override poll:)."""
    global BROWSER
    BROWSER = Browser(headless=headless, block_resources=block_resources, domains=[t.domain for t in SITE_TYPES])
    for t in SITE_TYPES:
        own = (CFG.get("sites") or {}).get(t.name) or {}
        key = "guard" if t.name == "wg-gesucht" else f"guard:{t.name}"  # WG-Gesucht keeps its old counts
        SITES[t.name] = t(BROWSER, RateGuard(DB_.c, {**CFG["poll"], **own}, key=key), own)


def main():
    global CFG, DB_, LLM_, COMMUTE_, APP, EXCEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--login",
        nargs="?",
        const="all",
        metavar="SITE",
        help="open a visible browser to log in to every searched site, or only to SITE (e.g. kleinanzeigen)",
    )
    a = ap.parse_args()
    CFG = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    CFG["telegram"]["chat_id"] = int(CFG["telegram"]["chat_id"])

    DB_ = DB()
    if a.login:
        setup_sites(headless=False, block_resources=False)
        browser_sites = {n: s for n, s in SITES.items() if not s.send_by_hand}
        if a.login in SITES and SITES[a.login].send_by_hand:
            raise SystemExit(f"{SITES[a.login].label} needs no login here: you send from its app.")
        sites = (
            [s for s in active_sites() if not s.send_by_hand]
            if a.login == "all"
            else [browser_sites[a.login]]
            if a.login in browser_sites
            else []
        )
        if not sites:
            if a.login == "all":
                raise SystemExit("None of your searches is on a site that needs a login.")
            raise SystemExit(f"Unknown site {a.login!r}. Choose from: {', '.join(browser_sites)}")
        asyncio.run(interactive_login(BROWSER, sites))  # its page loads count against the budgets too
        return

    setup_sites(CFG["poll"].get("headless", True), CFG["poll"].get("block_resources", True))
    for sc in CFG["search"]["searches"]:
        site_for_url(sc["url"]).check_search_url(sc["url"])  # fail now on a search URL a site can't use
    city = CFG.get("city", "München")
    LLM_ = LLM(CFG["llm"], CFG["me"], city)
    COMMUTE_ = Commute(CFG.get("commute"), DB_.kv_get, DB_.kv_set, city)

    EXCEL = CFG.get("replies", {}).get("excel_path", "applications.xlsx")
    app = APP = (
        Application.builder()
        .token(CFG["telegram"]["bot_token"])
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    owner = filters.Chat(chat_id=CFG["telegram"]["chat_id"])
    for name, fn in [
        ("start", cmd_help),
        ("help", cmd_help),
        ("status", cmd_status),
        ("pause", cmd_pause),
        ("resume", cmd_resume),
        ("check", cmd_check),
        ("test", cmd_test),
        ("login_check", cmd_login_check),
        ("excel", cmd_excel),
        ("replies", cmd_replies),
        ("summary", cmd_summary),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


DB_: DB
BROWSER: Browser
LLM_: LLM
COMMUTE_: Commute
APP: Application
EXCEL = "applications.xlsx"

if __name__ == "__main__":
    main()
