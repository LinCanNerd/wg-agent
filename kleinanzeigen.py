"""Kleinanzeigen: WG rooms ("Auf Zeit & WG") and flats ("Mietwohnungen").

Search and ad pages were checked against the live site on 2026-10-02. Sending and the inbox need a login and
are not verified yet, so sending is a dry run until `sites.kleinanzeigen.dry_run: false` (send_verified).
"""

import asyncio
import logging
import re
from datetime import datetime
from html import unescape
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import TimeoutError as PWTimeout

from sites import Listing, LoggedOut, Site, is_commercial

log = logging.getLogger("kleinanzeigen")
BASE = "https://www.kleinanzeigen.de"
PREFIX = "ka:"  # Listing ids: "ka:1234567890", so they can't clash with WG-Gesucht ids
ID_RE = re.compile(r"/(\d{8,12})-(\d+)-\d+")  # /s-anzeige/<slug>/1234567890-199-6411 (id, category)
EURO_RE = re.compile(r"(\d[\d.]*)(?:,\d+)?\s*€")
SIZE_RE = re.compile(r"(\d+)(?:,\d+)?\s*m²")
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
EXCHANGE_RE = re.compile(r"tauschangebot|wohnungstausch|tauschwohnung|zimmertausch", re.I)
WG_TITLE_RE = re.compile(r"\bwg\b|wg-?zimmer|mitbewohner|zimmer in (einer|meiner|unserer)", re.I)
MONTHS = "januar februar märz april mai juni juli august september oktober november dezember".split()
ROOM_CATEGORY, FLAT_CATEGORY = "199", "203"  # Auf Zeit & WG, Mietwohnungen
INBOX_URL = BASE + "/m-nachrichten.html"


def _euro(s):
    m = EURO_RE.search(s or "")
    return int(m.group(1).replace(".", "")) if m else None


def _size(s):
    m = SIZE_RE.search(s or "")
    return int(m.group(1)) if m else None


def _month_start(s):
    """'Oktober 2026' -> '01.10.2026'; a full date is kept."""
    s = (s or "").strip().lower()
    if m := DATE_RE.search(s):
        return m.group(1)
    if m := re.search(r"([a-zä]+)\s+(\d{4})", s):
        if m.group(1) in MONTHS:
            return f"01.{MONTHS.index(m.group(1)) + 1:02d}.{m.group(2)}"
    return None


def _large(url):
    return re.sub(r"rule=\$_\d+\.AUTO", "rule=$_59.AUTO", url)


def guess_kind(title, kind):
    """People post WG rooms in the flats category too: a title about a WG makes it a room."""
    return "room" if kind == "studio" and WG_TITLE_RE.search(title or "") else kind


def listing_from_url(url: str) -> Listing:
    m = ID_RE.search(url)
    kind = "room" if m and m.group(2) == ROOM_CATEGORY else "studio"
    return Listing(id=PREFIX + (m.group(1) if m else str(abs(hash(url)))), url=url, site="kleinanzeigen", kind=kind)


def prepare_search_url(url: str, max_rent=None, min_size=None) -> str:
    """Newest first, offers only (no 'Gesuche'), max rent. Filters are path segments before the
    category/location code: /s-auf-zeit-wg/muenchen/sortierung:neuste/anzeige:angebote/preis::900/c199l6411"""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2:
        return url
    code = segs.pop()
    have = {s.split(":")[0] for s in segs if ":" in s}
    if "sortierung" not in have:
        segs.append("sortierung:neuste")
    if "anzeige" not in have:
        segs.append("anzeige:angebote")
    if max_rent and "preis" not in have:
        segs.append(f"preis::{max_rent}")
    return urlunsplit(parts._replace(path="/" + "/".join(segs + [code])))


