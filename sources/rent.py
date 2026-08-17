"""rent.com — RentPath's own site, and the best-behaved source we have.

Zillow and its stable (Trulia, HotPads) sit behind PerimeterX, and
apartments.com and homes.com return a flat Access Denied. rent.com serves an
ordinary Next.js page whose `__NEXT_DATA__` blob carries the entire result
set: coordinates, per-floorplan beds/baths/sqft/rent, and availability. No
key, no proxy, no second request per property.

Two things make it cheap to crawl politely:

  neighbourhood paths   `/texas/austin-apartments/mueller-neighborhood`
                        returns 113 properties where the city-wide path
                        returns 4,136. We crawl a handful of neighbourhoods
                        around the anchors instead of paginating Austin.

  inline floor plans    `floorPlans[]` gives real per-bedroom pricing in the
                        same response, so unlike Redfin there is no
                        follow-up request to turn "1-3 beds, $604-1838" into
                        an answerable 2BR price.

The neighbourhood list is discovered, not hardcoded: each page carries
`seoLinks.nearby.neighborhoods` with a distance in miles from the one you
asked for, so seeding with Mueller and keeping everything inside the search
radius finds the rest by itself. That means a renamed or new neighbourhood
is picked up without a code change.

URL grammar, confirmed against the live site:

    /texas/austin-{category}/{hood}-neighborhood/{filters}/page-{n}
    category  apartments | houses | condos | townhouses
    filters   2-bedroom_min-price-1500_max-price-3000   (underscore-joined)
"""
import json
import re
import time

from . import SourceError

NAME = "rent"
LABEL = "Rent.com"
NEEDS_KEY = False

BASE = "https://www.rent.com"
# Houses first: they are the point of the exercise and the category is small.
CATEGORIES = ["houses", "townhouses", "condos", "apartments"]

NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S)

# rent.com's own taxonomy → ours.
PROP_TYPE = {
    "HOUSE": "house", "HOUSES": "house", "SINGLE_FAMILY": "house",
    "TOWNHOME": "townhouse", "TOWNHOUSE": "townhouse", "TOWNHOMES": "townhouse",
    "CONDO": "condo", "CONDOS": "condo",
    "DUPLEX": "duplex",
    "APARTMENT": "apartment", "APARTMENTS": "apartment",
}

PAGE_TTL = 20 * 60          # a crawl runs every 90 min; this only helps retries
HOOD_TTL = 7 * 86400        # the neighbourhood graph is near-static
MAX_PAGES = 4               # 30/page; deeper than this is out of the radius
SEED_HOODS = ["mueller"]    # everything else is discovered from here

# Seconds to wait before re-asking after a throttled (202, empty) response.
# Measured behaviour: the throttle is intermittent rather than a cooldown —
# a request often draws a 202 and the very next one succeeds — so these are
# short and there are several. The last entry is never slept on.
RETRY_PAUSES = [2, 5, 10, 0]

# Seconds between requests. Higher than the other sources because rent.com
# starts answering 202-with-no-body if you go faster; ~70 requests at this
# spacing is about three minutes, which is nothing on a 90-minute cycle.
MIN_GAP = 2.5

# If this many requests fail back to back, stop asking for the rest of the
# run. Bounds the worst case: without it a sustained block turns a crawl into
# an hour of retrying, and the other sources wait behind it.
CIRCUIT_BREAK = 8


def fetch(ctx):
    hoods = _neighbourhoods(ctx)
    ctx.log.info(f"rent: {len(hoods)} neighbourhoods in radius — {', '.join(hoods)}")

    out, errors, attempts = [], [], 0
    consecutive = 0
    for hood in hoods:
        if consecutive >= CIRCUIT_BREAK:
            break
        for category in CATEGORIES:
            for page in range(1, MAX_PAGES + 1):
                attempts += 1
                try:
                    listings, total = _page(ctx, category, hood, page)
                except SourceError as e:
                    # A 404 is a legitimate answer here: not every
                    # neighbourhood has houses, and asking is how we find out.
                    if "404" not in str(e):
                        errors.append(f"{hood}/{category}: {e}")
                        consecutive += 1
                    break
                consecutive = 0
                out.extend(listings)
                if page * 30 >= total or not listings:
                    break
            if consecutive >= CIRCUIT_BREAK:
                ctx.log.info(f"rent: {consecutive} failures in a row — "
                             "stopping early, keeping what we have")
                break

    if not out and errors:
        raise SourceError(f"every request failed, e.g. {errors[0]}")
    if errors:
        ctx.log.info(f"rent: {len(errors)}/{attempts} requests failed, kept going")
    return _dedupe(out)


