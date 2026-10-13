/* Page: Players — anytime goal, assist, 1+ shot on target, 2+ shots for each team's main players. */
(function () {
  "use strict";
  const { esc, pct } = MP;
  const NO_DATA = "No player data for this club. Understat covers the five big leagues; other clubs need API-Football Pro.";
  function table(team, rows) {
    if (!rows || !rows.length) return `<div class="muted small">${esc(team)}: ${NO_DATA}</div>`;
    return `<div class="table-wrap"><table><tr><th class="l">${esc(team)}</th><th>Goal</th><th>Assist</th><th>1+ SOT</th><th>2+ shots</th><th>Mins</th></tr>
      ${rows.map(r => `<tr><td class="l">${esc(r.player)}${r.penalty_taker ? ` <span class="badge" title="takes penalties">pen</span>` : ""}</td><td>${pct(r.anytime_goal)}</td><td>${pct(r.assist)}</td><td>${pct(r.sot_1plus)}</td><td>${pct(r.shots_2plus)}</td><td>${r.exp_minutes}</td></tr>`).join("")}</table></div>`;
  }
  MP.pages.players = {
    title: "Players",
    card(m) {
      const pl = (m.prediction && m.prediction.players) || {};
      const all = [...(pl.home || []).map(r => [r, m.home]), ...(pl.away || []).map(r => [r, m.away])]
        .sort((a, b) => b[0].anytime_goal - a[0].anytime_goal);
      const summary = all.length
        ? `<div class="line small">Most likely scorer: <b>${esc(all[0][0].player)}</b> <span class="muted">${esc(all[0][1])}</span> ${pct(all[0][0].anytime_goal)}${all[1] ? ` · ${esc(all[1][0].player)} ${pct(all[1][0].anytime_goal)}` : ""}</div>`
        : `<div class="muted small" style="margin-top:8px">${NO_DATA}</div>`;
      const body = all.length ? table(m.home, pl.home) + table(m.away, pl.away) +
        `<div class="muted small">From each player's Understat npxG, xA and shot shares × this match's expected goals and shots. Expected minutes use season averages until lineups are confirmed.</div>` : "";
      return MP.cardFrame(m, `<div class="label">players</div>`, summary, body);
    },
  };
})();
