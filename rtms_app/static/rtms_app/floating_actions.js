// static/rtms_app/floating_actions.js

/**
 * サーバーへAjax送信を行う関数
 * @param {string} formId 
 * @param {boolean} isAutosave 自動保存かどうか
 */
async function rtmsAjaxSave(formId, isAutosave = false) {
  const form = document.getElementById(formId);
  if (!form) throw new Error(`form not found: ${formId}`);

  // 自動保存の時、ブラウザ標準のチェックでNGなら送信しない（静かに終了）
  if (isAutosave && !form.checkValidity()) {
    return { status: 'skipped_client_validation' };
  }

  const fd = new FormData(form);
  
  // CSRF対策
  if (!fd.has('csrfmiddlewaretoken')) {
    const cookieValue = document.cookie.split('; ').find(row => row.startsWith('csrftoken='))?.split('=')[1];
    if (cookieValue) fd.append('csrfmiddlewaretoken', cookieValue);
  }

  // リクエスト送信
  const res = await fetch(window.location.href, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    body: fd,
    credentials: 'same-origin',
  });

  if (!res.ok) {
    // サーバーエラー(500等)の場合
    throw new Error(`Server Error: ${res.status}`);
  }

  // ▼ ここが修正ポイント: レスポンスがJSONかHTMLかを判定する ▼
  const contentType = res.headers.get('content-type');
  if (contentType && contentType.includes('text/html')) {
    // HTMLが返ってきた = サーバー側でバリデーションエラーになり、画面が再レンダリングされた可能性大
    if (isAutosave) {
      // 自動保存なら、ユーザーの邪魔をしないよう無視する
      return { status: 'skipped_html_response' };
    } else {
      // 手動保存なら、例外を投げて呼び出し元で「通常送信」に切り替えさせる
      throw new Error('HTML_RESPONSE'); 
    }
  }

  // JSONとしてパース
  const data = await res.json();
  
  // サーバー側が明示的にエラーJSONを返してきた場合
  if (data.status === 'error') {
    if (isAutosave) return { status: 'skipped_server_error' };
    
    let msg = '入力エラーがあります。';
    if (data.errors) {
      msg += '\n' + Object.entries(data.errors).map(([k, v]) => `・${k}: ${v}`).join('\n');
    }
    throw new Error(msg);
  }
  
  return data;
}

