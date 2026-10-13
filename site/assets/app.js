/* match-predictor (Phase A): W/D/L and scoreline predictions. Renders from data/*.json. */
(function () {
  "use strict";
  const page = document.body.dataset.page;
  const app = document.getElementById("app");

  // ---------- helpers ----------
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (p, d = 0) => (p == null ? "–" : (p * 100).toFixed(d) + "%");
  const num = (x, d = 3) => (x == null ? "–" : Number(x).toFixed(d));
  const dash = s => esc(String(s || "").replace("-", "–"));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };
  const localDay = m => (m.kickoff_utc ? new Date(m.kickoff_utc) : new Date(m.date + "T12:00:00Z"));
  const dayKey = d => d.toLocaleDateString(undefined, { year: "numeric", month: "2-digit", day: "2-digit" });
  function dayLabel(d) {
    const t = new Date(), y = new Date(); y.setDate(t.getDate() - 1);
    const tm = new Date(); tm.setDate(t.getDate() + 1);
    if (dayKey(d) === dayKey(t)) return "Today";
    if (dayKey(d) === dayKey(tm)) return "Tomorrow";
    if (dayKey(d) === dayKey(y)) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" });
  }
  const timeOf = m => (m.kickoff_utc ? new Date(m.kickoff_utc).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "");
  async function load(path) {
    const r = await fetch(path, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  }
  const fail = e => {
    const missing = /HTTP 404/.test(e.message);
    app.innerHTML = `<div class="empty">${missing ? "No data yet. The pipeline hasn't run." : "Could not load data."}<br><span class="small">${esc(e.message)}</span></div>`;
  };
  const notesBlock = d => (!d.notes || !d.notes.length) ? "" :
    `<details class="notes"><summary>Data notes (${d.notes.length})</summary><ul>${d.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></details>`;
  const badge = (ok, label) => `<span class="badge ${ok ? "hit" : "miss"}">${ok ? "✓" : "✗"} ${label}</span>`;

  // ---------- match card ----------
  function card(m) {
    const p = m.prediction;
    let right = `<span class="muted">TBD</span>`;
    if (m.result) right = `<b>FT ${m.result.home}–${m.result.away}</b>`;
    else if (m.status === "SCHEDULED" && m.kickoff_utc && m.kickoff_utc < new Date().toISOString()) right = `<span class="muted">awaiting result</span>`;
    let body = `<div class="muted small" style="margin-top:8px">No prediction (no rating available for one of the teams).</div>`;
    if (p) {
      const abs = (p.absences || []).filter(a => a.status === "out" || a.status === "bench");
      const keyAbs = abs.sort((a, b) => (b.gk - a.gk) || ((b.attack_share || 0) - (a.attack_share || 0))).slice(0, 4);
      body = `
        <div class="wdl"><div class="wdl-bar"><span class="h" style="width:${p.p_home * 100}%"></span><span class="d" style="width:${p.p_draw * 100}%"></span><span class="a" style="width:${p.p_away * 100}%"></span></div>
          <div class="wdl-labels"><span><b>${pct(p.p_home)}</b> win</span><span>draw <b>${pct(p.p_draw)}</b></span><span>win <b>${pct(p.p_away)}</b></span></div></div>
        <div class="scores">${p.top_scores.map((t, i) => `<span class="score-chip ${i === 0 ? "top" : ""}">${dash(t.score)} <span class="muted">${pct(t.prob)}</span></span>`).join("")}</div>
        <div class="meta small">
          ${keyAbs.length ? `<div><span class="muted">Key absences:</span> ${keyAbs.map(a => `${esc(a.player)}${a.gk ? " (GK)" : ""}${a.status === "bench" ? " (bench)" : ""} <span class="muted">${esc(a.team)}</span>`).join(", ")}${p.lineups_confirmed ? ` <span class="badge">lineups confirmed</span>` : ""}</div>` : ""}
          <div><span class="muted">Referee:</span> ${m.referee ? esc(m.referee) : "not announced"}</div>
          <div class="muted">xG ${num(p.xg.home, 2)}–${num(p.xg.away, 2)} · ${esc(p.source || "")}${(p.thin_data || []).length ? ` · thin data: ${esc(p.thin_data.join(", "))}` : ""}</div>
        </div>
        ${m.hit ? `<div class="hits">${badge(m.hit.result, "result")} ${badge(m.hit.exact, "exact score")} ${badge(m.hit.top3, "top 3")}</div>` : ""}`;
    }
    return `<article class="card">
      <div class="card-head"><span class="comp">${esc(m.badge)}</span><span>${esc(timeOf(m))}</span></div>
      <div class="teams"><div class="team">${esc(m.home)}</div>
        <div class="score">${p ? `<div class="pred">${dash(p.predicted_score)}</div><div class="label">most likely</div>` : ""}<div class="result">${right}</div></div>
        <div class="team away">${esc(m.away)}</div></div>
      ${body}
    </article>`;
  }

  function grouped(list) {
    const groups = [];
    for (const m of list) {
      const d = localDay(m), k = dayKey(d);
      if (!groups.length || groups[groups.length - 1].k !== k) groups.push({ k, label: dayLabel(d), items: [] });
      groups[groups.length - 1].items.push(m);
    }
    return groups.map(g => `<h3 class="day">${esc(g.label)}</h3>${g.items.map(card).join("")}`).join("");
  }

  async function indexPage() {
    const d = await load("data/predictions.json");
    const key = "mp.comp";
    let cur = store.get(key) || "all";
    const order = Object.keys(d.competitions);
    const present = [...new Set(d.matches.map(m => m.comp))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
    if (cur !== "all" && cur !== "mine" && !present.includes(cur)) cur = "all";
    const tracked = new Set(d.tracked_teams || []);
    const draw = () => {
      const chips = [["all", "All"], ["mine", "Tracked clubs"], ...present.map(c => [c, (d.competitions[c] || {}).badge || c])]
        .map(([k, n]) => `<button class="chip ${k === cur ? "on" : ""}" data-c="${esc(k)}">${esc(n)}</button>`).join("");
      const ms = d.matches.filter(m => cur === "all" || (cur === "mine" ? (tracked.has(m.home) || tracked.has(m.away)) : m.comp === cur));
      const now = new Date().toISOString();
      const upcoming = ms.filter(m => !m.result && (!m.kickoff_utc || m.kickoff_utc > now));
      const played = ms.filter(m => !upcoming.includes(m)).sort((a, b) => (localDay(b) - localDay(a)));
      app.innerHTML = `${notesBlock(d)}<div class="filters">${chips}</div>
        <h2>Upcoming</h2>${upcoming.length ? grouped(upcoming) : `<div class="empty">No fixtures in the next 7 days.</div>`}
        <h2>Results</h2>${played.length ? grouped(played) : `<div class="empty">No results in the last 7 days.</div>`}
        <p class="muted small">Updated ${esc(new Date(d.generated_at).toLocaleString())} · model ${esc(d.model_version)}</p>`;
      app.querySelectorAll("[data-c]").forEach(b => b.addEventListener("click", () => { cur = b.dataset.c; store.set(key, cur); draw(); }));
    };
    draw();
  }

  // ---------- accuracy page ----------
  const tile = (k, v, s) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;
  const metricTiles = (m, mk) => `<div class="tiles">
    ${tile("Result hit rate", pct(m.accuracy_1x2), mk && mk.n ? `bookmakers: ${pct(mk.accuracy_1x2)}` : "favourite won")}
    ${tile("Exact score", pct(m.exact_score_hit), "most likely score")}
    ${tile("Top-3 score", pct(m.top3_score_hit), "actual in top 3")}
    ${tile("Brier (1X2)", num(m.brier_1x2), mk && mk.n ? `bookmakers: ${num(mk.brier_1x2)}` : "lower is better")}
    ${tile("Log loss (1X2)", num(m.log_loss_1x2), mk && mk.n ? `bookmakers: ${num(mk.log_loss_1x2)}` : "lower is better")}
    ${tile("Matches", (m.n || 0).toLocaleString())}</div>`;

  function calibrationTable(cal) {
    if (!cal) return "";
    const rows = [];
    for (const [k, label] of [["home", "Home win"], ["draw", "Draw"], ["away", "Away win"]]) {
      for (const b of (cal[k] || []).filter(b => b.n >= 10)) rows.push(`<tr><td>${label}</td><td>${esc(b.bin)}</td><td>${b.n}</td><td>${pct(b.predicted, 1)}</td><td>${pct(b.observed, 1)}</td><td class="${Math.abs(b.observed - b.predicted) > 0.05 && b.n >= 30 ? "neg" : ""}">${((b.observed - b.predicted) * 100).toFixed(1)}</td></tr>`);
    }
    return `<div class="table-wrap"><table><tr><th>Outcome</th><th>Predicted</th><th>n</th><th>Avg predicted</th><th>Happened</th><th>Gap (pts)</th></tr>${rows.join("")}</table></div>
      <p class="muted small">Well calibrated = "Avg predicted" ≈ "Happened". Gaps over 5 points on 30+ matches are flagged; buckets under 10 matches are hidden.</p>`;
  }

  function matchTable(list, limit) {
    const rows = list.slice(0, limit).map(r => {
      const act = r.score || `${r.fthg}-${r.ftag}`;
      const fav = ["home", "draw", "away"].reduce((a, b) => (r[`p_${b}`] > r[`p_${a}`] ? b : a), "home");
      const out = r.fthg != null ? (r.fthg > r.ftag ? "home" : r.fthg < r.ftag ? "away" : "draw") : null;
      const rh = r.result_hit ?? (fav === out), ex = r.exact_hit ?? (r.predicted_score === act), t3 = r.top3_hit ?? (r.top_scores || []).includes(act);
      return `<tr><td>${esc(r.date)}</td><td>${esc(r.comp || r.league)}</td><td class="l">${esc(r.home)} v ${esc(r.away)}</td><td>${pct(r.p_home)}/${pct(r.p_draw)}/${pct(r.p_away)}</td><td>${dash(r.predicted_score)}</td><td><b>${dash(act)}</b></td><td>${rh ? "✓" : "✗"}</td><td>${ex ? "✓" : "–"}</td><td>${t3 ? "✓" : "–"}</td></tr>`;
    }).join("");
    return `<div class="table-wrap"><table><tr><th>Date</th><th>Comp</th><th class="l">Match</th><th>H/D/A</th><th>Pred</th><th>Actual</th><th>Res</th><th>Exact</th><th>Top 3</th></tr>${rows}</table></div>`;
  }

  async function accuracyPage() {
    const [bt, acc] = await Promise.all([load("data/backtest.json").catch(() => null), load("data/accuracy.json").catch(() => null)]);
    let html = `<h1>Accuracy</h1>`;
    if (bt && bt.summary) {
      html += `<section class="summary"><h2>In plain English</h2><ul>${bt.summary.map(s => `<li>${esc(s)}</li>`).join("")}</ul></section>`;
      const t = bt.test;
      html += `<h2>Backtest · ${esc(bt.test_season_label)} league matches</h2><p class="muted small">Walk-forward: before each match week the model is refitted on earlier matches only. Settings were tuned on ${esc(bt.tune_season_label)}. Bookmaker = closing odds with the margin removed (an accuracy yardstick, not betting).</p>
        ${metricTiles(t.selected, t.market)}`;
      html += `<h2>What each factor adds</h2><div class="table-wrap"><table><tr><th class="l">Variant</th><th>Result</th><th>Exact</th><th>Top 3</th><th>Brier</th><th>Log loss</th><th>Δ vs base</th></tr>
        ${t.ablation.map(a => `<tr><td class="l">${esc(a.variant)}</td><td>${pct(a.accuracy_1x2, 1)}</td><td>${pct(a.exact_score_hit, 1)}</td><td>${pct(a.top3_score_hit, 1)}</td><td>${num(a.brier_1x2, 4)}</td><td>${num(a.log_loss_1x2, 4)}</td><td class="${a.delta_log_loss_vs_base < 0 ? "pos" : a.delta_log_loss_vs_base > 0 ? "neg" : ""}">${a.delta_log_loss_vs_base == null ? "" : (a.delta_log_loss_vs_base > 0 ? "+" : "") + num(a.delta_log_loss_vs_base, 4)}</td></tr>`).join("")}
        ${t.market && t.market.n ? `<tr><td class="l">Closing bookmaker odds (${t.market.n} matches)</td><td>${pct(t.market.accuracy_1x2, 1)}</td><td>–</td><td>–</td><td>${num(t.market.brier_1x2, 4)}</td><td>${num(t.market.log_loss_1x2, 4)}</td><td></td></tr>` : ""}
        </table></div><p class="muted small">Negative Δ log loss = more accurate than the base model. A factor stays on only if it helped on ${esc(bt.tune_season_label)}.</p>`;
      const bl = t.by_league || {};
      html += `<h2>By league</h2><div class="table-wrap"><table><tr><th class="l">League</th><th>n</th><th>Result</th><th>Exact</th><th>Top 3</th><th>Log loss</th><th>Bookmaker</th></tr>
        ${Object.entries(bl).filter(([, v]) => v.model && v.model.n).map(([k, v]) => `<tr><td class="l">${esc(k)}</td><td>${v.model.n}</td><td>${pct(v.model.accuracy_1x2, 1)}</td><td>${pct(v.model.exact_score_hit, 1)}</td><td>${pct(v.model.top3_score_hit, 1)}</td><td>${num(v.model.log_loss_1x2)}</td><td>${v.market && v.market.n ? num(v.market.log_loss_1x2) : "n/a"}</td></tr>`).join("")}
        </table></div>`;
      if (bt.ucl) html += `<h2>Champions League · ${esc(bt.ucl.season_label)}</h2>${metricTiles(bt.ucl.selected)}<p class="muted small">${esc(bt.ucl.method || "")}, refitted weekly on earlier results only.${bt.ucl.elo_only ? ` Elo only: result ${pct(bt.ucl.elo_only.accuracy_1x2)}, log loss ${num(bt.ucl.elo_only.log_loss_1x2)}.` : ""}</p>`;
      html += `<h2>Calibration</h2>${calibrationTable(t.calibration_1x2)}`;
    } else html += `<div class="empty">No backtest yet.</div>`;

    if (acc && acc.live) {
      html += `<h2>Live tracking</h2><p class="muted small">Every match predicted before kickoff on this site, scored after the final whistle.</p>${acc.live.n ? metricTiles(acc.live) : `<div class="empty small">No finished predicted matches yet.</div>`}`;
      if (acc.live_matches && acc.live_matches.length) html += `<h3>Match by match (live)</h3>${matchTable(acc.live_matches, 300)}`;
    }
    if (bt && bt.matches) {
      const lgs = [...new Set(bt.matches.map(r => r.league))];
      html += `<h3>Match by match (backtest ${esc(bt.test_season_label)})</h3><div class="filters" id="btf">${["All", ...lgs].map((l, i) => `<button class="chip ${i === 0 ? "on" : ""}" data-l="${esc(l)}">${esc(l)}</button>`).join("")}</div><div id="bt-table"></div>`;
    }
    html += `<p class="muted small">Updated ${esc(new Date((acc && acc.generated_at) || Date.now()).toLocaleString())}</p>`;
    app.innerHTML = html;
    if (bt && bt.matches) {
      let lim = 100, lg = "All";
      const sorted = bt.matches.slice().sort((a, b) => (a.date < b.date ? 1 : -1));
      const render = () => {
        const list = sorted.filter(r => lg === "All" || r.league === lg);
        document.getElementById("bt-table").innerHTML = matchTable(list, lim) + (list.length > lim ? `<p><button class="chip" id="more">Show more (${list.length - lim} left)</button></p>` : "");
        const more = document.getElementById("more");
        if (more) more.addEventListener("click", () => { lim += 200; render(); });
      };
      document.querySelectorAll("#btf [data-l]").forEach(b => b.addEventListener("click", () => {
        lg = b.dataset.l; lim = 100;
        document.querySelectorAll("#btf .chip").forEach(c => c.classList.toggle("on", c === b));
        render();
      }));
      render();
    }
  }

  ({ index: indexPage, accuracy: accuracyPage }[page] || indexPage)().catch(fail);
})();
