"""walk — walkability: routed pedestrian distance and amenity density.

This module exists because `geo.py` is crow-flies haversine, and around
Mueller crow-flies lies. I-35, the rail corridor, Airport Blvd and the golf
course all mean two points 0.6 mi apart on a map can be a 1.9 mi walk. A
listing that scores well on straight-line distance to MLK Jr Station may be
a genuinely unpleasant walk from it.

Two independent measures, from two free keyless services:

  routed anchor walks   OSRM's public foot profile. Real pedestrian routing
                        over the OSM network, so the I-35 detour is counted.
                        One `table` request covers every listing against
                        every anchor, which is why this is affordable at all.

  amenity density       Overpass. Counts what you can actually walk to —
                        groceries, food, parks, transit, schools, pharmacy —
                        with distance decay and diminishing returns per
                        category, the way Walk Score works.

Deliberate asymmetry worth knowing: anchors get true routed distances,
amenities do not. Routing every listing to all ~1,800 nearby amenities would
be hundreds of thousands of route legs per crawl. Amenity distance is
haversine inflated by DETOUR_FACTOR, which is a decent estimator in
aggregate and does not pretend to be more.

Everything is cached in SQLite. Street networks and grocery stores do not
move, so the cache TTL is measured in weeks and a crawl normally makes zero
network calls here.
"""
import json
import urllib.parse

from geo import haversine_mi
from sources import SourceError

OSRM = "https://routing.openstreetmap.de/routed-foot"

# Overpass answers 406 to a browser User-Agent and to urllib's default one:
# the OSM projects require a request to identify the application making it,
# and the shared browser UA in sources/__init__.py is exactly what they
# refuse. This is not optional politeness — without it every call fails.
WALK_UA = ("leasing-agent/1.0 "
           "(+https://github.com/scootscootscootscoot/leasing-agent)")
WALK_HEADERS = {"User-Agent": WALK_UA}

# Two independent instances. The public Overpass servers are volunteer-run
# and time out under load often enough that a single endpoint would mean
# regularly losing walkability for a whole crawl.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS = OVERPASS_MIRRORS[0]      # the cache key; mirrors share it

# Median real-walk / straight-line ratio for a gridded US city. Used only for
# amenity distances, never for the anchors (those are routed for real).
DETOUR_FACTOR = 1.35

METERS_PER_MI = 1609.344
AMENITY_TTL = 30 * 86400      # a month; shops open and close slowly
ROUTE_TTL = 90 * 86400        # a quarter; the street network barely moves
OSRM_BATCH = 90               # coordinates per table request, incl. anchors

# Walk Score's insight is that the *second* grocery store is worth much less
# than the first, and the tenth restaurant is worth almost nothing. Each
# category lists per-slot weights: the nearest match takes the first slot,
# the next the second, and so on. Totals are relative, not absolute.
CATEGORIES = {
    "grocery": {
        "label": "groceries",
        "slots": [3.0, 1.0],
        "match": [("shop", {"supermarket", "grocery", "greengrocer",
                            "convenience"})],
    },
    "food": {
        "label": "food & drink",
        "slots": [1.0, 0.8, 0.6, 0.4, 0.3, 0.2],
        "match": [("amenity", {"restaurant", "cafe", "fast_food", "bar",
                               "pub"})],
    },
    "transit": {
        "label": "transit",
        "slots": [2.0, 1.0, 0.5],
        "match": [("highway", {"bus_stop"}),
                  ("railway", {"station", "halt", "tram_stop"})],
    },
    "park": {
        "label": "parks",
        "slots": [1.5, 0.7],
        "match": [("leisure", {"park", "playground", "garden",
                               "fitness_centre"})],
    },
    "errands": {
        "label": "errands",
        "slots": [1.0, 0.6, 0.3],
        "match": [("amenity", {"pharmacy", "bank", "post_office", "library"}),
                  ("shop", {"bakery", "hardware", "doityourself", "laundry"})],
    },
    "school": {
        "label": "schools",
        "slots": [1.0],
        "match": [("amenity", {"school", "kindergarten"})],
    },
}

