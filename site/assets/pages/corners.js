/* Page: Corners — expected corners per team and over/under lines. */
(function () {
  "use strict";
  const { esc, num, pct } = MP;
  MP.pages.corners = {
    title: "Corners",
    card(m) {
      const c = m.prediction && m.prediction.stats && m.prediction.stats.corners;
      if (!c) return MP.cardFrame(m, "", `<div class="muted small" style="margin-top:8px">No corners model for this fixture.</div>`, "");
      const main = c.total_lines["9.5"] || Object.values(c.total_lines)[0];
      const act = m.actual_stats && m.actual_stats.corners;
      const summary = `<div class="statline"><span>${esc(m.home)} <b>${num(c.home)}</b></span><span>${esc(m.away)} <b>${num(c.away)}</b></span></div>
        <div class="line small">Over 9.5 corners <b>${pct(main.over)}</b> · under ${pct(main.under)}${act && act[0] != null ? ` · <span class="muted">actual ${act[0]}–${act[1]} (${act[0] + act[1]})</span>` : ""}</div>`;
      return MP.cardFrame(m, `<div class="pred">${num(c.total)}</div><div class="label">exp. corners</div>`, summary,
        MP.linesTable("Corners", c, m, act) + MP.thinNote(c));
    },
  };
})();
