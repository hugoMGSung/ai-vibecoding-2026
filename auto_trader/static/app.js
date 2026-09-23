const $ = (id) => document.getElementById(id);
let selectedChartSymbol = null;
let activeChartCandles = [];

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = $("theme-toggle");
  button.textContent = theme === "dark" ? "☀️ Light" : "🌙 Dark";
  button.setAttribute("aria-label", theme === "dark" ? "라이트 테마로 변경" : "다크 테마로 변경");
  if (activeChartCandles.length) drawCandles(activeChartCandles);
}

const savedTheme = localStorage.getItem("theme") || "light";
applyTheme(savedTheme);
$("theme-toggle").addEventListener("click", () => {
  const nextTheme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("theme", nextTheme);
  applyTheme(nextTheme);
});

function setTossStatus(connected, label) {
  const button = $("toss-status");
  button.textContent = label;
  button.className = `status-button ${connected ? "status-connected" : "status-failed"}`;
}

async function checkTossStatus() {
  const button = $("toss-status");
  button.textContent = "연결 확인 중...";
  button.className = "status-button status-checking";
  try {
    const symbol = ($("symbols").value.split(",")[0] || "000660").trim();
    const response = await fetch(`/api/v1/toss/status?symbol=${encodeURIComponent(symbol)}`);
    const data = await response.json();
    setTossStatus(Boolean(data.connected), data.label || "연결실패");
  } catch (_) { setTossStatus(false, "연결실패"); }
}

async function loadMode() {
  try {
    const response = await fetch("/health");
    const data = await response.json();
    const button = $("trading-mode");
    button.textContent = data.mode;
    button.className = `status-button ${data.mode === "PAPER" ? "mode-paper" : "mode-dry-run"}`;
  } catch (_) { $("trading-mode").textContent = "모드 확인 실패"; }
}

