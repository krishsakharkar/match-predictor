/* Page: Accuracy — backtest summary, factor ablation, calibration, live tracking, match-by-match. */
(function () {
  "use strict";
  const app = document.getElementById("app");
  const { esc, pct, dash } = MP;
  const num = (x, d = 3) => MP.num(x, d);
  const load = MP.load;
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
      if (bt.bankers) html += `<h2>Bankers: only the most confident picks</h2><div class="table-wrap"><table><tr><th class="l">Favourite at least</th><th>Matches</th><th>Share of all</th><th>Correct</th></tr>
        ${Object.entries(bt.bankers).map(([k, v]) => `<tr><td class="l">${pct(Number(k))}</td><td>${v.n}</td><td>${pct(v.share, 1)}</td><td>${v.hit == null ? "–" : pct(v.hit, 1)}</td></tr>`).join("")}</table></div>
        <p class="muted small">Higher confidence means more correct picks but far fewer of them. Draws (about 1 in 4 matches) make a high hit rate on every match impossible.</p>`;
      const mk = bt.markets && bt.markets.markets;
      if (mk && Object.keys(mk).length) {
        const names = { corners: "Corners", cards: "Cards", shots: "Shots", sot: "Shots on target" };
        html += `<h2>Corners, cards and shots · ${esc(bt.markets.season_label)}</h2><div class="table-wrap"><table><tr><th class="l">Market</th><th>n</th><th>Line</th><th>Avg error</th><th>Pred / actual</th><th>Right side</th><th>Brier</th><th>Baseline</th></tr>
          ${Object.entries(mk).map(([k, v]) => `<tr><td class="l">${esc(names[k] || k)}</td><td>${v.n}</td><td>${v.line}</td><td>${num(v.mae_total, 2)}</td><td>${num(v.mean_pred, 1)} / ${num(v.mean_actual, 1)}</td><td>${pct(v.side_hit, 1)}</td><td class="${v.brier < v.brier_baseline ? "pos" : "neg"}">${num(v.brier, 4)}</td><td>${num(v.brier_baseline, 4)}</td></tr>`).join("")}</table></div>
          <p class="muted small">"Right side" = how often the more likely side of the main over/under line won. Baseline = always predicting last season's over rate; a model Brier below it means real skill. Player markets aren't backtested (no per-match player data).</p>`;
      }
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

  accuracyPage().catch(e => MP.fail(app, e));
})();
