// github.io（GitHub Pages）是纯静态托管：POST /api/run 必然 405，
// 定时/手动推送由 GitHub Actions 完成，按钮在此环境下改为跳转 Actions。
const isGithubPages=location.hostname.endsWith('.github.io');
const ACTIONS_URL='https://github.com/k-macao/05/actions/workflows/daily-push.yml';
const toast=document.querySelector('#toast');
const runBtn=document.querySelector('#runBtn');
const runState=runBtn ? runBtn.querySelector('.run-state') : null;
const refreshBtn=document.querySelector('#refreshBtn');

function showToast(text,ms=2600){
  if(!toast) return;
  toast.textContent=text;
  toast.classList.add('show');
  setTimeout(()=>toast.classList.remove('show'),ms);
}

// 手动运行：调用后端 /api/run 完成聚合、AI 总结与 PushPlus 推送；github.io 静态托管下改为跳转 Actions 手动触发。
if(runBtn){
runBtn.onclick=async()=>{
  if(isGithubPages){
    showToast('GitHub Pages 为纯静态托管，已打开 Actions 手动触发每日推送');
    window.open(ACTIONS_URL,'_blank');
    return;
  }
  const originalState=runState ? runState.textContent : '';
  runBtn.disabled=true;
  if(runState) runState.textContent='运行中…';
  showToast('正在聚合内容并生成 AI 简报…');
  try{
    const deaiMode=typeof window.getDeaiMode==='function' ? window.getDeaiMode() : 'polish';
    const humanizerMode=typeof window.getHumanizerMode==='function' ? window.getHumanizerMode() : 'polish';
    const res=await fetch('api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({deai_mode:deaiMode, humanizer_mode:humanizerMode})});
    if(res.ok){
      const data=await res.json();
      showToast((data&&data.message)||'简报已生成，将推送至 PushPlus');
    }else{
      let message=null;
      try{const data=await res.json();if(data&&data.message)message=data.message;}catch(e){/* 非 JSON 错误体 */ }
      if(res.status===405) message='当前页面由静态托管提供，不支持 POST /api/run，请改用 server.py 启动服务';
      showToast(message||`后端返回 ${res.status}，请检查服务`,(res.status===409||res.status===422)?6000:2600);
    }
  }catch(e){
    showToast('无法连接推送服务，请确认后端正在运行');
  }finally{
    runBtn.disabled=false;
    if(runState) runState.textContent=originalState;
  }
};
}

const editBtn=document.querySelector('#editSchedule');
if(editBtn) editBtn.onclick=()=>showToast('可编辑每日 12:30 / 19:30 推送时间');
const expandBtn=document.querySelector('#expandBtn');
if(expandBtn) expandBtn.onclick=()=>showToast('正在展开 AI 分析与延展');

// Schedule toggle persistence
document.querySelectorAll('.pixel-switch input[data-toggle]').forEach(input=>{
  const key='schedule_'+input.dataset.toggle;
  const saved=localStorage.getItem(key);
  if(saved!==null)input.checked=saved==='1';
  input.onchange=()=>{
    localStorage.setItem(key,input.checked?'1':'0');
    showToast(input.checked?'已开启推送':'已关闭推送');
  };
});

/* ===================== AI 情绪热力图 · 关键词级线索（面向股市投资） =====================
   热力图以关键词为单元，每个格子可下钻查看该词的 9 维指标、两个港股 ETF 及对应指数（线索列表不展示）。
   指标体系（9 维，全部可复算）：
     热度(0-100)=提及×(1+ln(跨源+1))归一 · 情绪分(-1~+1)=(多-空)/信号 · 净信号 · 跨源共振
     分歧度=1-|情绪| · 爆发=热度×|情绪| · 资金倾向(流入/流出/观望) · 评级(强多/偏多/中性/偏空/强空)
     风险(低/中/高) · 置信度；并关联港股 ETF 双标的、对应指数与政策维度，便于主题推演。
   数据来自 /api/heatmap（与 AI 每日总结同批标题、零外部依赖、可离线）。
*/
(function(){
  const headlineEl=document.getElementById('heatmapHeadline');
  const metaEl=document.getElementById('heatmapMeta');
  const gridEl=document.getElementById('heatmapGrid');
  const indicatorsEl=document.getElementById('heatmapIndicators');
  const drawerEl=document.getElementById('heatmapDrawer');
  const countEl=document.getElementById('heatmapCount');
  const pillEl=document.getElementById('heatmapPill');
  const footEl=document.getElementById('heatmapFoot');
  const filtersEl=document.getElementById('heatmapFilters');
  const poolEl=document.getElementById('heatmapEtfPool');
  if(!gridEl || !headlineEl) return;

  let heatmapData=null;
  let activeFilter='all';

  function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function trunc(s,n){ s=String(s||''); return s.length>n ? s.slice(0,n-1)+'…' : s; }

  function heatClass(heat){
    if(heat>=75) return '沸腾';
    if(heat>=50) return '热';
    if(heat>=25) return '温';
    return '冷';
  }

  function sentimentClass(label){
    if(label==='偏多') return 'bull';
    if(label==='偏空') return 'press';
    return 'neutral';
  }

  function barClass(sentiment){
    if(sentiment>0.2) return 'heat-bull';
    if(sentiment<-0.2) return 'heat-bear';
    return 'heat-neutral';
  }

  function ratingBadge(rating){
    if(rating.includes('强多')) return 'hot';
    if(rating.includes('强空')) return 'warn';
    if(rating.includes('偏多')) return 'hot';
    if(rating.includes('偏空')) return 'warn';
    return 'neutral';
  }

  function riskBadge(risk){
    if(risk==='高') return 'warn';
    if(risk==='中') return 'neutral';
    return 'neutral';
  }

  function renderIndicators(indicators){
    if(!indicatorsEl) return;
    if(!indicators || !indicators.length){
      indicatorsEl.innerHTML='';
      return;
    }
    const title=`<div class="ind-title"><span class="ind-label">指标说明</span><span style="color:var(--ink-soft);font-weight:400;font-size:10px;">9 维量化 · 面向股市投资 · 阈值写死可复算</span></div>`;
    const grid='<div class="ind-grid">'+indicators.map(ind=>`
      <div class="ind-item">
        <b>${esc(ind.label)}</b>
        <div class="ind-formula">${esc(ind.formula)}</div>
        <div class="ind-use">${esc(ind.use)}</div>
      </div>`).join('')+'</div>';
    const note='<div style="margin-top:6px;color:var(--ink-soft);font-size:10px;line-height:1.6;">用法：沸腾+强多=主线机会需防拥挤回落；沸腾+强空=风险集中区；跨源≥4 为确认信号，1-2 源为噪音；分歧高时等方向确认，爆发系数高时注意追高风险。仅统计信号，不构成投资建议。</div>';
    indicatorsEl.innerHTML=title+grid+note;
  }

  // 本版新纳入的港股 ETF 产品池：多柜台产品直接列出 HKD / CNY / USD 代码。
  function renderEtfPool(items){
    if(!poolEl) return;
    if(!Array.isArray(items) || !items.length){
      poolEl.hidden=true;
      poolEl.innerHTML='';
      return;
    }
    const chips=items.map(etf=>{
      const counters=etf.counters||{HKD:etf.code};
      const counterText=Object.keys(counters).map(c=>`${c} ${counters[c].replace('.HK','')}`).join(' · ');
      return `<span class="etf-chip${Object.keys(counters).length>1?' multi':''}" title="${esc(etf.name)}｜对应标的：${esc(etf.benchmark)}">
        <b>${esc((etf.code||'').replace('.HK',''))}</b>
        <span class="etf-chip-name">${esc(trunc(etf.name,22))}</span>
        <small>${esc(counterText)}</small>
      </span>`;
    }).join('');
      poolEl.innerHTML=`<div class="etf-pool-head"><span class="ind-label">新上市场景</span>
      <b>本版新纳入 ${items.length} 只港交所 ETF</b>
      <span>（${items.filter(x=>(x.counters&&Object.keys(x.counters).length>1)).length} 只设多柜台，已映射进下方热力格子）</span></div>
      <div class="etf-pool-chips">${chips}</div>`;
    poolEl.hidden=false;
  }

  function renderMeta(data){
    if(!metaEl) return;
    const totalTitles=data.total_titles!=null ? data.total_titles : (data.keywords||[]).reduce((s,k)=>s+k.mentions,0);
    // overall bias tag color
    const bias=data.overall_bias||'中性';
    const biasCls=bias==='偏多'?'hot':(bias==='偏空'?'warn':'neutral');
    metaEl.innerHTML=`
      <span class="meta-tag ${biasCls==='hot'?'':(biasCls==='warn'?'press':'neutral')}">${esc(bias)}</span>
      <span class="heat-stat">${data.total_hit||0} 个关键词命中</span>
      <span>· ${data.total_mentions||0} 条线索</span>
      <span>· ${totalTitles} 篇标题</span>
      <span>· 信号 ${data.total_signals||0} 条</span>
      ${data.top_opportunity && data.top_opportunity.length ? `<span>· 机会 <b>${esc(data.top_opportunity.map(k=>k.keyword).join('、'))}</b></span>`:''}
      ${data.top_pressure && data.top_pressure.length ? `<span>· 承压 <b>${esc(data.top_pressure.map(k=>k.keyword).join('、'))}</b></span>`:''}
    `;
    if(pillEl){
      pillEl.textContent=`${data.total_hit||0} 词 · ${bias}`;
    }
    if(countEl){
      const visible=gridEl.querySelectorAll('.heatmap-cell:not([hidden])').length;
      const total=(data.keywords||[]).length;
      countEl.textContent= activeFilter==='all' ? `${total} 个关键词` : `${visible} / ${total}`;
    }
  }

  function openDrawer(kw){
    if(!drawerEl) return;
    // mark selected
    gridEl.querySelectorAll('.heatmap-cell').forEach(el=>{
      el.classList.toggle('selected', el.dataset.keyword===kw.keyword);
    });
    const policyTags=(kw.policy_tags||[]).length ? kw.policy_tags.map(t=>`<span class="badge neutral">${esc(t)}</span>`).join('') : '<span style="color:var(--ink-soft);font-size:10px;">无关联政策维度</span>';
    const etfPair=Array.isArray(kw.etf_pair) ? kw.etf_pair : [];
    const etfPairHtml=etfPair.length===2 ? `
      <div class="etf-pair" aria-label="港股 ETF 双标的">
        ${etfPair.map((etf,index)=>`<div class="etf-leg"><span class="etf-no">0${index+1}</span><div><b>${esc(etf.code)}</b> ${esc(etf.name)}<small>对应标的：${esc(etf.benchmark)}</small>${etf.multi_counter?`<small>多柜台：${esc(etf.counter_text||'')}</small>`:''}</div></div>`).join('')}
        <div class="etf-mapping">映射口径：${esc(etfPair[0].mapping||'主题相关指数')} · 仅作主题观察，不构成投资建议</div>
      </div>` : `<div class="etf-pair"><div class="etf-mapping">港股 ETF 双标的数据暂不可用</div></div>`;
    drawerEl.hidden=false;
    drawerEl.innerHTML=`
      <div class="drawer-head">
        <h3>${esc(kw.keyword)} <span style="font-weight:400;font-size:11px;color:var(--ink-soft);">（${esc(kw.tag)}｜${esc(kw.concept)}）</span></h3>
        <button class="drawer-close" aria-label="关闭">×</button>
      </div>
      <div class="drawer-meta">
        <span class="badge ${kw.heat>=75?'hot':kw.heat>=50?'hot':'neutral'}">${esc(kw.heat_level)}·热度${kw.heat}</span>
        <span class="badge ${sentimentClass(kw.sentiment_label)==='bull'?'hot':sentimentClass(kw.sentiment_label)==='press'?'warn':'neutral'}">${esc(kw.sentiment_label)}·净${kw.net>0?'+':''}${kw.net}</span>
        <span class="badge neutral">${kw.sources}源·${kw.mentions}条</span>
        <span class="badge neutral">信号 ${kw.bull}:${kw.bear}</span>
        <span class="badge ${ratingBadge(kw.rating)}">${esc(kw.rating)}</span>
        <span class="badge ${kw.flow==='流入'?'flow-in':kw.flow==='流出'?'flow-out':'neutral'}">${esc(kw.flow)}</span>
        <span class="badge ${riskBadge(kw.risk)}">风险${esc(kw.risk)}</span>
        <span class="badge neutral">置信度${esc(kw.confidence)}</span>
        <span class="badge neutral">爆发${kw.burst}</span>
        <span class="badge neutral">分歧${kw.divergence}</span>
      </div>
      <div class="drawer-reading">${esc(kw.reading)}</div>
      <div style="margin:8px 0 4px;font-size:11px;font-weight:700;">港股 ETF 双标的</div>
      ${etfPairHtml}
      <div style="margin-bottom:6px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;"><span style="font-size:11px;font-weight:700;">关联政策维度：</span> ${policyTags}</div>
      <div style="margin-bottom:4px;font-size:11px;font-weight:700;">投资提示：<span style="font-weight:400;color:var(--ink-soft);">${esc(kw.hint)}</span></div>
      <div style="margin:8px 0 4px;font-size:10px;color:var(--ink-soft);">线索列表（标题/来源/时间/情绪标签）不展示；完整数据见 /api/heatmap 接口。仅统计信号，不构成投资建议。</div>
      <div style="margin-top:6px;color:var(--ink-soft);font-size:10px;">指标：热度 ${kw.heat}（${kw.heat_level}）= 提及${kw.mentions}×(1+ln(${kw.sources}+1))归一；情绪 ${kw.sentiment>0?'+':''}${kw.sentiment}（${kw.sentiment_label}）；爆发 ${kw.burst}=热度×|情绪|；分歧 ${kw.divergence}；资金倾向 ${kw.flow}。仅统计信号，不构成投资建议。</div>
    `;
    drawerEl.querySelector('.drawer-close').onclick=closeDrawer;
    drawerEl.scrollIntoView({behavior:'smooth',block:'nearest'});
  }

  function closeDrawer(){
    if(!drawerEl) return;
    drawerEl.hidden=true;
    drawerEl.innerHTML='';
    gridEl.querySelectorAll('.heatmap-cell').forEach(el=>el.classList.remove('selected'));
  }

  function filterCells(filter){
    activeFilter=filter;
    if(filtersEl){
      filtersEl.querySelectorAll('[data-filter]').forEach(btn=>{
        const on=btn.dataset.filter===filter;
        btn.classList.toggle('active',on);
        btn.setAttribute('aria-pressed', on?'true':'false');
      });
    }
    gridEl.querySelectorAll('.heatmap-cell').forEach(cell=>{
      const kw = JSON.parse(cell.dataset.payload||'{}');
      let show=true;
      if(filter==='all') show=true;
      else if(filter==='机会') show=(kw.rating||'').includes('机会');
      else if(filter==='承压') show=(kw.rating||'').includes('回避') || (kw.rating||'').includes('谨慎');
      else if(filter==='偏多') show=kw.sentiment_label==='偏多';
      else if(filter==='偏空') show=kw.sentiment_label==='偏空';
      else if(filter==='沸腾') show=(kw.heat||0)>=75;
      else show=true;
      cell.hidden=!show;
    });
    if(countEl && heatmapData){
      const visible=gridEl.querySelectorAll('.heatmap-cell:not([hidden])').length;
      const total=(heatmapData.keywords||[]).length;
      countEl.textContent= filter==='all' ? `${total} 个关键词` : `${visible} / ${total}`;
    }
    // if drawer open and its keyword now hidden, close it
    if(drawerEl && !drawerEl.hidden){
      const selected=gridEl.querySelector('.heatmap-cell.selected');
      if(selected && selected.hidden) closeDrawer();
    }
  }

  function renderGrid(data){
    if(!gridEl) return;
    const keywords=data.keywords||[];
    if(!keywords.length){
      gridEl.innerHTML=`<div class="heatmap-empty">当日样本中暂无关键词命中（投资主线词库均未命中），热力图等待数据刷新，不做无依据推演。</div>`;
      if(countEl) countEl.textContent='0 个关键词';
      return;
    }
    gridEl.innerHTML=keywords.map(kw=>{
      const sCls=kw.sentiment_label==='偏多'?'bull':kw.sentiment_label==='偏空'?'press':'neutral';
      const heatLabel=kw.heat_level;
      const badgeHot=kw.heat>=75?'hot':kw.heat>=50?'hot':'neutral';
      const badgeRating=ratingBadge(kw.rating);
      const flowCls=kw.flow==='流入'?'flow-in':kw.flow==='流出'?'flow-out':'neutral';
      const barCls=barClass(kw.sentiment);
      const etfPair=Array.isArray(kw.etf_pair)?kw.etf_pair:[];
      const etfLine=etfPair.length===2
        ? `<div class="cell-etf"><span>ETF</span> <b>${esc(etfPair[0].code.replace('.HK',''))}</b> · <b>${esc(etfPair[1].code.replace('.HK',''))}</b>${kw.etf_counters_text?'<em>多柜台</em>':''}</div>`
        : '';
      // store payload for filtering & drawer (stringified)
      const payload=JSON.stringify({keyword:kw.keyword,sentiment_label:kw.sentiment_label,heat:kw.heat,rating:kw.rating});
      return `
        <button class="heatmap-cell ${sCls}" data-keyword="${esc(kw.keyword)}" data-payload='${payload.replace(/'/g,"&#39;")}' aria-label="${esc(kw.keyword)} 热度${kw.heat} 情绪${kw.sentiment_label}">
          <div class="cell-top">
            <span class="cell-kw">${esc(kw.keyword)}</span>
            <span class="cell-tag">${esc(kw.tag)}</span>
          </div>
          <div class="heat-bar" title="热度 ${kw.heat}（${heatLabel}）"><i class="${barCls}" style="width:${kw.heat}%"></i></div>
          <div class="cell-stats">
            <span><b>${esc(heatLabel)}</b>·热度<b>${kw.heat}</b></span>
            <span>情绪<b>${esc(kw.sentiment_label)}</b>(${kw.net>0?'+':''}${kw.net})</span>
            <span>${kw.sources}源·${kw.mentions}条</span>
            <span>信号 ${kw.bull}:${kw.bear}</span>
          </div>
          <div class="cell-badges">
            <span class="badge ${badgeHot}">${esc(heatLabel)}</span>
            <span class="badge ${badgeRating}">${esc(kw.rating)}</span>
            <span class="badge ${flowCls}">${esc(kw.flow)}</span>
            <span class="badge ${riskBadge(kw.risk)}">风险${esc(kw.risk)}</span>
          </div>
          ${etfLine}
          <div class="cell-reading" title="${esc(kw.reading)}">${esc(kw.reading)}</div>
        </button>`;
    }).join('');
    // attach full data for drawer lookup via map
    const map={};
    (data.keywords||[]).forEach(k=>{ map[k.keyword]=k; });
    gridEl.querySelectorAll('.heatmap-cell').forEach(btn=>{
      btn.addEventListener('click',()=>{
        const kw=map[btn.dataset.keyword];
        if(kw) openDrawer(kw);
      });
    });
    // also need to store full data for filter details: enrich dataset with heatmapData lookup
    gridEl.querySelectorAll('.heatmap-cell').forEach(cell=>{
      const kw=map[cell.dataset.keyword];
      if(kw) cell.dataset.payload=JSON.stringify({keyword:kw.keyword,sentiment_label:kw.sentiment_label,heat:kw.heat,rating:kw.rating});
    });
  }

  function renderHeatmap(data){
    heatmapData=data;
    if(headlineEl) headlineEl.textContent=data.headline||'—';
    // if headline contains keyword names, highlight? keep plain for now, but escape already
    renderMeta(data);
    renderEtfPool(data.new_listings);
    renderGrid(data);
    renderIndicators(data.indicators);
    if(footEl){
      footEl.textContent=data.note || '指标口径：热度=提及×(1+ln(跨源+1))归一；情绪=(多-空)/信号；爆发=热度×|情绪|；仅统计信号，不构成投资建议。';
    }
    filterCells(activeFilter);
  }

  async function loadHeatmap(){
    if(headlineEl) headlineEl.textContent='正在聚合新闻并计算情绪热力…';
    if(gridEl) gridEl.innerHTML='<div class="heatmap-empty">加载中…</div>';
    try{
      // 优先走专用接口，失败则退回 /api/brief 里的 heatmap
      let data=null;
      try{
        const res=await fetch('api/heatmap?top=40');
        if(res.ok){ data=await res.json(); }
      }catch(e){ /* ignore */ }
      if(!data || data.error){
        const res2=await fetch('api/brief');
        if(res2.ok){
          const brief=await res2.json();
          // try to compute via brief if server already includes heatmap in analyze_brief? but we need analyze
          // fallback: fetch /api/policy?deep=0? no
          // if brief is dict of sources, we can try local? Instead request heatmap again with error handling
          // simplest: if data missing, show empty
          if(!data) data={keywords:[],total_hit:0,headline:'暂无数据',indicators:[]};
        }
      }
      if(data && !data.error){
        renderHeatmap(data);
      }else{
        throw new Error(data && data.error || 'empty');
      }
    }catch(e){
      console.warn('heatmap load failed',e);
      if(headlineEl) headlineEl.textContent='热力图加载失败，请稍后刷新重试（接口暂不可用）';
      if(gridEl) gridEl.innerHTML='<div class="heatmap-empty">接口暂不可用，已展示静态预览数据。请启动 server.py 后刷新。</div>';
      // still render indicators for preview
      renderIndicators([
        {label:'热度',formula:'提及×(1+ln(跨源+1))归一0-100',use:'关注度；>75沸腾需防拥挤回落'},
        {label:'情绪分',formula:'(多-空)/信号 -1…+1',use:'方向与强度；|0.6|以上为强一致'},
        {label:'净信号',formula:'多-空',use:'绝对强度；|≥3|为强信号'},
        {label:'跨源/共振',formula:'命中源数',use:'可信度；≥4源为确认，1-2源为噪音'},
        {label:'分歧度',formula:'1-|情绪分|',use:'撕裂度；高分歧易波动，低分歧趋势稳'},
        {label:'爆发系数',formula:'热度×|情绪分|',use:'主线爆发点；高分适合短线跟踪但忌追高'},
        {label:'资金倾向',formula:'情绪×热度',use:'流入/流出/观望；判断跟随或回避'},
        {label:'投资评级',formula:'热度+情绪综合',use:'强多·机会/偏多·关注/观望/偏空·谨慎/强空·回避'},
        {label:'风险等级',formula:'负面情绪+分歧',use:'低/中/高；高风险需控仓位'},
      ]);
      if(metaEl) metaEl.innerHTML='<span class="meta-tag neutral">离线预览</span><span>静态演示数据</span>';
    }
  }

  // filters
  if(filtersEl){
    filtersEl.querySelectorAll('[data-filter]').forEach(btn=>{
      btn.addEventListener('click',()=>filterCells(btn.dataset.filter));
    });
  }

  // drawer close on Esc
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape' && drawerEl && !drawerEl.hidden) closeDrawer();
  });

  // initial load
  loadHeatmap();

  // refresh button also reloads heatmap
  if(refreshBtn){
    const orig=refreshBtn.onclick;
    refreshBtn.addEventListener('click',()=>{
      loadHeatmap();
      if(orig) try{orig();}catch(e){}
    });
  } else if(refreshBtn===null){
    // if element missing, still provide global reload
  }

  // expose for debugging
  window._heatmapReload=loadHeatmap;
  window._heatmapClose=closeDrawer;
})();

