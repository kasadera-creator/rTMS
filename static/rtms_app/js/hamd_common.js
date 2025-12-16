// =========================
// HAMD 共通ロジック
// =========================

function isTouchDevice() {
  return window.matchMedia("(pointer: coarse)").matches ||
         "ontouchstart" in window ||
         navigator.maxTouchPoints > 0;
}

function initHAMDPopovers() {
  const trigger = isTouchDevice() ? "click" : "hover focus";
  document.querySelectorAll('[data-hamd-popover]').forEach(el => {
    new bootstrap.Popover(el, {
      trigger,
      container: "body",
      placement: "auto",
    });
  });

  if (isTouchDevice()) {
    document.addEventListener("click", (e) => {
      if (!e.target.closest('[data-hamd-popover]') &&
          !e.target.closest('.popover')) {
        document.querySelectorAll('[data-hamd-popover]').forEach(el => {
          const inst = bootstrap.Popover.getInstance(el);
          if (inst) inst.hide();
        });
      }
    });
  }
}

function calcHAMD17() {
  let total = 0;
  for (let i = 1; i <= 17; i++) {
    const input = document.querySelector(`input[name="q${i}"]:checked`);
    if (input) total += parseInt(input.value, 10);
  }

  document.getElementById("hamd17Total").textContent = total;

  let severity = "正常";
  if (total >= 23) severity = "最重症";
  else if (total >= 19) severity = "重症";
  else if (total >= 14) severity = "中等症";
  else if (total >= 8) severity = "軽症";

  document.getElementById("hamd17Severity").textContent = severity;
}

function initHAMDRealtime() {
  document.querySelectorAll('input[type="radio"]').forEach(el => {
    el.addEventListener("change", calcHAMD17);
  });
  calcHAMD17();
}

window.addEventListener("load", () => {
  initHAMDPopovers();
  initHAMDRealtime();
});