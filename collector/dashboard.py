"""Self-contained HTML overview page served by the collector daemon.

The page is intentionally dependency-free: inline CSS + vanilla JS, no build
step and no external network requests, so it renders behind a locked-down
reverse proxy or fully offline. It polls ``/api/status`` and derives the
collection rate + sparkline client-side, keeping the daemon stateless.
"""

from __future__ import annotations

# NOTE: plain triple-quoted string (not an f-string). Keep the embedded JS free
# of backslash escapes Python would reinterpret (\n, \t, \\, \"); use literal
# Unicode characters and String.fromCharCode where needed.
DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rettulf-pubdb collector</title>
<style>
  :root {
    --bg:#0f1419; --panel:#161b22; --border:#272e36; --fg:#e6edf3;
    --muted:#8b949e; --accent:#4fd1c5; --ok:#3fb950; --err:#f85149;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { display:flex; align-items:baseline; gap:.7rem; flex-wrap:wrap;
    padding:1rem 1.25rem; border-bottom:1px solid var(--border); }
  header h1 { font-size:1.05rem; margin:0; letter-spacing:.02em; }
  header .meta { color:var(--muted); font-size:.8rem; }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  #dot { width:.6rem; height:.6rem; border-radius:50%; background:var(--muted); display:inline-block; }
  #dot.live { background:var(--ok); }
  #dot.stale { background:var(--err); }
  main { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
    padding:1.25rem; max-width:1080px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:1rem 1.1rem; }
  .card h2 { font-size:.76rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
    margin:0 0 .8rem; font-weight:600; }
  .row { display:flex; justify-content:space-between; align-items:baseline; padding:.22rem 0; }
  .row .label { color:var(--muted); }
  .row .val { font-variant-numeric:tabular-nums; font-weight:600; }
  .big { font-size:1.9rem; font-weight:700; font-variant-numeric:tabular-nums; }
  .chips { display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.5rem; }
  .chip { font-size:.78rem; padding:.15rem .55rem; border-radius:999px; border:1px solid var(--border); color:var(--muted); }
  .chip b { color:var(--fg); }
  .bar { height:8px; border-radius:4px; background:var(--border); overflow:hidden; margin:.55rem 0; }
  .bar > i { display:block; height:100%; background:var(--accent); transition:width .4s ease; }
  table { width:100%; border-collapse:collapse; font-size:.8rem; }
  td,th { text-align:left; padding:.35rem .4rem; border-bottom:1px solid var(--border); vertical-align:top; }
  th { color:var(--muted); font-weight:600; }
  .errcell { color:var(--err); max-width:38ch; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .full { grid-column:1/-1; }
  svg { width:100%; height:48px; display:block; margin-top:.6rem; }
  #spark { fill:none; stroke:var(--accent); stroke-width:2; }
  .empty { color:var(--muted); font-style:italic; }
</style>
</head>
<body>
<header>
  <span id="dot"></span>
  <h1>rettulf-pubdb collector</h1>
  <span class="meta" id="meta"></span>
  <span class="meta" id="updated" style="margin-left:auto"></span>
</header>
<main>
  <section class="card">
    <h2>Queue</h2>
    <div id="queue-states"></div>
    <div class="chips" id="queue-variants"></div>
  </section>
  <section class="card">
    <h2>Throughput &amp; freshness</h2>
    <div class="big mono" id="entries">&ndash;</div>
    <div class="row"><span class="label">collection rate</span><span class="val mono" id="rate">&ndash;</span></div>
    <div class="row"><span class="label">last commit</span><span class="val mono" id="commit-age">&ndash;</span></div>
    <div class="row"><span class="label">pub.dev 429s</span><span class="val mono" id="r429">&ndash;</span></div>
    <div class="row"><span class="label">push conflicts</span><span class="val mono" id="conflicts">&ndash;</span></div>
    <svg viewBox="0 0 300 48" preserveAspectRatio="none"><polyline id="spark"/></svg>
  </section>
  <section class="card">
    <h2>Worklist coverage</h2>
    <div class="big mono" id="cov-pct">&ndash;</div>
    <div class="bar"><i id="cov-bar" style="width:0%"></i></div>
    <div class="row"><span class="label">packages with entries</span><span class="val mono" id="cov-pkgs">&ndash;</span></div>
    <div class="row"><span class="label">versions collected</span><span class="val mono" id="cov-vers">&ndash;</span></div>
  </section>
  <section class="card full">
    <h2>Recent failures</h2>
    <div id="failures"></div>
  </section>
</main>
<script>
var REFRESH_MS = 5000;
var MAX_SAMPLES = 120;
var history = [];
var lastOk = 0;
var NL = String.fromCharCode(10);

function $(id) { return document.getElementById(id); }
function setText(id, v) { $(id).textContent = v; }

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, function (c) {
    if (c === "&") return "&amp;";
    if (c === "<") return "&lt;";
    if (c === ">") return "&gt;";
    return "&quot;";
  });
}

