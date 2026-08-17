"""dashboard — self-contained HTML view of the corpus, served from fatman.

Stdlib http.server like the rest of the fleet's dashboards: no venv, no CDN,
no build step. Rendered server-side on each request; the corpus is small
enough that this stays instant and it means no client-side JS to break.

Binds 0.0.0.0 so it is reachable both on the LAN and over Tailscale. Every
section degrades to an error tile rather than a 500 — same rule as
fleet-dashboard.
"""
import html
import json
import logging
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("dashboard")

CSS = """
:root{--paper:#1a1510;--lift:#221b14;--ink:#eee4d3;--faded:#a4977f;
--faint:#6b5f4e;--rule:#382f24;--gold:#d2a24c;--live:#8dbb7a;--warn:#c9756a;
--serif:"Palatino Linotype",Palatino,"URW Palladio L","Book Antiqua",Georgia,serif;
--mono:ui-monospace,"Cascadia Code","SF Mono",Menlo,monospace}
*{box-sizing:border-box;margin:0}
body{background:var(--paper);color:var(--ink);font:15px/1.6 var(--serif);
padding:48px 24px 40px;max-width:1000px;margin:0 auto;min-height:100vh}
a{color:inherit}
header{text-align:center;margin-bottom:38px}
h1{font-size:clamp(30px,6vw,42px);font-style:italic;font-weight:500;letter-spacing:-.01em}
.sub{margin-top:10px;font-size:11.5px;letter-spacing:.28em;text-transform:uppercase;color:var(--faded)}
.orn{display:flex;align-items:center;gap:16px;margin-top:20px;color:var(--gold);font-size:15px}
.orn::before,.orn::after{content:"";flex:1;height:1px;background:var(--rule)}
h2{display:flex;align-items:baseline;gap:12px;margin:38px 0 10px;font-size:12px;
font-weight:600;letter-spacing:.2em;text-transform:uppercase}
h2 .num{font-style:italic;font-weight:400;font-size:14px;letter-spacing:0;
text-transform:none;color:var(--gold);min-width:1.4em}
h2::after{content:"";flex:1;height:1px;background:var(--rule)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}
.tile{background:var(--lift);border:1px solid var(--rule);border-radius:8px;padding:12px 14px}
.tile .k{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--faded)}
.tile .v{font-size:23px;font-style:italic;margin-top:3px}
.tile .n{font-size:11.5px;color:var(--faint);font-family:var(--mono)}
.tile.bad{border-color:var(--warn)}
.tile.bad .v{font-size:13px;font-style:normal;color:var(--warn);line-height:1.35}
.card{display:block;text-decoration:none;background:var(--lift);border:1px solid var(--rule);
border-radius:8px;padding:13px 15px;margin-bottom:9px;transition:border-color .18s,background .18s}
.card:hover{border-color:var(--gold);background:#271f16}
.row1{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.nm{font-size:17px;font-weight:600}
.card:hover .nm{color:var(--gold)}
.pr{margin-left:auto;font-size:18px;font-style:italic;color:var(--gold);white-space:nowrap}
.meta{margin-top:4px;font-family:var(--mono);font-size:11.5px;color:var(--faded);
display:flex;gap:14px;flex-wrap:wrap}
.badge{font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;
padding:2px 6px;border:1px solid var(--rule);border-radius:4px;color:var(--faded)}
.badge.house{color:var(--live);border-color:#3f5136}
.badge.base{color:var(--gold);border-color:#5b452a}
.badge.drop{color:var(--warn);border-color:#5b3630}
.sc{font-family:var(--mono);font-size:11px;color:var(--faint)}
.bar{height:3px;background:var(--rule);border-radius:2px;margin-top:7px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--gold)}
.empty{color:var(--faint);font-size:13.5px;padding:10px 0}
.split{display:grid;grid-template-columns:1fr 1fr;gap:22px}
footer{margin-top:48px;text-align:center;font-size:11px;letter-spacing:.16em;
text-transform:uppercase;color:var(--faint)}
footer code{font-family:var(--mono);font-size:10.5px;text-transform:none;color:var(--faded)}
@media(max-width:720px){.split{grid-template-columns:1fr}body{padding:32px 14px}
.pr{margin-left:0}}
"""


