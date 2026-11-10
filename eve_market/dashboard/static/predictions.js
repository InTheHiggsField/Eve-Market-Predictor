chartDefaults();
const state = { list: "core", model: "ridge", horizon: 7, group: "" };
const MODEL_LABEL = { no_change: "No change (baseline)", momentum: "Momentum (baseline)", ridge: "Ridge", gbm: "Gradient boosting" };
let data = null, charts = [], rankChart, reqId = 0;

function seg(id, key) {
  document.querySelectorAll(`#${id} button`).forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(`#${id} button`).forEach(x => x.classList.toggle("on", x === b));
    state[key] = isNaN(+b.dataset.v) ? b.dataset.v : +b.dataset.v;
    load();
  }));
}

function addDays(iso, n) { const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }

function renderSummary() {
  const rows = data.summary;
  const base = rows.find(r => r.model === "no_change").mae;
  const best = rows.filter(r => r.model !== "no_change").sort((a, b) => a.mae - b.mae)[0];
  document.querySelector("#summary").innerHTML = `
    <thead><tr><th>Model</th><th class="num">MAE</th><th class="num">vs. no change</th><th class="num">Direction</th></tr></thead>
    <tbody>${rows.map(r => `<tr style="cursor:default" class="${r.model === state.model ? "selected" : ""}">
      <td>${MODEL_LABEL[r.model]}</td><td class="num">${r.mae.toFixed(4)}</td>
      <td class="num ${r.mae < base ? "up" : r.mae > base ? "down" : "muted"}">${r.model === "no_change" ? "–" : fmtPct((r.mae / base - 1) * 100)}</td>
      <td class="num">${r.model === "no_change" ? "–" : (r.direction_acc * 100).toFixed(1) + "%"}</td></tr>`).join("")}</tbody>`;
  const sel = rows.find(r => r.model === state.model), v = document.getElementById("verdict");
  v.hidden = false;
  v.innerHTML = sel.mae < base
    ? `<b>${MODEL_LABEL[state.model]}</b> beats the no-change baseline on this list (error ${fmtPct((sel.mae / base - 1) * 100)}, direction right ${(sel.direction_acc * 100).toFixed(0)}% of the time). Treat forecasts as a weak signal, not a sure thing: the backtest ignores broker fees and sales tax.`
    : `<b>${MODEL_LABEL[state.model]}</b> does <b>not</b> beat simply assuming prices stay flat on this list (error ${fmtPct((sel.mae / base - 1) * 100)} vs. no change). Forecasts below are shown for exploration; don't trade on them.` +
      (best && best.mae < base ? ` ${MODEL_LABEL[best.model]} does slightly better here.` : "");
}

function renderRank() {
  const items = visibleItems().slice().sort((a, b) => b.pred_pct - a.pred_pct);
  rankChart?.destroy();
  const box = document.getElementById("rank-chart").parentElement;
  box.style.height = Math.max(300, items.length * 18) + "px";
  rankChart = new Chart(document.getElementById("rank-chart"), {
    type: "bar",
    data: { labels: items.map(i => i.name), datasets: [{ data: items.map(i => i.pred_pct),
      backgroundColor: items.map(i => i.pred_pct >= 0 ? css("--up") : css("--down")), borderRadius: 3 }] },
    options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: c => `${fmtPct(c.raw, 2)} → ${fmtIsk(items[c.dataIndex].pred_price)}` } } },
      scales: { x: { ticks: { callback: v => v + "%" } }, y: { ticks: { autoSkip: false, font: { size: 11 } } } },
      onClick: (_, els) => { if (els.length) document.getElementById("card-" + items[els[0].index].type_id).scrollIntoView({ behavior: "smooth", block: "center" }); } },
  });
}

function visibleItems() { return data.items.filter(i => !state.group || i.group === state.group); }

