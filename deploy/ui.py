"""Shared look for the ApplyPilot server pages: header, tabs, cards and buttons.

Colours follow the generated dashboard (slate background, blue accent, green
for good fits) so moving between pages feels like one site.
"""

from __future__ import annotations

import html

TABS = (
    ("/", "Jobs", "jobs"),
    ("/ready", "Ready to apply", "ready"),
    ("/applied", "Applied", "applied"),
    ("/autoapply", "Auto-apply", "auto"),
    ("/status", "Status", "status"),
    ("/setup", "Setup", "setup"),
)

PLANE_SVG = ('<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">'
             '<path d="M2.5 11.2 21 3.5l-7.7 18.5-2.4-7.4-8.4-3.4z" fill="currentColor"/></svg>')

TOPBAR_CSS = """
.ap-topbar{position:sticky;top:0;z-index:50;background:rgba(11,17,32,.88);
  -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);border-bottom:1px solid #26324a;
  font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.ap-topbar-in{max-width:1100px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:16px}
.ap-brand{display:flex;align-items:center;gap:9px;font-weight:700;color:#e6ebf5;text-decoration:none;
  font-size:17px;white-space:nowrap}
.ap-mark{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;color:#0b1120;
  background:linear-gradient(135deg,#5b9dff,#22c55e)}
.ap-tabs{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none;margin-left:auto}
.ap-tabs::-webkit-scrollbar{display:none}
.ap-tabs a{padding:7px 13px;border-radius:999px;color:#93a1b8;text-decoration:none;font-size:14px;
  font-weight:500;white-space:nowrap}
.ap-tabs a:hover{color:#e6ebf5;background:#1a2539}
.ap-tabs a.on{color:#0b1120;background:#5b9dff}
@media (max-width:560px){.ap-brand span.ap-name{display:none}.ap-tabs{margin-left:0}
  .ap-tabs a{padding:7px 10px;font-size:13.5px}.ap-topbar-in{gap:10px}}
/* on the generated dashboard: escape its body padding and span the full width */
.ap-dash{margin:-2rem -2rem 2rem}.ap-dash .ap-topbar-in{max-width:none;padding:10px 2rem}
@media (max-width:768px){.ap-dash{margin:-1rem -1rem 1.5rem}.ap-dash .ap-topbar-in{padding:10px 1rem}}
"""

