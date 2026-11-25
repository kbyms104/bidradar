(() => {
  'use strict';

  const THEME_KEY = 'bidradar.theme';
  const root = document.documentElement;

  // 라이트/다크 테마 — 대시보드와 같은 키를 써서 페이지 간 상태를 유지합니다.
  const forced = new URLSearchParams(location.search).get('theme');
  const safeGet = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const safeSet = (key, value) => { try { localStorage.setItem(key, value); } catch { /* blocked */ } };
  const stored = safeGet(THEME_KEY);
  const theme = forced === 'dark' || forced === 'light'
    ? forced
    : stored || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  root.dataset.theme = theme;

  const toggle = document.getElementById('themeToggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      safeSet(THEME_KEY, next);
    });
  }

  // 실데이터 모드면 데모 안내 문구를 실제 수집 안내로 바꿉니다.
  fetch('/api/health')
    .then((res) => res.json())
    .then((health) => {
      if (health.source !== 'live') return;
      document.querySelectorAll('.demo-tag').forEach((el) => el.remove());
      document.querySelectorAll('.demo-shot figcaption').forEach((el) => {
        el.textContent = '데모 화면 · 이미지를 눌러 열기';
      });
      const faq = document.getElementById('faqData');
      if (faq) {
        faq.textContent = '실제 공고입니다. 나라장터 입찰공고정보서비스에서 수집하며, 각 공고에는 공고번호와 마감일시가 원문 기준으로 표시됩니다.';
      }
      const note = document.getElementById('footNote');
      if (note) note.textContent = '공고 데이터는 나라장터(공공데이터포털)에서 수집합니다.';
    })
    .catch(() => {});

  // 상담 신청 폼
  const form = document.getElementById('leadForm');
  const status = document.getElementById('formStatus');
  const submit = document.getElementById('leadSubmit');
  if (!form || !status || !submit) return;

  const setStatus = (message, kind) => {
    status.textContent = message;
    status.className = `form-status${kind ? ` ${kind}` : ''}`;
  };

  const REQUIRED = [
    ['company', '회사명을 입력해 주세요.'],
    ['name', '담당자 이름을 입력해 주세요.'],
    ['contact', '연락처(이메일 또는 전화번호)를 입력해 주세요.'],
  ];

  form.addEventListener('submit', async (event) => {
    event.preventDefault();

    const data = new FormData(form);
    for (const [field, message] of REQUIRED) {
      const value = String(data.get(field) || '').trim();
      if (!value) {
        setStatus(message, 'err');
        form.elements[field].focus();
        return;
      }
    }
    if (!data.get('consent')) {
      setStatus('개인정보 수집·이용에 동의해 주세요.', 'err');
      return;
    }

    const payload = {
      company: String(data.get('company') || '').trim(),
      name: String(data.get('name') || '').trim(),
      contact: String(data.get('contact') || '').trim(),
      keywords: String(data.get('keywords') || '').trim(),
      method: String(data.get('method') || '').trim(),
      message: String(data.get('message') || '').trim(),
      website: String(data.get('website') || ''),
    };

    submit.disabled = true;
    setStatus('보내는 중…');

    try {
      const res = await fetch('/api/lead', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || !body.ok) {
        throw new Error(body.error || '신청을 접수하지 못했습니다.');
      }
      form.reset();
      form.classList.add('sent');
      submit.disabled = true;
      setStatus(`접수되었습니다 (접수번호 ${body.id}). 영업일 기준 1일 이내에 연락드리겠습니다.`, 'ok');
    } catch (error) {
      submit.disabled = false;
      setStatus(`${error.message} 잠시 후 다시 시도하시거나, 같은 내용으로 다시 보내주세요.`, 'err');
    }
  });
})();
