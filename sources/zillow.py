"""zillow — behind PerimeterX, so it only runs through a scraping proxy.

Direct requests to Zillow (both the HTML pages and the
`async-create-search-page-state` XHR) return 403 with a `px-captcha` body from
every datacenter IP, which is why this source is disabled by default and flips
on only once SCRAPER_API_KEY is set.

Because the proxy fetches pages as a browser would, we parse the server-side
`__NEXT_DATA__` blob rather than the XHR. The walk for `listResults` is a
recursive search instead of a fixed path — Zillow reshuffles the wrapper keys
regularly, but that array's own shape has been stable.

NOTE: written against Zillow's documented page structure but *not* verified
end-to-end here, because verifying it requires a paid proxy key. Expect to
adjust `_walk`'s field names on first live run; `/sources` in the bot will
show you the error if it does not line up.
"""
import json
import re
import urllib.parse

from geo import bbox
from . import SourceError

NAME = "zillow"
LABEL = "Zillow"
NEEDS_KEY = True

NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.S)


def fetch(ctx) -> list:
    search = ctx.cfg["search"]
    anchor = ctx.cfg["anchors"][search["primary_anchor"]]
    box = bbox(anchor["lat"], anchor["lon"], search["radius_mi"])

    qs = {
        "pagination": {},
        "isMapVisible": True,
        "mapBounds": {"west": box["west"], "east": box["east"],
                      "south": box["south"], "north": box["north"]},
        "filterState": {
            "price": {"min": search["budget_min"], "max": search["budget_max"]},
            "beds": {"min": search["beds_min"], "max": search["beds_max"]},
            "fr": {"value": True},    # for rent
            "fsba": {"value": False}, "fsbo": {"value": False},
            "nc": {"value": False}, "cmsn": {"value": False},
            "auc": {"value": False}, "fore": {"value": False},
        },
        "isListVisible": True,
    }
    url = ("https://www.zillow.com/homes/for_rent/?searchQueryState="
           + urllib.parse.quote(json.dumps(qs, separators=(",", ":"))))

    html = ctx.get(url, via_proxy=True, timeout=90,
                   headers={"Accept": "text/html,application/xhtml+xml"})
    m = NEXT_DATA.search(html)
    if not m:
        raise SourceError("no __NEXT_DATA__ in page (captcha or layout change)")
    try:
        blob = json.loads(m.group(1))
    except ValueError as e:
        raise SourceError(f"bad __NEXT_DATA__ JSON: {e}") from e

    results = _walk(blob)
    if results is None:
        raise SourceError("no listResults found in page state")

    out = []
    for r in results:
        rec = _parse(r)
        if rec:
            out.append(rec)
    return out


def _walk(node, depth=0):
    """Depth-first hunt for the listResults array."""
    if depth > 12:
        return None
    if isinstance(node, dict):
        got = node.get("listResults")
        if isinstance(got, list) and got:
            return got
        for v in node.values():
            found = _walk(v, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for v in node[:40]:
            found = _walk(v, depth + 1)
            if found:
                return found
    return None


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _parse(r):
    if not isinstance(r, dict):
        return None
    zpid = r.get("zpid") or r.get("id")
    if not zpid:
        return None
    info = r.get("hdpData", {}).get("homeInfo", {}) if isinstance(
        r.get("hdpData"), dict) else {}
    ll = r.get("latLong") or {}

    detail = r.get("detailUrl") or ""
    if detail and not detail.startswith("http"):
        detail = "https://www.zillow.com" + detail

    home_type = (info.get("homeType") or r.get("propertyType") or "").lower()
    prop_type = {
        "single_family": "house", "townhouse": "townhouse",
        "condo": "condo", "multi_family": "duplex",
        "apartment": "apartment", "manufactured": "house",
    }.get(home_type, "unknown")

    return {
        "source": NAME,
        "source_id": str(zpid),
        "url": detail or None,
        "title": r.get("address") or r.get("statusText"),
        "address": r.get("addressStreet") or r.get("address"),
        "city": r.get("addressCity") or info.get("city"),
        "state": r.get("addressState") or info.get("state"),
        "zip": r.get("addressZipcode") or info.get("zipcode"),
        "lat": _num(ll.get("latitude")) or _num(info.get("latitude")),
        "lon": _num(ll.get("longitude")) or _num(info.get("longitude")),
        "price": _num(r.get("unformattedPrice")) or _num(info.get("price")),
        "beds": _num(r.get("beds")) or _num(info.get("bedrooms")),
        "baths": _num(r.get("baths")) or _num(info.get("bathrooms")),
        "sqft": _num(r.get("area")) or _num(info.get("livingArea")),
        "prop_type": prop_type,
        "property_name": None,
        "is_complex": bool(r.get("isBuilding")),
        "unit": r.get("unitCount") and f"{r['unitCount']} units" or None,
        "available": None,
        "photo": r.get("imgSrc"),
        "description": None,
        "raw": {"statusType": r.get("statusType")},
    }
