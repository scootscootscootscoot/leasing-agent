#!/usr/bin/env python3
"""leasing-agent — 24/7 rental crawler for the Mueller / MLK Jr Station area.

Runs three things in one process on fatman:

  crawler    every `crawl.interval_min`, sweeps the enabled sources, scores
             what is in scope and stores it
  bot        Telegram commander + push alerts on high scorers
  dashboard  self-contained HTML on :8810, LAN and Tailscale

Stdlib only — no venv on the Pi, same rule as the other fleet services.

Usage
    ./agent.py              run the daemon (what systemd starts)
    ./agent.py crawl        one crawl, print the summary, exit
    ./agent.py top [n]      print the current best matches
    ./agent.py serve        dashboard only, no crawling
"""
import json
import logging
import os
import signal
import sys
import threading
import time

import bot as botmod
import dashboard
import sources
from crawler import crawl
from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
ENV = os.path.join(HERE, ".env")
DB = os.path.join(HERE, "data", "leasing.db")

log = logging.getLogger("leasing")


def load_config() -> dict:
    with open(CONFIG) as f:
        return json.load(f)


def load_secrets() -> dict:
    """Secrets come from .env only — never config.json, never git.

    Falls back to the botsmith registry when running on scootpc, so a local
    `./agent.py crawl` works without copying the token into a second place.
    """
    secrets = {}
    try:
        with open(ENV) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass

    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SCRAPER_API_KEY"):
        if os.environ.get(k):
            secrets[k] = os.environ[k]

    if not secrets.get("TELEGRAM_BOT_TOKEN"):
        name = secrets.get("BOTSMITH_NAME", "leasing-agent")
        reg = os.path.expanduser("~/.config/botsmith/registry.json")
        try:
            with open(reg) as f:
                entry = json.load(f).get(name)
            if entry:
                secrets.setdefault("TELEGRAM_BOT_TOKEN", entry["token"])
                secrets.setdefault("TELEGRAM_CHAT_ID", str(entry["chat_id"]))
                log.info("using botsmith registry entry %r", name)
        except (OSError, ValueError, KeyError):
            pass
    return secrets


def setup_logging(level=logging.INFO):
    logging.basicConfig(
        level=level, stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%H:%M:%S")


def build(cfg=None):
    cfg = cfg or load_config()
    secrets = load_secrets()
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    store = Store(DB)
    ctx = sources.Ctx(cfg, secrets, store, logging.getLogger("http"))
    return cfg, secrets, store, ctx


def main():
    setup_logging()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daemon"
    cfg, secrets, store, ctx = build()

    if cmd == "crawl":
        res = crawl(ctx, store, cfg)
        print(json.dumps(res, indent=2))
        return 0

    if cmd == "top":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        for r in store.top(n):
            d = json.loads(r.get("distances") or "{}")
            print(f"{r['score']:5.1f}  ${r['price'] or 0:>6,}  "
                  f"{(r.get('beds') or 0):g}bd {(r.get('sqft') or 0):>5}sf  "
                  f"{d.get('mueller', '?')}mi/{d.get('mlk_station', '?')}mi  "
                  f"{(r.get('property_name') or r.get('title') or '')[:44]}")
        return 0

    if cmd == "serve":
        dashboard.serve(store, cfg)
        return 0

    if cmd != "daemon":
        print(__doc__)
        return 1

    # ── daemon ──────────────────────────────────────────────────────────────
    enabled = [m.NAME for m in sources.load(cfg)]
    log.info("leasing-agent starting — sources: %s", ", ".join(enabled) or "none")
    if not secrets.get("SCRAPER_API_KEY"):
        log.info("no SCRAPER_API_KEY — zillow/hotpads stay off (captcha-walled)")

    stop = threading.Event()
    crawl_lock = threading.Lock()

    def crawl_now():
        """Serialised so a /crawl command cannot overlap the timer."""
        with crawl_lock:
            return crawl(ctx, store, cfg)

    tg = botmod.Bot(cfg, secrets, store, crawl_now)
    tg.start()

    threading.Thread(target=dashboard.serve, args=(store, cfg),
                     name="dashboard", daemon=True).start()

    def loop():
        interval = cfg["crawl"].get("interval_min", 90) * 60
        while not stop.is_set():
            try:
                res = crawl_now()
                sent = tg.alert_new(res)
                store.set_meta("last_crawl", time.strftime("%Y-%m-%dT%H:%M:%S"))
                log.info("cycle: %d in scope, %d new, %d alerts sent",
                         res["found"], res["new"], sent)
            except Exception:                       # noqa: BLE001
                log.exception("crawl cycle failed — continuing")
            stop.wait(interval)

    threading.Thread(target=loop, name="crawl-loop", daemon=True).start()

    def bye(*_):
        log.info("shutting down")
        stop.set()
        tg.stop()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    while not stop.is_set():
        stop.wait(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
