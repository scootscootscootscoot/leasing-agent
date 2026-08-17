"""hotpads — deliberately a stub, not an oversight.

HotPads is owned by Zillow: it sits behind the same PerimeterX wall (its
`/api/listings/search` endpoint returns the identical `px-captcha` 403) and it
is fed from the same inventory. Paying proxy credits to scrape it would buy us
a near-duplicate of what `zillow.py` already returns, deduplicated away in the
same crawl.

Left here as a named, disabled source so the shape of the decision is visible
in the dashboard's source table rather than buried in a commit message. If
HotPads ever diverges from Zillow's inventory, implement `fetch` the same way
zillow.py does — proxy fetch, parse the embedded state blob.
"""
from . import SourceError

NAME = "hotpads"
LABEL = "HotPads"
NEEDS_KEY = True
STUB = True


def fetch(ctx) -> list:
    raise SourceError(
        "not implemented on purpose — Zillow-owned, same PerimeterX wall and "
        "same inventory as the zillow source")
