"""sqft — recover, estimate and price square footage.

Square footage is the whole point of this search: the brief is a house with
*more space* than The Platform or Starlight for the same money. It is also
the field sources most often omit — precisely on the by-owner house listings
we care most about. `score.py` used to paper over that with a flat 0.4 credit
for unknown sqft, which is a guess dressed up as a number.

This module replaces the guess with three ordered strategies:

  1. reported    the source gave us a number; use it
  2. parsed      the number is in the free text ("2/1 1,150 sqft bungalow"),
                 which is where Craigslist puts it most of the time
  3. estimated   fall back to the corpus: the median sqft of comparable
                 listings with the same bed count and property type

Anything not reported is labelled — `sqft_basis` travels with the record and
surfaces on the dashboard, so an estimate is never mistaken for a fact.

The payoff is `$/sqft`, the metric that actually answers "more space for the
money" and the one the baselines are compared on.
"""
import re
import statistics

# A number with optional comma grouping. The alternation order matters:
# "1,150" must be tried as a grouped number *before* falling back to bare
# digits, or the pattern matches only the "150" and silently reads a
# 1,150 sq ft house as 150 sq ft.
NUM = r"\d{1,3}(?:,\d{3})+|\d{3,5}"

# "1,150 sqft", "1150 sq. ft.", "1150ft2", "1,150 square feet".
SQFT_RE = re.compile(
    rf"({NUM})\s*(?:\+/-\s*)?"
    r"(?:sq\.?\s*\.?\s*(?:ft|feet)\.?|sqft|square\s+f(?:ee)?t|ft2|ft²)",
    re.I)

# The same number written the other way round: "sqft: 1150".
SQFT_LABEL_RE = re.compile(
    rf"(?:sq\.?\s*ft\.?|sqft|square\s+footage|size)\s*[:=]\s*({NUM})",
    re.I)

# Outside this band it is not a dwelling's floor area — it is a lot size, a
# price, a year, or a typo. Rejecting them matters more than catching every
# last listing, because one bad parse pollutes the estimator for everything.
MIN_SQFT = 250
MAX_SQFT = 6000

# Minimum believable floor area per bedroom. Sources do publish nonsense —
# a "2 bed, 360 sq ft" turned up in a live crawl — and because the estimator
# learns its medians from reported values, one of those drags down every
# listing that has no size of its own. Set low enough that a genuinely tiny
# east-Austin duplex (a 528 sq ft two-bed did appear, and is real) survives.
MIN_SQFT_PER_BED = 250

# Austin rules of thumb, used only when the corpus has too few comparables to
# speak for itself. Deliberately conservative: under-estimating space costs a
# listing a little score, over-estimating puts a cramped place on the tour list.
FALLBACK_BY_BEDS = {0: 500, 1: 700, 2: 1000, 3: 1350, 4: 1750, 5: 2200}

# A house of a given bed count is usually roomier than an apartment of the
# same count. Applied to the fallback table, not to corpus medians, which
# already carry the difference.
TYPE_MULTIPLIER = {"house": 1.15, "duplex": 1.05, "townhouse": 1.0,
                   "condo": 0.95, "apartment": 0.92, "unknown": 1.0}

MIN_COMPARABLES = 5     # below this a median is noise, so use the table


def parse_sqft(*texts):
    """First plausible square footage found in any of `texts`, else None.

    Takes the *largest* plausible match rather than the first: listings that
    mention several numbers ("1 bed 750 sqft in a 1,900 sqft home") are
    describing the building last, and the unit first — but a bare "750" next
    to "1,900" is ambiguous enough that the conservative read is to prefer
    the labelled form, which is why labelled matches win outright.
    """
    for text in texts:
        if not text:
            continue
        labelled = SQFT_LABEL_RE.search(str(text))
        if labelled:
            value = _clean(labelled.group(1))
            if value:
                return value

    for text in texts:
        if not text:
            continue
        values = [_clean(m) for m in SQFT_RE.findall(str(text))]
        values = [v for v in values if v]
        if values:
            return min(values)     # the unit, not the building or the lot
    return None