async function loadPrices() {
  const symbols = $("symbols").value.trim();
  $("message").textContent = "토스증권 API 조회 중...";
  try {
    const response = await fetch(`/api/v1/market/prices?symbols=${encodeURIComponent(symbols)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "조회 실패");
    $("prices").innerHTML = data.map(p => `<tr><td>${p.symbol}</td><td>${p.name || "-"}</td><td>${Number(p.price).toLocaleString()}</td><td>${p.currency}</td></tr>`).join("") || '<tr><td colspan="4">데이터가 없습니다.</td></tr>';
    $("message").textContent = "조회 완료";
  } catch (error) { $("message").textContent = error.message; }
}

$("symbols").addEventListener("input", async () => {
  const query = $("symbols").value.trim();
  if (!query || query.includes(",") || /^\d+$/.test(query)) { $("search-results").innerHTML = ""; return; }
  try {
    const response = await fetch(`/api/v1/market/stocks/search?q=${encodeURIComponent(query)}`);
    const matches = await response.json();
    $("search-results").innerHTML = matches.map(item => `<button type="button" class="search-result" data-symbol="${item.symbol}"><strong>${item.name}</strong><span>${item.symbol}</span></button>`).join("");
    $("message").textContent = matches.length ? `${matches.length}개 종목을 찾았습니다.` : "일치하는 종목이 없습니다.";
  } catch (_) { /* 조회 버튼에서 최종 오류를 표시한다. */ }
});

$("search-results").addEventListener("click", (event) => {
  const result = event.target.closest(".search-result");
  if (!result) return;
  $("symbols").value = result.dataset.symbol;
  $("search-results").innerHTML = "";
  loadPrices();
});

async function loadPortfolio() { const r = await fetch("/api/v1/portfolio"); $("portfolio").textContent = JSON.stringify(await r.json(), null, 2); }
async function findCandidates() {
  $("recommendation-message").textContent = "추천 후보를 찾는 중...";
  $("recommendation-pipeline").textContent = "전체 2,601개 → 예산·유동성 30개 → 위험 제외 12개 → 지표 분석 12개";
  try {
    const response = await fetch("/api/v1/recommendations");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "후보 조회 실패");
    $("recommendations").innerHTML = data.map(item => `<article class="recommendation-item"><div class="recommendation-rank">#${item.rank} · ${item.score}점</div><h3 class="recommendation-name" data-symbol="${item.symbol}" data-name="${item.name}">${item.name}</h3><div class="recommendation-symbol">${item.symbol}</div><div class="recommendation-price">${Number(item.price).toLocaleString()} ${item.currency}</div><div class="indicator-box">${item.indicators}</div><p>추천 수량 ${item.recommended_quantity.toLocaleString()}주 · ${Number(item.recommended_amount).toLocaleString()} ${item.currency}</p><button class="paper-buy" data-symbol="${item.symbol}" data-price="${item.price}" data-quantity="${item.recommended_quantity}">PAPER 매수</button></article>`).join("");
    $("recommendation-message").textContent = "가상매매용 후보입니다. 실제 주문은 발생하지 않습니다.";
  } catch (error) { $("recommendation-message").textContent = error.message; }
}

async function loadChart(symbol, name) {
  try {
    const response = await fetch(`/api/v1/market/candles?symbol=${encodeURIComponent(symbol)}&count=60`);
    const candles = await response.json();
    if (!response.ok) throw new Error(candles.detail || "차트 조회 실패");
    $("chart-card").hidden = false;
    $("chart-title").textContent = `${name} (${symbol}) 일봉 차트`;
    activeChartCandles = candles;
    drawCandles(candles);
  } catch (error) { $("recommendation-message").textContent = error.message; }
}

function drawCandles(candles) {
  // 토스증권 API는 최신 봉부터 반환하므로 차트 시간축은 과거→현재 순서로 그린다.
  candles = [...candles].reverse();
  const canvas = $("candle-chart"), ratio = window.devicePixelRatio || 1, width = canvas.clientWidth || 760, height = 360;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio); ctx.clearRect(0, 0, width, height);
  const values = candles.flatMap(c => [Number(c.high), Number(c.low)]), max = Math.max(...values), min = Math.min(...values), range = max - min || 1;
  const pad = { top: 20, right: 18, bottom: 28, left: 58 }, plotH = height - pad.top - pad.bottom, step = (width - pad.left - pad.right) / candles.length;
  const y = value => pad.top + (max - value) / range * plotH;
  ctx.font = "12px system-ui"; ctx.fillStyle = getComputedStyle(document.body).color; ctx.strokeStyle = "#98a2b3";
  ctx.globalAlpha = .35; [0, .5, 1].forEach(t => { const lineY = pad.top + plotH * t; ctx.beginPath(); ctx.moveTo(pad.left, lineY); ctx.lineTo(width - pad.right, lineY); ctx.stroke(); ctx.fillText(Math.round(max - range * t).toLocaleString(), 4, lineY + 4); }); ctx.globalAlpha = 1;
  candles.forEach((c, index) => { const x = pad.left + step * index + step / 2, open = Number(c.open), close = Number(c.close), high = Number(c.high), low = Number(c.low), bullish = close >= open, color = bullish ? "#f04438" : "#2e6de6"; ctx.strokeStyle = color; ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(x, y(high)); ctx.lineTo(x, y(low)); ctx.stroke(); const top = y(Math.max(open, close)), body = Math.max(2, Math.abs(y(open) - y(close))); ctx.fillRect(x - Math.max(2, step * .28), top, Math.max(4, step * .56), body); });
}
// Enhanced chart renderer: candlesticks, 5/20-day moving averages, and volume.
function drawCandles(candles) {
  candles = [...candles].reverse();
  const canvas = $("candle-chart"), ratio = window.devicePixelRatio || 1, width = canvas.clientWidth || 760, height = 460;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
  const pad = { top: 28, right: 18, bottom: 30, left: 58 }, volumeHeight = 82, gap = 18;
  const priceHeight = height - pad.top - pad.bottom - volumeHeight - gap, step = (width - pad.left - pad.right) / candles.length;
  const values = candles.flatMap(c => [Number(c.high), Number(c.low)]), max = Math.max(...values), min = Math.min(...values), range = max - min || 1;
  const y = value => pad.top + (max - value) / range * priceHeight;
  const volumeMax = Math.max(...candles.map(c => Number(c.volume) || 0), 1), volumeTop = pad.top + priceHeight + gap;
  const isDark = document.documentElement.dataset.theme === "dark";
  const textColor = isDark ? "#f2f4f7" : "#172033";
  const gridColor = isDark ? "#667085" : "#98a2b3";
  ctx.font = "12px system-ui"; ctx.fillStyle = textColor; ctx.strokeStyle = gridColor; ctx.globalAlpha = .3;
  [0, .5, 1].forEach(t => { const lineY = pad.top + priceHeight * t; ctx.beginPath(); ctx.moveTo(pad.left, lineY); ctx.lineTo(width - pad.right, lineY); ctx.stroke(); ctx.fillText(Math.round(max - range * t).toLocaleString(), 4, lineY + 4); }); ctx.globalAlpha = 1;
  const sma = period => candles.map((_, i) => i + 1 >= period ? candles.slice(i - period + 1, i + 1).reduce((sum, c) => sum + Number(c.close), 0) / period : null);
  const ma5 = sma(5), ma20 = sma(20);
  const drawLine = (valuesToDraw, color) => { ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath(); let started = false; valuesToDraw.forEach((value, i) => { if (value === null) return; const x = pad.left + step * i + step / 2; if (!started) { ctx.moveTo(x, y(value)); started = true; } else ctx.lineTo(x, y(value)); }); ctx.stroke(); };
  candles.forEach((c, index) => { const x = pad.left + step * index + step / 2, open = Number(c.open), close = Number(c.close), bullish = close >= open, color = bullish ? "#f04438" : "#2e6de6"; ctx.strokeStyle = color; ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(x, y(Number(c.high))); ctx.lineTo(x, y(Number(c.low))); ctx.stroke(); const bodyTop = y(Math.max(open, close)), body = Math.max(2, Math.abs(y(open) - y(close))); ctx.fillRect(x - Math.max(2, step * .28), bodyTop, Math.max(4, step * .56), body); const volume = Number(c.volume) || 0; ctx.fillRect(x - Math.max(2, step * .28), volumeTop + volumeHeight - (volume / volumeMax * volumeHeight), Math.max(4, step * .56), volume / volumeMax * volumeHeight); });
  drawLine(ma5, "#f79009"); drawLine(ma20, "#7f56d9");
  const windowCandles = candles.slice(-20), support = Math.min(...windowCandles.map(c => Number(c.low))), resistance = Math.max(...windowCandles.map(c => Number(c.high)));
  const drawLevel = (value, color, label) => { const lineY = y(value); ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([6, 4]); ctx.beginPath(); ctx.moveTo(pad.left, lineY); ctx.lineTo(width - pad.right, lineY); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = color; ctx.fillText(`${label} ${Math.round(value).toLocaleString()}`, width - 112, lineY - 5); };
  drawLevel(support, "#12b76a", "지지"); drawLevel(resistance, "#f04438", "저항");
  ctx.fillStyle = textColor; ctx.font = "12px system-ui"; ctx.fillText("MA5", pad.left + 8, 16); ctx.fillStyle = "#f79009"; ctx.fillRect(pad.left + 42, 7, 18, 2); ctx.fillStyle = textColor; ctx.fillText("MA20", pad.left + 68, 16); ctx.fillStyle = "#7f56d9"; ctx.fillRect(pad.left + 108, 7, 18, 2); ctx.fillStyle = textColor; ctx.fillText("거래량", pad.left, volumeTop - 5);
}

