"""ImmoScout24: flats ("wohnung-mieten") and WG rooms ("wg-zimmer"), read through the ImmoScout24 app's API.

The website answers automated browsers with an "Ich bin kein Roboter" page on the very first request (checked
2026-10-05), so this reads the JSON API that the ImmoScout24 app uses, as Fredy and Flathunter do. That's a
deliberate exception to "no anti-detection stealth": the requests carry the app's User-Agent. They are
read-only, count against this site's page budget like every page load, and there is no captcha solving,
no login and no sending. You send the message yourself in the app and tap "I sent it" in Telegram.
"""

import logging
import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

from sites import EXCHANGE_RE, Blocked, Listing, Site, guess_kind

log = logging.getLogger("immoscout")
BASE = "https://www.immobilienscout24.de"
API = "https://api.mobile.immobilienscout24.de"
USER_AGENT = "ImmoScout_27.12_26.2_._"  # the app's; sites.immoscout.user_agent overrides it
PREFIX = "is:"  # Listing ids: "is:123456789"
ID_RE = re.compile(r"/expose/(\d+)")
EURO_RE = re.compile(r"(\d[\d.]*)(?:,\d+)?\s*€")
SIZE_RE = re.compile(r"(\d+)(?:,\d+)?\s*m²")
DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})\b")
AGE_RE = re.compile(r"(\d+|eine[mr]?)\s+(sekunde|minute|stunde|tag|woche|monat)", re.I)
MINUTES = {"sekunde": 0, "minute": 1, "stunde": 60, "tag": 1440, "woche": 10080, "monat": 43200}
TYPES = {"wohnung-mieten": "apartmentrent", "wg-zimmer": "flatshareroom"}  # web search path -> API type
WEB_ONLY = {"enteredfrom", "sorting", "pagenumber", "viewmode", "centerofsearchaddress"}  # not API filters
TZ = ZoneInfo("Europe/Berlin")


def _euro(s):
    m = EURO_RE.search(s or "")
    return int(m.group(1).replace(".", "")) if m else None


def _size(s):
    m = SIZE_RE.search(s or "")
    return int(m.group(1)) if m else None


def _date(s):
    """'4.10.2026' / '31.10.26' -> '04.10.2026', 'sofort' / 'ab sofort' -> today."""
    s = (s or "").strip().lower()
    if m := DATE_RE.search(s):
        d, mon, y = m.groups()
        return f"{int(d):02d}.{int(mon):02d}.{y if len(y) == 4 else '20' + y}"
    return f"{date.today():%d.%m.%Y}" if re.search(r"\bsofort\b", s) else None


def age_minutes(s):
    """'vor 34 Minuten' -> 34, 'vor einem Tag' -> 1440."""
    if m := AGE_RE.search(s or ""):
        return (int(m.group(1)) if m.group(1).isdigit() else 1) * MINUTES[m.group(2).lower()]
    return 0 if re.search(r"gerade|soeben|jetzt", s or "", re.I) else None


def _local(ts):
    """'2026-10-09T10:44:55.572+00:00' -> 'Fri 09.10. 12:44' (German time)."""
    try:
        return datetime.fromisoformat(ts).astimezone(TZ).strftime("%a %d.%m. %H:%M")
    except (TypeError, ValueError):
        return ""


def listing_from_url(url: str) -> Listing:
    m = ID_RE.search(url)
    lid = m.group(1) if m else str(abs(hash(url)))
    return Listing(id=PREFIX + lid, url=f"{BASE}/expose/{lid}", site="immoscout", kind="studio")


def prepare_search_url(url: str, max_rent=None, min_size=None) -> str:
    """Newest first, max rent (Warmmiete, like WG-Gesucht's Gesamtmiete), and for flats min size and no swap
    flats, as parameters of the website's search URL; search() turns that into an API query."""
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if max_rent and "price" not in q:
        q["price"] = f"-{max_rent}"
        q.setdefault("pricetype", "calculatedtotalrent")
    if parts.path.rstrip("/").endswith("wohnung-mieten"):
        if min_size and "livingspace" not in q:
            q["livingspace"] = f"{min_size}-"
        q.setdefault("exclusioncriteria", "swapflat")
    q["sorting"] = "2"
    return urlunsplit(parts._replace(query=urlencode(q)))


