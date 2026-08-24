# leasing-agent

A 24/7 crawler that hunts 2-bedroom rentals near **Mueller** and **MLK Jr
Station** in Austin, scores them against what we actually want, and pushes the
good ones to Telegram. Runs on `fatman` (Pi 5) beside the rest of the fleet.

Stdlib-only Python — no venv, no SDKs, no build step.

```
┌──────────┐  redfin ─────┐
│  crawler │  rent.com ───┤→ filter → dedupe → enrich → score → SQLite
└──────────┘  craigslist ─┤          (sqft, walk)              │
               zillow* ───┘                                    │
                                          ┌────────────────────┴──────────┐
                                    Telegram bot            HTML dashboard
                                    (commands + alerts)      :8810 + ratings
                                                                   │
                                                              learn.py
                                                        (weights ← feedback)
```

`*` proxy-gated, see [Sources](#sources).

## What it looks for

Set in `config.json`, no code changes needed:

| | |
|---|---|
| budget | $1,500 – $3,000 |
| beds | 2 – 3 |
| radius | 3 mi of Mueller |
| anchors | Mueller (30.2988, −97.7048) · MLK Jr Station (30.2799, −97.7135) |
| baselines | The Platform, Starlight — matched by name so you can compare |

### Scoring

Six weighted components, each 0–1 before weighting, normalised to 0–100.
Weights live in `config.json` and do not need to sum to anything in
particular.

| component | weight | what earns credit |
|---|---:|---|
| `price` | 25 | cheaper inside the budget band |
| `mueller` | 25 | walking proximity to Mueller |
| `space` | 20 | square footage, 700 → 1800 sq ft |
| `mlk_station` | 20 | walking proximity to the rail station |
| `walk` | 15 | what you can reach on foot (see below) |
| `house_bonus` | 10 | anything that is not an apartment complex |

Proximity credit is full inside `full_credit_mi`, zero past `zero_credit_mi`,
linear between — easy to read off the dashboard and easy to retune.

## Walkability

Straight-line distance lies around Mueller. I-35, the rail corridor and the
golf course all mean two points 0.6 mi apart on a map can be a 1.9 mi walk, so
`walk.py` measures the real thing using two free, keyless services:

- **Routed anchor walks** — OSRM's public pedestrian profile
  (`routing.openstreetmap.de/routed-foot`). One `table` request routes every
  listing against both anchors at once, which is what makes this affordable.
  Distances shown on the dashboard are real walks, with the minutes beside
  them; anything that could not be routed is labelled `direct` and falls back
  to crow-flies rather than disappearing.
- **Amenity density** — Overpass, counting what you can actually walk to:
  groceries, food, transit, parks, errands, schools. Scored the way Walk Score
  works, with distance decay and diminishing returns per category, so twenty
  cafés never beat a café plus a grocery plus a park.

Both are cached in SQLite for weeks — street networks and grocery stores do
not move — so a normal crawl makes no network calls here at all. One
deliberate asymmetry: **anchors are routed, amenities are not.** Routing every
listing to all ~1,800 nearby amenities would be hundreds of thousands of legs
per crawl, so amenity distance is haversine inflated by a 1.35 detour factor,
which is a fair estimator in aggregate and does not pretend to be more.

## Square footage

Square footage is the whole point — a house with *more space* than The
Platform or Starlight for the same money — and it is the field sources most
often omit, precisely on the by-owner house listings that matter most.
`sqft.py` recovers it in three ordered strategies, and every listing carries a
`sqft_basis` saying which one was used:

| basis | meaning |
|---|---|
| `reported` | the source gave a number |
| `parsed` | pulled out of the listing text (`2/1 1,150 sqft bungalow`) |
| `estimated` | the median of comparable listings, same beds and property type |

Estimates are learned from our own corpus rather than a static table, so they
are Austin-specific and improve as the database grows, and they fall back to a
conservative table below five comparables. **An estimate never earns full
space credit** — it is shrunk toward a neutral prior, so a listing cannot win
on space it has not proven it has. Estimated sizes render with a `~` on the
dashboard, in Telegram, and in `agent.py top`.

The payoff is `$/sqft`, which is the number that actually answers "more space
for the money", and the one the baselines are compared on.

## Rating listings, and what the agent does with it

Every card on the dashboard has a **rate** control: like or pass, plus a
free-text reason. It is a plain HTML form — no JavaScript anywhere in the
dashboard — so it works the same over Tailscale from a phone. The same thing
works from Telegram with `/like <id> why…` and `/dislike <id> why…`.

Verdicts are append-only: changing your mind is kept, because "liked it, then
saw it and hated it" is stronger signal than either verdict alone. Each one
snapshots the listing's score and component breakdown *as judged*, so a later
retune does not silently invalidate the history.

`learn.py` reads that back and asks which components actually predict what you
like. It splits the component credits by verdict, compares the means, and
suggests weight changes — with three guards, because the failure mode of a
self-tuning system is confidently learning from four data points:

- **minimum sample** — below four verdicts on each side it suggests nothing,
  and says so
- **effect size** — the gap is measured against the spread, so a large gap
  between two noisy, overlapping groups is not treated as evidence
- **bounded steps** — no weight moves more than 35% in one round

It is read-only by default. The dashboard's *What that implies* section shows
its current read; `python3 learn.py` prints it; `--apply` is the explicit
opt-in and keeps a timestamped backup of `config.json`.

For an LLM agent, `learn.py --export feedback.jsonl` writes each verdict
paired with its listing and the raw reason text. The arithmetic can tell you
*that* walkability predicts a like; only the sentences can tell you it was
really about a walk with no sidewalk. `/api/feedback` and `/api/learn` serve
the same data over HTTP.

## Sources

Every site was probed directly before being wired up:

| source | status | notes |
|---|---|---|
| **Redfin** | ✅ works | `search/rentals` + `floorPlans`. The workhorse. |
| **rent.com** | ✅ works | RentPath. Neighbourhood-targeted, inline floor plans. |
| **Craigslist** | ✅ works | `sapi.craigslist.org/web/v8`, by-owner houses Redfin never sees. |
| Zillow | 🔑 needs key | PerimeterX captcha on every datacenter IP. Implemented, off by default. |
| HotPads | ⛔ stub | Zillow-owned: same wall, same inventory. Deliberately not implemented. |
| Instagram | ⛔ stub | No public API; needs a logged-in session for thin coverage. Deferred. |
| apartments.com, homes.com, RentCafe | ⛔ | Flat `Access Denied` on every request. |
| Trulia | ⛔ | Zillow-owned, same PerimeterX wall. |
| realtor.com | ⛔ | 429 on every request, no honest way through. |
| Zumper / PadMapper | ⛔ | JS shell; its API rejects unsigned calls (`451 missing parameter: url`). |
| apartmentlist.com | ⛔ | Serves 200 but renders client-side; no listing data in the HTML. |

Everything in that table was probed directly rather than assumed, and the
losers were re-probed when walkability went in — which is how rent.com turned
up. It had been written off with the rest, but it answers normally.

### rent.com

The best-behaved source we have. An ordinary Next.js page whose
`__NEXT_DATA__` carries the whole result set — coordinates, per-floorplan
beds/baths/sqft/rent, availability — with no key, no proxy, and no second
request per property.

Two things make it cheap to crawl politely. **Neighbourhood paths**:
`/texas/austin-apartments/mueller-neighborhood` returns 113 properties where
the city-wide path returns 4,136, so we crawl a handful of neighbourhoods
around the anchors instead of paginating Austin. **Inline floor plans**:
unlike Redfin there is no follow-up request to turn "1–3 beds, $604–1838" into
an answerable 2BR price.

The neighbourhood list is discovered, not hardcoded — each page carries
`seoLinks.nearby.neighborhoods` with distances, so seeding with Mueller finds
the other fourteen inside the radius by itself and picks up new ones without a
code change.

One quirk worth knowing: rent.com soft-throttles by answering **`202 Accepted`
with a zero-length body** rather than a 4xx. That is a success as far as
urllib is concerned and an empty page as far as we are concerned, so requests
are spaced 2.5s apart and retried with a long backoff. Without that, a crawl
silently loses about a fifth of its requests.

Redfin does a lot of work here: its rental index ingests **Zillow and RentPath
feeds** (visible per-record in `feedOriginalSource`), so much of what Zillow
would return arrives anyway. Zillow being walled costs less than it looks.

Redfin returns multi-unit properties as *ranges* ("1–3 beds, $604–1838"), which
is useless for a 2BR budget question, so each qualifying complex is expanded
via `floorPlans` into real per-floorplan beds/baths/sqft/rent. Those responses
are cached in SQLite for 6h, which keeps a crawl to a few dozen requests.

### Turning Zillow on

```bash
echo 'SCRAPER_API_KEY=...' >> .env      # ScraperAPI or similar
# then flip sources.zillow to true in config.json
```

Zillow's parser is written against its documented page structure but has
**not** been verified end-to-end, because doing so needs a paid key. Expect to
adjust field names on the first live run — `/sources` in Telegram will show you
the error if it does not line up.

## Telegram

```
/top [n]      best-scoring listings right now
/new [n]      most recently found
/houses       houses, duplexes & condos only
/townhomes    townhomes only
/apts         apartment complexes only
/drops        rent cuts since we first saw them
/near [mi]    closest to MLK Jr Station
/detail <id>  everything on one listing, incl. price history
/pin <id>     save one (/unpin, /pinned)
/crawl        run a crawl now
/sources      per-source health from the last run
/stats        corpus summary + how your baselines compare
```

Alerts fire after each crawl for anything scoring ≥ `alert_min_score` that has
not been alerted before, capped at `alert_max_per_run` and held during quiet
hours (22:00–07:00 America/Chicago).

## Setup

The bot needs its own token — the fleet bot's is long-polled by `fleetbot` on
fatman and two `getUpdates` pollers on one token fight over the offset.

```bash
~/Desktop/botsmith/botsmith.py new leasing-agent   # BotFather step is manual
cp .env.example .env                               # paste token + chat id
python3 test_leasing_agent.py                      # 27 tests, no network
python3 agent.py crawl                             # one crawl, prints a summary
python3 agent.py top 15                            # see the ranking
./deploy/deploy.sh                                 # → fatman
```

`botsmith new` prints both the token and the chat id it auto-detects, and also
prints the line to add it to the encrypted fleet store
(`~/Desktop/fleet-secrets/edit-secrets.sh`).

## Known gaps

Logged rather than fixed, so they are not rediscovered as surprises:

- **Cross-source duplicates can still slip through.** Listings are identified
  by address first (normalised for case, punctuation and street-suffix
  abbreviations, so `Danbury Square` and `Danbury Sq` collapse), falling back
  to coordinates. A record with *no* address relies on coordinates alone, and
  two sources geocoding the same building can land either side of a
  4-decimal rounding boundary (~11 m) and survive as two cards. The real fix
  is to match on *either* identity via a union-find rather than one key per
  record; it is a contained change, just not a small one.
- **rent.com yield varies run to run.** Its 202-throttle means a cold-cache
  crawl returns fewer listings than a warm one (191 vs 359 observed). The
  retry and circuit-breaker keep it polite and bounded; coverage catches up
  on the next cycle rather than in any single run.
- **Zillow's parser is unverified.** It is written against the documented page
  structure but has never run end-to-end, because that needs a paid proxy key.

## Where things live

| | |
|---|---|
| source of truth | `~/Desktop/leasing_agent` on scootpc |
| running copy | `fatman:~/leasing_agent` (rsync, no git on the Pi) |
| service | `systemctl --user status leasing-agent` |
| dashboard | `http://fatman.local:8810/` (and over Tailscale) |
| database | `fatman:~/leasing_agent/data/leasing.db` (gitignored) |
| secrets | `.env`, chmod 600, never in git or `config.json` |

`journalctl --user` reports no journal files over ssh on fatman — use
`systemctl --user status leasing-agent` to read logs.

## Layout

```
agent.py       daemon: crawl loop + bot + dashboard threads, and the CLI
crawler.py     one pass: geo gate → filters → dedupe → enrich → score → upsert
sources/       one module per site; each may raise, the run continues
score.py       the weighted components above
geo.py         haversine, bounding boxes, proximity credit
walk.py        routed pedestrian distance (OSRM) + amenity density (Overpass)
sqft.py        square-footage parsing, estimation and $/sqft
store.py       SQLite (WAL): listings, price history, runs, pins, feedback
learn.py       reads feedback back into suggested scoring weights
bot.py         Telegram long-poll commander + push alerts
dashboard.py   server-side HTML, no JS, no CDN, plus the rating form
```

Graceful degradation is the rule throughout, same as the rest of the fleet: a
source that throws is recorded with its error and rendered as an error tile;
the run continues on the others. A run only fails if *every* source fails.
Listings are retired after `stale_days` of not being seen rather than
immediately, so one bad run cannot wipe the corpus.