function fmtAge(s) {
  if (s === undefined || s === null || s < 0) return "never";
  s = Math.floor(s);
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function setLive(ok) { $("dot").className = ok ? "live" : "stale"; }

function renderMeta(m) {
  setText("meta", "schema v" + m.pubdb_schema_version + " · " +
    m.workers + " worker" + (m.workers === 1 ? "" : "s") + " · push " +
    (m.push_enabled ? "on" : "off (dry-run)"));
}

function renderQueue(q) {
  var states = q.by_state || {};
  var keys = ["queued", "in_progress", "done", "failed"];
  var html = "";
  keys.forEach(function (k) {
    html += '<div class="row"><span class="label">' + k.replace("_", " ") +
      '</span><span class="val mono">' + (states[k] || 0) + '</span></div>';
  });
  $("queue-states").innerHTML = html;
  var v = q.by_variant || {};
  $("queue-variants").innerHTML =
    '<span class="chip">base <b>' + (v.base || 0) + '</b></span>' +
    '<span class="chip">obf <b>' + (v.obf || 0) + '</b></span>' +
    '<span class="chip">flutter <b>' + (v.flutter || 0) + '</b></span>';
}

function drawSpark() {
  if (history.length < 2) return;
  var vals = history.map(function (h) { return h.entries; });
  var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
  var span = (max - min) || 1;
  var n = vals.length;
  var pts = vals.map(function (v, i) {
    var x = (i / (n - 1)) * 300;
    var y = 46 - ((v - min) / span) * 44;
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  $("spark").setAttribute("points", pts);
}

function renderThroughput(t) {
  setText("entries", (t.entries_collected_total || 0).toLocaleString());
  setText("r429", t.pubdev_429_total || 0);
  setText("conflicts", t.publish_conflict_total || 0);
  setText("commit-age", fmtAge(t.last_commit_age_seconds));
  history.push({ t: Date.now(), entries: t.entries_collected_total || 0 });
  if (history.length > MAX_SAMPLES) history.shift();
  var rate = "–";
  if (history.length >= 2) {
    var a = history[0], b = history[history.length - 1];
    var dt = (b.t - a.t) / 1000;
    if (dt > 0) rate = ((b.entries - a.entries) / dt * 60).toFixed(2) + " /min";
  }
  setText("rate", rate);
  drawSpark();
}

function renderCoverage(c) {
  var pct = (c.percent_packages !== undefined && c.percent_packages !== null) ? c.percent_packages : 0;
  setText("cov-pct", pct + "%");
  $("cov-bar").style.width = pct + "%";
  setText("cov-pkgs", (c.packages_with_entries || 0) + " / " + (c.worklist_packages || 0));
  setText("cov-vers", (c.versions_collected || 0).toLocaleString());
}

function renderFailures(list) {
  var el = $("failures");
  if (!list || !list.length) {
    el.innerHTML = '<div class="empty">No failures.</div>';
    return;
  }
  var rows = list.map(function (f) {
    var id = f.package + ":" + f.version + ":" + f.variant;
    var full = f.last_error || "";
    var first = full.split(NL)[0];
    return '<tr><td class="mono">' + escapeHtml(id) + '</td>' +
      '<td class="mono">' + f.attempts + '</td>' +
      '<td class="errcell mono" title="' + escapeHtml(full) + '">' + escapeHtml(first) + '</td>' +
      '<td class="mono">' + escapeHtml(f.updated_at || "") + '</td></tr>';
  }).join("");
  el.innerHTML = '<table><thead><tr><th>item</th><th>tries</th>' +
    '<th>last error</th><th>updated</th></tr></thead><tbody>' + rows + '</tbody></table>';
}

async function poll() {
  try {
    var res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    var s = await res.json();
    renderMeta(s.meta || {});
    renderQueue(s.queue || {});
    renderThroughput(s.throughput || {});
    renderCoverage(s.coverage || {});
    renderFailures(s.recent_failures || []);
    lastOk = Date.now();
    setLive(true);
    setText("updated", "updated just now");
  } catch (e) {
    setLive(false);
    setText("updated", "unreachable — retrying");
  }
}

setInterval(function () {
  if (!lastOk) return;
  var ago = Math.round((Date.now() - lastOk) / 1000);
  if (ago > 0) setText("updated", "updated " + ago + "s ago");
  if (ago > (REFRESH_MS / 1000) * 3) setLive(false);
}, 1000);

poll();
setInterval(poll, REFRESH_MS);
</script>
</body>
</html>
"""
