"""geo — distances and search boxes. Stdlib only (no SDK on purpose).

Everything here is crow-flies haversine. That is deliberately not walking
distance: for the MLK Jr Station anchor a straight line understates the walk
whenever I-35 or the rail corridor is in the way. Treat station distance as a
shortlist filter, then eyeball the actual route before touring.
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


def proximity_credit(dist_mi: float, full_mi: float, zero_mi: float) -> float:
    """1.0 inside `full_mi`, 0.0 beyond `zero_mi`, linear in between.

    Linear rather than exponential decay on purpose: the falloff is easy to
    read off the dashboard and easy to retune in config.json.
    """
    if dist_mi is None:
        return 0.0
    if dist_mi <= full_mi:
        return 1.0
    if dist_mi >= zero_mi or zero_mi <= full_mi:
        return 0.0
    return (zero_mi - dist_mi) / (zero_mi - full_mi)
