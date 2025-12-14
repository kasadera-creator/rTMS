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

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.js-save-and-print');
  if (!btn) return;

  e.preventDefault();

  const formId = btn.dataset.formId || 'mainForm';
  const action = btn.dataset.action || '';
  const fallbackUrl = btn.dataset.printUrl || '';
  const target = btn.dataset.target || '_blank';

  if (!action && !fallbackUrl) {
    rtmsShowToast('印刷設定がありません', true);
    return;
  }

  try {
    const form = document.getElementById(formId);

    // ★フォームが無い画面（例：クリニカルパス表示）では保存せず印刷だけ
    if (!form) {
      const url = fallbackUrl;
      if (url) {
        window.open(url, target, 'noopener');
        return;
      }
      rtmsShowToast('印刷URLが取得できません', true);
      return;
    }

    const fd = new FormData(form);
    if (action) fd.append('action', action);

    const res = await fetch(window.location.href, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: fd,
      credentials: 'same-origin',
    });

// 自動保存（入力時デバウンス）
let _t = null;
document.addEventListener('input', (e) => {
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
