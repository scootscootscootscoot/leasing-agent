"""learn — turn like/dislike feedback into scoring changes.

The dashboard collects verdicts and free-text reasons. This module reads them
back and answers one question: which scoring components actually predict what
Scott likes, and which are just noise we are paying weight for?

The method is deliberately simple and inspectable. Every listing carries the
component credits behind its score (`parts`). Split them by verdict, compare
the means, and a component where liked listings consistently score higher is
a component worth more weight. A component where both groups look the same is
not measuring anything Scott cares about.

Three guards keep this honest, because the failure mode of a self-tuning
system is confidently learning from four data points:

  minimum sample   below MIN_PER_SIDE verdicts on each side, nothing is
                   suggested at all — the analysis still renders, labelled
                   as too thin to act on.
  effect size      the gap between the group means is expressed relative to
                   the spread. A large gap between two wide, overlapping
                   distributions is not evidence.
  bounded steps    a suggestion never moves a weight more than MAX_STEP of
                   its current value, and never below MIN_WEIGHT. Feedback
                   nudges the ranking; it cannot invert it overnight.

Nothing here writes to config.json unless asked. `python3 learn.py` prints
the analysis; `--apply` is the explicit opt-in, and it keeps a timestamped
backup. The rest of the time this is a read-only advisor whose output shows
up on the dashboard.

For an external LLM agent, `--export` writes newline-delimited JSON pairing
each verdict with the listing and reason — the free text is where the real
signal lives, and no amount of arithmetic here will extract "the kitchen was
dark" from a component credit.
"""
import json
import re
import shutil
import statistics
import sys
import time

MIN_PER_SIDE = 4        # fewer verdicts than this on either side ⇒ no advice
MAX_STEP = 0.35         # a suggestion may move a weight ±35% at most
MIN_WEIGHT = 2.0        # never let a component be tuned out of existence
MIN_EFFECT = 0.35       # standardised gap below which we call it noise

# Words carrying no signal when counting themes in the free-text reasons.
STOP = set("""a an and are as at be been but by for from had has have i if in
is it its of on or that the this to too very was we with you your there not
no dont really quite just also so about into out over under more most
much many few place house home apartment unit spot""".split())

WORD = re.compile(r"[a-z][a-z'-]{2,}")