# Full credit inside FULL_MI, nothing past ZERO_MI, linear between — the same
# shape as the anchor falloff so the two read consistently on the dashboard.
FULL_MI = 0.25
ZERO_MI = 1.25

MAX_POINTS = sum(sum(c["slots"]) for c in CATEGORIES.values())


def _bbox_query(box: dict) -> str:
    """Overpass QL for every amenity category inside a bounding box.

    `nwr` + `out center` rather than `node` + `out body`: parks and schools
    are usually ways or relations, and querying only nodes silently loses
    most of them — which is exactly the kind of quiet undercount that would
    make the whole score wrong without ever erroring.
    """
    b = f"{box['south']:.5f},{box['west']:.5f},{box['north']:.5f},{box['east']:.5f}"
    clauses = []
    for cat in CATEGORIES.values():
        for key, values in cat["match"]:
            alt = "|".join(sorted(values))
            clauses.append(f'nwr["{key}"~"^({alt})$"]({b});')
    return f'[out:json][timeout:90];({"".join(clauses)});out center 4000;'


def _classify(tags: dict) -> str | None:
    """Which category an OSM element belongs to, or None.

    First match wins, and CATEGORIES is ordered so the more specific
    categories are tested before the broad ones.
    """
    for name, cat in CATEGORIES.items():
        for key, values in cat["match"]:
            if tags.get(key) in values:
                return name
    return None


def fetch_amenities(ctx, box: dict) -> list:
    """[(category, lat, lon)] for the search box. Cached for a month."""
    query = _bbox_query(box)
    url = f"{OVERPASS}?{urllib.parse.urlencode({'data': query})}"

    cached = ctx.store.cache_get(url, AMENITY_TTL)
    if cached is not None:
        return json.loads(cached)

    # Overpass wants the query in the body; the URL above is the cache key.
    body = urllib.parse.urlencode({"data": query}).encode()
    headers = dict(WALK_HEADERS,
                   **{"Content-Type": "application/x-www-form-urlencoded"})

    last = None
    elements = None
    for mirror in OVERPASS_MIRRORS:
        try:
            raw = ctx.get(mirror, cache_ttl=0, min_gap=1.0, timeout=120,
                          data=body, headers=headers)
            elements = json.loads(raw).get("elements", [])
            break
        except (SourceError, ValueError) as e:
            last = e
            ctx.log.info(f"walk: {mirror.split('/')[2]} unavailable ({e}) — "
                         "trying the next mirror")
    if elements is None:
        raise SourceError(f"every overpass mirror failed, last: {last}")

    out = []
    for el in elements:
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        cat = _classify(el.get("tags") or {})
        if cat and lat is not None and lon is not None:
            out.append([cat, lat, lon])

    ctx.store.cache_put(url, json.dumps(out))
    ctx.log.info(f"walk: {len(out)} amenities cached for the search box")
    return out


def amenity_score(lat, lon, amenities: list) -> tuple:
    """(0..1 credit, {category: reachable count}) for one point.

    Amenities are bucketed by category, sorted by estimated walk distance,
    and paid out against that category's slot weights.
    """
    if lat is None or lon is None or not amenities:
        return 0.0, {}

    by_cat = {}
    for cat, alat, alon in amenities:
        # Cheap rejection before the trig: 0.02 deg is ~1.4 mi, comfortably
        # past ZERO_MI, and this runs ~1,800 times per listing.
        if abs(alat - lat) > 0.03 or abs(alon - lon) > 0.03:
            continue
        d = haversine_mi(lat, lon, alat, alon) * DETOUR_FACTOR
        if d < ZERO_MI:
            by_cat.setdefault(cat, []).append(d)

    earned = 0.0
    counts = {}
    for name, cat in CATEGORIES.items():
        dists = sorted(by_cat.get(name, []))
        if not dists:
            continue
        counts[name] = len(dists)
        for slot, dist in zip(cat["slots"], dists):
            if dist <= FULL_MI:
                earned += slot
            else:
                earned += slot * (ZERO_MI - dist) / (ZERO_MI - FULL_MI)

    return min(earned / MAX_POINTS, 1.0), counts


