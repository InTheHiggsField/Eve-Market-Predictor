chartDefaults();
const state = { list: "all", group: "", q: "", sortKey: "isk_30d", sortDir: -1, selected: null, range: 90, overview: "isk_30d" };
let items = [], detailChart, overviewChart, historyCache = {};

function seg(id, key, cb) {
  document.querySelectorAll(`#${id} button`).forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(`#${id} button`).forEach(x => x.classList.toggle("on", x === b));
    state[key] = isNaN(+b.dataset.v) ? b.dataset.v : +b.dataset.v;
    cb();
  }));
}

function filtered() {
  const q = state.q.toLowerCase();
  return items.filter(i => (state.list === "all" || i.list === state.list) && (!state.group || i.group === state.group)
    && (!q || i.name.toLowerCase().includes(q)));
}

function sortRows(rows) {
  const k = state.sortKey, d = state.sortDir;
  return rows.sort((a, b) => {
    const x = a[k], y = b[k];
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return typeof x === "string" ? x.localeCompare(y) * d : (x - y) * d;
  });
}

function renderTable() {
  const rows = sortRows(filtered());
  document.querySelectorAll("#tbl th[data-k]").forEach(th => {
    th.querySelector(".arrow")?.remove();
    if (th.dataset.k === state.sortKey) th.insertAdjacentHTML("beforeend", `<span class="arrow">${state.sortDir > 0 ? "▲" : "▼"}</span>`);
  });
  const pc = v => `<td class="num ${pctClass(v)}">${fmtPct(v)}</td>`;
  document.querySelector("#tbl tbody").innerHTML = rows.length ? rows.map(i => `
    <tr data-id="${i.type_id}" class="${i.type_id === state.selected ? "selected" : ""}">
      <td>${esc(i.name)}</td><td><span class="tag">${esc(i.group)}</span></td>
      <td class="num">${fmtIsk(i.price)}</td>${pc(i.chg_1d)}${pc(i.chg_7d)}${pc(i.chg_30d)}
      <td class="num">${fmtIsk(i.best_bid)}</td><td class="num">${fmtIsk(i.best_ask)}</td>
      <td class="num">${i.spread_pct === null ? "–" : i.spread_pct.toFixed(1) + "%"}</td>
      <td class="num">${fmtIsk(i.volume_30d)}</td><td class="num">${fmtIsk(i.isk_30d)}</td><td class="num">${fmtIsk(i.orders_30d)}</td>
      <td>${sparkline(i.spark)}</td>
    </tr>`).join("") : `<tr><td colspan="13" class="loading">No items match.</td></tr>`;
  document.querySelectorAll("#tbl tbody tr[data-id]").forEach(tr => tr.addEventListener("click", () => select(+tr.dataset.id)));
}

function renderOverview() {
  const k = state.overview;
  const rows = filtered().filter(i => i[k] !== null).sort((a, b) => b[k] - a[k]).slice(0, k === "isk_30d" ? 15 : 41);
  const isPct = k !== "isk_30d";
  const colors = rows.map(i => isPct ? (i[k] >= 0 ? css("--up") : css("--down")) : css("--accent"));
  overviewChart?.destroy();
  overviewChart = new Chart(document.getElementById("overview-chart"), {
    type: "bar",
    data: { labels: rows.map(i => i.name), datasets: [{ data: rows.map(i => i[k]), backgroundColor: colors, borderRadius: 3 }] },
    options: {
      indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false },
        tooltip: { callbacks: { label: c => isPct ? fmtPct(c.raw) : fmtIsk(c.raw) + " ISK/day" } } },
      scales: { x: { type: isPct ? "linear" : "logarithmic", ticks: { callback: v => isPct ? v + "%" : (Math.abs(Math.log10(v) - Math.round(Math.log10(v))) < 1e-9 ? fmtIsk(v) : "") } }, y: { ticks: { autoSkip: false, font: { size: 11 } } } },
      onClick: (_, els) => { if (els.length) select(rows[els[0].index].type_id); },
    },
  });
}

