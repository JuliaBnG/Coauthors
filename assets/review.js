// Co-author review layer for a pandoc-rendered draft.
// Every reviewable block is a <div class="rb" id="rb-N">; its markdown source,
// file and line range are in <script id="rb-meta">. Comments anchor to a block
// (and optionally a quoted passage); suggested edits replace a block's source.
(() => {
  'use strict';
  const meta = JSON.parse(document.getElementById('rb-meta').textContent);
  const LSKEY = `coauthors:${meta.paper}:key`;
  const LSINLINE = 'coauthors:inline';
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* private mode */ } },
  };

  // The personal key arrives in the fragment (never sent to the server or in
  // a Referer), is kept in this browser, and is removed from the address bar.
  let key = null;
  const km = location.hash.match(/(?:^#|&)k=([A-Za-z0-9_-]{16,})/);
  if (km) {
    key = km[1];
    store.set(LSKEY, key);
    history.replaceState(null, '', location.pathname + location.search);
  } else {
    key = store.get(LSKEY);
  }

  const S = {
    me: null, round: null, items: [], error: null,
    filter: 'open', inline: store.get(LSINLINE) !== '0', panel: false,
  };
  const order = Object.fromEntries(Object.keys(meta.blocks).map((k, i) => [k, i]));

  // ------------------------------------------------------------ helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'class') el.className = v;
      else if (k === 'style') el.style.cssText = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const c of kids.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  async function api(method, path, body) {
    const r = await fetch(`${meta.api}/${meta.paper}${path}`, {
      method,
      headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
    });
    let j = {};
    try { j = await r.json(); } catch { /* empty */ }
    if (!r.ok) throw new Error(j.error || `${r.status} ${r.statusText}`);
    return j;
  }

  function hue(name) {
    let x = 0;
    for (const c of name) x = (x * 31 + c.codePointAt(0)) >>> 0;
    return x % 360;
  }
  const who = (name) => h('span', { class: 'rb-who', style: `--who:${hue(name)}` }, name);
  const when = (iso) => new Date(iso).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const isDeletion = (it) => it.kind === 'suggestion' && !it.new_src.trim();
  const canWrite = () => S.me && S.round && S.round.state === 'open';
  const isOwner = () => S.me && S.me.role === 'owner';

  // ------------------------------------------------------------ word diff
  function tokens(s) {
    return s.match(/[\p{L}\p{N}_]+|\s+|[^\p{L}\p{N}_\s]/gu) || [];
  }
  const norm = (t) => (/^\s+$/.test(t) ? (/\n\s*\n/.test(t) ? '\n\n' : ' ') : t);

  function diff(a, b) {
    const A = tokens(a), B = tokens(b);
    let p = 0;
    while (p < A.length && p < B.length && norm(A[p]) === norm(B[p])) p++;
    let q = 0;
    while (q < A.length - p && q < B.length - p &&
           norm(A[A.length - 1 - q]) === norm(B[B.length - 1 - q])) q++;
    const a1 = A.slice(p, A.length - q), b1 = B.slice(p, B.length - q);
    const out = [];
    const push = (t, s) => {
      if (!s) return;
      if (out.length && out[out.length - 1].t === t) out[out.length - 1].s += s;
      else out.push({ t, s });
    };
    push('=', B.slice(0, p).join(''));
    const n = a1.length, m = b1.length;
    if (n * m > 4e6) {
      push('-', a1.join('')); push('+', b1.join(''));
    } else {
      const L = new Uint32Array((n + 1) * (m + 1));
      for (let i = n - 1; i >= 0; i--)
        for (let j = m - 1; j >= 0; j--)
          L[i * (m + 1) + j] = norm(a1[i]) === norm(b1[j])
            ? L[(i + 1) * (m + 1) + j + 1] + 1
            : Math.max(L[(i + 1) * (m + 1) + j], L[i * (m + 1) + j + 1]);
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (norm(a1[i]) === norm(b1[j])) { push('=', b1[j]); i++; j++; }
        else if (L[(i + 1) * (m + 1) + j] >= L[i * (m + 1) + j + 1]) push('-', a1[i++]);
        else push('+', b1[j++]);
      }
      while (i < n) push('-', a1[i++]);
      while (j < m) push('+', b1[j++]);
    }
    push('=', B.slice(B.length - q).join(''));
    return out;
  }

  // Unchanged runs longer than 2 * CTX characters are shortened to their ends.
  const CTX = 70;
  function diffView(a, b) {
    const ops = diff(a, b);
    return h('div', { class: 'rb-diff' }, ops.map((o, k) => {
      if (o.t !== '=') return h(o.t === '+' ? 'ins' : 'del', {}, o.s);
      if (o.s.length <= 2 * CTX + 20) return o.s;
      const head = k > 0 ? o.s.slice(0, CTX).replace(/\S*$/, '') : '';
      const tail = k < ops.length - 1 ? o.s.slice(-CTX).replace(/^\S*/, '') : '';
      return [head, h('span', { class: 'rb-gap' }, ' … '), tail];
    }));
  }

  // ------------------------------------------------------------ quote anchoring
  // Map a whitespace-normalised quote back to a DOM Range inside a block.
  function findRange(block, quote) {
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => n.parentElement.closest('.rb-ui, annotation')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    const nodes = [];
    let flat = '', map = [], prevSpace = true;
    for (let n; (n = walker.nextNode());) {
      nodes.push(n);
      const t = n.data;
      for (let k = 0; k < t.length; k++) {
        const sp = /\s/.test(t[k]);
        if (sp && prevSpace) continue;
        flat += sp ? ' ' : t[k];
        map.push([n, k]);
        prevSpace = sp;
      }
    }
    const q = quote.replace(/\s+/g, ' ').trim();
    const at = q ? flat.indexOf(q) : -1;
    if (at < 0) return null;
    const [n0, o0] = map[at], [n1, o1] = map[at + q.length - 1];
    const r = document.createRange();
    r.setStart(n0, o0);
    r.setEnd(n1, o1 + 1);
    return r;
  }

  function paintHighlights() {
    if (!window.CSS || !CSS.highlights || typeof Highlight === 'undefined') return;
    const hl = new Highlight();
    for (const it of S.items) {
      if (it.kind !== 'comment' || !it.quote || it.status === 'withdrawn') continue;
      const b = document.getElementById(it.block);
      const r = b && findRange(b, it.quote);
      if (r) hl.add(r);
    }
    CSS.highlights.set('rb-quote', hl);
  }

  // ------------------------------------------------------------ toolbar & panel
  const bar = h('div', { id: 'rb-bar', class: 'rb-ui' });
  const panel = h('aside', { id: 'rb-panel', class: 'rb-ui', 'aria-label': 'Review comments' });
  const selBtn = h('button', { id: 'rb-selbtn', class: 'rb-ui rb-btn', type: 'button', hidden: true },
    'Comment on selection');
  document.body.prepend(bar);
  document.body.append(panel, selBtn);
  document.documentElement.classList.add('rb-on');
  // The bar wraps on narrow screens: keep the page and the rail below it.
  new ResizeObserver(() => {
    document.body.style.paddingTop = `${bar.offsetHeight + 24}px`;
    document.documentElement.style.setProperty('--rb-bar-h', `${bar.offsetHeight}px`);
  }).observe(bar);

  // Colour theme, per browser and shared by all drafts on this site. The page
  // head applies the stored value before first paint (render.py).
  const LSTHEME = 'coauthors:theme';
  const THEMES = [['auto', 'Auto'], ['light', 'Light'], ['sepia', 'Sepia'],
                  ['dark', 'Dark'], ['black', 'Black']];
  const theme = () => document.documentElement.dataset.theme || 'auto';
  function setTheme(t) {
    if (t === 'auto') { delete document.documentElement.dataset.theme; store.del(LSTHEME); }
    else { document.documentElement.dataset.theme = t; store.set(LSTHEME, t); }
  }

  function setPanel(open) {
    S.panel = open;
    document.documentElement.classList.toggle('rb-panel-open', open);
    drawBar();
  }

  function drawBar() {
    bar.replaceChildren();
    const r = S.round;
    bar.append(h('span', { class: 'rb-title' },
      `Review round ${meta.round}`,
      r ? h('span', { class: `rb-chip rb-${r.state}` }, r.state) : null));
    if (!key) {
      bar.append(h('span', { class: 'rb-muted' },
        'Read-only. Open your personal link to comment and suggest edits.'));
    } else if (S.error) {
      bar.append(h('span', { class: 'rb-err' }, S.error));
    } else if (S.me) {
      bar.append(h('span', { class: 'rb-muted rb-you' }, who(S.me.name),
        isOwner() ? ' (owner)' : ''));
      const later = S.me.rounds.filter((x) => x.round > meta.round);
      if (later.length) bar.append(h('span', { class: 'rb-err' },
        `A newer round (${later[later.length - 1].round}) exists; reload the page.`));
    }
    const open = S.items.filter((i) => i.kind !== 'reply' && i.status === 'open').length;
    bar.append(...[h('span', { class: 'rb-spacer' }),
      h('label', { class: 'rb-muted' }, 'Theme ',
        h('select', { 'aria-label': 'Colour theme', onchange: (e) => setTheme(e.target.value) },
          THEMES.map(([v, t]) => h('option', { value: v, selected: theme() === v }, t)))),
      h('label', { class: 'rb-muted' },
        h('input', { type: 'checkbox', checked: S.inline, onchange: (e) => {
          S.inline = e.target.checked; store.set(LSINLINE, S.inline ? '1' : '0'); drawInline();
        } }), ' Edits in text'),
      key ? h('button', { class: 'rb-btn', type: 'button', onclick: () => setPanel(!S.panel) },
        S.panel ? 'Hide comments' : `Comments (${open} open)`) : null,
    ].filter(Boolean));
  }

  function threads() {
    const tops = S.items.filter((i) => i.kind !== 'reply');
    const f = S.filter;
    return tops.filter((i) => f === 'all' ? i.status !== 'withdrawn'
      : f === 'mine' ? S.me && i.author === S.me.id
      : i.status === f)
      .sort((x, y) => (order[x.block] ?? 1e9) - (order[y.block] ?? 1e9) || x.id - y.id);
  }

  function drawPanel() {
    const list = threads();
    const sel = h('select', { onchange: (e) => { S.filter = e.target.value; drawPanel(); } },
      [['open', 'Open'], ['accepted', 'Accepted'], ['rejected', 'Rejected'],
       ['mine', 'Mine'], ['all', 'All']].map(([v, t]) =>
        h('option', { value: v, selected: S.filter === v }, t)));
    panel.replaceChildren(
      h('div', { class: 'rb-phead' }, h('strong', {}, 'Comments and edits'), sel,
        h('button', { class: 'rb-btn rb-quiet', type: 'button', onclick: () => setPanel(false) }, '×')),
      canWrite() ? h('p', { class: 'rb-hint' },
        'Point at (or tap) a paragraph for 💬 comment or ✎ suggest edit, or select text to comment on it.') : null,
      ...(list.length ? list.map(card) : [h('p', { class: 'rb-muted' }, 'Nothing here.')]),
      h('p', { class: 'rb-foot' }, h('button', { class: 'rb-btn rb-quiet', type: 'button',
        onclick: () => { if (confirm('Forget your personal link in this browser?')) {
          store.del(LSKEY); location.reload(); } } }, 'Sign out of this browser')));
  }

  function card(it) {
    const replies = S.items.filter((r) => r.parent === it.id);
    const b = meta.blocks[it.block];
    const acts = [];
    if (canWrite() && it.author === S.me.id && it.status === 'open')
      acts.push(['withdraw', 'Withdraw']);
    if (isOwner() && it.status === 'open') acts.push(['accept', 'Accept'], ['reject', 'Reject']);
    if (isOwner() && (it.status === 'accepted' || it.status === 'rejected')) acts.push(['reopen', 'Reopen']);
    const reply = h('textarea', { rows: 2, placeholder: 'Reply…' });
    return h('article', { class: `rb-card rb-s-${it.status}`, id: `rb-item-${it.id}` },
      h('header', {}, who(it.author_name),
        h('span', { class: 'rb-muted' }, ` · ${isDeletion(it) ? 'suggested deletion' : it.kind === 'suggestion' ? 'suggested edit' : 'comment'}`
          + ` · ${when(it.created)}`),
        h('span', { class: `rb-chip rb-${it.status}` }, it.status),
        it.decided_by_name && it.status !== 'withdrawn'
          ? h('span', { class: 'rb-muted' }, ` by ${it.decided_by_name}`) : null),
      h('a', { href: `#${it.block}`, class: 'rb-where', onclick: (e) => { e.preventDefault(); focusBlock(it.block); } },
        b ? `${b.f}:${b.a}–${b.b}` : it.block),
      it.quote ? h('blockquote', {}, it.quote) : null,
      it.kind === 'suggestion' ? diffView(it.orig_src, it.new_src) : null,
      it.body ? h('p', { class: 'rb-body' }, it.body) : null,
      replies.map((r) => h('div', { class: 'rb-reply' }, who(r.author_name),
        h('span', { class: 'rb-muted' }, ` · ${when(r.created)}`), h('p', { class: 'rb-body' }, r.body))),
      canWrite() ? h('div', { class: 'rb-replybox' }, reply,
        h('button', { class: 'rb-btn', type: 'button', onclick: () => send(
          { kind: 'reply', parent: it.id, body: reply.value }, reply) }, 'Reply')) : null,
      acts.length ? h('div', { class: 'rb-acts' }, acts.map(([a, t]) =>
        h('button', { class: `rb-btn rb-${a}`, type: 'button', onclick: () => decide(it.id, a) }, t))) : null);
  }

  // ------------------------------------------------------------ blocks
  function focusBlock(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('rb-flash');
    setTimeout(() => el.classList.remove('rb-flash'), 1600);
  }

  function openThread(id) {
    if (!S.panel) setPanel(true);
    if (S.filter !== 'all') { S.filter = 'all'; drawPanel(); }
    const c = document.getElementById(`rb-item-${id}`);
    if (c) { c.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); c.classList.add('rb-flash');
      setTimeout(() => c.classList.remove('rb-flash'), 1600); }
  }

  function drawTools() {
    for (const el of document.querySelectorAll('div.rb')) {
      el.querySelector(':scope > .rb-tools')?.remove();
      const n = S.items.filter((i) => i.block === el.id && i.kind !== 'reply' && i.status === 'open').length;
      const tools = h('div', { class: 'rb-ui rb-tools' },
        n ? h('button', { class: 'rb-badge', type: 'button', title: `${n} open`,
          onclick: () => { S.filter = 'open'; setPanel(true); drawPanel();
            const first = S.items.find((i) => i.block === el.id && i.kind !== 'reply' && i.status === 'open');
            if (first) openThread(first.id); } }, String(n)) : null,
        canWrite() ? h('button', { class: 'rb-tool', type: 'button', title: 'Comment on this paragraph',
          onclick: () => compose(el, 'comment', '') }, '💬') : null,
        canWrite() ? h('button', { class: 'rb-tool', type: 'button', title: 'Suggest an edit',
          onclick: () => compose(el, 'suggestion') }, '✎') : null,
        canWrite() ? h('button', { class: 'rb-tool', type: 'button', title: 'Suggest deleting this block',
          onclick: () => compose(el, 'deletion') }, '🗑') : null);
      el.prepend(tools);
    }
  }

  function drawInline() {
    for (const x of document.querySelectorAll('.rb-inline')) x.remove();
    if (!S.inline) return;
    for (const it of S.items) {
      if (it.kind !== 'suggestion' || !['open', 'accepted'].includes(it.status)) continue;
      const el = document.getElementById(it.block);
      if (!el) continue;
      let box = el.nextElementSibling;
      if (!box || !box.classList.contains('rb-inline')) {
        box = h('div', { class: 'rb-ui rb-inline' });
        el.after(box);
      }
      box.append(h('div', { class: `rb-sugg rb-s-${it.status}`, style: `--who:${hue(it.author_name)}` },
        h('div', { class: 'rb-sugg-head' }, who(it.author_name),
          isDeletion(it) ? h('span', { class: 'rb-muted' }, 'delete this block') : null,
          h('span', { class: `rb-chip rb-${it.status}` }, it.status),
          h('button', { class: 'rb-btn rb-quiet', type: 'button', onclick: () => openThread(it.id) }, 'Discuss')),
        diffView(it.orig_src, it.new_src)));
    }
  }

  function compose(el, kind, quote) {
    document.querySelector('.rb-compose')?.remove();
    const b = meta.blocks[el.id];
    const note = h('textarea', { rows: 3, placeholder: kind === 'comment' ? 'Your comment…' : 'Why (optional)…' });
    let edit = null, preview = null;
    if (kind === 'deletion') preview = h('div', { class: 'rb-preview' }, diffView(b.src, ''));
    if (kind === 'suggestion') {
      edit = h('textarea', { class: 'rb-src', spellcheck: 'true',
        rows: Math.min(24, b.src.split('\n').length + 2) }, b.src);
      preview = h('div', { class: 'rb-preview' });
      const upd = () => preview.replaceChildren(edit.value === b.src
        ? h('span', { class: 'rb-muted' }, 'Edit the markdown above; your changes show here as tracked changes.')
        : diffView(b.src, edit.value));
      edit.addEventListener('input', upd);
      upd();
    }
    const title = { comment: 'Comment', suggestion: 'Suggest an edit', deletion: 'Suggest deleting this block' };
    const box = h('div', { class: 'rb-ui rb-compose', 'data-block': el.id },
      h('div', { class: 'rb-muted' }, title[kind], ` · ${b.f}, lines ${b.a}–${b.b}`),
      h('div', { class: 'rb-also' }),
      quote ? h('blockquote', {}, quote) : null,
      edit, preview,
      edit ? h('details', { class: 'rb-help-mini' }, h('summary', {}, 'Markdown help'), helpView()) : null,
      note,
      h('div', { class: 'rb-acts' },
        h('button', { class: 'rb-btn rb-accept', type: 'button', onclick: async () => {
          const body = kind === 'comment'
            ? { kind, block: el.id, quote: quote || '', body: note.value }
            : { kind: 'suggestion', block: el.id, orig_src: b.src,
                new_src: edit ? edit.value : '', body: note.value };
          if (await send(body)) { box.remove(); presenceSoon(); }
        } }, 'Save'),
        h('button', { class: 'rb-btn rb-quiet', type: 'button',
          onclick: () => { box.remove(); presenceSoon(); } }, 'Cancel')));
    el.after(box);
    (edit || note).focus();
    drawPresence();
    presenceSoon();
  }

  // ------------------------------------------------------------ side rail
  // Shown where the screen leaves room left of the text (review.css).
  const HELP = [
    ['Text', [
      ['*italic*', h('em', {}, 'italic')],
      ['**bold**', h('strong', {}, 'bold')],
      ['`code`', h('code', {}, 'code')],
      ['H~2~O  x^2^', ['H', h('sub', {}, '2'), 'O  x', h('sup', {}, '2')]],
      ['--  ---', 'en dash –, em dash —'],
    ]],
    ['Maths (LaTeX)', [
      ['$\\sigma^2_A$', 'inline maths'],
      ['$$ … $$', 'displayed equation'],
      ['$5{,}000$', 'comma in a number'],
      ['$\\text{CH}_4$', 'upright text'],
    ]],
    ['Citations', [
      ['[@key]', '(Author Year)'],
      ['[@a; @b]', 'two in one bracket'],
      ['@key', 'Author (Year)'],
      ['[-@key]', '(Year)'],
    ]],
    ['Paragraphs', [
      ['blank line', 'starts a new paragraph'],
      ['line breaks', 'free; text is re-wrapped'],
      ['- item', 'list (blank line before)'],
      ['^[text]', 'footnote'],
    ]],
  ];
  const helpView = () => h('div', { class: 'rb-help' }, HELP.map(([t, rows]) => [
    h('div', { class: 'rb-help-h' }, t),
    rows.map(([c, r]) => [h('code', { class: 'rb-help-c' }, c),
      // copies: the rail and an open editor each show the table
      h('span', { class: 'rb-help-r' }, [r].flat().map((x) => x instanceof Node ? x.cloneNode(true) : x))])]));

  const rail = h('aside', { id: 'rb-rail', class: 'rb-ui', 'aria-label': 'Contents and help' });
  document.body.append(rail);
  const heads = [...document.querySelectorAll('h2[id], h3[id]')].filter((x) => !x.closest('.rb-ui'));
  const toc = new Map();       // heading id -> link
  const parentOf = {};         // heading id -> id of its h2 (itself for an h2)
  { let h2 = null;
    for (const hd of heads) { if (hd.tagName === 'H2') h2 = hd.id; parentOf[hd.id] = h2 || hd.id; } }

  // Visible text only: MathML keeps the LaTeX source in <annotation>.
  function plainText(el) {
    const c = el.cloneNode(true);
    c.querySelectorAll('annotation, .rb-ui').forEach((x) => x.remove());
    return c.textContent.replace(/\s+/g, ' ').trim();
  }

  function sectionOf(blockId) {
    // the last h2/h3 before the block, in document order
    const el = document.getElementById(blockId);
    let sec = null;
    for (const hd of heads) {
      if (el && hd.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) sec = hd.id;
      else break;
    }
    return sec;
  }

  function insertCite(k) {
    const t = document.querySelector('.rb-compose textarea.rb-src');
    const txt = `[@${k}]`;
    if (t) {
      const a = t.selectionStart, b = t.selectionEnd;
      t.setRangeText(txt, a, b, 'end');
      t.dispatchEvent(new Event('input'));
      t.focus();
      return 'inserted';
    }
    navigator.clipboard?.writeText(txt);
    return 'copied';
  }

  function drawRail() {
    const counts = {};
    for (const it of S.items) {
      if (it.kind === 'reply' || it.status !== 'open') continue;
      const sec = sectionOf(it.block);
      if (!sec) continue;
      counts[sec] = (counts[sec] || 0) + 1;
      if (parentOf[sec] !== sec) counts[`h2:${parentOf[sec]}`] = (counts[`h2:${parentOf[sec]}`] || 0) + 1;
    }
    const nav = h('nav', { class: 'rb-toc' }, h('div', { class: 'rb-rail-h' }, 'Contents'),
      heads.map((hd) => {
        const a = h('a', { href: `#${hd.id}`, class: `rb-toc-${hd.tagName.toLowerCase()}`,
          onclick: (e) => { e.preventDefault(); hd.scrollIntoView({ behavior: 'smooth' });
            history.replaceState(null, '', `#${hd.id}`); } },
          plainText(hd), (() => {
            // an h2 counts its subsections too
            const n = (counts[hd.id] || 0) + (hd.tagName === 'H2' ? counts[`h2:${hd.id}`] || 0 : 0);
            return n ? h('span', { class: 'rb-toc-n' }, n) : null;
          })());
        toc.set(hd.id, a);
        return a;
      }));
    const parts = [nav];
    if (canWrite()) {
      parts.push(h('details', { class: 'rb-rail-sec', open: store.get('coauthors:help') !== '0',
        ontoggle: (e) => store.set('coauthors:help', e.target.open ? '1' : '0') },
        h('summary', { class: 'rb-rail-h' }, 'Markdown help'), helpView()));
      if (meta.cites && meta.cites.length) {
        const list = h('div', { class: 'rb-cites' });
        const fill = (q) => list.replaceChildren(...meta.cites
          .filter(([k, l]) => !q || (k + ' ' + l).toLowerCase().includes(q.toLowerCase()))
          .map(([k, l]) => h('button', { type: 'button', class: 'rb-cite', title: `Insert [@${k}]`,
            onclick: (e) => { const how = insertCite(k); const b = e.currentTarget;
              b.dataset.done = how; setTimeout(() => delete b.dataset.done, 1200); } },
            h('code', {}, `@${k}`), h('span', {}, l))));
        fill('');
        parts.push(h('details', { class: 'rb-rail-sec' },
          h('summary', { class: 'rb-rail-h' }, `Citation keys (${meta.cites.length})`),
          h('input', { type: 'search', placeholder: 'Filter…', oninput: (e) => fill(e.target.value) }),
          list));
      }
    }
    // keep the open/closed state of sections across refreshes
    const was = [...rail.querySelectorAll('details')].map((d) => d.open);
    rail.replaceChildren(...parts);
    if (was.length) rail.querySelectorAll('details').forEach((d, i) => { if (i < was.length) d.open = was[i]; });
    markCurrent();
  }

  function markCurrent() {
    let cur = null;
    const top = (parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--rb-bar-h')) || 50) + 24;
    for (const hd of heads) { if (hd.getBoundingClientRect().top <= top + 4) cur = hd.id; else break; }
    // subsections are listed only under the section being read
    for (const [id, a] of toc) {
      a.classList.toggle('rb-cur', id === cur);
      a.classList.toggle('rb-show', !!cur && parentOf[id] === parentOf[cur]);
    }
    const a = cur && toc.get(cur);
    if (a && rail.offsetParent) {
      const r = a.getBoundingClientRect(), rr = rail.getBoundingClientRect();
      if (r.top < rr.top || r.bottom > rr.bottom) a.scrollIntoView({ block: 'nearest' });
    }
  }
  let railTick = false;
  addEventListener('scroll', () => {
    if (railTick) return;
    railTick = true;
    requestAnimationFrame(() => { railTick = false; markCurrent(); });
  }, { passive: true });

  // ------------------------------------------------------------ selection
  function selectionBlock() {
    const s = getSelection();
    if (!s || s.isCollapsed || !s.rangeCount) return null;
    const r = s.getRangeAt(0);
    const a = r.startContainer.parentElement?.closest('div.rb');
    const b = r.endContainer.parentElement?.closest('div.rb');
    if (!a || a !== b || r.startContainer.parentElement.closest('.rb-ui')) return null;
    const text = s.toString().trim();
    return text ? { el: a, text: text.slice(0, 2000), rect: r.getBoundingClientRect() } : null;
  }
  let selTimer = 0;
  document.addEventListener('selectionchange', () => {
    clearTimeout(selTimer);
    selTimer = setTimeout(() => {
      const sb = canWrite() && selectionBlock();
      if (!sb) { selBtn.hidden = true; return; }
      selBtn.hidden = false;
      selBtn.style.top = `${scrollY + sb.rect.bottom + 6}px`;
      selBtn.style.left = `${Math.max(8, Math.min(scrollX + sb.rect.left, scrollX + innerWidth - 200))}px`;
      selBtn.onclick = () => { selBtn.hidden = true; compose(sb.el, 'comment', sb.text); };
    }, 200);
  });

  // Touch screens have no hover: a tap marks the block as active.
  document.addEventListener('click', (e) => {
    const el = e.target.closest('div.rb');
    if (e.target.closest('a, button, textarea, input, select')) return;
    for (const x of document.querySelectorAll('div.rb.rb-active')) if (x !== el) x.classList.remove('rb-active');
    el?.classList.add('rb-active');
  });

  // ------------------------------------------------------------ data
  async function send(body, field) {
    try {
      await api('POST', `/rounds/${meta.round}/items`, body);
      if (field) field.value = '';
      await refresh(true);
      return true;
    } catch (e) { alert(`Not saved: ${e.message}`); return false; }
  }

  async function decide(id, action) {
    if (action === 'withdraw' && !confirm('Withdraw this item?')) return;
    try { await api('POST', `/items/${id}`, { action }); await refresh(true); }
    catch (e) { alert(e.message); }
  }

  const busy = () => [...document.querySelectorAll('.rb-ui textarea')].some((t) => t.value.trim());

  async function refresh(force) {
    if (!key || (!force && busy())) return;
    try {
      if (!S.me) S.me = await api('GET', '/me');
      const d = await api('GET', `/rounds/${meta.round}`);
      S.round = d.round; S.items = d.items; S.error = null;
    } catch (e) {
      S.error = /key/.test(e.message) ? 'Your link is not valid for this paper (revoked or mistyped).'
        : `Comments unavailable: ${e.message}`;
    }
    drawBar(); drawPanel(); drawTools(); drawInline(); paintHighlights(); drawRail(); drawPresence();
  }

  // ------------------------------------------------------------ presence
  // Like Word's co-authoring flags: where each co-author is reading or
  // editing, refreshed every 10 s. The server keeps positions in memory only.
  const P = { others: [], sent: '' };
  const blocks = [...document.querySelectorAll('div.rb')];

  function myPosition() {
    const box = document.querySelector('.rb-compose');
    if (box) return { block: box.dataset.block, mode: 'editing' };
    if (document.hidden) return { block: null, mode: 'reading' };
    // the block crossing a line a third of the way down the text area
    const bar = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--rb-bar-h')) || 50;
    const y = bar + (innerHeight - bar) / 3;
    let cur = null;
    for (const el of blocks) { if (el.getBoundingClientRect().top <= y) cur = el.id; else break; }
    return { block: cur, mode: 'reading' };
  }

  async function heartbeat(force) {
    if (!key || !S.me) return;
    const pos = myPosition();
    const sig = `${pos.block}|${pos.mode}`;
    if (!force && sig === P.sent) return;
    P.sent = sig;
    try {
      P.others = (await api('POST', `/rounds/${meta.round}/presence`, pos)).others;
    } catch { P.others = []; }
    drawPresence();
  }
  let pTimer = 0;
  const presenceSoon = () => { clearTimeout(pTimer); pTimer = setTimeout(() => heartbeat(false), 800); };

  const initials = (name) => name.split(/\s+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join('').toUpperCase();
  function drawPresence() {
    for (const x of document.querySelectorAll('.rb-here, .rb-toc-p')) x.remove();
    const at = {};
    for (const o of P.others) (at[o.block] ||= []).push(o);
    for (const [id, os] of Object.entries(at)) {
      const el = document.getElementById(id);
      if (!el) continue;
      el.prepend(h('div', { class: 'rb-ui rb-here' }, os.map((o) =>
        h('span', { class: `rb-face rb-${o.mode}`, style: `--who:${hue(o.name)}`,
          title: `${o.name} is ${o.mode} here` }, o.mode === 'editing' ? '✎' : '', initials(o.name)))));
      // contents rail: faces at the section, and at its h2 when it is an h3
      const sec = sectionOf(id);
      for (const s of new Set([sec, sec && parentOf[sec]])) {
        const a = s && toc.get(s);
        if (!a) continue;
        let p = a.querySelector('.rb-toc-p');
        if (!p) { p = h('span', { class: 'rb-toc-p' }); a.insertBefore(p, a.querySelector('.rb-toc-n')); }
        for (const o of os) p.append(h('span', { class: 'rb-dot', style: `--who:${hue(o.name)}`, title: o.name }));
      }
    }
    for (const box of document.querySelectorAll('.rb-compose')) {
      const eds = (at[box.dataset.block] || []).filter((o) => o.mode === 'editing');
      box.querySelector('.rb-also')?.replaceChildren(...(eds.length ? [h('span', { class: 'rb-err' },
        `${eds.map((o) => o.name).join(', ')} ${eds.length > 1 ? 'are' : 'is'} also editing this block.`)] : []));
    }
  }

  drawBar();
  drawTools();
  drawRail();
  if (key) {
    refresh(true).then(() => heartbeat(true));
    setInterval(refresh, 60000);
    setInterval(() => { if (!document.hidden) heartbeat(true); }, 10000);
    addEventListener('scroll', presenceSoon, { passive: true });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) { refresh(); heartbeat(true); return; }
      // leaving: tell the others at once rather than after the timeout
      P.sent = 'null|reading';
      fetch(`${meta.api}/${meta.paper}/rounds/${meta.round}/presence`, { method: 'POST', keepalive: true,
        headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ block: null, mode: 'reading' }) }).catch(() => {});
    });
  }
})();
