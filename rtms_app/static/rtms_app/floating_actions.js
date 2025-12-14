// static/rtms_app/floating_actions.js
// -----------------------------------
// Floating print/save controller
// - POST form pages: AJAX save -> open redirect_url or print_url
// - GET/no-form pages: open print_url directly
// - Bundle docs: build docs=... from a docs form (checkboxes) if provided
// -----------------------------------

(function () {
  'use strict';

  // ----------------------------
  // Toast helpers
  // ----------------------------
  function showToast(msg, isError = false) {
    const el = document.getElementById('toastSave');
    if (!el) return;

    el.textContent = msg;
    el.classList.toggle('error', isError);
    el.classList.add('show');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => el.classList.remove('show'), 1800);
  }

  // ----------------------------
  // URL builder for bundle print:
  // - reads <form id="bundlePrintForm..."> checkboxes (name="docs")
  // - copies hidden inputs
  // ----------------------------
  function buildUrlWithDocs(baseUrl, docsFormId) {
    if (!baseUrl || !docsFormId) return baseUrl;

    const f = document.getElementById(docsFormId);
    if (!f) return baseUrl;

    // baseUrl may be path-only; make absolute for URL()
    const abs = new URL(baseUrl, window.location.origin);

    // reset docs params
    abs.searchParams.delete('docs');

    // checked docs
    const checked = Array.from(f.querySelectorAll('input[name="docs"]:checked'))
      .map((x) => x.value)
      .filter(Boolean);

    checked.forEach((d) => abs.searchParams.append('docs', d));

    // copy hidden fields (dashboard_date etc.)
    Array.from(f.querySelectorAll('input[type="hidden"][name]')).forEach((h) => {
      const name = h.getAttribute('name');
      const val = h.value;
      if (!name) return;
      if (val === undefined || val === null || val === '') return;
      abs.searchParams.set(name, val);
    });

    return abs.pathname + (abs.search || '');
  }

  // ----------------------------
  // AJAX save (POST)
  // ----------------------------
  async function ajaxSave(form, action) {
    const fd = new FormData(form);
    if (action) fd.append('action', action);

    const res = await fetch(window.location.href, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: fd,
      credentials: 'same-origin',
    });

    // If server returns HTML (redirect / error page), json() will throw.
    // We try to parse JSON safely with a fallback.
    const text = await res.text();

    let data = null;
    try {
      data = JSON.parse(text);
    } catch (e) {
      // Not JSON
      throw new Error(`Non-JSON response (status ${res.status})`);
    }

    if (!res.ok) {
      const msg = data && (data.message || data.detail) ? (data.message || data.detail) : `HTTP ${res.status}`;
      throw new Error(msg);
    }

    if (!data || data.status !== 'success') {
      throw new Error((data && data.message) || 'save failed');
    }

    return data; // may include redirect_url
  }

  // ----------------------------
  // Click handler: save and print
  // ----------------------------
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.js-save-and-print');
  if (!btn) return;

  e.preventDefault();

  const formId = btn.dataset.formId || 'mainForm';
  const action = btn.dataset.action || '';
  const target = btn.dataset.target || '_blank';
  const docsFormId = btn.dataset.docsFormId || '';

  // ★ まずは data-print-url を絶対優先
  let printUrl = (btn.dataset.printUrl || '').trim();

  // docsFormId があれば docs=... を反映（bundle用）
  if (printUrl && docsFormId) {
    printUrl = buildUrlWithDocs(printUrl, docsFormId);  // あなたが既に入れている関数
  }

  // ★ printUrl が無ければ推測しない（ここが重要）
  if (!printUrl) {
    showToast('印刷設定がありません', true);
    return;
  }

  const form = document.getElementById(formId);
  const method = form ? (form.getAttribute('method') || '').toLowerCase() : '';

  // ★ GETフォーム/フォーム無しの画面は、保存せずに printUrl を開くだけ
  if (!form || method === 'get') {
    window.open(printUrl, target, 'noopener');
    return;
  }

  // ★ POSTフォームのみ：保存してから印刷
  try {
    const data = await ajaxSave(form, action);  // あなたが既に入れている保存関数
    showToast('✓ 自動保存しました');

    const urlToOpen = (data && data.redirect_url) ? data.redirect_url : printUrl;
    window.open(urlToOpen, target, 'noopener');
  } catch (err) {
    console.error(err);
    showToast('保存または印刷に失敗しました', true);
  }
});

  // ----------------------------
  // Auto-save (debounced) for POST form pages
  // ----------------------------
  let autoTimer = null;

  document.addEventListener('input', (e) => {
    const form = document.getElementById('mainForm');
    if (!form) return;

    const method = (form.getAttribute('method') || '').toLowerCase();
    if (method !== 'post') return;

    // only when typing inside mainForm
    if (!e.target.closest('#mainForm')) return;

    clearTimeout(autoTimer);
    autoTimer = setTimeout(async () => {
      try {
        await ajaxSave(form, ''); // save only
        showToast('✓ 自動保存しました');
      } catch (err) {
        console.error(err);
        showToast('保存に失敗しました', true);
      }
    }, 1000);
  });
})();
