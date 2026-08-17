"""crawler — one pass over every enabled source.

Pipeline per record: geo gate (inside the radius) → hard filters (budget,
beds, baths) → cross-source dedupe → enrich (sqft, walkability) → score →
upsert.

Enrichment sits after dedupe on purpose. Walk routing and square-footage
estimation are the expensive steps, and doing them before dedupe would pay
for every duplicate — Craigslist reposts and rent.com's overlapping
neighbourhood pages roughly double the raw record count.

Graceful degradation is the rule here, matching the rest of the fleet: a
source that throws is recorded with its error and the run continues on the
others. A run only fails outright if *every* source failed.
"""
import logging
import re

import sources
import sqft as sqft_mod
import walk as walk_mod
from geo import bbox, within_radius
from score import is_baseline, passes_filters, score_listing

log = logging.getLogger("crawler")

# Redfin's structured data beats Craigslist's title-scraped guesses, so when
# the same unit shows up twice we keep the higher-priority source. rent.com
# sits just under Redfin: same structured quality, but its floor-plan ids are
# positional, so Redfin's stable unit ids win a tie.
PRIORITY = {"redfin": 3, "rent": 2, "zillow": 2, "hotpads": 2,
            "craigslist": 1, "instagram": 0}

_ws = re.compile(r"[^a-z0-9]+")


def dedupe_key(rec):
    """Identify the same unit across sources and across repostings.

    Three tiers, strongest first:

      address  a real street address plus the shape of the unit. Redfin and
               rent.com both give one; it survives a landlord retitling the
               post, and it is case- and punctuation-insensitive because
               "2928 E 13th St Unit A" and "2928 E 13th St unit A" are the
               same house.
      geo      lat/lon to 4dp (~11m) plus the same shape. This is what
               catches Craigslist reposts, where one complex lists the same
               unit five times under near-identical titles — identical pin,
               identical rent, identical bed count.
      identity never dedupes; used only when a record has neither.

    The "shape" is beds/baths/sqft/price rather than the free-text `unit`
    label. That label was in the key originally, and it turned out to be the
    wrong thing to trust: rent.com synthesises one from the floor plan
    ("2bd/1ba") where Redfin leaves it empty, so the same house arrived under
    two keys and showed up twice on the dashboard. Structured fields do the
    same job — two genuinely different floorplans at one complex differ in at
    least one of them — without depending on how a source happens to write
    things down.

    Addresses are normalised before comparison because sources do not agree
    on how to spell a street: "1304 Danbury Square Unit A" and "1304 Danbury
    Sq Unit A" are one house that shipped as two cards until suffixes were
    folded together.

    Known gap, logged rather than fixed: a record whose address is missing
    falls through to coordinates, and two sources geocoding the same building
    can land either side of a 4-decimal rounding boundary (~11 m), so the
    pair survives as two cards. Matching on *either* identity would close it
    and needs a union-find rather than one key per record — see the
    enhancements note in the README.
    """
    shape = "|".join(_norm(rec.get(k))
                     for k in ("beds", "baths", "sqft", "price"))

    addr = _norm_addr(rec.get("address"))
    if addr:
        return f"a|{addr}|{shape}"

    lat, lon = rec.get("lat"), rec.get("lon")
    if lat is not None and lon is not None:
        return f"g|{lat:.4f}|{lon:.4f}|{shape}"

    return f"i|{rec['source']}:{rec['source_id']}"


# Street-type abbreviations, so "Danbury Square" and "Danbury Sq" collapse to
# the same identity.
_SUFFIX = {"street": "st", "avenue": "ave", "road": "rd", "drive": "dr",
           "lane": "ln", "boulevard": "blvd", "square": "sq", "court": "ct",
           "place": "pl", "terrace": "ter", "parkway": "pkwy", "circle": "cir",
           "trail": "trl", "highway": "hwy", "apartment": "apt",
           "unit": "unit", "number": "no", "north": "n", "south": "s",
           "east": "e", "west": "w"}


def _norm_addr(addr):
    """Lowercase, expand-free, punctuation-free address for matching."""
    if not addr:
        return ""
    words = [_SUFFIX.get(w, w) for w in re.split(r"[^a-z0-9]+", addr.lower())]
    return "".join(w for w in words if w)


