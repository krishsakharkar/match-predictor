/* Page: Result & Score — W/D/L %, most likely scores, bankers, hit/miss. */
(function () {
  "use strict";
  const { esc, pct, dash, num } = MP;
  const actualScore = m => (m.result ? `${m.result.home}-${m.result.away}` : null);

  function body(m, p) {
    const abs = (p.absences || []).slice().sort((a, b) => (b.gk - a.gk) || ((b.attack_share || 0) - (a.attack_share || 0)));
    const act = actualScore(m);
    return `<div><div class="small"><b>6 most likely scores</b> <span class="muted">· the real score landed in this set 57% of the time in 2025-26</span></div>
      <div class="scores">${p.top_scores.map(t => `<span class="score-chip ${t.score === act ? "actual" : ""}">${dash(t.score)} <span class="muted">${pct(t.prob)}</span></span>`).join("")}</div></div>
      <div class="tiles">
        <div class="tile"><div class="k">Expected goals</div><div class="v">${num(p.xg.home, 2)}<span class="muted"> – </span>${num(p.xg.away, 2)}</div><div class="s">${esc(m.home)} – ${esc(m.away)}</div></div>
        <div class="tile"><div class="k">Fair odds</div><div class="v small" style="font-size:16px">${MP.odds(p.p_home)} · ${MP.odds(p.p_draw)} · ${MP.odds(p.p_away)}</div><div class="s">home · draw · away</div></div>
      </div>
      ${abs.length ? `<div class="small"><span class="muted">Absences:</span> ${abs.map(a => `${esc(a.player)}${a.gk ? " (GK)" : ""}${a.status === "bench" ? " (bench)" : ""} <span class="muted">${esc(a.team)}</span>`).join(", ")}</div>` : ""}
      <div class="muted small">${esc(p.source || "")}${(p.thin_data || []).length ? ` · thin data: ${esc(p.thin_data.join(", "))}` : ""} · referee: ${m.referee ? esc(m.referee) : "not announced"}</div>`;
  }

  MP.pages.results = {
    title: "Result & Score",
    top(d, list) {
      const th = (d.bankers || {}).threshold || 0.7;
      const now = new Date().toISOString();
      const picks = list.filter(m => m.prediction && !m.result && m.kickoff_utc > now)
        .map(m => { const k = MP.favourite(m.prediction); return { m, k, p: m.prediction[`p_${k}`] }; })
        .filter(x => x.p >= th).sort((a, b) => b.p - a.p);
      const hist = (d.bankers || {}).history;
      return `<section class="bankers"><div class="bankers-head"><h2>Bankers · ≥ ${pct(th)}</h2>
          <span class="muted small">${hist ? `right ${pct(hist.hit)} in 2025-26 (${hist.n})` : "the model's surest picks"}</span></div>
        ${picks.length ? `<div class="banker-row">${picks.map(x => `<div class="banker">
            <div class="banker-top">${MP.compTag(x.m)}<span>${esc(MP.shortDay(MP.localDay(x.m)))} · ${esc(MP.timeOf(x.m))}</span></div>
            <div class="pick"><span class="pill-p">${pct(x.p)}</span>${esc(x.k === "draw" ? "Draw" : `${x.m[x.k]} to win`)}</div>
            <div class="vs">${esc(x.m.home)} v ${esc(x.m.away)}</div></div>`).join("")}</div>`
          : `<div class="empty small">No upcoming match clears ${pct(th)} right now.</div>`}</section>`;
    },
    card(m) {
      const p = m.prediction;
      if (!p) return MP.cardFrame(m, "", `<div class="muted small line">No prediction (no rating for one of the teams).</div>`, "");
      const fav = MP.favourite(p);
      const banker = p[`p_${fav}`] >= 0.7 && !m.result ? `<span class="badge banker">★ banker</span>` : "";
      const act = actualScore(m);
      const keyAbs = (p.absences || []).filter(a => a.gk || (a.attack_share || 0) >= 0.08).slice(0, 3);
      const seg = (k, cls) => `<span class="${cls} ${k === fav ? "fav" : ""}" style="width:${p[`p_${k}`] * 100}%"></span>`;
      const lab = (k, text) => `<span class="${k === fav ? "fav" : ""}">${text}</span>`;
      const summary = `
        <div class="wdl"><div class="wdl-bar">${seg("home", "h")}${seg("draw", "d")}${seg("away", "a")}</div>
          <div class="wdl-labels">${lab("home", `<b>${pct(p.p_home)}</b> home`)}${lab("draw", `draw <b>${pct(p.p_draw)}</b>`)}${lab("away", `away <b>${pct(p.p_away)}</b>`)}</div></div>
        <div class="scores">${p.top_scores.slice(0, 3).map((t, i) => `<span class="score-chip ${t.score === act ? "actual" : i ? "" : "top"}">${dash(t.score)} <span class="muted">${pct(t.prob)}</span></span>`).join("")}</div>
        ${keyAbs.length ? `<div class="meta small"><div><span class="muted">Key absences:</span> ${keyAbs.map(a => `${esc(a.player)}${a.gk ? " (GK)" : ""} <span class="muted">${esc(a.team)}</span>`).join(", ")}${p.lineups_confirmed ? ` <span class="badge">lineups confirmed</span>` : ""}</div></div>` : ""}
        ${m.hit ? `<div class="hits">${MP.mark(m.hit.result, "result")} ${MP.mark(m.hit.exact, "exact")} ${MP.mark(m.hit.top3, "top 3")} ${MP.mark(m.hit.top6, "top 6")}</div>` : ""}`;
      return MP.cardFrame(m, `<div class="pred">${dash(p.predicted_score)}</div><div class="label">${m.result ? "predicted" : "most likely"}</div>`,
        summary, body(m, p), { badge: banker, verdict: m.hit ? m.hit.result : null });
    },
  };
})();
