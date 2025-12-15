// static/rtms_app/floating_actions.js

// サーバーへデータを送信する関数
async function rtmsAjaxSave(formId, isAutosave = false) {
  const form = document.getElementById(formId);
  if (!form) throw new Error(`form not found: ${formId}`);

  // 自動保存の場合、ブラウザの簡易チェック(必須項目など)に通らなければ
  // サーバーには送らず、エラーも出さずに終了する
  if (isAutosave && !form.checkValidity()) {
    console.log('Autosave skipped: form is invalid yet.');
    return { status: 'skipped' };
  }

  const fd = new FormData(form);
  
  // CSRFトークンの確保（もしフォーム内にない場合）
  if (!fd.has('csrfmiddlewaretoken')) {
    const cookieValue = document.cookie
      .split('; ')
      .find(row => row.startsWith('csrftoken='))
      ?.split('=')[1];
    if (cookieValue) {
      fd.append('csrfmiddlewaretoken', cookieValue);
    }
  }

  const res = await fetch(window.location.href, {
    method: 'POST',
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    body: fd,
    credentials: 'same-origin',
  });

  if (!res.ok) throw new Error(`通信エラー: HTTP ${res.status}`);

  const data = await res.json();
  
  // サーバー側でバリデーションエラー（入力不備）があった場合
  if (data.status === 'error') {
    let errorMsg = '入力内容に不備があります。';
    if (data.errors) {
      // エラーメッセージを抽出して連結 (例: "MT(%): この項目は必須です")
      const details = Object.entries(data.errors)
        .map(([field, errs]) => `・${field}: ${errs.join(', ')}`)
        .join('\n');
      errorMsg += '\n' + details;
    }
    // 自動保存なら静かに無視、手動保存ならエラーを投げる
    if (isAutosave) {
        return { status: 'skipped_server_validation' };
    }
    throw new Error(errorMsg);
  }
  
  if (data.status !== 'success') throw new Error('不明なエラーが発生しました');
  
  return data;
}

// トースト通知を表示する関数
function rtmsShowToast(msg, isError=false) {
  // トースト要素がなければ作成する
  let el = document.getElementById('toastSave');
  if (!el) {
      el = document.createElement('div');
      el.id = 'toastSave';
      el.className = 'toast align-items-center text-white border-0';
      el.style.position = 'fixed';
      el.style.bottom = '20px';
      el.style.right = '20px';
      el.style.zIndex = '1070';
      el.style.minWidth = '250px';
      el.innerHTML = `<div class="d-flex"><div class="toast-body" id="toastBody"></div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
      document.body.appendChild(el);
  }
  
  const toastBody = el.querySelector('#toastBody');
  toastBody.innerText = msg;
  
  el.classList.remove('bg-success', 'bg-danger');
  el.classList.add(isError ? 'bg-danger' : 'bg-success');
  el.classList.add('show'); // Bootstrapを使わない簡易表示
  el.style.display = 'block';

  // BootstrapのToast機能があれば使う、なければCSSで制御
  if (typeof bootstrap !== 'undefined' && bootstrap.Toast) {
      const bsToast = bootstrap.Toast.getOrCreateInstance(el);
      bsToast.show();
  } else {
      setTimeout(() => { el.style.display = 'none'; }, 3000);
  }
}

// 印刷用URL生成ヘルパー
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

// ボタンクリック（保存・印刷・完了）のイベントリスナー
document.addEventListener('click', async (e) => {
  // .js-save-and-print クラスを持つボタン、または type="submit" ボタンを対象にする
  const btn = e.target.closest('.js-save-and-print, button[type="submit"][name="action"]');
  if (!btn) return;

  // フローティングメニュー内のボタンのみ対象とする(通常のフォーム内ボタンと競合しないよう)
  if (!btn.closest('.floating-actions')) return;

  e.preventDefault();

  const formId = btn.getAttribute('form') || btn.dataset.formId || 'mainForm';
  const action = btn.value || btn.dataset.action || ''; // ボタンのvalue (saveなど)
  const target = btn.dataset.target || '_blank';
  const form = document.getElementById(formId);
  const isPrintBtn = btn.classList.contains('js-save-and-print');
  
  if (!form) {
      alert('エラー: 対象のフォームが見つかりません (ID: ' + formId + ')');
      return;
  }

  // 手動クリック時は、ブラウザ標準のバリデーションを表示してあげる
  if (!form.reportValidity()) {
      return; // 必須項目が足りないなどの場合、ここで止める（ブラウザが吹き出しを出す）
  }

  try {
    // フォームに action (saveなど) を追加して送信
    const fd = new FormData(form);
    if (action) fd.append('action', action);

    // Ajax送信を実行
    // ここでの fetch は rtmsAjaxSave を使わず、action値を渡すために直接書くか、
    // rtmsAjaxSave を改造する。ここでは直接実装します。
    
    const res = await fetch(window.location.href, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        body: fd,
        credentials: 'same-origin',
    });

    if (!res.ok) throw new Error(`通信エラー: ${res.status}`);
    const data = await res.json();

    if (data.status === 'error') {
        let msg = '保存できませんでした。入力内容を確認してください。\n';
        if (data.errors) {
             msg += Object.entries(data.errors).map(([k, v]) => `・${k}: ${v}`).join('\n');
        }
        alert(msg); // ユーザーに具体的な理由を通知
        return;
    }

    // 成功時
    if (isPrintBtn) {
        rtmsShowToast('✓ 保存しました。印刷画面を開きます');
        const printUrl = buildPrintUrl(btn) || data.redirect_url;
        if (printUrl) window.open(printUrl, target, 'noopener');
    } else {
        // 完了ボタンなどの場合
        rtmsShowToast('✓ 保存しました');
        if (data.redirect_url) {
            setTimeout(() => window.location.href = data.redirect_url, 500);
        }
    }

  } catch (err) {
    console.error(err);
    alert('エラーが発生しました: ' + err.message);
  }
});

// 自動保存（入力時デバウンス）
let _t = null;
document.addEventListener('input', (e) => {
  const mainForm = document.getElementById('mainForm');
  // 明示的に無効化されている場合やフォームがない場合はスキップ
  if (!mainForm || mainForm.dataset.disableFloatingAutosave === 'true') return;
  
  // フォーム内の入力でなければ無視
  if (!e.target.closest('#mainForm')) return;
  
  clearTimeout(_t);
  _t = setTimeout(async () => {
    try {
      // 第2引数 true = 自動保存モード (エラーは無視する)
      const result = await rtmsAjaxSave('mainForm', true);
      
      if (result.status === 'success') {
        rtmsShowToast('✓ 自動保存しました');
      }
      // skipped などの場合は何もしない
    } catch (err) {
      // 自動保存のエラーはコンソールに出すだけで、ユーザーには通知しない（邪魔になるため）
      console.warn('Autosave failed (silently ignored):', err.message);
    }
  }, 1500); // 間隔を少し長めに(1.5秒)
});