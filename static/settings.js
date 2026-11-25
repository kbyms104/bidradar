(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const apiBase = window.BIDRADAR_API || '/api/app';

  const state = {
    me: null,
    conditions: [],
    history: [],
    options: { categories: [], regions: [] },
  };

  /* ------------------------------------------------------------ helpers */

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);

  const won = (value) => {
    const n = Number(value) || 0;
    if (!n) return '금액 무관';
    if (n >= 100000000) return `${(n / 100000000).toFixed(n % 100000000 ? 1 : 0)}억원 이상`;
    if (n >= 10000000) return `${(n / 10000000).toFixed(n % 10000000 ? 1 : 0)}천만원 이상`;
    return `${n.toLocaleString('ko-KR')}원 이상`;
  };

  const when = (ts) => {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    if (Number.isNaN(d.getTime())) return '-';
    const pad = (v) => String(v).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}.${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  const stageLabel = (stage) => {
    const n = Number(stage);
    if (n === 3) return 'D-3';
    if (n === 1) return 'D-1';
    return '테스트';
  };

  const setStatus = (el, message, tone) => {
    el.textContent = message || '';
    el.className = `status${tone ? ` ${tone}` : ''}`;
  };

  async function api(path, options = {}) {
    const res = await fetch(apiBase + path, {
      headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
      ...options,
    });
    if (res.status === 401) {
      location.href = '/login';
      throw new Error('로그인이 필요합니다.');
    }
    const payload = await res.json().catch(() => ({}));
    if (!res.ok || payload.ok === false) {
      throw new Error(payload.error || `요청 실패 (${res.status})`);
    }
    return payload;
  }

  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

  /* ------------------------------------------------------------ render */

  function renderAccount() {
    const me = state.me || {};
    $('#accountBadge').textContent = (me.company || me.email || '내 계정').slice(0, 24);
    $('#acctEmail').value = me.email || '';
    $('#acctCompany').value = me.company || '';
    $('#acctName').value = me.name || '';
    $('#acctPhone').value = me.phone || '';

    const webhook = $('#webhook');
    webhook.value = me.webhook || '';
    const ok = String(me.webhook || '').startsWith('https://discord.com/api/webhooks/');
    setStatus($('#webhookState'), ok ? '웹훅 저장됨' : '웹훅 미설정', ok ? 'ok' : 'err');
  }

  function renderOptions() {
    const { regions = [], categories = [] } = state.options || {};
    $('#regionChecks').innerHTML = regions.length
      ? regions.map((r) => `<label><input type="checkbox" class="region" value="${esc(r)}"> ${esc(r)}</label>`).join('')
      : '<span class="hint">지역 목록을 불러오지 못했습니다.</span>';
    $('#categoryChecks').innerHTML = categories.length
      ? categories.map((c) => `<label><input type="checkbox" class="category" value="${esc(c)}"> ${esc(c)}</label>`).join('')
      : '<span class="hint">분야 목록을 불러오지 못했습니다.</span>';
  }

  function conditionTags(cond) {
    const tags = [];
    (cond.keywords || []).forEach((k) => tags.push(`<span class="tag">#${esc(k)}</span>`));
    (cond.exclude_keywords || []).forEach((k) => tags.push(`<span class="tag off">제외 ${esc(k)}</span>`));
    (cond.regions || []).forEach((r) => tags.push(`<span class="tag">${esc(r)}</span>`));
    (cond.categories || []).forEach((c) => tags.push(`<span class="tag">${esc(c)}</span>`));
    if (Number(cond.min_amount) > 0) tags.push(`<span class="tag">${esc(won(cond.min_amount))}</span>`);
    (cond.stages || []).forEach((s) => tags.push(`<span class="tag">${stageLabel(s)} 발송</span>`));
    return tags.join('') || '<span class="tag">전체 공고</span>';
  }

  function renderConditions() {
    const list = $('#condList');
    if (!state.conditions.length) {
      list.innerHTML = '<p class="history-empty">아직 조건이 없습니다. ‘+ 조건 추가’를 눌러 첫 조건을 만드세요.</p>';
      return;
    }
    list.innerHTML = state.conditions.map((cond) => `
      <div class="cond-item" data-id="${cond.id}">
        <div class="row-actions" style="justify-content:space-between">
          <b>${esc(cond.name)}</b>
          <span class="status ${cond.enabled ? 'ok' : ''}">${cond.enabled ? '사용 중' : '일시중지'}</span>
        </div>
        <div class="tags">${conditionTags(cond)}</div>
        <div class="ops">
          <button class="btn btn-ghost btn-sm" data-act="edit" type="button">수정</button>
          <button class="btn btn-ghost btn-sm" data-act="toggle" type="button">${cond.enabled ? '일시중지' : '다시 사용'}</button>
          <button class="btn btn-ghost btn-sm" data-act="delete" type="button">삭제</button>
        </div>
      </div>
    `).join('');
  }

  function renderHistory() {
    const body = $('#historyBody');
    if (!state.history.length) {
      body.innerHTML = '<p class="history-empty">아직 발송 기록이 없습니다. 조건과 웹훅을 저장하면 D-3 · D-1에 자동 발송됩니다.</p>';
      return;
    }
    const rows = state.history.map((row) => {
      const ok = Number(row.ok) === 1;
      return `<tr>
        <td>${esc(when(row.sent_at))}</td>
        <td>${esc(row.condition_name || '-')}</td>
        <td>${esc(stageLabel(row.stage))}</td>
        <td>${Number(row.bid_count) || 0}건</td>
        <td style="color:${ok ? 'var(--green)' : 'var(--red)'};font-weight:700">${ok ? '발송 완료' : '실패'}</td>
        <td style="color:var(--faint)">${esc(row.detail || '')}</td>
      </tr>`;
    }).join('');
    body.innerHTML = `<div style="overflow-x:auto"><table class="history-table">
      <thead><tr><th>시각</th><th>조건</th><th>시점</th><th>건수</th><th>결과</th><th>비고</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  }

  /* ------------------------------------------------------------ condition form */

  const form = $('#condForm');

  function clearChecks(selector) {
    form.querySelectorAll(selector).forEach((input) => { input.checked = false; });
  }

  function openForm(cond) {
    $('#condId').value = cond ? cond.id : '';
    $('#condName').value = cond ? cond.name : '';
    $('#condKeywords').value = cond ? (cond.keywords || []).join(', ') : '';
    $('#condExclude').value = cond ? (cond.exclude_keywords || []).join(', ') : '';
    $('#condMin').value = cond && cond.min_amount ? cond.min_amount : '';
    $('#condEnabled').checked = cond ? !!cond.enabled : true;

    clearChecks('.region');
    clearChecks('.category');
    (cond?.regions || []).forEach((value) => {
      const el = form.querySelector(`.region[value="${CSS.escape(value)}"]`);
      if (el) el.checked = true;
    });
    (cond?.categories || []).forEach((value) => {
      const el = form.querySelector(`.category[value="${CSS.escape(value)}"]`);
      if (el) el.checked = true;
    });

    const stages = cond ? (cond.stages || []) : [3, 1];
    form.querySelectorAll('.stage').forEach((input) => {
      input.checked = stages.map(String).includes(input.value);
    });

    $('#condSubmit').textContent = cond ? '수정 저장' : '조건 저장';
    setStatus($('#condMsg'), '');
    form.classList.add('open');
    $('#condName').focus();
  }

  function closeForm() {
    form.classList.remove('open');
    $('#condId').value = '';
  }

  function collectCondition() {
    return {
      id: Number($('#condId').value) || undefined,
      name: $('#condName').value.trim() || '내 조건',
      keywords: $('#condKeywords').value,
      exclude_keywords: $('#condExclude').value,
      regions: [...form.querySelectorAll('.region:checked')].map((el) => el.value),
      categories: [...form.querySelectorAll('.category:checked')].map((el) => el.value),
      min_amount: Number(String($('#condMin').value).replace(/[^0-9]/g, '')) || 0,
      stages: [...form.querySelectorAll('.stage:checked')].map((el) => Number(el.value)),
      enabled: $('#condEnabled').checked,
    };
  }

  /* ------------------------------------------------------------ events */

  $('#newCondition').addEventListener('click', () => {
    if (form.classList.contains('open') && !$('#condId').value) return closeForm();
    openForm(null);
  });
  $('#condCancel').addEventListener('click', closeForm);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = collectCondition();
    if (!payload.name) return setStatus($('#condMsg'), '조건 이름을 입력해 주세요.', 'err');
    if (!payload.stages.length) payload.stages = [3, 1];

    const submit = $('#condSubmit');
    submit.disabled = true;
    setStatus($('#condMsg'), '저장 중…');
    try {
      const res = await post('/conditions', payload);
      state.conditions = res.conditions || [];
      renderConditions();
      closeForm();
      refreshHistory();
    } catch (err) {
      setStatus($('#condMsg'), err.message, 'err');
    } finally {
      submit.disabled = false;
    }
  });

  $('#condList').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const item = button.closest('.cond-item');
    const id = Number(item?.dataset.id);
    const cond = state.conditions.find((c) => Number(c.id) === id);
    if (!cond) return;

    if (button.dataset.act === 'edit') return openForm(cond);

    if (button.dataset.act === 'toggle') {
      button.disabled = true;
      try {
        const res = await post('/conditions', { ...cond, enabled: !cond.enabled });
        state.conditions = res.conditions || [];
        renderConditions();
      } catch (err) {
        setStatus($('#condMsg'), err.message, 'err');
      } finally {
        button.disabled = false;
      }
      return;
    }

    if (button.dataset.act === 'delete') {
      if (!confirm(`‘${cond.name}’ 조건을 삭제할까요?\n발송 이력은 남습니다.`)) return;
      button.disabled = true;
      try {
        const res = await post('/conditions/delete', { id });
        state.conditions = res.conditions || [];
        renderConditions();
        if (Number($('#condId').value) === id) closeForm();
      } catch (err) {
        setStatus($('#condMsg'), err.message, 'err');
      } finally {
        button.disabled = false;
      }
    }
  });

  $('#saveWebhook').addEventListener('click', async () => {
    const url = $('#webhook').value.trim();
    if (url && !url.startsWith('https://discord.com/api/webhooks/')) {
      return setStatus($('#webhookMsg'), '디스코드 웹훅 URL 형식이 아닙니다.', 'err');
    }
    const button = $('#saveWebhook');
    button.disabled = true;
    setStatus($('#webhookMsg'), '저장 중…');
    try {
      const res = await post('/account', { ...state.me, webhook: url });
      state.me = res.me || state.me;
      renderAccount();
      setStatus($('#webhookMsg'), url ? '저장했습니다. 이제 테스트 발송을 해보세요.' : '웹훅을 비웠습니다.', 'ok');
    } catch (err) {
      setStatus($('#webhookMsg'), err.message, 'err');
    } finally {
      button.disabled = false;
    }
  });

  $('#testWebhook').addEventListener('click', async () => {
    const button = $('#testWebhook');
    button.disabled = true;
    try {
      // 저장하지 않은 URL로 테스트하면 LIVE 판정과 실제 발송이 어긋나므로 먼저 저장합니다.
      const url = $('#webhook').value.trim();
      if (url !== (state.me?.webhook || '')) {
        setStatus($('#webhookMsg'), '웹훅 저장 중…');
        const saved = await post('/account', { ...state.me, webhook: url });
        state.me = saved.me || state.me;
        renderAccount();
      }
      setStatus($('#webhookMsg'), '테스트 발송 중…');
      const res = await post('/test', { webhook: url });
      setStatus($('#webhookMsg'), res.detail ? `디스코드를 확인하세요 (${res.detail})` : '디스코드를 확인하세요.', 'ok');
    } catch (err) {
      setStatus($('#webhookMsg'), err.message, 'err');
    } finally {
      button.disabled = false;
    }
  });

  $('#savePassword').addEventListener('click', async () => {
    const current = $('#pwCurrent').value;
    const next = $('#pwNew').value;
    const confirm = $('#pwConfirm').value;
    if (!current) return setStatus($('#pwMsg'), '현재 비밀번호를 입력해 주세요.', 'err');
    if (next.length < 8) return setStatus($('#pwMsg'), '새 비밀번호는 8자 이상이어야 합니다.', 'err');
    if (next !== confirm) return setStatus($('#pwMsg'), '새 비밀번호 확인이 일치하지 않습니다.', 'err');
    const button = $('#savePassword');
    button.disabled = true;
    setStatus($('#pwMsg'), '변경 중…');
    try {
      await post('/password', { current, new: next });
      $('#pwCurrent').value = $('#pwNew').value = $('#pwConfirm').value = '';
      setStatus($('#pwMsg'), '변경했습니다. 다른 기기는 로그아웃됩니다.', 'ok');
    } catch (err) {
      setStatus($('#pwMsg'), err.message, 'err');
    } finally {
      button.disabled = false;
    }
  });

  $('#saveAccount').addEventListener('click', async () => {
    const button = $('#saveAccount');
    button.disabled = true;
    setStatus($('#acctMsg'), '저장 중…');
    try {
      const res = await post('/account', {
        company: $('#acctCompany').value.trim(),
        name: $('#acctName').value.trim(),
        phone: $('#acctPhone').value.trim(),
        webhook: state.me?.webhook || '',
      });
      state.me = res.me || state.me;
      renderAccount();
      setStatus($('#acctMsg'), '저장했습니다.', 'ok');
    } catch (err) {
      setStatus($('#acctMsg'), err.message, 'err');
    } finally {
      button.disabled = false;
    }
  });

  $('#refreshHistory').addEventListener('click', () => refreshHistory(true));

  async function refreshHistory(notifyUser) {
    try {
      const res = await api('/me');
      state.history = res.history || [];
      state.conditions = res.conditions || state.conditions;
      renderHistory();
      renderConditions();
      if (notifyUser) setStatus($('#acctMsg'), '');
    } catch (err) {
      $('#historyBody').innerHTML = `<p class="history-empty" style="color:var(--red)">${esc(err.message)}</p>`;
    }
  }

  /* ------------------------------------------------------------ boot */

  (async () => {
    try {
      const res = await api('/me');
      state.me = res.me || {};
      state.conditions = res.conditions || [];
      state.history = res.history || [];
      state.options = res.options || state.options;
      renderAccount();
      renderOptions();
      renderConditions();
      renderHistory();
    } catch (err) {
      $('#condList').innerHTML = `<p class="history-empty" style="color:var(--red)">${esc(err.message)}</p>`;
      setStatus($('#webhookState'), '오류', 'err');
    }
  })();
})();
