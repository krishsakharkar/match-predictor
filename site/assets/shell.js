/* Shell: merges every page module listed in <body data-pages="..."> into one view.
   One module -> a standalone page; several -> tabs. Shared filters: competition / tracked clubs. */
(function () {
  "use strict";
  const { esc } = MP;
  const app = document.getElementById("app");
  const ids = (document.body.dataset.pages || "results").split(",").map(s => s.trim()).filter(id => MP.pages[id]);
  const fromHash = location.hash.slice(1);
  let tab = ids.includes(fromHash) ? fromHash : (ids.includes(MP.store.get("mp.tab")) ? MP.store.get("mp.tab") : ids[0]);
  let comp = MP.store.get("mp.comp") || "all";

  MP.load("data/predictions.json").then(d => {
    const order = Object.keys(d.competitions);
    const present = [...new Set(d.matches.map(m => m.comp))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
    const tracked = new Set(d.tracked_teams || []);
    if (comp !== "all" && comp !== "mine" && !present.includes(comp)) comp = "all";
    const draw = () => {
      const page = MP.pages[tab];
      const tabs = ids.length > 1 ? `<nav class="tabs" role="tablist">${ids.map(id => `<button role="tab" class="tab ${id === tab ? "on" : ""}" aria-selected="${id === tab}" data-tab="${id}">${esc(MP.pages[id].title)}</button>`).join("")}</nav>` : `<h1>${esc(page.title)}</h1>`;
      const chips = [["all", "All"], ["mine", "Tracked clubs"], ...present.map(c => [c, (d.competitions[c] || {}).badge || c])]
        .map(([k, n]) => `<button class="chip ${k === comp ? "on" : ""}" data-c="${esc(k)}">${esc(n)}</button>`).join("");
      const ms = d.matches.filter(m => comp === "all" || (comp === "mine" ? (tracked.has(m.home) || tracked.has(m.away)) : m.comp === comp));
      const now = new Date().toISOString();
      const upcoming = ms.filter(m => !m.result && (!m.kickoff_utc || m.kickoff_utc > now));
      const played = ms.filter(m => !upcoming.includes(m)).sort((a, b) => MP.localDay(b) - MP.localDay(a));
      app.innerHTML = `${tabs}${MP.notes(d)}<div class="filters">${chips}</div>
        ${page.top ? page.top(d, ms) : ""}
        <h2>Upcoming</h2>${upcoming.length ? MP.grouped(upcoming, page.card) : `<div class="empty">No fixtures in the next 7 days.</div>`}
        <h2>Results</h2>${played.length ? MP.grouped(played, page.card) : `<div class="empty">No results in the last 7 days.</div>`}
        <p class="muted small">Updated ${esc(new Date(d.generated_at).toLocaleString())} · model ${esc(d.model_version)}</p>`;
      app.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => {
        tab = b.dataset.tab; MP.store.set("mp.tab", tab); history.replaceState(null, "", "#" + tab); draw();
      }));
      app.querySelectorAll("[data-c]").forEach(b => b.addEventListener("click", () => { comp = b.dataset.c; MP.store.set("mp.comp", comp); draw(); }));
      MP.bindExpand(app);
    };
    draw();
  }).catch(e => MP.fail(app, e));
})();