def route_walks(ctx, points: list, anchors: dict, max_new=None) -> dict:
    """{(lat, lon): {anchor_key: {"mi": float, "min": float}}} via OSRM.

    One `table` request per batch rather than one route per listing-anchor
    pair. Returns {} on any failure — walk routing is an enhancement, and a
    flaky public router must never take a crawl down with it.

    `max_new` caps how many *uncached* points are fetched, not how many are
    returned. Capping the total instead would mean the same popular points
    were re-served from cache every run while the tail was never routed at
    all; this way each run advances through the backlog and the corpus fills
    in over a few cycles.
    """
    keys = list(anchors)
    if not points or not keys:
        return {}

    anchor_coords = [(anchors[k]["lat"], anchors[k]["lon"]) for k in keys]
    out = {}
    todo = []

    for lat, lon in points:
        hit = ctx.store.cache_get(_route_key(lat, lon), ROUTE_TTL)
        if hit is not None:
            out[(lat, lon)] = json.loads(hit)
        else:
            todo.append((lat, lon))

    if max_new is not None and len(todo) > max_new:
        ctx.log.info(f"walk: {len(todo)} unrouted points, doing {max_new} "
                     "this run — the rest follow next crawl")
        todo = todo[:max_new]

    room = OSRM_BATCH - len(anchor_coords)
    for i in range(0, len(todo), room):
        batch = todo[i:i + room]
        try:
            fetched = _osrm_table(ctx, batch, anchor_coords, keys)
        except (SourceError, ValueError, KeyError, TypeError) as e:
            ctx.log.info(f"walk: routing batch failed ({e}) — falling back to "
                    "crow-flies for these")
            continue
        for point, walks in fetched.items():
            out[point] = walks
            ctx.store.cache_put(_route_key(*point), json.dumps(walks))

    return out


def _route_key(lat, lon) -> str:
    # ~11 m of precision: two units in the same building share a route.
    return f"walkroute:{lat:.4f},{lon:.4f}"


def _osrm_table(ctx, batch: list, anchor_coords: list, keys: list) -> dict:
    """One OSRM table request: `batch` sources, `anchor_coords` destinations."""
    coords = list(batch) + list(anchor_coords)
    path = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
    sources = ";".join(str(i) for i in range(len(batch)))
    dests = ";".join(str(i) for i in range(len(batch), len(coords)))
    url = (f"{OSRM}/table/v1/foot/{path}?sources={sources}"
           f"&destinations={dests}&annotations=distance,duration")

    data = ctx.get_json(url, min_gap=1.2, timeout=90, headers=WALK_HEADERS)
    if data.get("code") != "Ok":
        raise SourceError(f"osrm: {data.get('code')} {data.get('message', '')}")

    distances = data.get("distances") or []
    durations = data.get("durations") or []
    out = {}
    for row, point in enumerate(batch):
        walks = {}
        for col, key in enumerate(keys):
            metres = _cell(distances, row, col)
            seconds = _cell(durations, row, col)
            if metres is None:
                continue          # unrouteable: caller falls back to haversine
            walks[key] = {"mi": round(metres / METERS_PER_MI, 2),
                          "min": round((seconds or 0) / 60.0, 1)}
        if walks:
            out[point] = walks
    return out


def _cell(matrix, row, col):
    """matrix[row][col], or None if absent — OSRM nulls unreachable pairs."""
    try:
        value = matrix[row][col]
    except (IndexError, TypeError):
        return None
    return None if value is None else float(value)


def describe(credit) -> str:
    """A short human label for a walkability credit, for the dashboard."""
    if credit is None:
        return "unscored"
    if credit >= 0.70:
        return "very walkable"
    if credit >= 0.45:
        return "walkable"
    if credit >= 0.25:
        return "some errands on foot"
    return "car needed"
