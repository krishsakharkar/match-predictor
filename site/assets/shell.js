/* Shell: merges every page module listed in <body data-pages="..."> into one view.
   One module -> a standalone page; several -> tabs. Shared: summary strip, competition filter,
   Upcoming / Results switch (all remembered per browser). */
(function () {
  "use strict";
  const { esc, pct } = MP;
  const app = document.getElementById("app");
  const ids = (document.body.dataset.pages || "results").split(",").map(s => s.trim()).filter(id => MP.pages[id]);
  const fromHash = location.hash.slice(1);
  let tab = ids.includes(fromHash) ? fromHash : (ids.includes(MP.store.get("mp.tab")) ? MP.store.get("mp.tab") : ids[0]);
  const q = new URLSearchParams(location.search);       // ?view=results&comp=UCL - shareable links
  let comp = q.get("comp") || MP.store.get("mp.comp") || "all";
  let view = ["upcoming", "results"].includes(q.get("view")) ? q.get("view") : (MP.store.get("mp.view") || "upcoming");

  // summary strip: what's coming, how sure the model is, how it has done on the shown results
  function strip(d, upcoming, played) {
    const now = new Date();
    const todayKey = now.toLocaleDateString("en-US", { timeZone: MP.TZ });
    const today = upcoming.filter(m => MP.localDay(m).toLocaleDateString("en-US", { timeZone: MP.TZ }) === todayKey).length;
    const th = (d.bankers || {}).threshold || 0.7;
    const sure = upcoming.filter(m => m.prediction && Math.max(m.prediction.p_home, m.prediction.p_draw, m.prediction.p_away) >= th).length;
    const judged = played.filter(m => m.hit && !(m.prediction && m.prediction.retro));
    const right = judged.filter(m => m.hit.result).length;
    const all = played.filter(m => m.hit);
    const allRight = all.filter(m => m.hit.result).length;
    const rec = judged.length ? `${right}/${judged.length}` : all.length ? `${allRight}/${all.length}` : "–";
    const recPct = judged.length ? pct(right / judged.length) : all.length ? pct(allRight / all.length) : "";
    return `<div class="strip">
      <div class="stat"><div class="k">Upcoming</div><div class="v">${upcoming.length}<small>${today ? `${today} today` : "next 7 days"}</small></div></div>
      <div class="stat"><div class="k">Bankers</div><div class="v">${sure}<small>≥ ${pct(th)}</small></div></div>
      <div class="stat"><div class="k">Last 7 days</div><div class="v">${rec}<small>${recPct ? `${recPct} right` : "results"}</small></div></div>
    </div>`;
  }

  MP.load("data/predictions.json").then(d => {
    const order = Object.keys(d.competitions);
    const present = [...new Set(d.matches.map(m => m.comp))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
    const tracked = new Set(d.tracked_teams || []);
    if (comp !== "all" && comp !== "mine" && !present.includes(comp)) comp = "all";
    const draw = () => {
      const page = MP.pages[tab];
      const tabs = ids.length > 1
        ? `<nav class="tabs" role="tablist">${ids.map(id => `<button role="tab" class="tab ${id === tab ? "on" : ""}" aria-selected="${id === tab}" data-tab="${id}">${esc(MP.pages[id].title)}</button>`).join("")}</nav>`
        : `<h1>${esc(page.title)}</h1>`;
      const chips = [["all", "All", null], ["mine", "★ My clubs", null], ...present.map(c => [c, (d.competitions[c] || {}).badge || c, MP.compColour(c)])]
        .map(([k, n, col]) => `<button class="chip ${k === comp ? "on" : ""}" data-c="${esc(k)}">${col ? `<span class="dot" style="--c:${col}"></span>` : ""}${esc(n)}</button>`).join("");
      const ms = d.matches.filter(m => comp === "all" || (comp === "mine" ? (tracked.has(m.home) || tracked.has(m.away)) : m.comp === comp));
      const now = new Date().toISOString();
      const upcoming = ms.filter(m => !m.result && (!m.kickoff_utc || m.kickoff_utc > now));
      const played = ms.filter(m => !upcoming.includes(m)).sort((a, b) => MP.localDay(b) - MP.localDay(a));
      const list = view === "results" ? played : upcoming;
      const empty = view === "results" ? "No results in the last 7 days." : "No fixtures in the next 7 days.";
      app.innerHTML = `${tabs}${strip(d, upcoming, played)}${MP.notes(d)}
        ${page.top && view === "upcoming" ? page.top(d, ms) : ""}
        <div class="controls"><div class="filters" role="group" aria-label="Competition">${chips}</div>
          <div class="view" role="group" aria-label="Show">
            <button data-v="upcoming" class="${view === "upcoming" ? "on" : ""}">Upcoming<span class="count">${upcoming.length}</span></button>
            <button data-v="results" class="${view === "results" ? "on" : ""}">Results<span class="count">${played.length}</span></button>
          </div></div>
        ${list.length ? MP.grouped(list, page.card) : `<div class="empty">${empty}</div>`}
        <p class="muted small updated">Updated ${esc(MP.stamp(d.generated_at))} · model ${esc(d.model_version)}</p>`;
      app.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => {
        tab = b.dataset.tab; MP.store.set("mp.tab", tab); history.replaceState(null, "", "#" + tab); draw();
      }));
      app.querySelectorAll("[data-c]").forEach(b => b.addEventListener("click", () => { comp = b.dataset.c; MP.store.set("mp.comp", comp); draw(); }));
      app.querySelectorAll("[data-v]").forEach(b => b.addEventListener("click", () => { view = b.dataset.v; MP.store.set("mp.view", view); draw(); }));
      MP.bindExpand(app);
    };
    draw();
  }).catch(e => MP.fail(app, e));
})();
