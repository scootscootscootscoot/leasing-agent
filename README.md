# leasing-agent

A 24/7 crawler that hunts 2-bedroom rentals near **Mueller** and **MLK Jr
Station** in Austin, scores them against what we actually want, and pushes the
good ones to Telegram. Runs on `fatman` (Pi 5) beside the rest of the fleet.

Stdlib-only Python — no venv, no SDKs, no build step.

```
┌──────────┐   redfin ──┐
│  crawler │   craigslist ─┤→ filter → dedupe → score → SQLite
└──────────┘   zillow* ──┘                              │
                                          ┌─────────────┴─────────────┐
                                    Telegram bot            HTML dashboard
                                    (commands + alerts)      :8810
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

Five weighted components, each 0–1 before weighting, normalised to 0–100.
Weights live in `config.json` and do not need to sum to anything in
particular.

| component | weight | what earns credit |
|---|---:|---|
| `price` | 25 | cheaper inside the budget band |
| `mueller` | 25 | proximity to Mueller |
| `mlk_station` | 20 | proximity to the rail station |
| `space` | 20 | square footage, 700 → 1800 sq ft |
| `house_bonus` | 10 | anything that is not an apartment complex |

Proximity credit is full inside `full_credit_mi`, zero past `zero_credit_mi`,
linear between — easy to read off the dashboard and easy to retune.

Two deliberate choices worth knowing:

- **Distances are crow-flies**, not walking routes. For the MLK Jr Station
  anchor that understates the real walk wherever I-35 or the rail corridor is
  in the way. Treat it as a shortlist filter, then check the actual route.
- **Missing square footage scores 0.4, not 0.** Plenty of good by-owner house
  listings just omit it, and zeroing them would bury the exact listings we are
  looking for.

## Sources

Every site was probed directly before being wired up:

| source | status | notes |
|---|---|---|
| **Redfin** | ✅ works | `search/rentals` + `floorPlans`. The workhorse. |
| **Craigslist** | ✅ works | `sapi.craigslist.org/web/v8`, by-owner houses Redfin never sees. |
| Zillow | 🔑 needs key | PerimeterX captcha on every datacenter IP. Implemented, off by default. |
| HotPads | ⛔ stub | Zillow-owned: same wall, same inventory. Deliberately not implemented. |
| Instagram | ⛔ stub | No public API; needs a logged-in session for thin coverage. Deferred. |
| apartments.com, Trulia, Zumper, realtor.com | ⛔ | All refused (403/429). Not wired up. |

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
/houses       houses, townhomes & duplexes only
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
crawler.py     one pass: geo gate → filters → dedupe → score → upsert
sources/       one module per site; each may raise, the run continues
score.py       the weighted components above
geo.py         haversine, bounding boxes, proximity credit
store.py       SQLite (WAL): listings, price history, runs, pins
bot.py         Telegram long-poll commander + push alerts
dashboard.py   server-side HTML, no JS, no CDN
```

Graceful degradation is the rule throughout, same as the rest of the fleet: a
source that throws is recorded with its error and rendered as an error tile;
the run continues on the others. A run only fails if *every* source fails.
Listings are retired after `stale_days` of not being seen rather than
immediately, so one bad run cannot wipe the corpus.
