chartDefaults();
const state = { list: "all", label: "", q: "", sortKey: "score", sortDir: -1 };
let data = { items: [] }, scatterChart, scoreChart;
const FEE = 0.05;  // rough round-trip cost in fees and tax

function seg(id, key) {
  document.querySelectorAll(`#${id} button`).forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(`#${id} button`).forEach(x => x.classList.toggle("on", x === b));
    state[key] = b.dataset.v;
    renderAll();
  }));
}

const frac = (v, d = 1) => v === null || v === undefined ? "–" : (v * 100).toFixed(d) + "%";
const labelColor = l => l === "Healthy" ? css("--up") : l === "Thin" ? css("--warn") : css("--down");

function filtered() {
  const q = state.q.toLowerCase();
  return data.items.filter(i => (state.list === "all" || i.list === state.list) && (!state.label || i.label === state.label)
    && (!q || i.name.toLowerCase().includes(q)));
}

function renderTable() {
  const k = state.sortKey, d = state.sortDir;
  const rows = filtered().sort((a, b) => {
    const x = a[k], y = b[k];
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return typeof x === "string" ? x.localeCompare(y) * d : (x - y) * d;
  });
  document.querySelectorAll("#tbl th[data-k]").forEach(th => {
    th.querySelector(".arrow")?.remove();
    if (th.dataset.k === k) th.insertAdjacentHTML("beforeend", `<span class="arrow">${d > 0 ? "▲" : "▼"}</span>`);
  });
  const n = (v, f) => `<td class="num">${v === null || v === undefined ? "–" : f(v)}</td>`;
  document.querySelector("#tbl tbody").innerHTML = rows.length ? rows.map(i => `
    <tr style="cursor:default">
      <td>${esc(i.name)} <span class="tag">${esc(i.group)}</span></td>
      <td class="num"><b>${i.score === null ? "–" : i.score.toFixed(0)}</b></td>
      <td><span style="color:${labelColor(i.label)};font-weight:600">${i.label}</span></td>
      ${n(i.isk_turnover, fmtIsk)}${n(i.trades, v => fmtIsk(v))}${n(i.zero_days, v => frac(v, 0))}
      ${n(i.quoted_spread, frac)}${n(i.effective_spread, frac)}${n(i.depth, fmtIsk)}
      ${n(i.imbalance, v => (v > 0 ? "+" : "") + v.toFixed(2))}
      ${n(i.amihud, v => v >= 100 ? "&gt;100%" : v < 0.01 ? "&lt;0.01%" : v.toFixed(2) + "%")}
      ${n(i.roll_spread, frac)}${n(i.volatility, v => frac(v, 0))}${n(i.bounce, v => v.toFixed(2))}
      ${n(i.hub_gap, frac)}${n(i.top_share, v => frac(v, 0))}${n(i.churn, v => frac(v, 0))}
      <td>${i.flags.map(f => `<span class="tag" style="color:var(--warn)">${esc(f)}</span>`).join(" ")}</td>
    </tr>`).join("") : `<tr><td colspan="18" class="loading">No items match.</td></tr>`;
}

