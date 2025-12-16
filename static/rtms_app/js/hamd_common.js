// static/rtms_app/js/hamd_common.js
(function () {
  function isTouchDevice() {
    return (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) ||
      ("ontouchstart" in window) ||
      (navigator.maxTouchPoints > 0);
  }

  // ---- Popover ----
  function initPopovers() {
    // bootstrap がまだ無い場合は何もしない（ロード順問題の保険）
    if (!window.bootstrap || !bootstrap.Popover) return;

    const trigger = isTouchDevice() ? "click" : "hover focus";

    // data-bs-toggle="popover" を対象にする（属性ズレに強い）
    document.querySelectorAll('[data-bs-toggle="popover"]').forEach((el) => {
      // 二重初期化防止
      if (bootstrap.Popover.getInstance(el)) return;
      new bootstrap.Popover(el, {
        trigger,
        container: "body",
        placement: "auto",
      });
    });

    // touch端末：外側タップで閉じる
    if (isTouchDevice()) {
      document.addEventListener("click", (e) => {
        const clickedTrigger = e.target.closest('[data-bs-toggle="popover"]');
        const pop = document.querySelector(".popover");
        const clickedPopover = pop && pop.contains(e.target);
        if (!clickedTrigger && !clickedPopover) {
          document.querySelectorAll('[data-bs-toggle="popover"]').forEach((el) => {
            const inst = bootstrap.Popover.getInstance(el);
            if (inst) inst.hide();
          });
        }
      });
    }
  }

  // ---- HAMD17 realtime ----
  function severity17(total) {
    if (total <= 7) return "正常";
    if (total <= 13) return "軽症";
    if (total <= 18) return "中等症";
    if (total <= 22) return "重症";
    return "最重症";
  }

  function calcHAMD17() {
    let total = 0;
    for (let i = 1; i <= 17; i++) {
      const checked = document.querySelector(`input[name="q${i}"]:checked`);
      if (checked) {
        const v = parseInt(checked.value || "0", 10);
        total += Number.isFinite(v) ? v : 0;
      }
    }
    const totalEl = document.getElementById("hamd17Total");
    const sevEl = document.getElementById("hamd17Severity");
    if (totalEl) totalEl.textContent = String(total);
    if (sevEl) sevEl.textContent = severity17(total);
  }

  function bindHAMDListeners() {
    // ★イベント委譲：後からDOMが変わっても必ず拾う
    document.addEventListener("change", (e) => {
      const t = e.target;
      if (t && t.matches('input[type="radio"][data-hamd-score="1"]')) {
        calcHAMD17();
      }
    });
    document.addEventListener("click", (e) => {
      // labelクリックでもchangeが遅延する環境対策
      const t = e.target;
      if (t && t.closest && t.closest('label[data-hamd-label="1"]')) {
        // 次tickで計算（checked反映後）
        setTimeout(calcHAMD17, 0);
      }
    });
  }

  function boot() {
    initPopovers();
    bindHAMDListeners();
    // 初期描画＆復元後に必ず計算
    calcHAMD17();
    // 1回目で0のままでも、復元処理が遅いテンプレ対策で少し後に再計算
    setTimeout(calcHAMD17, 50);
    setTimeout(calcHAMD17, 200);
  }

  // DOM準備後に起動
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // 外部から呼べるように公開
  window.calcHAMD17 = calcHAMD17;
})();