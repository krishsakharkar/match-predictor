/* Page: Cards — expected cards (yellow + red) per team, referee-adjusted, with over/under lines. */
(function () {
  "use strict";
  const { esc, num, pct } = MP;
  MP.pages.cards = {
    title: "Cards",
    card(m) {
      const c = m.prediction && m.prediction.stats && m.prediction.stats.cards;
      if (!c) return MP.cardFrame(m, "", `<div class="muted small" style="margin-top:8px">No cards model for this fixture.</div>`, "");
      const main = c.total_lines["4.5"] || Object.values(c.total_lines)[0];
      const act = m.actual_stats && m.actual_stats.cards;
      const ref = m.referee ? `${esc(m.referee)}${c.referee_factor ? ` (card rate ×${num(c.referee_factor, 2)})` : ""}` : "not announced";
      const summary = `<div class="statline"><span>${esc(m.home)} <b>${num(c.home)}</b></span><span>${esc(m.away)} <b>${num(c.away)}</b></span></div>
        <div class="line small">Over 4.5 cards <b>${pct(main.over)}</b> · under ${pct(main.under)} · <span class="muted">Referee:</span> ${ref}${act && act[0] != null ? ` · <span class="muted">actual ${act[0]}–${act[1]}</span>` : ""}</div>`;
      return MP.cardFrame(m, `<div class="pred">${num(c.total)}</div><div class="label">exp. cards</div>`, summary,
        MP.linesTable("Cards", c, m, act) + MP.thinNote(c));
    },
  };
})();