def _norm(value):
    """Numbers to a canonical string so 2 and 2.0 are the same key."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def enrich(ctx, store, cfg, records) -> None:
    """Fill in square footage and walkability in place, best-effort.

    Every step here is optional. Overpass and OSRM are free public services
    with no uptime promise, and a listing with no walk data still scores on
    price, space and straight-line proximity — so failures are logged and
    swallowed rather than allowed to end a run.
    """
    for rec in records:
        sqft_mod.enrich(rec)

    # Learn typical sizes from everything reported so far — this run plus the
    # whole corpus — then fill the gaps. Estimates stay flagged as estimates.
    model = sqft_mod.SqftModel(list(records) + store.top(limit=5000,
                                                         active_only=False))
    for rec in records:
        model.apply(rec)
    ctx.log.info(f"sqft: {_basis_summary(records)} · model knows "
            f"{len(model.by_type_beds)} type/bed medians")

    walk_cfg = cfg.get("walk", {})
    if not walk_cfg.get("enabled", True):
        return

    search = cfg["search"]
    anchor = cfg["anchors"][search["primary_anchor"]]
    box = bbox(anchor["lat"], anchor["lon"], search["radius_mi"] + 0.5)

    try:
        amenities = walk_mod.fetch_amenities(ctx, box)
    except Exception as e:                              # noqa: BLE001
        log.warning("walk: amenity fetch failed (%s) — skipping walk scores", e)
        amenities = []

    if amenities:
        for rec in records:
            credit, counts = walk_mod.amenity_score(rec.get("lat"),
                                                    rec.get("lon"), amenities)
            rec["walk_credit"] = round(credit, 3)
            rec["walk_counts"] = counts

    if not walk_cfg.get("route_anchors", True):
        return

    # Route the strongest candidates first: the cap applies to points we have
    # not routed before, so ordering decides who gets a real walk soonest.
    located = [r for r in records
               if r.get("lat") is not None and r.get("lon") is not None]
    located.sort(key=lambda r: (r.get("walk_credit") or 0), reverse=True)
    points = list(dict.fromkeys((r["lat"], r["lon"]) for r in located))

    try:
        walks = walk_mod.route_walks(
            ctx, points, cfg["anchors"],
            max_new=int(walk_cfg.get("max_routed_per_run", 400)))
    except Exception as e:                              # noqa: BLE001
        log.warning("walk: routing failed (%s) — using crow-flies", e)
        return

    for rec in located:
        hit = walks.get((rec["lat"], rec["lon"]))
        if hit:
            rec["walk_routes"] = hit
    ctx.log.info(f"walk: {len(walks)}/{len(points)} points routed on foot")


def _basis_summary(records) -> str:
    counts = {}
    for rec in records:
        basis = rec.get("sqft_basis") or "missing"
        counts[basis] = counts.get(basis, 0) + 1
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def crawl(ctx, store, cfg) -> dict:
    search = cfg["search"]
    anchor = cfg["anchors"][search["primary_anchor"]]
    radius = search["radius_mi"]

    run_id = store.start_run()
    detail, kept = {}, {}
    n_sources_ok = 0

    for mod in sources.load(cfg):
        name = mod.NAME
        try:
            recs = mod.fetch(ctx)
            n_sources_ok += 1
        except Exception as e:                      # noqa: BLE001
            log.warning("source %s failed: %s", name, e)
            detail[name] = {"n": 0, "raw": 0, "error": str(e)[:200]}
            continue

        n_raw, n_kept = len(recs), 0
        for rec in recs:
            if not within_radius(rec.get("lat"), rec.get("lon"),
                                 anchor["lat"], anchor["lon"], radius):
                continue
            if not passes_filters(rec, cfg):
                continue
            key = dedupe_key(rec)
            prev = kept.get(key)
            if prev and PRIORITY.get(prev["source"], 0) >= PRIORITY.get(name, 0):
                continue
            kept[key] = rec
            n_kept += 1
        detail[name] = {"n": n_kept, "raw": n_raw, "error": None}
        log.info("source %s: %d raw → %d in scope", name, n_raw, n_kept)

    records = list(kept.values())
    enrich(ctx, store, cfg, records)

    new = drops = 0
    for rec in records:
        rec["score"], rec["parts"], rec["distances"] = score_listing(rec, cfg)
        rec["is_baseline"] = is_baseline(rec, cfg)
        rec["ppsf"] = sqft_mod.price_per_sqft(rec)
        outcome = store.upsert(rec)
        if outcome == "new":
            new += 1
        elif outcome == "drop":
            drops += 1

    retired = store.deactivate_missing(set(),
                                       cfg["crawl"].get("stale_days", 14))
    store.cache_prune(48 * 3600)

    ok = n_sources_ok > 0
    store.finish_run(run_id, len(kept), new, drops, ok, detail)
    log.info("crawl done: %d in scope, %d new, %d price drops, %d retired",
             len(kept), new, drops, retired)
    return {"found": len(kept), "new": new, "drops": drops,
            "retired": retired, "detail": detail, "ok": ok}