def analyse(store, cfg) -> dict:
    """Compare component credits between liked and disliked listings."""
    rows = store.all_feedback(limit=5000)
    latest = {}
    for row in rows:                     # rows are newest-first
        latest.setdefault(row["listing_id"], row)

    liked = [r for r in latest.values() if r["verdict"] == "like"]
    disliked = [r for r in latest.values() if r["verdict"] == "dislike"]

    out = {
        "n_like": len(liked),
        "n_dislike": len(disliked),
        "enough": len(liked) >= MIN_PER_SIDE and len(disliked) >= MIN_PER_SIDE,
        "min_per_side": MIN_PER_SIDE,
        "components": [],
        "themes": {"like": _themes(liked), "dislike": _themes(disliked)},
        "suggestions": [],
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    weights = dict(cfg.get("weights", {}))
    for key, anchor in cfg.get("anchors", {}).items():
        weights[key] = anchor.get("weight", 0)

    for name, weight in sorted(weights.items()):
        yes = _credits(liked, name)
        no = _credits(disliked, name)
        if not yes or not no:
            continue
        effect = _effect_size(yes, no)
        out["components"].append({
            "component": name,
            "weight": weight,
            "like_mean": round(statistics.mean(yes), 3),
            "dislike_mean": round(statistics.mean(no), 3),
            "gap": round(statistics.mean(yes) - statistics.mean(no), 3),
            "effect": round(effect, 2),
            "n": len(yes) + len(no),
        })

    out["components"].sort(key=lambda c: abs(c["effect"]), reverse=True)
    if out["enough"]:
        out["suggestions"] = _suggest(out["components"])
    return out


def _credits(rows, component):
    """The credit this component contributed, per listing, where recorded."""
    values = []
    for row in rows:
        parts = _parts(row)
        value = parts.get(component)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _parts(row) -> dict:
    """Component credits for one verdict, preferring the snapshot.

    The snapshot is what the listing looked like when it was judged. Falling
    back to the listing's current `parts` is better than nothing, but it can
    be stale if weights or walk data changed since.
    """
    for source in (row.get("snapshot"), None):
        if source:
            try:
                snap = json.loads(source)
            except (ValueError, TypeError):
                continue
            parts = snap.get("parts")
            if isinstance(parts, str):
                try:
                    parts = json.loads(parts)
                except ValueError:
                    parts = None
            if isinstance(parts, dict) and parts:
                return parts
    try:
        parts = json.loads(row.get("parts") or "{}")
    except (ValueError, TypeError):
        return {}
    return parts if isinstance(parts, dict) else {}


def _effect_size(yes, no) -> float:
    """Standardised mean difference (Cohen's d), 0 when there is no spread.

    Using the pooled spread rather than the raw gap is what stops a component
    that happens to differ by 0.2 between two very noisy groups from being
    mistaken for a real preference.
    """
    if len(yes) < 2 or len(no) < 2:
        return 0.0
    gap = statistics.mean(yes) - statistics.mean(no)
    var_yes, var_no = statistics.pvariance(yes), statistics.pvariance(no)
    pooled = ((var_yes * len(yes) + var_no * len(no))
              / (len(yes) + len(no))) ** 0.5
    if pooled < 1e-9:
        # No spread at all: a real gap is meaningful, an absent one is not.
        return 0.0 if abs(gap) < 1e-9 else (3.0 if gap > 0 else -3.0)
    return gap / pooled


def _suggest(components) -> list:
    """Bounded weight nudges for components with a real effect."""
    out = []
    for comp in components:
        effect = comp["effect"]
        if abs(effect) < MIN_EFFECT:
            continue
        # Scale the step by how far past the threshold the effect is, capped.
        strength = min((abs(effect) - MIN_EFFECT) / 1.0, 1.0)
        delta = MAX_STEP * strength * (1 if effect > 0 else -1)
        current = comp["weight"] or MIN_WEIGHT
        proposed = max(MIN_WEIGHT, round(current * (1 + delta), 1))
        if abs(proposed - current) < 0.5:
            continue
        out.append({
            "component": comp["component"],
            "from": current,
            "to": proposed,
            "effect": effect,
            "why": (f"liked listings average {comp['like_mean']} on this "
                    f"vs {comp['dislike_mean']} for passed ones"),
        })
    return out


def _themes(rows, top=8) -> list:
    """Most frequent meaningful words in the reasons. Crude, and labelled so.

    Word counts are not comprehension. This is here to point a human — or an
    LLM reading the export — at what to look at, not to draw the conclusion.
    """
    counts = {}
    for row in rows:
        for word in WORD.findall((row.get("reason") or "").lower()):
            if word not in STOP:
                counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"word": w, "n": n} for w, n in ranked[:top] if n > 1]


def apply_suggestions(cfg_path, analysis) -> list:
    """Write suggested weights into config.json. Returns what changed.

    Backs the file up first. Anchor weights live under `anchors.<key>.weight`
    and component weights under `weights.<key>`, so each suggestion is routed
    to whichever one actually holds it.
    """
    if not analysis.get("suggestions"):
        return []

    with open(cfg_path) as f:
        cfg = json.load(f)

    backup = f"{cfg_path}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(cfg_path, backup)

    changed = []
    for suggestion in analysis["suggestions"]:
        name, value = suggestion["component"], suggestion["to"]
        if name in cfg.get("anchors", {}):
            cfg["anchors"][name]["weight"] = value
        elif name in cfg.get("weights", {}):
            cfg["weights"][name] = value
        else:
            continue
        changed.append(suggestion)

    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return changed + [{"backup": backup}]


