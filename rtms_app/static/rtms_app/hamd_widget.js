// rtms_app/static/rtms_app/hamd_widget.js
(function () {
  function isTouchDevice() {
    return (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) ||
      ("ontouchstart" in window) ||
      (navigator.maxTouchPoints > 0);
  }

  function getSeverity(total) {
    if (total <= 7) return "正常";
    if (total <= 13) return "軽症";
    if (total <= 18) return "中等症";
    if (total <= 22) return "重症";
    return "最重症";
  }

  function calcHAMD17() {
    let total = 0;
    for (let i = 1; i <= 17; i++) {
      const hidden = document.querySelector(`input[name="q${i}"][type="hidden"]`);
      if (hidden) {
        const v = parseInt(hidden.value || "0", 10);
        total += Number.isFinite(v) ? v : 0;
      }
    }
    const totalEl = document.getElementById("hamd17Total");
    const sevEl = document.getElementById("hamd17Severity");
    if (totalEl) totalEl.textContent = String(total);
    if (sevEl) sevEl.textContent = getSeverity(total);
  }

  function initButtonGroups() {
    document.querySelectorAll('.hamd-btn-group').forEach(group => {
      const key = group.dataset.hamdKey;
      const hidden = document.querySelector(`input[name="${key}"][type="hidden"]`);
      if (!hidden) return;

      const buttons = group.querySelectorAll('.hamd-btn');
      buttons.forEach(btn => {
        btn.addEventListener('click', () => {
          buttons.forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          hidden.value = btn.dataset.value;
          calcHAMD17();
        });
      });
    });
  }

  function initPopovers() {
    if (!window.bootstrap || !bootstrap.Popover) return;

    const trigger = isTouchDevice() ? "click" : "hover focus";

    document.querySelectorAll('[data-bs-toggle="popover"]').forEach((el) => {
      if (bootstrap.Popover.getInstance(el)) return;
      new bootstrap.Popover(el, {
        trigger,
        container: "body",
        placement: "auto",
      });
    });

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

  function restoreValues() {
    document.querySelectorAll('.hamd-btn-group').forEach(group => {
      const key = group.dataset.hamdKey;
      const hidden = document.querySelector(`input[name="${key}"][type="hidden"]`);
      if (!hidden) return;

      const current = String(hidden.value || "0");
      const buttons = group.querySelectorAll('.hamd-btn');
      buttons.forEach(btn => {
        btn.classList.remove('active');
        if (String(btn.dataset.value) === current) {
          btn.classList.add('active');
        }
      });
    });
    calcHAMD17();
  }

  function boot() {
    initButtonGroups();
    initPopovers();
    restoreValues();
    setTimeout(calcHAMD17, 50);
    setTimeout(calcHAMD17, 200);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.calcHAMD17 = calcHAMD17;
  window.restoreHAMDValues = restoreValues;
})();
