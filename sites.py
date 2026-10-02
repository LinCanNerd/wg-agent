"""What every site has in common: the Listing record, one shared Chromium for all sites, and the Site
base class with the page-load helpers. Each site (wg.py, kleinanzeigen.py) adds its own URLs and selectors.
"""

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

log = logging.getLogger("sites")

BLOCK_WORDS = re.compile(
    r"captcha|sicherheitsabfrage|zugriff verweigert|access denied|too many requests|"
    r"ungewöhnlich viele|verify you are human|warum erscheint diese seite|ich bin kein roboter|"
    r"nutzungsaktivitäten, die den zweck haben",
    re.I,
)
# Chromium forgets session-only cookies when it closes, and some logins (WG-Gesucht) need them.
# --login and Browser.stop() save all cookies here; Browser.start() puts back the session-only ones.
# Private: gitignored.
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
    "mr. lodge",
    "mr lodge",
    "miethelden",
)
SITE_LABELS = {"wg-gesucht": "WG-Gesucht", "kleinanzeigen": "Kleinanzeigen"}


class Blocked(Exception):
    """Captcha / rate limit detected."""


class LoggedOut(Exception):
    pass


@dataclass
class Listing:
    id: str  # WG-Gesucht ids as they are; other sites get a prefix ("ka:1234567890")
    url: str
    site: str = "wg-gesucht"
    title: str = ""
    card_text: str = ""
    kind: str = "room"  # room | studio
    rent: int | None = None
    size: int | None = None
    available_from: str | None = None  # dd.mm.yyyy
    available_to: str | None = None
    district: str = ""
    street: str = ""  # empty if the ad only gives the postcode area
    flat_type: str = ""  # "3er WG", "1-Zimmer-Wohnung"
    flatmates: str = ""  # "3er WG (1w,1m,0d,0n)"
    wanted: str = ""  # who they look for (from icons / "Gesucht wird")
    poster: str = ""  # name shown on the card
    poster_id: str = ""  # the advertiser's user id on the site (same-person check)
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


def is_commercial(poster: str, title: str = "") -> bool:
    return any(c in f"{poster} {title}".lower() for c in COMMERCIAL_POSTERS)


class Browser:
    """One persistent Chromium profile for all sites, so a single --login keeps every site's cookies."""

    def __init__(
        self,
        profile_dir="browser-profile",
        headless=True,
        shots_dir="screenshots",
        block_resources=True,
        session_file=SESSION_FILE,
        domains=("wg-gesucht.de",),
    ):
        self.profile_dir = str(Path(profile_dir).resolve())
        self.headless = headless
        self.block_resources = block_resources
        self.session_file = Path(session_file)
        self.domains = tuple(domains)
        self.shots = Path(shots_dir)
        self.shots.mkdir(exist_ok=True)
        self.lock = asyncio.Lock()  # one page at a time, across all sites
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
            if c.get("expires", -1) == -1
            and any(d in c["domain"] for d in self.domains)
            and (c["name"], c["domain"]) not in have
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

    async def shot(self, page, tag):
        p = self.shots / f"{int(time.time())}_{tag}.png"
        try:
            await page.screenshot(path=str(p), full_page=False)
            return str(p)
        except Exception:
            return None


class Site:
    """One flat-ad site. Subclasses fill in the class attributes and the methods marked 'per site'."""

    name = ""  # key in config.yaml and the database, e.g. "wg-gesucht"
    label = ""  # shown to you, e.g. "WG-Gesucht"
    domain = ""  # e.g. "wg-gesucht.de"
    login_url = ""  # where --login opens
    send_verified = True  # False: sending is a dry run unless sites.<name>.dry_run says otherwise

    def __init__(self, browser: Browser, guard=None, cfg: dict | None = None):
        self.browser = browser
        self.guard = guard
        self.cfg = cfg or {}

    @classmethod
    def handles(cls, url: str) -> bool:
        return urlsplit(url).netloc.lower().endswith(cls.domain)

    @property
    def ctx(self):
        return self.browser.ctx

    @property
    def lock(self):
        return self.browser.lock

    # ---------- per site ----------
    def listing_from_url(self, url: str) -> Listing:
        raise NotImplementedError

    def prepare_search_url(self, url: str, max_rent=None, min_size=None) -> str:
        """Let the site do the filtering and sort newest first."""
        return url

    async def search(self, url: str, kind="room") -> list[Listing]:
        raise NotImplementedError

    async def fetch_details(self, l: Listing) -> Listing:
        """Fill in the whole ad. Raises LookupError if the ad is gone."""
        raise NotImplementedError

    async def logged_in_on(self, page) -> bool:
        """Load a members-only page in `page` (1 page load) and tell whether we're logged in."""
        raise NotImplementedError

    async def send_message(self, l: Listing, text: str, dry_run=True):
        """Returns (status, screenshot_path, info). status: sent | dry_run | already | failed."""
        raise NotImplementedError

    async def inbox(self) -> list[dict]:
        """Conversations: key, href, name, title, when, text; optionally ad_id (the Listing id) and
        mine (True if the newest message is my own)."""
        return []

    async def conversation_ad_id(self, href: str) -> str | None:
        """Listing id of a conversation, when the inbox doesn't say (costs a page load)."""
        return None

    # ---------- shared ----------
    async def is_logged_in(self) -> bool:
        async with self.lock, self._page() as page:
            return await self.logged_in_on(page)

    @asynccontextmanager
    async def _page(self):
        page = await self.ctx.new_page()
        try:
            yield page
        finally:
            await page.close()

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
        """Every page load goes through here, so it counts against this site's budget."""
        if self.guard:
            self.guard.acquire(priority=priority)  # raises BudgetExceeded / CoolingDown
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if resp and resp.status in (401, 403, 429, 503):
            raise Blocked(f"HTTP {resp.status}")
        await self._dismiss_overlays(page)
        return resp

    async def _shot(self, page, tag):
        return await self.browser.shot(page, f"{self.name}_{tag}")

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

    async def _type_into(self, box, text):
        await box.fill(text)
        await box.press("End")
        await box.type(" ", delay=80)  # real key events, so script-driven forms register the text
        await box.press("Backspace")


async def interactive_login(browser: Browser, sites: list[Site]):
    """Open a visible browser with one tab per site; you log in by hand, then press Enter."""
    await browser.start()
    results = []
    try:
        pages = []
        for s in sites:
            page = await browser.ctx.new_page()
            await s._goto(page, s.login_url, priority=True)
            pages.append(page)
        names = " and ".join(s.label for s in sites)
        print(f"\nA browser window opened. Log in to {names} there (one tab each; accept cookies too).")
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter here when you're logged in... ")
        # check in this same browser, before closing it drops the session-only cookies (1 page load each)
        for s, page in zip(sites, pages, strict=True):
            results.append((s, await s.logged_in_on(page)))
    finally:
        await browser.stop()  # also saves all cookies, session-only ones included, to SESSION_FILE
    for s, ok in results:
        if ok:
            print(f"✅ {s.label}: logged in. Session saved in {browser.profile_dir} and {browser.session_file}.")
        else:
            print(f"❌ {s.label} still shows the login page. Run --login {s.name} again and log in before Enter.")
