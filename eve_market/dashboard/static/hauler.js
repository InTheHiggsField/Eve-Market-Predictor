// Hauler page. The profit math is a line-for-line port of eve_market/hauling.py (keep the two in step):
// the page fetches the raw inputs (order books, volumes, routes) once per origin and works out every
// quantity / skill / mode change here, so the static GitHub Pages build behaves like the local dashboard.

// ---------------------------------------------------------------- math (mirrors hauling.py)
const H = (() => {
  function feeRates(accounting, broker, d) {
    const acc = Math.min(Math.max(parseInt(accounting) || 0, 0), 5), br = Math.min(Math.max(parseInt(broker) || 0, 0), 5);
    return { sales_tax: d.sales_tax * (1 - d.accounting_step * acc), broker_fee: Math.max(d.broker_fee - d.broker_step * br, 0) };
  }
  function walk(levels, qty) {
    let got = 0, isk = 0;
    for (const [price, vol] of levels) {
      if (got >= qty) break;
      const take = Math.min(vol, qty - got);
      got += take; isk += take * price;
    }
    return [got, isk];
  }
  const depth = levels => levels.reduce((s, l) => s + l[1], 0);
  const tick = p => Math.max(10 ** (Math.floor(Math.log10(p)) - 3), 0.01);
  const round2 = x => Math.round(x * 100) / 100;
  function undercut(p) {
    if (p <= 0.01) return p;
    const step = tick(p);
    let below = (Math.ceil(p / step - 1e-9) - 1) * step;
    if (below < 10 ** Math.floor(Math.log10(p)) && step > 0.01) below = p - step / 10;
    return round2(below);
  }
  function overbid(p) { const step = tick(p); return round2((Math.floor(p / step + 1e-9) + 1) * step); }
  function maxProfitable(asks, bids, buyMult, sellMult) {
    let i = 0, j = 0, aLeft = asks.length ? asks[0][1] : 0, bLeft = bids.length ? bids[0][1] : 0, units = 0;
    while (i < asks.length && j < bids.length) {
      if (bids[j][0] * sellMult <= asks[i][0] * buyMult) break;
      const take = Math.min(aLeft, bLeft);
      units += take; aLeft -= take; bLeft -= take;
      if (aLeft <= 0) { i++; aLeft = i < asks.length ? asks[i][1] : 0; }
      if (bLeft <= 0) { j++; bLeft = j < bids.length ? bids[j][1] : 0; }
    }
    return units;
  }
  function autoQty(item, origin, budget) {
    const book = (item.books || {})[origin];
    if (!book || !book.asks.length) return 1;
    let got = 0, isk = 0;
    for (const [price, vol] of book.asks) {
      const take = Math.min(vol, Math.max(Math.floor((budget - isk) / price), 0));
      got += take; isk += take * price;
      if (take < vol) break;
    }
    return Math.max(Math.floor(got), 1);
  }
  const n0 = v => v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  const ROUTE_KEYS = ["jumps", "jumps_secure", "low", "null", "low_secure", "null_secure"];

  function evaluate(item, origin, dest, qty, fees, mode, buyMode, route, d) {
    const books = item.books || {}, ob = books[origin], db = books[dest], flags = [];
    const row = { destination: dest, qty, filled: 0, buy_price: null, sell_price: null, cost: null, revenue: null,
      sales_tax: null, broker: null, net_profit: null, profit_per_unit: null, roi: null, m3: null, profit_per_m3: null,
      profit_per_jump: null, daily_volume: (item.daily_volume || {})[dest] ?? null, days_to_sell: null,
      max_profitable: null, risk: null, flags, systems: route ? route.systems : null };
    for (const k of ROUTE_KEYS) row[k] = route ? route[k] ?? null : null;
    if (item.global_market) { flags.push("one global market: nothing to haul"); return row; }
    if (!ob) { flags.push(`no order snapshot at ${origin}`); return row; }
    if (!db) { flags.push(`no order snapshot at ${dest}`); return row; }
    for (const [hub, b] of [[origin, ob], [dest, db]]) if (b.stale) flags.push(`${hub} snapshot is old (${b.taken_at})`);
    if (qty <= 0) { flags.push("doesn't fit in the cargo hold"); return row; }

    let buyPrice = null, buyAvail, buyMult;
    if (buyMode === "order") {
      if (!ob.bids.length) { flags.push(`no buy orders at ${origin} to outbid`); return row; }
      buyPrice = overbid(ob.bids[0][0]); buyAvail = qty; buyMult = 1 + fees.broker_fee;
      flags.push("buy order: fill time not estimated");
    } else {
      if (!ob.asks.length) { flags.push(`no sell orders at ${origin}`); return row; }
      buyAvail = depth(ob.asks); buyMult = 1;
    }
    let sellPrice = null, sellAvail, sellMult;
    if (mode === "patient") {
      const ref = (item.ref_price || {})[dest];
      if (db.asks.length) {
        sellPrice = undercut(db.asks[0][0]);
        if (ref && sellPrice > ref * d.listing_sanity) {
          flags.push(`${dest} listings are ${(sellPrice / ref).toFixed(1)}x the daily average: priced at the average`);
          sellPrice = ref;
        }
      } else if (ref) {
        sellPrice = ref; flags.push(`no sell orders at ${dest}: priced at its daily average`);
      } else { flags.push(`no sell orders or price history at ${dest}`); return row; }
      sellAvail = qty; sellMult = 1 - fees.sales_tax - fees.broker_fee;
    } else {
      if (!db.bids.length) { flags.push(`no buy orders at ${dest}`); return row; }
      sellAvail = depth(db.bids); sellMult = 1 - fees.sales_tax;
    }
    const filled = Math.min(qty, buyAvail, sellAvail);
    if (filled < qty) flags.push(`only ${n0(filled)} of ${n0(qty)} fillable from ${buyAvail < sellAvail ? origin + " sell orders" : dest + " buy orders"}`);
    const grossCost = buyPrice === null ? walk(ob.asks, filled)[1] : buyPrice * filled;
    const grossRev = sellPrice === null ? walk(db.bids, filled)[1] : sellPrice * filled;
    const buyBroker = grossCost * (buyMult - 1), tax = grossRev * fees.sales_tax;
    const sellBroker = mode === "patient" ? grossRev * fees.broker_fee : 0;
    const cost = grossCost + buyBroker, net = grossRev - tax - sellBroker - cost;
    Object.assign(row, { filled, cost, revenue: grossRev, sales_tax: tax, broker: buyBroker + sellBroker, net_profit: net,
      buy_price: filled ? grossCost / filled : null, sell_price: filled ? grossRev / filled : null });
    if (filled) { row.profit_per_unit = net / filled; row.roi = cost ? net / cost : null; }
    if (item.volume_m3) { row.m3 = item.volume_m3 * filled; row.profit_per_m3 = row.m3 ? net / row.m3 : null; }
    else flags.push("item volume unknown");
    const jumps = row.jumps_secure ?? row.jumps;
    if (jumps) row.profit_per_jump = net / jumps;
    else if (!route) flags.push("route unknown");
    const dv = row.daily_volume;
    if (mode === "patient") { row.days_to_sell = dv ? filled / dv : null; if (!dv) flags.push(`no recent trades in ${dest}'s region`); }
    else row.days_to_sell = 0;
    if (buyMode === "instant" && mode === "instant") row.max_profitable = maxProfitable(ob.asks, db.bids, buyMult, sellMult);
    return row;
  }
  const byProfit = (a, b) => (a.net_profit === null) - (b.net_profit === null) || (b.net_profit || 0) - (a.net_profit || 0);

  function rankDestinations(inputs, item, qty, opts) {
    const d = inputs.defaults, origin = inputs.origin;
    let q = qty ? qty : autoQty(item, origin, opts.budget);
    if (opts.cargo_m3 && item.volume_m3) q = Math.min(q, Math.floor(opts.cargo_m3 / item.volume_m3));
    const rows = [];
    for (const dest of Object.keys(inputs.hubs)) {
      if (dest === origin) continue;
      let qd = q;
      const dv = (item.daily_volume || {})[dest];
      if (!qty && opts.mode === "patient" && dv) qd = Math.min(q, Math.max(Math.ceil(dv * d.auto_sell_days), 1));
      rows.push(evaluate(item, origin, dest, qd, opts.fees, opts.mode, opts.buy_mode, (inputs.routes || {})[dest], d));
    }
    return rows.sort(byProfit);
  }
  function basketTotals(perItem) {
    const out = {};
    for (const rows of perItem) for (const r of rows) {
      const t = out[r.destination] ||= { destination: r.destination, net_profit: 0, cost: 0, revenue: 0, m3: 0, items: 0,
        missing: 0, days_to_sell: 0, risk: null, systems: r.systems, ...Object.fromEntries(ROUTE_KEYS.map(k => [k, r[k]])) };
      if (r.net_profit === null) { t.missing++; continue; }
      t.items++; t.net_profit += r.net_profit; t.cost += r.cost; t.revenue += r.revenue; t.m3 += r.m3 || 0;
      t.days_to_sell = Math.max(t.days_to_sell, r.days_to_sell || 0);
    }
    return Object.values(out).map(t => {
      if (!t.items) Object.assign(t, { net_profit: null, cost: null, revenue: null });
      t.roi = t.cost ? t.net_profit / t.cost : null;
      const jumps = t.jumps_secure ?? t.jumps;
      t.profit_per_jump = jumps && t.net_profit !== null ? t.net_profit / jumps : null;
      return t;
    }).sort(byProfit);
  }
  function bestRuns(inputs, qty, opts, top = 25) {
    const out = [];
    for (const item of inputs.items) for (const r of rankDestinations(inputs, item, qty, opts))
      if (r.net_profit !== null && r.net_profit > 0) out.push({ type_id: item.type_id, name: item.name, group: item.group || "", ...r });
    return out.sort(byProfit).slice(0, top);
  }
  return { feeRates, walk, undercut, overbid, maxProfitable, autoQty, evaluate, rankDestinations, basketTotals, bestRuns };
})();
if (typeof module !== "undefined") module.exports = H;

