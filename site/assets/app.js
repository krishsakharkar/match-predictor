/* match-predictor static site. Renders everything from data/*.json. */
(function () {
  "use strict";
  const page = document.body.dataset.page;
  const app = document.getElementById("app");
  document.querySelectorAll("[data-nav]").forEach(a => {
    if (a.dataset.nav === page || (page === "match" && a.dataset.nav === "index")) a.classList.add("active");
  });

  // ---------- helpers ----------
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (p, d = 0) => (p == null ? "–" : (p * 100).toFixed(d) + "%");
  const odds = o => (o == null ? "–" : Number(o).toFixed(2));
  const num = (x, d = 1) => (x == null ? "–" : Number(x).toFixed(d));
  const signed = (x, d = 1) => (x == null ? "–" : (x > 0 ? "+" : "") + (x * 100).toFixed(d) + "%");
  const cls = x => (x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "");
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };
  const when = m => {
    if (!m.kickoff_utc) return m.date;
    const d = new Date(m.kickoff_utc);
    return d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  };
  const isToday = iso => { const d = new Date(iso), n = new Date(); return d.toDateString() === n.toDateString(); };
  const SEL = { home: "Home", draw: "Draw", away: "Away", over: "Over", under: "Under", yes: "Yes", no: "No" };
  const MARKET = { h2h: "1X2", totals: "Total goals", btts: "BTTS" };
  const selLabel = (v, m) => {
    if (v.market === "h2h") return v.selection === "draw" ? "Draw" : (v.selection === "home" ? m.home : m.away);
    if (v.market === "totals") return `${SEL[v.selection]} ${v.line}`;
    return `BTTS ${SEL[v.selection]}`;
  };
  async function load(path) {
    const r = await fetch(path, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  }
  const fail = e => {
    const missing = /HTTP 404/.test(e.message);
    app.innerHTML = `<div class="empty">${missing ? "No predictions yet. The pipeline hasn't run." : "Could not load data."}<br><span class="small">${esc(e.message)}</span></div>`;
  };

  function notesBlock(d) {
    if (!d.notes || !d.notes.length) return "";
    return `<details class="notes"><summary>Data notes (${d.notes.length})</summary><ul>${d.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></details>`;
  }
  function wdl(p, m) {
    const x = p.markets["1x2"];
    return `<div class="wdl"><div class="wdl-bar"><span class="h" style="width:${x.home * 100}%"></span><span class="d" style="width:${x.draw * 100}%"></span><span class="a" style="width:${x.away * 100}%"></span></div>
      <div class="wdl-labels"><span><b>${pct(x.home)}</b> ${esc(m.home)}</span><span>Draw <b>${pct(x.draw)}</b></span><span>${esc(m.away)} <b>${pct(x.away)}</b></span></div></div>`;
  }
  const favourite = p => { const x = p.markets["1x2"]; return ["home", "draw", "away"].reduce((a, b) => (x[b] > x[a] ? b : a), "home"); };
  const outcome = r => (r.home > r.away ? "home" : r.home < r.away ? "away" : "draw");

  function card(m) {
    const p = m.prediction;
    const nValue = p ? (p.value || []).filter(v => v.is_value).length : 0;
    let res = `<div class="result muted">TBD</div>`;
    if (m.result) {
      const hit = p ? favourite(p) === outcome(m.result) : null;
      res = `<div class="result final">FT ${m.result.home}–${m.result.away}</div>`;
      if (hit != null) res += `<span class="badge ${hit ? "hit" : "miss"}">${hit ? "✓ called" : "✗ missed"}</span>`;
    }
    return `<a class="card" href="match.html?id=${encodeURIComponent(m.id)}">
      <div class="card-head"><span>${esc(m.league_name)}</span><span>${esc(when(m))}</span></div>
      <div class="teams"><div class="team">${esc(m.home)}</div>
        <div class="score">${p ? `<div class="pred">${esc(p.predicted_score.replace("-", "–"))}</div><div class="label">predicted</div>` : `<div class="label">no prediction</div>`}${res}</div>
        <div class="team away">${esc(m.away)}</div></div>
      ${p ? wdl(p, m) : ""}
      ${nValue ? `<div style="margin-top:8px"><span class="badge value">${nValue} value bet${nValue > 1 ? "s" : ""}</span></div>` : ""}
    </a>`;
  }

  function leagueFilter(d, render) {
    const key = "mp.league";
    let cur = store.get(key) || "all";
    if (cur !== "all" && !d.leagues[cur]) cur = "all";
    const draw = () => {
      const chips = [["all", "All"], ...Object.entries(d.leagues)].map(([k, n]) => `<button class="chip ${k === cur ? "on" : ""}" data-lg="${esc(k)}">${esc(n)}</button>`).join("");
      app.innerHTML = render(cur, `<div class="filters">${chips}</div>`);
      app.querySelectorAll("[data-lg]").forEach(b => b.addEventListener("click", () => { cur = b.dataset.lg; store.set(key, cur); draw(); }));
    };
    draw();
  }

  // ---------- pages ----------
  async function indexPage() {
    const d = await load("data/predictions.json");
    const now = new Date().toISOString();
    leagueFilter(d, (lg, filters) => {
      const ms = d.matches.filter(m => lg === "all" || m.league === lg);
      const upcoming = ms.filter(m => m.status !== "FT" && (!m.kickoff_utc || m.kickoff_utc > now));
      const recent = ms.filter(m => !upcoming.includes(m)).reverse();
      return `<h1>Matches</h1>${notesBlock(d)}${filters}
        <h2>Upcoming</h2>${upcoming.length ? upcoming.map(card).join("") : `<div class="empty">No upcoming fixtures.</div>`}
        <h2>Recent</h2>${recent.length ? recent.map(card).join("") : `<div class="empty">No recent matches.</div>`}
        <p class="muted small">Updated ${esc(new Date(d.generated_at).toLocaleString())} · model ${esc(d.model_version)}</p>`;
    });
  }

  function linesTable(title, lines, side) {
    if (!lines) return "";
    return Object.entries(lines).map(([l, v]) => `<tr><td>${esc(title)} ${side ? esc(side) + " " : ""}O/U ${esc(l)}</td><td>${pct(v.over)}</td><td>${odds(1 / v.over)}</td><td>${pct(v.under)}</td><td>${odds(1 / v.under)}</td></tr>`).join("");
  }

  async function matchPage() {
    const d = await load("data/predictions.json");
    const id = new URLSearchParams(location.search).get("id");
    const m = d.matches.find(x => x.id === id);
    if (!m) { app.innerHTML = `<a class="back" href="index.html">← Matches</a><div class="empty">Match not found.</div>`; return; }
    const p = m.prediction;
    let html = `<a class="back" href="index.html">← Matches</a>${card(m).replace('<a class="card"', '<div class="card"').replace(/<\/a>$/, "</div>")}`;
    if (!p) { app.innerHTML = html + `<div class="empty">No prediction was made for this match.</div>`; return; }
    const mk = p.markets, st = p.stats || {}, o = p.odds || {}, act = m.actual_stats;
    const x = mk["1x2"];

    html += `<h2>Win / draw / loss</h2><div class="table-wrap"><table><tr><th></th><th>Model</th><th>Fair</th><th>FanDuel</th></tr>
      ${["home", "draw", "away"].map(k => `<tr><td>${k === "draw" ? "Draw" : esc(m[k])}</td><td>${pct(x[k], 1)}</td><td>${odds(p.fair_odds["1x2"][k])}</td><td>${odds((o.h2h || {})[k])}</td></tr>`).join("")}
      </table></div>`;

    const statRow = (label, key, d1 = 2) => {
      const s = key === "xg" ? { home: p.xg.home, away: p.xg.away, total: p.xg.home + p.xg.away } : st[key];
      if (!s) return "";
      const a = act && act[key] ? ` <span class="muted small">(${act[key][0]}–${act[key][1]})</span>` : "";
      const res = key === "xg" && m.result ? ` <span class="muted small">(${m.result.home}–${m.result.away})</span>` : a;
      return `<tr><td>${label}</td><td>${num(s.home, d1)}</td><td>${num(s.away, d1)}</td><td>${num(s.total, d1)}${res}</td></tr>`;
    };
    html += `<h2>Expected per team</h2><div class="table-wrap"><table><tr><th></th><th>${esc(m.home)}</th><th>${esc(m.away)}</th><th>Total</th></tr>
      ${statRow("xG (model)", "xg")}${statRow("Shots", "shots", 1)}${statRow("Shots on target", "sot", 1)}${statRow("Corners", "corners", 1)}${statRow("Cards", "cards", 1)}
      </table></div><p class="muted small">Brackets show the actual numbers once the match is played.${p.referee ? ` Referee: ${esc(p.referee)}${st.cards && st.cards.referee_factor ? ` (card factor ×${num(st.cards.referee_factor, 2)})` : ""}.` : ""}${(p.adjustments.home_multiplier < 1 || p.adjustments.away_multiplier < 1) ? ` Absence adjustment: home ×${num(p.adjustments.home_multiplier, 2)}, away ×${num(p.adjustments.away_multiplier, 2)}.` : ""}${p.promoted.home || p.promoted.away ? " Promoted-team prior applied." : ""}</p>`;

    // value / market comparison
    const vals = p.value || [];
    html += `<h2>Value vs FanDuel</h2>`;
    if (!vals.length) html += `<div class="empty small">No FanDuel prices captured for this match yet.</div>`;
    else html += `<div class="table-wrap"><table><tr><th>Selection</th><th>Model</th><th>Fair</th><th>FanDuel</th><th>No-vig</th><th>Edge</th><th>Stake</th></tr>
      ${vals.slice().sort((a, b) => b.edge - a.edge).map(v => `<tr class="${v.is_value ? "vb" : ""}"><td>${esc(MARKET[v.market])}: ${esc(selLabel(v, m))}${v.is_value ? ` <span class="badge value">value</span>` : v.suspect ? ` <span class="badge miss" title="Edge above the sanity cap: more likely model error than a mispriced line">suspect</span>` : ""}</td><td>${pct(v.model_prob, 1)}</td><td>${odds(v.fair_odds)}</td><td>${odds(v.price)}</td><td>${pct(v.market_prob, 1)}</td><td class="${cls(v.edge)}">${signed(v.edge)}</td><td>${v.stake ? pct(v.stake, 2) : "–"}</td></tr>`).join("")}
      </table></div>`;

    html += `<h2>Goals markets</h2><div class="table-wrap"><table><tr><th>Market</th><th>Over / Yes</th><th>Fair</th><th>Under / No</th><th>Fair</th></tr>
      ${["1.5", "2.5", "3.5"].filter(l => mk.totals[l]).map(l => `<tr><td>Goals O/U ${l}</td><td>${pct(mk.totals[l].over)}</td><td>${odds(1 / mk.totals[l].over)}</td><td>${pct(mk.totals[l].under)}</td><td>${odds(1 / mk.totals[l].under)}</td></tr>`).join("")}
      <tr><td>BTTS</td><td>${pct(mk.btts.yes)}</td><td>${odds(1 / mk.btts.yes)}</td><td>${pct(mk.btts.no)}</td><td>${odds(1 / mk.btts.no)}</td></tr>
      ${linesTable(m.home, mk.team_totals.home)}${linesTable(m.away, mk.team_totals.away)}
      </table></div>`;

    html += `<h2>Correct score (top 5)</h2><div class="table-wrap"><table><tr><th>Score</th><th>Prob</th><th>Fair</th></tr>
      ${mk.correct_score.map(c => `<tr><td>${esc(c.score)}</td><td>${pct(c.prob, 1)}</td><td>${odds(c.fair_odds)}</td></tr>`).join("")}</table></div>`;

    const statLines = ["corners", "sot", "cards"].filter(k => st[k] && st[k].total_lines);
    if (statLines.length) {
      const names = { corners: "Corners", sot: "SOT", cards: "Cards" };
      html += `<h2>Corners, shots on target, cards</h2><div class="table-wrap"><table><tr><th>Market</th><th>Over</th><th>Fair</th><th>Under</th><th>Fair</th></tr>
        ${statLines.map(k => linesTable(names[k], st[k].total_lines) + linesTable(names[k], st[k].home_lines, m.home) + linesTable(names[k], st[k].away_lines, m.away)).join("")}
        </table></div>`;
    }

    html += `<h2>Lineups</h2>`;
    const lu = p.lineups || {};
    if (lu.home || lu.away) {
      html += `<div class="grid2">${["home", "away"].map(s => `<div class="tile"><div class="k">${esc(m[s])}${lu[s] && lu[s].formation ? ` · ${esc(lu[s].formation)}` : ""}</div>${lu[s] ? `<ol class="small" style="padding-left:18px;margin:6px 0">${lu[s].starters.map(x => `<li>${esc(x.name)} <span class="muted">${esc(x.pos || "")}</span></li>`).join("")}</ol>` : `<div class="muted small">Not announced</div>`}</div>`).join("")}</div>`;
    } else html += `<div class="empty small">Lineups appear about an hour before kickoff (requires API-Football current-season access).</div>`;
    if (p.absences && p.absences.length) {
      html += `<h2>Absences</h2><div class="table-wrap"><table><tr><th>Player</th><th>Team</th><th>Reason</th></tr>${p.absences.map(a => `<tr><td>${esc(a.player)}</td><td>${esc(a.team)}</td><td>${esc(a.reason || "")}</td></tr>`).join("")}</table></div>`;
    }

    html += `<h2>Top player props</h2>`;
    const pr = p.props || {};
    if (pr.home || pr.away) {
      html += `<div class="table-wrap"><table><tr><th>Player</th><th>Anytime</th><th>Fair</th><th>1+ SOT</th><th>Fair</th></tr>
        ${["home", "away"].flatMap(s => (pr[s] || []).map(r => `<tr><td>${esc(r.player)} <span class="muted small">${esc(m[s])}</span></td><td>${pct(r.anytime_scorer)}</td><td>${odds(r.anytime_scorer_fair)}</td><td>${pct(r.sot_1plus)}</td><td>${odds(r.sot_1plus_fair)}</td></tr>`)).join("")}
        </table></div>`;
    } else html += `<div class="empty small">No current-season player data (API-Football) for these teams.</div>`;

    html += `<p class="muted small">Predicted ${esc(m.predicted_at ? new Date(m.predicted_at).toLocaleString() : "–")}${o.updated ? ` · odds ${esc(new Date(o.updated).toLocaleString())}` : ""}</p>`;
    app.innerHTML = html;
  }

  async function valuePage() {
    const d = await load("data/predictions.json");
    const byId = Object.fromEntries(d.matches.map(m => [m.id, m]));
    const r = d.value_rules;
    leagueFilter(d, (lg, filters) => {
      const vb = d.value_bets.filter(v => lg === "all" || v.league === lg);
      const today = vb.filter(v => isToday(v.kickoff_utc));
      const later = vb.filter(v => !isToday(v.kickoff_utc));
      const row = v => {
        const m = byId[v.match_id] || v;
        return `<a class="card vb" href="match.html?id=${encodeURIComponent(v.match_id)}">
          <div class="card-head"><span>${esc(m.home)} v ${esc(m.away)}</span><span>${esc(when(v))}</span></div>
          <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">
            <div><b>${esc(MARKET[v.market])}: ${esc(selLabel(v, m))}</b><div class="muted small">model ${pct(v.model_prob, 1)} · no-vig ${pct(v.market_prob, 1)} · fair ${odds(v.fair_odds)}</div></div>
            <div style="text-align:right"><div style="font-size:20px;font-weight:700">${odds(v.price)}</div><div class="small"><span class="pos">${signed(v.edge)}</span> · stake ${pct(v.stake, 2)}</div></div>
          </div></a>`;
      };
      return `<h1>Value Bets</h1>${notesBlock(d)}
        <p class="muted small">Model probability vs FanDuel's vig-free price. Flagged when edge ≥ ${pct(r.min_edge)} and EV &gt; 0 at odds ${r.min_odds}–${r.max_odds}${r.max_edge != null ? ` (edges above ${pct(r.max_edge)} are treated as suspect model error and not listed)` : ""}; stake = ${r.kelly_fraction}× Kelly, capped at ${pct(r.max_stake)} of bankroll. Single bookmaker, so this is value, not arbitrage.</p>
        ${filters}
        <h2>Today</h2>${today.length ? today.map(row).join("") : `<div class="empty">No value bets today.</div>`}
        <h2>Upcoming</h2>${later.length ? later.map(row).join("") : `<div class="empty">None flagged.</div>`}`;
    });
  }

  function calibrationSvg(buckets) {
    if (!buckets || !buckets.length) return `<div class="empty small">Not enough settled predictions yet.</div>`;
    const W = 320, H = 240, P = 34;
    const sx = v => P + v * (W - P - 10), sy = v => H - P - v * (H - P - 10);
    const maxN = Math.max(...buckets.map(b => b.n));
    const dots = buckets.map(b => `<circle cx="${sx(b.predicted)}" cy="${sy(b.observed)}" r="${3 + 6 * Math.sqrt(b.n / maxN)}" fill="var(--home)" fill-opacity=".75"><title>${b.bin}: predicted ${pct(b.predicted, 1)}, observed ${pct(b.observed, 1)} (n=${b.n})</title></circle>`).join("");
    const ticks = [0, .25, .5, .75, 1].map(t => `<text x="${sx(t)}" y="${H - P + 16}" text-anchor="middle">${t * 100}%</text><text x="${P - 6}" y="${sy(t) + 4}" text-anchor="end">${t * 100}%</text>`).join("");
    return `<div class="table-wrap" style="padding:8px"><svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Calibration chart">
      <line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(1)}" y2="${sy(1)}" stroke="var(--border)" stroke-dasharray="4 4"/>
      <line x1="${P}" y1="${sy(0)}" x2="${W - 10}" y2="${sy(0)}" stroke="var(--border)"/><line x1="${P}" y1="${sy(0)}" x2="${P}" y2="10" stroke="var(--border)"/>
      ${ticks}${dots}<text x="${W / 2}" y="${H - 2}" text-anchor="middle">predicted</text></svg>
      <div class="muted small" style="padding:0 6px 4px">Dots on the dashed line = well calibrated. Dot size = number of predictions.</div></div>`;
  }

  async function performancePage() {
    const [perf, bt] = await Promise.all([load("data/performance.json"), load("data/backtest.json").catch(() => null)]);
    const pr = perf.predictions || { n: 0 }, b = perf.bets || {};
    const tile = (k, v, s) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;
    let html = `<h1>Performance</h1><h2>Live tracking</h2><div class="tiles">
      ${tile("Settled predictions", pr.n || 0)}
      ${tile("Brier 1X2", num(pr.brier_1x2, 3), "lower is better (0–2)")}
      ${tile("Log loss 1X2", num(pr.log_loss_1x2, 3), "uniform = 1.099")}
      ${tile("Favourite hit rate", pr.accuracy_1x2 != null ? pct(pr.accuracy_1x2) : "–")}
      ${tile("Value bets", b.n_bets || 0, `${b.n_settled || 0} settled`)}
      ${tile("ROI (Kelly)", b.roi_kelly != null ? `<span class="${cls(b.roi_kelly)}">${signed(b.roi_kelly)}</span>` : "–", b.roi_flat != null ? `flat ${signed(b.roi_flat)}` : "")}
      ${tile("Avg CLV", b.avg_clv != null ? `<span class="${cls(b.avg_clv)}">${signed(b.avg_clv)}</span>` : "–", b.clv_positive_rate != null ? `${pct(b.clv_positive_rate)} beat close` : "odds taken ÷ closing − 1")}
      ${tile("Brier O2.5 / BTTS", `${num(pr.brier_over25, 3)} / ${num(pr.brier_btts, 3)}`)}
    </div>`;
    html += `<h2>Calibration (1X2, O2.5, BTTS)</h2>${calibrationSvg(pr.calibration)}`;
    const bl = perf.by_league || {};
    if (Object.keys(bl).length) html += `<h2>By league</h2><div class="table-wrap"><table><tr><th>League</th><th>n</th><th>Brier</th><th>Log loss</th><th>Hit</th></tr>${Object.entries(bl).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${num(v.brier_1x2, 3)}</td><td>${num(v.log_loss_1x2, 3)}</td><td>${pct(v.accuracy_1x2)}</td></tr>`).join("")}</table></div>`;
    if (pr.biggest_misses && pr.biggest_misses.length) {
      html += `<h2>Biggest misses</h2><div class="table-wrap"><table><tr><th>Match</th><th>Result</th><th>Pred</th><th>H/D/A</th><th>P(actual)</th></tr>
        ${pr.biggest_misses.map(x => `<tr><td><a href="match.html?id=${encodeURIComponent(x.id)}">${esc(x.home)} v ${esc(x.away)}</a> <span class="muted small">${esc(x.date)}</span></td><td>${esc(x.score)}</td><td>${esc(x.predicted_score || "")}</td><td>${pct(x.p_home)}/${pct(x.p_draw)}/${pct(x.p_away)}</td><td class="neg">${pct(x.p_actual, 1)}</td></tr>`).join("")}</table></div>`;
    }
    const log = perf.bets_log || [];
    html += `<h2>Bet log</h2>`;
    if (!log.length) html += `<div class="empty small">No value bets logged yet.</div>`;
    else html += `<div class="table-wrap"><table><tr><th>Bet</th><th>Taken</th><th>Close</th><th>CLV</th><th>Stake</th><th>Result</th></tr>
      ${log.map(x => `<tr><td>${esc(x.home)} v ${esc(x.away)} · ${esc(MARKET[x.market])} ${esc(selLabel(x, x))}</td><td>${odds(x.odds_taken)}</td><td>${odds(x.closing_odds)}</td><td class="${cls(x.closing_odds ? x.odds_taken / x.closing_odds - 1 : null)}">${x.closing_odds ? signed(x.odds_taken / x.closing_odds - 1) : "–"}</td><td>${pct(x.stake, 2)}</td><td class="${x.result === "win" ? "pos" : x.result === "loss" ? "neg" : ""}">${esc(x.result || "pending")}</td></tr>`).join("")}</table></div>`;

    if (bt) {
      html += `<h2>Backtest · season ${esc(bt.season)}</h2><p class="muted small">Walk-forward: refit weekly on data available before each match week; time decay tuned on ${esc(bt.tune_season)}.</p>
        <div class="table-wrap"><table><tr><th>League</th><th>n</th><th>Model Brier</th><th>Baseline</th><th>Market</th><th>Log loss</th><th>Hit</th></tr>
        ${Object.entries(bt.leagues).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.model.n}</td><td>${num(v.model.brier_1x2, 4)}</td><td>${num(v.baseline.brier_1x2, 4)}</td><td>${v.market_closing ? num(v.market_closing.brier_1x2, 4) : "n/a"}</td><td>${num(v.model.log_loss_1x2, 4)}</td><td>${pct(v.model.accuracy_1x2)}</td></tr>`).join("")}
        </table></div>`;
      const vb = bt.overall_value_bets_at_close;
      if (vb) html += `<p class="small">Value bets at closing odds: ${vb.n_bets} bets, flat ROI <span class="${cls(vb.roi_flat)}">${signed(vb.roi_flat)}</span>, Kelly ROI <span class="${cls(vb.roi_kelly)}">${signed(vb.roi_kelly)}</span>.</p>`;
      html += `<h2>Backtest calibration</h2>${calibrationSvg(bt.overall && bt.overall.calibration)}`;
    }
    html += `<p class="muted small">Updated ${esc(new Date(perf.generated_at).toLocaleString())}</p>`;
    app.innerHTML = html;
  }

  const pages = { index: indexPage, match: matchPage, value: valuePage, performance: performancePage };
  (pages[page] || indexPage)().catch(fail);
})();