async function loadCapital() {
  try {
    const response = await fetch("/api/v1/capital");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "금액 조회 실패");
    const format = (value) => `${Number(value).toLocaleString()} ${data.currency}`;
    $("account-cash").textContent = format(data.account_cash);
    $("recommended-amount").textContent = format(data.recommended_amount);
    const ratio = Number(data.recommended_ratio) * 100;
    const note = document.querySelector(".capital-card .muted");
    if (note) note.textContent = `현재 추천 사용 금액은 가상계좌 현금의 ${ratio.toLocaleString(undefined, { maximumFractionDigits: 2 })}%로 계산됩니다.`;
  } catch (error) { $("account-cash").textContent = error.message; $("recommended-amount").textContent = "-"; }
}

$("load").addEventListener("click", loadPrices);
$("symbols").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadPrices(); } });
$("refresh-page").addEventListener("click", () => window.location.reload());
$("toss-status").addEventListener("click", checkTossStatus);
$("find-candidates").addEventListener("click", findCandidates);
$("recommendations").addEventListener("click", async (event) => {
  const name = event.target.closest(".recommendation-name");
  if (name) {
    const chart = $("chart-card");
    if (!chart.hidden && selectedChartSymbol === name.dataset.symbol) {
      chart.hidden = true;
      selectedChartSymbol = null;
    } else {
      selectedChartSymbol = name.dataset.symbol;
      await loadChart(name.dataset.symbol, name.dataset.name);
    }
    return;
  }
  const button = event.target.closest(".paper-buy");
  if (!button) return;
  const response = await fetch("/api/v1/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol: button.dataset.symbol, side: "BUY", quantity: button.dataset.quantity, price: button.dataset.price }) });
  const order = await response.json();
  alert(response.ok ? `PAPER 주문 ${order.status}` : (order.detail || "주문 실패"));
});
loadPrices(); loadCapital(); loadPortfolio(); checkTossStatus(); loadMode();