// ---------------------------------------------------------------- page
if (typeof document !== "undefined") (() => {
  const store = { get(k, d) { try { const v = localStorage.getItem("hauler." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
                  set(k, v) { try { localStorage.setItem("hauler." + k, JSON.stringify(v)); } catch (e) {} } };
  const state = { accounting: store.get("accounting", 0), broker: store.get("broker", 0), mode: store.get("mode", "instant"),
    buy_mode: store.get("buy_mode", "instant"), cargo: store.get("cargo", ""), budget: store.get("budget", null),
    qty: store.get("qty", ""), basket: store.get("basket", [44992]) };
  let inputs = null;
  const $ = id => document.getElementById(id);

  const num = (v, f) => `<td class="num">${v === null || v === undefined || Number.isNaN(v) ? "–" : f(v)}</td>`;
  const isk = v => `<span class="${pctClass(v)}">${fmtIsk(v)}</span>`;
  const units = v => v >= 1e5 ? fmtIsk(v) : v.toLocaleString("en-US", { maximumFractionDigits: v < 10 ? 2 : 0 });
  const roi = v => `<span class="${pctClass(v)}">${fmtPct(v * 100)}</span>`;
  const days = v => v === 0 ? "now" : v < 1 ? "<1" : v >= 100 ? fmtIsk(v) : v.toFixed(1);

  function routeCells(r) {
    const path = (r.systems || []).map(s => `${s.name} ${s.security === null ? "?" : s.security.toFixed(1)}`).join(" → ");
    const safe = r.jumps_secure ?? null, short = r.jumps ?? null;
    const lowNull = r.low === null || r.low === undefined ? "–" : `${r.low} / ${r.null}`;
    const safeRisk = r.low_secure || r.null_secure ? ` <span class="tag" style="color:var(--warn)" title="No high-sec-only route exists">${r.low_secure} low, ${r.null_secure} null</span>` : "";
    return `<td class="num">${safe ?? "–"}${safeRisk}</td><td class="num muted">${short ?? "–"}</td>` +
      `<td class="num" title="Shortest route: ${esc(path)}">${lowNull}</td>`;
  }
  const HEAD = `<tr><th>Destination</th><th class="num" title="Jumps on the high-sec-only route (used for profit per jump)">Jumps</th>
    <th class="num" title="Jumps on the shortest route">Short</th><th class="num" title="Low-sec / null-sec systems on the shortest route (hover a cell for the systems)">Low/Null</th>`;
  const flagTags = fl => fl.map(f => `<span class="tag" style="color:var(--warn)">${esc(f)}</span>`).join(" ");

  function itemTable(rows) {
    const head = HEAD + `<th class="num">Units</th><th class="num" title="Average price paid per unit at the origin">Buy</th>
      <th class="num" title="Average price received per unit at the destination, before fees">Sell</th>
      <th class="num" title="Sales tax plus broker fees">Fees</th><th class="num">Net profit</th><th class="num">Per unit</th>
      <th class="num">ROI</th><th class="num">m³</th><th class="num">Per m³</th><th class="num">Per jump</th>
      <th class="num" title="Median units traded per day in the destination's region, last 30 days">Units/day</th>
      <th class="num" title="Quantity / units per day (instant sales sell now)">Days</th>
      <th class="num" title="Instant buy and sell: units worth carrying before the next unit loses money">Max profitable</th><th>Notes</th></tr>`;
    const body = rows.map((r, i) => `<tr class="${i === 0 && r.net_profit > 0 ? "best" : ""}" style="cursor:default">
      <td><b>${esc(r.destination)}</b></td>${routeCells(r)}
      ${num(r.filled, units)}${num(r.buy_price, fmtIsk)}${num(r.sell_price, fmtIsk)}
      ${num(r.sales_tax === null ? null : r.sales_tax + r.broker, fmtIsk)}${num(r.net_profit, v => `<b>${isk(v)}</b>`)}
      ${num(r.profit_per_unit, isk)}${num(r.roi, roi)}${num(r.m3, units)}${num(r.profit_per_m3, isk)}${num(r.profit_per_jump, isk)}
      ${num(r.daily_volume, units)}${num(r.days_to_sell, days)}${num(r.max_profitable, units)}<td>${flagTags(r.flags)}</td></tr>`).join("");
    return `<thead>${head}</thead><tbody>${body}</tbody>`;
  }

  function options() {
    return { fees: H.feeRates(state.accounting, state.broker, inputs.defaults), mode: state.mode, buy_mode: state.buy_mode,
      cargo_m3: parseFloat(state.cargo) || null, budget: parseFloat(state.budget) || inputs.defaults.budget };
  }
  const qty = () => parseFloat(state.qty) || null;

  function renderBasketTags() {
    const names = Object.fromEntries((inputs?.items || []).map(i => [i.type_id, i.name]));
    $("basket").innerHTML = state.basket.length > 1 ? `<span class="muted" style="font:600 11px/1.4 var(--hud);letter-spacing:1px;text-transform:uppercase">Basket</span> ` +
      state.basket.map(t => `<span class="tag">${esc(names[t] || t)} <a href="#" data-rm="${t}" style="text-decoration:none">×</a></span>`).join(" ") : "";
    $("basket").querySelectorAll("[data-rm]").forEach(a => a.addEventListener("click", e => {
      e.preventDefault(); state.basket = state.basket.filter(t => t !== +a.dataset.rm); save(); render();
    }));
  }

  function render() {
    if (!inputs) return;
    renderBasketTags();
    const opts = options(), byId = Object.fromEntries(inputs.items.map(i => [i.type_id, i]));
    const chosen = state.basket.map(t => byId[t]).filter(Boolean);
    const per = chosen.map(i => ({ item: i, rows: H.rankDestinations(inputs, i, qty(), opts) }));
    const f = opts.fees;
    $("asof").textContent = `From ${inputs.origin}. Order books as of ${inputs.taken_at || "–"} UTC. ` +
      `Sales tax ${(f.sales_tax * 100).toFixed(2)}%, broker fee ${(f.broker_fee * 100).toFixed(2)}%${state.mode === "patient" || state.buy_mode === "order" ? "" : " (not charged: instant buy and sell)"}.`;

    $("items").innerHTML = per.map(({ item, rows }) => {
      const q = rows.length ? Math.max(...rows.map(r => r.qty)) : 0;
      return `<div class="panel" style="margin-bottom:16px"><div class="card-head"><h2>${esc(item.name)} <span class="tag">${esc(item.group)}</span></h2>
        <span class="muted" style="font-size:12px">${qty() ? "" : "auto "}${units(q)} units · ${item.volume_m3 ? units(item.volume_m3) + " m³ each" : "volume unknown"}</span></div>
        <div class="table-wrap"><table>${itemTable(rows)}</table></div></div>`;
    }).join("");

    const summary = $("summary");
    if (chosen.length > 1) {
      const totals = H.basketTotals(per.map(p => p.rows));
      $("basket-panel").hidden = false;
      $("basket-tbl").innerHTML = `<thead>${HEAD}<th class="num">Items priced</th><th class="num">Cost</th><th class="num">Net profit</th><th class="num">ROI</th><th class="num">m³</th><th class="num">Per jump</th><th class="num" title="Slowest item">Days</th></tr></thead><tbody>` +
        totals.map((t, i) => `<tr class="${i === 0 && t.net_profit > 0 ? "best" : ""}" style="cursor:default"><td><b>${esc(t.destination)}</b></td>${routeCells(t)}
          <td class="num">${t.items}/${t.items + t.missing}</td>${num(t.cost, fmtIsk)}${num(t.net_profit, v => `<b>${isk(v)}</b>`)}${num(t.roi, roi)}${num(t.m3, units)}${num(t.profit_per_jump, isk)}${num(t.days_to_sell, days)}</tr>`).join("") + "</tbody>";
      const b = totals[0];
      summary.hidden = false;
      summary.innerHTML = b && b.net_profit > 0
        ? `<b>Best for the basket:</b> ${esc(b.destination)}, ${fmtIsk(b.net_profit)} ISK net (${fmtPct(b.roi * 100)}) over ${b.jumps_secure ?? b.jumps} jumps.`
        : `No destination makes a profit on this basket with these settings.`;
    } else {
      $("basket-panel").hidden = true;
      const p = per[0], b = p && p.rows[0];
      summary.hidden = !p;
      if (p) summary.innerHTML = b && b.net_profit > 0
        ? `<b>Best:</b> haul ${units(b.filled)} ${esc(p.item.name)} to <b>${esc(b.destination)}</b> for ${fmtIsk(b.net_profit)} ISK net (${fmtPct(b.roi * 100)}, ${fmtIsk(b.profit_per_jump)}/jump over ${b.jumps_secure ?? b.jumps} jumps).`
        : p.item.global_market ? `${esc(p.item.name)} trades on one global market: buying and selling happen from the vault at any hub, so there is nothing to haul.`
        : `No hub pays more than ${esc(inputs.origin)} for ${esc(p.item.name)} after fees with these settings.${state.mode === "instant" ? " Try Patient selling." : ""}`;
    }

    const runs = H.bestRuns(inputs, qty(), opts, 25);
    $("best-title").textContent = `Best runs from ${inputs.origin}`;
    $("best-note").textContent = `${state.mode === "patient" ? "listed (patient) sales" : "instant sales"}, top ${runs.length}`;
    $("best-tbl").innerHTML = `<thead><tr><th>Item</th>${HEAD.replace("<tr>", "")}<th class="num">Units</th><th class="num">Cost</th><th class="num">Net profit</th><th class="num">ROI</th><th class="num">Per m³</th><th class="num">Per jump</th><th class="num">Days</th><th>Notes</th></tr></thead><tbody>` +
      (runs.length ? runs.map(r => `<tr data-t="${r.type_id}" title="Click to look at this item"><td>${esc(r.name)} <span class="tag">${esc(r.group)}</span></td>
        <td><b>${esc(r.destination)}</b></td>${routeCells(r)}${num(r.filled, units)}${num(r.cost, fmtIsk)}${num(r.net_profit, v => `<b>${isk(v)}</b>`)}
        ${num(r.roi, roi)}${num(r.profit_per_m3, isk)}${num(r.profit_per_jump, isk)}${num(r.days_to_sell, days)}<td>${flagTags(r.flags)}</td></tr>`).join("")
        : `<tr><td colspan="14" class="loading">Nothing is profitable to haul from ${esc(inputs.origin)} with these settings.${state.mode === "instant" ? " Instant arbitrage between hubs is rare; try Patient selling." : ""}</td></tr>`) + "</tbody>";
    $("best-tbl").querySelectorAll("tr[data-t]").forEach(tr => tr.addEventListener("click", () => {
      state.basket = [+tr.dataset.t]; $("item-pick").value = tr.dataset.t; save(); render(); window.scrollTo({ top: 0, behavior: "smooth" });
    }));
  }

  function save() { for (const k of Object.keys(state)) store.set(k, state[k]); }

  async function load() {
    $("items").innerHTML = `<div class="panel loading" style="margin-bottom:16px">Loading order books…</div>`;
    try {
      inputs = await getJSON(apiUrl("hauling/inputs", { market: currentMarket() }));
      if (state.budget === null) $("budget").value = inputs.defaults.budget;
      render();
    } catch (e) {
      $("items").innerHTML = `<div class="panel down" style="margin-bottom:16px">Could not load hauling data: ${esc(e.message)}</div>`;
    }
  }

  // controls
  for (const [id, key] of [["qty", "qty"], ["cargo", "cargo"], ["budget", "budget"], ["accounting", "accounting"], ["broker", "broker"]]) {
    const el = $(id);
    if (state[key] !== null && state[key] !== "") el.value = state[key];
    el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => { state[key] = el.value; save(); render(); });
  }
  for (const [id, key] of [["mode-seg", "mode"], ["buy-seg", "buy_mode"]]) {
    const btns = document.querySelectorAll(`#${id} button`);
    btns.forEach(b => {
      b.classList.toggle("on", b.dataset.v === state[key]);
      b.addEventListener("click", () => { btns.forEach(x => x.classList.toggle("on", x === b)); state[key] = b.dataset.v; save(); render(); });
    });
  }
  const pick = $("item-pick");
  if (state.basket.length && [...pick.options].some(o => +o.value === state.basket[0])) pick.value = state.basket[0];
  else state.basket = [+pick.value];
  pick.addEventListener("change", () => { if (state.basket.length <= 1) { state.basket = [+pick.value]; save(); render(); } });
  $("item-set").addEventListener("click", () => { state.basket = [+pick.value]; save(); render(); });
  $("item-add").addEventListener("click", () => { if (!state.basket.includes(+pick.value)) state.basket.push(+pick.value); save(); render(); });
  initMarketPicker(load);
  load();
})();
