(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const CATEGORIES = [
    '소프트웨어/SI', '건설/토목', '시설/유지관리', '물품/구매',
    '용역/컨설팅', '전기/통신', '의료/바이오',
  ];
  const REGIONS = [
    '서울', '경기', '인천', '부산', '대구', '광주', '대전', '울산', '세종',
    '강원', '충북', '충남', '전북', '전남', '경북', '경남', '제주', '전국',
  ];
  const QUICK = [
    { label: 'AI·데이터', patch: { q: 'AI' } },
    { label: '경기·건설', patch: { region: '경기', category: '건설/토목' } },
    { label: '3억 이상', patch: { min_amount: 300000000 } },
    { label: '마감 임박', patch: { days: 3 } },
  ];
  const STORE = {
    theme: 'bidradar.theme',
    favorites: 'bidradar.favorites',
    filters: 'bidradar.filters',
    alerts: 'bidradar.alerts',
  };
  const STAR = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m12 3.6 2.6 5.3 5.8.8-4.2 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.2-4.1 5.8-.8L12 3.6Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>';
  const PAPERCLIP = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m20 11.5-7.6 7.6a5 5 0 0 1-7.1-7.1l8-8a3.3 3.3 0 1 1 4.7 4.7l-8 8a1.6 1.6 0 1 1-2.3-2.3l7.2-7.2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  // 고객 콘솔(/app)에서는 서버가 window.BIDRADAR_API="/api/app" 를 심습니다.
  const API_BASE = window.BIDRADAR_API || '/api';
  const CUSTOMER = !!window.BIDRADAR_API;
  const apiUrl = (path) => API_BASE + String(path).replace(/^\/api/, '');

  const DISCORD_LOGO = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M19.7 5.4A17.3 17.3 0 0 0 15.4 4l-.2.4a16 16 0 0 1 3.8 1.9 15.6 15.6 0 0 0-13.9 0A16 16 0 0 1 8.9 4.4L8.6 4a17.3 17.3 0 0 0-4.3 1.4C1.5 9.6.8 13.7 1.1 17.7a17.4 17.4 0 0 0 5.2 2.6l1.1-1.8c-.6-.2-1.2-.5-1.7-.9l.4-.3a12.4 12.4 0 0 0 11.8 0l.4.3c-.5.4-1.1.7-1.7.9l1.1 1.8a17.4 17.4 0 0 0 5.2-2.6c.4-4.6-.8-8.7-3.2-12.3ZM8.7 15.2c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Zm6.6 0c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Z" fill="currentColor"/></svg>';

  const state = {
    filters: { q: '', region: '', category: '', min_amount: 0, days: 0, sort: 'close' },
    rawItems: [],
    total: 0,
    offset: 0,
    onlyFav: false,
    favorites: new Set(readJSON(STORE.favorites, [])),
    alerts: readJSON(STORE.alerts, []),
    notifyChannel: '',
    reqId: 0,
    detailId: null,
  };

  // localStorage를 막아둔 브라우저(사생활 보호 모드·인앱 브라우저)에서도
  // 화면이 멈추지 않도록 모든 접근을 안전하게 감쌉니다.
  const ls = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* blocked */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* blocked */ } },
  };

  function readJSON(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  }

  function writeJSON(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
  }

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));

  function fmtAmount(value) {
    const n = Number(value) || 0;
    if (n >= 1e12) return `${(n / 1e12).toFixed(2).replace(/\.?0+$/, '')}조`;
    if (n >= 1e8) {
      const eok = n / 1e8;
      return `${eok >= 10 ? Math.round(eok).toLocaleString('ko-KR') : eok.toFixed(1).replace(/\.0$/, '')}억`;
    }
    if (n >= 1e4) return `${Math.round(n / 1e4).toLocaleString('ko-KR')}만`;
    return n.toLocaleString('ko-KR');
  }

  function kst(ts) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(new Date(ts * 1000));
    const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
    return { md: `${map.month}.${map.day}`, hm: `${map.hour}:${map.minute}`, h: map.hour };
  }

  function relative(ts) {
    const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))}분 전`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
    return `${Math.floor(diff / 86400)}일 전`;
  }

  function dday(closeAt) {
    const diff = closeAt * 1000 - Date.now();
    if (diff <= 0) return { label: '마감', sub: '종료', cls: '' };
    const days = Math.ceil(diff / 86400000);
    if (days <= 3) return { label: `D-${days}`, sub: '마감 임박', cls: 'urgent' };
    if (days <= 7) return { label: `D-${days}`, sub: '이번 주', cls: 'soon' };
    return { label: `D-${days}`, sub: '접수 중', cls: '' };
  }

  function typeBadge(type) {
    const cls = type === '긴급공고' ? 'type-urgent' : type === '재공고' ? 'type-re' : 'type-normal';
    return `<span class="badge ${cls}">${esc(type)}</span>`;
  }

  function toast(message) {
    const el = $('#toast');
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.remove('show'), 2600);
  }

  function debounce(fn, wait) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  async function api(path) {
    const res = await fetch(apiUrl(path), { headers: { Accept: 'application/json' } });
    if (res.status === 401 && CUSTOMER) {
      location.href = '/login';
      throw new Error('로그인이 필요합니다');
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  /* ------------------------------------------------------------ setup selects */

  function fillSelect(select, values, placeholder) {
    const first = `<option value="">${placeholder}</option>`;
    select.innerHTML = first + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  }

  function setupControls() {
    fillSelect($('#region'), REGIONS, '전체 지역');
    fillSelect($('#category'), CATEGORIES, '전체 업종');
    fillSelect($('#alertRegion'), REGIONS, '전체');
    fillSelect($('#alertCategory'), CATEGORIES, '전체');

    // ?reset=1 로 열면 브라우저에 저장된 필터를 지웁니다. (빈 목록 응급 복구용)
    if (new URLSearchParams(location.search).get('reset') === '1') {
      ls.remove(STORE.filters);
    }
    const saved = readJSON(STORE.filters, null);
    if (saved && typeof saved === 'object') {
      state.filters = { ...state.filters, ...saved };
    }
    const f = state.filters;
    $('#q').value = f.q || '';
    $('#region').value = f.region || '';
    $('#category').value = f.category || '';
    $('#minAmount').value = String(f.min_amount || 0);
    $('#days').value = String(f.days || 0);
    $('#sort').value = f.sort || 'close';

    const forced = new URLSearchParams(location.search).get('theme');
    const theme = forced === 'dark' || forced === 'light'
      ? forced
      : ls.get(STORE.theme)
        || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;

    const filterCard = $('.filter-card');
    const filterToggle = $('#filterToggle');
    const mobile = window.matchMedia('(max-width: 640px)');
    const setCollapsed = (collapsed) => {
      filterCard.classList.toggle('collapsed', collapsed);
      filterToggle.textContent = collapsed ? '필터 열기' : '필터 접기';
      filterToggle.setAttribute('aria-expanded', String(!collapsed));
    };
    setCollapsed(mobile.matches);
    mobile.addEventListener('change', (event) => setCollapsed(event.matches));
    filterToggle.addEventListener('click', () => {
      setCollapsed(!filterCard.classList.contains('collapsed'));
    });
  }

  function syncFilters() {
    state.filters = {
      q: $('#q').value.trim(),
      region: $('#region').value,
      category: $('#category').value,
      min_amount: Number($('#minAmount').value) || 0,
      days: Number($('#days').value) || 0,
      sort: $('#sort').value,
    };
    writeJSON(STORE.filters, state.filters);
  }

  function applyPatch(patch) {
    const f = state.filters;
    if (patch.q !== undefined) f.q = patch.q;
    if (patch.region !== undefined) f.region = patch.region;
    if (patch.category !== undefined) f.category = patch.category;
    if (patch.min_amount !== undefined) f.min_amount = patch.min_amount;
    if (patch.days !== undefined) f.days = patch.days;
    if (patch.sort !== undefined) f.sort = patch.sort;
    $('#q').value = f.q || '';
    $('#region').value = f.region || '';
    $('#category').value = f.category || '';
    $('#minAmount').value = String(f.min_amount || 0);
    $('#days').value = String(f.days || 0);
    $('#sort').value = f.sort || 'close';
    writeJSON(STORE.filters, state.filters);
    renderChips();
    loadBids(true);
  }

  function resetAll() {
    applyPatch({ q: '', region: '', category: '', min_amount: 0, days: 0, sort: 'close' });
    $('#onlyFav').checked = false;
    state.onlyFav = false;
    toast('필터를 초기화했습니다.');
  }

  /* ------------------------------------------------------------ summary */

  function renderSummary(data) {
    const k = data.kpis;
    $('#kpiNew').textContent = `${k.new24}건`;
    const delta = $('#kpiDelta');
    const d = Number(k.new_delta) || 0;
    delta.textContent = `${d > 0 ? '+' : ''}${d}건`;
    delta.className = `delta ${d > 0 ? 'up' : d < 0 ? 'down' : 'flat'}`;

    $('#kpiClosing').textContent = `${k.closing3}건`;
    $('#kpiWeek').textContent = `${k.week_close}건`;
    const weekAmount = data.weekly.reduce((sum, day) => sum + (day.amount || 0), 0);
    $('#kpiWeekAmount').textContent = `마감 예정 ${fmtAmount(weekAmount)} 규모`;
    $('#kpiAmount').textContent = fmtAmount(k.open_amount);

    const sync = kst(data.last_sync);
    $('#lastSync').textContent = sync.hm;

    renderWeekly(data.weekly);
    renderCategories(data.categories);
    renderTelegram(data.alerts || []);
    applySource(data.source);
    applyAlertState(data.notify, data.telegram, data.bid_alerts);
  }

  function applyAlertState(notifyChannel, telegramReady, bidAlerts) {
    // 고객용 공고 알림(D-3/D-1)이 실제로 발송 중일 때만 LIVE로 표시합니다.
    // 상담 알림용 채널 설정만으로 LIVE라고 쓰면 거짓 표시가 됩니다.
    const ch = notifyChannel || (telegramReady ? 'telegram' : '');
    state.notifyChannel = ch;
    const title = $('#alertTitle');
    const sub = $('#alertSubtitle');
    const logo = $('#alertLogo');
    const badge = $('.live-dot');

    if (ch === 'discord') {
      if (title) title.textContent = '디스코드 알림';
      if (logo) logo.innerHTML = DISCORD_LOGO;
    } else if (ch === 'telegram') {
      if (title) title.textContent = '텔레그램 알림';
    }

    if (bidAlerts) {
      if (sub) sub.textContent = '마감 D-3 · D-1 자동 발송';
      if (badge) badge.textContent = 'LIVE';
      return;
    }
    if (title && !ch) title.textContent = '알림 미리보기';
    if (sub) sub.textContent = ch ? '공고 알림 예시 · D-3 / D-1' : '알림 예시 · 발송 설정 필요';
    if (badge) badge.textContent = 'PREVIEW';
  }

  function applySource(source) {
    // 고객 콘솔에서는 계정 배지를 그대로 둡니다.
    if (CUSTOMER) return;
    // live 모드(나라장터 실데이터)일 때 데모 표기를 실제 수집 표기로 바꿉니다.
    if (source !== 'live') return;
    $('.demo-badge')?.remove();
    const card = $('.demo-card');
    if (card) {
      card.innerHTML = `
        <div class="demo-card-title"><span class="demo-dot"></span> 실시간 수집</div>
        <p>나라장터 입찰공고정보서비스에서 수집한 <b>실제 공고</b>입니다. 수집 주기 15분, 마감이 지난 공고는 7일 뒤 정리됩니다.</p>
        <div class="source-grid">
          <div><b>15분</b><span>수집 주기</span></div>
          <div><b>5종</b><span>공고 유형</span></div>
          <div><b>원문</b><span>나라장터</span></div>
        </div>`;
    }
    const foot = $('.site-foot span');
    if (foot) foot.textContent = 'BidRadar — 입찰 공고 모니터링 (실시간 수집)';
  }

  function renderWeekly(weekly) {
    const max = Math.max(1, ...weekly.map((d) => d.n));
    const total = weekly.reduce((sum, d) => sum + d.n, 0);
    $('#weekTotal').textContent = `${total}건`;
    if (weekly.length) {
      $('#weekRange').textContent = `${weekly[0].date} — ${weekly[weekly.length - 1].date}`;
    }
    $('#weekBars').innerHTML = weekly.map((d) => {
      const height = Math.max(4, Math.round((d.n / max) * 100));
      return `<div class="bar-col ${d.today ? 'today' : ''}" title="${esc(d.date)} · ${d.n}건">
        <span class="bar-n">${d.n}</span>
        <div class="bar-track"><i style="height:${height}%"></i></div>
        <span class="bar-label">${esc(d.label)}</span>
      </div>`;
    }).join('');
  }

  function renderCategories(categories) {
    const top = categories.slice(0, 5);
    const max = Math.max(1, ...top.map((c) => c.n));
    $('#categoryBars').innerHTML = top.map((c) => `
      <div class="cat-row">
        <span class="cat-name">${esc(c.name)}</span>
        <span class="cat-n">${c.n}</span>
        <div class="cat-track"><i style="width:${Math.round((c.n / max) * 100)}%"></i></div>
      </div>`).join('');
  }

  function renderTelegram(alerts) {
    const feed = $('#telegramFeed');
    if (!alerts.length) {
      feed.innerHTML = '<div class="tg-empty">표시할 알림이 없습니다.</div>';
      return;
    }
    feed.innerHTML = alerts.map((a) => `
      <div class="tg-msg ${esc(a.tone)}">
        <div class="tg-msg-title">${esc(a.title)}</div>
        <div class="tg-msg-body">${esc(a.body)}</div>
        <div class="tg-msg-time">${esc(a.time)}</div>
      </div>`).join('');
  }

  /* ------------------------------------------------------------ list */

  function queryString() {
    const f = state.filters;
    const params = new URLSearchParams({
      q: f.q, region: f.region, category: f.category,
      min_amount: String(f.min_amount), days: String(f.days), sort: f.sort,
      limit: String(state.onlyFav ? 60 : 15), offset: '0',
    });
    return params.toString();
  }

  async function loadBids(reset = false) {
    if (reset) {
      state.offset = 0;
      state.rawItems = [];
    }
    const reqId = ++state.reqId;
    const params = new URLSearchParams(queryString());
    params.set('offset', String(reset ? 0 : state.offset));

    try {
      const data = await api(`/api/bids?${params}`);
      if (reqId !== state.reqId) return;
      state.total = data.total;
      state.rawItems = reset ? data.items : state.rawItems.concat(data.items);
      state.offset = (reset ? 0 : state.offset) + data.items.length;
      renderList();
    } catch (err) {
      if (reqId !== state.reqId) return;
      // 실제 원인을 화면과 서버 로그에 남깁니다. (원인 없이 "안 나옴"만 보이지 않도록)
      if (window.__brlog) {
        window.__brlog('fetch_fail', {
          message: (err && err.message) || 'fetch failed',
          url: `/api/bids?${params}`,
        });
      }
      $('#bidList').innerHTML = `<div class="empty-state"><b>공고를 불러오지 못했습니다</b>`
        + `<span>${esc((err && err.message) || '네트워크 오류')}</span>`
        + `<button class="btn btn-ghost" type="button" data-retry-bids>다시 시도</button></div>`;
      $('#resultCount').textContent = '0';
      $('#loadMore').hidden = true;
      toast('데이터를 불러오지 못했습니다.');
    }
  }

  function renderList() {
    let items = state.rawItems;
    if (state.onlyFav) items = items.filter((item) => state.favorites.has(item.id));

    $('#resultCount').textContent = state.onlyFav ? items.length : state.total;

    if (!items.length) {
      $('#bidList').innerHTML = state.onlyFav
        ? '<div class="empty-state"><b>관심 공고가 없습니다</b><span>목록에서 별표를 눌러 관심 공고를 모아보세요.</span></div>'
        : '<div class="empty-state"><b>조건에 맞는 공고가 없습니다</b><span>키워드나 필터를 조금 넓혀보세요.</span><button class="btn btn-ghost" type="button" data-reset-filters>필터 초기화</button></div>';
      $('#loadMore').hidden = true;
      return;
    }

    $('#bidList').innerHTML = items.map((item) => {
      const day = dday(item.close_at);
      const close = kst(item.close_at);
      const on = state.favorites.has(item.id);
      const fresh = Date.now() / 1000 - item.posted_at < 86400;
      const tags = [];
      if (fresh) tags.push('<span class="tag">신규</span>');
      if (item.amount >= 1e9) tags.push('<span class="tag">대형사업</span>');
      if (day.cls === 'urgent') tags.push('<span class="tag">마감임박</span>');
      return `<article class="bid" data-id="${esc(item.id)}" tabindex="0" role="button" aria-label="${esc(item.title)} 상세 보기">
        <div class="dday ${day.cls}"><b>${day.label}</b><span>${day.sub}</span></div>
        <div class="bid-body">
          <div class="bid-badges">
            ${typeBadge(item.bid_type)}
            <span class="badge">${esc(item.region)}</span>
            <span class="badge">${esc(item.category)}</span>
          </div>
          <h3>${esc(item.title)}</h3>
          <div class="bid-meta">
            <b>${esc(item.org)}</b><span class="sep">·</span>${esc(item.notice_no)}
            <span class="sep">·</span>${relative(item.posted_at)} 등록
          </div>
          ${tags.length ? `<div class="bid-tags">${tags.join('')}</div>` : ''}
        </div>
        <div class="bid-right">
          <div class="bid-amount">${item.amount > 0 ? fmtAmount(item.amount) : '미정'}</div>
          <div class="bid-close">${close.md} ${close.hm} 마감</div>
          <button class="star-btn ${on ? 'on' : ''}" data-star="${esc(item.id)}" aria-pressed="${on}" aria-label="관심 공고">
            ${STAR}${on ? '관심' : '관심'}
          </button>
        </div>
      </article>`;
    }).join('');

    const canLoadMore = !state.onlyFav && state.rawItems.length < state.total;
    $('#loadMore').hidden = !canLoadMore;
  }

  function renderChips() {
    const f = state.filters;
    const active = (patch) => Object.entries(patch).every(([key, value]) => {
      if (key === 'q') return f.q.toLowerCase() === String(value).toLowerCase();
      return f[key] === value;
    });
    const chips = QUICK.map((chip, index) => {
      const on = active(chip.patch);
      return `<button class="chip ${on ? 'active' : ''}" type="button" data-quick="${index}">
        ${esc(chip.label)}${on ? '<span class="chip-x">×</span>' : ''}
      </button>`;
    });

    const userAlerts = state.alerts.slice(0, 2).map((a) => {
      const on = f.q === a.keyword && f.region === a.region && f.category === a.category;
      return `<button class="chip ${on ? 'active' : ''}" type="button" data-user-alert="${esc(a.id)}">
        ${esc(a.name)}${on ? '<span class="chip-x">×</span>' : ''}
      </button>`;
    });

    $('#savedChips').innerHTML = chips.concat(userAlerts).join('');
  }

  function toggleFavorite(id) {
    if (state.favorites.has(id)) state.favorites.delete(id);
    else state.favorites.add(id);
    writeJSON(STORE.favorites, [...state.favorites]);

    $$(`[data-star="${id}"]`).forEach((btn) => {
      const on = state.favorites.has(id);
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', String(on));
    });
    const detailBtn = $('#detailFav');
    if (detailBtn && state.detailId === id) {
      const on = state.favorites.has(id);
      detailBtn.classList.toggle('btn-primary', false);
      detailBtn.innerHTML = `${STAR}${on ? '관심 해제' : '관심 공고'}`;
    }
    if (state.onlyFav) renderList();
    toast(state.favorites.has(id) ? '관심 공고에 추가했습니다.' : '관심 공고에서 제거했습니다.');
  }

  /* ------------------------------------------------------------ alerts (local) */

  function saveAlerts() {
    writeJSON(STORE.alerts, state.alerts);
    renderMyAlerts();
    renderChips();
  }

  function renderMyAlerts() {
    $('#alertCount').textContent = state.alerts.length;
    if (!state.alerts.length) {
      $('#myAlerts').innerHTML = '<div class="empty">저장된 알림이 없습니다.</div>';
      return;
    }
    $('#myAlerts').innerHTML = state.alerts.map((a) => `
      <div class="alert-item">
        <button class="alert-apply" type="button" data-alert="${esc(a.id)}" title="${esc(a.name)} 조건 적용">
          <i></i><span>${esc(a.name)}</span>
        </button>
        <button class="alert-del" type="button" data-del="${esc(a.id)}" aria-label="${esc(a.name)} 삭제">×</button>
      </div>`).join('');
  }

  function applyAlert(id) {
    const a = state.alerts.find((item) => item.id === id);
    if (!a) return;
    applyPatch({
      q: a.keyword || '', region: a.region || '', category: a.category || '',
      min_amount: 0, days: 0, sort: 'close',
    });
    $('#onlyFav').checked = false;
    state.onlyFav = false;
    toast(`"${a.name}" 조건을 적용했습니다.`);
    document.querySelector('.list-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  /* ------------------------------------------------------------ drawer */

  function openDrawer(id) {
    state.detailId = id;
    $('#drawer').classList.add('open');
    $('#drawer').setAttribute('aria-hidden', 'false');
    $('#drawerOverlay').classList.add('show');
    document.body.style.overflow = 'hidden';
    $('#drawerBody').innerHTML = '<div class="skeleton-row" style="height:220px"></div>';
    loadDetail(id);
  }

  async function loadDetail(id) {
    try {
      const bid = await api(`/api/bid?id=${encodeURIComponent(id)}`);
      if (state.detailId !== id) return;
      renderDetail(bid);
    } catch {
      if (state.detailId !== id) return;
      $('#drawerBody').innerHTML = '<div class="empty-state"><b>상세 정보를 불러오지 못했습니다</b><span>잠시 후 다시 시도해 주세요.</span></div>';
    }
  }

  function renderDetail(bid) {
    const day = dday(bid.close_at);
    const close = kst(bid.close_at);
    const posted = kst(bid.posted_at);
    const fav = state.favorites.has(bid.id);
    const qualifications = bid.qualifications || [];
    const attachments = bid.attachments || [];

    $('#drawerBody').innerHTML = `
      <div class="detail-badges">
        ${typeBadge(bid.bid_type)}
        <span class="badge">${esc(bid.region)}</span>
        <span class="badge">${esc(bid.category)}</span>
      </div>
      <h2 class="detail-title">${esc(bid.title)}</h2>
      <div class="detail-org">${esc(bid.org)} · ${esc(bid.notice_no)}</div>

      <div class="detail-hero">
        <div><span>추정 사업금액</span><b>${bid.amount > 0 ? `${fmtAmount(bid.amount)}원` : '미정'}</b></div>
        <div class="dday ${day.cls}"><b>${day.label}</b><span>${day.sub}</span></div>
      </div>

      <div class="detail-grid">
        <div><span>공고번호</span><b>${esc(bid.notice_no)}</b></div>
        <div><span>공고유형</span><b>${esc(bid.bid_type)}</b></div>
        <div><span>등록일</span><b>${posted.md} ${posted.hm}</b></div>
        <div><span>마감일</span><b>${close.md} ${close.hm}</b></div>
        <div><span>담당부서</span><b>${esc(bid.contact_dept)}</b></div>
        <div><span>담당자</span><b>${esc(bid.contact_name)} · ${esc(bid.contact_phone)}</b></div>
      </div>

      ${bid.url ? `<a class="detail-source" href="${esc(bid.url)}" target="_blank" rel="noopener noreferrer">나라장터 원문 보기 <span aria-hidden="true">↗</span></a>` : ''}

      <div class="detail-section">
        <h4>사업 개요</h4>
        <p>${esc(bid.description)}</p>
      </div>

      <div class="detail-section">
        <h4>참가 자격</h4>
        <ul>${qualifications.map((q) => `<li>${esc(q)}</li>`).join('')}</ul>
      </div>

      <div class="detail-section">
        <h4>첨부파일 ${attachments.length}건</h4>
        <div class="attach-list">
          ${attachments.map((name) => `<div class="attach">${PAPERCLIP}<span>${esc(name)}</span><em>${esc((name.split('.').pop() || '').toUpperCase())}</em></div>`).join('')}
        </div>
      </div>

      <div class="detail-actions">
        <button class="btn btn-ghost" id="detailFav" type="button">${STAR}${fav ? '관심 해제' : '관심 공고'}</button>
        <button class="btn btn-primary" id="detailAlert" type="button">알림 받기</button>
      </div>`;

    $('#detailFav').addEventListener('click', () => toggleFavorite(bid.id));
    $('#detailAlert').addEventListener('click', () => {
      closeDrawer();
      openModal({
        name: bid.title.length > 18 ? `${bid.title.slice(0, 18)}…` : bid.title,
        keyword: '',
        region: bid.region,
        category: bid.category,
      });
    });
  }

  function closeDrawer() {
    state.detailId = null;
    $('#drawer').classList.remove('open');
    $('#drawer').setAttribute('aria-hidden', 'true');
    $('#drawerOverlay').classList.remove('show');
    document.body.style.overflow = '';
  }

  /* ------------------------------------------------------------ modal */

  function openModal(prefill = {}) {
    $('#alertName').value = prefill.name || '';
    $('#alertKeyword').value = prefill.keyword ?? state.filters.q ?? '';
    $('#alertRegion').value = prefill.region ?? state.filters.region ?? '';
    $('#alertCategory').value = prefill.category ?? state.filters.category ?? '';
    $('#alertChannel').value = state.notifyChannel === 'discord' ? '디스코드' : '텔레그램';
    $('#alertWhen').value = '실시간';
    $('#modalOverlay').classList.add('show');
    document.body.style.overflow = 'hidden';
    setTimeout(() => $('#alertName').focus(), 60);
  }

  function closeModal() {
    $('#modalOverlay').classList.remove('show');
    if (!$('#drawer').classList.contains('open')) document.body.style.overflow = '';
  }

  function submitAlert(event) {
    event.preventDefault();
    const name = $('#alertName').value.trim();
    if (!name) return;
    state.alerts.unshift({
      id: `alert-${Date.now()}`,
      name,
      keyword: $('#alertKeyword').value.trim(),
      region: $('#alertRegion').value,
      category: $('#alertCategory').value,
      channel: $('#alertChannel').value,
      when: $('#alertWhen').value,
    });
    saveAlerts();
    closeModal();
    toast(`"${name}" 알림을 저장했습니다.`);
  }

  /* ------------------------------------------------------------ events */

  function bindEvents() {
    const search = debounce(() => {
      syncFilters();
      renderChips();
      loadBids(true);
    }, 220);

    $('#q').addEventListener('input', search);
    ['#region', '#category', '#minAmount', '#days', '#sort'].forEach((sel) => {
      $(sel).addEventListener('change', () => {
        syncFilters();
        renderChips();
        loadBids(true);
      });
    });

    $('#resetFilters').addEventListener('click', resetAll);

    $('#onlyFav').addEventListener('change', (event) => {
      state.onlyFav = event.target.checked;
      loadBids(true);
    });

    $('#loadMore').addEventListener('click', () => loadBids(false));

    $('#savedChips').addEventListener('click', (event) => {
      const quick = event.target.closest('[data-quick]');
      if (quick) {
        const chip = QUICK[Number(quick.dataset.quick)];
        const f = state.filters;
        const on = Object.entries(chip.patch).every(([key, value]) => (
          key === 'q' ? f.q.toLowerCase() === String(value).toLowerCase() : f[key] === value
        ));
        if (on) {
          const reset = { q: '', region: '', category: '', min_amount: 0, days: 0 };
          applyPatch(reset);
        } else {
          applyPatch({ q: '', region: '', category: '', min_amount: 0, days: 0, ...chip.patch });
        }
        return;
      }
      const userAlert = event.target.closest('[data-user-alert]');
      if (userAlert) applyAlert(userAlert.dataset.userAlert);
    });

    $('#bidList').addEventListener('click', (event) => {
      if (event.target.closest('[data-retry-bids]')) {
        loadBids(true);
        return;
      }
      if (event.target.closest('[data-reset-filters]')) {
        resetAll();
        return;
      }
      const star = event.target.closest('[data-star]');
      if (star) {
        event.stopPropagation();
        toggleFavorite(star.dataset.star);
        return;
      }
      const card = event.target.closest('.bid');
      if (card) openDrawer(card.dataset.id);
    });

    $('#bidList').addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const card = event.target.closest('.bid');
      if (card && !event.target.closest('[data-star]')) {
        event.preventDefault();
        openDrawer(card.dataset.id);
      }
    });

    $('#drawerClose').addEventListener('click', closeDrawer);
    $('#drawerOverlay').addEventListener('click', closeDrawer);

    $('#myAlerts').addEventListener('click', (event) => {
      const del = event.target.closest('[data-del]');
      if (del) {
        state.alerts = state.alerts.filter((a) => a.id !== del.dataset.del);
        saveAlerts();
        toast('알림을 삭제했습니다.');
        return;
      }
      const apply = event.target.closest('[data-alert]');
      if (apply) applyAlert(apply.dataset.alert);
    });

    const openAlert = () => {
      if (CUSTOMER) {
        location.href = '/app/settings';
        return;
      }
      openModal();
    };
    $('#alertBtn').addEventListener('click', openAlert);
    if (!CUSTOMER) {
      $('#navAlerts').addEventListener('click', (event) => { event.preventDefault(); openAlert(); });
    }
    $('#addAlert').addEventListener('click', () => {
      // 고객 콘솔에서는 서버에 저장되는 설정 화면으로 보냅니다.
      if (CUSTOMER) {
        location.href = '/app/settings';
        return;
      }
      openAlert();
    });
    $('#previewAlert').addEventListener('click', () => {
      if (CUSTOMER) {
        location.href = '/app/settings';
        return;
      }
      $('#telegramFeed').scrollTo({ top: 0, behavior: 'smooth' });
      toast(`${state.notifyChannel === 'discord' ? '디스코드' : '텔레그램'} 알림 미리보기입니다.`);
    });
    $('#navPricing').addEventListener('click', (event) => {
      // 실제 요금 안내는 랜딩페이지(#pricing)에 있습니다.
      if (event.currentTarget.getAttribute('href') === '#') event.preventDefault();
    });

    $('#alertForm').addEventListener('submit', submitAlert);
    $('#modalClose').addEventListener('click', closeModal);
    $('#modalCancel').addEventListener('click', closeModal);
    $('#modalOverlay').addEventListener('click', (event) => {
      if (event.target === $('#modalOverlay')) closeModal();
    });

    $('#themeToggle').addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      ls.set(STORE.theme, next);
    });

    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if ($('#modalOverlay').classList.contains('show')) closeModal();
      else if ($('#drawer').classList.contains('open')) closeDrawer();
    });

    $('#weekly').addEventListener('click', () => {
      document.querySelector('.list-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  /* ------------------------------------------------------------ boot */

  async function boot() {
    setupControls();
    renderMyAlerts();
    renderChips();
    bindEvents();

    try {
      const [summary] = await Promise.all([api('/api/summary'), loadBids(true)]);
      renderSummary(summary);
    } catch {
      toast('요약 데이터를 불러오지 못했습니다.');
    }

    const params = new URLSearchParams(location.search);
    const deepBid = params.get('bid');
    if (deepBid) openDrawer(deepBid);
    if (params.get('modal') === 'alert') openModal();
  }

  boot();
})();