def e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def money(n):
    return f"${n:,.0f}" if isinstance(n, (int, float)) else "—"


def section(fn):
    """Render a section, or an error tile if it blows up. Never a 500."""
    try:
        return fn()
    except Exception:                               # noqa: BLE001
        log.warning("section failed:\n%s", traceback.format_exc())
        return ('<div class="tile bad"><div class="k">section error</div>'
                f'<div class="v">{e(traceback.format_exc().strip().splitlines()[-1])}</div></div>')


def card(r, show_drop=False):
    dists = {}
    try:
        dists = json.loads(r.get("distances") or "{}")
    except ValueError:
        pass

    name = e(r.get("property_name") or r.get("title") or r.get("address")
             or "untitled")
    badges = []
    pt = r.get("prop_type") or "unknown"
    housey = pt in ("house", "townhouse", "duplex", "condo")
    badges.append(f'<span class="badge {"house" if housey else ""}">{e(pt)}</span>')
    if r.get("is_baseline"):
        badges.append('<span class="badge base">baseline</span>')
    if show_drop and r.get("old_price"):
        cut = r["old_price"] - r["price"]
        badges.append(f'<span class="badge drop">−{money(cut)}</span>')

    meta = []
    if r.get("beds"):
        meta.append(f"{r['beds']:g}bd")
    if r.get("baths"):
        meta.append(f"{r['baths']:g}ba")
    if r.get("sqft"):
        meta.append(f"{r['sqft']:,}sf")
        if r.get("price"):
            meta.append(f"${r['price'] / r['sqft']:.2f}/sf")
    if dists.get("mueller") is not None:
        meta.append(f"{dists['mueller']}mi Mueller")
    if dists.get("mlk_station") is not None:
        meta.append(f"{dists['mlk_station']}mi MLK stn")
    if r.get("unit"):
        meta.append(e(r["unit"]))
    if r.get("available"):
        meta.append(f"avail {e(r['available'])}")
    meta.append(e(r.get("source")))

    score = r.get("score") or 0
    return f"""<a class="card" href="{e(r.get('url') or '#')}" target="_blank" rel="noopener">
  <div class="row1"><span class="nm">{name}</span>{''.join(badges)}
    <span class="pr">{money(r.get('price'))}</span></div>
  <div class="meta">{'<span>' + '</span><span>'.join(meta) + '</span>'}</div>
  <div class="bar"><i style="width:{max(0, min(100, score)):.0f}%"></i></div>
  <div class="sc">{score:.0f} pts · {e(r['id'][:8])}</div>
</a>"""


def cards(rows, empty, show_drop=False):
    if not rows:
        return f'<p class="empty">{e(empty)}</p>'
    return "".join(card(r, show_drop) for r in rows)


