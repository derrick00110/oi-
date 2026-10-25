const WINDOWS = [
  { id: "5m", label: "5分钟" },
  { id: "15m", label: "15分钟" },
  { id: "1h", label: "1小时" },
  { id: "4h", label: "4小时" },
  { id: "24h", label: "24小时" },
];

const PREF_KEY = "binance-oi-monitor";

const state = {
  data: null,
  error: "",
  loading: false,
  req: 0,
  timer: 0,
  open: new Set(),
  limit: "400",
  window: "1h",
  sort: "oiValue:1h",
  desc: true,
  only: "all",
  hotPct: 5,
  minOi: 0,
  query: "",
  auto: true,
  rise: "green",
};

const $ = (selector) => document.querySelector(selector);

function loadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
    for (const key of ["limit", "window", "sort", "desc", "only", "hotPct", "minOi", "query", "auto", "rise"]) {
      if (saved[key] !== undefined) state[key] = saved[key];
    }
  } catch {
    /* ignore broken local prefs */
  }
}

function savePrefs() {
  const { limit, window, sort, desc, only, hotPct, minOi, query, auto, rise } = state;
  localStorage.setItem(PREF_KEY, JSON.stringify({ limit, window, sort, desc, only, hotPct, minOi, query, auto, rise }));
}

function el(tag, props = {}, kids = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "title") node.title = String(value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const kid of kids) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function windowLabel(id) {
  return (WINDOWS.find((item) => item.id === id) || WINDOWS[1]).label;
}