async function select(typeId) {
  state.selected = typeId;
  renderTable();
  const item = items.find(i => i.type_id === typeId);
  document.getElementById("detail-title").textContent = item.name;
  document.getElementById("detail-stats").innerHTML =
    `<span>Avg <b>${fmtIsk(item.price)}</b></span><span>7d <b class="${pctClass(item.chg_7d)}">${fmtPct(item.chg_7d)}</b></span>` +
    `<span>Bid/ask <b>${fmtIsk(item.best_bid)} / ${fmtIsk(item.best_ask)}</b></span><span>ISK/day <b>${fmtIsk(item.isk_30d)}</b></span>`;
  const key = currentMarket() + ":" + typeId;
  historyCache[key] ||= await getJSON(`/api/history/${typeId}?market=${encodeURIComponent(currentMarket())}`);
  if (state.selected === typeId) renderDetail();
}

function renderDetail() {
  const h = historyCache[currentMarket() + ":" + state.selected];
  if (!h) return;
  const n = Math.min(state.range, h.dates.length), s = h.dates.length - n, sl = a => a.slice(s);
  detailChart?.destroy();
  const accent = css("--accent");
  detailChart = new Chart(document.getElementById("detail-chart"), {
    data: {
      labels: sl(h.dates),
      datasets: [
        { type: "line", label: "Average", data: sl(h.average), borderColor: accent, borderWidth: 2, pointRadius: 0, yAxisID: "y", tension: .15 },
        { type: "line", label: "High", data: sl(h.highest), borderColor: accent + "55", borderWidth: 1, pointRadius: 0, yAxisID: "y", fill: "+1", backgroundColor: accent + "18" },
        { type: "line", label: "Low", data: sl(h.lowest), borderColor: accent + "55", borderWidth: 1, pointRadius: 0, yAxisID: "y" },
        { type: "bar", label: "Volume", data: sl(h.volume), backgroundColor: css("--muted") + "40", yAxisID: "v" },
      ],
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmtIsk(c.raw)}` } } },
      scales: {
        x: { ticks: { maxTicksLimit: 8 } },
        y: { position: "left", ticks: { callback: fmtIsk } },
        v: { position: "right", grid: { display: false }, ticks: { callback: fmtIsk }, max: Math.max(...sl(h.volume)) * 3 },
      },
    },
  });
}

function renderAll() { renderTable(); renderOverview(); }

async function loadMarket() {
  document.querySelector("#tbl tbody").innerHTML = `<tr><td colspan="13" class="loading">Loading…</td></tr>`;
  items = await loadItems();
  const m = document.getElementById("market"), label = m ? m.options[m.selectedIndex].text : currentMarket();
  const latest = items.map(i => i.date).sort().pop();
  document.getElementById("asof").textContent =
    `${label}: daily history for the whole region, bid/ask at the hub station. PLEX comes from the global PLEX market. Latest day: ${latest}. ${items.length} items.`;
  const keep = items.find(i => i.type_id === state.selected);
  renderAll();
  select(keep ? keep.type_id : items.slice().sort((a, b) => b.isk_30d - a.isk_30d)[0].type_id);
}

(async () => {
  initMarketPicker(loadMarket);
  items = await loadItems();
  const groups = [...new Set(items.map(i => i.group))];
  document.getElementById("group").insertAdjacentHTML("beforeend", groups.map(g => `<option>${esc(g)}</option>`).join(""));
  document.querySelectorAll("#tbl th[data-k]").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.k;
    state.sortDir = state.sortKey === k ? -state.sortDir : (k === "name" || k === "group" ? 1 : -1);
    state.sortKey = k; renderTable();
  }));
  seg("list-seg", "list", renderAll);
  seg("range-seg", "range", renderDetail);
  seg("overview-seg", "overview", renderOverview);
  document.getElementById("group").addEventListener("change", e => { state.group = e.target.value; renderAll(); });
  document.getElementById("search").addEventListener("input", e => { state.q = e.target.value; renderAll(); });
  loadMarket();
})();
