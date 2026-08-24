"""score — rank a listing against what we actually want.

The brief: a 2-bedroom near Mueller, $1500-3000, genuinely walkable to MLK Jr
Station, and ideally a house with more space than the Mueller apartment
comparables (The Platform, Starlight). That maps to six weighted components,
all of them 0..1 before weighting so the config weights stay readable:

    price        cheaper inside the budget band scores higher
    space        square footage between the config floor and ceiling
    walk         density of what you can reach on foot (see walk.py)
    mueller      proximity credit to the Mueller anchor
    mlk_station  proximity credit to the rail station
    house_bonus  full credit for anything that is not an apartment complex

Weights live in config.json. They do not need to sum to 100 — the result is
normalised by the total — so you can bump one without rebalancing the rest.

Anchor proximity prefers the *routed walking* distance from walk.py when the
crawler managed to fetch one, and falls back to crow-flies otherwise. Those
are different numbers around here — I-35 and the rail corridor turn short
straight lines into long walks — so `dists` records which basis was used.
"""
from datetime import datetime, timedelta, timezone

from geo import anchor_distances, proximity_credit
from sqft import space_credit

# Property types that count as "more space than an apartment" for the bonus.
HOUSEY = {"house", "townhouse", "duplex", "condo"}

# How the dashboard and the bot bucket listings into sections. These are a
# presentation split, not a scoring one — everything in HOUSEY still earns the
# same house_bonus regardless of which bucket it is shown under.
#
# Townhomes get their own bucket rather than being folded in with the houses.
# There are enough of them around Mueller to read on their own, and inside a
# combined section they were being buried: the best townhome scores in the
# low 60s, under every house and duplex above it, so it never appeared.
# `unknown` deliberately belongs to no bucket — it still shows in Best matches.
HOUSE_TYPES = ("house", "duplex", "condo")
TOWNHOUSE_TYPES = ("townhouse",)
APARTMENT_TYPES = ("apartment",)

# Condition and amenity signals, learned from the first round of dashboard
# feedback: every "old / outdated / vintage" dislike was invisible to the
# weighted components, and "really nice but not pool" showed amenities were
# too. Keywords are matched against title + description, case-insensitive.
# Positive and negative cancel within a group, so "renovated 1950s kitchen"
# nets out instead of double-counting the era.
CONDITION_POS = ("renovated", "remodeled", "updated", "newly", "modern",
                 "upgraded", "gut rehab", "brand new")
CONDITION_NEG = ("vintage", "dated", "outdated", "original condition",
                 "as-is", "as is", "fixer", "unupdated", "old charm",
                 "charming 19", "1940s", "1950s", "1960s", "1970s")
AMENITY_POS = ("pool", "in-unit laundry", "w/d in unit", "washer/dryer in unit",
               "washer and dryer in unit", "gym", "fitness center")


def _keyword_hits(text, words):
    t = (text or "").lower()
    return sum(1 for w in words if w in t)


def condition_credit(rec) -> float:
    """1.0 renovated, 0.0 visibly dated, 0.5 no signal either way."""
    text = " ".join(str(rec.get(k) or "") for k in ("title", "description"))
    pos = _keyword_hits(text, CONDITION_POS)
    neg = _keyword_hits(text, CONDITION_NEG)
    if pos > neg:
        return 1.0
    if neg > pos:
        return 0.0
    return 0.5


def amenity_credit(rec) -> float:
    """1.0 when a wanted amenity is mentioned, 0.5 when nothing is said.

    Absence of the word "pool" is not proof of absence of a pool, so the
    neutral case sits at 0.5 rather than 0 — this nudges rather than gates.
    """
    text = " ".join(str(rec.get(k) or "") for k in ("title", "description"))
    return 1.0 if _keyword_hits(text, AMENITY_POS) else 0.5


def price_credit(price, budget_min, budget_max) -> float:
    """1.0 at or below budget_min, 0.0 at budget_max, linear between.

    Above budget_max scores 0 rather than negative — such listings are
    normally filtered out before scoring, so this only guards stragglers.
    """
    if not price:
        return 0.0
    if price <= budget_min:
        return 1.0
    if price >= budget_max or budget_max <= budget_min:
        return 0.0
    return (budget_max - price) / (budget_max - budget_min)


