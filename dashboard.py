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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import score
import walk

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
.split{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:22px}
.card.liked{border-color:#3f5136;background:#1d2318}
.card.disliked{border-color:#4a2f2b;opacity:.62}
.est{color:var(--faint);font-style:italic}
.fb{border-top:1px solid var(--rule);margin-top:10px;padding-top:8px}
.fb summary{list-style:none;cursor:pointer;font-family:var(--mono);font-size:10.5px;
letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
display:flex;align-items:center;gap:8px}
.fb summary::-webkit-details-marker{display:none}
.fb summary:hover{color:var(--gold)}
.fb summary .said{color:var(--faded);text-transform:none;letter-spacing:0;
font-style:italic;font-family:var(--serif);font-size:12px}
.fb[open] summary{color:var(--gold);margin-bottom:9px}
.fb textarea{width:100%;background:var(--paper);color:var(--ink);
border:1px solid var(--rule);border-radius:6px;padding:9px 11px;
font:13.5px/1.5 var(--serif);resize:vertical;min-height:62px}
.fb textarea:focus{outline:none;border-color:var(--gold)}
.fb .btns{display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap}
.fb button{font:inherit;font-size:12px;cursor:pointer;padding:6px 15px;
border-radius:6px;border:1px solid var(--rule);background:var(--paper);
color:var(--faded);transition:border-color .18s,color .18s}
.fb button:hover{border-color:var(--gold);color:var(--gold)}
.fb button.yes:hover{border-color:var(--live);color:var(--live)}
.fb button.no:hover{border-color:var(--warn);color:var(--warn)}
.fb .hint{font-family:var(--mono);font-size:10px;color:var(--faint)}
.note{background:var(--lift);border:1px solid var(--rule);border-left:2px solid var(--gold);
border-radius:6px;padding:11px 14px;margin-bottom:9px;font-size:13.5px}
.note .who{font-family:var(--mono);font-size:10px;letter-spacing:.12em;
text-transform:uppercase;color:var(--faint);margin-bottom:4px;
display:flex;gap:10px;flex-wrap:wrap}
.note.no{border-left-color:var(--warn)}
.note .quote{font-style:italic;color:var(--faded)}
.learn{background:var(--lift);border:1px solid var(--rule);border-radius:8px;
padding:14px 16px;font-size:13.5px}
.learn table{width:100%;border-collapse:collapse;margin-top:8px;
font-family:var(--mono);font-size:11.5px}
.learn td,.learn th{text-align:left;padding:4px 8px 4px 0;
border-bottom:1px solid var(--rule)}
.learn th{color:var(--faint);font-weight:400;letter-spacing:.1em;
text-transform:uppercase;font-size:9.5px}
.learn .up{color:var(--live)}
.learn .down{color:var(--warn)}
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


def section_value(fn, fallback):
    """Like `section`, but for data a page needs rather than markup.

    A failure here degrades the page — cards render without their verdicts —
    instead of taking the whole document down with it.
    """
    try:
        return fn()
    except Exception:                               # noqa: BLE001
        log.warning("data fetch failed:\n%s", traceback.format_exc())
        return fallback


def _anchor_meta(dists, key, label):
    """'0.8mi MLK stn · 17min walk', tolerating both stored distance shapes."""
    entry = dists.get(key)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        return f"{entry}mi {label}"          # pre-walk-routing rows
    miles = entry.get("mi")
    if miles is None:
        return None
    out = f"{miles}mi {label}"
    if entry.get("walk_min") is not None:
        out += f" ({entry['walk_min']:.0f}min walk)"
    elif entry.get("basis") == "straight":
        out += " (direct)"
    return out


def card(r, show_drop=False, verdict=None, feedback=True):
    """One listing. `verdict` is the latest like/dislike, if any.

    The card is a div rather than an anchor because the feedback form lives
    inside it, and a form nested in an `<a>` is invalid HTML that browsers
    resolve by dropping one or the other.
    """
    dists = _loads(r.get("distances"))

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
        # An estimated size is marked so it is never read as measured.
        estimated = r.get("sqft_basis") == "estimated"
        size = f"~{r['sqft']:,}sf" if estimated else f"{r['sqft']:,}sf"
        meta.append(f'<span class="est">{size}</span>' if estimated else size)
        if r.get("price"):
            meta.append(f"${r['price'] / r['sqft']:.2f}/sf")
    for key, label in (("mueller", "Mueller"), ("mlk_station", "MLK stn")):
        got = _anchor_meta(dists, key, label)
        if got:
            meta.append(got)
    if r.get("walk_credit") is not None:
        meta.append(f"walk {r['walk_credit'] * 100:.0f} "
                    f"({walk.describe(r['walk_credit'])})")
    if r.get("unit"):
        meta.append(e(r["unit"]))
    if r.get("available"):
        meta.append(f"avail {e(r['available'])}")
    meta.append(e(r.get("source")))

    score = r.get("score") or 0
    lid = r["id"]
    klass = {"like": " liked", "dislike": " disliked"}.get(
        (verdict or {}).get("verdict"), "")

    return f"""<div class="card{klass}" id="l{e(lid[:8])}">
  <a href="{e(r.get('url') or '#')}" target="_blank" rel="noopener"
     style="text-decoration:none;display:block">
    <div class="row1"><span class="nm">{name}</span>{''.join(badges)}
      <span class="pr">{money(r.get('price'))}</span></div>
    <div class="meta">{'<span>' + '</span><span>'.join(meta) + '</span>'}</div>
    <div class="bar"><i style="width:{max(0, min(100, score)):.0f}%"></i></div>
    <div class="sc">{score:.0f} pts · {e(lid[:8])}</div>
  </a>
  {feedback_form(lid, verdict) if feedback else ''}
</div>"""


def feedback_form(lid, verdict=None):
    """The like / pass control, as a native disclosure widget.

    `<details>` gives us an expanding "bubble" with no JavaScript, which
    keeps the dashboard a single server-rendered document — nothing to break
    when it is loaded over Tailscale from a phone.
    """
    said = ""
    if verdict:
        mark = "liked" if verdict["verdict"] == "like" else "passed"
        reason = (verdict.get("reason") or "").strip()
        said = (f'<span class="said">{mark}'
                f'{" — " + e(reason[:90]) if reason else ""}</span>')

    return f"""<details class="fb">
  <summary>&#9825; rate {said}</summary>
  <form method="post" action="/feedback">
    <input type="hidden" name="id" value="{e(lid)}">
    <textarea name="reason" rows="2"
      placeholder="why? — layout, street, light, the walk to the station, the landlord…"></textarea>
    <div class="btns">
      <button class="yes" type="submit" name="verdict" value="like">&#9829; like</button>
      <button class="no" type="submit" name="verdict" value="dislike">&#10007; pass</button>
      <span class="hint">saved to the corpus &amp; fed to the tuner</span>
    </div>
  </form>
</details>"""


def cards(rows, empty, show_drop=False, verdicts=None, feedback=True):
    if not rows:
        return f'<p class="empty">{e(empty)}</p>'
    verdicts = verdicts or {}
    return "".join(card(r, show_drop, verdicts.get(r["id"]), feedback)
                   for r in rows)


def _loads(raw, default=None):
    """json.loads that returns `default` instead of raising on junk."""
    try:
        return json.loads(raw) if raw else (default if default is not None else {})
    except (ValueError, TypeError):
        return default if default is not None else {}


def render(store, cfg) -> str:
    runs = store.last_runs(1)
    last = runs[0] if runs else None
    verdicts = section_value(store.latest_feedback, {})

    def tiles():
        st = store.stats()
        budget = cfg["search"]
        fb = store.feedback_counts()
        out = [
            ("in scope", str(st["active"] or 0),
             f"{budget['budget_min']}–{budget['budget_max']} · "
             f"{budget['beds_min']}+bd · {budget['radius_mi']}mi"),
            ("houses", str(st["houses"] or 0), "incl. townhome / duplex"),
            ("apartments", str(st["apts"] or 0), "complex floorplans"),
            ("avg rent", money(st["avg_price"]),
             f"cheapest {money(st['min_price'])}"),
            ("best $/sf", f"${st['best_ppsf']:.2f}" if st.get("best_ppsf")
             else "—", "most space per dollar"),
            ("rated", f"{fb['like']}/{fb['like'] + fb['dislike']}",
             f"{fb['dislike']} passed"),
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
        return cards(rows, "", verdicts=verdicts)

    def notes():
        rows = [r for r in store.all_feedback(limit=14) if (r.get("reason") or "").strip()]
        if not rows:
            return ('<p class="empty">no notes yet — hit <em>rate</em> on any '
                    'listing above, say what you thought, and it lands here '
                    'and in the tuner below.</p>')
        out = []
        for r in rows:
            name = e(r.get("property_name") or r.get("title") or r["listing_id"][:8])
            klass = " no" if r["verdict"] == "dislike" else ""
            mark = "&#9829; liked" if r["verdict"] == "like" else "&#10007; passed"
            price = money(r.get("price")) if r.get("price") else ""
            link = (f'<a href="{e(r["url"])}" target="_blank" rel="noopener">{name}</a>'
                    if r.get("url") else name)
            out.append(
                f'<div class="note{klass}"><div class="who"><span>{mark}</span>'
                f'<span>{link}</span><span>{e(price)}</span>'
                f'<span>{e(r["created"][:16].replace("T", " "))}</span>'
                f'<span>via {e(r.get("source") or "?")}</span></div>'
                f'<div class="quote">{e(r["reason"])}</div></div>')
        return "".join(out)

    def tuner():
        import learn
        analysis = learn.analyse(store, cfg)
        counts = (f'{analysis["n_like"]} liked &middot; '
                  f'{analysis["n_dislike"]} passed')

        if not analysis["components"]:
            return (f'<div class="learn">{counts}<br><br>Rate a few listings '
                    'and this panel starts comparing what the liked ones have '
                    'in common against the passed ones, component by '
                    'component.</div>')

        rows = "".join(
            f'<tr><td>{e(c["component"])}</td><td>{c["like_mean"]:.2f}</td>'
            f'<td>{c["dislike_mean"]:.2f}</td>'
            f'<td class="{"up" if c["effect"] > 0 else "down"}">'
            f'{c["effect"]:+.2f}</td><td>{c["weight"]:g}</td></tr>'
            for c in analysis["components"])

        if not analysis["enough"]:
            verdict_html = (
                f'<p style="margin-top:10px;color:var(--faint)">Not enough to '
                f'act on yet — {analysis["min_per_side"]} of each verdict are '
                'needed before any weight is changed. Effect sizes this thin '
                'are noise.</p>')
        elif analysis["suggestions"]:
            items = "".join(
                f'<li>{e(s["component"])}: <b>{s["from"]:g} &rarr; '
                f'{s["to"]:g}</b> — {e(s["why"])}</li>'
                for s in analysis["suggestions"])
            verdict_html = (
                f'<p style="margin-top:12px">Suggested reweighting:</p>'
                f'<ul style="margin:6px 0 0 18px;line-height:1.7">{items}</ul>'
                '<p style="margin-top:10px;color:var(--faint);font-size:12px">'
                'Apply on fatman with <code>python3 learn.py --apply</code> '
                '(keeps a backup); the next crawl rescores everything.</p>')
        else:
            verdict_html = ('<p style="margin-top:10px;color:var(--faint)">'
                            'No component separates likes from passes strongly '
                            'enough to justify a change. The weights look '
                            'about right.</p>')

        themes = []
        for verdict in ("like", "dislike"):
            words = analysis["themes"][verdict]
            if words:
                themes.append(f'<b>{verdict}s</b>: ' + ", ".join(
                    f'{e(t["word"])} &times;{t["n"]}' for t in words))
        themes_html = (f'<p style="margin-top:10px;font-size:12px;'
                       f'color:var(--faded)">{" &nbsp;&middot;&nbsp; ".join(themes)}'
                       '</p>') if themes else ""

        return (f'<div class="learn">{counts}'
                '<table><tr><th>component</th><th>liked</th><th>passed</th>'
                f'<th>effect</th><th>weight</th></tr>{rows}</table>'
                f'{verdict_html}{themes_html}</div>')

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
{section(lambda: cards(store.top(12), "nothing in scope yet — run /crawl",
                       verdicts=verdicts))}

<div class="split">
<div>
<h2><span class="num">III</span> Houses</h2>
{section(lambda: cards(store.top(8, prop_types=score.HOUSE_TYPES),
                       "no houses in range yet", verdicts=verdicts))}
</div>
<div>
<h2><span class="num">IV</span> Townhomes</h2>
{section(lambda: cards(store.top(8, prop_types=score.TOWNHOUSE_TYPES),
                       "no townhomes in range yet", verdicts=verdicts))}
</div>
<div>
<h2><span class="num">V</span> Apartments</h2>
{section(lambda: cards(store.top(8, prop_types=score.APARTMENT_TYPES),
                       "no apartments in range yet", verdicts=verdicts))}
</div>
</div>

<h2><span class="num">VI</span> Rent cuts</h2>
{section(lambda: cards(store.price_drops(6), "no price drops seen yet", True,
                       verdicts=verdicts))}

<h2><span class="num">VII</span> Pinned</h2>
{section(lambda: cards(store.pinned(), "nothing pinned — /pin <id> in Telegram",
                       verdicts=verdicts))}

<h2><span class="num">VIII</span> Your baselines</h2>
{section(baselines)}

<h2><span class="num">IX</span> What you said</h2>
{section(notes)}

<h2><span class="num">X</span> What that implies</h2>
{section(tuner)}

<h2><span class="num">XI</span> Sources</h2>
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

        def _redirect(self, to):
            """303 back to the page, so a refresh cannot re-submit the form."""
            self.send_response(303)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            self.end_headers()

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
                elif path == "/api/feedback":
                    body = json.dumps(store.all_feedback(500),
                                      default=str).encode()
                    self._send(body, "application/json")
                elif path == "/api/learn":
                    import learn
                    body = json.dumps(learn.analyse(store, cfg),
                                      default=str).encode()
                    self._send(body, "application/json")
                else:
                    self._send(b"not found", "text/plain", 404)
            except Exception:                       # noqa: BLE001
                log.exception("request failed")
                self._send(b"internal error", "text/plain", 500)

        def do_POST(self):
            """The only writable route: recording a like/dislike."""
            if self.path.split("?")[0] != "/feedback":
                self._send(b"not found", "text/plain", 404)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                # A reason is free text from a human; cap it so a stuck
                # client cannot post a gigabyte into the database.
                raw = self.rfile.read(min(length, 64_000)).decode(
                    "utf-8", "replace")
                form = urllib.parse.parse_qs(raw)
                lid = (form.get("id") or [""])[0].strip()
                verdict = (form.get("verdict") or [""])[0].strip()
                reason = (form.get("reason") or [""])[0].strip()

                if verdict not in ("like", "dislike") or not lid:
                    self._send(b"bad request", "text/plain", 400)
                    return
                if not store.find(lid):
                    self._send(b"no such listing", "text/plain", 404)
                    return

                store.add_feedback(lid, verdict, reason, source="dashboard")
                log.info("feedback: %s %s (%d chars)", verdict, lid[:8],
                         len(reason))
                # Anchor back to the card that was just rated.
                self._redirect(f"/#l{lid[:8]}")
            except Exception:                       # noqa: BLE001
                log.exception("feedback POST failed")
                self._send(b"internal error", "text/plain", 500)

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    log.info("dashboard on http://%s:%d/", host, port)
    srv.serve_forever()
