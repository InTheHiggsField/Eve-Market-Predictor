// Hauler page. The profit math is a line-for-line port of eve_market/hauling.py (keep the two in step):
// the page fetches the raw inputs (order books, volumes, the hub route matrix) once and works out every
// quantity / skill / mode / filter change here, so the static GitHub Pages build behaves like the local dashboard.

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

  function tripQty(item, origin, dest, qty, opts, d) {
    let q = qty ? qty : autoQty(item, origin, opts.budget);
    if (opts.cargo_m3 && item.volume_m3) q = Math.min(q, Math.floor(opts.cargo_m3 / item.volume_m3));
    const dv = (item.daily_volume || {})[dest];
    if (!qty && opts.mode === "patient" && dv) q = Math.min(q, Math.max(Math.ceil(dv * d.auto_sell_days), 1));
    return q;
  }
  function rankDestinations(inputs, item, qty, opts) {
    const d = inputs.defaults, origin = inputs.origin, rows = [];
    for (const dest of Object.keys(inputs.hubs)) {
      if (dest === origin) continue;
      rows.push(evaluate(item, origin, dest, tripQty(item, origin, dest, qty, opts, d), opts.fees, opts.mode, opts.buy_mode,
        (inputs.routes || {})[dest], d));
    }
    return rows.sort(byProfit);
  }
  const SORT_KEYS = ["net_profit", "roi", "profit_per_m3", "profit_per_jump", "profit_per_total_jump"];
  const sortBy = k => (a, b) => ((a[k] ?? null) === null) - ((b[k] ?? null) === null) || (b[k] || 0) - (a[k] || 0);
  const legJumps = leg => !leg ? null : (leg.jumps_secure ?? leg.jumps ?? null);
  // One buy hub -> sell hub trip; with a starting system, an empty first leg to the buy hub adds jumps.
  function evaluateTrip(market, item, origin, dest, qty, opts, start) {
    const d = market.defaults, route = ((market.route_matrix || {})[origin] || {})[dest];
    const row = evaluate(item, origin, dest, tripQty(item, origin, dest, qty, opts, d), opts.fees, opts.mode, opts.buy_mode, route, d);
    row.origin = origin;
    const loaded = legJumps(route);
    row.loaded_jumps = loaded; row.start = null; row.start_jumps = null;
    let total = loaded;
    if (start) {
      const leg = (start.legs || {})[origin];
      row.start = { ...Object.fromEntries(ROUTE_KEYS.map(k => [k, leg ? leg[k] ?? null : null])), risk: null, systems: leg ? leg.systems : null };
      row.start_jumps = legJumps(leg);
      total = loaded !== null && row.start_jumps !== null ? loaded + row.start_jumps : null;
      if (!leg) row.flags.push(`route from ${start.system.name} to ${origin} unknown`);
    }
    row.total_jumps = total;
    row.profit_per_total_jump = row.net_profit !== null && total ? row.net_profit / total : null;
    return row;
  }
  function rankTrips(market, item, qty, opts, start, sort) {
    const rows = [], hubs = Object.keys(market.hubs);
    for (const a of hubs) for (const b of hubs) if (a !== b) rows.push(evaluateTrip(market, item, a, b, qty, opts, start));
    return rows.sort(sortBy(sort || (start ? "profit_per_total_jump" : "net_profit")));
  }
  function arbitrage(market, qty, opts, start, f = {}) {
    const out = [], hubs = Object.keys(market.hubs);
    for (const item of market.items) for (const a of hubs) {
      if (f.buy_hub && a !== f.buy_hub) continue;
      for (const b of hubs) {
        if (a === b || (f.sell_hub && b !== f.sell_hub)) continue;
        const r = evaluateTrip(market, item, a, b, qty, opts, start), net = r.net_profit;
        if (net === null || net <= 0 || net < (f.min_profit || 0)) continue;
        if (f.min_roi !== null && f.min_roi !== undefined && (r.roi === null || r.roi < f.min_roi)) continue;
        out.push({ type_id: item.type_id, name: item.name, group: item.group || "", ...r });
      }
    }
    out.sort(sortBy(f.sort || "net_profit"));
    return f.top ? out.slice(0, f.top) : out;
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
  return { feeRates, walk, undercut, overbid, maxProfitable, autoQty, evaluate, rankDestinations, basketTotals, bestRuns,
           SORT_KEYS, legJumps, evaluateTrip, rankTrips, arbitrage };
})();
if (typeof module !== "undefined") module.exports = H;