def _clean(raw):
    try:
        value = int(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if MIN_SQFT <= value <= MAX_SQFT else None


def plausible(sqft, beds=None) -> bool:
    """Is this a believable floor area for a dwelling with `beds` bedrooms?"""
    if not sqft or not MIN_SQFT <= sqft <= MAX_SQFT:
        return False
    if beds and beds >= 1 and sqft / beds < MIN_SQFT_PER_BED:
        return False
    return True


def enrich(rec: dict) -> dict:
    """Fill in `sqft` from the listing text and stamp `sqft_basis`.

    Mutates and returns `rec`. Runs before scoring and before the corpus
    estimator, so a parsed number is available to both.
    """
    reported = rec.get("sqft")
    if plausible(reported, rec.get("beds")):
        rec["sqft_basis"] = "reported"
        return rec

    if reported:
        # A lot size, a typo, or a bedroom count that cannot fit in the area
        # given. Drop it and let the estimator fill in, rather than scoring
        # a number we do not believe.
        rec["sqft"] = None

    parsed = parse_sqft(rec.get("title"), rec.get("description"),
                        rec.get("unit"))
    if parsed and plausible(parsed, rec.get("beds")):
        rec["sqft"] = parsed
        rec["sqft_basis"] = "parsed"
    else:
        rec["sqft_basis"] = "missing"
    return rec


class SqftModel:
    """Median square footage by (property type, bed count), from the corpus.

    Built once per crawl from everything we have already seen with a real
    reported number, then used to estimate the ones that have none. Learning
    from our own corpus beats a static table because it is Austin-specific,
    current, and improves as the database grows.
    """

    def __init__(self, rows=()):
        self.by_type_beds = {}
        self.by_beds = {}
        self._fit(rows)

    def _fit(self, rows):
        typed, bedded = {}, {}
        for row in rows:
            sqft, beds = row.get("sqft"), row.get("beds")
            basis = row.get("sqft_basis") or "reported"
            if beds is None or basis not in ("reported", "parsed"):
                continue
            if not plausible(sqft, beds):
                continue
            beds = int(beds)
            typed.setdefault((row.get("prop_type") or "unknown", beds),
                             []).append(sqft)
            bedded.setdefault(beds, []).append(sqft)

        self.by_type_beds = {k: round(statistics.median(v))
                             for k, v in typed.items()
                             if len(v) >= MIN_COMPARABLES}
        self.by_beds = {k: round(statistics.median(v))
                        for k, v in bedded.items()
                        if len(v) >= MIN_COMPARABLES}

    def estimate(self, prop_type, beds):
        """Best available estimate, most specific comparable first."""
        if beds is None:
            return None
        beds = int(beds)
        prop_type = prop_type or "unknown"

        hit = self.by_type_beds.get((prop_type, beds))
        if hit:
            return hit
        hit = self.by_beds.get(beds)
        if hit:
            return round(hit * TYPE_MULTIPLIER.get(prop_type, 1.0))

        base = FALLBACK_BY_BEDS.get(beds)
        if base is None:
            return None
        return round(base * TYPE_MULTIPLIER.get(prop_type, 1.0))

    def apply(self, rec: dict) -> dict:
        """Estimate `sqft` if it is still missing. Mutates and returns `rec`."""
        if rec.get("sqft"):
            return rec
        estimate = self.estimate(rec.get("prop_type"), rec.get("beds"))
        if estimate:
            rec["sqft"] = estimate
            rec["sqft_basis"] = "estimated"
        return rec

    def summary(self) -> dict:
        """{'house 2bd': 1180, ...} — what the model learned, for /stats."""
        return {f"{t} {b}bd": v
                for (t, b), v in sorted(self.by_type_beds.items())}


def price_per_sqft(rec):
    """Rent per square foot, or None. The 'more space for the money' number."""
    price, sqft = rec.get("price"), rec.get("sqft")
    if not price or not sqft:
        return None
    return round(price / sqft, 2)


# An estimated size is shrunk toward a neutral prior rather than trusted at
# face value. Shrinkage rather than a flat discount because it has to work at
# both ends: a generous estimate must not reach full credit, and a stingy one
# must not be punished as hard as a measured small unit would be.
ESTIMATE_PRIOR = 0.45
ESTIMATE_TRUST = 0.7


def space_credit(sqft, floor, ceiling, basis="reported") -> float:
    """Square footage scaled between the config floor and ceiling, 0..1.

    An estimated number never earns full credit: the estimate is good enough
    to rank on, not good enough to let a listing win on space it has not
    actually proven it has.
    """
    if not sqft:
        return 0.4          # nothing to go on; neither reward nor bury it
    if sqft <= floor:
        credit = 0.0
    elif sqft >= ceiling or ceiling <= floor:
        credit = 1.0
    else:
        credit = (sqft - floor) / (ceiling - floor)

    if basis == "estimated":
        credit = ESTIMATE_PRIOR + ESTIMATE_TRUST * (credit - ESTIMATE_PRIOR)
    return credit


def compare_to_baselines(rec, baselines) -> dict:
    """How this listing's $/sqft and size stack up against the liked ones.

    Returns {} when either side lacks the numbers — the honest answer when
    the baselines have not been found in a crawl yet.
    """
    ours = price_per_sqft(rec)
    theirs = [price_per_sqft(b) for b in baselines]
    theirs = [t for t in theirs if t]
    if not ours or not theirs:
        return {}

    baseline_ppsf = statistics.median(theirs)
    sizes = [b["sqft"] for b in baselines if b.get("sqft")]
    baseline_sqft = statistics.median(sizes) if sizes else None

    out = {"ppsf": ours, "baseline_ppsf": round(baseline_ppsf, 2),
           "ppsf_delta_pct": round((ours - baseline_ppsf) / baseline_ppsf * 100)}
    if baseline_sqft and rec.get("sqft"):
        out["baseline_sqft"] = round(baseline_sqft)
        out["extra_sqft"] = round(rec["sqft"] - baseline_sqft)
    return out
