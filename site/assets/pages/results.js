/* Page: Result & Score — W/D/L %, most likely scores, bankers, hit/miss. */
(function () {
  "use strict";
  const { esc, pct, dash, num } = MP;
  function body(m, p) {
    const abs = (p.absences || []).slice().sort((a, b) => (b.gk - a.gk) || ((b.attack_share || 0) - (a.attack_share || 0)));
    return `<div class="small"><b>6 most likely scores</b> <span class="muted">(the real score landed in this set 57% of the time in 2025-26)</span></div>
      <div class="scores">${p.top_scores.map(t => `<span class="score-chip">${dash(t.score)} <span class="muted">${pct(t.prob)}</span></span>`).join("")}</div>
      ${abs.length ? `<div class="small" style="margin-top:8px"><span class="muted">Absences:</span> ${abs.map(a => `${esc(a.player)}${a.gk ? " (GK)" : ""}${a.status === "bench" ? " (bench)" : ""} <span class="muted">${esc(a.team)}</span>`).join(", ")}</div>` : ""}
      <div class="muted small" style="margin-top:6px">xG ${num(p.xg.home, 2)}–${num(p.xg.away, 2)} · ${esc(p.source || "")}${(p.thin_data || []).length ? ` · thin data: ${esc(p.thin_data.join(", "))}` : ""}</div>`;
  }
  MP.pages.results = {
    title: "Result & Score",
    top(d, list) {
      const th = (d.bankers || {}).threshold || 0.7;
      const now = new Date().toISOString();
      const picks = list.filter(m => m.prediction && !m.result && m.kickoff_utc > now)
        .map(m => { const p = m.prediction; const k = ["home", "draw", "away"].reduce((a, b) => (p[`p_${b}`] > p[`p_${a}`] ? b : a), "home"); return { m, k, p: p[`p_${k}`] }; })
        .filter(x => x.p >= th).sort((a, b) => b.p - a.p);
      const hist = (d.bankers || {}).history;
      return `<section class="bankers"><h2>Bankers · favourite ≥ ${pct(th)}</h2>
        <p class="muted small">Only the matches where the model is most sure.${hist ? ` In 2025-26 these picks were right ${pct(hist.hit)} of the time (${hist.n} matches).` : ""}</p>
        ${picks.length ? picks.map(x => `<div class="banker"><span class="comp">${esc(x.m.badge)}</span> <b>${esc(x.k === "draw" ? "Draw" : x.m[x.k])}</b> <span class="muted">${esc(x.m.home)} v ${esc(x.m.away)} · ${esc(MP.dayLabel(MP.localDay(x.m)))} ${esc(MP.timeOf(x.m))}</span><span class="banker-p">${pct(x.p)}</span></div>`).join("")
          : `<div class="empty small">No upcoming match clears ${pct(th)} right now.</div>`}</section>`;
    },
    card(m) {
      const p = m.prediction;
      if (!p) return MP.cardFrame(m, "", `<div class="muted small" style="margin-top:8px">No prediction (no rating for one of the teams).</div>`, "");
      const keyAbs = (p.absences || []).filter(a => a.gk || (a.attack_share || 0) >= 0.08).slice(0, 3);
      const summary = `
        <div class="wdl"><div class="wdl-bar"><span class="h" style="width:${p.p_home * 100}%"></span><span class="d" style="width:${p.p_draw * 100}%"></span><span class="a" style="width:${p.p_away * 100}%"></span></div>
          <div class="wdl-labels"><span><b>${pct(p.p_home)}</b> win</span><span>draw <b>${pct(p.p_draw)}</b></span><span>win <b>${pct(p.p_away)}</b></span></div></div>
        <div class="scores">${p.top_scores.slice(0, 3).map((t, i) => `<span class="score-chip ${i ? "" : "top"}">${dash(t.score)} <span class="muted">${pct(t.prob)}</span></span>`).join("")}</div>
        <div class="meta small">
          ${keyAbs.length ? `<div><span class="muted">Key absences:</span> ${keyAbs.map(a => `${esc(a.player)}${a.gk ? " (GK)" : ""} <span class="muted">${esc(a.team)}</span>`).join(", ")}${p.lineups_confirmed ? ` <span class="badge">lineups confirmed</span>` : ""}</div>` : ""}
          <div><span class="muted">Referee:</span> ${m.referee ? esc(m.referee) : "not announced"}</div>
        </div>
        ${m.hit ? `<div class="hits">${MP.mark(m.hit.result, "result")} ${MP.mark(m.hit.exact, "exact")} ${MP.mark(m.hit.top3, "top 3")} ${MP.mark(m.hit.top6, "top 6")}</div>` : ""}`;
      return MP.cardFrame(m, `<div class="pred">${dash(p.predicted_score)}</div><div class="label">most likely</div>`, summary, body(m, p));
    },
  };
})();