// ---------------------------------------------------------------- page
if (typeof document !== "undefined") (() => {
  const store = { get(k, d) { try { const v = localStorage.getItem("hauler." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
                  set(k, v) { try { localStorage.setItem("hauler." + k, JSON.stringify(v)); } catch (e) {} } };
  const state = { accounting: store.get("accounting", 0), broker: store.get("broker", 0), mode: store.get("mode", "instant"),
    buy_mode: store.get("buy_mode", "instant"), cargo: store.get("cargo", ""), budget: store.get("budget", null),
    qty: store.get("qty", ""), basket: store.get("basket", [44992]), system: store.get("system", ""),
    trip_sort: store.get("trip_sort", "profit_per_total_jump"),
    arb: { min_profit: "", min_roi: "", buy_hub: "", sell_hub: "", sort: "net_profit", ...store.get("arb", {}) } };
  let market = null, start = null, systems = null, systemsComplete = true;
  const $ = id => document.getElementById(id);

  const num = (v, f) => `<td class="num">${v === null || v === undefined || Number.isNaN(v) ? "–" : f(v)}</td>`;
  const isk = v => `<span class="${pctClass(v)}">${fmtIsk(v)}</span>`;
  const units = v => v >= 1e5 ? fmtIsk(v) : v.toLocaleString("en-US", { maximumFractionDigits: v < 10 ? 2 : 0 });
  const roi = v => `<span class="${pctClass(v)}">${fmtPct(v * 100)}</span>`;
  const days = v => v === 0 ? "now" : v < 1 ? "<1" : v >= 100 ? fmtIsk(v) : v.toFixed(1);
  const jumpsTxt = j => j === null || j === undefined ? "? jumps" : `${j} jump${j === 1 ? "" : "s"}`;
  const noteMark = fl => fl.length ? `<span class="tag" style="color:var(--warn);cursor:help" title="${esc(fl.join("; "))}">⚠${fl.length > 1 ? " " + fl.length : ""}</span>` : "";
  const flagTags = fl => fl.map(f => `<span class="tag" style="color:var(--warn)">${esc(f)}</span>`).join(" ");
  const path = leg => (leg && leg.systems || []).map(s => `${s.name} ${s.security === null ? "?" : s.security.toFixed(1)}`).join(" → ");

  // A leg's cells: high-sec jumps (or the shortest, with a warning tag if no high-sec route), shortest, low/null.
  function legCells(r) {
    if (!r) return `<td class="num">–</td><td class="num muted">–</td><td class="num">–</td>`;
    const safe = r.jumps_secure ?? null, short = r.jumps ?? null;
    const lowNull = r.low === null || r.low === undefined ? "–" : `${r.low} / ${r.null}`;
    const safeRisk = r.low_secure || r.null_secure ? ` <span class="tag" style="color:var(--warn)" title="No high-sec-only route exists">${r.low_secure} low, ${r.null_secure} null</span>` : "";
    return `<td class="num">${safe ?? "–"}${safeRisk}</td><td class="num muted">${short ?? "–"}</td>` +
      `<td class="num" title="Shortest route: ${esc(path(r))}">${lowNull}</td>`;
  }
  // Compact form for the wide arbitrage table: "high-sec / short" in one cell, then low/null.
  function legCells2(r) {
    if (!r) return `<td class="num">–</td><td class="num">–</td>`;
    const safe = r.jumps_secure ?? null, short = r.jumps ?? null;
    const noSafe = r.low_secure || r.null_secure ? ` <span style="color:var(--warn)" title="No high-sec-only route: ${r.low_secure} low-sec, ${r.null_secure} null-sec systems">⚠</span>` : "";
    return `<td class="num">${safe ?? "–"}${noSafe} <span class="muted">/ ${short ?? "–"}</span></td>` +
      `<td class="num" title="Shortest route: ${esc(path(r))}">${r.low === null || r.low === undefined ? "–" : `${r.low} / ${r.null}`}</td>`;
  }
  const LEG_HEAD2 = label => `<th class="num" title="High-sec-only jumps / shortest-route jumps">${label}</th><th class="num" title="Low-sec / null-sec systems on the shortest route">L/N</th>`;
  const LEG_HEAD = (label = "Jumps") => `<th class="num" title="Jumps on the high-sec-only route (used for profit per jump)">${label}</th>
    <th class="num" title="Jumps on the shortest route">Short</th><th class="num" title="Low-sec / null-sec systems on the shortest route (hover a cell for the systems)">Low/Null</th>`;

  function options() {
    return { fees: H.feeRates(state.accounting, state.broker, market.defaults), mode: state.mode, buy_mode: state.buy_mode,
      cargo_m3: parseFloat(state.cargo) || null, budget: parseFloat(state.budget) || market.defaults.budget };
  }
  const qty = () => parseFloat(state.qty) || null;
  const origin = () => currentMarket() in (market?.hubs || {}) ? currentMarket() : "Jita";
  const inputs = () => ({ ...market, origin: origin(), routes: (market.route_matrix || {})[origin()] || {} });
  function save() { for (const k of Object.keys(state)) store.set(k, state[k]); }

  // ---- destinations from the picked hub (no starting system)
  function destTable(rows) {
    const head = `<tr><th>Destination</th>${LEG_HEAD()}<th class="num">Units</th><th class="num" title="Average price paid per unit at the origin">Buy</th>
      <th class="num" title="Average price received per unit at the destination, before fees">Sell</th>
      <th class="num" title="Sales tax plus broker fees">Fees</th><th class="num">Net profit</th><th class="num">Per unit</th>
      <th class="num">ROI</th><th class="num">m³</th><th class="num">Per m³</th><th class="num">Per jump</th>
      <th class="num" title="Median units traded per day in the destination's region, last 30 days">Units/day</th>
      <th class="num" title="Quantity / units per day (instant sales sell now)">Days</th>
      <th class="num" title="Instant buy and sell: units worth carrying before the next unit loses money">Max profitable</th><th>Notes</th></tr>`;
    const body = rows.map((r, i) => `<tr class="${i === 0 && r.net_profit > 0 ? "best" : ""}" style="cursor:default">
      <td><b>${esc(r.destination)}</b></td>${legCells(r)}
      ${num(r.filled, units)}${num(r.buy_price, fmtIsk)}${num(r.sell_price, fmtIsk)}
      ${num(r.sales_tax === null ? null : r.sales_tax + r.broker, fmtIsk)}${num(r.net_profit, v => `<b>${isk(v)}</b>`)}
      ${num(r.profit_per_unit, isk)}${num(r.roi, roi)}${num(r.m3, units)}${num(r.profit_per_m3, isk)}${num(r.profit_per_jump, isk)}
      ${num(r.daily_volume, units)}${num(r.days_to_sell, days)}${num(r.max_profitable, units)}<td>${flagTags(r.flags)}</td></tr>`).join("");
    return `<thead>${head}</thead><tbody>${body}</tbody>`;
  }

  // ---- trips from a starting system: empty leg to any buy hub, then the loaded leg
  function tripTable(rows, limit = 12) {
    const head = `<tr><th>Buy at</th><th>Sell at</th>${LEG_HEAD("To buy hub")}${LEG_HEAD("Haul")}
      <th class="num" title="Jumps to the buy hub plus the loaded jumps">Total</th><th class="num">Units</th><th class="num">Net profit</th>
      <th class="num">ROI</th><th class="num">Per m³</th><th class="num" title="Net profit / loaded jumps">Per haul jump</th>
      <th class="num" title="Net profit / (jumps to the buy hub + loaded jumps)">Per total jump</th><th class="num">Days</th><th>Notes</th></tr>`;
    const body = rows.slice(0, limit).map((r, i) => `<tr class="${i === 0 && r.net_profit > 0 ? "best" : ""}" style="cursor:default">
      <td><b>${esc(r.origin)}</b></td><td><b>${esc(r.destination)}</b></td>${legCells(r.start)}${legCells(r)}
      ${num(r.total_jumps, v => v)}${num(r.filled, units)}${num(r.net_profit, v => `<b>${isk(v)}</b>`)}${num(r.roi, roi)}
      ${num(r.profit_per_m3, isk)}${num(r.profit_per_jump, isk)}${num(r.profit_per_total_jump, v => `<b>${isk(v)}</b>`)}
      ${num(r.days_to_sell, days)}<td>${flagTags(r.flags)}</td></tr>`).join("");
    return `<thead>${head}</thead><tbody>${body}</tbody>`;
  }

  function renderBasketTags() {
    const names = Object.fromEntries((market?.items || []).map(i => [i.type_id, i.name]));
    $("basket").innerHTML = state.basket.length > 1 ? `<span class="muted" style="font:600 11px/1.4 var(--hud);letter-spacing:1px;text-transform:uppercase">Basket</span> ` +
      state.basket.map(t => `<span class="tag">${esc(names[t] || t)} <a href="#" data-rm="${t}" style="text-decoration:none">×</a></span>`).join(" ") : "";
    $("basket").querySelectorAll("[data-rm]").forEach(a => a.addEventListener("click", e => {
      e.preventDefault(); state.basket = state.basket.filter(t => t !== +a.dataset.rm); save(); render();
    }));
  }

  function renderStartNote() {
    const el = $("start-note");
    if (!start) { el.textContent = ""; return; }
    const reach = Object.entries(start.legs).map(([h, l]) => [h, l.jumps_secure ?? l.jumps]).sort((a, b) => a[1] - b[1]);
    el.innerHTML = `From <b>${esc(start.system.name)}</b>${start.system.security === null ? "" : ` (${start.system.security.toFixed(1)})`}: ` +
      reach.map(([h, j]) => `${h === start.nearest ? "<b>" : ""}${esc(h)} ${j}${h === start.nearest ? "</b>" : ""}`).join(" · ") + " jumps";
  }

  function render() {
    if (!market) return;
    renderBasketTags(); renderStartNote();
    const opts = options(), inp = inputs(), byId = Object.fromEntries(market.items.map(i => [i.type_id, i]));
    const chosen = state.basket.map(t => byId[t]).filter(Boolean), f = opts.fees;
    $("asof").textContent = `${start ? `Starting in ${start.system.name}, any buy hub` : `From ${inp.origin}`}. Order books as of ${market.taken_at || "–"} UTC. ` +
      `Sales tax ${(f.sales_tax * 100).toFixed(2)}%, broker fee ${(f.broker_fee * 100).toFixed(2)}%${state.mode === "patient" || state.buy_mode === "order" ? "" : " (not charged: instant buy and sell)"}.`;
    $("trip-sort-wrap").hidden = !start;

    const summary = $("summary");
    if (start) {
      const per = chosen.map(i => ({ item: i, rows: H.rankTrips(market, i, qty(), opts, start, state.trip_sort) }));
      $("items").innerHTML = per.map(({ item, rows }) => `<div class="panel" style="margin-bottom:16px"><div class="card-head">
        <h2>${esc(item.name)} <span class="tag">${esc(item.group)}</span></h2>
        <span class="muted" style="font-size:12px">best 12 of ${rows.length} trips · ${qty() ? units(qty()) + " units" : "auto quantity"} · ${item.volume_m3 ? units(item.volume_m3) + " m³ each" : "volume unknown"}</span></div>
        <div class="table-wrap"><table>${tripTable(rows)}</table></div></div>`).join("");
      $("basket-panel").hidden = true;
      const p = per[0], b = p && p.rows[0];
      summary.hidden = !p;
      if (p) summary.innerHTML = `<b>Nearest hub:</b> ${esc(start.nearest || "–")} (${jumpsTxt(start.nearest ? H.legJumps(start.legs[start.nearest]) : null)}). ` +
        (b && b.net_profit > 0
          ? `<b>Best trip for ${esc(p.item.name)}:</b> fly ${jumpsTxt(b.start_jumps)} to <b>${esc(b.origin)}</b>, haul ${units(b.filled)} to <b>${esc(b.destination)}</b> (${jumpsTxt(b.loaded_jumps)}) for ${fmtIsk(b.net_profit)} ISK net, ${fmtIsk(b.profit_per_total_jump)} per total jump.`
          : p.item.global_market ? `${esc(p.item.name)} trades on one global market, so there is nothing to haul.` : `No trip makes a profit on ${esc(p.item.name)} with these settings.`);
    } else {
      const per = chosen.map(i => ({ item: i, rows: H.rankDestinations(inp, i, qty(), opts) }));
      $("items").innerHTML = per.map(({ item, rows }) => {
        const q = rows.length ? Math.max(...rows.map(r => r.qty)) : 0;
        return `<div class="panel" style="margin-bottom:16px"><div class="card-head"><h2>${esc(item.name)} <span class="tag">${esc(item.group)}</span></h2>
          <span class="muted" style="font-size:12px">${qty() ? "" : "auto "}${units(q)} units · ${item.volume_m3 ? units(item.volume_m3) + " m³ each" : "volume unknown"}</span></div>
          <div class="table-wrap"><table>${destTable(rows)}</table></div></div>`;
      }).join("");
      if (chosen.length > 1) {
        const totals = H.basketTotals(per.map(p => p.rows));
        $("basket-panel").hidden = false;
        $("basket-tbl").innerHTML = `<thead><tr><th>Destination</th>${LEG_HEAD()}<th class="num">Items priced</th><th class="num">Cost</th><th class="num">Net profit</th><th class="num">ROI</th><th class="num">m³</th><th class="num">Per jump</th><th class="num" title="Slowest item">Days</th></tr></thead><tbody>` +
          totals.map((t, i) => `<tr class="${i === 0 && t.net_profit > 0 ? "best" : ""}" style="cursor:default"><td><b>${esc(t.destination)}</b></td>${legCells(t)}
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
          : `No hub pays more than ${esc(inp.origin)} for ${esc(p.item.name)} after fees with these settings.${state.mode === "instant" ? " Try Patient selling." : ""}`;
      }
    }
    renderBest(inp, opts);
    renderArbitrage(opts);
  }

  function renderBest(inp, opts) {
    const runs = H.bestRuns(inp, qty(), opts, 25);
    $("best-title").textContent = `Best runs from ${inp.origin}`;
    $("best-note").textContent = `${state.mode === "patient" ? "listed (patient) sales" : "instant sales"}, top ${runs.length}`;
    $("best-tbl").innerHTML = `<thead><tr><th>Item</th><th>Destination</th>${LEG_HEAD()}<th class="num">Units</th><th class="num">Cost</th><th class="num">Net profit</th><th class="num">ROI</th><th class="num">Per m³</th><th class="num">Per jump</th><th class="num">Days</th><th>Notes</th></tr></thead><tbody>` +
      (runs.length ? runs.map(r => `<tr data-t="${r.type_id}" title="Click to look at this item"><td>${esc(r.name)} <span class="tag">${esc(r.group)}</span></td>
        <td><b>${esc(r.destination)}</b></td>${legCells(r)}${num(r.filled, units)}${num(r.cost, fmtIsk)}${num(r.net_profit, v => `<b>${isk(v)}</b>`)}
        ${num(r.roi, roi)}${num(r.profit_per_m3, isk)}${num(r.profit_per_jump, isk)}${num(r.days_to_sell, days)}<td>${flagTags(r.flags)}</td></tr>`).join("")
        : `<tr><td colspan="14" class="loading">Nothing is profitable to haul from ${esc(inp.origin)} with these settings.${state.mode === "instant" ? " Instant arbitrage between hubs is rare; try Patient selling." : ""}</td></tr>`) + "</tbody>";
    clickToPick("#best-tbl");
  }

  const ARB_COLS = { net_profit: "Net profit", roi: "ROI", profit_per_m3: "Per m³", profit_per_jump: "Per jump", profit_per_total_jump: "Per total jump" };
  function renderArbitrage(opts) {
    const a = state.arb;
    const rows = H.arbitrage(market, qty(), opts, start, { min_profit: (parseFloat(a.min_profit) || 0) * 1e6,
      min_roi: a.min_roi === "" ? null : parseFloat(a.min_roi) / 100, buy_hub: a.buy_hub || null, sell_hub: a.sell_hub || null,
      sort: a.sort === "profit_per_total_jump" && !start ? "profit_per_jump" : a.sort });
    const shown = rows.slice(0, 100);
    $("arb-note").textContent = `${rows.length} profitable trades${rows.length > 100 ? ", best 100 shown" : ""} · ${state.mode === "patient" ? "listed (patient) sales" : "instant sales"}`;
    const sortTh = (k, extra = "") => `<th class="num sortable${a.sort === k ? " on" : ""}" data-sort="${k}" ${extra}>${ARB_COLS[k]}${a.sort === k ? ' <span class="arrow">▼</span>' : ""}</th>`;
    $("arb-tbl").innerHTML = `<thead><tr><th>Item</th><th>Buy at</th><th>Sell at</th>${start ? LEG_HEAD2("To buy hub") : ""}${LEG_HEAD2("Haul")}
      ${start ? '<th class="num">Total</th>' : ""}<th class="num">Units</th><th class="num">Cost</th>${sortTh("net_profit")}${sortTh("roi")}
      ${sortTh("profit_per_m3")}${sortTh("profit_per_jump", 'title="Net profit / loaded jumps"')}${start ? sortTh("profit_per_total_jump") : ""}
      <th class="num">Days</th><th>Notes</th></tr></thead><tbody>` +
      (shown.length ? shown.map(r => `<tr data-t="${r.type_id}" title="${esc(r.group)}: click to look at this item"><td class="item-name">${esc(r.name)}</td>
        <td><b>${esc(r.origin)}</b></td><td><b>${esc(r.destination)}</b></td>${start ? legCells2(r.start) : ""}${legCells2(r)}
        ${start ? num(r.total_jumps, v => v) : ""}${num(r.filled, units)}${num(r.cost, fmtIsk)}${num(r.net_profit, v => `<b>${isk(v)}</b>`)}
        ${num(r.roi, roi)}${num(r.profit_per_m3, isk)}${num(r.profit_per_jump, isk)}${start ? num(r.profit_per_total_jump, isk) : ""}
        ${num(r.days_to_sell, days)}<td>${noteMark(r.flags)}</td></tr>`).join("")
        : `<tr><td colspan="20" class="loading">No trade passes these filters.${state.mode === "instant" ? " Try Patient selling." : ""}</td></tr>`) + "</tbody>";
    $("arb-tbl").querySelectorAll("th[data-sort]").forEach(th => th.addEventListener("click", () => {
      a.sort = th.dataset.sort; $("arb-sort").value = a.sort; save(); renderArbitrage(options());
    }));
    clickToPick("#arb-tbl");
  }

  function clickToPick(sel) {
    document.querySelectorAll(`${sel} tr[data-t]`).forEach(tr => tr.addEventListener("click", () => {
      state.basket = [+tr.dataset.t]; $("item-pick").value = tr.dataset.t; save(); render(); window.scrollTo({ top: 0, behavior: "smooth" });
    }));
  }

  // ---- starting system
  async function loadSystems() {
    try {
      const data = await getJSON(apiUrl("hauling/systems", { market: "all" }));
      systems = new Map(data.systems.map(([id, name]) => [name.toLowerCase(), { id, name }]));
      systemsComplete = data.complete;
      $("systems").innerHTML = data.systems.map(([, n]) => `<option value="${esc(n)}"></option>`).join("");
      $("system-help").textContent = systemsComplete ? "" :
        `This static copy knows ${data.systems.length} systems on the trade lanes. Any other system needs the local dashboard (python -m eve_market dashboard).`;
    } catch (e) { systems = new Map(); }
  }
  async function setSystem(text) {
    const q = text.trim();
    state.system = q; save();
    if (!q) { start = null; $("system-error").textContent = ""; render(); return; }
    const known = systems && systems.get(q.toLowerCase());
    if (!known && !systemsComplete) { start = null; $("system-error").textContent = `${q} isn't in this static copy.`; render(); return; }
    try {
      start = await getJSON(window.EVE_STATIC ? apiUrl("hauling/start", { market: known.id }) : apiUrl("hauling/start", { system: known ? known.id : q }));
      $("system").value = start.system.name; $("system-error").textContent = "";
    } catch (e) {
      start = null; $("system-error").textContent = `No solar system called ${q}.`;
    }
    render();
  }

  async function load() {
    $("items").innerHTML = `<div class="panel loading" style="margin-bottom:16px">Loading order books…</div>`;
    try {
      market = await getJSON(apiUrl("hauling/market", { market: "all" }));
      if (state.budget === null) $("budget").value = market.defaults.budget;
      render();
    } catch (e) {
      $("items").innerHTML = `<div class="panel down" style="margin-bottom:16px">Could not load hauling data: ${esc(e.message)}</div>`;
    }
  }

  // ---- controls
  for (const [id, key] of [["qty", "qty"], ["cargo", "cargo"], ["budget", "budget"], ["accounting", "accounting"], ["broker", "broker"]]) {
    const el = $(id);
    if (state[key] !== null && state[key] !== "") el.value = state[key];
    el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => { state[key] = el.value; save(); render(); });
  }
  for (const [id, key] of [["mode-seg", "mode"], ["buy-seg", "buy_mode"], ["trip-sort", "trip_sort"]]) {
    const btns = document.querySelectorAll(`#${id} button`);
    btns.forEach(b => {
      b.classList.toggle("on", b.dataset.v === state[key]);
      b.addEventListener("click", () => { btns.forEach(x => x.classList.toggle("on", x === b)); state[key] = b.dataset.v; save(); render(); });
    });
  }
  for (const [id, key] of [["arb-min-profit", "min_profit"], ["arb-min-roi", "min_roi"], ["arb-buy", "buy_hub"], ["arb-sell", "sell_hub"], ["arb-sort", "sort"]]) {
    const el = $(id);
    el.value = state.arb[key];
    el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => { state.arb[key] = el.value; save(); if (market) renderArbitrage(options()); });
  }
  const pick = $("item-pick");
  if (state.basket.length && [...pick.options].some(o => +o.value === state.basket[0])) pick.value = state.basket[0];
  else state.basket = [+pick.value];
  pick.addEventListener("change", () => { if (state.basket.length <= 1) { state.basket = [+pick.value]; save(); render(); } });
  $("item-set").addEventListener("click", () => { state.basket = [+pick.value]; save(); render(); });
  $("item-add").addEventListener("click", () => { if (!state.basket.includes(+pick.value)) state.basket.push(+pick.value); save(); render(); });
  $("system").value = state.system;
  $("system").addEventListener("change", e => setSystem(e.target.value));
  $("system").addEventListener("keydown", e => { if (e.key === "Enter") setSystem(e.target.value); });
  $("system-clear").addEventListener("click", () => { $("system").value = ""; setSystem(""); });
  initMarketPicker(() => render());
  Promise.all([load(), loadSystems()]).then(() => { if (state.system) setSystem(state.system); });
})();