def export_jsonl(store, path, limit=5000) -> int:
    """Write one JSON object per verdict, for an agent to read.

    Deliberately includes the raw reason text and the full component
    breakdown: the arithmetic in this module can tell you *that* walkability
    predicts a like, but only the sentences can tell you it was really about
    the walk being along a road with no sidewalk.
    """
    rows = store.all_feedback(limit=limit)
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps({
                "verdict": row["verdict"],
                "reason": row.get("reason") or "",
                "when": row["created"],
                "via": row.get("source"),
                "listing": {
                    "id": row["listing_id"],
                    "name": row.get("property_name") or row.get("title"),
                    "address": row.get("address"),
                    "url": row.get("url"),
                    "price": row.get("price"),
                    "beds": row.get("beds"),
                    "baths": row.get("baths"),
                    "sqft": row.get("sqft"),
                    "sqft_basis": row.get("sqft_basis"),
                    "ppsf": row.get("ppsf"),
                    "prop_type": row.get("prop_type"),
                    "score": row.get("score"),
                    "walk_credit": row.get("walk_credit"),
                    "source": row.get("listing_source"),
                    "parts": _parts(row),
                    "distances": _safe(row.get("distances")),
                },
            }, default=str) + "\n")
    return len(rows)


def _safe(raw):
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}


def report(analysis) -> str:
    """Human-readable summary, shared by the CLI and the Telegram bot."""
    lines = [f"feedback: {analysis['n_like']} liked · "
             f"{analysis['n_dislike']} passed"]

    if not analysis["components"]:
        lines.append("no scored feedback yet — rate a few on the dashboard.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"{'component':<14}{'liked':>8}{'passed':>9}{'effect':>9}")
    for comp in analysis["components"]:
        lines.append(f"{comp['component']:<14}{comp['like_mean']:>8.2f}"
                     f"{comp['dislike_mean']:>9.2f}{comp['effect']:>9.2f}")

    if not analysis["enough"]:
        lines.append("")
        lines.append(f"not enough to act on — need {analysis['min_per_side']} "
                     "of each verdict before suggesting weight changes.")
        return "\n".join(lines)

    lines.append("")
    if analysis["suggestions"]:
        lines.append("suggested weights:")
        for s in analysis["suggestions"]:
            arrow = "↑" if s["to"] > s["from"] else "↓"
            lines.append(f"  {arrow} {s['component']:<12} {s['from']} → {s['to']}"
                         f"   ({s['why']})")
        lines.append("")
        lines.append("apply with:  python3 learn.py --apply")
    else:
        lines.append("no component separates likes from passes strongly "
                     "enough to retune. The weights look reasonable.")

    for verdict in ("like", "dislike"):
        themes = analysis["themes"][verdict]
        if themes:
            words = ", ".join(f"{t['word']}({t['n']})" for t in themes)
            lines.append(f"words in {verdict}s: {words}")
    return "\n".join(lines)


def main(argv):
    import agent                                   # local import: CLI only
    cfg, _secrets, store, _ctx = agent.build()
    analysis = analyse(store, cfg)

    if "--export" in argv:
        index = argv.index("--export")
        path = argv[index + 1] if len(argv) > index + 1 else "feedback.jsonl"
        n = export_jsonl(store, path)
        print(f"wrote {n} verdicts to {path}")
        return 0

    print(report(analysis))

    if "--apply" in argv:
        if not analysis["enough"]:
            print("\nrefusing to apply: not enough feedback yet.")
            return 1
        changed = apply_suggestions(agent.CONFIG, analysis)
        if not changed:
            print("\nnothing to apply.")
            return 0
        print(f"\napplied {len(changed) - 1} weight change(s); "
              f"backup at {changed[-1]['backup']}")
        print("the next crawl rescores everything with the new weights.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
