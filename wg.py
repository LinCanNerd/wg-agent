"""WG-Gesucht: search pages, ad pages, contact form and inbox.

Selectors are based on the 2026 markup (cross-checked with Fredy's fixtures, Flathunter,
MietRadar, nickirk/immo, jonasdieker & AykoSc bots).
"""

import asyncio
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import TimeoutError as PWTimeout

from sites import Listing, LoggedOut, Site, is_commercial

log = logging.getLogger("wg")
BASE = "https://www.wg-gesucht.de"
ID_RE = re.compile(r"\.(\d{5,})\.html")
EURO_RE = re.compile(r"(\d[\d.]*)\s*€")
SIZE_RE = re.compile(r"(\d{1,3})\s*m²")
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
COORD_RE = re.compile(r'markers:\s*\[\s*\{\s*"lat"\s*:\s*([\d.]+)\s*,\s*"lng"\s*:\s*([\d.]+)')
# Logged-out version of member pages (e.g. nachrichten.html) in 2026: same URL, no login form, just this text.
LOGGED_OUT_WORDS = re.compile(r"bitte loggen sie sich hier ein|schön, dass sie vorbeischauen", re.I)
INBOX_URL = BASE + "/nachrichten.html?filter_type=0"
SECTION_NAMES = ["Zimmer", "Lage", "WG-Leben", "Sonstiges"]


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
  // the advertiser's id sits on their phone-number popup, next to the ad id; logged in, the page also
  // carries YOUR id in another [data-user_id], so never take just any of them
  const uid = document.querySelector('#phone_numbers_modal[data-user_id], [data-user_id][data-asset_id]');
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


class WGGesucht(Site):
    name = "wg-gesucht"
    label = "WG-Gesucht"
    domain = "wg-gesucht.de"
    login_url = BASE + "/"

    def listing_from_url(self, url: str) -> Listing:
        return listing_from_url(url)

    def prepare_search_url(self, url, max_rent=None, min_size=None) -> str:
        return prepare_search_url(url, max_rent, min_size)

    async def search(self, url, kind="room") -> list[Listing]:
        async with self.lock, self._page() as page:
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
                if not is_commercial(l.poster, l.title):
                    out.append(l)
            return out

    async def fetch_details(self, l: Listing) -> Listing:
        async with self.lock, self._page() as page:
            resp = await self._goto(page, l.url)
            if resp and resp.status in (404, 410):
                raise LookupError("ad no longer online")
            d = await page.evaluate(DETAIL_JS)
            if not d.get("sections"):
                await self._check_block(page)
                raise LookupError("no description found (ad deactivated or markup changed)")
            return apply_detail(l, d, await page.content())

    async def logged_in_on(self, page) -> bool:
        # the homepage looks the same logged in or out, so ask a members-only page
        await self._goto(page, INBOX_URL, priority=True)
        return not await shows_logged_out(page)

    async def send_message(self, l: Listing, text: str, dry_run=True):
        send_url = l.send_url or BASE + "/nachricht-senden/" + urlsplit(l.url).path.lstrip("/")
        async with self.lock, self._page() as page:
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
            await self._type_into(box, text)  # real key events so the Angular form registers it

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

    async def inbox(self) -> list[dict]:
        """Conversation list (1 page load)."""
        async with self.lock, self._page() as page:
            await self._goto(page, INBOX_URL)
            if await shows_logged_out(page):
                raise LoggedOut()
            try:
                await page.wait_for_selector("div.conversation_list_item", timeout=8000)
            except PWTimeout:
                await self._check_block(page)
                return []
            convs = await page.evaluate(INBOX_JS)
        for c in convs:
            c["key"] = "conv:" + c["href"].split("nachrichten-id=")[-1].split("&")[0]
        return convs

    async def conversation_ad_id(self, href) -> str | None:
        """Open one conversation to find which ad it belongs to (only when title/name matching fails)."""
        async with self.lock, self._page() as page:
            await self._goto(page, href)
            src = await page.content()
            m = re.search(r"\.(\d{6,10})\.html", src) or re.search(r"Nummer\s*(?:<[^>]+>)?\s*(\d{6,10})", src)
            return m.group(1) if m else None