// トースト通知（右下のメッセージ）
function rtmsShowToast(msg, isError=false) {
  if (!document.body) return; // body が存在しない場合、無視
  let el = document.getElementById('toastSave');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toastSave';
    el.className = 'toast align-items-center text-white border-0';
    el.style.cssText = 'position:fixed; bottom:20px; right:20px; z-index:1070; min-width:250px;';
    document.body.appendChild(el);
  }
  el.innerHTML = `<div class="d-flex"><div class="toast-body" id="toastBody"></div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
  const body = el.querySelector('#toastBody');
  if (!body) return;
  body.textContent = msg;
  el.className = `toast align-items-center text-white border-0 ${isError ? 'bg-danger' : 'bg-success'} show`;
  el.style.display = 'block';
  if (typeof bootstrap !== 'undefined' && bootstrap.Toast) {
    bootstrap.Toast.getOrCreateInstance(el).show();
  } else {
    setTimeout(() => { el.style.display = 'none'; }, 3000);
  }
}

// 印刷URL生成
function buildPrintUrl(btn) {
  const base = btn.dataset.printUrl || '';
  if (!base) return '';
  const url = new URL(base, window.location.origin);
  const docsFormId = btn.dataset.docsFormId;
  if (docsFormId) {
    const docsForm = document.getElementById(docsFormId);
    if (docsForm) {
      new FormData(docsForm).forEach((v, k) => {
        if (v) url.searchParams.append(k, v);
      });
    }
  }
  if (!url.searchParams.has('return_to')) {
    url.searchParams.set('return_to', window.location.pathname);
  }
  return url.toString();
}

// ▼ ボタンクリック時の処理 ▼
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.js-save-and-print, button[type="submit"][name="action"]');
  if (!btn || !btn.closest('.floating-actions')) return;

  e.preventDefault();

  const formId = btn.getAttribute('form') || btn.dataset.formId || 'mainForm';
  const action = btn.value || btn.dataset.action || '';
  const target = btn.dataset.target || '_blank';
  const form = document.getElementById(formId);
  const isPrintBtn = btn.classList.contains('js-save-and-print');

  if (!form) return alert('フォームが見つかりません');

  // ブラウザ標準の入力チェックを表示
  if (!form.reportValidity()) return;

  try {
    if (isPrintBtn) {
      // 印刷ボタンの場合、保存をスキップして直接印刷
      rtmsShowToast('✓ 印刷を開きます');
      const url = buildPrintUrl(btn);
      if (url) window.open(url, target, 'noopener');
    } else {
      // 送信前に action 値を hidden で追加（FormDataに含まれるように）
      let actionInput = form.querySelector('input[name="action"]');
      if (!actionInput) {
        actionInput = document.createElement('input');
        actionInput.type = 'hidden';
        actionInput.name = 'action';
        form.appendChild(actionInput);
      }
      actionInput.value = action;

      // Ajax送信を試みる
      const data = await rtmsAjaxSave(formId, false); // false = 手動保存

      // 成功時の処理
      rtmsShowToast('✓ 保存しました');
      if (data.redirect_url) {
        setTimeout(() => window.location.href = data.redirect_url, 500);
      }
    }

  } catch (err) {
    // ★ ここが重要: HTMLが返ってきた（=エラー画面）なら、Ajaxを諦めて普通に送信する
    if (err.message === 'HTML_RESPONSE') {
      console.warn('Ajax response was HTML. Fallback to normal submit.');
      form.submit(); // 画面遷移してエラーを表示させる
    } else {
      console.error(err);
      alert('エラー: ' + err.message);
    }
  }
});

// 共通オートセーブ: debounce / in-flight / status表示
const RtmsAutoSave = (function(){
  const timers = new Map();
  const inFlight = new Map();
  const debounceMs = 1200;

  function getStatusEl(form) {
    let el = form.querySelector('.autosave-status');
    if (!el) {
      el = document.createElement('div');
      el.className = 'autosave-status small text-muted ms-2';
      el.style.minWidth = '110px';
      el.style.display = 'inline-block';
      const controls = form.querySelector('.floating-actions') || form.querySelector('.card-footer') || form;
      if (controls && controls.parentNode) controls.parentNode.insertBefore(el, controls.nextSibling);
      else form.appendChild(el);
    }
    return el;
  }

  function setStatus(form, state) {
    const el = getStatusEl(form);
    if (state === 'saving') {
      el.textContent = '保存中…';
      el.classList.remove('text-success', 'text-danger');
    } else if (state === 'saved') {
      el.textContent = '✓ 保存済';
      el.classList.add('text-success');
      setTimeout(() => { el.textContent = ''; el.classList.remove('text-success'); }, 2000);
    } else if (state === 'failed') {
      el.textContent = '保存失敗';
      el.classList.add('text-danger');
    } else {
      el.textContent = '';
      el.classList.remove('text-success', 'text-danger');
    }
  }

  async function doSave(formId, isAutosave=false) {
    const form = document.getElementById(formId);
    if (!form) return;
    if (inFlight.get(formId)) return; // prevent concurrent saves
    inFlight.set(formId, true);
    setStatus(form, 'saving');
    try {
      const res = await rtmsAjaxSave(formId, isAutosave);
      if (res && (res.status === 'success' || res.status === undefined)) {
        setStatus(form, 'saved');
      } else {
        setStatus(form, 'failed');
      }
    } catch (err) {
      console.error('autosave error', err);
      setStatus(form, 'failed');
    } finally {
      inFlight.set(formId, false);
    }
  }

  function scheduleSave(formId) {
    if (timers.has(formId)) clearTimeout(timers.get(formId));
    timers.set(formId, setTimeout(() => { doSave(formId, true); timers.delete(formId); }, debounceMs));
  }

  function attachToForm(formId) {
    const form = document.getElementById(formId);
    if (!form) return;
    if (form.dataset.disableFloatingAutosave === 'true') return;

    const handler = (e) => {
      if (!e.target.closest(`#${formId}`)) return;
      scheduleSave(formId);
    };

    form.addEventListener('input', handler);
    form.addEventListener('change', handler);
    form.querySelectorAll('textarea').forEach(t => t.addEventListener('blur', () => scheduleSave(formId)));
  }

  return { attachToForm, doSave, setStatus };
})();

// 自動保存のアタッチ（ページロード時）
document.addEventListener('DOMContentLoaded', function() {
  // RtmsAutoSave.attachToForm('mainForm');
  // RtmsAutoSave.attachToForm('hamdForm');
  // RtmsAutoSave.attachToForm('assessmentForm');
  // RtmsAutoSave.attachToForm('treatmentForm');
});

// グローバルで手動トリガを呼べるように
window.RtmsAutoSave = RtmsAutoSave;