function renderCards() {
  charts.forEach(c => c.destroy()); charts = [];
  const items = visibleItems();
  document.getElementById("cards").innerHTML = items.map(i => `
    <div class="panel" id="card-${i.type_id}">
      <div class="card-head"><span class="name">${esc(i.name)}</span><span class="tag">${esc(i.group)}</span></div>
      <div class="stats">
        <span>Now <b>${fmtIsk(i.price)}</b></span>
        <span>${data.horizon}d forecast <b class="${pctClass(i.pred_pct)}">${fmtIsk(i.pred_price)} (${fmtPct(i.pred_pct)})</b></span>
        <span>Range <b>${fmtIsk(i.pred_low)}–${fmtIsk(i.pred_high)}</b></span>
        <span>Hit rate <b>${i.direction_acc === null ? "–" : (i.direction_acc * 100).toFixed(0) + "%"}</b></span>
      </div>
      <div class="chart-box small"><canvas></canvas></div>
    </div>`).join("");
  const accent = css("--accent"), warn = css("--warn"), up = css("--up"), down = css("--down");
  items.forEach(i => {
    const labels = i.history.dates.slice();
    for (let d = addDays(labels[labels.length - 1], 1); d <= i.target_date; d = addDays(d, 1)) labels.push(d);
    const idx = Object.fromEntries(labels.map((d, k) => [d, k]));
    const actual = labels.map((_, k) => k < i.history.price.length ? i.history.price[k] : null);
    const bt = labels.map(() => null);
    i.backtest.dates.forEach((d, k) => { if (d in idx) bt[idx[d]] = i.backtest.pred_price[k]; });
    const fc = labels.map(() => null), lo = labels.map(() => null), hi = labels.map(() => null);
    const last = i.history.price.length - 1, t = labels.length - 1;
    fc[last] = lo[last] = hi[last] = i.price;
    fc[t] = i.pred_price; lo[t] = i.pred_low; hi[t] = i.pred_high;
    const color = i.pred_pct >= 0 ? up : down;
    charts.push(new Chart(document.querySelector(`#card-${i.type_id} canvas`), {
      type: "line",
      data: { labels, datasets: [
        { label: "Actual", data: actual, borderColor: accent, borderWidth: 1.8, pointRadius: 0, tension: .15 },
        { label: "Past predictions", data: bt, borderColor: warn, backgroundColor: warn, borderWidth: 0, pointRadius: 1.8, showLine: false },
        { label: "Range high", data: hi, borderColor: "transparent", pointRadius: 0, spanGaps: true, fill: "+1", backgroundColor: color + "26" },
        { label: "Range low", data: lo, borderColor: "transparent", pointRadius: 0, spanGaps: true },
        { label: "Forecast", data: fc, borderColor: color, borderDash: [5, 4], borderWidth: 2, spanGaps: true,
          pointRadius: fc.map(v => v === null ? 0 : 3), pointBackgroundColor: color },
      ] },
      options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { labels: { filter: l => !l.text.startsWith("Range"), font: { size: 11 } } },
          tooltip: { filter: c => c.raw !== null, callbacks: { label: c => `${c.dataset.label}: ${fmtIsk(c.raw)}` } } },
        scales: { x: { ticks: { maxTicksLimit: 5, font: { size: 10 } } }, y: { ticks: { callback: fmtIsk, font: { size: 10 } } } } },
    }));
  });
}

function renderAll() { renderSummary(); renderRank(); renderCards(); }

async function load() {
  const my = ++reqId;
  document.getElementById("h-label").textContent = state.horizon;
  document.querySelector("#summary").innerHTML = `<tbody><tr><td class="loading">Training models… (a few seconds)</td></tr></tbody>`;
  document.getElementById("cards").style.opacity = .4;
  try {
    const d = await getJSON(`/api/predictions?list=${state.list}&model=${state.model}&horizon=${state.horizon}`);
    if (my !== reqId) return;
    data = d;
    const sel = document.getElementById("group"), groups = [...new Set(data.items.map(i => i.group))];
    if (!groups.includes(state.group)) state.group = "";
    sel.innerHTML = `<option value="">All groups</option>` + groups.map(g => `<option ${g === state.group ? "selected" : ""}>${esc(g)}</option>`).join("");
    renderAll();
  } catch (e) {
    document.querySelector("#summary").innerHTML = `<tbody><tr><td class="down">Failed to load predictions: ${esc(e.message)}</td></tr></tbody>`;
  } finally { document.getElementById("cards").style.opacity = 1; }
}

seg("list-seg", "list"); seg("model-seg", "model"); seg("horizon-seg", "horizon");
document.getElementById("group").addEventListener("change", e => { state.group = e.target.value; renderRank(); renderCards(); });
load();
