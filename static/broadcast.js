(() => {
const WINDOW_LABELS = {
  "15m": "15分钟",
  "1h": "1小时",
  "4h": "4小时",
  "24h": "24小时",
};

const voiceBox = document.querySelector("#voice");
const feed = document.querySelector("#feed");
const rulesBox = document.querySelector("#rules");
let seenId = 0;
let primed = false;
let feedKey = "";
let rulesKey = "";
let noticeUntil = 0;

voiceBox.checked = localStorage.getItem("oi-voice") === "1";
voiceBox.addEventListener("change", () => {
  localStorage.setItem("oi-voice", voiceBox.checked ? "1" : "0");
});

function node(tag, props = {}, kids = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null) continue;
    if (key === "class") element.className = value;
    else if (key === "text") element.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") element.addEventListener(key.slice(2).toLowerCase(), value);
    else element.setAttribute(key, value === true ? "" : String(value));
  }
  for (const kid of kids) {
    if (kid) element.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return element;
}

function speak(text) {
  if (!voiceBox.checked || !window.speechSynthesis) return;
  const utterance = new SpeechSynthesisUtterance(text.replace(/^[🟢🔴⚪]\s*/, ""));
  utterance.lang = "zh-CN";
  window.speechSynthesis.speak(utterance);
}

function renderFeed(items) {
  if (!items.length) {
    feed.replaceChildren(node("p", { class: "meta", text: "还没有越过阈值的合约。下一轮扫描后，符合规则的会写在这里。" }));
    return;
  }
  feed.replaceChildren(...items.map((item) => node("article", { class: "cast" }, [
    node("div", { class: "when", text: new Date(item.at).toLocaleString("zh-CN", { hour12: false }) }),
    node("p", { text: item.text }),
    node("button", {
      type: "button",
      text: "填入下单",
      onclick: () => {
        document.querySelector("#orderSymbol").value = item.symbol;
        document.querySelector(".trade").open = true;
        document.querySelector("#preview").textContent = `已填入 ${item.symbol}。方向和数量仍要自己定，播报不会代选。`;
      },
    }),
  ])));
}

function renderRules(rules) {
  rulesBox.replaceChildren(...rules.map((rule) => node("div", { class: "rule" }, [
    node("label", { class: "check" }, [
      node("input", { type: "checkbox", checked: rule.enabled ? "checked" : null, "data-field": "enabled" }),
      "启用",
    ]),
    node("span", { text: `${WINDOW_LABELS[rule.window] || rule.window} · ${rule.metric === "oiPct" ? "持仓量" : "持仓价值"}` }),
    node("span", { text: "绝对值 ≥" }),
    node("input", { type: "number", min: "0.1", max: "500", step: "0.1", value: rule.minAbsPct, "data-field": "minAbsPct" }),
    node("span", { text: "% ，持仓价值" }),
    node("input", { type: "number", min: "0", step: "100", value: Number(rule.minOiValue || 0) / 10000, "data-field": "minWan" }),
    node("span", { text: "到" }),
    node("input", { type: "number", min: "0", step: "100", value: rule.maxOiValue ? Number(rule.maxOiValue) / 10000 : "", "data-field": "maxWan", placeholder: "不限" }),
    node("span", { text: "万 USDT，价格涨幅 ≥" }),
    node("input", { type: "number", min: "0", step: "0.1", value: rule.minPricePct ?? "", "data-field": "minPricePct", placeholder: "不限" }),
    node("span", { text: "%" }),
    node("select", { "data-field": "direction" }, [
      node("option", { value: "both", text: "双向", selected: rule.direction === "both" ? "selected" : null }),
      node("option", { value: "up", text: "只播增加", selected: rule.direction === "up" ? "selected" : null }),
      node("option", { value: "down", text: "只播减少", selected: rule.direction === "down" ? "selected" : null }),
    ]),
    node("button", { type: "button", text: "保存", "data-id": rule.id, onclick: () => saveRule(rule) }),
  ])));
}

function blankToNull(raw, scale) {
  if (raw == null || String(raw).trim() === "") return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value * scale;
}

function rowOf(id) {
  return [...rulesBox.querySelectorAll(".rule")].find((row) => row.querySelector(`[data-id="${id}"]`));
}

async function saveRule(rule) {
  const row = rowOf(rule.id);
  const next = {
    ...rule,
    enabled: row.querySelector('[data-field="enabled"]').checked,
    minAbsPct: Number(row.querySelector('[data-field="minAbsPct"]').value),
    minOiValue: Number(row.querySelector('[data-field="minWan"]').value) * 10000,
    maxOiValue: blankToNull(row.querySelector('[data-field="maxWan"]').value, 10000),
    minPricePct: blankToNull(row.querySelector('[data-field="minPricePct"]').value, 1),
    direction: row.querySelector('[data-field="direction"]').value,
  };
  const current = await (await fetch("/api/broadcasts")).json();
  const rules = current.rules.map((item) => (item.id === rule.id ? next : item));
  const response = await fetch("/api/rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rules }),
  });
  const payload = await response.json();
  const meta = document.querySelector("#broadcastMeta");
  if (!response.ok) {
    meta.textContent = payload.error || "规则没有保存";
    return;
  }
  rulesKey = "";
  noticeUntil = Date.now() + 2500;
  meta.textContent = "规则已保存";
}

