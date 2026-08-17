"""sources — pluggable listing scrapers.

Each module exposes:

    NAME     short key, matches the config.json "sources" toggle
    LABEL    human name for the dashboard
    NEEDS_KEY  True if it only works through a paid scraping proxy
    fetch(ctx) -> list[dict]   normalised listing records

`fetch` may raise. The orchestrator catches per-source, records the error and
renders an error tile rather than failing the run — one blocked site must
never cost us the others.

Normalised record keys: source, source_id, url, title, address, city, state,
zip, lat, lon, price, beds, baths, sqft, prop_type, property_name, is_complex,
unit, available, photo, description, raw.

prop_type is one of: house, townhouse, duplex, condo, apartment, unknown.
"""
import gzip
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# A plain desktop UA. We are polite: low volume, cached, serial-ish, and we
# only touch endpoints the sites serve to ordinary browsers.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

SCRAPER_ENDPOINT = "https://api.scraperapi.com/"

_throttle = threading.Semaphore(4)
_last_hit = {}
_last_lock = threading.Lock()


class SourceError(RuntimeError):
    """Raised when a source cannot produce listings this run."""


class Ctx:
    """Everything a source needs: config, secrets, cache, logging."""

    def __init__(self, cfg, secrets, store, log):
        self.cfg = cfg
        self.secrets = secrets
        self.store = store
        self.log = log

    @property
    def scraper_key(self):
        return self.secrets.get("SCRAPER_API_KEY") or ""

    def get(self, url, headers=None, cache_ttl=0, via_proxy=False,
            min_gap=0.6, timeout=30):
        """GET a URL, optionally through the scraping proxy and the cache.

        `min_gap` spaces out consecutive requests to the same host so a crawl
        never looks like a hammering. `cache_ttl` seconds of 0 disables the
        SQLite response cache (used for the per-complex floorplan lookups,
        which dominate a Redfin crawl).
        """
        if cache_ttl:
            hit = self.store.cache_get(url, cache_ttl)
            if hit is not None:
                return hit

        target = url
        if via_proxy:
            if not self.scraper_key:
                raise SourceError(
                    "needs SCRAPER_API_KEY (see .env.example) — this site "
                    "blocks direct requests with a PerimeterX captcha")
            target = SCRAPER_ENDPOINT + "?" + urllib.parse.urlencode(
                {"api_key": self.scraper_key, "url": url,
                 "country_code": "us"})

        host = urllib.parse.urlsplit(target).netloc
        with _throttle:
            with _last_lock:
                wait = min_gap - (time.time() - _last_hit.get(host, 0))
            if wait > 0:
                time.sleep(wait + random.uniform(0, 0.25))
            with _last_lock:
                _last_hit[host] = time.time()

            h = {"User-Agent": UA, "Accept": "*/*",
                 "Accept-Language": "en-US,en;q=0.9",
                 "Accept-Encoding": "gzip"}
            h.update(headers or {})
            req = urllib.request.Request(target, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
            except urllib.error.HTTPError as e:
                detail = "blocked (captcha)" if e.code == 403 else e.reason
                raise SourceError(f"HTTP {e.code} — {detail}") from e
            except OSError as e:
                raise SourceError(f"network: {e}") from e

        text = body.decode("utf-8", "replace")
        if cache_ttl:
            self.store.cache_put(url, text)
        return text

    def get_json(self, url, **kw):
        text = self.get(url, **kw)
        # Redfin prefixes its stingray payloads with a JS-eval guard.
        if text.startswith("{}&&"):
            text = text[4:]
        try:
            return json.loads(text)
        except ValueError as e:
            raise SourceError(f"bad JSON ({text[:60]!r})") from e


def load(cfg) -> list:
    """Enabled source modules, in a stable order."""
    from . import craigslist, hotpads, instagram, redfin, zillow
    all_mods = [redfin, craigslist, zillow, hotpads, instagram]
    toggles = cfg.get("sources", {})
    return [m for m in all_mods if toggles.get(m.NAME)]


def all_modules() -> list:
    from . import craigslist, hotpads, instagram, redfin, zillow
    return [redfin, craigslist, zillow, hotpads, instagram]
