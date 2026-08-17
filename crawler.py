"""crawler — one pass over every enabled source.

Pipeline per record: geo gate (inside the radius) → hard filters (budget,
beds, baths) → cross-source dedupe → score → upsert.

Graceful degradation is the rule here, matching the rest of the fleet: a
source that throws is recorded with its error and the run continues on the
others. A run only fails outright if *every* source failed.
"""
import logging
import re

import sources
from geo import within_radius
from score import is_baseline, passes_filters, score_listing

log = logging.getLogger("crawler")

# Redfin's structured data beats Craigslist's title-scraped guesses, so when
# the same unit shows up twice we keep the higher-priority source.
PRIORITY = {"redfin": 3, "zillow": 2, "hotpads": 2, "craigslist": 1,
            "instagram": 0}

_ws = re.compile(r"[^a-z0-9]+")


def dedupe_key(rec):
    """Identify the same unit across sources and across repostings.

    Three tiers, strongest first:

      address  a real street address plus beds/rent/floorplan. Redfin and
               Zillow give one; it survives a landlord retitling the post.
      geo      lat/lon to 4dp (~11m) plus beds/rent/floorplan. This is what
               catches Craigslist reposts, where the same complex lists the
               same unit five times under near-identical titles — identical
               pin, identical rent, identical bed count.
      identity never dedupes; used only when a record has neither.

    `unit` is in the key on purpose: two different floorplans in one complex
    legitimately share coordinates and sometimes share a price.
    """
    unit = _ws.sub("", (rec.get("unit") or "").lower())
    beds, price = rec.get("beds"), rec.get("price")

    addr = _ws.sub("", (rec.get("address") or "").lower())
    if addr:
        return f"a|{addr}|{beds}|{price}|{unit}"

    lat, lon = rec.get("lat"), rec.get("lon")
    if lat is not None and lon is not None:
        return f"g|{lat:.4f}|{lon:.4f}|{beds}|{price}|{unit}"

    return f"i|{rec['source']}:{rec['source_id']}"


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

    new = drops = 0
    for rec in kept.values():
        rec["score"], _, rec["distances"] = score_listing(rec, cfg)
        rec["is_baseline"] = is_baseline(rec, cfg)
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