def api_params(url: str) -> dict:
    """The website's search URL (/Suche/de/bayern/muenchen/wohnung-mieten?price=-1100...) as an API query. The
    app and the website share one search backend, so the filter parameters carry over unchanged."""
    parts = urlsplit(url)
    segs = [s for s in parts.path.split("/") if s]
    rtype = TYPES.get(segs[-1].lower()) if len(segs) >= 3 and segs[0].lower() == "suche" else None
    if not rtype:
        raise ValueError(f"not an ImmoScout24 search URL ending in /{' or /'.join(TYPES)}: {url}")
    p = {k: v for k, v in parse_qsl(parts.query) if k.lower() not in WEB_ONLY}
    if segs[1].lower() in ("radius", "shape"):  # /Suche/radius/wohnung-mieten?geocoordinates=48.1;11.5;3.0
        p["searchType"] = segs[1].lower()
    else:
        p.update(searchType="region", geocodes="/" + "/".join(segs[1:-1]))
    p.update(realestatetype=rtype, sorting="-firstactivation", pagenumber="1")
    return p


def card_to_listing(it: dict, kind="studio", plus=False) -> Listing:
    """One search result. The rent here is the Kaltmiete; the ad page gives the Gesamtmiete."""
    l = listing_from_url(f"/expose/{it.get('id')}")
    l.title = re.sub(r"\s+", " ", it.get("title") or "").strip()
    vals = [a.get("value") or "" for a in it.get("attributes") or []]
    l.rent = _euro(vals[0]) if vals else None
    l.size = _size(" ".join(vals))
    if m := re.search(r"([\d,]+)\s*Zi\.", " ".join(vals)):
        l.flat_type = f"{m.group(1)} Zimmer"
    addr = it.get("address") or {}
    line = re.sub(r"\s*\(unvollständige Adresse\)", "", addr.get("line") or "").strip()
    parts = [p.strip() for p in line.split(",")]  # "Musterstraße 1, 80331 München, Altstadt"
    l.street = parts[0] if len(parts) > 2 and not re.match(r"\d{5}\b", parts[0]) else ""
    l.district = parts[-1] if len(parts) > 1 else ""
    l.address = line
    if addr.get("lat") and addr.get("lon"):
        l.lat, l.lng = float(addr["lat"]), float(addr["lon"])
    l.online = it.get("published") or ""  # "vor 34 Minuten"
    l.online_min = age_minutes(l.online)
    l.kind = "room" if it.get("realEstateType") == "flatshareroom" else guess_kind(l.title, kind)
    pay = it.get("paywallListing") or {}
    if pay.get("active") and not plus:
        until = _local(pay.get("earlyAccessExpiresAt"))
        l.contact_note = "Only ImmoScout Plus members can write" + (f" until {until}" if until else "")
    l.card_text = " | ".join([l.title, *vals, line, l.online, "privat" if it.get("isPrivate") else "gewerblich"])
    return l


def _centre(shapes):
    """Middle of the postcode area an ad with a hidden address shows on its map."""
    pts = [p for s in shapes or [] for p in s.get("outline") or [] if "lat" in p and "lng" in p]
    if not pts:
        return None
    return sum(p["lat"] for p in pts) / len(pts), sum(p["lng"] for p in pts) / len(pts)


