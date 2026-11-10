// Shared helpers: number formatting, theme-aware chart colors, ticker ribbon.
//List of help functions that are common among every page on our website
function fmtIsk(v) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e12) return (v / 1e12).toFixed(2) + "T";
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
  if (a >= 100) return v.toFixed(0);
  return v.toFixed(2);
}
function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  const t = v.toFixed(digits);
  if (+t === 0) return (0).toFixed(digits) + "%";
  return (v > 0 ? "+" : "") + t + "%";
}
function pctClass(v) { return v > 0 ? "up" : v < 0 ? "down" : "muted"; }
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])); }

function chartDefaults() {
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--chart-grid");
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
  Chart.defaults.animation = false;
}

function sparkline(values, w = 90, h = 24) {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1) * w).toFixed(1)},${(h - 2 - (v - min) / span * (h - 4)).toFixed(1)}`);
  const color = values[values.length - 1] >= values[0] ? css("--up") : css("--down");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline fill="none" stroke="${color}" stroke-width="1.5" points="${pts.join(" ")}"/></svg>`;
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

// Selected regional market: ?market= in the URL wins, then the last choice, then Jita.
function currentMarket() {
  const fromUrl = new URLSearchParams(location.search).get("market");
  let saved = null;
  try { saved = localStorage.getItem("market"); } catch (e) {}
  return fromUrl || saved || "Jita";
}
function initMarketPicker(onChange) {
  const sel = document.getElementById("market");
  if (!sel) return;
  const m = currentMarket();
  sel.value = [...sel.options].some(o => o.value === m) ? m : "Jita";
  sel.addEventListener("change", () => {
    try { localStorage.setItem("market", sel.value); } catch (e) {}
    const url = new URL(location.href); url.searchParams.set("market", sel.value);
    history.replaceState(null, "", url);
    _itemsPromise = null;
    initTicker();
    onChange && onChange(sel.value);
  });
}

let _itemsPromise = null;
function loadItems() { return _itemsPromise ||= getJSON(`/api/items?list=all&market=${encodeURIComponent(currentMarket())}`); }

async function initTicker() {
  const el = document.getElementById("ticker");
  document.getElementById("ticker-label").textContent = currentMarket();
  try {
    const items = await loadItems();
    const html = items.sort((a, b) => b.isk_30d - a.isk_30d).map(i =>
      `<span class="ticker-item"><b>${esc(i.name)}</b>${fmtIsk(i.price)}<span class="chg ${pctClass(i.chg_1d)}">${i.chg_1d > 0 ? "▲" : i.chg_1d < 0 ? "▼" : "•"} ${fmtPct(i.chg_1d, 2)}</span></span>`
    ).join("");
    el.innerHTML = html;
    el.style.setProperty("--ticker-duration", Math.max(40, items.length * 3.5) + "s");
  } catch (e) {
    el.innerHTML = `<span class="down">Could not load market data. Run <code>python -m eve_market collect</code>.</span>`;
  }
}
