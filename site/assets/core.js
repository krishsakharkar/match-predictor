/* match-predictor shared helpers. Every page module registers itself in MP.pages. */
(function () {
  "use strict";
  const MP = (window.MP = window.MP || { pages: {} });
  MP.esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  MP.pct = (p, d = 0) => (p == null ? "–" : (p * 100).toFixed(d) + "%");
  MP.num = (x, d = 1) => (x == null ? "–" : Number(x).toFixed(d));
  MP.odds = p => (p ? (1 / p).toFixed(2) : "–");
  MP.dash = s => MP.esc(String(s || "").replace("-", "–"));
  MP.store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };
  MP.load = async path => {
    const r = await fetch(path, { cache: "no-cache" });
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  };
  MP.fail = (el, e) => {
    const missing = /HTTP 404/.test(e.message);
    el.innerHTML = `<div class="empty">${missing ? "No data yet. The pipeline hasn't run." : "Could not load data."}<br><span class="small">${MP.esc(e.message)}</span></div>`;
  };
  MP.localDay = m => (m.kickoff_utc ? new Date(m.kickoff_utc) : new Date(m.date + "T12:00:00Z"));
  const dayKey = d => d.toLocaleDateString(undefined, { year: "numeric", month: "2-digit", day: "2-digit" });
  MP.dayLabel = d => {
    const t = new Date(), y = new Date(), tm = new Date();
    y.setDate(t.getDate() - 1); tm.setDate(t.getDate() + 1);
    if (dayKey(d) === dayKey(t)) return "Today";
    if (dayKey(d) === dayKey(tm)) return "Tomorrow";
    if (dayKey(d) === dayKey(y)) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" });
  };
  MP.timeOf = m => (m.kickoff_utc ? new Date(m.kickoff_utc).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "");
  MP.grouped = (list, cardFn) => {
    const groups = [];
    for (const m of list) {
      const d = MP.localDay(m), k = dayKey(d);
      if (!groups.length || groups[groups.length - 1].k !== k) groups.push({ k, label: MP.dayLabel(d), items: [] });
      groups[groups.length - 1].items.push(m);
    }
    return groups.map(g => `<h3 class="day">${MP.esc(g.label)}</h3>${g.items.map(cardFn).join("")}`).join("");
  };
  MP.mark = (ok, label) => ok == null ? "" : `<span class="badge ${ok ? "hit" : "miss"}">${ok ? "✓" : "✗"} ${label}</span>`;
  MP.notes = d => (!d.notes || !d.notes.length) ? "" :
    `<details class="notes"><summary>Data notes (${d.notes.length})</summary><ul>${d.notes.map(n => `<li>${MP.esc(n)}</li>`).join("")}</ul></details>`;

  /* The common card frame: header (badge, time), teams, a centre block, a summary line, and a body
     that expands on tap. Each page supplies centre/summary/body. */
  MP.cardFrame = (m, centre, summary, body) => {
    let right = `<span class="muted">TBD</span>`;
    if (m.result) right = `<b>FT ${m.result.home}–${m.result.away}</b>`;
    else if (m.kickoff_utc && m.kickoff_utc < new Date().toISOString()) right = `<span class="muted">awaiting result</span>`;
    return `<article class="card" tabindex="0" data-expand>
      <div class="card-head"><span><span class="comp">${MP.esc(m.badge)}</span>${m.prediction && m.prediction.retro ? ` <span class="badge" title="Predicted after kickoff using only data from before the match. Not counted in live accuracy.">backfilled</span>` : ""}</span><span>${MP.esc(MP.timeOf(m) || m.date)}</span></div>
      <div class="teams"><div class="team">${MP.esc(m.home)}</div>
        <div class="score">${centre}<div class="result">${right}</div></div>
        <div class="team away">${MP.esc(m.away)}</div></div>
      ${summary}
      ${body ? `<div class="more" hidden>${body}</div><div class="tap muted small">Tap for details</div>` : ""}
    </article>`;
  };
  MP.bindExpand = root => root.querySelectorAll("[data-expand]").forEach(c => {
    const toggle = () => {
      const more = c.querySelector(".more"), tap = c.querySelector(".tap");
      if (!more) return;
      more.hidden = !more.hidden;
      if (tap) tap.textContent = more.hidden ? "Tap for details" : "Tap to close";
    };
    c.addEventListener("click", e => { if (!e.target.closest("a,button")) toggle(); });
    c.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  });

  /* Over/under table for a market block {total_lines, home_lines, away_lines}. */
  MP.linesTable = (label, blk, m, actual) => {
    if (!blk) return "";
    const row = (name, lines, act) => Object.entries(lines || {}).map(([l, v]) => {
      const res = act == null ? "" : (act > Number(l) ? "over" : "under");
      return `<tr><td class="l">${MP.esc(name)} ${MP.esc(l)}</td><td>${MP.pct(v.over)}</td><td>${MP.odds(v.over)}</td><td>${MP.pct(v.under)}</td><td>${MP.odds(v.under)}</td><td>${res ? `<span class="${res === "over" ? "pos" : ""}">${res}</span>` : ""}</td></tr>`;
    }).join("");
    const a = actual || [null, null];
    const tot = a[0] == null || a[1] == null ? null : a[0] + a[1];
    return `<div class="table-wrap"><table><tr><th class="l">${MP.esc(label)}</th><th>Over</th><th>Fair</th><th>Under</th><th>Fair</th><th>Result</th></tr>
      ${row("Total", blk.total_lines, tot)}${row(m.home, blk.home_lines, a[0])}${row(m.away, blk.away_lines, a[1])}</table></div>`;
  };
  MP.thinNote = blk => blk && blk.thin && blk.thin.length ? `<div class="muted small">${MP.esc(blk.source || "")} · thin data: ${MP.esc(blk.thin.join(", "))}</div>`
    : blk ? `<div class="muted small">${MP.esc(blk.source || "")}</div>` : "";
})();
