"""instagram — stub, deferred by design.

Instagram has no public listings API. Getting agent and property-manager posts
means either scraping public hashtag pages (thin, brittle, and rate-limited
hard) or driving a logged-in session, which puts whatever account it uses at
real risk of a ban. Neither is worth it while Redfin and Craigslist between
them cover the Mueller-area inventory.

The interface is kept so it can be dropped in later without touching the
orchestrator: implement `fetch(ctx)` returning normalised records, flip
`sources.instagram` to true in config.json, and it joins the crawl.

If you do implement it: use a throwaway account, keep its credentials in .env
(never config.json), and expect to re-do it every time Instagram ships a
layout change.
"""
from . import SourceError

NAME = "instagram"
LABEL = "Instagram"
NEEDS_KEY = False
STUB = True


def fetch(ctx) -> list:
    raise SourceError(
        "stub — deferred; needs a logged-in session (ban risk) for thin "
        "coverage. See the module docstring before enabling.")
