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
    console.log('[hamd_widget] initPopovers() called');
    
    // Find all popover elements
    const popoverElements = document.querySelectorAll('[data-bs-toggle="popover"]');
    console.log('[hamd_widget] Found', popoverElements.length, 'popover elements');
    
    if (popoverElements.length === 0) {
      console.warn('[hamd_widget] No popover elements found!');
      return;
    }
    
    // Destroy existing popovers first to avoid duplicates
    popoverElements.forEach((el) => {
      const existingInstance = bootstrap.Popover.getInstance(el);
      if (existingInstance) {
        console.log('[hamd_widget] Disposing existing popover instance');
        existingInstance.dispose();
      }
    });

    const trigger = isTouchDevice() ? "click" : "hover focus";
    console.log('[hamd_widget] Using trigger:', trigger);

    let createdCount = 0;
    popoverElements.forEach((el) => {
      try {
        const content = el.getAttribute('data-bs-content');
        console.log('[hamd_widget] Creating popover with content:', content ? content.substring(0, 50) + '...' : 'NO CONTENT');
        
        new bootstrap.Popover(el, {
          trigger,
          container: "body",
          placement: "auto",
        });
        createdCount++;
      } catch (e) {
        console.error('[hamd_widget] Failed to create popover:', e);
      }
    });
    
    console.log('[hamd_widget] Created', createdCount, 'popover instances');

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
    console.log('[hamd_widget] boot() called');
    initButtonGroups();
    
    // Wait for Bootstrap to be fully loaded before initializing popovers
    if (typeof bootstrap !== 'undefined' && bootstrap.Popover) {
      console.log('[hamd_widget] Bootstrap detected, initializing popovers');
      initPopovers();
      // Retry initialization after a delay to ensure all DOM elements are ready
      setTimeout(() => {
        console.log('[hamd_widget] Re-initializing popovers after delay');
        initPopovers();
      }, 500);
    } else {
      console.log('[hamd_widget] Bootstrap not yet loaded, waiting...');
      let attempts = 0;
      const checkBootstrap = setInterval(() => {
        attempts++;
        if (typeof bootstrap !== 'undefined' && bootstrap.Popover) {
          console.log('[hamd_widget] Bootstrap loaded after', attempts, 'attempts');
          clearInterval(checkBootstrap);
          initPopovers();
          // Retry initialization after a delay
          setTimeout(() => {
            console.log('[hamd_widget] Re-initializing popovers after delay');
            initPopovers();
          }, 500);
        } else if (attempts > 50) {
          console.error('[hamd_widget] Bootstrap failed to load after 50 attempts');
          clearInterval(checkBootstrap);
        }
      }, 100);
    }
    
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
  window.reinitHAMDPopovers = initPopovers; // Expose for manual re-initialization
})();