function tone(value) {
  if (value == null || Number.isNaN(value) || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

function pct(value, signed = true) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const number = Number(value);
  const sign = signed && number > 0 ? "+" : "";
  return `${sign}${number.toFixed(2)}%`;
}

function compact(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "-" : "";
  const absolute = Math.abs(value);
  const trim = (number) => number.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
  if (absolute >= 1e12) return `${sign}${trim(absolute / 1e12)}万亿`;
  if (absolute >= 1e8) return `${sign}${trim(absolute / 1e8)}亿`;
  if (absolute >= 1e4) return `${sign}${trim(absolute / 1e4)}万`;
  if (absolute >= 1) return `${sign}${trim(absolute)}`;
  return sign + absolute.toPrecision(3);
}

function qty(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  if (absolute >= 10000 || (absolute > 0 && absolute < 1)) return compact(value);
  return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function signedCompact(value, asQty = false) {
  if (value == null || !Number.isFinite(value)) return "";
  const body = asQty ? qty(Math.abs(value)) : compact(Math.abs(value));
  if (value > 0) return `+${body}`;
  if (value < 0) return `-${body}`;
  return body;
}

function priceFmt(value) {
  if (!Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  if (absolute >= 1000) {
    return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  const digits = absolute >= 100 ? 2 : absolute >= 1 ? 4 : absolute >= 0.01 ? 5 : absolute >= 0.0001 ? 6 : 4;
  return value.toFixed(digits).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

function timeFmt(ms) {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

function lagText(ms) {
  if (!ms) return "";
  const delta = Date.now() - ms;
  if (delta < 0) return timeFmt(ms);
  if (delta < 90_000) return `${Math.round(delta / 1000)} 秒前`;
  if (delta < 3_600_000) return `${Math.round(delta / 60_000)} 分钟前`;
  return `${(delta / 3_600_000).toFixed(1)} 小时前`;
}

function sortValue(row, key) {
  if (key === "scale5m") return row.cycle?.oiValuePct;
  if (key.includes(":")) {
    const [field, windowId] = key.split(":");
    const item = row.changes[windowId] || {};
    return field === "oi" ? item.oiPct : item.oiValuePct;
  }
  return row[key];
}

function threshold() {
  const value = Number(state.hotPct);
  return Number.isFinite(value) && value > 0 ? value : 5;
}

function visibleRows() {
  const query = state.query.trim().toUpperCase();
  const hotAt = threshold();
  return (state.data?.rows || []).filter((row) => {
    if (query && !row.symbol.includes(query) && !row.base.toUpperCase().includes(query)) return false;
    if (state.minOi && row.oiValue < Number(state.minOi)) return false;
    const change = row.changes[state.window]?.oiValuePct;
    if (state.only === "up" && !(change > 0)) return false;
    if (state.only === "down" && !(change < 0)) return false;
    if (state.only === "hot" && !(change != null && Math.abs(change) >= hotAt)) return false;
    if (["long", "wash", "abandon", "watch-up", "watch-flat"].includes(state.only)) {
      const read = row.reads?.[state.window];
      if (!read || !read.qualified || read.code !== state.only) return false;
    }
    return true;
  }).sort((a, b) => {
    const left = sortValue(a, state.sort);
    const right = sortValue(b, state.sort);
    const leftMissing = left == null || Number.isNaN(left);
    const rightMissing = right == null || Number.isNaN(right);
    if (leftMissing && rightMissing) return a.volumeRank - b.volumeRank;
    if (leftMissing) return 1;
    if (rightMissing) return -1;
    if (typeof left === "string") {
      const compared = left.localeCompare(right);
      return state.desc ? -compared : compared;
    }
    return state.desc ? right - left : left - right;
  });
}

function extreme(rows, gain) {
  let best = null;
  for (const row of rows) {
    const value = row.changes[state.window]?.oiValuePct;
    if (value == null) continue;
    if (!best) best = row;
    else {
      const current = best.changes[state.window].oiValuePct;
      if (gain ? value > current : value < current) best = row;
    }
  }
  return best;
}

function renderStats(rows) {
  const host = $("#stats");
  const changes = rows.map((row) => row.changes[state.window]?.oiValuePct).filter((value) => value != null);
  const up = changes.filter((value) => value > 0).length;
  const down = changes.filter((value) => value < 0).length;
  const total = rows.reduce((sum, row) => sum + (row.oiValue || 0), 0);
  const gainer = extreme(rows, true);
  const loser = extreme(rows, false);
  const label = windowLabel(state.window);
  host.hidden = false;
  host.replaceChildren(
    statCard("当前筛选未平仓金额", compact(total), `${rows.length} 个合约`),
    statCard(`${label}价值方向`, `${up} 增 / ${down} 减`, changes.length ? `平 ${changes.length - up - down}` : "没有可比较的历史"),
    moverCard(`${label}价值涨幅最大`, gainer),
    moverCard(`${label}价值跌幅最大`, loser),
  );
}

function statCard(label, value, sub) {
  return el("div", { class: "stat" }, [
    el("div", { class: "k", text: label }),
    el("div", { class: "v", text: value }),
    el("div", { class: "s", text: sub }),
  ]);
}

function moverCard(label, row) {
  if (!row) return statCard(label, "—", "当前筛选里没有数据");
  const change = row.changes[state.window].oiValuePct;
  const button = el("button", {
    type: "button",
    class: "stat",
    onclick: () => {
      state.query = row.base;
      $("#query").value = row.base;
      savePrefs();
      render();
    },
  }, [
    el("div", { class: "k", text: label }),
    el("div", { class: `v ${tone(change)}`, text: `${row.base} ${pct(change)}` }),
    el("div", { class: "s", text: `价值 ${signedCompact(row.changes[state.window].oiValueDelta)} · 数量 ${pct(row.changes[state.window].oiPct)}` }),
  ]);
  return button;
}

function sparkline(values) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", "0 0 112 32");
  svg.setAttribute("width", "112");
  svg.setAttribute("height", "32");
  if (!values || values.length < 2) return svg;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values.map((value, index) => {
    const x = 2 + (index / (values.length - 1)) * 108;
    const y = 3 + (1 - (value - min) / span) * 26;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("fill", "none");
  line.setAttribute("stroke-width", "1.6");
  line.setAttribute("stroke", values[values.length - 1] >= values[0] ? "var(--up)" : "var(--down)");
  line.setAttribute("points", points);
  svg.append(line);
  const low = compact(min);
  const high = compact(max);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `近 24 小时未平仓金额，低点 ${low}，高点 ${high}`);
  return svg;
}

function otherWindows(row, field) {
  return el("div", { class: "mini" }, WINDOWS.filter((item) => item.id !== state.window).map((item) => {
    const value = row.changes[item.id]?.[field];
    return el("button", {
      type: "button",
      class: tone(value),
      text: `${item.id} ${pct(value)}`,
      title: `按${item.label}排序`,
      onclick: (event) => {
        event.stopPropagation();
        const prefix = field === "oiPct" ? "oi" : "oiValue";
        state.sort = `${prefix}:${item.id}`;
        state.desc = true;
        state.window = item.id;
        savePrefs();
        render();
      },
    });
  }));
}

function changeCell(row, field) {
  const item = row.changes[state.window] || {};
  const value = item[field];
  const delta = field === "oiPct" ? item.oiDelta : item.oiValueDelta;
  const tip = field === "oiValuePct"
    ? "15%、10% 和 5% 看的是未平仓金额涨跌。"
    : "未平仓合约数量涨跌，只作对照。门槛不看这一列。";
  return el("div", { class: "stack", title: tip }, [
    el("span", { class: `primary pill ${tone(value)}`, text: pct(value) }),
    el("span", { class: "sub", text: signedCompact(delta, field === "oiPct") }),
    otherWindows(row, field),
  ]);
}

function renderTable(rows) {
  const body = $("#rows");
  if (!state.data) {
    body.replaceChildren(el("tr", {}, [el("td", { class: "empty", colspan: "13", text: state.loading ? "正在扫描未平仓合约…" : "等待数据" })]));
    return;
  }
  if (!rows.length) {
    body.replaceChildren(el("tr", {}, [el("td", { class: "empty", colspan: "13", text: "没有符合筛选条件的合约" })]));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const row of rows) {
    const change = row.changes[state.window]?.oiValuePct;
    const hot = change != null && Math.abs(change) >= threshold();
    const main = el("tr", {
      class: `main${hot ? " hot" : ""}${state.open.has(row.symbol) ? " open" : ""}`,
      onclick: () => {
        if (state.open.has(row.symbol)) state.open.delete(row.symbol);
        else state.open.add(row.symbol);
        render();
      },
    }, [
      el("td", {}, [
        el("span", { class: "symbol", text: row.base }),
        el("span", { class: "rank", text: `#${row.volumeRank}` }),
        el("span", { class: "pair", text: row.symbol }),
      ]),
      el("td", { class: "num", text: priceFmt(row.price) }),
      el("td", { class: `num ${tone(row.priceChangePercent)}`, text: pct(row.priceChangePercent) }),
      el("td", { class: "num", text: qty(row.oi) }),
      el("td", { class: "num", text: compact(row.oiValue) }),
      el("td", { class: "num" }, [scaleCell(row)]),
      el("td", { class: "num" }, [changeCell(row, "oiValuePct")]),
      el("td", { class: "num" }, [changeCell(row, "oiPct")]),
      el("td", { class: `fact ${tone(readTone(row))}`, text: readText(row) }),
      el("td", { class: "num", text: compact(row.marketCap) }),
      el("td", { class: "num", text: pct(row.oiToMarketCapPct, false) }),
      el("td", { class: "num", text: compact(row.quoteVolume) }),
      el("td", {}, [sparkline(row.spark)]),
    ]);
    fragment.append(main);
    if (state.open.has(row.symbol)) fragment.append(detailRow(row));
  }
  body.replaceChildren(fragment);
}

function readOf(row) {
  return row.cycleRead || row.reads?.[state.window] || null;
}

function readText(row) {
  return readOf(row)?.text || "—";
}

function readTone(row) {
  const bias = readOf(row)?.bias;
  if (bias === "up") return 1;
  if (bias === "down") return -1;
  return 0;
}

function fundingText(rate) {
  if (rate == null) return "暂无";
  const percent = Number(rate) * 100;
  const who = percent > 0 ? "多头付给空头" : percent < 0 ? "空头付给多头" : "为零";
  const sign = percent > 0 ? "+" : "";
  return `${sign}${percent.toFixed(4)}% · ${who}`;
}

function scaleCell(row) {
  const cycle = row.cycle || {};
  const ticks = cycle.ticks || [];
  const span = ticks.length
    ? ticks.map((tick) => (tick ? compact(tick.oiValue) : "—")).join(" → ")
    : "刻度未齐";
  const price = cycle.pricePct == null ? "" : `价格 ${pct(cycle.pricePct)}`;
  return el("div", { class: "stack", title: "每 5 分钟记一次。播报只比第 1 次和第 5 次的未平仓金额，价格同样比这两次。中间三次不参与门槛。" }, [
    el("span", { class: `primary pill ${tone(cycle.oiValuePct)}`, text: cycle.oiValuePct == null ? "—" : pct(cycle.oiValuePct) }),
    el("span", { class: "sub", text: span }),
    price ? el("span", { class: "sub", text: price }) : null,
  ]);
}

function metricLine(label, value, extra, toneValue) {
  return el("div", { class: "metric" }, [
    el("span", { class: "sub", text: label }),
    el("span", { class: tone(toneValue), text: value }),
    extra ? el("span", { class: "sub", text: extra }) : null,
  ]);
}

function detailRow(row) {
  const panels = WINDOWS.map((item) => {
    const change = row.changes[item.id];
    return el("div", { class: "panel" }, [
      el("div", { class: "k", text: item.label }),
      metricLine("未平仓合约", pct(change.oiPct), signedCompact(change.oiDelta, true), change.oiPct),
      metricLine("未平仓金额", pct(change.oiValuePct), signedCompact(change.oiValueDelta), change.oiValuePct),
      metricLine("隐含价格", pct(change.pricePct), "", change.pricePct),
    ]);
  });
  panels.push(el("div", { class: "panel" }, [
    el("div", { class: "k", text: "市值与快照" }),
    metricLine("流通量", row.circulatingSupply == null ? "—" : qty(row.circulatingSupply)),
    metricLine("市值", compact(row.marketCap)),
    metricLine("OI/市值", pct(row.oiToMarketCapPct, false)),
    metricLine("快照", timeFmt(row.snapshotAt)),
    metricLine("资金费率", fundingText(row.fundingRate)),
    metricLine("事实", readText(row), "", readTone(row)),
  ]));
  return el("tr", {}, [el("td", { class: "detail-cell", colspan: "13" }, [el("div", { class: "detail" }, panels)])]);
}

function renderChrome() {
  document.documentElement.dataset.rise = state.rise === "red" ? "red" : "green";
  $("#rise").textContent = state.rise === "red" ? "红涨绿跌" : "绿涨红跌";
  $("#limit").value = state.limit;
  $("#only").value = state.only;
  $("#minOi").value = String(state.minOi);
  $("#auto").checked = !!state.auto;
  const valueHeader = $("#valueSort");
  const oiHeader = $("#oiSort");
  valueHeader.dataset.sort = `oiValue:${state.window}`;
  oiHeader.dataset.sort = `oi:${state.window}`;
  valueHeader.textContent = `金额涨跌 · ${windowLabel(state.window)}`;
  const factHeader = $("#factSort");
  if (factHeader) factHeader.textContent = "事实 · 本轮";
  oiHeader.textContent = `数量涨跌 · ${windowLabel(state.window)}`;
  document.querySelectorAll("#windows button").forEach((button) => {
    button.classList.toggle("active", button.dataset.window === state.window);
  });
  document.querySelectorAll("thead th").forEach((header) => {
    const active = header.dataset.sort === state.sort;
    header.classList.toggle("sorted", active);
    if (active) header.setAttribute("aria-sort", state.desc ? "descending" : "ascending");
    else header.removeAttribute("aria-sort");
  });
  const refresh = $("#refresh");
  refresh.disabled = state.loading;
  refresh.textContent = state.loading ? "扫描中" : "刷新";
}

function renderStatus(rows) {
  const data = state.data;
  if (!data) {
    $("#meta").textContent = state.loading ? "正在连接币安…" : "";
    $("#count").textContent = "";
    return;
  }
  let host = data.source || "";
  try { host = new URL(data.source).host; } catch { /* keep raw source */ }
  const cached = data.cached ? ` · 缓存 ${Math.round(data.cacheAgeMs / 1000)} 秒` : "";
  $("#meta").textContent = `${host} · 快照 ${lagText(data.snapshotAt)}${cached}`;
  const range = state.limit === "all" ? "全市场" : `成交额前 ${state.limit}`;
  $("#count").textContent = `${range}，显示 ${rows.length}/${data.returned}，全市场 ${data.universe}，用时 ${(data.elapsedMs / 1000).toFixed(1)} 秒`;
}

function renderBanner() {
  const banner = $("#banner");
  if (state.error) {
    banner.hidden = false;
    banner.className = "banner bad";
    banner.textContent = state.error;
    return;
  }
  const count = state.data?.errorCount || 0;
  if (count) {
    const names = (state.data.errors || []).map((item) => item.symbol).join("、");
    banner.hidden = false;
    banner.className = "banner warn";
    banner.textContent = `${count} 个合约的未平仓数据没有拉到${names ? `：${names}` : ""}`;
    return;
  }
  banner.hidden = true;
  banner.textContent = "";
}

function render() {
  renderChrome();
  const rows = state.data ? visibleRows() : [];
  if (state.data) renderStats(rows);
  renderStatus(rows);
  renderBanner();
  renderTable(rows);
}

async function load(fresh) {
  const id = ++state.req;
  state.loading = true;
  render();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 180000);
  try {
    const params = new URLSearchParams({ limit: state.limit });
    if (fresh) params.set("fresh", "1");
    const response = await fetch(`/api/scan?${params}`, { signal: controller.signal });
    const payload = await response.json();
    if (id !== state.req) return;
    if (!response.ok) throw new Error(payload.error || "扫描失败");
    state.data = payload;
    state.error = "";
  } catch (error) {
    if (id !== state.req) return;
    state.error = error.name === "AbortError" ? "扫描超过 3 分钟，已取消。可以先缩小范围再试。" : (error.message || "扫描失败");
  } finally {
    clearTimeout(timer);
    if (id === state.req) {
      state.loading = false;
      render();
    }
  }
}

function exportCsv() {
  const rows = visibleRows();
  const headers = [
    "symbol", "base", "price", "priceChangePercent", "oi", "oiValue", "marketCap", "oiToMarketCapPct", "quoteVolume", "volumeRank",
  ];
  for (const item of WINDOWS) {
    headers.push(`oiPct_${item.id}`, `oiValuePct_${item.id}`, `pricePct_${item.id}`, `oiDelta_${item.id}`, `oiValueDelta_${item.id}`);
  }
  headers.push("circulatingSupply", "snapshotAt");
  const lines = [headers.join(",")];
  for (const row of rows) {
    const values = [
      row.symbol, row.base, row.price, row.priceChangePercent, row.oi, row.oiValue,
      row.marketCap ?? "", row.oiToMarketCapPct ?? "", row.quoteVolume, row.volumeRank,
    ];
    for (const item of WINDOWS) {
      const change = row.changes[item.id];
      values.push(change.oiPct ?? "", change.oiValuePct ?? "", change.pricePct ?? "", change.oiDelta ?? "", change.oiValueDelta ?? "");
    }
    values.push(row.circulatingSupply ?? "", row.snapshotAt);
    lines.push(values.join(","));
  }
  const blob = new Blob([`\uFEFF${lines.join("\n")}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: "binance-oi.csv" });
  link.click();
  URL.revokeObjectURL(url);
}

function schedule() {
  clearInterval(state.timer);
  if (!state.auto) return;
  state.timer = setInterval(() => {
    if (!state.loading && !document.hidden) load(false);
  }, 60_000);
}

function bind() {
  $("#refresh").addEventListener("click", () => load(true));
  $("#export").addEventListener("click", exportCsv);
  $("#rise").addEventListener("click", () => {
    state.rise = state.rise === "red" ? "green" : "red";
    savePrefs();
    render();
  });
  $("#limit").addEventListener("change", (event) => {
    state.limit = event.target.value;
    savePrefs();
    if (window.syncBroadcastLimit) window.syncBroadcastLimit(state.limit);
    load(true);
  });
  $("#only").addEventListener("change", (event) => {
    state.only = event.target.value;
    savePrefs();
    render();
  });
  $("#minOi").addEventListener("change", (event) => {
    state.minOi = Number(event.target.value);
    savePrefs();
    render();
  });
  $("#hotPct").addEventListener("input", (event) => {
    state.hotPct = Number(event.target.value);
    savePrefs();
    render();
  });
  $("#query").addEventListener("input", (event) => {
    state.query = event.target.value;
    savePrefs();
    render();
  });
  $("#auto").addEventListener("change", (event) => {
    state.auto = event.target.checked;
    savePrefs();
    schedule();
  });
  $("#windows").addEventListener("click", (event) => {
    const button = event.target.closest("[data-window]");
    if (!button) return;
    const next = button.dataset.window;
    if (state.sort.startsWith("oiValue:") || state.sort.startsWith("oi:")) {
      state.sort = `${state.sort.split(":")[0]}:${next}`;
    }
    state.window = next;
    savePrefs();
    render();
  });
  $("thead").addEventListener("click", (event) => {
    const header = event.target.closest("th[data-sort]");
    if (!header) return;
    const key = header.dataset.sort;
    if (state.sort === key) state.desc = !state.desc;
    else {
      state.sort = key;
      state.desc = key !== "symbol";
    }
    savePrefs();
    render();
  });
}

loadPrefs();
$("#limit").value = state.limit;
$("#only").value = state.only;
$("#hotPct").value = state.hotPct;
$("#minOi").value = String(state.minOi);
$("#query").value = state.query;
$("#auto").checked = !!state.auto;
bind();
schedule();
render();
load(false);
