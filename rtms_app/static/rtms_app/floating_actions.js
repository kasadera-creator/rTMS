// static/rtms_app/floating_actions.js
async function rtmsAjaxSave(formId) {
  const form = document.getElementById(formId);
  if (!form) throw new Error(`form not found: ${formId}`);

  const fd = new FormData(form);
  const res = await fetch(window.location.href, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    body: fd,
    credentials: 'same-origin',
  });
  if (!res.ok) throw new Error(`save failed: HTTP ${res.status}`);

  const data = await res.json();
  if (data.status !== 'success') throw new Error('save failed');
  return data;
}

function rtmsShowToast(msg, isError=false) {
  const el = document.getElementById('toastSave');
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(rtmsShowToast._t);
  rtmsShowToast._t = setTimeout(() => el.classList.remove('show'), 1800);
}

function buildPrintUrl(btn) {
  const base = btn.dataset.printUrl || '';
  if (!base) return '';

  const url = new URL(base, window.location.origin);
  const docsFormId = btn.dataset.docsFormId;

  if (docsFormId) {
    const docsForm = document.getElementById(docsFormId);
    if (docsForm) {
      const fd = new FormData(docsForm);
      for (const [key, value] of fd.entries()) {
        if (value === null || value === undefined || value === '') continue;
        url.searchParams.append(key, value);
      }
    }
  }

  if (!url.searchParams.has('return_to')) {
    url.searchParams.set('return_to', window.location.pathname);
  }

  return url.toString();
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.js-save-and-print');
  if (!btn) return;

  e.preventDefault();

  const formId = btn.dataset.formId || 'mainForm';
  const action = btn.dataset.action || '';
  const target = btn.dataset.target || '_blank';
  const form = document.getElementById(formId);
  const isPostForm = form && form.tagName === 'FORM' && (form.getAttribute('method') || 'post').toLowerCase() === 'post';

  const printUrl = buildPrintUrl(btn);

  try {
    if (isPostForm) {
      const fd = new FormData(form);
      if (action) fd.append('action', action);

      const res = await fetch(window.location.href, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        body: fd,
        credentials: 'same-origin',
      });

      if (!res.ok) throw new Error(`save failed: HTTP ${res.status}`);

      let data = {};
      try { data = await res.json(); } catch (_) {}

      rtmsShowToast('✓ 自動保存しました');

      const url = data.redirect_url || printUrl;
      if (url) window.open(url, target, 'noopener');
      else rtmsShowToast('印刷URLが設定されていません', true);
    } else {
      if (!printUrl) {
        rtmsShowToast('印刷URLが設定されていません', true);
        return;
      }
      window.open(printUrl, target, 'noopener');
    }
  } catch (err) {
    console.error(err);
    rtmsShowToast('保存または印刷に失敗しました', true);
  }
});

// 自動保存（入力時デバウンス）
let _t = null;
document.addEventListener('input', (e) => {
  const mainForm = document.getElementById('mainForm');
  if (!mainForm || mainForm.dataset.disableFloatingAutosave === 'true') return;
  if (!e.target.closest('#mainForm')) return;
  clearTimeout(_t);
  _t = setTimeout(async () => {
    try {
      await rtmsAjaxSave('mainForm');
      rtmsShowToast('✓ 自動保存しました');
    } catch (err) {
      console.error(err);
      rtmsShowToast('保存に失敗しました', true);
    }
  }, 1000);
});
