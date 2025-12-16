// static/rtms_app/hamd_widget.js
(function () {
  // ---- Device detection ----
  function isTouchDevice() {
    return (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) ||
      ("ontouchstart" in window) ||
      (navigator.maxTouchPoints > 0);
  }

  // ---- Severity calculation ----
  function getSeverity(total) {
    if (total <= 7) return "正常";
    if (total <= 13) return "軽症";
    if (total <= 18) return "中等症";
    if (total <= 22) return "重症";
    return "最重症";
  }

  // ---- HAMD17 calculation ----
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

  // ---- Button group handling ----
  function initButtonGroups() {
    document.querySelectorAll('.hamd-btn-group').forEach(group => {
      const key = group.dataset.hamdKey;
      const hidden = document.querySelector(`input[name="${key}"][type="hidden"]`);
      if (!hidden) return;

      const buttons = group.querySelectorAll('.hamd-btn');
      buttons.forEach(btn => {
        btn.addEventListener('click', () => {
          // Remove active from all buttons in group
          buttons.forEach(b => b.classList.remove('active'));
          // Add active to clicked button
          btn.classList.add('active');
          // Update hidden input
          hidden.value = btn.dataset.value;
          // Recalculate total
          calcHAMD17();
        });
      });
    });
  }

  // ---- Popover initialization ----
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

    // Touch device: close on outside click
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

  // ---- Restore existing values ----
  function restoreValues() {
    document.querySelectorAll('.hamd-btn-group').forEach(group => {
      const key = group.dataset.hamdKey;
      const hidden = document.querySelector(`input[name="${key}"][type="hidden"]`);
      if (!hidden || !hidden.value) return;

      const buttons = group.querySelectorAll('.hamd-btn');
      buttons.forEach(btn => {
        if (btn.dataset.value === hidden.value) {
          btn.classList.add('active');
        }
      });
    });
    // Calculate after restore
    calcHAMD17();
  }

  // ---- Boot ----
  function boot() {
    initButtonGroups();
    initPopovers();
    restoreValues();
    // Extra calculations for safety
    setTimeout(calcHAMD17, 50);
    setTimeout(calcHAMD17, 200);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // Expose for manual calls
  window.calcHAMD17 = calcHAMD17;
})();</content>
<parameter name="filePath">/Users/kuniyuki/rTMS/static/rtms_app/hamd_widget.js