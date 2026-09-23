const won=n=>new Intl.NumberFormat('ko-KR',{style:'currency',currency:'KRW',maximumFractionDigits:0}).format(n);
const pct=n=>`${n>=0?'+':''}${(n*100).toFixed(2)}%`;
const color=n=>n>=0?'profit':'loss';
const toast=m=>{const e=document.querySelector('#toast');e.style.whiteSpace='pre-line';e.textContent=typeof m==='string'?m:JSON.stringify(m);e.classList.add('show');setTimeout(()=>e.classList.remove('show'),3600)};
const AUTO_REFRESH_MS=5*60*1000;
let dashboardLoading=false;
let autoRefreshTimer=null;

async function load(){
 if(dashboardLoading)return;
 dashboardLoading=true;
 const refreshButton=document.querySelector('#refreshDashboard');
 if(refreshButton){refreshButton.disabled=true;refreshButton.textContent='갱신 중…'}
 const budget=document.querySelector('#budget').value;
 document.querySelector('#source').textContent='전체 종목 필터링 중…';
 try{
 const r=await fetch(`/api/dashboard?budget=${budget}`),d=await r.json();
 if(!r.ok){toast(d.detail);return}
 const alert=document.querySelector('#connectionAlert');
 alert.classList.toggle('hidden',!d.connection?.ip_not_allowed);
 document.querySelector('#cash').textContent=won(d.cash);
 const time=d.data_timestamp?` · ${new Date(d.data_timestamp).toLocaleString('ko-KR')}`:'';
 document.querySelector('#source').textContent=`데이터: ${d.data_source}${time}`;
 if(d.filter_stats){const s=d.filter_stats;document.querySelector('#filterStats').textContent=`전체 ${s.universe.toLocaleString()}개 → 예산·유동성 ${s.affordable}개 → 위험 제외 ${s.safe}개 → 지표 분석 ${s.analyzed}개`;}
 if(d.warnings?.length)toast(`${d.warnings.length}개 데이터 경고가 있습니다.`);
 document.querySelector('#cards').innerHTML=d.recommendations.length?d.recommendations.map((s,i)=>`<article class="card"><span class="rank">#${i+1} · <span class="score">${s.score}점</span></span><div class="stock">${s.name}</div><div class="symbol">${s.symbol}</div><div class="price">${won(s.price)}</div><div class="metrics">MA5 ${won(s.ma5)}<br>MA20 ${won(s.ma20)}<br>RSI ${s.rsi14.toFixed(1)} · 거래량 ${s.volume_ratio.toFixed(1)}배</div><div class="reason">${s.reasons.slice(0,2).join('<br>')}</div><button class="buy" onclick="buy('${s.symbol}',${s.buyable_quantity})">${s.buyable_quantity}주 가상매수</button></article>`).join(''):'<div class="empty">예산 조건에 맞는 후보가 없습니다.</div>';
 const p=d.performance;
 document.querySelector('#performance').innerHTML=[['총 평가자산',won(p.equity),''],['누적 손익',won(p.total_pnl),color(p.total_pnl)],['누적 수익률',pct(p.total_return_rate),color(p.total_return_rate)],['실현 / 평가 손익',`${won(p.realized_pnl)} / ${won(p.unrealized_pnl)}`,color(p.realized_pnl+p.unrealized_pnl)],['매도 승률',p.sell_count?`${pct(p.win_rate)} · ${p.sell_count}건`:'기록 없음','']].map(x=>`<div class="kpi"><small>${x[0]}</small><b class="${x[2]}">${x[1]}</b></div>`).join('');
 const a=d.auto_exit;
 document.querySelector('#autoStatus').textContent=a.enabled?(a.last_error?'감시 오류':'자동 감시 ON'):'자동 감시 OFF';
 document.querySelector('#autoStatus').className=a.enabled&&!a.last_error?'positive':'negative';
 document.querySelector('#autoRules').innerHTML=`<div class="auto-rules"><span>손절 ${(a.stop_loss_rate*100).toFixed(1)}%</span><span>익절 +${(a.take_profit_rate*100).toFixed(1)}%</span><span>추적손절 ${(a.trailing_stop_rate*100).toFixed(1)}%</span><span>최대 ${a.max_holding_days}일</span><span>${a.interval_seconds}초 간격</span><span>연속오류 ${a.consecutive_errors||0}회</span><span>${a.next_check?'다음 '+new Date(a.next_check).toLocaleTimeString('ko-KR'):'서버 시작 후 확인 예정'}</span></div>${a.last_error?`<small class="negative">${a.last_error}</small>`:''}`;
 const ab=d.auto_buy,paused=Boolean(ab.state.auto_buy_paused),toggle=document.querySelector('#automationToggle');
 document.querySelector('#autoBuyStatus').textContent=paused?`중지됨 · ${ab.state.pause_reason}`:(ab.last_error?'감시 오류':'실행 중');
 document.querySelector('#autoBuyStatus').className=paused||ab.last_error?'negative':'positive';
 toggle.textContent=paused?'자동매수 시작':'신규 자동매수 긴급정지';toggle.dataset.paused=paused?'true':'false';toggle.className=paused?'danger-button start':'danger-button';
 const risk=ab.risk?`일일 손익 ${pct(ab.risk.loss_rate)}`:'일일 기준값 대기';
 const mf=ab.market_filter;
 const marketRule=mf?`${mf.label} · 기준 ${mf.min_score}점 · MA20 위 ${(mf.breadth*100).toFixed(0)}%`:`기본 기준 ${ab.min_score}점`;
 document.querySelector('#autoBuyRules').innerHTML=`<div class="auto-rules"><span>${ab.interval_seconds}초 간격</span><span>${marketRule}</span><span>최대 ${ab.max_positions}종목</span><span>1회 ${won(ab.max_order_amount)}</span><span>일손실 ${(ab.daily_loss_limit_rate*100).toFixed(1)}%</span><span>${risk}</span><span>연속오류 ${ab.consecutive_errors||0}회</span><span>${ab.next_check?'다음 '+new Date(ab.next_check).toLocaleTimeString('ko-KR'):'검사 대기'}</span></div>${ab.last_error?`<small class="negative">${ab.last_error}</small>`:''}`;
 document.querySelector('#autoSignals').innerHTML=ab.signals.length?`<b>최근 자동매수 신호</b>${ab.signals.map(x=>`<div class="row"><span>${x.signal_date} · <b>${x.name||x.symbol}</b> <small>${x.symbol}</small></span><span>${x.score}점 · ${won(x.price)}</span><small>${x.reason}</small></div>`).join('')}`:'';
 const market=document.querySelector('#marketStatus');market.textContent=d.market.label+(d.market.next_event?` · ${new Date(d.market.next_event).toLocaleString('ko-KR')}`:'');market.className=d.market.status==='OPEN'?'market-badge open':'market-badge';
 const dr=d.daily_report;document.querySelector('#dailyReport').innerHTML=`<div class="compare-line"><span>날짜</span><b>${dr.date}</b></div><div class="compare-line"><span>매수 / 매도</span><b>${dr.buy_count}건 / ${dr.sell_count}건</b></div><div class="compare-line"><span>실현손익</span><b class="${color(dr.realized_pnl)}">${won(dr.realized_pnl)}</b></div><div class="compare-line"><span>일일 수익률</span><b class="${color(dr.return_rate)}">${pct(dr.return_rate)}</b></div>`;
 const dh=d.daily_performance_history||[];document.querySelector('#dailyPerformanceHistory').innerHTML=dh.length?`<div class="daily-table"><div class="daily-head"><span>날짜</span><span>평가자산</span><span>일손익</span><span>누적손익</span><span>매수/매도</span><span>상태</span></div>${dh.map(x=>`<div class="daily-line"><span>${x.performance_date}</span><b>${won(x.equity)}</b><b class="${color(x.daily_pnl)}">${won(x.daily_pnl)}<small>${pct(x.daily_return_rate)}</small></b><b class="${color(x.total_pnl)}">${won(x.total_pnl)}</b><span>${x.buy_count}/${x.sell_count}</span><small>${x.is_final?'확정':'진행 중'} · ${x.price_source}</small></div>`).join('')}</div>`:'<div class="empty">서버 시작 후 일별 성과가 저장됩니다.</div>';
 document.querySelector('#performanceCompare').innerHTML=d.comparison?`<div class="compare-line"><span>최근 백테스트</span><b>${pct(d.comparison.backtest_return_rate)}</b></div><div class="compare-line"><span>PAPER 누적</span><b>${pct(d.comparison.paper_return_rate)}</b></div><div class="compare-line"><span>성과 차이</span><b class="${color(d.comparison.difference)}">${pct(d.comparison.difference)}</b></div>`:'<div class="empty">백테스트를 실행하면 비교 결과가 표시됩니다.</div>';
 const cfg=d.automation_config;document.querySelector('#cfgStop').value=(cfg.stop_loss_rate*100).toFixed(1);document.querySelector('#cfgTake').value=(cfg.take_profit_rate*100).toFixed(1);document.querySelector('#cfgTrailing').value=(cfg.trailing_stop_rate*100).toFixed(1);document.querySelector('#cfgHolding').value=cfg.max_holding_days;document.querySelector('#cfgScore').value=cfg.min_score;document.querySelector('#cfgPositions').value=cfg.max_positions;document.querySelector('#cfgOrder').value=cfg.max_order_amount;document.querySelector('#cfgDailyLoss').value=(cfg.daily_loss_limit_rate*100).toFixed(1);
 document.querySelector('#automationEvents').innerHTML=d.automation_events.length?d.automation_events.map(x=>`<div class="row"><span><b class="${x.event_type.includes('ERROR')||x.event_type.includes('HALT')?'event-error':x.event_type.includes('BUY')?'event-buy':''}">${x.event_type}</b>${x.symbol?` · ${x.symbol}`:''}<br><small>${x.message}</small></span><small>${x.created_at}</small></div>`).join(''):'<div class="empty">아직 자동매매 판단 기록이 없습니다.</div>';
 document.querySelector('#holdings').innerHTML=d.holdings.length?d.holdings.map(x=>`<div class="row"><span><b>${x.name}</b><br><small>${x.quantity}주 · 평균 ${won(x.avg_price)} · 현재 ${won(x.current_price)}</small><br><button class="sell" onclick="sell('${x.symbol}',${x.quantity})">${x.quantity}주 가상매도</button></span><b class="${color(x.unrealized_pnl)}">${won(x.unrealized_pnl)}<br><small>${pct(x.return_rate)}</small></b></div>`).join(''):'<div class="empty">아직 보유 종목이 없습니다.</div>';
 document.querySelector('#trades').innerHTML=d.trades.length?d.trades.map(x=>`<div class="row"><span><b>${x.name}</b> <small class="badge-${x.side.toLowerCase()}">${x.side}</small><br><small>${x.created_at}${x.reason&&x.reason!=='MANUAL'?` · ${x.reason}`:''}</small></span><b>${x.quantity}주 · ${won(x.price)}${x.side==='SELL'?`<br><small class="${color(x.realized_pnl)}">손익 ${won(x.realized_pnl)}</small>`:''}</b></div>`).join(''):'<div class="empty">아직 거래 기록이 없습니다.</div>';
 document.querySelector('#history').innerHTML=d.recommendation_history.length?d.recommendation_history.map(x=>`<div class="row"><span><b>${x.name}</b> <small>${x.symbol}</small><br><small>${x.recommendation_date} · ${x.score}점 · 추천 ${won(x.price)}</small></span><span>현재 ${won(x.current_price)}</span><b class="${color(x.return_rate)}">${pct(x.return_rate)}</b></div>`).join(''):'<div class="empty">대시보드를 조회하면 추천 이력이 저장됩니다.</div>';
 }finally{
  if(refreshButton){refreshButton.disabled=false;refreshButton.textContent='새로고침'}
  dashboardLoading=false;
 }
}