# Runs in the page. The 2026 result list uses utility CSS classes only, so this goes by structure and text.
CARDS_JS = r"""
() => {
  const txt = (el) => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('article[data-adid]')) {
    const id = a.getAttribute('data-adid');
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const ps = [...a.querySelectorAll('p')];
    const title = a.querySelector('h2 a, h3 a');
    const desc = title && title.closest('h2, h3').nextElementSibling;
    const tags = [...a.querySelectorAll('p span')].map(txt).filter(Boolean);
    const logo = a.querySelector('img[alt^="Logo des Unternehmens"]');   // companies: logo + name
    const company = logo ? (txt(logo.parentElement.querySelector('span')) || logo.alt.replace(/^Logo des Unternehmens\s*/, '')) : '';
    const img = a.querySelector('img');
    out.push({
      id,
      href: a.getAttribute('data-href') || (title ? title.getAttribute('href') : ''),
      title: txt(title),
      desc: desc && desc.tagName === 'P' ? txt(desc) : '',
      loc: [...a.querySelectorAll('span')].map(txt).find(s => /^\d{5}\b/.test(s)) || '',
      price: ps.map(txt).find(t => /^[\d.]+(,\d+)?\s*€/.test(t)) || '',
      facts: ps.map(txt).find(t => /m²|Zi\./.test(t) && t.length < 80) || '',
      seller: company || (tags.length ? tags[tags.length - 1] : ''),   // company name, or "Von Privat"
      top: [...a.querySelectorAll('div')].some(d => !d.children.length && txt(d) === 'TOP'),
      image: img ? img.getAttribute('src') : '',
      text: txt(a).slice(0, 1500),
    });
  }
  return out;
}
"""


def card_to_listing(r: dict) -> Listing:
    href = r.get("href") or ""
    l = listing_from_url(href if href.startswith("http") else BASE + href)
    l.title = (r.get("title") or "").strip()
    l.card_text = r.get("text") or ""
    l.rent = _euro(r.get("price"))
    l.size = _size(r.get("facts"))
    loc = r.get("loc") or ""  # "81543 Untergiesing-Harlaching"
    l.district = re.sub(r"^\d{5}\s*", "", loc)
    l.address = loc
    if m := re.search(r"([\d,]+)\s*Zi\.", r.get("facts") or ""):
        l.flat_type = f"{m.group(1)} Zimmer"
    seller = r.get("seller") or ""
    l.poster = "" if seller.lower().startswith("von privat") else seller
    l.image = _large(r.get("image") or "")
    l.exchange = bool(EXCHANGE_RE.search(f"{l.title} {r.get('desc', '')}"))
    return l


# Kleinanzeigen A/B-tests a redesigned ad page in 2026; both versions keep the viewad-* ids used here.
DETAIL_JS = r"""
() => {
  const txt = (el) => el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  const h1 = document.querySelector('#viewad-title');
  let title = '';
  if (h1) {                                  // the classic h1 also holds hidden "Reserviert • Gelöscht •" labels
    const c = h1.cloneNode(true);
    c.querySelectorAll('.is-hidden').forEach(n => n.remove());
    title = txt(c);
  }
  const pairs = [], tags = new Set([...document.querySelectorAll('.checktag')].map(txt).filter(Boolean));
  const root = document.querySelector('#viewad-details');
  for (const li of root ? root.querySelectorAll('li') : []) {
    const spans = li.querySelectorAll(':scope > span');
    const v = li.querySelector('.addetailslist--detail--value');
    if (spans.length === 2 && !v) {            // redesign: <li><span>Wohnfläche</span><span>12 m²</span></li>
      pairs.push([txt(spans[0]), txt(spans[1])]);
    } else if (v) {                            // classic: <li>Wohnfläche<span class="...--value">12 m²</span></li>
      const k = li.cloneNode(true);
      k.querySelectorAll('.addetailslist--detail--value').forEach(n => n.remove());
      pairs.push([txt(k), txt(v)]);
    } else if (txt(li) && txt(li).length < 40) {
      tags.add(txt(li));                       // features: Balkon, Einbauküche, ...
    }
  }
  const desc = document.querySelector('#viewad-description-text');
  const prof = document.querySelector('#viewad-contact a[href*="userId="], a[aria-label^="Profil von"][href*="userId="]');
  const named = document.querySelector('#viewad-contact .userprofile-vip a, #viewad-contact .userprofile-vip');
  const since = [...document.querySelectorAll('span')].find(s => /^Aktiv seit/.test(txt(s)));
  const box = document.querySelector('#viewad-contact') || (since?.parentElement?.parentElement?.parentElement || null);
  const images = [...document.querySelectorAll('img[data-imgsrc], #viewad-image, img[src*="prod-ads"]')]
    .map(i => i.getAttribute('data-imgsrc') || i.getAttribute('src') || '')
    .filter(s => s.includes('img.kleinanzeigen.de') && s.includes('rule=$_59.'));   // the gallery, not similar ads
  const meta = (p) => { const m = document.querySelector(`meta[property="${p}"]`); return m ? m.content : ''; };
  const reserved = [...document.querySelectorAll('#viewad-title .pvap-reserved-title')].some(n => !n.classList.contains('is-hidden'));
  const date = [...document.querySelectorAll('#viewad-extra-info span, #viewad-main-info span')].map(txt)
    .find(t => /^\d{2}\.\d{2}\.\d{4}$/.test(t)) || '';
  return {
    title, pairs, tags: [...tags], images, reserved, date,
    price: txt(document.querySelector('#viewad-price')),
    street: txt(document.querySelector('[itemprop="streetAddress"]')),
    locality: txt(document.querySelector('#viewad-locality')),
    description: desc ? desc.innerText.trim() : '',
    poster: named ? txt(named) : (prof ? (prof.getAttribute('aria-label') || '').replace(/^Profil von\s*/, '') : ''),
    contact: txt(box),
    poster_id: prof ? ((prof.getAttribute('href') || '').match(/userId=(\d+)/) || [])[1] || '' : '',
    lat: meta('og:latitude'), lng: meta('og:longitude'),
  };
}
"""
# The redesign has no og:latitude; its embedded page data has the ad's location block instead.
LOCATION_RE = re.compile(
    r'"locationSearchParameters":\[0,\{.{0,300}?"streetAndHouseNumber":\[0,(null|"[^"]*")\].{0,300}?'
    r'"latitude":\[0,([\d.]+)\],"longitude":\[0,([\d.]+)\]',
    re.S,
)


