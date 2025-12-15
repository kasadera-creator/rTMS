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
  let el = document.getElementById('toastSave');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toastSave';
    el.className = 'toast align-items-center text-white border-0';
    el.style.cssText = 'position:fixed; bottom:20px; right:20px; z-index:1070; min-width:250px;';
    el.innerHTML = `<div class="d-flex"><div class="toast-body" id="toastBody"></div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
    document.body.appendChild(el);
  }
  const body = el.querySelector('#toastBody');
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
    if (isPrintBtn) {
      rtmsShowToast('✓ 保存しました。印刷を開きます');
      const url = buildPrintUrl(btn) || data.redirect_url;
      if (url) window.open(url, target, 'noopener');
    } else {
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

// 自動保存
let _t = null;
document.addEventListener('input', (e) => {
  const form = document.getElementById('mainForm');
  if (!form || form.dataset.disableFloatingAutosave === 'true') return;
  if (!e.target.closest('#mainForm')) return;

  clearTimeout(_t);
  _t = setTimeout(async () => {
    try {
      const res = await rtmsAjaxSave('mainForm', true); // true = 自動保存
      if (res.status === 'success') rtmsShowToast('✓ 自動保存しました');
    } catch (e) {
      // 自動保存の失敗はユーザーに通知しない
    }
  }, 1500);
});