function startAutoRefresh(){
 if(autoRefreshTimer)clearInterval(autoRefreshTimer);
 autoRefreshTimer=setInterval(()=>load(),AUTO_REFRESH_MS);
}

async function trade(path,symbol,quantity,message){if(!confirm(message))return;const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,quantity})}),d=await r.json();toast(d.message||d.detail);if(r.ok)load()}
const buy=(symbol,quantity)=>trade('/api/paper/buy',symbol,quantity,`${quantity}주를 가상매수할까요?`);
const sell=(symbol,quantity)=>trade('/api/paper/sell',symbol,quantity,`${quantity}주 전량을 가상매도할까요?`);
document.querySelector('#scanForm').addEventListener('submit',e=>{e.preventDefault();load()});load();startAutoRefresh();
document.querySelector('#refreshDashboard').addEventListener('click',load);
document.querySelector('#resetPaper').addEventListener('click',async()=>{
 if(!confirm('PAPER 가상계좌와 거래 기록을 초기화할까요?\\n기존 가상매매 기록, 자동매수 신호, 일별 성과가 삭제됩니다.'))return;
 const r=await fetch('/api/paper/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:'RESET_PAPER'})}),d=await r.json();
 toast(d.message||d.detail);if(r.ok)load();
});
async function reconnectToss(){const status=document.querySelector('#tossStatus');status.textContent='새 토큰 연결 중…';const r=await fetch('/api/toss/reconnect',{method:'POST'}),d=await r.json();if(!r.ok){status.textContent='연결 실패';toast(d.detail?.message||'연결 실패');await load();return}status.textContent=`연결됨 · 계좌 ${d.accounts.length}개`;status.classList.add('ok');toast(d.message);await load();const h=await fetch('/api/toss/holdings'),hd=await h.json();if(!h.ok){toast(hd.detail?.message||'보유 주식 조회 실패');return}const result=hd.result;document.querySelector('#realHoldings').innerHTML=result.items?.length?result.items.map(x=>`<div class="row"><span><b>${x.name}</b> <small>${x.symbol}</small><br><small>${x.quantity}주 · 평균 ${won(Number(x.averagePurchasePrice))}</small></span><b>${won(Number(x.marketValue.amount))}<br><small class="${color(Number(x.profitLoss.rate))}">${(Number(x.profitLoss.rate)*100).toFixed(2)}%</small></b></div>`).join(''):'<div class="empty">보유 주식이 없습니다.</div>'}
document.querySelector('#connect').addEventListener('click',reconnectToss);
document.querySelector('#recoverConnection').addEventListener('click',reconnectToss);

document.querySelector('#backtestForm').addEventListener('submit',async e=>{
 e.preventDefault();const out=document.querySelector('#backtestResult');out.className='empty';out.textContent='과거 일봉을 분석 중입니다…';
 const body={symbol:document.querySelector('#btSymbol').value,initial_cash:Number(document.querySelector('#btCash').value),stop_loss_percent:Number(document.querySelector('#btStop').value),take_profit_percent:Number(document.querySelector('#btTake').value),max_holding_days:Number(document.querySelector('#btDays').value)};
 const r=await fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();
 if(!r.ok){out.textContent=d.detail?.message||d.detail||'백테스트 실패';toast(out.textContent);return}
 out.className='';const trades=d.trades.length?d.trades.map(x=>`<div class="row"><span>${x.entry_date} → ${x.exit_date}<br><small>${x.reason}</small></span><span>${x.quantity}주 · ${won(x.entry_price)} → ${won(x.exit_price)}</span><b class="${color(x.pnl)}">${won(x.pnl)}<br><small>${pct(x.return_rate)}</small></b></div>`).join(''):'<div class="empty">조건에 해당하는 매수 신호가 없었습니다.</div>';
 const verifiedReturn=d.initial_cash?d.total_pnl/d.initial_cash:d.total_return_rate;
 out.innerHTML=`<div class="backtest-kpis">${[['최종 평가금',won(d.final_equity)],['총 손익',won(d.total_pnl)],['수익률',pct(verifiedReturn)],['최대 낙폭',pct(-d.max_drawdown)],['승률 / 거래',`${pct(d.win_rate)} · ${d.trade_count}건`]].map(x=>`<div class="kpi"><small>${x[0]}</small><b>${x[1]}</b></div>`).join('')}</div><div class="backtest-trades"><b>${d.name} · ${d.start_date}~${d.end_date}</b>${trades}</div>`;
});

document.querySelector('#automationToggle').addEventListener('click',async e=>{
 const paused=e.currentTarget.dataset.paused==='true';
 const message=paused?'PAPER 자동매수를 시작할까요?':'신규 PAPER 자동매수를 즉시 중지할까요?';
 if(!confirm(message))return;
 const r=await fetch('/api/paper/automation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:!paused})}),d=await r.json();
 toast(d.message||d.detail);if(r.ok)load();
});

document.querySelector('#automationConfigForm').addEventListener('submit',async e=>{
 e.preventDefault();const body={stop_loss_percent:Number(document.querySelector('#cfgStop').value),take_profit_percent:Number(document.querySelector('#cfgTake').value),trailing_stop_percent:Number(document.querySelector('#cfgTrailing').value),max_holding_days:Number(document.querySelector('#cfgHolding').value),min_score:Number(document.querySelector('#cfgScore').value),max_positions:Number(document.querySelector('#cfgPositions').value),max_order_amount:Number(document.querySelector('#cfgOrder').value),daily_loss_limit_percent:Number(document.querySelector('#cfgDailyLoss').value)};
 const r=await fetch('/api/paper/automation/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();
 toast(d.message||d.detail);if(r.ok)load();
});
