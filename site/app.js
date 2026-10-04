(() => {
  'use strict';

  // ---------- utilities ----------
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem('sc:' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem('sc:' + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };
  const DOW = ['日', '月', '火', '水', '木', '金', '土'];
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const parseDate = (d) => { const [y, m, day] = d.split('-').map(Number); return new Date(y, m - 1, day); };
  const jaDate = (d) => { const x = parseDate(d); return `${x.getMonth() + 1}月${x.getDate()}日(${DOW[x.getDay()]})`; };
  const fmtTime = (s) => { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const fmtMin = (s) => (s ? `${Math.round(s / 60)}分` : '');
  const absUrl = (u) => new URL(u, location.href).href;

  let toastTimer;
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg; t.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2400);
  }

  // ---------- settings ----------
  const SPEEDS = [0.8, 0.9, 1.0, 1.1, 1.25, 1.5];
  const settings = {
    mode: store.get('mode', 'both'),
    size: store.get('size', 'm'),
    follow: store.get('follow', true),
    speed: store.get('speed', 1.0),
  };
  function applySettings() {
    document.body.dataset.mode = settings.mode;
    document.documentElement.dataset.size = settings.size;
    audio.playbackRate = settings.speed;
    audio.defaultPlaybackRate = settings.speed;
    $('#p-speed').textContent = (Number.isInteger(settings.speed) ? settings.speed.toFixed(1) : String(settings.speed)) + '×';
  }

  // ---------- state ----------
  const audio = $('#audio');
  const player = $('#player');
  const view = $('#view');
  const state = { index: null, cache: new Map(), playing: null, lines: [], active: -1, userScrollAt: 0, tab: 'script', viewDate: null };

  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) throw new Error(r.status + ' ' + url);
    return r.json();
  }
  async function loadIndex() {
    if (!state.index) state.index = await getJSON('data/index.json');
    return state.index;
  }
  async function loadEpisode(date) {
    if (!state.cache.has(date)) state.cache.set(date, await getJSON(`data/episodes/${date}.json`));
    return state.cache.get(date);
  }

  // ---------- routing ----------
  async function route() {
    const m = location.hash.match(/^#\/ep\/(\d{4}-\d{2}-\d{2})/);
    try {
      if (m) await renderEpisode(m[1]);
      else await renderHome();
    } catch (e) {
      console.error(e);
      view.innerHTML = `<div class="empty"><p>読み込めませんでした。</p><p><small>${esc(e.message)}</small></p><p><a class="btn" href="#/">ホームへ</a></p></div>`;
    }
    view.focus({ preventScroll: true });
  }
  window.addEventListener('hashchange', route);

  // ---------- home ----------
  async function renderHome() {
    state.viewDate = null; state.lines = []; state.active = -1;
    const idx = await loadIndex();
    const eps = idx.episodes || [];
    document.title = 'The Silicon Commute';
    if (!eps.length) {
      view.innerHTML = `<div class="empty"><h2>まだエピソードがありません</h2>
        <p>平日の朝6時ごろに最初の回が配信されます。<br>GitHub の Actions タブから「Run workflow」で今すぐ作ることもできます。</p></div>` + subscribeHtml(idx);
      bindCopy();
      return;
    }
    const [latest, ...older] = eps;
    view.innerHTML = `
      <article class="hero">
        <div class="eyebrow">最新 · ${esc(jaDate(latest.date))}${latest.duration ? ' · ' + fmtMin(latest.duration) : ''}</div>
        <h1>${esc(latest.title_en)}</h1>
        <p class="ja-title">${esc(latest.title_ja)}</p>
        <p class="summary">${esc(latest.summary_ja)}</p>
        <ul class="chips">${(latest.stories || []).map((s) => `<li class="${s.section === 'world' ? 'world' : ''}">${s.section === 'world' ? '🌏 ' : ''}${esc(s.title_ja || s.title_en)}</li>`).join('')}</ul>
        <div class="btn-row">
          ${latest.audio_url ? `<button class="btn primary" data-play="${latest.date}">▶ 再生する</button>` : ''}
          <a class="btn" href="#/ep/${latest.date}">スクリプトを読む</a>
        </div>
      </article>
      ${older.length ? `<h2 class="section">これまでのエピソード</h2>
      <ul class="ep-list">${older.map((e) => {
        const d = parseDate(e.date);
        return `<li><a href="#/ep/${e.date}">
          <span class="date-badge"><b>${d.getDate()}</b><small>${MON[d.getMonth()]} · ${DOW[d.getDay()]}</small></span>
          <span><span class="t-en">${esc(e.title_en)}</span><span class="t-ja">${esc(e.title_ja)}</span></span>
          <span class="dur">${e.audio_url ? fmtMin(e.duration) : 'テキスト'}</span></a></li>`;
      }).join('')}</ul>` : ''}
      ${subscribeHtml(idx)}`;
    view.querySelectorAll('[data-play]').forEach((b) => b.addEventListener('click', async () => {
      const ep = await loadEpisode(b.dataset.play);
      loadIntoPlayer(ep, true);
      location.hash = `#/ep/${ep.date}`;
    }));
    bindCopy();
  }

  function subscribeHtml(idx) {
    const feed = idx.feed_url || absUrl('feed.xml');
    return `<section class="subscribe">
      <b>🎧 Podcastアプリで聴く（通勤におすすめ）</b>
      <code id="feed-url">${esc(feed)}</code>
      <div class="btn-row"><button class="btn small" id="copy-feed">URLをコピー</button></div>
      <ol>
        <li>iPhoneの「Podcast」アプリ →「ライブラリ」→ 右上の「…」</li>
        <li>「URLで番組をフォロー」に上のURLを貼り付け</li>
        <li>毎朝の新しい回が自動でダウンロードされます</li>
      </ol>
      <p class="hint">英日対訳・単語はこのアプリで。ホーム画面に追加（Safariの共有 →「ホーム画面に追加」）するとアプリのように使えます。</p>
    </section>`;
  }
  function bindCopy() {
    const b = $('#copy-feed');
    if (!b) return;
    b.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText($('#feed-url').textContent); toast('コピーしました'); }
      catch { toast('コピーできませんでした。長押しで選択してください'); }
    });
  }

  // ---------- episode ----------
  async function renderEpisode(date) {
    const ep = await loadEpisode(date);
    state.viewDate = date;
    document.title = `${ep.title_en} — The Silicon Commute`;
    const hosts = ep.hosts || ['Alex', 'Mika'];
    // put this episode in the player unless another one is currently playing
    if (ep.audio_url && (!state.playing || (state.playing.date !== date && audio.paused))) loadIntoPlayer(ep, false);

    let n = 0;
    const KIND = { intro: 'Intro', story: 'Tech', world: 'World', ceo_watch: 'CEO Watch', phrase: 'Phrase', outro: 'Wrap-up', recap: '日本語' };
    const script = ep.segments.map((seg, si) => {
      const ja = seg.lang === 'ja';
      const head = ja
        ? `<div class="seg-head recap-head"><span class="kind">🇯🇵 ${esc(seg.heading_ja || '日本語でおさらい')}</span></div>`
        : `<div class="seg-head"><h3>${esc(seg.heading_en)}<small>${esc(seg.heading_ja)}</small></h3>
            <span class="kind k-${esc(seg.kind)}">${esc(KIND[seg.kind] || '')}</span></div>`;
      return `
      <section class="segment${ja ? ' recap' : ''}" data-seg="${si}">
        ${head}
        ${seg.lines.map((l) => {
          const s = Math.max(0, hosts.indexOf(l.speaker));
          const body = ja
            ? `<p class="ja-spoken" lang="ja">${esc(l.ja)}</p>`
            : `<p class="en" lang="en">${esc(l.en)}</p><p class="ja">${esc(l.ja)}</p><button class="reveal" type="button">和訳</button>`;
          return `<div class="line${ja ? ' ja-line' : ''}" data-i="${n++}" data-t="${l.t ?? ''}">
            <span class="avatar s${s}" aria-hidden="true">${esc(l.speaker[0])}</span>
            <div><div class="who">${esc(l.speaker)}${ja ? ' · 日本語' : ''}</div>${body}</div>
          </div>`;
        }).join('')}
      </section>`;
    }).join('');

    const storySegs = ep.segments.map((s, i) => ({ s, i })).filter((x) => x.s.kind === 'story' || x.s.kind === 'world');
    const stories = (ep.stories || []).map((st, i) => `
      <article class="card">
        <span class="sec sec-${st.section === 'world' ? 'world' : 'tech'}">${st.section === 'world' ? '🌏 World' : '💻 Tech'}</span>
        <h3>${esc(st.title_ja)}</h3>
        <p class="sub" lang="en">${esc(st.title_en)}</p>
        <p>${esc(st.summary_ja)}</p>
        ${st.why_ja ? `<div class="why"><b>なぜ重要か</b>${esc(st.why_ja)}</div>` : ''}
        ${(st.sources || []).length ? `<ul class="sources">${st.sources.map((src) => `<li><span class="pub">${esc(src.publisher)}</span><a href="${esc(src.url)}" target="_blank" rel="noopener">${esc(src.title)}</a></li>`).join('')}</ul>` : ''}
        ${storySegs[i] && ep.audio_url ? `<div class="actions"><button class="btn small" data-seek-seg="${storySegs[i].i}">▶ この話を聴く</button></div>` : ''}
      </article>`).join('');

    const p = ep.phrase_of_the_day || {};
    const vocab = `
      ${p.phrase ? `<article class="card phrase"><div class="eyebrow">Phrase of the day</div>
        <div class="big" lang="en">${esc(p.phrase)}</div><p>${esc(p.meaning_ja)}</p>
        <p lang="en"><i>${esc(p.example_en)}</i></p><p class="sub">${esc(p.example_ja)}</p></article>` : ''}
      <ul class="vocab">${(ep.vocabulary || []).map((v) => `
        <li><span class="term" lang="en">${esc(v.term)}</span><span class="mean">${esc(v.meaning_ja)}</span>
        ${v.note_ja ? `<p class="note">${esc(v.note_ja)}</p>` : ''}
        <p class="ex" lang="en" data-ex="${esc(v.example_en)}" title="タップでその場面へ">${highlight(v.example_en, v.term)}</p></li>`).join('')}</ul>`;

    view.innerHTML = `
      <a class="back" href="#/">‹ エピソード一覧</a>
      <header class="ep-head">
        <div class="eyebrow">${esc(jaDate(ep.date))}${ep.duration ? ' · ' + fmtMin(ep.duration) : ''}</div>
        <h1 lang="en">${esc(ep.title_en)}</h1>
        <p class="ja-title">${esc(ep.title_ja)}</p>
      </header>
      <nav class="tabs" role="tablist">
        <button role="tab" data-tab="script">スクリプト</button>
        <button role="tab" data-tab="stories">ニュース</button>
        <button role="tab" data-tab="vocab">単語</button>
      </nav>
      ${ep.audio_url ? '' : '<p class="no-audio-note">この回は音声の生成に失敗したため、テキストのみです。</p>'}
      <div data-panel="script">${script}</div>
      <div data-panel="stories" hidden>${stories}</div>
      <div data-panel="vocab" hidden>${vocab}</div>`;

    state.lines = [...view.querySelectorAll('.line')].map((el) => ({ el, t: parseFloat(el.dataset.t) }));
    state.active = -1;
    setTab(state.tab);

    view.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
    view.querySelector('[data-panel="script"]').addEventListener('click', (e) => {
      const rev = e.target.closest('.reveal');
      if (rev) { rev.previousElementSibling.classList.add('shown'); return; }
      const line = e.target.closest('.line');
      if (line && !isNaN(parseFloat(line.dataset.t))) playEpisodeAt(ep, parseFloat(line.dataset.t));
    });
    view.querySelectorAll('[data-seek-seg]').forEach((b) => b.addEventListener('click', () => {
      const seg = ep.segments[+b.dataset.seekSeg];
      setTab('script');
      playEpisodeAt(ep, seg.t ?? seg.lines[0].t ?? 0);
      scrollToLine(view.querySelector(`[data-seg="${b.dataset.seekSeg}"] .line`));
    }));
    view.querySelectorAll('[data-ex]').forEach((el) => el.addEventListener('click', () => {
      const key = el.dataset.ex.slice(0, 40).toLowerCase();
      const hit = state.lines.find((l) => $('.en', l.el).textContent.toLowerCase().includes(key));
      if (!hit) return;
      setTab('script');
      scrollToLine(hit.el);
      if (!isNaN(hit.t)) playEpisodeAt(ep, hit.t);
    }));
    syncActive(true);
  }

  function highlight(text, term) {
    const t = esc(text);
    const words = String(term || '').split(/\s+/).filter((w) => w.length > 2).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!words.length) return t;
    const stem = words.map((w) => w.replace(/(e|s|ed|ing)$/i, '')).join('\\w*\\s+');
    try { return t.replace(new RegExp(`(${stem}\\w*)`, 'i'), '<mark>$1</mark>'); } catch { return t; }
  }

  function setTab(tab) {
    state.tab = tab;
    view.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
    view.querySelectorAll('[data-panel]').forEach((p) => { p.hidden = p.dataset.panel !== tab; });
  }

  function scrollToLine(el) {
    if (!el) return;
    state.userScrollAt = 0;
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  // ---------- player ----------
  function mediaSession(ep) {
    if (!('mediaSession' in navigator)) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: ep.title_en, artist: 'The Silicon Commute', album: jaDate(ep.date),
        artwork: [{ src: absUrl('icons/cover.png'), sizes: '1600x1600', type: 'image/png' }],
      });
      const h = navigator.mediaSession.setActionHandler.bind(navigator.mediaSession);
      h('play', () => audio.play());
      h('pause', () => audio.pause());
      h('seekbackward', () => skip(-15));
      h('seekforward', () => skip(15));
      h('seekto', (d) => { audio.currentTime = d.seekTime; });
    } catch { /* optional */ }
  }

  function loadIntoPlayer(ep, autoplay, startAt) {
    if (!ep.audio_url) return;
    if (!state.playing || state.playing.date !== ep.date) {
      state.playing = ep;
      audio.src = absUrl(ep.audio_url);
      audio.playbackRate = settings.speed;
      const saved = store.get('pos:' + ep.date, 0);
      const start = startAt ?? (saved > 5 && (!ep.duration || saved < ep.duration - 10) ? saved : 0);
      audio.addEventListener('loadedmetadata', () => { if (start) audio.currentTime = start; audio.playbackRate = settings.speed; }, { once: true });
      $('#p-title').textContent = `${jaDate(ep.date)} · ${ep.title_en}`;
      $('#p-dur').textContent = fmtTime(ep.duration);
      player.hidden = false;
      mediaSession(ep);
      updateSaved();
    } else if (startAt !== undefined) {
      audio.currentTime = startAt;
    }
    if (autoplay) audio.play().catch(() => toast('再生ボタンを押してください'));
  }

  function playEpisodeAt(ep, t) {
    if (!ep.audio_url) return;
    if (!state.playing || state.playing.date !== ep.date) loadIntoPlayer(ep, true, t);
    else { audio.currentTime = t; audio.play().catch(() => {}); }
  }

  function skip(sec) { audio.currentTime = Math.max(0, Math.min((audio.duration || 1e9) - 0.5, audio.currentTime + sec)); }

  $('#p-play').addEventListener('click', () => (audio.paused ? audio.play() : audio.pause()));
  $('#p-back').addEventListener('click', () => skip(-15));
  $('#p-fwd').addEventListener('click', () => skip(15));
  $('#p-speed').addEventListener('click', () => {
    const i = SPEEDS.indexOf(settings.speed);
    settings.speed = SPEEDS[(i + 1) % SPEEDS.length];
    store.set('speed', settings.speed);
    applySettings();
    toast(`再生速度 ${settings.speed}×`);
  });

  const seek = $('#p-seek');
  let dragging = false;
  seek.addEventListener('input', () => { dragging = true; $('#p-cur').textContent = fmtTime(seek.value); });
  seek.addEventListener('change', () => { audio.currentTime = +seek.value; dragging = false; });

  audio.addEventListener('loadedmetadata', () => { seek.max = audio.duration; $('#p-dur').textContent = fmtTime(audio.duration); });
  audio.addEventListener('play', () => player.classList.add('playing'));
  audio.addEventListener('pause', () => { player.classList.remove('playing'); savePos(); });
  audio.addEventListener('ended', () => { if (state.playing) store.set('pos:' + state.playing.date, 0); });
  let lastSave = 0;
  audio.addEventListener('timeupdate', () => {
    if (!dragging) { seek.value = audio.currentTime; $('#p-cur').textContent = fmtTime(audio.currentTime); }
    if (Date.now() - lastSave > 5000) savePos();
    syncActive(false);
  });
  function savePos() { lastSave = Date.now(); if (state.playing) store.set('pos:' + state.playing.date, audio.currentTime); }

  // highlight the line being spoken
  function syncActive(force) {
    if (!state.lines.length || !state.playing || state.playing.date !== state.viewDate) return;
    const t = audio.currentTime + 0.2;
    let lo = 0, hi = state.lines.length - 1, idx = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (state.lines[mid].t <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
    if (idx === state.active && !force) return;
    if (state.lines[state.active]) state.lines[state.active].el.classList.remove('active');
    state.active = idx;
    const cur = state.lines[idx];
    if (!cur) return;
    cur.el.classList.add('active');
    if (settings.follow && !audio.paused && state.tab === 'script' && Date.now() - state.userScrollAt > 4000) {
      cur.el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }
  ['wheel', 'touchmove'].forEach((ev) => window.addEventListener(ev, () => { state.userScrollAt = Date.now(); }, { passive: true }));

  // offline save (Cache API; the service worker serves it with range support)
  async function updateSaved() {
    const b = $('#p-save');
    if (!('caches' in window) || !state.playing) return;
    try {
      const hit = await caches.match(absUrl(state.playing.audio_url));
      b.classList.toggle('saved', !!hit);
      b.setAttribute('aria-label', hit ? 'オフライン保存済み' : 'オフライン保存');
    } catch { /* ignore */ }
  }
  $('#p-save').addEventListener('click', async () => {
    if (!('caches' in window) || !state.playing) return toast('このブラウザでは保存できません');
    const url = absUrl(state.playing.audio_url);
    if (new URL(url).origin !== location.origin) return toast('この回はオフライン保存に対応していません');
    try {
      toast('保存中…');
      const c = await caches.open('sc-audio');
      await c.add(url);
      const d = await caches.open('sc-data');
      await d.add(absUrl(`data/episodes/${state.playing.date}.json`));
      updateSaved();
      toast('オフラインで聴けるように保存しました');
    } catch { toast('保存に失敗しました'); }
  });

  // keyboard
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select, dialog') || player.hidden) return;
    if (e.code === 'Space') { e.preventDefault(); audio.paused ? audio.play() : audio.pause(); }
    if (e.code === 'ArrowLeft') skip(-5);
    if (e.code === 'ArrowRight') skip(5);
  });

  // ---------- settings dialog ----------
  const dlg = $('#settings');
  $('#btn-settings').addEventListener('click', () => {
    dlg.querySelector(`input[name="mode"][value="${settings.mode}"]`).checked = true;
    dlg.querySelector(`input[name="size"][value="${settings.size}"]`).checked = true;
    $('#follow').checked = settings.follow;
    dlg.showModal();
  });
  dlg.addEventListener('change', (e) => {
    const { name, value, checked } = e.target;
    if (name === 'mode' || name === 'size') { settings[name] = value; store.set(name, value); }
    if (e.target.id === 'follow') { settings.follow = checked; store.set('follow', checked); }
    applySettings();
  });

  // ---------- boot ----------
  applySettings();
  route();
  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    navigator.serviceWorker.register('sw.js').catch(() => {});
  }
})();