async function poll() {
  const response = await fetch("/api/broadcasts");
  const payload = await response.json();
  const items = payload.items || [];
  const key = items.map((item) => item.id).join(",");
  if (key !== feedKey) {
    renderFeed(items);
    feedKey = key;
  }
  const ruleIds = (payload.rules || []).map((rule) => `${rule.id}:${rule.minAbsPct}:${rule.maxOiValue}:${rule.minPricePct}:${rule.direction}:${rule.enabled}`).join("|");
  if (ruleIds !== rulesKey) {
    renderRules(payload.rules || []);
    rulesKey = ruleIds;
  }
  const newest = items.reduce((max, item) => Math.max(max, item.id), 0);
  if (primed) {
    items.filter((item) => item.id > seenId).sort((a, b) => a.id - b.id).forEach((item) => speak(item.text));
  }
  seenId = Math.max(seenId, newest);
  primed = true;
  const when = payload.lastScanAt ? new Date(payload.lastScanAt).toLocaleTimeString("zh-CN", { hour12: false }) : "尚未扫描";
  const settings = payload.settings || {};
  const chop = document.querySelector("#chopPct");
  if (chop && document.activeElement !== chop && settings.priceChopPct != null) {
    chop.value = settings.priceChopPct;
  }
  const error = payload.lastError ? ` · ${payload.lastError}` : "";
  if (Date.now() >= noticeUntil) {
    document.querySelector("#broadcastMeta").textContent = `最近扫描 ${when} · 每轮最多 ${settings.maxPerRound || 5} 条${error}`;
  }
}

document.querySelector("#saveChop").addEventListener("click", async () => {
  const value = Number(document.querySelector("#chopPct").value);
  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ priceChopPct: value }),
  });
  const payload = await response.json();
  const meta = document.querySelector("#broadcastMeta");
  meta.textContent = response.ok ? `价格震荡带已改为 ${payload.settings.priceChopPct}%` : (payload.error || "震荡带没有保存");
  noticeUntil = Date.now() + 2500;
});

window.syncBroadcastLimit = async (limit) => {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limit: limit === "all" ? "all" : Number(limit) }),
  });
};

function orderBody() {
  return {
    symbol: document.querySelector("#orderSymbol").value.trim(),
    side: document.querySelector("#orderSide").value,
    type: document.querySelector("#orderType").value,
    quantity: document.querySelector("#orderQty").value.trim(),
    price: document.querySelector("#orderPrice").value.trim(),
    reduceOnly: document.querySelector("#reduceOnly").checked,
  };
}

function setPreview(text) {
  document.querySelector("#preview").textContent = text;
}

async function refreshTrade() {
  const response = await fetch("/api/trade/status");
  const status = await response.json();
  const bits = [
    status.tradingEnabled ? "真实下单已打开" : "真实下单关闭",
    status.keysConfigured ? `密钥 ${status.keyHint || "已配置"}（${status.keySource}）` : "未配置密钥",
    `单笔上限 ${status.maxOrderUsdt || "—"} USDT`,
    "不会自动跟单",
  ];
  document.querySelector("#tradeStatus").textContent = bits.join(" · ");
  document.querySelector("#sendOrder").disabled = !status.tradingEnabled;
}

document.querySelector("#previewOrder").addEventListener("click", async () => {
  const response = await fetch("/api/trade/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(orderBody()),
  });
  const payload = await response.json();
  if (!response.ok) {
    setPreview(payload.error || "预览失败");
    return;
  }
  const side = payload.side === "BUY" ? "买入" : "卖出";
  const gate = payload.tradingEnabled ? "真实下单已打开，仍要点确认才会发送。" : "这只是预览，真实下单还关着。";
  const notional = Number(payload.notional);
  const notionalText = notional >= 1 ? notional.toFixed(2) : notional.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  setPreview(`${payload.symbol} ${side} ${payload.quantity}，参考价 ${payload.price}，名义价值约 ${notionalText} USDT。${payload.ok ? gate : payload.reason}`);
});

document.querySelector("#sendOrder").addEventListener("click", async () => {
  const body = orderBody();
  if (!window.confirm(`向你自己的币安合约账户发送 ${body.symbol} ${body.side} ${body.quantity}？`)) return;
  const response = await fetch("/api/trade/order", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-OI-Confirm": "1" },
    body: JSON.stringify({ ...body, confirm: "下单" }),
  });
  const payload = await response.json();
  if (!response.ok) {
    setPreview(payload.error || "没有发送");
    return;
  }
  setPreview(`已提交。订单号 ${payload.orderId || "—"}，状态 ${payload.status || "—"}。`);
});

document.querySelector("#keyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await fetch("/api/trade/keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      apiKey: document.querySelector("#apiKey").value,
      apiSecret: document.querySelector("#apiSecret").value,
    }),
  });
  const payload = await response.json();
  document.querySelector("#apiKey").value = "";
  document.querySelector("#apiSecret").value = "";
  setPreview(response.ok ? "密钥已写在本机，页面不再保留原文。" : (payload.error || "没有保存"));
  refreshTrade();
});

document.querySelector("#clearKeys").addEventListener("click", async () => {
  await fetch("/api/trade/keys", { method: "DELETE" });
  setPreview("本机保存的密钥已清除。环境变量里的密钥需要你自己取消。");
  refreshTrade();
});

document.querySelector("#testTrade").addEventListener("click", async () => {
  const response = await fetch("/api/trade/test", { method: "POST" });
  const payload = await response.json();
  if (!response.ok) {
    setPreview(payload.error || "连接失败");
    return;
  }
  setPreview(`已连上合约账户。可用 USDT ${Number(payload.usdtAvailable || 0).toFixed(2)}。`);
});

poll();
refreshTrade();
setInterval(poll, 3000);
})();
