"""geo — distances and search boxes. Stdlib only (no SDK on purpose).

Everything here is crow-flies haversine, which around Mueller understates the
real walk wherever I-35, the rail corridor or the golf course is in the way.
That is why `walk.py` exists: it routes the anchor legs over the actual
pedestrian network and this module is the fallback for when that is
unavailable. Straight-line distance is still the right tool for the coarse
radius gate, which is all `bbox` and `within_radius` are used for.
"""
import math

EARTH_MI = 3958.7613


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * EARTH_MI * math.asin(math.sqrt(a))


def bbox(lat: float, lon: float, radius_mi: float) -> dict:
    """Lat/lon box enclosing the radius circle.

    Sources search by rectangle, so we over-fetch this box and let
    `within_radius` do the exact circular cut afterwards.
    """
    dlat = radius_mi / 69.0
    # Longitude degrees shrink with latitude; guard the poles so we never
    # divide by ~0 (Austin is nowhere near them, but this is cheap).
    denom = 69.0 * max(math.cos(math.radians(lat)), 0.01)
    dlon = radius_mi / denom
    return {"south": lat - dlat, "north": lat + dlat,
            "west": lon - dlon, "east": lon + dlon}


def within_radius(lat, lon, anchor_lat, anchor_lon, radius_mi) -> bool:
    if lat is None or lon is None:
        return False
    return haversine_mi(lat, lon, anchor_lat, anchor_lon) <= radius_mi


def anchor_distances(lat, lon, anchors: dict) -> dict:
    """{anchor_key: miles} for every configured anchor; {} if unlocated."""
    if lat is None or lon is None:
        return {}
    return {k: round(haversine_mi(lat, lon, a["lat"], a["lon"]), 2)
            for k, a in anchors.items()}


def dist_mi(distances: dict, key: str):
    """Miles to one anchor, tolerating both stored shapes.

    Distances used to be `{anchor: miles}` and are now
    `{anchor: {"mi": ..., "basis": ...}}`. Rows written before walk routing
    existed are still in the database, so every reader goes through here
    rather than assuming a shape.
    """
    entry = (distances or {}).get(key)
    if isinstance(entry, dict):
        return entry.get("mi")
    return entry


def walk_min(distances: dict, key: str):
    """Routed walking minutes to one anchor, or None if never routed."""
    entry = (distances or {}).get(key)
    return entry.get("walk_min") if isinstance(entry, dict) else None


def proximity_credit(miles: float, full_mi: float, zero_mi: float) -> float:
    """1.0 inside `full_mi`, 0.0 beyond `zero_mi`, linear in between.

    Linear rather than exponential decay on purpose: the falloff is easy to
    read off the dashboard and easy to retune in config.json.
    """
    if miles is None:
        return 0.0
    if miles <= full_mi:
        return 1.0
    if miles >= zero_mi or zero_mi <= full_mi:
        return 0.0
    return (zero_mi - miles) / (zero_mi - full_mi)
