"""WG-Gesucht access via a real (persistent, logged-in) Chromium profile.

Selectors are based on the 2026 markup (cross-checked with Fredy's fixtures, Flathunter,
MietRadar, nickirk/immo, jonasdieker & AykoSc bots).
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

log = logging.getLogger("wg")
BASE = "https://www.wg-gesucht.de"
ID_RE = re.compile(r"\.(\d{5,})\.html")
EURO_RE = re.compile(r"(\d[\d.]*)\s*€")
SIZE_RE = re.compile(r"(\d{1,3})\s*m²")
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
COORD_RE = re.compile(r'markers:\s*\[\s*\{\s*"lat"\s*:\s*([\d.]+)\s*,\s*"lng"\s*:\s*([\d.]+)')
BLOCK_WORDS = re.compile(
    r"captcha|sicherheitsabfrage|zugriff verweigert|access denied|too many requests|"
    r"ungewöhnlich viele|verify you are human|warum erscheint diese seite|"
    r"nutzungsaktivitäten, die den zweck haben",
    re.I,
)
# Logged-out version of member pages (e.g. nachrichten.html) in 2026: same URL, no login form, just this text.
LOGGED_OUT_WORDS = re.compile(r"bitte loggen sie sich hier ein|schön, dass sie vorbeischauen", re.I)
# Chromium forgets session-only cookies when it closes, and WG-Gesucht's login needs them. --login and
# WG.stop() save all cookies here; WG.start() puts back the session-only ones. Private: gitignored.
SESSION_FILE = "browser-session.json"
COMMERCIAL_POSTERS = (
    "housinganywhere",
    "spotahome",
    "uniplaces",
    "medici",
    "spacest",
    "airbnb",
    "wunderflats",
    "homelike",
    "roomlessrent",
    "habyt",
    "mietcampus",
)
SECTION_NAMES = ["Zimmer", "Lage", "WG-Leben", "Sonstiges"]


class Blocked(Exception):
    """Captcha / rate limit detected."""


class LoggedOut(Exception):
    pass


@dataclass
class Listing:
    id: str
    url: str
    title: str = ""
    card_text: str = ""
    kind: str = "room"  # room | studio
    rent: int | None = None
    size: int | None = None
    available_from: str | None = None  # dd.mm.yyyy
    available_to: str | None = None
    district: str = ""
    street: str = ""
    flat_type: str = ""  # "3er WG", "1-Zimmer-Wohnung"
    flatmates: str = ""  # "3er WG (1w,1m,0d,0n)"
    wanted: str = ""  # who they look for (from icons / "Gesucht wird")
    poster: str = ""  # name shown on the card
    poster_id: str = ""  # WG-Gesucht user id of the advertiser (same-person check)
    applicants: int | None = None  # "x Bewerbungen/Anfragen" (visible with WG-Gesucht Plus)
    online: str = ""  # "Online: 26 Minuten"
    online_min: int | None = None
    image: str = ""
    images: list = field(default_factory=list)  # the ad's own gallery, large versions
    description: str = ""  # all tabs, labelled
    details: str = ""  # key facts as text
    address: str = ""
    lat: float | None = None
    lng: float | None = None
    member_since: str = ""
    send_url: str = ""
    exchange: bool = False  # Tauschangebot
    commute: dict = field(default_factory=dict)
    commute_text: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def listing_from_url(url: str) -> Listing:
    m = ID_RE.search(url)
    kind = "studio" if "1-zimmer-wohnung" in url.lower() else "room"
    return Listing(id=m.group(1) if m else str(abs(hash(url))), url=url, kind=kind)


def _int(s):
    if not s:
        return None
    m = EURO_RE.search(s) or SIZE_RE.search(s) or re.search(r"(\d[\d.]*)", s)
    return int(m.group(1).replace(".", "")) if m else None


def online_minutes(s: str) -> int | None:
    """'Online: 26 Minuten' -> 26, '1 Stunde' -> 60, '2 Tage' -> 2880, '01.10.2026' -> big."""
    s = (s or "").lower()
    if m := re.search(r"(\d+)\s*(sekunde|minute|stunde|tag)", s):
        n, unit = int(m.group(1)), m.group(2)
        return n * {"sekunde": 0, "minute": 1, "stunde": 60, "tag": 1440}[unit]
    if DATE_RE.search(s):
        return 99999
    return None


def prepare_search_url(url: str, max_rent=None, min_size=None) -> str:
    """Make WG-Gesucht do the filtering and sort newest first (fewer pages, no Tauschangebote)."""
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    m = re.search(r"\.(\d+)\.([\d+]+)\.\d+\.\d+\.html", parts.path)
    if m:
        q.setdefault("offer_filter", "1")
        q.setdefault("city_id", m.group(1))
        q.setdefault("noDeact", "1")
        for i, cat in enumerate(m.group(2).split("+")):
            q.setdefault(f"categories[{i}]", cat)
    q.setdefault("sort_column", "0")
    q.setdefault("sort_order", "0")
    q.setdefault("exc", "2")  # no Tauschangebote
    if max_rent:
        q.setdefault("rMax", str(max_rent))
    if min_size:
        q.setdefault("sMin", str(min_size))
    return urlunsplit(parts._replace(query=urlencode(q)))


def card_to_listing(r: dict) -> Listing:
    href = r.get("href") or ""
    url = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
    l = Listing(id=str(r["id"]), url=url, title=r.get("title", "").strip(), card_text=r.get("text", ""))
    l.rent = _int(r.get("rent"))
    l.size = _int(r.get("size"))
    dates = DATE_RE.findall(r.get("dates") or "")
    if dates:
        l.available_from = dates[0]
    if len(dates) > 1:
        l.available_to = dates[1]
    parts = [p.strip() for p in (r.get("loc") or "").split("|")]
    if parts:
        l.flat_type = parts[0]
    if len(parts) > 1:
        l.district = re.sub(r"^München\s*", "", parts[1]).strip()
    if len(parts) > 2:
        l.street = parts[2]
    l.flatmates = r.get("flatmates") or ""
    l.wanted = r.get("wanted") or ""
    l.poster = r.get("poster") or ""
    l.online = r.get("online") or ""
    l.online_min = online_minutes(l.online)
    l.image = (r.get("image") or "").replace(".small.", ".large.")
    # fallbacks if a selector broke: regex over the card text
    t = l.card_text
    if l.rent is None and (m := EURO_RE.search(t)):
        l.rent = int(m.group(1).replace(".", ""))
    if l.size is None and (m := SIZE_RE.search(t)):
        l.size = int(m.group(1))
    return l


# Runs in the page. Returns one dict per *real* offer; skips paid / company / partner cards.
CARDS_JS = r"""
() => {
  const txt = (el) => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  const out = [], seen = new Set();
  const cards = document.querySelectorAll('.wgg_card[data-id], .offer_list_item[data-id]');
  for (const c of cards) {
    const id = c.getAttribute('data-id');
    if (!id || seen.has(id)) continue;
    if (c.closest('.premium_user_extra_list')) continue;                 // company block
    if (c.classList.contains('display-none')) continue;
    if (c.matches('.airbnb_ad, .housinganywhere_ad')) continue;          // partner ads
    if (c.querySelector('.label_verified, .premium_sticky_asset, [data-campaign_type="premium_ads"]')) continue;
    const a = c.querySelector('.truncate_title a[href]') ||
              c.querySelector('a.detailansicht[href]') ||
              c.querySelector('a[href*=".' + id + '.html"]');
    let href = a ? a.getAttribute('href') : '';
    if (!href || href.includes('asset_id') || !href.includes(id)) href = '/' + id + '.html';
    seen.add(id);
    const loc = c.querySelector('.col-xs-11 > span');
    const fm = c.querySelector('.col-xs-11 span.noprint[title]');
    const online = [...c.querySelectorAll('span')].find(s => /^\s*Online:/.test(s.textContent));
    const img = c.querySelector('img.img-responsive');
    out.push({
      id, href,
      title: txt(c.querySelector('.truncate_title')),
      rent: txt(c.querySelector('.middle .col-xs-3')),
      dates: txt(c.querySelector('.middle .text-center')),
      size: txt(c.querySelector('.middle .text-right')),
      loc: txt(loc),
      flatmates: fm ? fm.getAttribute('title') : '',
      wanted: fm ? [...fm.querySelectorAll('img[alt]')].map(i => i.alt).filter(a => /gesucht/i.test(a)).join(', ') : '',
      poster: txt(c.querySelector('span.ml5')),
      online: txt(online),
      image: img ? img.getAttribute('src') : '',
      text: txt(c).slice(0, 1500),
    });
  }
  return out;
}
"""

DETAIL_JS = r"""
() => {
  const txt = (el) => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  const sections = [];
  for (let i = 0; i < 6; i++) {               // hidden tabs too (textContent, not innerText)
    const el = document.getElementById('freitext_' + i);
    if (!el) continue;
    const clone = el.cloneNode(true);
    clone.querySelectorAll('script, style').forEach(n => n.remove());
    const t = txt(clone).replace(/googletag\.cmd\.push\([^)]*\)\s*;?/g, '');
    if (t) sections.push([i, t]);
  }
  const pairs = [];
  document.querySelectorAll('.row').forEach(r => {
    const k = r.querySelector(':scope > div > .section_panel_detail');
    const v = r.querySelector(':scope > div > .section_panel_value');
    if (k && v) pairs.push([txt(k).replace(/:$/, ''), txt(v)]);
  });
  document.querySelectorAll('.key_fact_detail').forEach(k => {
    const v = k.parentElement.querySelector('.key_fact_value');
    if (v) pairs.push([txt(k), txt(v)]);
  });
  const facts = [...document.querySelectorAll('li .section_panel_detail')].map(txt).filter(Boolean);
  const send = document.querySelector('a[href*="nachricht-senden"]');
  const member = [...document.querySelectorAll('p')].find(p => /Mitglied seit/.test(p.textContent));
  const titleEl = document.querySelector('h1.detailed-view-title') || document.querySelector('h1');
  const uid = document.querySelector('[data-user_id]');
  // Only #gallery_slides: the page also holds map-pin, similar-ad and profile images of other people.
  const images = [...document.querySelectorAll('#gallery_slides img')]
    .map(i => i.getAttribute('data-src') || i.getAttribute('src') || '')   // later slides are lazy (data-src)
    .filter(s => s.includes('img.wg-gesucht.de'));
  return {
    title: txt(titleEl),
    flatmates: (titleEl && titleEl.querySelector('[title]')) ? titleEl.querySelector('[title]').getAttribute('title') : '',
    sections, pairs, facts,
    address: txt(document.querySelector('a[href="#map_container"] .section_panel_detail')),
    send: send ? send.href : '',
    member: txt(member),
    poster_id: uid ? uid.getAttribute('data-user_id') : '',
    images,
    head: document.body.innerText.slice(0, 6000),
  };
}
"""


def apply_detail(l: Listing, d: dict, html: str) -> Listing:
    if d.get("title"):
        l.title = d["title"]
    if d.get("flatmates") and not l.flatmates:
        l.flatmates = d["flatmates"]
    names = SECTION_NAMES if l.kind == "room" else ["Wohnung", "Lage", "Sonstiges", "Sonstiges"]
    l.description = "\n\n".join(f"[{names[i] if i < len(names) else 'Text'}]\n{t}" for i, t in d.get("sections", []))[
        :8000
    ]
    pairs = {k: v for k, v in d.get("pairs", [])}
    facts = d.get("facts", [])
    l.details = " | ".join([f"{k}: {v}" for k, v in pairs.items()] + facts)[:2500]
    for key in ("Gesamtmiete", "Miete"):
        if key in pairs and _int(pairs[key]):
            l.rent = _int(pairs[key])
            break
    for key in ("Zimmergröße", "Größe", "Wohnungsgröße"):
        if key in pairs and _int(pairs[key]):
            l.size = _int(pairs[key])
            break
    if "frei ab" in pairs and DATE_RE.search(pairs["frei ab"]):
        l.available_from = DATE_RE.search(pairs["frei ab"]).group(1)
    if "frei bis" in pairs and DATE_RE.search(pairs["frei bis"]):
        l.available_to = DATE_RE.search(pairs["frei bis"]).group(1)
    wanted = [f for f in facts if re.search(r"^(geschlecht|frau|frauen|mann|männer|mitbewohner)\b|jahren$", f, re.I)]
    if wanted:
        l.wanted = "; ".join(dict.fromkeys([l.wanted] + wanted if l.wanted else wanted))
    l.address = d.get("address", "") or l.address
    l.member_since = (d.get("member") or "").replace("Mitglied seit", "").strip()
    l.send_url = d.get("send") or l.send_url
    l.poster_id = d.get("poster_id") or l.poster_id
    imgs = [re.sub(r"\.(sized|small)\.", ".large.", u.replace("/./", "/")) for u in d.get("images", [])]
    if imgs := list(dict.fromkeys(imgs)):
        l.images = imgs
        l.image = imgs[0]
    if m := re.search(
        r"(\d+)\s*(Bewerbungen|Bewerber|Anfragen|Interessent|applicants|applications|requests)", d.get("head", ""), re.I
    ):
        l.applicants = int(m.group(1))
    l.exchange = bool(
        re.search(r"tauschangebot|wohnungstausch|tauschwohnung|zimmertausch", f"{l.title} {l.description}", re.I)
    )
    if m := COORD_RE.search(html):
        l.lat, l.lng = float(m.group(1)), float(m.group(2))
    return l


async def shows_logged_out(page) -> bool:
    if "login" in page.url.lower() or await page.locator("#login_email_username:visible").count():
        return True
    return bool(LOGGED_OUT_WORDS.search((await page.inner_text("body"))[:5000]))


class WG:
    def __init__(
        self,
        profile_dir="browser-profile",
        headless=True,
        shots_dir="screenshots",
        guard=None,
        block_resources=True,
        session_file=SESSION_FILE,
    ):
        self.session_file = Path(session_file)
        self.guard = guard
        self.block_resources = block_resources
        self.profile_dir = str(Path(profile_dir).resolve())
        self.headless = headless
        self.shots = Path(shots_dir)
        self.shots.mkdir(exist_ok=True)
        self.lock = asyncio.Lock()
        self._pw = None
        self.ctx = None

    async def start(self):
        self._pw = await async_playwright().start()
        self.ctx = await self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.headless,
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1366, "height": 900},
        )
        if self.block_resources:
            # Skip images/fonts/media: far fewer requests per page load and less bandwidth.
            await self.ctx.route("**/*", self._route)
        await self._restore_session_cookies()

    async def _restore_session_cookies(self):
        if not self.session_file.exists():
            return
        try:
            saved = json.loads(self.session_file.read_text()).get("cookies", [])
        except Exception as e:
            log.warning("could not read %s: %s", self.session_file, e)
            return
        have = {(c["name"], c["domain"]) for c in await self.ctx.cookies()}
        missing = [
            {k: v for k, v in c.items() if k != "expires"}
            for c in saved
            if c.get("expires", -1) == -1 and "wg-gesucht.de" in c["domain"] and (c["name"], c["domain"]) not in have
        ]
        if missing:
            await self.ctx.add_cookies(missing)
            log.info("restored %d session cookie(s) from %s", len(missing), self.session_file)

    async def save_session(self):
        await self.ctx.storage_state(path=str(self.session_file))
        self.session_file.chmod(0o600)

    @staticmethod
    async def _route(route):
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    async def stop(self):
        if self.ctx:
            try:  # keep the saved session current, so the next start is still logged in
                await self.save_session()
            except Exception as e:
                log.warning("could not save the session: %s", e)
            await self.ctx.close()
        if self._pw:
            await self._pw.stop()

    # ---------- helpers ----------
    async def _dismiss_overlays(self, page):
        for name in ("Alle akzeptieren", "Accept all", "Akzeptieren", "Einverstanden"):
            try:
                b = page.get_by_role("button", name=name)
                if await b.count() and await b.first.is_visible():
                    await b.first.click(timeout=2000)
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                pass
        try:  # leftover consent / lightbox layers that swallow clicks (AykoSc, jonasdieker)
            await page.evaluate("""() => document.querySelectorAll(
                '#cmpbox, #cmpbox2, .cmpboxBG, .modal-backdrop, div.lightbox').forEach(e => e.remove())""")
        except Exception:
            pass

    async def _check_block(self, page):
        if "captcha" in page.url.lower():
            raise Blocked("redirected to captcha")
        for sel in ("iframe[src*='recaptcha/api2/bframe']", "iframe[src*='hcaptcha']", ".g-recaptcha"):
            loc = page.locator(sel)
            for i in range(min(await loc.count(), 3)):
                box = await loc.nth(i).bounding_box()
                if box and box["height"] > 10 and await loc.nth(i).is_visible():  # invisible ones always exist
                    raise Blocked("visible captcha")
        head = (await page.inner_text("body"))[:5000]
        if BLOCK_WORDS.search(head):
            raise Blocked("block / captcha page")

    async def _goto(self, page, url, priority=False):
        if self.guard:
            self.guard.acquire(priority=priority)  # raises BudgetExceeded / CoolingDown
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if resp and resp.status in (403, 429, 503):
            raise Blocked(f"HTTP {resp.status}")
        await self._dismiss_overlays(page)
        return resp

    async def _shot(self, page, tag):
        p = self.shots / f"{int(time.time())}_{tag}.png"
        try:
            await page.screenshot(path=str(p), full_page=False)
            return str(p)
        except Exception:
            return None

    # ---------- public ----------
    async def search(self, url, kind="room") -> list[Listing]:
        async with self.lock:
            page = await self.ctx.new_page()
            try:
                await self._goto(page, url)
                try:
                    await page.wait_for_selector(".wgg_card, .offer_list_item", timeout=10000)
                except PWTimeout:
                    pass
                raw = await page.evaluate(CARDS_JS)
                if not raw:
                    await self._check_block(page)
                    log.warning("0 listings parsed – search URL wrong or page markup changed")
                out = []
                for r in raw:
                    l = card_to_listing(r)
                    l.kind = kind
                    if any(c in (l.poster + " " + l.title).lower() for c in COMMERCIAL_POSTERS):
                        continue
                    out.append(l)
                return out
            finally:
                await page.close()

    async def fetch_details(self, l: Listing) -> Listing:
        async with self.lock:
            page = await self.ctx.new_page()
            try:
                resp = await self._goto(page, l.url)
                if resp and resp.status in (404, 410):
                    raise LookupError("ad no longer online")
                d = await page.evaluate(DETAIL_JS)
                if not d.get("sections"):
                    await self._check_block(page)
                    raise LookupError("no description found (ad deactivated or markup changed)")
                return apply_detail(l, d, await page.content())
            finally:
                await page.close()

    async def is_logged_in(self) -> bool:
        async with self.lock:
            page = await self.ctx.new_page()
            try:
                # the homepage looks the same logged in or out, so ask a members-only page (1 page load)
                await self._goto(page, BASE + "/nachrichten.html?filter_type=0", priority=True)
                return not await shows_logged_out(page)
            finally:
                await page.close()

    async def _first_visible(self, page, selectors, timeout=8000):
        """Try selectors one by one (a comma list + .first would pick by page order, not priority)."""
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for sel in selectors:
                loc = page.locator(sel)
                n = await loc.count()
                for i in range(min(n, 5)):
                    if await loc.nth(i).is_visible():
                        return loc.nth(i)
            await page.wait_for_timeout(400)
        return None

    async def send_message(self, l: Listing, text: str, dry_run=True):
        """Returns (status, screenshot_path, info). status: sent | dry_run | already | failed."""
        send_url = l.send_url or BASE + "/nachricht-senden/" + urlsplit(l.url).path.lstrip("/")
        async with self.lock:
            page = await self.ctx.new_page()
            try:
                await self._goto(page, send_url, priority=True)  # straight to the form: 1 page load
                await self._check_block(page)

                if await shows_logged_out(page):
                    raise LoggedOut()

                # "I've read the safety tips" (DE + EN labels)
                tips = await self._first_visible(
                    page,
                    [
                        "#sicherheit_bestaetigung",
                        "button:has-text('Sicherheitstipps')",
                        "button:has-text('Security Advice')",
                    ],
                    timeout=2500,
                )
                if tips:
                    await tips.click(timeout=3000)
                    await page.wait_for_timeout(500)

                if await page.locator("#message_timestamp").count():
                    return "already", await self._shot(page, "already"), "Already contacted this ad – not sending again"

                box = await self._first_visible(
                    page,
                    [
                        "#message_input",
                        "textarea[name='content']",
                        "textarea[name='message']",
                        ".conversation-input textarea",
                    ],
                    timeout=12000,
                )
                if not box:
                    return "failed", await self._shot(page, "nobox"), "Message box not found"
                await box.fill(text)
                await box.press("End")
                await box.type(" ", delay=80)  # real key events so the Angular form registers it
                await box.press("Backspace")

                if dry_run:
                    return "dry_run", await self._shot(page, "dryrun"), "DRY RUN – form filled, not sent"

                btn = await self._first_visible(
                    page,
                    [
                        "button[data-ng-click='submit()']",
                        "button.create_new_conversation",
                        "button:has-text('Nachricht senden')",
                        "button:has-text('Senden')",
                        "button:has-text('Send message')",
                    ],
                    timeout=5000,
                )
                if not btn:
                    return "failed", await self._shot(page, "nobutton"), "Send button not found"

                api_ok = asyncio.get_running_loop().create_future()

                async def on_resp(resp):
                    if "action=conversations" in resp.url and not api_ok.done():
                        try:
                            body = await resp.text()
                        except Exception:
                            body = ""
                        api_ok.set_result(resp.ok and "conversation_id" in body)

                page.on("response", on_resp)
                await btn.click(timeout=10000)
                ok = False
                try:
                    ok = await asyncio.wait_for(asyncio.shield(api_ok), timeout=12)
                except asyncio.TimeoutError:
                    pass
                if not ok:  # UI fallback: the sent message bubble / timestamp shows up
                    try:
                        await page.wait_for_selector(
                            "#message_timestamp, .message_content, .conversation_message", timeout=6000
                        )
                        ok = True
                    except PWTimeout:
                        pass
                shot = await self._shot(page, "sent" if ok else "unsure")
                if ok:
                    return "sent", shot, "Sent ✅"
                return "failed", shot, "Clicked send but couldn't confirm – check the screenshot"
            finally:
                await page.close()


INBOX_JS = r"""
() => [...document.querySelectorAll('div.conversation_list_item')].map(it => {
  const a = it.querySelector("a.link-conversation-list[href*='nachrichten-id']") || it.querySelector("a[href*='nachrichten-id']");
  const t = (el) => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  return {
    href: a ? a.href : '',
    name: t(it.querySelector('span.list_item_public_name')),
    title: t(it.querySelector('h3')),
    when: t(it.querySelector('div.latest_message_timestamp_list')),
    unread: it.matches('.unread, .new_message, [class*="unread"]') || !!it.querySelector('.unread, [class*="unread"], .badge'),
    text: t(it).slice(0, 600),
  };
}).filter(c => c.href)
"""


async def _inbox_page(wg, page_url):
    page = await wg.ctx.new_page()
    try:
        await wg._goto(page, page_url)
        if await shows_logged_out(page):
            raise LoggedOut()
        try:
            await page.wait_for_selector("div.conversation_list_item", timeout=8000)
        except PWTimeout:
            await wg._check_block(page)
            return []
        return await page.evaluate(INBOX_JS)
    finally:
        await page.close()


async def inbox(wg) -> list[dict]:
    """Conversation list (1 page load). Each: href, name, title, when, unread, text."""
    async with wg.lock:
        return await _inbox_page(wg, BASE + "/nachrichten.html?filter_type=0")


async def conversation_ad_id(wg, href) -> str | None:
    """Open one conversation to find which ad it belongs to (only when title/name matching fails)."""
    async with wg.lock:
        page = await wg.ctx.new_page()
        try:
            await wg._goto(page, href)
            src = await page.content()
            m = re.search(r"\.(\d{6,10})\.html", src) or re.search(r"Nummer\s*(?:<[^>]+>)?\s*(\d{6,10})", src)
            return m.group(1) if m else None
        finally:
            await page.close()


async def interactive_login(profile_dir="browser-profile", guard=None):
    wg = WG(profile_dir, headless=False, block_resources=False, guard=guard)
    await wg.start()
    try:
        page = await wg.ctx.new_page()
        await wg._goto(page, BASE + "/", priority=True)
        print("\nA browser window opened. Log in to WG-Gesucht there (accept cookies too).")
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter here when you're logged in... ")
        # check in this same browser, before closing it drops the session-only cookies (1 page load)
        await wg._goto(page, BASE + "/nachrichten.html?filter_type=0", priority=True)
        ok = not await shows_logged_out(page)
    finally:
        await wg.stop()  # also saves all cookies, session-only ones included, to SESSION_FILE
    if ok:
        print(f"✅ Logged in: your inbox opened. Session saved in {profile_dir}/ and {wg.session_file}.")
    else:
        print("❌ WG-Gesucht still shows the login page. Run --login again and log in before pressing Enter.")