function renderScatter() {
  // Quoted spread from station snapshots when we have it, else the spread implied by daily history.
  const rows = filtered().filter(i => i.isk_turnover > 0);
  const useQuoted = rows.filter(i => i.quoted_spread > 0).length >= rows.length / 2;
  const y = i => useQuoted ? i.quoted_spread : i.roll_spread;
  const pts = rows.filter(i => y(i) > 0);
  document.getElementById("scatter-note").textContent = useQuoted ? "spread quoted at the hub station" : "spread implied by daily history (Roll)";
  scatterChart?.destroy();
  if (!pts.length) return;
  const xs = pts.map(i => i.isk_turnover);
  const feeLine = { id: "fee", afterDatasetsDraw(c) {
    const yy = c.scales.y.getPixelForValue(FEE * 100), a = c.chartArea;
    if (yy < a.top || yy > a.bottom) return;
    const g = c.ctx; g.save(); g.setLineDash([5, 4]); g.strokeStyle = css("--muted"); g.beginPath();
    g.moveTo(a.left, yy); g.lineTo(a.right, yy); g.stroke();
    g.fillStyle = css("--muted"); g.font = "11px sans-serif"; g.fillText("~5% fees and tax", a.left + 6, yy - 5); g.restore();
  } };
  scatterChart = new Chart(document.getElementById("scatter"), {
    type: "scatter",
    data: { datasets: [{ data: pts.map(i => ({ x: i.isk_turnover, y: y(i) * 100 })),
      backgroundColor: pts.map(i => labelColor(i.label)), pointRadius: 5, pointHoverRadius: 7 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: c => { const i = pts[c.dataIndex];
        return `${i.name}: ${fmtIsk(i.isk_turnover)} ISK/day, spread ${frac(y(i))}, score ${i.score.toFixed(0)}`; } } } },
      scales: {
        x: { type: "logarithmic", min: Math.min(...xs) / 2, max: Math.max(...xs) * 2, title: { display: true, text: "ISK traded per day (log)" },
          ticks: { callback: v => Math.abs(Math.log10(v) - Math.round(Math.log10(v))) < 1e-9 ? fmtIsk(v) : "" } },
        y: { type: "logarithmic", title: { display: true, text: "Spread (log)" },
          ticks: { callback: v => [1, 2, 5].includes(+(v / 10 ** Math.floor(Math.log10(v))).toFixed(0)) ? v + "%" : "" } },
      } },
    plugins: [feeLine],
  });
}

function renderScore() {
  const rows = filtered().filter(i => i.score !== null).sort((a, b) => b.score - a.score);
  scoreChart?.destroy();
  const box = document.getElementById("score-chart").parentElement;
  box.style.height = Math.max(360, rows.length * 22 + 50) + "px";
  scoreChart = new Chart(document.getElementById("score-chart"), {
    type: "bar",
    data: { labels: rows.map(i => i.name), datasets: [{ data: rows.map(i => i.score), backgroundColor: rows.map(i => labelColor(i.label)), borderRadius: 3 }] },
    options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: c => `${rows[c.dataIndex].label}, score ${c.raw.toFixed(0)}` } } },
      scales: { x: { position: "top", min: 0, max: 100 }, y: { ticks: { autoSkip: false, font: { size: 11 } } } } },
  });
}

function renderSummary() {
  const rows = filtered(), el = document.getElementById("summary");
  const count = l => rows.filter(i => i.label === l).length;
  const flagged = rows.filter(i => i.label === "Healthy" && i.flags.length);
  el.hidden = false;
  el.innerHTML = `<b>${esc(data.market)}:</b> ${count("Healthy")} healthy, ${count("Thin")} thin and ${count("Illiquid")} illiquid of ${rows.length} items. ` +
    `Illiquid items (under ${fmtIsk(data.min_isk)} ISK a day, or a score under 35) are left out of the prediction models.` +
    (flagged.length ? ` ${flagged.length} healthy items carry a warning; check the last column.` : "");
}

function renderAll() { renderSummary(); renderTable(); renderScatter(); renderScore(); }

async function load() {
  document.querySelector("#tbl tbody").innerHTML = `<tr><td colspan="18" class="loading">Loading…</td></tr>`;
  try {
    data = await getJSON(apiUrl("health", { market: currentMarket() }));
    renderAll();
  } catch (e) {
    document.querySelector("#tbl tbody").innerHTML = `<tr><td colspan="18" class="down">Could not load health data: ${esc(e.message)}</td></tr>`;
  }
}

document.querySelectorAll("#tbl th[data-k]").forEach(th => th.addEventListener("click", () => {
  const k = th.dataset.k;
  state.sortDir = state.sortKey === k ? -state.sortDir : (k === "name" || k === "label" ? 1 : -1);
  state.sortKey = k; renderTable();
}));
seg("list-seg", "list"); seg("label-seg", "label");
document.getElementById("search").addEventListener("input", e => { state.q = e.target.value; renderAll(); });
initMarketPicker(load);
load();