def score_listing(rec: dict, cfg: dict) -> tuple:
    """Return (score 0-100, {component: credit}, {anchor: distance info}).

    `rec` may carry two optional enrichments from the crawler:
        walk_credit  0..1 amenity density from walk.py
        walk_routes  {anchor_key: {"mi": float, "min": float}} routed walks

    Both are optional by design. If the public routers are down the listing
    still scores, just on crow-flies — degraded, never broken.
    """
    search = cfg["search"]
    anchors = cfg["anchors"]
    weights = cfg["weights"]
    space_cfg = cfg.get("space", {})

    straight = anchor_distances(rec.get("lat"), rec.get("lon"), anchors)
    routed = rec.get("walk_routes") or {}

    parts = {
        "price": (price_credit(rec.get("price"), search["budget_min"],
                               search["budget_max"]), weights.get("price", 0)),
        "space": (space_credit(rec.get("sqft"),
                               space_cfg.get("sqft_floor", 700),
                               space_cfg.get("sqft_ceiling", 1800),
                               rec.get("sqft_basis", "reported")),
                  weights.get("space", 0)),
        "walk": (float(rec.get("walk_credit") or 0.0),
                 weights.get("walk", 0)),
        "house_bonus": (1.0 if rec.get("prop_type") in HOUSEY else 0.0,
                        weights.get("house_bonus", 0)),
        "condition": (condition_credit(rec), weights.get("condition", 0)),
        "amenity": (amenity_credit(rec), weights.get("amenity", 0)),
    }

    dists = {}
    for key, a in anchors.items():
        walk = routed.get(key) or {}
        # Prefer the real walk. Credit thresholds in config are expressed as
        # walking distances, so feeding them a straight line is the lenient
        # reading — noted in `basis` so the dashboard can say which it used.
        miles = walk.get("mi", straight.get(key))
        dists[key] = {
            "mi": miles,
            "straight_mi": straight.get(key),
            "walk_min": walk.get("min"),
            "basis": "routed" if walk.get("mi") is not None else "straight",
        }
        parts[key] = (
            proximity_credit(miles, a.get("full_credit_mi", 0.5),
                             a.get("zero_credit_mi", 3.0)),
            a.get("weight", 0),
        )

    total_w = sum(w for _, w in parts.values())
    if not total_w:
        return 0.0, {}, dists
    score = sum(c * w for c, w in parts.values()) / total_w * 100
    # Property-type preference, learned from feedback ("dont like duplexes
    # they should be downgraded"). A multiplier rather than a weighted
    # component so it can push a listing *down*, not just fail to lift it.
    ptype_w = (cfg.get("prop_type_weights") or {}).get(rec.get("prop_type"), 1.0)
    score *= ptype_w
    return round(score, 1), {k: round(c, 3) for k, (c, _) in parts.items()}, dists


def passes_filters(rec: dict, cfg: dict) -> bool:
    """Hard gates. Anything failing these never reaches the score."""
    s = cfg["search"]
    price = rec.get("price")
    if not price or not (s["budget_min"] <= price <= s["budget_max"]):
        return False
    beds = rec.get("beds")
    if beds is None or not (s["beds_min"] <= beds <= s["beds_max"]):
        return False
    baths = rec.get("baths")
    if baths is not None and baths < s.get("baths_min", 0):
        return False
    if not _available_soon(rec.get("available"),
                           s.get("available_within_days", 45)):
        return False
    # sqft is advisory, not a gate: many house listings omit it entirely and
    # dropping them would defeat the point of looking for houses.
    return True


def _available_soon(value, within_days) -> bool:
    """Gate on the advertised availability date.

    Feedback surfaced a "not available" listing as a dislike — a unit that
    cannot actually be leased soon is noise no matter how well it scores.
    rent.com gives ISO dates or "TODAY"; anything dated further out than
    `within_days` is dropped. Unknown or unparseable values pass: Craigslist
    and Redfin rarely say, and silence is not unavailability.
    """
    if not value:
        return True
    v = str(value).strip()
    if v.upper() in ("TODAY", "NOW", "AVAILABLE NOW", "IMMEDIATE"):
        return True
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return True
    # Sources disagree on timezone: rent.com sends "...Z", Redfin sends a
    # naive local stamp. Treat naive as UTC rather than crash the pipeline.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc) + timedelta(days=within_days)


def is_baseline(rec: dict, cfg: dict) -> bool:
    """Does this listing look like one of the apartments we already like?"""
    hay = " ".join(str(rec.get(k) or "").lower()
                   for k in ("property_name", "title", "address"))
    for b in cfg.get("baselines", []):
        if any(m.lower() in hay for m in b.get("match", [])):
            return True
    return False
