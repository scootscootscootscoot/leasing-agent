"""redfin — the workhorse source.

Two calls per crawl tier:

  1. `/stingray/api/v1/search/rentals` returns every rental in a polygon, but
     a multi-unit property is summarised as *ranges* ("1-3 beds, $604-1838").
     Those ranges are useless for a 2BR budget question.
  2. `/stingray/api/v1/rentals/{id}/floorPlans` returns the actual unit types
     with real per-floorplan beds/baths/sqft/rent, which is what we score.

Step 2 only runs for properties whose bed range could contain a 2BR and whose
price range overlaps the budget, and its responses are cached in SQLite — that
keeps a crawl to a few dozen requests instead of a few hundred.

Worth knowing: Redfin's rental index ingests feeds from Zillow and RentPath
(visible in each record's `feedOriginalSource`), so a lot of what Zillow would
have given us arrives here anyway — which is why Zillow being captcha-walled
costs less than it looks.
"""
from concurrent.futures import ThreadPoolExecutor

from geo import bbox
from . import SourceError

NAME = "redfin"
LABEL = "Redfin"
NEEDS_KEY = False

SEARCH = ("https://www.redfin.com/stingray/api/v1/search/rentals"
          "?al=1&market=austin&num_homes={n}&poly={poly}&status=9"
          "&uipt=1,2,3,4&v=8")
FLOORPLANS = "https://www.redfin.com/stingray/api/v1/rentals/{rid}/floorPlans"
BASE = "https://www.redfin.com"

# homeData.propertyType, mapped to our vocabulary. Verified against live
# results: 6 is where the detached single-family rentals land, 5 is the
# apartment complexes, 13 is Redfin's catch-all.
PROP_TYPE = {1: "house", 2: "condo", 3: "townhouse", 4: "duplex",
             5: "apartment", 6: "house", 13: "unknown"}

FLOORPLAN_TTL = 6 * 3600


def _poly(box) -> str:
    """Redfin wants a closed 'lon lat,...' ring."""
    w, e, s, n = box["west"], box["east"], box["south"], box["north"]
    pts = [(w, s), (e, s), (e, n), (w, n), (w, s)]
    return ",".join(f"{x:.6f} {y:.6f}" for x, y in pts)


def _num(v):
    return v if isinstance(v, (int, float)) else None


def fetch(ctx) -> list:
    search = ctx.cfg["search"]
    anchor = ctx.cfg["anchors"][search["primary_anchor"]]
    box = bbox(anchor["lat"], anchor["lon"], search["radius_mi"])

    url = SEARCH.format(n=350, poly=_poly(box).replace(" ", "%20"))
    data = ctx.get_json(url, headers={"Referer": BASE + "/"})
    homes = data.get("homes")
    if homes is None:
        raise SourceError("no 'homes' in search response")

    simple, complexes = [], []
    for h in homes:
        home = h.get("homeData") or {}
        ext = h.get("rentalExtension") or {}
        addr = home.get("addressInfo") or {}
        cen = ((addr.get("centroid") or {}).get("centroid")) or {}

        beds = ext.get("bedRange") or {}
        price = ext.get("rentPriceRange") or {}
        base = {
            "source": NAME,
            "url": BASE + home["url"] if home.get("url") else None,
            "address": addr.get("formattedStreetLine"),
            "city": addr.get("city"), "state": addr.get("state"),
            "zip": addr.get("zip"),
            "lat": _num(cen.get("latitude")), "lon": _num(cen.get("longitude")),
            "prop_type": PROP_TYPE.get(home.get("propertyType"), "unknown"),
            "property_name": ext.get("propertyName"),
            "description": (ext.get("description") or "")[:600],
        }

        # A named property with a spread of bed counts is a complex: go get
        # its real floorplans. Everything else is already a single unit.
        spread = beds.get("min") != beds.get("max")
        if ext.get("rentalId") and (ext.get("propertyName") or spread):
            lo, hi = beds.get("min"), beds.get("max")
            pmin, pmax = price.get("min"), price.get("max")
            bed_ok = (lo is None or hi is None
                      or (lo <= search["beds_max"] and hi >= search["beds_min"]))
            price_ok = (pmin is None or pmax is None
                        or (pmin <= search["budget_max"]
                            and pmax >= search["budget_min"] * 0.6))
            if bed_ok and price_ok:
                complexes.append((ext["rentalId"], base, ext))
            continue

        simple.append({
            **base,
            "source_id": str(home.get("propertyId") or ext.get("rentalId")),
            "title": base["address"] or ext.get("propertyName"),
            "price": _num(price.get("min")),
            "beds": _num(beds.get("min")),
            "baths": _num((ext.get("bathRange") or {}).get("min")),
            "sqft": _num((ext.get("sqftRange") or {}).get("max")),
            "is_complex": False,
            "available": None,
            "raw": {"feed": ext.get("feedOriginalSource"),
                    "propertyType": home.get("propertyType")},
        })

    ctx.log.info("redfin: %d single units, %d complexes to expand",
                 len(simple), len(complexes))

    out = list(simple)
    if complexes:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for units in pool.map(lambda c: _expand(ctx, *c), complexes):
                out.extend(units)
    return out


def _expand(ctx, rental_id, base, ext) -> list:
    """One complex → one record per matching floorplan."""
    try:
        data = ctx.get_json(FLOORPLANS.format(rid=rental_id),
                            headers={"Referer": BASE + "/"},
                            cache_ttl=FLOORPLAN_TTL)
    except SourceError as e:
        ctx.log.debug("redfin floorplans %s: %s", rental_id, e)
        return []

    search = ctx.cfg["search"]
    seen, out = set(), []
    for group in data.get("unitTypesByBedroom") or []:
        for ut in group.get("availableUnitTypes") or []:
            uid = ut.get("unitTypeId")
            if not uid or uid in seen:
                continue          # "All" group repeats the per-bed groups
            seen.add(uid)

            beds = _num(ut.get("bedrooms"))
            if beds is None or not (search["beds_min"] <= beds <= search["beds_max"]):
                continue
            price = _num(ut.get("rentPriceMin"))
            if price is None:
                continue

            baths = (_num(ut.get("fullBaths")) or 0) + \
                    0.5 * (_num(ut.get("halfBaths")) or 0)
            out.append({
                **base,
                "source_id": f"{rental_id}:{uid}",
                "title": base.get("property_name") or base.get("address"),
                "price": int(price),
                "beds": beds,
                "baths": baths or None,
                "sqft": _num(ut.get("sqftMax")) or _num(ut.get("sqftMin")),
                "is_complex": True,
                "unit": ut.get("style") or ut.get("name"),
                "available": (ut.get("dateAvailable") or "")[:10] or None,
                "raw": {"feed": ext.get("feedOriginalSource"),
                        "availableUnits": ut.get("availableUnits"),
                        "amenities": (ut.get("specificAmenities") or [])[:8]},
            })
    return out