def _filters(cfg) -> str:
    """The underscore-joined filter segment of the path."""
    search = cfg["search"]
    parts = [f"{int(search['beds_min'])}-bedroom"]
    if search.get("budget_min"):
        parts.append(f"min-price-{int(search['budget_min'])}")
    if search.get("budget_max"):
        parts.append(f"max-price-{int(search['budget_max'])}")
    return "_".join(parts)


def _neighbourhoods(ctx) -> list:
    """Neighbourhood slugs within the search radius, discovered from a seed.

    Cached for a week. Falls back to the seed alone if discovery fails, so a
    bad response costs coverage rather than the whole source.
    """
    radius = ctx.cfg["search"].get("radius_mi", 3.0)
    key = f"rent:hoods:{radius}"
    cached = ctx.store.cache_get(key, HOOD_TTL)
    if cached is not None:
        return json.loads(cached)

    found = dict.fromkeys(SEED_HOODS, 0.0)
    for seed in SEED_HOODS:
        try:
            data = _next_data(ctx, f"{BASE}/texas/austin-apartments/"
                                   f"{seed}-neighborhood")
        except SourceError as e:
            ctx.log.info(f"rent: neighbourhood discovery failed ({e})")
            continue
        location = _dig(data, "props", "pageProps", "pageData", "location") or {}
        nearby = _dig(location, "seoLinks", "nearby", "neighborhoods") or []
        for entry in nearby:
            miles = entry.get("distanceInMiles")
            slug = _slug(entry.get("url"))
            if slug and miles is not None and miles <= radius:
                found.setdefault(slug, round(miles, 2))

    hoods = sorted(found, key=lambda h: found[h])
    ctx.store.cache_put(key, json.dumps(hoods))
    return hoods


def _slug(url):
    """'/texas/austin-apartments/hyde-park-neighborhood' → 'hyde-park'."""
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-len("-neighborhood")] if tail.endswith("-neighborhood") else None


def _page(ctx, category, hood, page):
    """One result page → ([records], total_for_this_query)."""
    url = f"{BASE}/texas/austin-{category}/{hood}-neighborhood/{_filters(ctx.cfg)}"
    if page > 1:
        url += f"/page-{page}"

    search = _dig(_next_data(ctx, url), "props", "pageProps", "pageData",
                  "location", "listingSearch")
    if not search:
        return [], 0

    records = []
    for item in search.get("listings") or []:
        records.extend(_parse(item, category))
    return records, search.get("total") or 0


def _next_data(ctx, url):
    """Fetch a page and return its parsed `__NEXT_DATA__`.

    rent.com soft-throttles by answering `202 Accepted` with a zero-length
    body instead of a 4xx — a success as far as urllib is concerned, and a
    page with no data as far as we are concerned. Backing off and asking
    again clears it; a bare retry loop is the whole fix, but without it a
    crawl silently loses a fifth of its requests.
    """
    cached = ctx.store.cache_get(url, PAGE_TTL)
    if cached is not None:
        return json.loads(cached)

    last = ""
    for attempt, pause in enumerate(RETRY_PAUSES, start=1):
        # cache_ttl=0 throughout: only a *parsed* page is worth caching, and
        # letting ctx.get store the response would cache throttle bodies too,
        # turning one 202 into twenty minutes of empty results.
        html = ctx.get(url, cache_ttl=0, min_gap=MIN_GAP, timeout=45,
                       headers={"Accept": "text/html,application/xhtml+xml"})
        match = NEXT_DATA.search(html)
        if match:
            try:
                data = json.loads(match.group(1))
            except ValueError as e:
                raise SourceError(f"__NEXT_DATA__ is not JSON: {e}") from e
            ctx.store.cache_put(url, json.dumps(_trim(data)))
            return data

        last = ("throttled (empty body)" if len(html.strip()) < 200 else
                "no __NEXT_DATA__ in the page (layout changed?)")
        if attempt < len(RETRY_PAUSES):
            time.sleep(pause)

    raise SourceError(last)


