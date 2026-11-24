// ==UserScript==
// @name         AI 提问捕获 v2.5（多轮线索 · 附件原件 · 回答防误抓）
// @namespace    local.ai.capture
// @version      2.5
// @description  发送瞬间抓问题，把附件原件和回答一起存进本地卡片库；同一会话的多轮追问自动串成一条线索
// @match        https://www.doubao.com/*
// @match        https://doubao.com/*
// @match        https://chat.deepseek.com/*
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @match        https://www.kimi.com/*
// @match        https://kimi.moonshot.cn/*
// @match        https://claude.ai/*
// @match        https://tongyi.aliyun.com/*
// @match        https://chat.qwen.ai/*
// @match        https://yiyan.baidu.com/*
// @match        https://chatglm.cn/*
// @match        https://www.zhipuai.cn/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const BASE = 'http://127.0.0.1:8765';
  const IDLE_MS = 3000;      // 页面静止多久算"生成可能结束"
  const CONFIRM_MS = 1400;   // 两次确认的间隔
  const NEED_STABLE = 2;     // 需要连续几次读数一致才落库
  const MAX_WAIT = 180000;   // 单轮最多等 3 分钟
  const MAX_CHARS = 20000;

  let lastSent = { text: '', at: 0 };
  let pending = null;

  const SOURCE_BY_HOST = [
    ['doubao', '豆包'], ['deepseek', 'DeepSeek'], ['openai', 'ChatGPT'], ['chatgpt', 'ChatGPT'],
    ['kimi', 'Kimi'], ['moonshot', 'Kimi'], ['claude', 'Claude'], ['tongyi', '通义千问'],
    ['qwen', '通义千问'], ['yiyan', '文心一言'], ['chatglm', '智谱'], ['zhipuai', '智谱']
  ];

  function source() {
    const h = location.hostname;
    for (const [k, v] of SOURCE_BY_HOST) if (h.includes(k)) return v;
    return h;
  }

  function post(path, payload, cb) {
    const data = JSON.stringify(payload);
    const done = r => {
      let j = null;
      try { j = JSON.parse(r.responseText); } catch (e) {}
      if (cb) cb(j, r.status);
    };
    if (typeof GM_xmlhttpRequest === 'function') {
      GM_xmlhttpRequest({
        method: 'POST', url: BASE + path,
        headers: { 'Content-Type': 'application/json' }, data,
        onload: r => { console.log('[capture]', path, r.status); done(r); },
        onerror: () => console.warn('[capture] 本地服务未启动')
      });
    } else {
      fetch(BASE + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: data })
        .then(r => r.text().then(t => ({ status: r.status, responseText: t })))
        .then(done)
        .catch(() => console.warn('[capture] 本地服务未启动'));
    }
  }

  // ---------------------------------------------------------------- 会话识别
  // 一条"线索" = 网页上的一个会话。优先用 URL 里的会话 id，
  // 首条消息发出前 URL 还没有 id，就先按标签页记一个临时的，等 id 出现再改名。

  const K_CONV = 'aicards_conv';
  const K_URLID = 'aicards_urlid';
  const K_TURN = 'aicards_turn_';

  function urlConvId() {
    const m = location.pathname.match(/\/(?:chat|c|a\/chat\/s|qianwen)\/([A-Za-z0-9_-]{6,})/);
    if (m) return m[1];
    const q = new URLSearchParams(location.search);
    for (const k of ['conversationId', 'conversation_id', 'sessionId', 'chatId', 'convId']) {
      const v = q.get(k);
      if (v && v.length > 5) return v;
    }
    return '';
  }

  function uid() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  function sget(k) { try { return sessionStorage.getItem(k) || ''; } catch (e) { return ''; } }
  function sset(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }

  // 返回 { conv, turn }；同一会话里轮次递增，换会话就归 1
  function convState() {
    let conv = sget(K_CONV);
    const prev = sget(K_URLID);
    const cur = urlConvId();

    if (!conv) {
      conv = cur ? 'url:' + cur : 'tab:' + uid();
    } else if (cur && prev && cur !== prev) {
      // 明确从一个会话切到另一个会话
      conv = 'url:' + cur;
    } else if (!cur && prev) {
      // 从会话页回到了新对话页
      conv = 'tab:' + uid();
    }

    // 首问时 URL 还没有会话 id，回答生成后才有。这时算同一个会话，
    // 把临时线索改名过去，并把轮次计数带过去，不能另起一条。
    const carry = +sget(K_TURN + conv) || 0;
    if (conv.indexOf('tab:') === 0 && cur) {
      post('/api/thread/rebind', { old: conv, new: 'url:' + cur });
      conv = 'url:' + cur;
      if (carry) sset(K_TURN + conv, String(carry));
    }

    sset(K_CONV, conv);
    sset(K_URLID, cur);
    const turn = (+sget(K_TURN + conv) || 0) + 1;
    sset(K_TURN + conv, String(turn));
    return { conv, turn };
  }

  // ---------------------------------------------------------------- 输入框

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 40 && r.height > 8;
  }

  function findInput() {
    const a = document.activeElement;
    if (a && (a.tagName === 'TEXTAREA' || a.isContentEditable) && visible(a)) return a;
    const nodes = [...document.querySelectorAll('textarea, [contenteditable="true"]')].filter(visible);
    return nodes.length ? nodes[nodes.length - 1] : null;
  }

  function readValue(el) {
    if (!el) return '';
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return el.value || '';
    return el.innerText || el.textContent || '';
  }

  // ---------------------------------------------------------------- 回答块

  // 顺序即优先级：越靠前越"精确"，命中就不再往下找
  const ANSWER_SEL = [
    '[data-message-author-role="assistant"]',
    '[data-testid="message_text_content"]',
    '.ds-markdown',
    '.font-claude-message',
    '[class*="markdown-body"]',
    '[class*="flow-markdown"]',
    '[class*="markdown"]',
    '[class*="message-content"]',
    '[class*="answer-content"]'
  ];

  // 元素可能被同名 class 嵌套（外层容器 + 内层 body），要取最外层，
  // 否则只能拿到第一个小标题。只沿"同样的选择器"往上爬，不会跨到别的消息。
  function outermost(el, sel) {
    let cur = el;
    let guard = 0;
    while (cur.parentElement && guard++ < 12) {
      let hit = false;
      try { hit = cur.parentElement.matches(sel); } catch (e) { hit = false; }
      if (!hit) break;
      cur = cur.parentElement;
    }
    return cur;
  }

  // 只留文字，用来判断"这段是不是我刚发出去的那句话"
  function normText(s) {
    return (s || '').replace(/\s+/g, '')
      .replace(/[。，、！？；：""''（）【】《》.,!?;:'"()\[\]{}<>·—…\-~]/g, '');
  }

  // 豆包这类站点会把用户刚发的话也放进 markdown 容器里。
  // 不排掉的话，抓到的"回答"其实就是自己的提问 —— 多一个空格少一个标点，
  // 长度判断就漏过去了（线上真出现过：问题 372 字、答案 364 字，内容一模一样）。
  function isEcho(t, p) {
    if (!p || !t) return false;
    if (t === p) return true;
    const head = p.slice(0, 50);
    if (head.length >= 16 && t.indexOf(head) === 0 && t.length / p.length < 1.3) return true;
    return false;
  }

  // 发消息前先记下页面上已有的回答。回答还没开始渲染时，
  // 页面上最后一条就是"上一轮的答案"，直接存进来会把整条线索写串。
  function snapshotAnswers() {
    const set = new Set();
    for (const sel of ANSWER_SEL) {
      let els = [];
      try { els = [...document.querySelectorAll(sel)]; } catch (e) { continue; }
      for (const e of els) {
        const t = normText(e.innerText || '');
        if (t) set.add(t.slice(0, 300));
      }
    }
    return set;
  }

  function lastAnswerEl(skip, promptNorm) {
    const bodyLen = (document.body.innerText || '').length;
    const input = findInput();          // 包着输入框的元素是输入区，不可能是回答

    for (const sel of ANSWER_SEL) {
      let els = [];
      try { els = [...document.querySelectorAll(sel)].filter(visible); } catch (e) { continue; }
      if (!els.length) continue;

      const ok = [];
      for (const e of els) {
        const el = outermost(e, sel);
        if (ok.indexOf(el) >= 0) continue;
        if (el.contains(document.activeElement)) continue;
        if (input && (el === input || el.contains(input))) continue;
        const t = (el.innerText || '').trim();
        if (t.length < 20) continue;
        if (bodyLen > 0 && t.length > bodyLen * 0.8) continue;  // 整页文本，肯定是抓错了
        const n = normText(t).slice(0, 300);
        if (isEcho(n, promptNorm)) continue;                    // 这是我自己说的话
        if (skip && skip.has(n)) continue;                      // 这是发送前就存在的旧回答
        ok.push({ el, sel });
      }
      // ANSWER_SEL 按精确度排序：第一个出结果的就用它，同类里取 DOM 最后一个（最新那条）
      if (ok.length) return ok[ok.length - 1];
    }

    // 一个都没命中时，退而求其次：找页面上最后一条"像回答"的块，
    // 但必须不是自己的话、也不是旧回答，宁可漏也不能存错。
    const loose = [...document.querySelectorAll('div, article, section')].filter(visible);
    for (let i = loose.length - 1; i >= 0; i--) {
      const el = loose[i];
      if (input && (el === input || el.contains(input))) continue;
      const t = (el.innerText || '').trim();
      if (t.length < 60) continue;
      if (bodyLen > 0 && t.length > bodyLen * 0.7) continue;
      if (![...el.children].some(k => /^(P|UL|OL|PRE|H[1-6]|TABLE|BLOCKQUOTE)$/.test(k.tagName))) continue;
      const n = normText(t).slice(0, 300);
      if (isEcho(n, promptNorm) || (skip && skip.has(n))) continue;
      return { el, sel: 'fallback' };
    }
    return null;
  }

  function stillGenerating() {
    const els = document.querySelectorAll('button, [role="button"]');
    for (const b of els) {
      if (!visible(b)) continue;
      const label = (b.getAttribute('aria-label') || '') + ' ' + (b.title || '') + ' ' + (b.dataset.testid || '');
      if (/停止|停止生成|stop generating|stop streaming|stop response|中止/i.test(label)) return true;
    }
    return false;
  }

  // ---------------------------------------------------------------- DOM → Markdown

  const MD_SKIP = /^(script|style|svg|button|textarea|input|noscript|canvas)$/i;
  const INLINE = /^(a|abbr|b|strong|i|em|code|span|u|del|s|sub|sup|mark|kbd|small|font|time|img|br)$/i;

  let _codeStash = [];

  function textOf(node) {
    return node.innerText !== undefined ? (node.innerText || '') : (node.textContent || '');
  }

  // 行内元素 → markdown
  function mdInline(node) {
    let s = '';
    for (const ch of [...(node.childNodes || [])]) {
      if (ch.nodeType === 3) { s += ch.nodeValue.replace(/\s+/g, ' '); continue; }
      if (ch.nodeType !== 1) continue;
      const t = ch.tagName.toLowerCase();
      if (MD_SKIP.test(t)) continue;

      if (t === 'br') { s += '\n'; continue; }
      if (t === 'code') {
        const c = textOf(ch).replace(/\n/g, ' ').replace(/`/g, '\\`');
        s += c ? '`' + c + '`' : '';
        continue;
      }
      if (t === 'strong' || t === 'b') {
        const x = mdInline(ch).trim();
        s += x ? '**' + x + '**' : '';
        continue;
      }
      if (t === 'em' || t === 'i') {
        const x = mdInline(ch).trim();
        s += x ? '*' + x + '*' : '';
        continue;
      }
      if (t === 'del' || t === 's') {
        const x = mdInline(ch).trim();
        s += x ? '~~' + x + '~~' : '';
        continue;
      }
      if (t === 'a') {
        const href = ch.getAttribute('href') || '';
        const label = mdInline(ch).trim();
        if (!label) continue;
        s += /^https?:/i.test(href) ? '[' + label + '](' + href + ')' : label;
        continue;
      }
      if (t === 'img') {
        const src = ch.getAttribute('src') || '';
        if (/^https?:/i.test(src)) s += '![' + (ch.getAttribute('alt') || '图片') + '](' + src + ')';
        continue;
      }
      s += mdInline(ch);
    }
    return s.replace(/[ \t]{2,}/g, ' ');
  }

  function codeLang(pre, code) {
    const raw = ((code && code.className) || '') + ' ' + (pre.className || '') + ' ' +
                ((code && code.getAttribute('data-language')) || '');
    const m = raw.match(/(?:language|lang|highlight)-([a-z0-9+#._-]+)/i);
    if (m) return m[1].toLowerCase();
    const head = pre.querySelector('[class*="banner"],[class*="header"],[class*="title"],[class*="lang"]');
    if (head) {
      const t = (head.innerText || '').trim().toLowerCase();
      if (/^[a-z0-9+#._-]{1,15}$/.test(t)) return t;
    }
    return '';
  }

  function listMd(node, depth) {
    const ordered = node.tagName.toLowerCase() === 'ol';
    const pad = '  '.repeat(depth);
    let out = '';
    let idx = 1;

    for (const li of [...node.children]) {
      if (li.tagName.toLowerCase() !== 'li') { out += blockMd(li); continue; }
      const nested = [];
      let content = '';
      for (const ch of [...li.childNodes]) {
        if (ch.nodeType === 3) {
          const x = ch.nodeValue.replace(/\s+/g, ' ');
          if (x.trim()) content += x;
          continue;
        }
        if (ch.nodeType !== 1) continue;
        const t = ch.tagName.toLowerCase();
        if (t === 'ul' || t === 'ol') { nested.push(ch); continue; }
        content += INLINE.test(t) ? mdInline(ch) : blockMd(ch);
      }
      const body = content.replace(/\n{2,}/g, '\n').trim().replace(/\n/g, '\n' + pad + '  ');
      out += pad + (ordered ? (idx++) + '.' : '-') + ' ' + body + '\n';
      for (const nl of nested) out += listMd(nl, depth + 1) + '\n';
    }
    return out.replace(/\n+$/, '');
  }

  function tableMd(node) {
    const rows = [...node.querySelectorAll('tr')];
    if (!rows.length) return '';
    const cells = tr => [...tr.children].map(c => mdInline(c).trim().replace(/\|/g, '\\|'));
    const head = cells(rows[0]);
    let s = '| ' + head.join(' | ') + ' |\n| ' + head.map(() => '---').join(' | ') + ' |\n';
    for (const r of rows.slice(1)) s += '| ' + cells(r).join(' | ') + ' |\n';
    return s.replace(/\n+$/, '');
  }

  function childrenBlock(node) {
    let s = '';
    for (const ch of [...(node.childNodes || [])]) {
      if (ch.nodeType === 3) {
        const t = ch.nodeValue.replace(/\s+/g, ' ');
        if (t.trim()) s += t;
        continue;
      }
      if (ch.nodeType !== 1) continue;
      const t = ch.tagName.toLowerCase();
      s += INLINE.test(t) ? mdInline(ch) : blockMd(ch);
    }
    return s;
  }

  function blockMd(node) {
    const tag = node.tagName.toLowerCase();
    if (MD_SKIP.test(tag)) return '';
    if (INLINE.test(tag) && tag !== 'br') return mdInline(node);
    if (tag === 'br') return '\n';

    if (/^h[1-6]$/.test(tag)) {
      return '\n\n' + '#'.repeat(+tag[1]) + ' ' + mdInline(node).trim() + '\n\n';
    }
    if (tag === 'p') {
      const x = mdInline(node).trim();
      return x ? '\n\n' + x + '\n\n' : '';
    }
    if (tag === 'pre') {
      const code = node.querySelector('code') || node;
      const lang = codeLang(node, code);
      const body = textOf(code).replace(/\r/g, '').replace(/\n+$/, '');
      if (!body.trim()) return '';
      _codeStash.push('```' + lang + '\n' + body + '\n```');
      return '\n\n\u0002' + (_codeStash.length - 1) + '\u0002\n\n';
    }
    if (tag === 'ul' || tag === 'ol') return '\n\n' + listMd(node, 0) + '\n\n';
    if (tag === 'blockquote') {
      const inner = childrenBlock(node).trim();
      if (!inner) return '';
      return '\n\n' + inner.split('\n').map(l => (l ? '> ' + l : '>')).join('\n') + '\n\n';
    }
    if (tag === 'table') {
      const tb = tableMd(node);
      return tb ? '\n\n' + tb + '\n\n' : '';
    }
    if (tag === 'hr') return '\n\n---\n\n';

    const inner = childrenBlock(node).trim();
    return inner ? '\n\n' + inner + '\n\n' : '';
  }

  function domToMd(root) {
    _codeStash = [];
    let md = '';
    try {
      md = childrenBlock(root);
    } catch (e) {
      console.warn('[capture] markdown 转换失败，退回纯文本', e);
      return '';
    }
    md = md
      .replace(/\r/g, '')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();

    if (_codeStash.length) {
      md = md.replace(/\n*\u0002(\d+)\u0002\n*/g, (m, i) => '\n\n' + _codeStash[+i] + '\n\n').trim();
    }
    _codeStash = [];
    return md;
  }

  // 取一次快照：纯文本用于判断"长度是否还在变"，markdown 用于落库
  function grab(skip, promptNorm) {
    const hit = lastAnswerEl(skip, promptNorm);
    if (!hit) return null;
    const el = hit.el;
    const txt = (el.innerText || '').trim();
    if (!txt) return null;
    const md = domToMd(el);
    // 转换结果明显不完整时，宁可存纯文本
    const useMd = md.length >= 40 && md.length >= txt.length * 0.4;
    return {
      md: useMd ? md : txt,
      fmt: useMd ? 'md' : 'text',
      len: txt.length,
      sel: hit.sel,
      head: normText(txt).slice(0, 40)
    };
  }

  // ---------------------------------------------------------------- 等待生成结束

  // 核心：不靠单一信号，而是"静止 + 两次读数一致"，落库后再补抓一轮防截断
  function watchAnswer(st, skipSet) {
    const P = st.text;                    // 补抓阶段 pending 已清空，必须用局部变量
    const PN = normText(P);
    const skip = skipSet || null;
    pending = { text: P, at: Date.now() };
    let timer = null;
    let done = false;
    let lastLen = -1;
    let stable = 0;
    const started = Date.now();

    const mo = new MutationObserver(() => {
      if (done) return;
      clearTimeout(timer);
      timer = setTimeout(check, IDLE_MS);
    });

    try {
      mo.observe(document.body, { childList: true, subtree: true, characterData: true });
    } catch (e) { return; }

    timer = setTimeout(check, IDLE_MS);

    function push(s, status) {
      const payload = {
        prompt: P,
        conv: st.conv,
        turn: st.turn,
        answer: (s ? s.md : '').slice(0, MAX_CHARS),
        fmt: s ? s.fmt : 'text',
        status: status
      };
      // 有卡片 id 就按 id 落，绝不会把追问的答案写回别的问题
      if (st.id) payload.id = st.id;
      post('/api/answer', payload);
    }

    function check() {
      if (done || !pending) return;

      if (stillGenerating()) {
        timer = setTimeout(check, 1200);
        return;
      }

      const s = grab(skip, PN);
      const elapsed = Date.now() - started;

      // 找不到合格回答就继续等。以前这里用"长度和问题一样"来判断，
      // 结果是：多一个标点就存成自己的提问，正好一样就判成没抓到 —— 两个毛病都从这来。
      if (!s) {
        if (elapsed < 90000) { timer = setTimeout(check, 1500); return; }
        push(null, 'missing');
        console.log('[capture] 90 秒内没找到合格的回答，标记 missing');
        finish();
        return;
      }

      // 长度和上次一样 → 累加稳定计数；变了 → 清零重来
      if (s.len !== lastLen) {
        lastLen = s.len;
        stable = 0;
        timer = setTimeout(check, CONFIRM_MS);
        return;
      }

      stable++;
      if (stable < NEED_STABLE) {
        timer = setTimeout(check, CONFIRM_MS);
        return;
      }

      push(s, 'ok');
      console.log('[capture] answer saved: %d 字 / %s / %d chars md / 命中 %s', s.len, s.fmt, s.md.length, s.sel);
      finish();
      verifyLater(s.md.length, s.head);
    }

    // 落库后再盯 20 秒：如果回答其实还在长，覆盖成更完整的版本
    function verifyLater(baseLen, head) {
      let round = 0;
      const t = setInterval(() => {
        round++;
        const now = grab(skip, PN);
        // 必须是同一条回答在变长，不能拿别的块顶上
        if (now && now.head === head && now.md.length > baseLen * 1.15) {
          push(now, 'ok');
          console.log('[capture] answer extended to %d chars', now.md.length);
          baseLen = now.md.length;
        }
        if (round >= 7) clearInterval(t);
      }, 3000);
    }

    function finish() {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try { mo.disconnect(); } catch (e) {}
      pending = null;
    }

    setTimeout(() => { if (!done) finish(); }, MAX_WAIT);
  }

  // ---------------------------------------------------------------- 附件
  // 两条腿：能拿到真正的 File（选文件 / 拖进来 / 粘贴）就上传原件；
  // 豆包这类先把文件传到云端的站点，页面里只剩文件名和预览图，
  // 就把预览图抓下来，实在不行只记名字，并在卡片上标明"仅存预览"。

  const MAX_FILE = 20 * 1024 * 1024;
  const STASH_TTL = 10 * 60 * 1000;
  const EXT_RE = /\.(pdf|docx?|xlsx?|pptx?|csv|tsv|txt|md|markdown|json|zip|rar|7z|png|jpe?g|webp|gif|bmp)\b/i;

  let stash = [];              // [{file, at}]
  let domUsed = { sig: '', at: 0 };

  function nameOf(f) {
    if (f.name) return f.name;
    const t = (f.type || '').toLowerCase();
    if (t.indexOf('image/') === 0) return '粘贴图片.' + (t.split('/')[1] || 'png');
    return '附件';
  }

  function keepFiles(list) {
    const at = Date.now();
    for (const f of list || []) {
      if (!f || !f.size || f.size > MAX_FILE) continue;
      if (f.size === 0) continue;
      stash.push({ file: f, at });
    }
    if (stash.length > 8) stash = stash.slice(-8);
  }

  // 捕获阶段的三个入口：文件选择器、拖拽、粘贴。站点自己会清空 input，必须当场接住。
  document.addEventListener('change', e => {
    const el = e.target;
    if (el && el.tagName === 'INPUT' && String(el.type || '').toLowerCase() === 'file'
        && el.files && el.files.length) keepFiles(el.files);
  }, true);

  document.addEventListener('drop', e => {
    const f = e.dataTransfer && e.dataTransfer.files;
    if (f && f.length) keepFiles(f);
  }, true);

  document.addEventListener('paste', e => {
    const f = e.clipboardData && e.clipboardData.files;
    if (f && f.length) keepFiles(f);
  }, true);

  function composerBox() {
    const el = findInput();
    if (!el) return null;
    let cur = el;
    for (let i = 0; i < 6 && cur && cur.parentElement && cur !== document.body; i++) {
      const box = cur.parentElement;
      const txt = box.innerText || '';
      if (txt.length > 4000) break;                       // 已经爬到整页了，别再往上
      if (box.querySelector('img') || EXT_RE.test(txt)) return box;
      cur = box;
    }
    return el.parentElement;
  }

  // 从输入框附近找出附件线索：文件名 + 预览图地址
  function scanComposer() {
    const out = { names: [], urls: [] };
    const box = composerBox();
    if (!box) return out;
    const seen = new Set();
    const push = n => {
      n = (n || '').trim().replace(/^[•·\-\s]+/, '');
      if (!n || n.length > 120 || seen.has(n)) return;
      if (!EXT_RE.test(n)) return;
      seen.add(n);
      out.names.push(n);
    };
    box.querySelectorAll('img').forEach(img => {
      const r = img.getBoundingClientRect();
      if (r.width < 36 || r.height < 36) return;          // 图标不算附件
      if (img.closest('button, [role="button"]')) return;
      const src = img.currentSrc || img.src || '';
      if (!src || src.indexOf('data:image/svg') === 0) return;
      if (out.urls.indexOf(src) < 0) out.urls.push(src);
      push(img.getAttribute('alt'));
    });
    box.querySelectorAll('[title], [aria-label], [download]').forEach(el => {
      ['title', 'aria-label', 'download'].forEach(a => {
        const v = el.getAttribute && el.getAttribute(a);
        if (v) push(v);
      });
    });
    (box.innerText || '').split('\n').slice(0, 40).forEach(line => {
      const m = line.match(/[^\s\/\\]{1,60}\.(pdf|docx?|xlsx?|pptx?|csv|txt|md|png|jpe?g|webp|gif|zip)/i);
      if (m) push(m[0]);
    });
    return out;
  }

  function pendingCount() {
    const now = Date.now();
    stash = stash.filter(s => now - s.at < STASH_TTL);
    if (stash.length) return stash.length;
    const d = scanComposer();
    const sig = d.names.join('|') + '#' + d.urls.join('|');
    // 刚发出去的那一份别重复算进来（有些站点发送后预览图还挂着）
    if (sig && sig === domUsed.sig && now - domUsed.at < 60000) return 0;
    return d.names.length + d.urls.length;
  }

  function blobToB64(blob) {
    return new Promise(resolve => {
      const fr = new FileReader();
      fr.onload = () => {
        const s = String(fr.result || '');
        resolve(s.slice(s.indexOf(',') + 1));
      };
      fr.onerror = () => resolve('');
      fr.readAsDataURL(blob);
    });
  }

  function urlToB64(src) {
    return fetch(src, { credentials: 'include' })
      .then(r => (r.ok ? r.blob() : null))
      .then(b => (b && b.size <= MAX_FILE ? blobToB64(b) : ''))
      .catch(() => '');
  }

  async function collectAttachments() {
    const files = [], remotes = [];
    const now = Date.now();
    stash = stash.filter(s => now - s.at < STASH_TTL);

    if (stash.length) {
      for (const s of stash) {
        const data = await blobToB64(s.file);
        if (data) files.push({
          name: nameOf(s.file),
          mime: s.file.type || '',
          data: data
        });
      }
      stash = [];
      return { files, remotes };
    }

    const d = scanComposer();
    if (!d.names.length && !d.urls.length) return { files, remotes };
    domUsed = { sig: d.names.join('|') + '#' + d.urls.join('|'), at: now };

    for (let i = 0; i < d.urls.length && files.length < 6; i++) {
      const src = d.urls[i];
      const nm = d.names[i] || (src.indexOf('blob:') === 0 ? '截图.png' : '预览图.png');
      if (src.indexOf('blob:') === 0 || src.indexOf('data:image') === 0) {
        const data = await urlToB64(src);
        if (data) {
          files.push({ name: nm, mime: 'image/png', data: data });
          continue;
        }
      }
      remotes.push({ name: nm, url: src.indexOf('http') === 0 ? src : '' });
    }
    for (const n of d.names) {
      if (files.some(f => f.name === n) || remotes.some(r => r.name === n)) continue;
      remotes.push({ name: n, url: '' });
    }
    return { files, remotes };
  }

  // ---------------------------------------------------------------- 触发

  function send(text) {
    text = (text || '').trim();
    const withFiles = pendingCount() > 0;
    // 只拖了图/文件、一个字没打也要抓，否则这一轮在库里完全不存在
    if (text.length < 2 && !withFiles) return;
    const now = Date.now();
    if (!withFiles && text === lastSent.text && now - lastSent.at < 4000) return;
    lastSent = { text, at: now };

    const q = text.length >= 2 ? text : '（只发了附件）';
    // 关键：先给"页面上已有的回答"拍个快照，再往下走。
    // 等站点把新回答渲染出来时，凡是快照里的文本都不能当成本轮答案。
    const baseline = snapshotAnswers();

    const st = convState();
    st.text = q;
    st.id = null;

    // 附件要当场快照：站点在你按下发送后可能立刻清空输入区，等落库回来再扫就晚了
    const attTask = withFiles ? collectAttachments() : null;

    // 先开始盯回答，再落库：上传原件可能要几秒，不能拖住抓取
    watchAnswer(st, baseline);

    post('/api/capture', {
      text: q, source: source(), url: location.href, conv: st.conv, turn: st.turn
    }, j => {
      if (!j || !j.card) return;
      st.id = j.card.id;
      if (!attTask) return;
      attTask.then(att => {
        if (!att.files.length && !att.remotes.length) return;
        post('/api/attach', {
          card_id: j.card.id, files: att.files, remote_files: att.remotes
        }, r => console.log('[capture] 附件已挂上:', r && r.files ? r.files.length : 0));
      });
    });
  }

  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    const el = findInput();
    if (!el) return;
    const v = readValue(el);
    if (v.trim().length >= 4 || pendingCount() > 0) send(v);
  }, true);

  document.addEventListener('click', e => {
    const btn = e.target.closest('button, [role="button"]');
    if (!btn) return;
    const hint = ((btn.getAttribute('aria-label') || '') + (btn.title || '') + (btn.textContent || '')).toLowerCase();
    const looksSend = /发送|send|submit/.test(hint) || btn.id === 'composer-submit-button';
    if (!looksSend) return;
    setTimeout(() => {
      const el = findInput();
      if (el) send(readValue(el));
    }, 0);
  }, true);

  console.log('[capture] v2.5 loaded on', location.hostname);
})();
