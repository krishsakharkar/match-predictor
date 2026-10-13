/* Page: Shots — expected shots and shots on target per team with over/under lines. */
(function () {
  "use strict";
  const { esc, num } = MP;
  MP.pages.shots = {
    title: "Shots",
    card(m) {
      const st = (m.prediction && m.prediction.stats) || {};
      const s = st.shots, t = st.sot;
      if (!s && !t) return MP.cardFrame(m, "", `<div class="muted small" style="margin-top:8px">No shots model for this fixture.</div>`, "");
      const a = m.actual_stats || {};
      const summary = `<div class="statline"><span>${esc(m.home)} <b>${num(s && s.home)}</b> shots · <b>${num(t && t.home)}</b> on target</span></div>
        <div class="statline"><span>${esc(m.away)} <b>${num(s && s.away)}</b> shots · <b>${num(t && t.away)}</b> on target</span></div>
        ${a.shots && a.shots[0] != null ? `<div class="line small muted">actual: shots ${a.shots[0]}–${a.shots[1]}, on target ${a.sot[0]}–${a.sot[1]}</div>` : ""}`;
      return MP.cardFrame(m, `<div class="pred">${num(t && t.total)}</div><div class="label">exp. on target</div>`, summary,
        MP.linesTable("Shots", s, m, a.shots) + MP.linesTable("Shots on target", t, m, a.sot) + MP.thinNote(s || t));
    },
  };
})();
