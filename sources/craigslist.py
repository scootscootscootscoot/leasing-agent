"""craigslist — the by-owner house rentals Redfin never sees.

Craigslist's current search API (`sapi.craigslist.org/web/v8`) answers plain
GETs with no captcha, but returns each posting as a *positional array* with a
delta-encoded id and date. The layout, confirmed against live postings:

    [0]  postingId  - offset from decode.minPostingId
    [1]  posted     - offset in seconds from decode.minPostedDate
    [2]  category
    [3]  price
    [4]  "acc:acc~lat~lon"
    [5]  image server prefix
    [6]  a signed index into decode.locationDescriptions - NOT decoded here.
         Checked against live postings and it does not resolve unambiguously
         (a post titled "Historic Hyde Park" indexes to "Central", and the
         field is sometimes a list rather than an int), so rather than label
         listings with a neighbourhood that might be wrong we drop it. The
         lat/lon and the anchor distances already say where a place is.
    [7:] tagged tuples, plus one bare string which is the title:
             (5, beds, sqft)   (6, url-slug)   (10, "$1,524")
             (4, ...photos)    (13, posting hash)

Anything unrecognised is ignored rather than guessed at, so a layout change
degrades to thinner records instead of wrong ones.
"""
from . import SourceError

NAME = "craigslist"
LABEL = "Craigslist"
NEEDS_KEY = False

SEARCH = ("https://sapi.craigslist.org/web/v8/postings/search/full"
          "?batch=12-0-360-0-0&cc=US&lang=en&searchPath=apa"
          "&min_price={pmin}&max_price={pmax}"
          "&min_bedrooms={bmin}&max_bedrooms={bmax}"
          "&lat={lat}&lon={lon}&search_distance={radius}")

# Craigslist housing_type codes that mean "not an apartment".
HOUSEY_WORDS = (("house", "house"), ("home", "house"), ("bungalow", "house"),
                ("cottage", "house"), ("duplex", "duplex"),
                ("townhome", "townhouse"), ("townhouse", "townhouse"),
                ("condo", "condo"))


def _prop_type(title: str) -> str:
    """Craigslist has no reliable structured property type in search results,
    so infer from the title and fall back to unknown (never to 'apartment' —
    a wrong 'apartment' would silently cost the house bonus)."""
    t = (title or "").lower()
    for word, kind in HOUSEY_WORDS:
        if word in t:
            return kind
    if "apartment" in t or "apt" in t:
        return "apartment"
    return "unknown"


def fetch(ctx) -> list:
    search = ctx.cfg["search"]
    anchor = ctx.cfg["anchors"][search["primary_anchor"]]

    url = SEARCH.format(pmin=search["budget_min"], pmax=search["budget_max"],
                        bmin=search["beds_min"], bmax=search["beds_max"],
                        lat=anchor["lat"], lon=anchor["lon"],
                        radius=search["radius_mi"])
    payload = ctx.get_json(url)
    data = payload.get("data")
    if not data:
        raise SourceError("no 'data' in search response")

    dec = data.get("decode") or {}
    min_id = dec.get("minPostingId", 0)
    host = "austin"
    try:
        host = dec["locations"][1][1]
    except (KeyError, IndexError, TypeError):
        pass

    out = []
    for item in data.get("items") or []:
        rec = _parse(item, min_id, host)
        if rec:
            out.append(rec)
    return out


def _parse(item, min_id, host):
    if not isinstance(item, list) or len(item) < 7:
        return None
    try:
        pid = min_id + item[0]
        price = item[3] if isinstance(item[3], int) else None

        lat = lon = None
        if isinstance(item[4], str) and "~" in item[4]:
            bits = item[4].split("~")
            if len(bits) >= 3:
                lat, lon = float(bits[1]), float(bits[2])

        title = slug = None
        beds = sqft = None
        for f in item[7:]:
            if isinstance(f, str):
                title = f
            elif isinstance(f, list) and f:
                if f[0] == 6 and len(f) > 1:
                    slug = f[1]
                elif f[0] == 5:
                    beds = f[1] if len(f) > 1 else None
                    sqft = f[2] if len(f) > 2 else None

        if not (pid and title):
            return None

        url = (f"https://{host}.craigslist.org/apa/d/{slug}/{pid}.html"
               if slug else f"https://{host}.craigslist.org/apa/{pid}.html")

        return {
            "source": NAME,
            "source_id": str(pid),
            "url": url,
            "title": title,
            # Craigslist gives no street address. Leaving `address` empty
            # matters: the deduper treats it as a unique street identity, so
            # anything vaguer than a street would collapse distinct houses.
            "address": None,
            "city": "Austin", "state": "TX", "zip": None,
            "lat": lat, "lon": lon,
            "price": price,
            "beds": float(beds) if isinstance(beds, (int, float)) else None,
            "baths": None,
            "sqft": int(sqft) if isinstance(sqft, (int, float)) and sqft else None,
            "prop_type": _prop_type(title),
            "property_name": None,
            "is_complex": False,
            "unit": None, "available": None,
            "description": None,
            "raw": {},
        }
    except (TypeError, ValueError, IndexError):
        return None
