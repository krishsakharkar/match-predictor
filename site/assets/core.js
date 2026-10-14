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
  // every date and time on the site is US Eastern (the owner is in New York), whatever the viewer's device says
  MP.TZ = "America/New_York";
  const dayKey = d => d.toLocaleDateString("en-US", { timeZone: MP.TZ, year: "numeric", month: "2-digit", day: "2-digit" });
  MP.dayLabel = d => {
    const t = new Date(), y = new Date(t.getTime() - 864e5), tm = new Date(t.getTime() + 864e5);
    if (dayKey(d) === dayKey(t)) return "Today";
    if (dayKey(d) === dayKey(tm)) return "Tomorrow";
    if (dayKey(d) === dayKey(y)) return "Yesterday";
    return d.toLocaleDateString("en-US", { timeZone: MP.TZ, weekday: "long", day: "numeric", month: "short" });
  };
  MP.shortDay = d => {
    const l = MP.dayLabel(d);
    return ["Today", "Tomorrow", "Yesterday"].includes(l) ? l : d.toLocaleDateString("en-US", { timeZone: MP.TZ, weekday: "short", month: "short", day: "numeric" });
  };
  MP.timeOf = m => (m.kickoff_utc ? new Date(m.kickoff_utc).toLocaleTimeString("en-US", { timeZone: MP.TZ, hour: "numeric", minute: "2-digit" }) + " ET" : "");
  MP.stamp = iso => new Date(iso).toLocaleString("en-US", { timeZone: MP.TZ, dateStyle: "medium", timeStyle: "short" }) + " ET";
  MP.grouped = (list, cardFn) => {
    const groups = [];
    for (const m of list) {
      const d = MP.localDay(m), k = dayKey(d);
      if (!groups.length || groups[groups.length - 1].k !== k) groups.push({ k, label: MP.dayLabel(d), items: [] });
      groups[groups.length - 1].items.push(m);
    }
    return groups.map(g => `<h3 class="day">${MP.esc(g.label)} <span class="n">${g.items.length}</span></h3>${g.items.map(cardFn).join("")}`).join("");
  };
  MP.mark = (ok, label) => ok == null ? "" : `<span class="badge ${ok ? "hit" : "miss"}">${ok ? "✓" : "✗"} ${label}</span>`;
  MP.notes = d => (!d.notes || !d.notes.length) ? "" :
    `<details class="notes"><summary>Data notes (${d.notes.length})</summary><ul>${d.notes.map(n => `<li>${MP.esc(n)}</li>`).join("")}</ul></details>`;
  MP.favourite = p => ["home", "draw", "away"].reduce((a, b) => (p[`p_${b}`] > p[`p_${a}`] ? b : a), "home");

  /* Competition colours (badge -> CSS colour) and club crests (initials on a colour derived from the name). */
  const COMP_COLOURS = { EPL: "#a66bff", UCL: "#4f8dff", LALIGA: "#ff6b4a", BUNDESLIGA: "#e8414f", SERIEA: "#2ec4b6",
    LIGUE1: "#c7e04a", UEL: "#ff9f1c", UECL: "#3ddc84" };
  MP.compColour = comp => COMP_COLOURS[comp] || "#7d8a9b";
  MP.crest = name => {
    const words = String(name || "?").replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(w => w && !/^(fc|cf|sc|ac|as|fk|sk|afc|rc|cd|ud|sd|ssc)$/i.test(w));
    const ini = (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 3)).toUpperCase();
    let h = 0;
    for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) % 360;
    return `<span class="crest" style="--h:${h}" aria-hidden="true">${MP.esc(ini)}</span>`;
  };
  MP.compTag = m => `<span class="comp" style="--c:${MP.compColour(m.comp)}" title="${MP.esc(m.comp_name || m.badge)}">${MP.esc(m.badge)}</span>`;

  /* The common card frame: top line (competition, status, time), the two clubs stacked with the final score,
     a highlighted centre block, a summary, and details that expand on tap. Pages supply centre/summary/body. */
  MP.cardFrame = (m, centre, summary, body, opts = {}) => {
    const r = m.result;
    const started = m.kickoff_utc && m.kickoff_utc < new Date().toISOString();
    const status = r ? `<span class="status ft">FT</span>` : started ? `<span class="status wait">Awaiting result</span>` : "";
    const when = MP.esc(MP.timeOf(m) || m.date);      // the day is the group heading
    const side = (name, goals, other) => {
      const cls = r ? (goals > other ? "winner" : goals < other ? "loser" : "") : "";
      return `<div class="side ${cls}">${MP.crest(name)}<span class="name">${MP.esc(name)}</span>${r ? `<span class="res">${goals}</span>` : "<span></span>"}</div>`;
    };
    const retro = m.prediction && m.prediction.retro
      ? ` <span class="badge" title="Predicted after kickoff using only data from before the match. Not counted in live accuracy.">backfilled</span>` : "";
    const verdict = opts.verdict == null ? "" : opts.verdict ? "hit" : "miss";
    return `<article class="card ${verdict}" tabindex="0" ${body ? "data-expand" : ""}>
      <div class="card-top">${MP.compTag(m)}${opts.badge || ""}${retro}${status}<span class="when">${when}</span></div>
      <div class="matchup"><div class="sides">${side(m.home, r && r.home, r && r.away)}${side(m.away, r && r.away, r && r.home)}</div>
        <div class="centre">${centre || ""}</div></div>
      ${summary}
      ${body ? `<button class="toggle" type="button" aria-expanded="false"><span class="tl">Details</span> <span class="chev">▾</span></button><div class="more" hidden>${body}</div>` : ""}
    </article>`;
  };
  MP.bindExpand = root => root.querySelectorAll("[data-expand]").forEach(c => {
    const more = c.querySelector(".more"), btn = c.querySelector(".toggle");
    if (!more) return;
    const toggle = () => {
      more.hidden = !more.hidden;
      c.classList.toggle("open", !more.hidden);
      if (btn) { btn.setAttribute("aria-expanded", String(!more.hidden)); btn.querySelector(".tl").textContent = more.hidden ? "Details" : "Hide details"; }
    };
    c.addEventListener("click", e => { if (e.target.closest(".more a, .more table")) return; if (!e.target.closest("a")) toggle(); });
    c.addEventListener("keydown", e => { if (e.target === c && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(); } });
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