def apply_detail(l: Listing, d: dict, plus=False) -> Listing:
    head = d.get("header") or {}
    if (state := head.get("publicationState")) and state != "active":
        raise LookupError(f"ad is {state}")
    by_type = {}
    for s in d.get("sections") or []:
        by_type.setdefault(s.get("type"), []).append(s)

    def first(t):
        return (by_type.get(t) or [{}])[0]

    top = {a.get("label") or "": a.get("text") or "" for a in first("TOP_ATTRIBUTES").get("attributes") or []}
    pairs, facts = {}, []
    for s in by_type.get("ATTRIBUTE_LIST", []):  # Hauptkriterien, Kosten, WG-Details, Ausstattung, ...
        for a in s.get("attributes") or []:
            label = re.sub(r"(\s+ca\.)?\s*:\s*$", "", a.get("label") or "").strip()
            if a.get("type") == "TEXT" and label:
                pairs[label] = a.get("text") or ""
                facts.append(f"{label}: {pairs[label]}")
            elif a.get("type") == "CHECK" and label:  # Balkon/Terrasse, Einbauküche, ...
                facts.append(label)

    if title := re.sub(r"\s+", " ", first("TITLE").get("title") or "").strip():
        l.title = title
    l.kind = "room" if head.get("realEstateType") == "flatshareroom" else guess_kind(l.title, l.kind)
    l.rent = (
        _euro(pairs.get("Gesamtmiete"))
        or _euro(top.get("Warmmiete"))
        or _euro(pairs.get("Kaltmiete (zzgl. Nebenkosten)"))
        or l.rent
    )
    l.size = _size(top.get("Zimmerfläche") or pairs.get("Wohnfläche") or top.get("Wohnfläche")) or l.size
    l.available_from = (
        _date(pairs.get("Bezugsfrei ab") or pairs.get("Verfügbar ab") or top.get("Verfügbar ab")) or l.available_from
    )
    l.available_to = _date(pairs.get("Verfügbar bis") or pairs.get("Frei bis")) or l.available_to
    flat_type = pairs.get("Wohnungstyp", "")
    rooms = top.get("Zimmer") or pairs.get("Zimmer")
    l.flat_type = ", ".join(filter(None, [flat_type if flat_type != "Sonstige" else "", rooms and f"{rooms} Zimmer"]))
    if l.kind == "room":
        counts = [
            f"{pairs[k]}{s}"
            for k, s in (("Weibliche Mitbewohner", "w"), ("Männliche Mitbewohner", "m"), ("Diverse Mitbewohner", "d"))
            if pairs.get(k)
        ]
        wg = pairs.get("WG-Größe", "")
        l.flatmates = f"{wg} ({','.join(counts)})" if wg and counts else wg or l.flatmates
        g = pairs.get("Gesuchtes Geschlecht", "").lower()  # "weiblich", "männlich", "männlich oder weiblich"
        female, male = "weiblich" in g or "frau" in g, "männlich" in g or "mann" in g
        wanted = (
            "Mitbewohnerin gesucht" if female and not male else "Mitbewohner gesucht" if male and not female else ""
        )
        if age := pairs.get("Gesuchtes Alter"):
            wanted = "; ".join(filter(None, [wanted, f"Alter: {age}"]))
        l.wanted = wanted or l.wanted

    if m := first("MAP"):
        line1, line2 = (m.get("addressLine1") or "").strip(), (m.get("addressLine2") or "").strip()
        # a hidden address says "Die vollständige Adresse der Immobilie erhältst du vom Anbieter."
        l.street = "" if "adresse" in line1.lower() else line1
        l.address = ", ".join(filter(None, [l.street, line2])) or l.address  # "Musterstraße 1, 80331 ..."
        if not l.district and (dm := re.match(r"\d{5}\s+([^,]+)", line2)):
            l.district = dm.group(1).strip()
        loc = m.get("location") or {}
        if loc.get("lat") and loc.get("lng"):
            l.lat, l.lng = float(loc["lat"]), float(loc["lng"])
        elif l.lat is None and (c := _centre(m.get("zipCodeShapes"))):
            l.lat, l.lng = c

    l.description = "\n\n".join(
        f"[{s.get('title') or 'Text'}]\n{s.get('text').strip()}" for s in by_type.get("TEXT_AREA", []) if s.get("text")
    )[:8000]
    agent = first("AGENTS_INFO")
    company, name = (agent.get("company") or "").strip(), (agent.get("name") or "").strip()
    private = str((d.get("adTargetingParameters") or {}).get("obj_privateOffer")).lower() == "true"
    l.poster = company or name or l.poster
    facts.append("Anbieter: " + ("privat" if private else f"gewerblich ({company})" if company else "gewerblich"))
    if not l.street:
        facts.append("Lage: nur das Postleitzahl-Gebiet ist bekannt, keine Straße")
    l.details = " | ".join(facts)[:2500]
    # an agency has many flats: writing about one must not block the others, so only private advertisers
    if private and (cid := (d.get("adTargetingParameters") or {}).get("obj_cId")):
        l.poster_id = PREFIX + str(cid)
    imgs = [
        x["fullImageUrl"].replace("/format/webp/", "/format/jpg/")  # Telegram can't show webp photos
        for s in by_type.get("MEDIA", [])
        for x in s.get("media") or []
        if x.get("type") == "PICTURE" and x.get("fullImageUrl")
    ]
    if imgs := list(dict.fromkeys(imgs)):
        l.images = imgs
        l.image = imgs[0]
    l.exchange = l.exchange or bool(EXCHANGE_RE.search(f"{l.title} {l.description}"))

    c = first("CONTACT")
    window = c.get("freemiumSettings") or {}
    if plus:
        l.contact_note = ""
    elif str(c.get("premiumProfileRequiredForContacting")).lower() == "true":
        l.contact_note = "Only ImmoScout Plus members can contact this advertiser"
    elif window.get("freemiumPeriodActive"):
        until = _local(window.get("dateEnding"))
        l.contact_note = "Only ImmoScout Plus members can write" + (f" until {until}" if until else "")
    elif "freemiumPeriodActive" in window:  # the window is over (the search result may still say otherwise)
        l.contact_note = ""
    return l