def apply_detail(l: Listing, d: dict, html: str = "") -> Listing:
    if d.get("title"):
        l.title = d["title"]
    pairs = dict(d.get("pairs") or [])
    # Warmmiete (with Nebenkosten) when the ad gives it, like WG-Gesucht's Gesamtmiete
    l.rent = _euro(pairs.get("Warmmiete")) or _euro(d.get("price")) or l.rent
    l.size = _size(pairs.get("Wohnfläche")) or l.size
    art = (pairs.get("Art der Unterkunft") or "").lower()  # Auf Zeit & WG: Privatzimmer, Gemeinsames Zimmer, ...
    if "zimmer" in art:
        l.kind = "room"
    elif any(w in art for w in ("wohnung", "haus", "apartment", "appartement")):
        l.kind = "studio"
    l.kind = guess_kind(l.title, l.kind)
    if l.kind == "room" and (n := pairs.get("Anzahl Mitbewohner", "")).isdigit():
        l.flatmates = f"{int(n) + 1}er WG ({n} Mitbewohner)"
    l.flat_type = pairs.get("Wohnungstyp") or pairs.get("Art der Unterkunft") or l.flat_type
    l.available_from = _month_start(pairs.get("Verfügbar ab")) or l.available_from
    page_data = LOCATION_RE.search(unescape(html)) if not d.get("lat") and html else None
    street = (d.get("street") or "").strip().rstrip(",").strip()
    if not street and page_data and page_data.group(1) != "null":
        street = page_data.group(1).strip('"')
    locality = d.get("locality") or ""  # "81375 München - Hadern"
    l.street = street
    l.address = ", ".join(filter(None, [street, locality])) or l.address
    if m := re.search(r"\d{5}\s+[^-]+-\s*(.+)$", locality):
        l.district = m.group(1).strip()
    l.description = f"[Beschreibung]\n{d.get('description') or ''}"[:8000]
    contact = d.get("contact") or ""
    facts = [f"{k}: {v}" for k, v in pairs.items()] + d.get("tags", [])
    facts.append("Anbieter: " + ("gewerblich" if "gewerblich" in contact.lower() else "privat"))
    if not street:
        facts.append("Lage: nur das Postleitzahl-Gebiet ist bekannt, keine Straße")
    l.details = " | ".join(facts)[:2500]
    if (poster := d.get("poster") or "").lower() not in ("privat", "private", "anbieter"):  # some type that in
        l.poster = poster or l.poster
    if d.get("poster_id"):
        l.poster_id = PREFIX + d["poster_id"]
    if m := re.search(r"Aktiv seit (\d{2}\.\d{2}\.\d{4})", contact):
        l.member_since = m.group(1)
    if imgs := list(dict.fromkeys(_large(u) for u in d.get("images", []))):
        l.images = imgs
        l.image = imgs[0]
    try:
        l.lat, l.lng = float(d["lat"]), float(d["lng"])
    except (KeyError, TypeError, ValueError):
        if page_data:
            l.lat, l.lng = float(page_data.group(2)), float(page_data.group(3))
    if m := DATE_RE.search(d.get("date") or ""):  # the day it was posted (no time on Kleinanzeigen)
        posted = datetime.strptime(m.group(1), "%d.%m.%Y")
        l.online_min = max(0, int((datetime.now() - posted).total_seconds() // 60))
        days = (datetime.now().date() - posted.date()).days
        l.online = "Online: " + {0: "heute", 1: "seit gestern"}.get(days, f"seit {m.group(1)}")
    swap = (pairs.get("Tauschangebot") or "").lower()
    l.exchange = (
        l.exchange or bool(swap and "kein" not in swap) or bool(EXCHANGE_RE.search(f"{l.title} {l.description}"))
    )
    return l


async def shows_logged_out(page) -> bool:
    url = page.url.lower()
    if "login.kleinanzeigen.de" in url or "einloggen" in url:
        return True
    if await page.locator("#viewad-contact-button-login:visible").count():  # ad page: "log in to write"
        return True
    return '"user_logged_in":"false"' in await page.content()


def _conversations(payloads) -> list[dict]:
    """Conversation dicts from the message box's own API responses (field names vary, so look around)."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            if any(k in x for k in ("adId", "adTitle")) and any(k in x for k in ("id", "conversationId")):
                found.append(x)
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for p in payloads:
        walk(p)
    out = {}
    for c in found:
        cid = str(c.get("id") or c.get("conversationId"))
        bound = str(c.get("boundness") or "").upper()  # INBOUND = their message is the newest
        unread = c.get("unread") or c.get("unseen") or c.get("unreadMessagesCount") or 0
        out[cid] = {
            "key": "conv:ka:" + cid,
            "href": f"{INBOX_URL}?conversationId={cid}",
            "ad_id": PREFIX + str(c.get("adId")) if c.get("adId") else "",
            "name": str(c.get("sellerName") or c.get("partnerName") or c.get("userName") or ""),
            "title": str(c.get("adTitle") or ""),
            "when": str(c.get("receivedDate") or c.get("lastMessageDate") or c.get("updatedAt") or ""),
            "text": str(c.get("textShortTrimmed") or c.get("lastMessage") or c.get("text") or "")[:600],
            "unread": bool(unread),
            "mine": True if bound == "OUTBOUND" else False if bound == "INBOUND" else None,
        }
    return list(out.values())


class Kleinanzeigen(Site):
    name = "kleinanzeigen"
    label = "Kleinanzeigen"
    domain = "kleinanzeigen.de"
    login_url = BASE + "/m-einloggen.html"
    send_verified = False  # flip after a real send was confirmed in the Kleinanzeigen inbox

    def listing_from_url(self, url: str) -> Listing:
        return listing_from_url(url)

    def prepare_search_url(self, url, max_rent=None, min_size=None) -> str:
        return prepare_search_url(url, max_rent, min_size)

    async def search(self, url, kind="room") -> list[Listing]:
        async with self.lock, self._page() as page:
            await self._goto(page, url)
            try:
                await page.wait_for_selector("article[data-adid]", timeout=10000)
            except PWTimeout:
                pass
            raw = await page.evaluate(CARDS_JS)
            if not raw:
                await self._check_block(page)
                log.warning("0 listings parsed – search URL wrong or page markup changed")
            out = []
            for r in raw:
                l = card_to_listing(r)
                l.kind = guess_kind(l.title, kind)
                if not is_commercial(l.poster, l.title):
                    out.append(l)
            return out

    async def fetch_details(self, l: Listing) -> Listing:
        async with self.lock, self._page() as page:
            resp = await self._goto(page, l.url)
            if resp and resp.status in (404, 410):
                raise LookupError("ad no longer online")
            d = await page.evaluate(DETAIL_JS)
            if not d.get("title"):
                await self._check_block(page)
                raise LookupError("no ad found (deleted, or the page markup changed)")
            if d.get("reserved"):
                raise LookupError("ad is reserved or deleted")
            return apply_detail(l, d, await page.content())

    async def logged_in_on(self, page) -> bool:
        await self._goto(page, INBOX_URL, priority=True)  # logged out, it sends you to login.kleinanzeigen.de
        return not await shows_logged_out(page)

    async def send_message(self, l: Listing, text: str, dry_run=True):
        """The contact form sits on the ad page (1 page load). Not verified on the live site yet."""
        async with self.lock, self._page() as page:
            await self._goto(page, l.url, priority=True)
            if await shows_logged_out(page):
                raise LoggedOut()
            boxes = [
                "#viewad-contact-form textarea",
                "form[action*='kontakt'] textarea",
                "textarea[name='message']",
                "#viewad-contact-message",
            ]
            box = await self._first_visible(page, boxes, timeout=3000)
            if not box:  # some layouts open the form with a button first
                btn = await self._first_visible(
                    page,
                    [
                        "#viewad-contact-button",
                        "button:has-text('Nachricht schreiben')",
                        "a:has-text('Nachricht schreiben')",
                    ],
                    timeout=3000,
                )
                if btn:
                    await btn.click(timeout=5000)
                    await page.wait_for_timeout(800)
                box = await self._first_visible(page, boxes, timeout=8000)
            if not box:
                await self._check_block(page)
                return "failed", await self._shot(page, "nobox"), "Message box not found"
            await self._type_into(box, text)

            if dry_run:
                return "dry_run", await self._shot(page, "dryrun"), "DRY RUN – form filled, not sent"

            btn = await self._first_visible(
                page,
                [
                    "#viewad-contact-form button[type='submit']",
                    "form:has(textarea[name='message']) button[type='submit']",
                    "button:has-text('Nachricht senden')",
                ],
                timeout=5000,
            )
            if not btn:
                return "failed", await self._shot(page, "nobutton"), "Send button not found"

            api_ok = asyncio.get_running_loop().create_future()

            async def on_resp(resp):
                if resp.request.method == "POST" and "kontakt" in resp.url.lower() and not api_ok.done():
                    api_ok.set_result(resp.ok)

            page.on("response", on_resp)
            await btn.click(timeout=10000)
            ok = False
            try:
                ok = await asyncio.wait_for(asyncio.shield(api_ok), timeout=12)
            except asyncio.TimeoutError:
                pass
            if not ok:  # UI fallback: the page says it went out
                try:
                    await page.get_by_text(re.compile(r"nachricht.{0,30}(gesendet|verschickt)", re.I)).first.wait_for(
                        timeout=6000
                    )
                    ok = True
                except PWTimeout:
                    pass
            shot = await self._shot(page, "sent" if ok else "unsure")
            if ok:
                return "sent", shot, "Sent ✅"
            return "failed", shot, "Clicked send but couldn't confirm – check the screenshot"

    async def inbox(self) -> list[dict]:
        """The message box is an app that loads its conversation list as JSON: catch that (1 page load)."""
        payloads = []

        async def on_resp(resp):
            if "conversation" in resp.url.lower() and "json" in (resp.headers.get("content-type") or ""):
                try:
                    payloads.append(await resp.json())
                except Exception:
                    pass

        async with self.lock, self._page() as page:
            page.on("response", on_resp)
            await self._goto(page, INBOX_URL)
            if await shows_logged_out(page):
                raise LoggedOut()
            await page.wait_for_timeout(5000)
            convs = _conversations(payloads)
            if not convs:
                await self._check_block(page)
                log.info("no conversations found in the Kleinanzeigen message box (%d responses)", len(payloads))
            return convs
