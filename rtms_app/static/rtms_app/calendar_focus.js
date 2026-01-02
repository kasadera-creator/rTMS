(function(){
  function getQueryParam(name) {
    const params = new URLSearchParams(window.location.search);
    return params.get(name);
  }
  function isISODate(s) {
    return typeof s === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(s);
  }

  function applyCalendarFocus(opts) {
    opts = opts || {};
    const calendar = opts.calendar || (window.calendar || null);
    const root = opts.rootEl || document;

    // Idempotent guard per-root
    try {
      if (root instanceof Element) {
        if (root.dataset && root.dataset.calendarFocusApplied === '1') return;
        root.dataset.calendarFocusApplied = '1';
      } else if (root === document) {
        if (document._calendarFocusApplied) return;
        document._calendarFocusApplied = true;
      }
    } catch (e) {}

    function doFocus() {
      const focus = getQueryParam('focus');
      if (!isISODate(focus)) return;

      // Try FullCalendar-like instance first
      try {
        if (calendar && typeof calendar.gotoDate === 'function') {
          calendar.gotoDate(focus);
          // remove focus param
          try {
            const url = new URL(window.location.href);
            url.searchParams.delete('focus');
            window.history.replaceState({}, '', url.toString());
          } catch (e) {}
          return;
        }
      } catch (e) {
        console.warn('calendar.gotoDate failed for focus=', focus, e);
      }

      // Otherwise look for data-date attr in DOM under root
      try {
        const el = root.querySelector(`[data-date="${focus}"]`);
        if (el) {
          try { el.scrollIntoView({behavior: 'smooth', block: 'center'}); } catch (e) {}
          el.classList.add('focus-day');
          try {
            const url = new URL(window.location.href);
            url.searchParams.delete('focus');
            window.history.replaceState({}, '', url.toString());
          } catch (e) {}
          return;
        }
      } catch (e) {}

      // If not found, navigate to month containing focus (only if focus is valid)
      try {
        const d = new Date(focus + 'T00:00:00');
        if (!isNaN(d.getTime())) {
          const q = new URLSearchParams(window.location.search);
          q.set('year', d.getFullYear());
          q.set('month', d.getMonth() + 1);
          q.delete('focus');
          const base = window.location.pathname;
          window.location.href = base + '?' + q.toString();
        }
      } catch (e) {}
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', doFocus);
    } else {
      setTimeout(doFocus, 0);
    }
  }

  // expose
  window.applyCalendarFocus = applyCalendarFocus;
})();