def render(store, cfg) -> str:
    s = section(store.stats)
    runs = store.last_runs(1)
    last = runs[0] if runs else None

    def tiles():
        st = store.stats()
        budget = cfg["search"]
        out = [
            ("in scope", str(st["active"] or 0),
             f"{budget['budget_min']}–{budget['budget_max']} · "
             f"{budget['beds_min']}+bd · {budget['radius_mi']}mi"),
            ("houses", str(st["houses"] or 0), "incl. townhome / duplex"),
            ("apartments", str(st["apts"] or 0), "complex floorplans"),
            ("avg rent", money(st["avg_price"]),
             f"cheapest {money(st['min_price'])}"),
        ]
        return "".join(
            f'<div class="tile"><div class="k">{e(k)}</div>'
            f'<div class="v">{e(v)}</div><div class="n">{e(n)}</div></div>'
            for k, v, n in out)

    def sources_tiles():
        if not last:
            return '<p class="empty">no crawl has run yet</p>'
        detail = json.loads(last.get("detail") or "{}")
        if not detail:
            return '<p class="empty">no source detail recorded</p>'
        out = []
        for name, d in detail.items():
            if d.get("error"):
                out.append(f'<div class="tile bad"><div class="k">{e(name)}</div>'
                           f'<div class="v">{e(d["error"])}</div></div>')
            else:
                out.append(f'<div class="tile"><div class="k">{e(name)}</div>'
                           f'<div class="v">{d["n"]}</div>'
                           f'<div class="n">of {d.get("raw", "?")} raw</div></div>')
        return "".join(out)

    def baselines():
        rows = store.baselines()
        if not rows:
            return ('<p class="empty">neither The Platform nor Starlight has '
                    'appeared in a crawl yet — they are matched by name, so '
                    'they will show up here once a source lists them.</p>')
        return cards(rows, "")

    anchors = cfg["anchors"]
    anchor_note = " · ".join(
        f"{a['label']} ({a['lat']:.4f}, {a['lon']:.4f})" for a in anchors.values())

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>leasing agent</title><style>{CSS}</style></head><body>

<header>
  <h1>Leasing Agent</h1>
  <p class="sub">2 bedrooms &middot; near Mueller &amp; MLK Jr Station</p>
  <div class="orn">&#10087;</div>
</header>

<h2><span class="num">I</span> At a glance</h2>
<div class="tiles">{section(tiles)}</div>

<h2><span class="num">II</span> Best matches</h2>
{section(lambda: cards(store.top(12), "nothing in scope yet — run /crawl"))}

<div class="split">
<div>
<h2><span class="num">III</span> Houses</h2>
{section(lambda: cards(store.top(8, prop_types=("house", "townhouse", "duplex", "condo")),
                       "no houses in range yet"))}
</div>
<div>
<h2><span class="num">IV</span> Apartments</h2>
{section(lambda: cards(store.top(8, prop_types=("apartment",)),
                       "no apartments in range yet"))}
</div>
</div>

<h2><span class="num">V</span> Rent cuts</h2>
{section(lambda: cards(store.price_drops(6), "no price drops seen yet", True))}

<h2><span class="num">VI</span> Pinned</h2>
{section(lambda: cards(store.pinned(), "nothing pinned — /pin <id> in Telegram"))}

<h2><span class="num">VII</span> Your baselines</h2>
{section(baselines)}

<h2><span class="num">VIII</span> Sources</h2>
<div class="tiles">{section(sources_tiles)}</div>

<footer>
  <div class="orn">&#10087;</div>
  last crawl {e(last['started'] if last else 'never')} &middot;
  {e(last['found'] if last else 0)} in scope &middot;
  {e(last['new'] if last else 0)} new<br>
  <code>{e(anchor_note)}</code><br>
  <code>~/Desktop/leasing_agent on scootpc &rarr; fatman:leasing_agent</code>
</footer>
</body></html>"""


def serve(store, cfg, stop_event=None):
    host = cfg["dashboard"].get("host", "0.0.0.0")
    port = int(cfg["dashboard"].get("port", 8810))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            log.debug(fmt, *a)

        def _send(self, body: bytes, ctype: str, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/index.html"):
                    self._send(render(store, cfg).encode(),
                               "text/html; charset=utf-8")
                elif path == "/healthz":
                    self._send(b'{"ok":true}', "application/json")
                elif path == "/api/listings":
                    body = json.dumps(store.top(200), default=str).encode()
                    self._send(body, "application/json")
                elif path == "/api/runs":
                    body = json.dumps(store.last_runs(10), default=str).encode()
                    self._send(body, "application/json")
                else:
                    self._send(b"not found", "text/plain", 404)
            except Exception:                       # noqa: BLE001
                log.exception("request failed")
                self._send(b"internal error", "text/plain", 500)

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    log.info("dashboard on http://%s:%d/", host, port)
    srv.serve_forever()