/* AI 政策深度：近 30 日全球政策发布对 A股 / 港股 / 美股的影响。
   静态页是离线预览；server.py 可用时用 /api/policy?deep=0 覆盖，不跑 LSTM。 */
(function(){
  const headlineEl=document.getElementById('policyDeepHeadline');
  const boardEl=document.getElementById('policyDeepBoard');
  const listEl=document.getElementById('policyDeepList');
  const footEl=document.getElementById('policyDeepFoot');
  const pillEl=document.getElementById('policyDeepPill');
  if(!headlineEl || !boardEl || !listEl) return;

  function esc(s){ return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function biasCls(bias){
    const text=String(bias||'');
    if(text.includes('空')) return 'press';
    if(text.includes('多')) return 'hot';
    return 'neutral';
  }
  function chip(text, press){
    return press ? `<mark class="press">${esc(text)}</mark>` : `<mark>${esc(text)}</mark>`;
  }

  const STATE_NOTE={
    empty:'窗口内无新发文，也无存量政策，不外推',
    carry:'仅存量政策仍生效，按半权计入'
  };
  function span(days){
    const n=Math.max(0, Number(days)||0);
    return n<90 ? `${n} 天` : `约 ${Math.max(1, Math.round(n/30))} 个月`;
  }

  function render(impact){
    if(!impact || !impact.headline) return;
    let headline=esc(impact.headline);
    const shorts=[...(impact.pressure_events||[]), ...(impact.support_events||[])]
      .map(item=>item.short).filter(Boolean)
      .sort((a,b)=>b.length-a.length);
    shorts.forEach(short=>{
      const safe=esc(short);
      const press=(impact.pressure_events||[]).some(item=>item.short===short);
      headline=headline.replace(safe, chip(short, press));
    });
    headlineEl.innerHTML=headline;

    boardEl.innerHTML=(impact.markets||[]).map(market=>{
      const cls=biasCls(market.bias);
      const note=STATE_NOTE[market.state] || (market.observed ? '含已观测收盘反应' : '机制推演，不外推点位');
      const split=market.state!=='empty'
        ? `（新发文 ${Number(market.score_window||0).toFixed(2)} / 存量 ${Number(market.score_carry||0).toFixed(2)}）`
        : '';
      const support=(market.support||[]).join('、') || '—';
      const pressure=(market.pressure||[]).join('、') || '—';
      const cellCls=cls==='press' ? 'press' : (cls==='hot' ? '' : 'neutral');
      return `<div class="impact-cell ${cellCls}">
        <div class="impact-name">${esc(market.name)}，指数分 ${Number(market.score).toFixed(2)}${esc(split)}</div>
        <div class="impact-bias">${esc(market.bias)}</div>
        <div class="impact-score">${esc(note)}</div>
        <div class="impact-split"><b>受益</b> ${esc(support)}<br><b>承压</b> ${esc(pressure)}</div>
      </div>`;
    }).join('');

    const themes=(impact.themes||[]).map(item=>{
      const press=item.direction==='压制';
      const src=[item.window?`新 ${item.window}`:'', item.carry?`存量 ${item.carry}`:''].filter(Boolean).join(' / ');
      return `<li><span class="point-no">主</span><span class="point-label ${press?'press':''}">${esc(item.theme)}</span>`
        + `<span>${chip(item.direction||'', press)}<span class="sector-stat">${esc(src)}，合成分 ${Number(item.net||0).toFixed(2)}</span>`
        + ((item.shorts||[]).length ? `<ul class="sector-evidence"><li>· ${esc(item.shorts.join('、'))}</li></ul>` : '')
        + `</span></li>`;
    }).join('');

    const releases=(impact.releases||[]).map(item=>{
      const press=item.stance==='偏鹰';
      return `<li><span class="point-no">${esc((item.date||'').slice(5))}</span><span class="point-label ${press?'press':''}">${esc(item.topic||item.kind)}</span><span>${chip((item.stance||'')+' · '+(item.kind||''), press)}<span class="sector-stat">${esc(item.issuer)} · ${esc(item.horizon||'')}</span>
        <ul class="sector-evidence"><li>· ${esc(item.title)}</li><li>· ${esc(item.stock_line)}</li></ul></span></li>`;
    }).join('');

    const carry=(impact.carry_releases||[]).map(item=>{
      const press=item.stance==='偏鹰';
      return `<li><span class="point-no">${esc((item.date||'').slice(5))}</span><span class="point-label ${press?'press':''}">${esc(item.topic||item.kind)}</span><span>${chip((item.stance||'')+' · '+(item.kind||''), press)}<span class="sector-stat">已过 ${span(item.days_ago)}，影响期至 ${esc(item.active_until||'')}（剩 ${span(item.remaining_days)}），${esc(item.issuer)}</span>
        <ul class="sector-evidence"><li>· ${esc(item.short||item.title)}</li><li>· ${esc(item.stock_line)}</li></ul></span></li>`;
    }).join('');

    const board=impact.sector_board||{};
    const support=(board.support||[]).map(row=>row.name).join('、') || '—';
    const pressure=(board.pressure||[]).map(row=>row.name).join('、') || '—';
    const counts=impact.counts||{};
    const official=impact.official||{};
    const autoRows=(impact.auto_releases||[]).map(item=>(
      `<li><span class="point-no">${esc((item.date||'').slice(5))}</span><span class="point-label">${esc(item.issuer)}</span>`
      + `<span>${chip('官方原文')}<span class="sector-stat">${esc(item.dimension||'')}，已过 ${Number(item.days_ago)||0} 天</span>`
      + `<ul class="sector-evidence"><li>· ${esc(item.title)}</li></ul></span></li>`
    )).join('');
    const candidates=(impact.official_candidates||[]).slice(0,4).map(item=>(
      `<li><span class="point-no">·</span><span>${esc(item.source||'')}｜${esc(item.title||'')}</span></li>`
    )).join('');

    listEl.innerHTML=`<li><span class="point-no">源</span><span>${esc(official.display||'')}</span></li>`
      + ((official.failed||[]).length ? `<li><span class="point-no">断</span><span>${esc((official.failed||[]).join('、'))}</span></li>` : '')
      + (themes
        ? `<li class="group-row"><span class="group-label">主线归并</span><span class="group-note" data-meta="1">同主题合并，存量按半权，三市合成分</span></li>` + themes
        : '')
      + `<li class="group-row"><span class="group-label">窗口内政策发布</span><span class="group-note">${esc(impact.window_label||'')}，新发文 ${counts.in_window||0} 项，存量生效 ${counts.carry||0} 项，新的在前</span></li>`
      + (releases || `<li><span class="point-no">·</span><span>窗口内没有已核验发文，未核实日期的快讯不编造成政策发布。</span></li>`)
      + (carry
        ? `<li class="group-row"><span class="group-label">存量政策仍在生效</span><span class="group-note" data-meta="1">${(impact.carry_releases||[]).length} 项，发文早于窗口、影响期未走完，按半权计入</span></li>` + carry
        : '')
      + (autoRows
        ? `<li class="group-row"><span class="group-label">官方新发文（自动抓取）</span><span class="group-note" data-meta="1">${(impact.auto_releases||[]).length} 条 · 官方原文 · 影响方向待标注，不计入指数分</span></li>` + autoRows
        : '')
      + (candidates
        ? `<li class="group-row"><span class="group-label">当日官方源动态</span><span class="group-note" data-meta="1">${(impact.official_candidates||[]).length} 条 · 待核验发文日，不入台账</span></li>` + candidates
        : '')
      + ((impact.lapsed_releases||[]).length
        ? `<li class="group-row"><span class="group-label">窗口内已到期</span><span class="group-note" data-meta="1">窗口前段生效、现已过期，不计分</span></li>`
          + `<li><span class="point-no">·</span><span>${esc((impact.lapsed_releases||[]).slice(0,4).map(item=>`${(item.date||'').slice(5)} ${item.short||''}`).join('、'))}</span></li>`
        : '')
      + `<li class="group-row"><span class="group-label">板块传导</span><span class="group-note" data-meta="1">跨政策加总，含存量半权，不是指数点位</span></li>`
      + `<li><span class="point-no">＋</span><span class="point-label">受益</span><span>${esc(support)}</span></li>`
      + `<li><span class="point-no">－</span><span class="point-label press">承压</span><span>${esc(pressure)}</span></li>`;
    if(footEl){
      footEl.innerHTML=`${esc(impact.gap_note||'')} 指数分可复算，仅供参考，不构成投资建议。实时结果见 <code>/api/policy</code>`;
    }
    if(pillEl){
      const us=(impact.markets||[]).find(market=>market.name==='美股');
      pillEl.textContent=`近${impact.days||30}日，新发文 ${counts.in_window||0}，存量 ${counts.carry||0}`
        + (us ? `，美股${us.bias}` : '');
    }
  }

  async function loadPolicyDeep(){
    try{
      const res=await fetch('api/policy?deep=0');
      if(!res.ok) return;
      const data=await res.json();
      if(data && data.market_impact && !data.error) render(data.market_impact);
    }catch(e){ /* 离线或静态托管：保留页面里的 30 日预览 */ }
  }
  loadPolicyDeep();
  if(typeof refreshBtn!=='undefined' && refreshBtn){
    refreshBtn.addEventListener('click', loadPolicyDeep);
  }
  window._policyDeepReload=loadPolicyDeep;
})();

/* ---- 大 V 分析 · 「预测历史」框架启发的 AI 仿推演 ----
   静态页保留 2026-10-26 快照；server.py 可用时用 /api/big-v 按当日快讯重算。
   所有动态内容都转义；不把外部新闻标题当作 HTML 插入。 */
(function(){
  const headlineEl=document.getElementById('bigVHeadline');
  const metaEl=document.getElementById('bigVMeta');
  const stepsEl=document.getElementById('bigVSteps');
  const domainsEl=document.getElementById('bigVDomains');
  const scenariosEl=document.getElementById('bigVScenarios');
  const watchEl=document.getElementById('bigVWatch');
  const refsEl=document.getElementById('bigVRefs');
  const footEl=document.getElementById('bigVFoot');
  const pillEl=document.getElementById('bigVPill');
  if(!headlineEl || !metaEl || !stepsEl || !domainsEl || !scenariosEl) return;

  function esc(s){
    return String(s==null?'':s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function trunc(s,n){ s=String(s==null?'':s); return s.length>n ? s.slice(0,n-1)+'…' : s; }
  function safeHref(url){
    try{
      const u=new URL(String(url||''),location.href);
      return (u.protocol==='http:' || u.protocol==='https:') ? esc(u.href) : '#';
    }catch(e){ return '#'; }
  }
  function arr(value){ return Array.isArray(value)?value:[]; }

  /* 采集明细：逐字段说明这次日报的数字从哪来、哪些是代理、哪些没取到。
     数据源故障与缺口写在报告里，不让读者自己猜。 */
  function renderProvenance(data){
    const el=document.getElementById('quantProvenance');
    if(!el) return;
    const rows=arr(data.provenance);
    const stats=data.provenance_stats||{};
    if(!rows.length){
      el.innerHTML='<summary>采集明细（本次未返回）</summary>';
      return;
    }
    const head=`采集明细 · 实测 ${Number(stats.live||0)} / 代理 ${Number(stats.proxy||0)} / 未取得 ${Number(stats.missing||0)}（点击展开）`;
    const body=rows.map(r=>(
      `<tr><td>${esc(r.field)}</td><td>${provMark(r.status)}</td>`
      + `<td>${esc(r.source||'')}${r.detail?`<small>${esc(r.detail)}</small>`:''}</td></tr>`
    )).join('');
    el.innerHTML=`<summary>${esc(head)}</summary>`
      + '<table class="quant-action-table"><tr><th>字段</th><th>状态</th><th>来源 / 缺口说明</th></tr>'
      + body + '</table>';
  }

  function render(data){
    if(!data || data.error) return;
    const stamp=esc(data.as_of||'今日');
    headlineEl.innerHTML=`<mark>${stamp} AI 推演</mark>${esc(data.headline||'')}`;
    const counts=data.counts||{};
    metaEl.innerHTML=`<span class="meta-tag">${esc(data.risk_label||'框架推演')}</span>`
      + arr(data.themes).slice(0,3).map(t=>`<span class="meta-tag neutral">${esc(t)}</span>`).join('')
      + `<span>当日 ${Number(counts.sources||0)} 源 / ${Number(counts.mentions||0)} 条相关标题 · ${esc(data.status||'')}</span>`;

    stepsEl.innerHTML=arr(data.framework_steps).map(step=>(
      `<div class="bigv-step"><span>${esc(step.no||'·')}</span><b>${esc(step.label||'')}</b>`
      + `<small>${esc(step.question||step.output||'')}</small></div>`
    )).join('');

    const domainRows=arr(data.domains);
    domainsEl.innerHTML=domainRows.length ? domainRows.map((d,i)=>{
      const chain=arr(d.chain).map(x=>`<div class="bigv-chain">${esc(x)}</div>`).join('');
      const evidence=arr(d.evidence).map(ev=>(
        `<li>· <span class="ev-src">${esc(ev.source||'')}</span>｜${esc(trunc(ev.title,70))}</li>`
      )).join('');
      return `<article class="bigv-domain">`
        + `<div class="bigv-domain-head"><span class="point-no">0${i+1}</span><b>${esc(d.label||'')}</b>`
        + `<em>${esc((d.focus||[]).join(' · ')||'跨领域推演')}</em></div>`
        + chain
        + `<p>${esc(d.conclusion||'')}</p>`
        + `<ul><li><b>激励：</b>${esc(d.incentive||'')}</li>`
        + `<li><b>约束：</b>${esc(d.constraints||'')}</li></ul>`
        + `<ul class="sector-evidence">${evidence||'<li>· 当日样本暂无该领域证据</li>'}</ul>`
        + `</article>`;
    }).join('') : '<article class="bigv-domain"><p>当日样本中没有可用于推演的政治 / 地缘 / 经济证据。</p></article>';

    scenariosEl.innerHTML=arr(data.scenarios).map(s=>(
      `<div class="bigv-scenario ${s.key==='escalation'?'press':''}">`
      + `<div class="bigv-scenario-head"><b>${esc(s.label||'')}</b><span>关注度 ${Number(s.weight||0)}%</span></div>`
      + `<p><b>触发：</b>${esc(s.trigger||'')}</p><p>${esc(s.chain||'')}</p>`
      + `<small>观察：${esc(arr(s.watch).join(' · '))}</small></div>`
    )).join('');

    if(watchEl){
      watchEl.innerHTML=`<b>推演必须被证伪的信号</b><ul>`
        + arr(data.falsifiers).map(x=>`<li>${esc(x)}</li>`).join('')
        + `</ul>`;
    }
    if(refsEl){
      refsEl.innerHTML=`<b>框架与近期事实依据</b>`
        + arr(data.references).map(ref=>(
          `<a href="${safeHref(ref.url)}" target="_blank" rel="noopener noreferrer">${esc(ref.title||'资料')}</a>`
        )).join(' · ');
    }
    if(footEl){
      footEl.innerHTML=`<span>${esc(data.disclaimer||'')} ${esc(data.boundary||'')} `
        + `${esc(data.scenario_weights_note||'')} 实时结果见 <code>/api/big-v</code>。</span>`;
    }
    if(pillEl) pillEl.textContent=`${esc(data.name||'预测历史')} · ${esc(data.risk_label||'框架推演')}`;
  }

  async function loadBigV(){
    try{
      const res=await fetch('api/big-v');
      if(!res.ok) return;
      const data=await res.json();
      if(data && !data.error) render(data);
    }catch(e){ /* 静态托管 / 离线：保留页面里的 2026-10-26 快照 */ }
  }
  loadBigV();
  if(typeof refreshBtn!=='undefined' && refreshBtn){
    refreshBtn.addEventListener('click', loadBigV);
  }
  window._bigVReload=loadBigV;
})();

/* ---- AI 独立开发雷达（方法来自 github.com/susumr/radar-dashboard）
   19 个独立开发 / 产品信号源 → 7 模块 + 心法。静态页保留当日快照，
   server.py 可用时用 /api/radar 重算；某个模块当天没有素材就不渲染，不占位。 */
(function(){
  const wrap=document.getElementById('radarModules');
  const summaryEl=document.getElementById('radarSummary');
  const wisdomEl=document.getElementById('radarWisdom');
  const pillEl=document.getElementById('radarPill');
  if(!wrap) return;

  const MODULES=[
    {key:'opportunities',no:'1',label:'今日机会'},
    {key:'money_cases',no:'2',label:'今日赚钱案例'},
    {key:'ideas',no:'3',label:'今日产品灵感'},
    {key:'growth',no:'4',label:'今日增长技巧'},
    {key:'tools',no:'5',label:'今日工具'},
    {key:'pitfalls',no:'6',label:'今日踩坑'},
    {key:'data_signals',no:'7',label:'今日数据信号'}
  ];
  function esc(s){
    return String(s==null?'':s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function safeHref(url){
    try{
      const u=new URL(String(url||''),location.href);
      return (u.protocol==='http:'||u.protocol==='https:') ? esc(u.href) : '#';
    }catch(e){ return '#'; }
  }
  function arr(v){ return Array.isArray(v)?v:[]; }

  function itemHtml(module, entry){
    if(module.key==='data_signals'){
      return `<li class="radar-item"><b>${esc(entry.observation||'')}</b>`
        + (entry.judgment?`<span class="radar-judgment">${esc(entry.judgment)}</span>`:'')
        + '</li>';
    }
    const heat=entry.points?`，热度 ${esc(entry.points)}`:'';
    return `<li class="radar-item"><a href="${safeHref(entry.url)}" target="_blank" rel="noopener noreferrer">`
      + `${esc(entry.title||'')}</a><span class="radar-src">${esc(entry.source||'')}${heat}</span></li>`;
  }

  function render(data){
    if(!data || data.error) return;
    const modules=data.modules||{};
    const html=MODULES.map(module=>{
      const entries=arr(modules[module.key]);
      if(!entries.length) return '';
      return `<div class="radar-module"><h3><span class="radar-no">${module.no}</span>${esc(module.label)}</h3>`
        + `<ul class="radar-items">${entries.map(entry=>itemHtml(module,entry)).join('')}</ul></div>`;
    }).join('');
    if(html) wrap.innerHTML=html;
    if(summaryEl && data.headline){
      summaryEl.innerHTML=`<mark>${esc(data.as_of||'')}</mark>${esc(data.headline)}`;
    }
    const wisdom=data.wisdom||{};
    if(wisdomEl && wisdom.status==='live'){
      wisdomEl.innerHTML=`<b>今日心法：${esc(wisdom.method||'')}</b>`
        + `<p>${esc(wisdom.scenario||'')}</p>`
        + `<ol>${arr(wisdom.steps).map(step=>`<li>${esc(step)}</li>`).join('')}</ol>`;
    }
    const counts=data.counts||{};
    if(pillEl){
      pillEl.textContent=`${Number(counts.items||0)} 条信号、${Number(counts.sources_ok||0)}`
        + `/${Number(counts.sources_total||0)} 源有数据、${Number(counts.modules_filled||0)} 个模块有料`;
    }
  }

  async function load(){
    if(typeof isGithubPages!=='undefined' && isGithubPages) return;
    try{
      const res=await fetch('api/radar');
      if(!res.ok) return;
      const data=await res.json();
      if(data && !data.error) render(data);
    }catch(e){ /* 静态托管 / 离线：保留页面里的当日快照 */ }
  }
  load();
  if(typeof refreshBtn!=='undefined' && refreshBtn){
    refreshBtn.addEventListener('click', load);
  }
  window._radarReload=load;
})();

/* ---- DeepSeek 全网搜索（2026-10-17 Search API）----
   表单走相对路径 api/search（浏览器不直连 api.deepseek.com，Key 只在服务端）。 */
(function(){
  const form=document.getElementById('dsSearchForm');
  const input=document.getElementById('dsSearchInput');
  const btn=document.getElementById('dsSearchBtn');
  const state=document.getElementById('dsSearchState');
  const list=document.getElementById('dsSearchResults');
  const pill=document.getElementById('deepseekSearchPill');
  if(!form || !input || !btn || !state || !list) return;

  function esc(s){ return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

  async function loadStatus(){
    try{
      const res=await fetch('api/deepseek/status');
      if(!res.ok) return;
      const data=await res.json();
      if(pill) pill.textContent=data.enabled ? 'DeepSeek 已启用' : '未配置走 RSS 回退';
      if(data.enabled){
        state.textContent=`DeepSeek Search 已启用（${data.search_backed_sources?data.search_backed_sources.length:0} 个搜索型数据源同后端）· 每个查询最多 128 字符，结果数默认 10`;
      }
    }catch(e){ /* 静态托管/离线：保留默认文案 */ }
  }

  async function doSearch(q){
    btn.disabled=true;
    state.textContent='搜索中…';
    list.innerHTML='';
    try{
      const res=await fetch('api/search?q='+encodeURIComponent(q)+'&count=10');
      const data=await res.json().catch(()=>({}));
      if(!res.ok){
        state.textContent=data.message || `搜索失败（HTTP ${res.status}）`;
        return;
      }
      const rows=data.results||[];
      state.textContent=rows.length
        ? `找到 ${rows.length} 条结果${data.cached?'（命中 5 分钟缓存，不重复计费）':''}`
        : '没有匹配结果（空结果不计费）。';
      list.innerHTML=rows.map(item=>{
        const meta=[
          item.site_name?esc(item.site_name):'',
          item.date?esc(String(item.date).slice(0,10)):'',
          (item.rerank_score!=null)?`相关度 ${Number(item.rerank_score).toFixed(1)}/3`:'',
          (item.authority!=null && item.authority>=0)?`权威度 ${item.authority}/4`:'',
        ].filter(Boolean).join(' · ');
        const href=item.url?esc(item.url):'#';
        return `<li><a href="${href}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a>`
          + (meta?`<div class="ds-result-meta">${meta}</div>`:'')
          + (item.snippet?`<div class="ds-result-snippet">${esc(item.snippet)}</div>`:'')
          + `</li>`;
      }).join('');
    }catch(e){
      state.textContent='搜索请求失败：请用 server.py 启动服务（静态托管没有 /api/search）。';
    }finally{
      btn.disabled=false;
    }
  }

  form.addEventListener('submit', function(ev){
    ev.preventDefault();
    const q=input.value.trim();
    if(!q){ state.textContent='先输入搜索关键词（最多 128 字符，支持 site: / lang: / after: 算子）。'; return; }
    doSearch(q);
  });
  loadStatus();
})();

/* ===================== 推送前 · 去 AI 味体检（Hallmark 静态闸门） =====================
   数据源：GET /api/hallmark（或 /api/deai）。GitHub Pages 纯静态托管时没有后端，
   保留 index.html 里的静态快照文案，不发送任何请求。 */
(function(){
  const section=document.getElementById('deaiSection');
  if(!section) return;
  const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const $=id=>document.getElementById(id);
  const modeSel=$('deaiMode');

  // 记住用户选择的模式，并在手动运行时随 /api/run 一起提交。
  try{
    const saved=localStorage.getItem('deai.mode');
    if(saved && ['polish','audit','off'].includes(saved)) modeSel.value=saved;
  }catch(e){/* 隐私模式下 localStorage 不可用 */}
  modeSel.addEventListener('change',()=>{
    try{ localStorage.setItem('deai.mode', modeSel.value); }catch(e){}
  });
  window.getDeaiMode=()=>modeSel ? modeSel.value : 'polish';


  /* 改写对照：把「前 → 后」摊开给读者看；没自动改的逐条给出原因。
     只做展示，不在前端补数：报告里没有的就写没有。 */
  function renderDiffs(diffId, blockedId, blockedWrapId, reports){
    const diffList=$(diffId), blockedList=$(blockedId), blockedWrap=$(blockedWrapId);
    const diffs=[], blocked=[];
    for(const r of reports){
      if(!r) continue;
      for(const d of (r.diffs||[])) diffs.push(d);
      for(const b of (r.blocked||[])) blocked.push(b);
    }
    if(diffList){
      diffList.innerHTML=diffs.length
        ? diffs.slice(0,8).map(d=>
            `<li><span class="diff-gate">gate ${esc(d.gate)}</span>`+
            `<span><span class="diff-before">${esc(d.before)}</span>`+
            `<span class="diff-arrow">→</span>`+
            `<span class="diff-after">${esc(d.after)}</span></span></li>`).join('')
        : '<li><span class="diff-gate">本轮无自动改写</span>'
          + '<span class="diff-why">命中的都是需要结构 / 语义判断的项，见下方原因；这不是「没跑」。'+
          '</span></li>';
    }
    if(blockedList && blockedWrap){
      blockedWrap.hidden = !blocked.length;
      blockedList.innerHTML=blocked.map(b=>
        `<li><span class="diff-gate">gate ${esc(b.gate)} · ${esc(b.name||'')} ×${Number(b.count)||0}</span>`+
        `<span class="diff-why">${esc(b.reason||'')}</span></li>`).join('');
    }
  }
  // 文本层面板在另一个 IIFE 里，共用这一个渲染函数，别复制一份逻辑。
  window.renderDeaiDiffs = renderDiffs;

  function renderFindings(report){
    const push=report.surfaces?.push||null, page=report.surfaces?.page||null;
    $('deaiScoreBefore').textContent=report.overall_score_before ?? '—';
    $('deaiScoreAfter').textContent=report.overall_score_after ?? '—';
    const d=(report.overall_score_after??0)-(report.overall_score_before??0);
    const delta=$('deaiScoreDelta');
    delta.textContent=(d>0?`+${d}`:String(d))+' 分 · 启发式体检分，非 AIGC 概率';
    if(d>0) delta.style.color='var(--up)';

    for(const [key,scoreEl,stateEl,label] of [
      ['push','deaiPushScore','deaiPushState','推送 HTML'],
      ['page','deaiPageScore','deaiPageState','管理页'],
    ]){
      const r=key==='push'?push:page;
      if(!r){ continue; }
      $(scoreEl).textContent=`${r.score_before} → ${r.score_after}`;
      const a=r.after||{}, c=a.counts||{};
      $(stateEl).textContent=`${label}：${a.findings?.length||0} 条 gate 命中（自动可修 ${c.auto_fixable||0} / 需人工 ${c.manual||0}）；`+
        `本次改写 ${r.fixed_count ?? r.fixes?.filter(f=>f.gate!=='fact-lock').length ?? 0} 处。` +
        (r.changed?'已生成改写预览。':'无需改写或已是 audit 结果。');
    }

    // 合并两面命中，Floor 优先，其次按命中数降序。
    const merged=[];
    for(const s of [push,page]){
      const tag=s===push?'推送':'页面';
      for(const f of (s?.before?.findings||[])){
        merged.push({...f, surface:tag});
      }
    }
    const rank={Floor:0,Reflex:1,Finish:2};
    merged.sort((a,b)=>(rank[a.tier]??3)-(rank[b.tier]??3) || b.count-a.count);
    const list=$('deaiFindings');
    if(!merged.length){
      list.innerHTML='<li><span class="deai-finding-tier">clean</span>两个面均未命中静态 gate。</li>';
    }else{
      list.innerHTML=merged.slice(0,14).map(f=>
        `<li><span class="deai-finding-tier ${esc(f.tier.toLowerCase())}">${esc(f.tier)}</span>`+
        `<span class="deai-finding-gate">gate ${esc(f.gate)} · ${esc(f.surface)}</span>`+
        `<b>${esc(f.name)} ×${Number(f.count)||0}</b>`+
        `<span class="deai-finding-auto">${f.auto?'自动可修':'需人工判断'} · ${esc((f.fix||'').slice(0,54))}</span></li>`
      ).join('');
    }

    renderDiffs('deaiDiffs', 'deaiBlocked', 'deaiBlockedWrap', [push,page]);

    const waivers=report.waivers||[];
    if(waivers.length){
      $('deaiWaivers').innerHTML=waivers.map(w=>
        `<li><b>${esc(w.item)}</b>（gate ${esc(w.gate)}）：${esc(w.reason)}</li>`).join('');
    }
    let boundary=report.boundary||'';
    const residual=[];
    for(const s of [push,page]){ const n=s?.after?.residual_note; if(n && !residual.includes(n)) residual.push(n); }
    if(residual.length) boundary+=' 【残留说明】'+residual.join(' ');
    if(report.limits) boundary+=' '+report.limits;
    if(boundary) $('deaiBoundary').textContent=boundary;
    const cfg=report.config||{};
    if(cfg.mode) $('deaiPill').textContent=`Hallmark · ${cfg.mode}`;
    $('deaiFoot').innerHTML=`<span>规则来源：Nutlope/hallmark（MIT）· 引擎 ${esc(report.engine||'')} v${esc(report.version||'')} · `+
      `检查时间 ${esc(report.checked_at||'')} · 实时报告见 <code>/api/hallmark</code>。</span>`;
  }

  async function load(){
    if(isGithubPages) return; // 静态托管：保留快照文案
    try{
      const res=await fetch('api/hallmark');
      if(!res.ok) return;
      const data=await res.json();
      if(data && data.surfaces) renderFindings(data);
    }catch(e){/* 后端未启动时保留静态快照 */}
  }
  load();
})();

/* ===================== 推送前 · 文本内容去 AI 味（Humanizer-zh 静态闸门） =====================
   数据源：GET /api/humanizer（或 /api/text-deai）。与上面的设计层面板并列，
   GitHub Pages 纯静态托管时保留 index.html 里的静态快照，不发送请求。 */
(function(){
  const section=document.getElementById('humanizerSection');
  if(!section) return;
  const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const $=id=>document.getElementById(id);
  const modeSel=$('humanizerMode');

  try{
    const saved=localStorage.getItem('humanizer.mode');
    if(saved && ['polish','audit','off'].includes(saved)) modeSel.value=saved;
  }catch(e){}
  modeSel.addEventListener('change',()=>{
    try{ localStorage.setItem('humanizer.mode', modeSel.value); }catch(e){}
  });
  window.getHumanizerMode=()=>modeSel ? modeSel.value : 'polish';

  function render(report){
    const push=report.surfaces?.push||null, page=report.surfaces?.page||null;
    $('hzScoreBefore').textContent=report.overall_score_before ?? '—';
    $('hzScoreAfter').textContent=report.overall_score_after ?? '—';
    const d=(report.overall_score_after??0)-(report.overall_score_before??0);
    $('hzScoreDelta').textContent=(d>0?`+${d}`:String(d))+' 分 · 启发式体检分，非 AI 生成概率';

    for(const [r,scoreEl,stateEl,label] of [
      [push,'hzPushScore','hzPushState','推送 HTML'],
      [page,'hzPageScore','hzPageState','管理页'],
    ]){
      if(!r) continue;
      $(scoreEl).textContent=`${r.score_before} → ${r.score_after}`;
      const a=r.after||{}, c=a.counts||{};
      $(stateEl).textContent=`${label}：${a.findings?.length||0} 类模式命中（可清理 ${c.auto_fixable||0} / 需语义判断 ${c.manual||0}）；`+
        `本次清理 ${r.fixed_count ?? r.fixes?.filter(f=>f.gate!=='fact-lock').length ?? 0} 处零信息套话。`;
    }

    const merged=[];
    for(const s of [push,page]){
      const tag=s===push?'推送':'页面';
      for(const f of (s?.before?.findings||[])) merged.push({...f, surface:tag});
    }
    const rank={strong:0,medium:1,weak:2};
    merged.sort((a,b)=>(rank[a.tier]??3)-(rank[b.tier]??3) || b.count-a.count);
    const list=$('hzFindings');
    if(!merged.length){
      list.innerHTML='<li><span class="deai-finding-tier">clean</span>两个面均未命中文本 AI 味模式。</li>';
    }else{
      list.innerHTML=merged.slice(0,14).map(f=>
        `<li><span class="deai-finding-tier ${esc(f.tier.toLowerCase())}">${esc(f.tier)}</span>`+
        `<span class="deai-finding-gate">${esc(f.gate)} · ${esc(f.surface)}</span>`+
        `<b>${esc(f.name)} ×${Number(f.count)||0}</b>`+
        `<span class="deai-finding-auto">${f.auto?'可清理':'需语义判断'} · ${esc((f.fix||'').slice(0,50))}</span></li>`
      ).join('');
    }

    if(window.renderDeaiDiffs) window.renderDeaiDiffs('hzDiffs','hzBlocked','hzBlockedWrap',[push,page]);

    const waivers=report.waivers||[];
    if(waivers.length){
      $('hzWaivers').innerHTML=waivers.map(w=>
        `<li><b>${esc(w.item)}</b>（${esc(w.gate)}）：${esc(w.reason)}</li>`).join('');
    }
    let boundary=report.boundary||'';
    if(report.limits) boundary+=' '+report.limits;
    if(boundary) $('hzBoundary').textContent=boundary;
    const cfg=report.config||{};
    if(cfg.mode) $('humanizerPill').textContent=`Humanizer-zh · ${cfg.mode}`;
    $('hzFoot').innerHTML=`<span>引擎 ${esc(report.engine||'')} v${esc(report.version||'')} · `+
      `检查 ${esc(String(report.patterns?.length||0))} 类模式 · `+
      `检查时间 ${esc(report.checked_at||'')} · 实时报告见 <code>/api/humanizer</code>。</span>`;
  }

  async function load(){
    if(isGithubPages) return;
    try{
      const res=await fetch('api/humanizer');
      if(!res.ok) return;
      const data=await res.json();
      if(data && data.surfaces) render(data);
    }catch(e){}
  }
  load();
})();

/* ---- AI 量化日报 · 五步流程 × 五维分析 ----
   静态页保留 2026-10-26 示例快照（quant_daily.demo_bundle 渲染结果）；
   server.py 可用时用 /api/quant 按当日行情 + 快讯重算，非交易日显示「今日非交易日，无日报」。
   所有外部文本都转义；数据缺失项按接口返回的 status 标 ⚠️，不在前端补数。 */
(function(){
  const summaryEl=document.getElementById('quantSummary');
  const metaEl=document.getElementById('quantMeta');
  const dimsEl=document.getElementById('quantDims');
  const stratEl=document.getElementById('quantStrategy');
  const risksEl=document.getElementById('quantRisks');
  const pillEl=document.getElementById('quantPill');
  if(!summaryEl||!metaEl||!dimsEl||!stratEl||!risksEl) return;

  function esc(s){
    return String(s==null?'':s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function arr(v){ return Array.isArray(v)?v:[]; }
  function mark(status){
    if(status==='missing'||status==='anomaly') return ' ⚠️';
    if(status==='proxy') return '（代理）';
    return '';   // na（如北向已停止披露）= 不适用，不打警告
  }
  function provMark(status){
    if(status==='live') return '✅ 实测';
    if(status==='proxy') return '◐ 代理';
    if(status==='missing') return '⚠️ 未取得';
    return esc(status||'');
  }
  function signalClass(signal){
    if(signal==='看空') return 'press';
    if(signal==='中性') return 'neutral';
    return '';
  }

  function renderDims(dims){
    if(!dims.length){
      dimsEl.innerHTML='<article class="quant-dim"><p class="quant-dim-note">当日五维数据均未取得。</p></article>';
      return;
    }
    dimsEl.innerHTML=dims.map(d=>{
      const metrics=arr(d.metrics).slice(0,4).map(m=>(
        `<li><span>${esc(m.label)}</span><b>${esc(m.value)}${mark(m.status)}</b>`
        + (m.delta?`<small>${esc(m.delta)}</small>`:'')
        + `</li>`
      )).join('');
      const first=arr(d.highlights)[0]||'';
      return `<article class="quant-dim">`
        + `<div class="quant-dim-head"><span class="point-no">${esc(d.no)}</span>`
        + `<b>${esc(d.label)}</b>`
        + `<em class="${signalClass(d.signal)}">${esc(d.signal)}</em></div>`
        + `<p class="quant-dim-focus">${esc(d.focus||'')}</p>`
        + `<ul>${metrics}</ul>`
        + `<p class="quant-dim-note">${esc(first)}</p>`
        + `</article>`;
    }).join('');
  }

  function renderStrategy(strategy){
    const items=arr(strategy&&strategy.items);
    const position=(strategy&&strategy.position)||{};
    let html='<tr><th>策略</th><th>信号</th><th>动作</th></tr>';
    html+=items.map(i=>(
      `<tr><td>${esc(i.label)}</td><td>${esc(i.signal)}</td>`
      + `<td>${esc(i.title)}<small>${esc(i.detail)}</small></td></tr>`
    )).join('');
    if(position.value!=null){
      const delta=(position.delta!=null)?`（较上一日 ${position.delta>0?'+':''}${position.delta}pct）`:'';
      html+=`<tr><td>总仓位</td><td>${esc(position.signal||'')}</td>`
        + `<td>权益仓位中枢 <b>${esc(position.value)}%</b>${esc(delta)}`
        + `<small>${esc(position.reason||'')}</small></td></tr>`;
    }
    stratEl.innerHTML=html;
  }

  function renderRisks(risks){
    risksEl.innerHTML=arr(risks).map(r=>{
      const icon=(r.level==='高'||r.level==='中')?'⚠️':'✅';
      return `<li><b>${icon} ${esc(r.title)}（${esc(r.level)}）</b>${esc(r.detail)}</li>`;
    }).join('');
  }

  /* 采集明细：逐字段说明这次日报的数字从哪来、哪些是代理、哪些没取到。
     数据源故障与缺口写在报告里，不让读者自己猜。 */
  function renderProvenance(data){
    const el=document.getElementById('quantProvenance');
    if(!el) return;
    const rows=arr(data.provenance);
    const stats=data.provenance_stats||{};
    if(!rows.length){
      el.innerHTML='<summary>采集明细（本次未返回）</summary>';
      return;
    }
    const head=`采集明细 · 实测 ${Number(stats.live||0)} / 代理 ${Number(stats.proxy||0)} / 未取得 ${Number(stats.missing||0)}（点击展开）`;
    const body=rows.map(r=>(
      `<tr><td>${esc(r.field)}</td><td>${provMark(r.status)}</td>`
      + `<td>${esc(r.source||'')}${r.detail?`<small>${esc(r.detail)}</small>`:''}</td></tr>`
    )).join('');
    el.innerHTML=`<summary>${esc(head)}</summary>`
      + '<table class="quant-action-table"><tr><th>字段</th><th>状态</th><th>来源 / 缺口说明</th></tr>'
      + body + '</table>';
  }

  function render(data){
    if(!data || data.error) return;
    if(data.status==='skipped'){
      summaryEl.innerHTML=`<mark>${esc(data.as_of||'今日')}</mark>${esc(data.summary||'今日非交易日，无日报')}`;
      metaEl.innerHTML='<span class="meta-tag neutral">非交易日 · 自动跳过</span>'
        + `<span>${esc((data.trading_day&&data.trading_day.reason)||'')} · 执行方案仍随接口返回</span>`;
      if(pillEl) pillEl.textContent='今日非交易日';
      dimsEl.innerHTML='';
      stratEl.innerHTML='';
      renderRisks([]);
      renderProvenance({provenance:[]});
      return;
    }
    const quality=data.quality||{};
    const dims=arr(data.dimensions);
    const longCount=dims.filter(d=>d.signal==='看多').length;
    const neutralCount=dims.filter(d=>d.signal==='中性').length;
    const position=(data.strategy&&data.strategy.position)||{};
    summaryEl.innerHTML=`<mark>核心结论 · ${esc(data.as_of||'')}</mark>${esc(data.summary||'')}`;
    metaEl.innerHTML=`<span class="meta-tag">数据完整度 ${Number(quality.score||0)}%</span>`
      + `<span class="meta-tag neutral">五维 ${longCount} 看多 · ${neutralCount} 中性</span>`
      + `<span class="meta-tag">权益仓位 ${esc(position.value!=null?position.value+'%':'数据暂缺')}</span>`
      + `<span>数据源 ${esc(data.source==='live'?'实时接口 + 当日快讯':(data.source==='demo'?'示例数据包':'外部数据包'))}`
      + ` · 归档 <code>reports/${esc(String(data.as_of||'').replace(/-/g,''))}_量化交易日报.md</code></span>`;
    renderDims(dims);
    renderStrategy(data.strategy||{});
    renderRisks(data.risks||[]);
    renderProvenance(data);
    if(pillEl) pillEl.textContent=`数据完整度 ${Number(quality.score||0)}%`;
  }

  async function load(){
    if(typeof isGithubPages!=='undefined' && isGithubPages) return;
    try{
      const res=await fetch('api/quant');
      if(!res.ok) return;
      const data=await res.json();
      if(!data || data.error) return;
      const score=Number((data.quality||{}).score||0);
      if(data.status!=='skipped' && score<40){
        /* 实时采集没拿到当日数据（本机无外网 / 接口故障）：保留标注过的示例快照，
           只说明原因与缺口入口，不把「数据暂缺」渲染成看着像当日结论的卡片。 */
        if(pillEl) pillEl.textContent=`实时采集受限 · 完整度 ${score}%`;
        if(metaEl && !metaEl.dataset.restricted){
          metaEl.dataset.restricted='1';
          const note=document.createElement('span');
          note.textContent=`实时采集未取得当日数据（完整度 ${score}%），以下为 2026-10-26 示例快照；`
            + '逐字段缺口见 /api/quant?format=json 的 provenance';
          metaEl.appendChild(note);
        }
        return;
      }
      render(data);
    }catch(e){ /* 静态托管 / 离线：保留页面里的示例快照 */ }
  }
  load();
  if(typeof refreshBtn!=='undefined' && refreshBtn){
    refreshBtn.addEventListener('click', load);
  }
  window._quantReload=load;
})();