PAGE_CSS = """
:root{--bg:#0b1120;--surface:#131c2e;--surface-2:#1a2539;--line:#26324a;--field:#0b1120;--text:#e6ebf5;
  --muted:#93a1b8;--accent:#5b9dff;--accent-ink:#0b1120;--good:#22c55e;--good-soft:#22c55e1a;--warn:#f59e0b}
*{box-sizing:border-box}
html,body{background:var(--bg)}
body{margin:0;color:var(--text);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
a{color:var(--accent)}
.wrap{max-width:760px;margin:0 auto;padding:26px 16px 56px}
h1{font-size:28px;line-height:1.2;margin:0 0 6px;letter-spacing:-.015em}
h2{font-size:18px;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 22px}
.muted{color:var(--muted)}.small{font-size:14px}.hint{color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px}
.card + .card,.stack > * + *{margin-top:14px}
.status{display:flex;gap:14px;align-items:flex-start}
.status p{margin:2px 0 0}
.status .head{font-size:18px;font-weight:600}
.dot{width:11px;height:11px;border-radius:50%;margin-top:8px;flex:none;background:var(--muted)}
.dot.run{background:var(--accent);animation:pulse 1.8s infinite}
.dot.ok{background:var(--good);padding:0;border:0;border-radius:50%;margin:8px 0 0}.dot.warn{background:var(--warn)}
@keyframes pulse{0%{box-shadow:0 0 0 0 #5b9dff99}70%{box-shadow:0 0 0 10px #5b9dff00}100%{box-shadow:0 0 0 0 #5b9dff00}}
@media (prefers-reduced-motion:reduce){.dot.run{animation:none}}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:14px 0}
@media (max-width:560px){.tiles{grid-template-columns:repeat(2,1fr)}}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 14px 12px}
.tile b{display:block;font-size:28px;line-height:1.1;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.tile span{color:var(--muted);font-size:13px}
.tile.hl b{color:var(--good)}.tile.txt b{font-size:18px;line-height:1.55}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:11px 18px;border-radius:10px;
  font-family:inherit;font-weight:600;font-size:15px;line-height:1.2;text-decoration:none;
  border:1px solid transparent;cursor:pointer}
.btn.primary{background:var(--accent);color:var(--accent-ink)}
.btn.primary:hover{background:#7ab0ff}
.btn.ghost{background:transparent;color:var(--text);border-color:var(--line)}
.btn.ghost:hover{background:var(--surface-2)}
.btn.good{background:var(--good-soft);color:var(--good);border-color:#22c55e55}
.btn.good:hover{background:#22c55e2e}
.btn.sm{padding:8px 13px;font-size:14px}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row .btn{flex:1 1 200px}.row .btn.sm{flex:1 1 120px}
.steps{counter-reset:s;list-style:none;padding:0;margin:4px 0 0}
.steps li{counter-increment:s;position:relative;padding:0 0 16px 42px}
.steps li:last-child{padding-bottom:0}
.steps li::before{content:counter(s);position:absolute;left:0;top:-1px;width:28px;height:28px;border-radius:50%;
  background:var(--surface-2);border:1px solid var(--line);display:grid;place-items:center;font-size:13px;
  font-weight:700;color:var(--accent)}
.steps b{color:var(--text)}
details>summary{cursor:pointer;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸";display:inline-block;width:1.1em;color:var(--muted);transition:transform .15s}
details[open]>summary::before{transform:rotate(90deg)}
.job{display:flex;flex-direction:column;gap:12px}
.job-top{display:flex;gap:13px;align-items:flex-start}
.score{flex:none;width:42px;height:42px;border-radius:11px;display:grid;place-items:center;font-weight:800;
  font-size:18px;color:#0b1120;background:var(--good)}
.score.mid{background:var(--warn)}
.job-title{font-weight:650;color:var(--text);text-decoration:none;font-size:16.5px;line-height:1.35}
.job-title:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:13.5px;margin-top:2px}
.review{background:#f59e0b14;border:1px solid #f59e0b55;border-radius:10px;padding:9px 12px;font-size:14px}
.review summary{color:var(--warn);font-weight:600}
.review p{margin:8px 0 2px;color:var(--text)}
.mark{margin:0}.mark .check{width:100%}
.applied-tag{color:var(--good);font-weight:600;font-size:14px;margin-right:10px}
.linkbtn{background:none;border:0;color:var(--accent);cursor:pointer;font:inherit;font-size:14px;padding:0;text-decoration:underline}
.job.done{opacity:.8}
.empty{text-align:center;padding:38px 20px}
.empty .big{font-size:40px;line-height:1;margin-bottom:10px}
.files a{display:flex;justify-content:space-between;gap:12px;padding:12px 2px;border-bottom:1px solid var(--line);
  color:var(--text);text-decoration:none}
.files a:hover{color:var(--accent)}
.files a span{color:var(--muted);font-size:13px;white-space:nowrap}
"""


def topbar(active: str = "") -> str:
    tabs = "".join(
        f'<a href="{href}"{" class=on" if key == active else ""}>{label}</a>' for href, label, key in TABS
    )
    return (f'<header class="ap-topbar"><div class="ap-topbar-in"><a class="ap-brand" href="/">'
            f'<span class="ap-mark">{PLANE_SVG}</span><span class="ap-name">ApplyPilot</span></a>'
            f'<nav class="ap-tabs">{tabs}</nav></div></header>')


def shell(title: str, body: str, active: str = "", extra_css: str = "", head: str = "") -> bytes:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
{head}<style>{PAGE_CSS}{TOPBAR_CSS}{extra_css}</style></head>
<body>{topbar(active)}<main class="wrap">{body}</main></body></html>""".encode()


def dashboard_with_topbar(dashboard_html: str) -> str:
    """Add the site header to the generated dashboard, which has its own 2rem body padding."""
    bar = topbar("jobs").replace('class="ap-topbar"', 'class="ap-topbar ap-dash"', 1)
    return dashboard_html.replace("</head>", f"<style>{TOPBAR_CSS}</style></head>", 1).replace(
        "<body>", "<body>" + bar, 1)
