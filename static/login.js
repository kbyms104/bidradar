(() => {
  'use strict';

  const form = document.getElementById('authForm');
  const errorBox = document.getElementById('authError');
  const submit = document.getElementById('authSubmit');
  const fields = document.getElementById('signupFields');
  const tabs = [...document.querySelectorAll('[data-tab]')];
  let mode = 'login';

  const showError = (message) => {
    errorBox.textContent = message || '';
    errorBox.hidden = !message;
  };

  const setMode = (next) => {
    mode = next;
    const signup = next === 'signup';
    fields.hidden = !signup;
    submit.textContent = signup ? '계정 만들기' : '로그인';
    form.querySelector('[name="password"]').autocomplete = signup ? 'new-password' : 'current-password';
    fields.querySelectorAll('input').forEach((input) => {
      input.required = false;
    });
    tabs.forEach((tab) => {
      const on = tab.dataset.tab === next;
      tab.classList.toggle('on', on);
      tab.setAttribute('aria-selected', String(on));
    });
    showError('');
  };

  tabs.forEach((tab) => tab.addEventListener('click', () => setMode(tab.dataset.tab)));

  // ?mode=signup 으로 바로 가입 화면을 열 수 있습니다.
  if (new URLSearchParams(location.search).get('mode') === 'signup') setMode('signup');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    showError('');

    const data = Object.fromEntries(new FormData(form).entries());
    const email = String(data.email || '').trim();
    const password = String(data.password || '');

    if (!email || !email.includes('@')) return showError('이메일을 확인해 주세요.');
    if (password.length < 8) return showError('비밀번호는 8자 이상이어야 합니다.');
    if (mode === 'signup' && !String(data.company || '').trim()) {
      return showError('회사명을 입력해 주세요.');
    }

    submit.disabled = true;
    submit.textContent = mode === 'signup' ? '만드는 중…' : '로그인 중…';

    try {
      const res = await fetch(mode === 'signup' ? '/api/signup' : '/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || !payload.ok) {
        throw new Error(payload.error || `요청 실패 (${res.status})`);
      }
      location.href = payload.next || '/app';
    } catch (err) {
      showError(err.message || '잠시 후 다시 시도해 주세요.');
      submit.disabled = false;
      submit.textContent = mode === 'signup' ? '계정 만들기' : '로그인';
    }
  });
})();
