"""bot — Telegram control plane for the leasing agent.

Same shape as the fleet's fleetbot: stdlib urllib, one long-poll loop, and
only the configured chat id is honoured. This bot gets its *own* token from
botsmith rather than sharing the fleet bot's — two getUpdates pollers on one
token fight over the offset, so per-project bots stay separate.

Push alerts fire after a crawl for listings above the score threshold that we
have not alerted on before, capped per run and silenced during quiet hours.
"""
import html
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("bot")
TG_API = "https://api.telegram.org/bot{token}/{method}"

HELP = """\
<b>leasing agent</b>
/top [n] — best-scoring listings right now
/new [n] — most recently found
/houses — houses, townhomes &amp; duplexes only
/apts — apartment complexes only
/drops — rent cuts since we first saw them
/near [mi] — closest to MLK Jr Station
/detail &lt;id&gt; — everything on one listing
/pin &lt;id&gt; [note] · /unpin &lt;id&gt; · /pinned
/crawl — run a crawl now
/sources — per-source health from the last run
/stats — corpus summary + baselines
/help — this"""


def tg_call(token, method, params, timeout=20):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(TG_API.format(token=token, method=method),
                                 data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        import json
        return json.load(r)


def esc(s):
    return html.escape(str(s or ""), quote=False)


def money(n):
    return f"${n:,.0f}" if isinstance(n, (int, float)) else "—"


def fmt_listing(r, rank=None, verbose=False):
    """One listing as a Telegram HTML block."""
    import json
    head = esc(r.get("property_name") or r.get("title") or r.get("address")
               or "untitled")
    if r.get("url"):
        head = f'<a href="{esc(r["url"])}">{head}</a>'
    prefix = f"{rank}. " if rank else ""

    bits = [f"{money(r.get('price'))}"]
    if r.get("beds"):
        bits.append(f"{r['beds']:g}bd")
    if r.get("baths"):
        bits.append(f"{r['baths']:g}ba")
    if r.get("sqft"):
        bits.append(f"{r['sqft']:,}sf")
        if r.get("price"):
            bits.append(f"${r['price'] / r['sqft']:.2f}/sf")

    try:
        d = json.loads(r.get("distances") or "{}")
    except ValueError:
        d = {}
    geo = []
    if d.get("mueller") is not None:
        geo.append(f"{d['mueller']}mi Mueller")
    if d.get("mlk_station") is not None:
        geo.append(f"{d['mlk_station']}mi MLK stn")

    lines = [f"{prefix}<b>{head}</b>",
             f"   {' · '.join(bits)}",
             f"   {esc(r.get('prop_type'))} · {' · '.join(geo) or 'no geo'}"
             f" · <code>{r['id'][:8]}</code> · {r.get('score', 0):.0f}pts"]
    if r.get("address") and r.get("property_name"):
        lines.insert(1, f"   {esc(r['address'])}")
    if verbose:
        if r.get("unit"):
            lines.append(f"   floorplan {esc(r['unit'])}")
        if r.get("available"):
            lines.append(f"   available {esc(r['available'])}")
        if r.get("description"):
            lines.append(f"   <i>{esc(r['description'][:300])}</i>")
        lines.append(f"   source: {esc(r.get('source'))}")
    return "\n".join(lines)


def fmt_list(rows, title, empty="nothing matches yet", verbose=False):
    if not rows:
        return f"<b>{title}</b>\n{empty}"
    body = "\n\n".join(fmt_listing(r, i + 1, verbose)
                       for i, r in enumerate(rows))
    return f"<b>{title}</b>\n\n{body}"


class Bot:
    def __init__(self, cfg, secrets, store, crawl_now, log_=None):
        self.cfg = cfg
        self.token = secrets.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(secrets.get("TELEGRAM_CHAT_ID", ""))
        self.store = store
        self.crawl_now = crawl_now
        self.log = log_ or log
        self._offset = 0
        self._stop = threading.Event()

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    # ── outbound ────────────────────────────────────────────────────────────

    def send(self, text, chat_id=None):
        if not self.enabled:
            return False
        try:
            out = tg_call(self.token, "sendMessage", {
                "chat_id": chat_id or self.chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true"})
            return bool(out.get("ok"))
        except Exception as e:                      # noqa: BLE001
            self.log.warning("telegram send failed: %s", e)
            return False

    def in_quiet_hours(self):
        start, end = self.cfg["crawl"].get("quiet_hours", [22, 7])
        tz = ZoneInfo(self.cfg["crawl"].get("timezone", "America/Chicago"))
        h = datetime.now(tz).hour
        return start <= h or h < end if start > end else start <= h < end

    def alert_new(self, result):
        """Push the good stuff found by a crawl. Returns count sent."""
        crawl = self.cfg["crawl"]
        rows = self.store.unalerted(crawl.get("alert_min_score", 70),
                                    crawl.get("alert_max_per_run", 6))
        if not rows:
            return 0
        if self.in_quiet_hours():
            self.log.info("quiet hours — holding %d alerts", len(rows))
            return 0
        head = (f"🏠 <b>{len(rows)} new match"
                f"{'es' if len(rows) != 1 else ''}</b> "
                f"(scored ≥ {crawl.get('alert_min_score', 70)})")
        if self.send(fmt_list(rows, head)):
            self.store.mark_alerted([r["id"] for r in rows])
            return len(rows)
        return 0

    # ── inbound ─────────────────────────────────────────────────────────────

    def start(self):
        if not self.enabled:
            self.log.warning("telegram disabled — no token/chat id in .env")
            return
        threading.Thread(target=self._poll, name="tg-poll",
                         daemon=True).start()
        self.log.info("telegram commander polling")

    def stop(self):
        self._stop.set()

    def _poll(self):
        while not self._stop.is_set():
            try:
                out = tg_call(self.token, "getUpdates",
                              {"offset": self._offset + 1, "timeout": 25},
                              timeout=35)
                for u in out.get("result", []):
                    self._offset = max(self._offset, u["update_id"])
                    msg = u.get("message") or {}
                    chat = str((msg.get("chat") or {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    if chat != self.chat_id:
                        self.log.warning("ignoring message from chat %s", chat)
                        continue
                    try:
                        self.send(self.handle(text))
                    except Exception as e:          # noqa: BLE001
                        self.log.exception("command failed")
                        self.send(f"⚠️ {esc(type(e).__name__)}: {esc(e)}")
            except urllib.error.HTTPError as e:
                self.log.warning("getUpdates HTTP %s", e.code)
                time.sleep(10)
            except Exception as e:                  # noqa: BLE001
                self.log.warning("getUpdates: %s", e)
                time.sleep(5)

    def handle(self, text) -> str:
        parts = text.split()
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        args = parts[1:]

        def n_arg(default=5, cap=15):
            try:
                return max(1, min(int(args[0]), cap))
            except (IndexError, ValueError):
                return default

        if cmd in ("start", "help"):
            return HELP

        if cmd == "top":
            return fmt_list(self.store.top(n_arg()), "top matches")

        if cmd == "new":
            return fmt_list(self.store.newest(n_arg()), "most recently found")

        if cmd == "houses":
            rows = self.store.top(n_arg(), prop_types=(
                "house", "townhouse", "duplex", "condo"))
            return fmt_list(rows, "houses, townhomes & duplexes",
                            "no houses in range yet — the Mueller area runs "
                            "apartment-heavy, try /top or widen radius_mi")

        if cmd == "apts":
            return fmt_list(self.store.top(n_arg(), prop_types=("apartment",)),
                            "apartment complexes")

        if cmd == "drops":
            rows = self.store.price_drops(n_arg())
            if not rows:
                return "<b>rent cuts</b>\nnone seen yet"
            body = "\n\n".join(
                f"{fmt_listing(r, i + 1)}\n   was {money(r['old_price'])} → "
                f"now {money(r['price'])} "
                f"(−{money(r['old_price'] - r['price'])})"
                for i, r in enumerate(rows))
            return f"<b>rent cuts</b>\n\n{body}"

        if cmd == "near":
            try:
                lim = float(args[0])
            except (IndexError, ValueError):
                lim = 1.0
            import json as _json
            rows = [r for r in self.store.top(200)
                    if (_json.loads(r.get("distances") or "{}")
                        .get("mlk_station", 99)) <= lim]
            rows.sort(key=lambda r: _json.loads(r["distances"])["mlk_station"])
            return fmt_list(rows[:10], f"within {lim}mi of MLK Jr Station",
                            f"nothing within {lim}mi — try /near 1.5")

        if cmd == "detail":
            if not args:
                return "usage: /detail &lt;id&gt;"
            r = self.store.find(args[0])
            if not r:
                return f"no listing starting {esc(args[0])}"
            hist = self.store.history(r["id"])
            out = fmt_listing(r, verbose=True)
            if len(hist) > 1:
                out += "\n\n<b>price history</b>\n" + "\n".join(
                    f"   {h['seen_at'][:10]}  {money(h['price'])}" for h in hist)
            return out

        if cmd == "pin":
            if not args:
                return "usage: /pin &lt;id&gt; [note]"
            r = self.store.find(args[0])
            if not r:
                return f"no listing starting {esc(args[0])}"
            self.store.pin(r["id"], " ".join(args[1:]))
            return f"📌 pinned {esc(r.get('title') or r['id'][:8])}"

        if cmd == "unpin":
            if not args:
                return "usage: /unpin &lt;id&gt;"
            r = self.store.find(args[0])
            if r:
                self.store.unpin(r["id"])
            return "unpinned"

        if cmd == "pinned":
            return fmt_list(self.store.pinned(), "pinned", "nothing pinned")

        if cmd == "crawl":
            self.send("crawling…")
            res = self.crawl_now()
            lines = [f"<b>crawl done</b> — {res['found']} in scope, "
                     f"{res['new']} new, {res['drops']} price cuts"]
            for name, d in res["detail"].items():
                mark = "✓" if not d["error"] else "✗"
                lines.append(f"   {mark} {name}: {d['n']}"
                             + (f" — {esc(d['error'])}" if d["error"] else ""))
            sent = self.alert_new(res)
            if sent:
                lines.append(f"   pushed {sent} alert(s)")
            return "\n".join(lines)

        if cmd == "sources":
            runs = self.store.last_runs(1)
            if not runs:
                return "no crawls yet"
            import json as _json
            detail = _json.loads(runs[0].get("detail") or "{}")
            lines = [f"<b>sources</b> — last run {runs[0]['started']}"]
            for name, d in detail.items():
                if d.get("error"):
                    lines.append(f"   ✗ <b>{name}</b> — {esc(d['error'])}")
                else:
                    lines.append(f"   ✓ <b>{name}</b> — {d['n']} in scope "
                                 f"of {d.get('raw', '?')} raw")
            return "\n".join(lines)

        if cmd == "stats":
            s = self.store.stats()
            runs = self.store.last_runs(1)
            lines = [
                "<b>corpus</b>",
                f"   {s['active'] or 0} active of {s['n'] or 0} ever seen",
                f"   {s['houses'] or 0} housey · {s['apts'] or 0} apartments",
                f"   avg {money(s['avg_price'])} · cheapest {money(s['min_price'])}",
            ]
            base = self.store.baselines()
            if base:
                lines.append("\n<b>your baselines</b>")
                for b in base:
                    name = esc(b.get("property_name") or b.get("title"))
                    sqft = f" · {b['sqft']:,}sf" if b.get("sqft") else ""
                    lines.append(f"   {name} — {money(b['price'])}{sqft}")
            if runs:
                lines.append(f"\nlast crawl {runs[0]['started']}")
            return "\n".join(lines)

        return f"unknown command {esc(cmd)} — /help"