def _trim(data):
    """Keep only the branch we read before caching.

    A rent.com page's `__NEXT_DATA__` is ~600 KB, almost all of it SEO link
    farms and page chrome. Caching the whole thing would bloat the SQLite
    file by hundreds of megabytes over a few days of crawling.
    """
    location = _dig(data, "props", "pageProps", "pageData", "location") or {}
    return {"props": {"pageProps": {"pageData": {"location": {
        "listingSearch": location.get("listingSearch"),
        "seoLinks": {"nearby": {"neighborhoods": _dig(
            location, "seoLinks", "nearby", "neighborhoods") or []}},
    }}}}}


def _parse(item, category):
    """One property → one record per matching floor plan.

    A property with no usable floor plans still yields a single record from
    its summary ranges, flagged `is_complex`, so a promising address is never
    dropped just because rent.com withheld the unit breakdown.
    """
    listing_id = item.get("id")
    if not listing_id:
        return []

    location = item.get("location") or {}
    lat, lon = location.get("lat"), location.get("lng")
    name = item.get("name")
    path = item.get("urlPathname") or ""
    prop_type = PROP_TYPE.get((item.get("propertyType") or "").upper()) \
        or _type_from_category(category)

    base = {
        "source": NAME,
        "url": BASE + path if path else BASE,
        "title": name,
        "address": item.get("address"),
        "city": location.get("city") or "Austin",
        "state": location.get("stateAbbr") or "TX",
        "zip": item.get("zipCode") or location.get("zip"),
        "lat": lat, "lon": lon,
        "prop_type": prop_type,
        "property_name": name,
        "photo": _photo(item),
        "description": item.get("addressFull"),
    }

    plans = [p for p in (item.get("floorPlans") or []) if _usable(p)]
    if not plans:
        price_range = item.get("priceRange") or {}
        bed_range = item.get("bedRange") or {}
        return [dict(base,
                     source_id=str(listing_id),
                     price=price_range.get("min"),
                     beds=bed_range.get("min"),
                     baths=None, sqft=None,
                     is_complex=True,
                     unit=None,
                     available=item.get("availabilityStatus"),
                     raw={"summary_only": True, "id": listing_id})]

    out = []
    for index, plan in enumerate(plans):
        price = (plan.get("priceRange") or {}).get("min")
        sqft = (plan.get("sqFtRange") or {}).get("min")
        beds = plan.get("bedCount")
        out.append(dict(
            base,
            # Floor plans carry no stable id of their own, so the position
            # within the property stands in. It is stable run to run for an
            # unchanged property, which is all upsert needs.
            source_id=f"{listing_id}:{beds}b{index}",
            price=price,
            beds=beds,
            baths=plan.get("bathCount"),
            sqft=sqft,
            is_complex=bool(item.get("floorPlans") and len(plans) > 1),
            unit=_plan_name(plan, beds),
            available=plan.get("availableDate") or item.get("availabilityStatus"),
            raw={"id": listing_id, "plan": index,
                 "available_count": plan.get("availableCount")},
        ))
    return out


def _usable(plan) -> bool:
    return bool(plan and plan.get("bedCount") is not None
                and (plan.get("priceRange") or {}).get("min"))


def _plan_name(plan, beds):
    """A display label for the floor plan, e.g. '2bd/1ba'.

    Purely derived from beds and baths — rent.com does not name its floor
    plans the way Redfin does. It is a label for humans, not an identifier,
    which is why `crawler.dedupe_key` matches on beds/baths/sqft/price rather
    than on this string.
    """
    baths = plan.get("bathCount")
    if beds is None:
        return None
    bed_label = "studio" if not beds else f"{int(beds)}bd"
    return f"{bed_label}/{baths:g}ba" if baths else bed_label


def _type_from_category(category):
    return {"houses": "house", "townhouses": "townhouse",
            "condos": "condo", "apartments": "apartment"}.get(category,
                                                              "unknown")


def _photo(item):
    for photo in item.get("optimizedPhotos") or []:
        for size in ("large", "medium", "small", "url"):
            if photo.get(size):
                return photo[size]
    return None


def _dedupe(records):
    """The same property appears under several categories and neighbourhoods.

    Neighbourhood boundaries overlap and a townhouse is listed under both
    `townhouses` and `apartments`, so the raw list has heavy repetition.
    Collapsing here keeps the crawler's cross-source dedupe honest about how
    much rent.com actually contributed.
    """
    seen = {}
    for record in records:
        seen.setdefault(record["source_id"], record)
    return list(seen.values())


def _dig(obj, *keys):
    """Nested lookup that returns None instead of raising on any miss."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj
