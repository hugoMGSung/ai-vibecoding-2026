const $ = (id) => document.getElementById(id);
let selectedChartSymbol = null;
let activeChartCandles = [];
const portfolioCard = document.querySelector(".portfolio-card");
const autoCard = document.querySelector(".auto-card");
const chartCard = document.querySelector("#chart-card");
if (portfolioCard) {
  const insertBefore = chartCard || autoCard;
  if (insertBefore) insertBefore.parentNode.insertBefore(portfolioCard, insertBefore);
}
if (autoCard) autoCard.parentNode.appendChild(autoCard);

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
    const response = await fetch("/api/v1/toss/status?symbol=000660", { cache: "no-store" });
    const data = await response.json();
    setTossStatus(response.ok && Boolean(data.connected), data.label || "연결실패");
    button.title = data.detail || "토스 시세 API 연결 상태";
  } catch (error) { setTossStatus(false, "연결실패"); button.title = error.message; }
}

async function loadLiveAccount() {
  $("live-account-summary").textContent = "실계좌 조회 중...";
  try {
    const response = await fetch("/api/v1/live/snapshot", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "실계좌 조회 실패");
    $("live-account-summary").textContent = `토스 실계좌 · 원화 매수 가능 ${Number(data.cash_krw).toLocaleString()}원 · 달러 매수 가능 ${Number(data.cash_usd).toLocaleString()} USD · 미체결 ${data.open_order_count}건 · PAPER 현금 ${Number(data.paper_cash_for_comparison).toLocaleString()}원`;
    const container = $("live-account-positions");
    container.replaceChildren();
    for (const item of data.positions) {
      const row = document.createElement("p");
      row.textContent = `${item.name || item.symbol} (${item.symbol}) · ${item.quantity}주 · 평균 매입가 ${Number(item.average_purchase_price).toLocaleString()} ${item.currency} · 평가 ${Number(item.market_value).toLocaleString()} ${item.currency}`;
      container.appendChild(row);
    }
    if (!data.positions.length) container.textContent = "보유 주식이 없습니다.";
  } catch (error) { $("live-account-summary").textContent = error.message; $("live-account-positions").replaceChildren(); }
}

async function loadOperationsStatus() {
  try {
    const response = await fetch("/api/v1/operations/status");
    const data = await response.json();
    $("operations-alerts").textContent = `운영 경고 ${data.alerts_today}건 · 실계좌 주문 재동기화 ${data.live_reconciliation.ok ? "완료" : "확인 필요"}`;
  } catch (error) { $("operations-alerts").textContent = `운영 상태 조회 실패: ${error.message}`; }
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
  const symbolsInput = $("symbols");
  const symbols = symbolsInput ? symbolsInput.value.trim() : "";
  $("message").textContent = "토스증권 API 조회 중...";
  try {
    const response = await fetch(`/api/v1/market/prices?symbols=${encodeURIComponent(symbols)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "조회 실패");
    $("prices").innerHTML = data.map(p => `<tr><td>${p.symbol}</td><td>${p.name || "-"}</td><td>${Number(p.price).toLocaleString()}</td><td>${p.currency}</td></tr>`).join("") || '<tr><td colspan="4">데이터가 없습니다.</td></tr>';
    $("message").textContent = "조회 완료";
  } catch (error) { $("message").textContent = error.message; }
}

if ($("symbols")) $("symbols").addEventListener("input", async () => {
  const query = $("symbols").value.trim();
  if (!query || query.includes(",") || /^\d+$/.test(query)) { $("search-results").innerHTML = ""; return; }
  try {
    const response = await fetch(`/api/v1/market/stocks/search?q=${encodeURIComponent(query)}`);
    const matches = await response.json();
    $("search-results").innerHTML = matches.map(item => `<button type="button" class="search-result" data-symbol="${item.symbol}"><strong>${item.name}</strong><span>${item.symbol}</span></button>`).join("");
    $("message").textContent = matches.length ? `${matches.length}개 종목을 찾았습니다.` : "일치하는 종목이 없습니다.";
  } catch (_) { /* 조회 버튼에서 최종 오류를 표시한다. */ }
});

if ($("search-results")) $("search-results").addEventListener("click", (event) => {
  const result = event.target.closest(".search-result");
  if (!result) return;
  $("symbols").value = result.dataset.symbol;
  $("search-results").innerHTML = "";
  loadPrices();
});

async function loadPortfolio() { const r = await fetch("/api/v1/portfolio"); const portfolio = await r.json(); if ($("portfolio")) $("portfolio").textContent = JSON.stringify(portfolio, null, 2); const summary = await (await fetch("/api/v1/portfolio/summary")).json(); $("portfolio-summary").innerHTML = `<div class="portfolio-total"><div><span>보유 평가금액</span><strong>${Number(summary.total_value).toLocaleString()} KRW</strong></div><div><span>총 수익률</span><strong class="${Number(summary.total_return_rate) >= 0 ? "profit" : "loss"}">${Number(summary.total_return_rate).toFixed(2)}%</strong></div></div>${summary.positions.length ? `<table><thead><tr><th>종목</th><th>수량</th><th>매수가(주당)</th><th>현재가</th><th>평가금액</th><th>수익률</th><th>매도</th></tr></thead><tbody>${summary.positions.map(item => `<tr><td>${item.name}<br><small>${item.symbol}</small></td><td>${item.quantity}</td><td>${Number(item.average_cost).toLocaleString()}</td><td>${Number(item.current_price).toLocaleString()}</td><td>${Number(item.market_value).toLocaleString()}</td><td class="${Number(item.return_rate) >= 0 ? "profit" : "loss"}">${Number(item.return_rate).toFixed(2)}%<br><small>${Number(item.profit_loss).toLocaleString()}</small></td><td><button class="sell-position" data-symbol="${item.symbol}" data-price="${item.current_price}" data-quantity="${item.quantity}">매도</button></td></tr>`).join("")}</tbody></table>` : '<p class="muted">현재 보유한 주식이 없습니다.</p>'}`; }

if ($("portfolio-summary")) $("portfolio-summary").addEventListener("click", async (event) => { const button = event.target.closest(".sell-position"); if (!button) return; if (!confirm(`${button.dataset.symbol} ${button.dataset.quantity}주를 PAPER 매도할까요?`)) return; const response = await fetch("/api/v1/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol: button.dataset.symbol, side: "SELL", quantity: button.dataset.quantity, price: button.dataset.price }) }); const data = await response.json(); if (!response.ok) alert(data.detail || "매도 실패"); await loadPortfolio(); await loadCapital(); });
async function loadAutoStatus() { const response = await fetch("/api/v1/auto-trader/status"); const data = await response.json(); $("auto-status").textContent = `${data.running ? "실행 중" : "중지됨"} · ${data.message || ""}${data.last_action ? ` · ${data.last_action}` : ""}`; const history = await (await fetch("/api/v1/auto-trader/history")).json(); $("auto-history").innerHTML = history.length ? history.map(item => `<div class="history-row"><time>${item.time}</time><strong>${item.event}</strong><span>${item.detail}</span></div>`).join("") : '<p class="muted">기록이 없습니다.</p>'; }
async function loadRiskStatus() {
  try {
    const response = await fetch("/api/v1/risk/status");
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "위험 상태 조회 실패");
    const button = $("emergency-stop");
    button.textContent = data.emergency_stop ? "긴급 정지 해제" : "긴급 매매 정지";
    button.classList.toggle("emergency-active", Boolean(data.emergency_stop));
    const daily = data.daily_risk_available === false ? `위험 계산 불가 · ${data.risk_error || "시세 확인 실패"}` : `당일 손익 ${Number(data.pnl || 0).toLocaleString()}원 / 손실 한도 -${Number(data.loss_limit || 0).toLocaleString()}원`;
    $("risk-status").textContent = `${daily} · 주문 최대 ${Number(data.limits.max_order_amount).toLocaleString()}원 · 종목 한도 ${Number(data.limits.max_symbol_exposure_amount).toLocaleString()}원/${Number(data.limits.max_symbol_quantity).toLocaleString()}주 · 전체 보유 한도 ${Number(data.limits.max_portfolio_exposure_amount).toLocaleString()}원 · ${data.emergency_stop ? `비상정지: ${data.emergency_reason}` : data.halted ? `당일 매수 정지: ${data.reason}` : "정상"}${data.live_order_ready ? "" : ` · LIVE 주문 잠김(${data.live_block_reason || "실계좌 위험 확인 필요"})`}`;
  } catch (error) { $("risk-status").textContent = error.message; }
}
function syncAutoButton(running) { const button = $("auto-start"); if (!button) return; button.textContent = running ? "자동매매 중지" : "자동매매 시작"; button.classList.toggle("stop-button", running); }
async function setAutoTrader(action) { const response = await fetch(`/api/v1/auto-trader/${action}`, { method: "POST" }); const data = await response.json(); if (!response.ok) { $("auto-status").textContent = data.detail || "자동매매 요청 실패"; return; } await loadAutoStatus(); await loadCapital(); await loadPortfolio(); }
async function toggleAutoTrader() { const response = await fetch("/api/v1/auto-trader/status"); const data = await response.json(); await setAutoTrader(data.running ? "stop" : "start"); syncAutoButton(!data.running); }
async function refreshDashboard() {
  await Promise.allSettled([loadCapital(), loadPortfolio(), loadAutoStatus(), loadRiskStatus(), loadOperationsStatus(), checkTossStatus()]);
}
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
    const totalReturn = Number(data.total_return_rate);
    $("total-portfolio-return").innerHTML = `<span>전체 포트폴리오 수익률(현금 포함)</span><strong class="${totalReturn >= 0 ? "profit" : "loss"}">${totalReturn.toFixed(2)}% <small>(${Number(data.total_profit).toLocaleString()} ${data.currency})</small></strong>`;
    const ratio = Number(data.recommended_ratio) * 100;
    $("ratio-input").value = ratio;
    const note = document.querySelector(".capital-card .muted");
    if (note) note.textContent = `현재 추천 사용 금액은 가상계좌 현금의 ${ratio.toLocaleString(undefined, { maximumFractionDigits: 2 })}%로 계산됩니다.`;
  } catch (error) { $("account-cash").textContent = error.message; $("recommended-amount").textContent = "-"; }
}
$("save-ratio").addEventListener("click", async () => {
  const percent = Number($("ratio-input").value);
  if (!Number.isFinite(percent) || percent < 0 || percent > 100) { alert("거래 비율은 0~100 사이로 입력하세요."); return; }
  const response = await fetch("/api/v1/capital/recommended-ratio", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ratio: percent / 100 }) });
  if (!response.ok) { alert("거래 비율 저장에 실패했습니다."); return; }
  await loadCapital();
});

if ($("load")) $("load").addEventListener("click", loadPrices);
if ($("symbols")) $("symbols").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadPrices(); } });
$("refresh-page").addEventListener("click", () => window.location.reload());
$("toss-status").addEventListener("click", checkTossStatus);
$("load-live-account").addEventListener("click", loadLiveAccount);
const autoStartButton = $("auto-start");
const autoStopButton = $("auto-stop");
if (autoStartButton) { autoStartButton.textContent = "자동매매 시작"; autoStartButton.addEventListener("click", toggleAutoTrader); }
if (autoStopButton) autoStopButton.style.display = "none";
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
$("emergency-stop").addEventListener("click", async () => {
  const current = await (await fetch("/api/v1/risk/status")).json();
  if (current.emergency_stop) {
    if (!confirm("비상 정지를 해제할까요? 당일 손실 한도 정지는 별도로 유지될 수 있습니다.")) return;
    await fetch("/api/v1/risk/emergency-stop/clear?confirm=true", { method: "POST" });
  } else {
    if (!confirm("긴급 정지하면 자동매매가 멈추고 신규 매수가 차단됩니다. 진행할까요?")) return;
    await fetch("/api/v1/risk/emergency-stop", { method: "POST" });
  }
  await Promise.allSettled([loadRiskStatus(), loadAutoStatus()]);
});
loadPrices(); loadCapital(); loadPortfolio(); loadAutoStatus(); loadRiskStatus(); loadOperationsStatus(); checkTossStatus(); loadMode();
setInterval(refreshDashboard, 30000);
