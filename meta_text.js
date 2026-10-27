// 由 meta_text.py --emit-js 生成，勿手改（改规则请改 meta_text.py）。
window.META_TEXT_RULES = {
  "sentence": [
    "(映射|指标|采集|统计|情感|检索|数据|计价|展示)口径",
    "^口径\\s*[:：]",
    "统计信号",
    "停止(实时)?披露",
    "阈值",
    "可复算",
    "启发式",
    "不是统计概率",
    "模型情景",
    "非预测",
    "证据\\s*[:：]\\s*\\d+\\s*源",
    "数据源故障",
    "实测\\s*\\d+\\s*项",
    "五维加权",
    "不展示",
    "未展示",
    "仅列标题",
    "完整数据",
    "完整\\s*Markdown",
    "见\\s*reports/",
    "实时结果见",
    "结果见\\s*`?/api",
    "图例",
    "供前端",
    "不构成投资建议",
    "免责",
    "disclaimer",
    "不猜数",
    "不编数",
    "说明\\s*[:：]",
    "(?:^|[。；，、\\s（(])注\\s*[:：]",
    "注解",
    "注析",
    "提醒",
    "请注意",
    "\\d{1,2}:\\d{2}\\s*(采集|交付|启动|归档|推送)",
    "只借鉴",
    "不模仿",
    "不代表其观点",
    "背书"
  ],
  "clause": [
    "采集(口径|层|明细|完成|时间|说明|任务|脚本)",
    "仅供参考",
    "不作为投资依据",
    "^\\s*分析研究\\s*$",
    "数据暂缺",
    "未取得",
    "数据完整度",
    "暂无数据",
    "代理\\s*\\d+\\s*项",
    "实测\\s*\\d+\\s*项",
    "降级"
  ],
  "leading": "^\\s*[·•\\-—]?\\s*(口径\\s*[:：]|映射口径|指标口径|采集口径|情感口径|统计口径|数据口径|检索口径|分解结果为|框架\\s*[:：])",
  "bullet": "^[\\s·•\\-—，,、；;。：:]*$"
};

/* 页面侧的同一套规则：说明 / 注解 / 解析 / 提醒类文字不展示。
   静态部分已在 index.html 上标了 data-meta（styles.css 直接 display:none），
   这里负责把 app.js 运行时注入的说明文字也清掉。

   刻意保守：只处理「整块都是说明」的容器（.insight-foot / .quant-notice /
   .bigv-notice / .group-note / .note / .ev / .ftr / small / summary 等），
   不碰表格单元格与结构性空标签（开关滑块、分隔线），避免把版式拆坏。 */
(function () {
  var R = window.META_TEXT_RULES;
  if (!R) return;
  var SENTENCE = new RegExp(R.sentence.join('|'));
  var CLAUSE = new RegExp(R.clause.join('|'));
  var ANY = new RegExp('(?:' + SENTENCE.source + ')|(?:' + CLAUSE.source + ')');
  var SPLIT = /(?<=[。；！？])/;
  var CLS = /(?<=[，,　])/;
  var PROTECT = { deaiSection: 1, humanizerSection: 1, quantActions: 1 };
  var META_BLOCKS = '[data-meta], .insight-foot, .quant-notice, .bigv-notice, .group-note,' +
                    ' .note, .foot, .hint, .ev, .ftr, .sub, .quant-dim-note, small, summary,' +
                    ' li, p';

  function dropClauses(text) {
    if (!CLAUSE.test(text)) return text;
    var kept = text.split(CLS).filter(function (part) {
      return part.trim() && !CLAUSE.test(part);
    });
    var out = kept.join('').trim().replace(/^[，,、；;。]+/, '').replace(/[，,、；;]+$/, '');
    if (!out) return '';
    return /[。]\s*$/.test(text) && !/[。]$/.test(out) ? out + '。' : out;
  }

  function stripText(text) {
    if (!text || !ANY.test(text)) return text;
    var pieces = text.split(SPLIT), kept = [];
    for (var i = 0; i < pieces.length; i++) {
      var piece = pieces[i];
      if (!piece.trim()) continue;
      if (SENTENCE.test(piece)) continue;
      kept.push(dropClauses(piece));
    }
    return kept.join('').replace(/[，,、；;]+$/, '');
  }

  function inProtected(node) {
    for (var el = node.parentNode; el && el.nodeType === 1; el = el.parentNode) {
      if (PROTECT[el.id]) return true;
    }
    return false;
  }

  function cleanBlock(el) {
    if (PROTECT[el.id] || el.getAttribute('data-meta-done')) return false;
    el.setAttribute('data-meta-done', '1');
    if (el.hasAttribute('data-meta')) {          /* 整块说明：直接移出文档流 */
      el.style.display = 'none';
      return true;
    }
    var text = el.textContent;
    if (!text || !ANY.test(text)) return false;
    var cleaned = stripText(text);
    if (cleaned.trim() === text.trim()) return false;
    if (!cleaned.trim()) {                       /* 清空就整块收掉 */
      el.style.display = 'none';
      el.setAttribute('data-meta', '1');
      return true;
    }
    /* 只在「没有行内结构」时改写文字，避免把 <code> / <b> 拆散。 */
    if (!el.querySelector('code,b,i,em,strong,a')) el.textContent = cleaned;
    return true;
  }

  function sweep(root) {
    if (!root || !root.querySelectorAll) return 0;
    var changed = 0;
    Array.prototype.forEach.call(root.querySelectorAll(META_BLOCKS), function (el) {
      if (inProtected(el)) return;
      if (cleanBlock(el)) changed++;
    });
    return changed;
  }

  function boot() {
    sweep(document.body);
    var pending = false;
    new MutationObserver(function (records) {
      var relevant = records.some(function (r) {
        return r.addedNodes && r.addedNodes.length;
      });
      if (!relevant || pending) return;
      pending = true;
      requestAnimationFrame(function () {
        try { sweep(document.body); } finally { pending = false; }
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