class ImmoScout(Site):
    name = "immoscout"
    label = "ImmoScout24"
    domain = "immobilienscout24.de"
    send_by_hand = True

    @property
    def plus(self) -> bool:
        """sites.immoscout.plus: you have ImmoScout Plus, so ads only Plus members can answer get no 🔒 note."""
        return bool(self.cfg.get("plus", False))

    def listing_from_url(self, url: str) -> Listing:
        return listing_from_url(url)

    def prepare_search_url(self, url, max_rent=None, min_size=None) -> str:
        return prepare_search_url(url, max_rent, min_size)

    def check_search_url(self, url):
        api_params(url)

    async def _api(self, path, params=None, body=None):
        """Every API request goes through here, so it counts against this site's page budget (RateGuard)."""
        if self.guard:
            self.guard.acquire()  # raises BudgetExceeded / CoolingDown
        headers = {"User-Agent": self.cfg.get("user_agent") or USER_AGENT, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            if body is None:
                r = await client.get(API + path, params=params, headers=headers)
            else:
                r = await client.post(API + path, params=params, json=body, headers=headers)
        if r.status_code in (401, 403, 429, 503):
            raise Blocked(f"HTTP {r.status_code}")
        if r.status_code in (404, 410):
            raise LookupError("ad no longer online")
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            raise Blocked("the app API answered with a web page instead of data") from None

    async def search(self, url, kind="studio") -> list[Listing]:
        d = await self._api("/search/list", api_params(url), {"supportedResultListTypes": [], "userData": {}})
        out = []
        for r in d.get("resultListItems") or []:
            it = r.get("item") or {}
            if r.get("type") != "EXPOSE_RESULT" or not it.get("id"):
                continue
            l = card_to_listing(it, kind, self.plus)
            if l.kind == "room" and not it.get("isPrivate"):  # WG rooms from companies: mostly co-living
                continue
            out.append(l)
        return out

    async def fetch_details(self, l: Listing) -> Listing:
        return apply_detail(l, await self._api(f"/expose/{l.id.removeprefix(PREFIX)}"), self.plus)
