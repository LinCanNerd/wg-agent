"""Real commute times (bike + public transport) from each ad to your destinations.

The ad page contains exact coordinates (map_config markers). From those we ask:
  - Transitous (free, community-run MOTIS, no key) – one /plan call per destination returns the
    bike route AND the fastest public-transport connection. Throttled to 1 req/s, self-identifying
    User-Agent, 15 min pause on HTTP 429 (same etiquette as Fredy). Destinations with
    `bike_to_station_minutes` get a second call: bike to a station first, then the train.
  - or Google Distance Matrix if you set a key (provider: google).
  - fallback: straight-line estimate for the bike (marked with ≈).
The LLM gets the numbers – it doesn't have to guess travel times.
"""

import asyncio
import hashlib
import json
import logging
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("commute")
UA = "wg-agent/1.0 (personal flat search, low volume)"
TZ = ZoneInfo("Europe/Berlin")
MUNICH_VIEWBOX = "11.36,48.25,11.73,48.06"  # default geocoding area (lon,lat,lon,lat)


def haversine_km(a_lat, a_lng, b_lat, b_lng):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def next_weekday_at(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    d = datetime.now(TZ) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.replace(hour=h, minute=m, second=0, microsecond=0)


class Commute:
    def __init__(self, cfg: dict, kv_get, kv_set, city: str = "München"):
        self.cfg = cfg or {}
        self.city = city
        default_box = MUNICH_VIEWBOX if city.lower() in ("münchen", "muenchen", "munich") else ""
        self.viewbox = self.cfg.get("viewbox", default_box)
        self.enabled = bool(self.cfg.get("enabled", True)) and bool(self.cfg.get("destinations"))
        self.provider = self.cfg.get("provider", "transitous")
        self.dests = []
        self.kv_get, self.kv_set = kv_get, kv_set
        self._last = 0.0
        self._paused_until = 0.0
        self._lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=15, headers={"User-Agent": UA})

    # ---------- plumbing ----------
    async def _get(self, url, params):
        async with self._lock:
            if time.time() < self._paused_until:
                return None
            wait = 1.1 - (time.time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.time()
            try:
                r = await self.client.get(url, params=params)
            except Exception as e:
                log.warning("commute request failed: %s", e)
                return None
            if r.status_code == 429:
                self._paused_until = time.time() + 900
                log.warning("routing service rate-limited us, pausing 15 min")
                return None
            if r.status_code != 200:
                log.warning("routing HTTP %s: %s", r.status_code, r.text[:200])
                return None
            try:
                return r.json()
            except Exception:
                return None

    async def geocode(self, query):
        key = "geo:" + query
        if (hit := self.kv_get(key, None)) is not None:
            return hit
        params = {"q": query, "format": "json", "limit": 1, "countrycodes": "de"}
        if self.viewbox:
            params.update(viewbox=self.viewbox, bounded=1)
        data = await self._get("https://nominatim.openstreetmap.org/search", params)
        if data:
            res = [float(data[0]["lat"]), float(data[0]["lon"])]
            self.kv_set(key, res)
            return res
        return None

    async def setup(self):
        if not self.enabled:
            return
        for d in self.cfg["destinations"]:
            lat, lng = d.get("lat"), d.get("lng")
            if lat is None or lng is None:
                got = await self.geocode(d["query"])
                if not got:
                    log.error("could not geocode destination %r – set lat/lng in config", d["query"])
                    continue
                lat, lng = got
            self.dests.append({**d, "lat": float(lat), "lng": float(lng)})
            log.info("commute destination %s at %.5f,%.5f", d["name"], lat, lng)
        self.enabled = bool(self.dests)

    # ---------- providers ----------
    async def _transitous(self, lat, lng, d, when):
        data = await self._get(
            "https://api.transitous.org/api/v1/plan",
            {
                "fromPlace": f"{lat},{lng}",
                "toPlace": f"{d['lat']},{d['lng']}",
                "time": when.isoformat(),
                "arriveBy": "false",
                "directModes": "BIKE",
                "maxDirectTime": 5400,
                "numItineraries": 4,
            },
        )
        if not data:
            return None
        out = {}
        for it in data.get("direct") or []:
            legs = it.get("legs") or []
            if legs and legs[0].get("mode") == "BIKE" and it.get("duration"):
                out["bike_min"] = round(it["duration"] / 60)
                if legs[0].get("distance"):
                    out["bike_km"] = round(legs[0]["distance"] / 1000, 1)
                break
        if best := self._best_itinerary(data):
            out["transit_min"] = round(best["duration"] / 60)
            out["transfers"] = int(best.get("transfers") or 0)
            out["lines"] = self._lines(best)
        return out

    async def _transitous_bike_train(self, lat, lng, d, when):
        """Bike up to d['bike_to_station_minutes'] to any station, then public transport."""
        data = await self._get(
            "https://api.transitous.org/api/v1/plan",
            {
                "fromPlace": f"{lat},{lng}",
                "toPlace": f"{d['lat']},{d['lng']}",
                "time": when.isoformat(),
                "arriveBy": "false",
                "preTransitModes": "BIKE",
                "maxPreTransitTime": int(d["bike_to_station_minutes"]) * 60,
                "numItineraries": 4,
            },
        )
        best = self._best_itinerary(data)
        if not best:
            return {}
        legs = best.get("legs") or []
        out = {"bike_train_min": round(best["duration"] / 60), "bike_train_lines": self._lines(best)}
        if legs and legs[0].get("mode") == "BIKE":
            out["bike_to_station_min"] = round(legs[0]["duration"] / 60)
        return out

    @staticmethod
    def _best_itinerary(data):
        its = [i for i in (data or {}).get("itineraries") or [] if i.get("duration")]
        return min(its, key=lambda i: i["duration"]) if its else None

    @staticmethod
    def _lines(it):
        return [
            l.get("routeShortName")
            for l in it.get("legs", [])
            if l.get("mode") not in ("WALK", "BIKE") and l.get("routeShortName")
        ]

    async def _google(self, lat, lng, d, when):
        key = self.cfg.get("google_api_key")
        out = {}
        for mode in ("bicycling", "transit"):
            data = await self._get(
                "https://maps.googleapis.com/maps/api/distancematrix/json",
                {
                    "origins": f"{lat},{lng}",
                    "destinations": f"{d['lat']},{d['lng']}",
                    "mode": mode,
                    "departure_time": int(when.timestamp()),
                    "key": key,
                },
            )
            try:
                el = data["rows"][0]["elements"][0]
                if el.get("status") != "OK":
                    continue
                mins = round(el["duration"]["value"] / 60)
            except (TypeError, KeyError, IndexError):
                continue
            if mode == "bicycling":
                out["bike_min"] = mins
                out["bike_km"] = round(el["distance"]["value"] / 1000, 1)
            else:
                out["transit_min"] = mins
        return out

    # ---------- public ----------
    async def for_listing(self, l) -> tuple[dict, str]:
        """Returns ({dest_name: {...}}, one-line summary). Never raises."""
        if not self.enabled:
            return {}, ""
        lat, lng = l.lat, l.lng
        if lat is None and l.address:
            got = await self.geocode(f"{l.address}, {self.city}")
            if got:
                lat, lng = got
        if lat is None:
            return {}, "📍 no location in ad"
        # the key includes the destination settings, so moving a destination doesn't reuse stale times
        dest_sig = json.dumps([(d["name"], d["lat"], d["lng"], d.get("bike_to_station_minutes")) for d in self.dests])
        cache_key = f"route:{round(lat, 4)},{round(lng, 4)}:{hashlib.sha1(dest_sig.encode()).hexdigest()[:8]}"
        cached = self.kv_get(cache_key, None)
        if cached and all(d["name"] in cached for d in self.dests):
            res = cached
        else:
            when = next_weekday_at(self.cfg.get("depart", "08:00"))
            res = {}
            for d in self.dests:
                r = None
                try:
                    r = await (self._google if self.provider == "google" else self._transitous)(lat, lng, d, when)
                except Exception as e:
                    log.warning("routing failed: %s", e)
                r = r or {}
                if d.get("bike_to_station_minutes") and self.provider != "google":
                    try:
                        r.update(await self._transitous_bike_train(lat, lng, d, when))
                    except Exception as e:
                        log.warning("bike+train routing failed: %s", e)
                km = haversine_km(lat, lng, d["lat"], d["lng"])
                r["air_km"] = round(km, 1)
                if "bike_min" not in r:  # rough estimate: detour factor 1.3 at 15 km/h
                    r["bike_min"] = round(km * 1.3 / 15 * 60 + 2)
                    r["estimated"] = True
                res[d["name"]] = r
            if not any(v.get("estimated") for v in res.values()):
                self.kv_set(cache_key, res)
        return res, self.summary(res)

    def summary(self, res):
        parts = []
        for name, r in res.items():
            s = f"{name}: 🚲 {'≈' if r.get('estimated') else ''}{r.get('bike_min', '?')} min"
            if r.get("transit_min") is not None:
                lines = "/".join(dict.fromkeys(r.get("lines") or []))
                s += f" · 🚇 {r['transit_min']} min" + (f" ({lines})" if lines else "")
            if self._bike_train_better(r):
                lines = "/".join(dict.fromkeys(r.get("bike_train_lines") or []))
                s += f" · 🚲+🚇 {r['bike_train_min']} min" + (f" ({lines})" if lines else "")
            parts.append(s)
        return " | ".join(parts)

    @staticmethod
    def _bike_train_better(r):
        """Only worth showing when biking to the station beats walking there."""
        bt = r.get("bike_train_min")
        return bt is not None and (r.get("transit_min") is None or bt < r["transit_min"])

    def check(self, res) -> tuple[str | None, list[str]]:
        """Apply per-destination limits. Returns (exclude_reason or None, [warnings])."""
        warn = []
        for d in self.dests:
            r = res.get(d["name"])
            if not r:
                continue
            mb, mt = d.get("max_bike_minutes"), d.get("max_transit_minutes")
            if mb is None and mt is None:
                continue
            ok = (
                (mb is not None and r.get("bike_min") is not None and r["bike_min"] <= mb)
                or (mt is not None and r.get("transit_min") is not None and r["transit_min"] <= mt)
                or (mt is not None and r.get("bike_train_min") is not None and r["bike_train_min"] <= mt)
            )
            if not ok and r.get("estimated") and r.get("transit_min") is None:
                # routing service unreachable: never exclude on a rough guess, let you decide
                warn.append(f"route to {d['name']} not verified (≈{r.get('bike_min')} min by bike)")
                continue
            if not ok:
                msg = f"too far from {d['name']} ({self.summary({d['name']: r})})"
                if d.get("action", "mark") == "exclude":
                    return msg, warn
                warn.append(msg)
        return None, warn

    def prompt_text(self, res):
        if not res:
            return "COMMUTE: unknown (no location)."
        lines = ["COMMUTE (real routing, weekday 08:00 departure):"]
        for d in self.dests:
            r = res.get(d["name"])
            if not r:
                continue
            t = f"- to {d['name']}: bike {'~' if r.get('estimated') else ''}{r.get('bike_min')} min"
            if r.get("bike_km"):
                t += f" ({r['bike_km']} km)"
            if r.get("transit_min") is not None:
                t += f"; public transport {r['transit_min']} min, {r.get('transfers', 0)} changes"
                if r.get("lines"):
                    t += f" via {', '.join(dict.fromkeys(r['lines']))}"
            if self._bike_train_better(r):
                t += f"; bike+train {r['bike_train_min']} min"
                if r.get("bike_to_station_min") is not None:
                    via = ", ".join(dict.fromkeys(r.get("bike_train_lines") or []))
                    t += f" (bike {r['bike_to_station_min']} min to the station" + (f", then {via})" if via else ")")
            t += f"; straight line {r.get('air_km')} km"
            lines.append(t)
        return "\n".join(lines)
