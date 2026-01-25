const SurveyPage = (() => {
  let cfg = {};
  let instrument = {};
  let answers = {};
  let saveTimer = null;

  function readJson(id) {
    const el = document.getElementById(id);
    if (!el) return {};
    try { return JSON.parse(el.textContent || '{}'); } catch (e) { return {}; }
  }

  function restoreSelections() {
    document.querySelectorAll('.btn-check').forEach((input) => {
      const val = answers[input.name];
      if (val !== undefined && String(val) === String(input.value)) {
        input.checked = true;
      }
    });
  }

  function collectAnswers() {
    const collected = {};
    document.querySelectorAll('.btn-check').forEach((input) => {
      if (input.checked) {
        collected[input.name] = input.value;
      }
    });
    answers = collected;
    return collected;
  }

  function findOption(question, value) {
    return (question.options || []).find((o) => String(o.id) === String(value));
  }

  function computeTotal() {
    let total = 0;
    (instrument.questions || []).forEach((q) => {
      if (q.include_in_total === false) return;
      const val = answers[q.key];
      if (val === undefined) return;
      const opt = findOption(q, val);
      if (!opt) return;
      let score = Number(opt.score) || 0;
      if (q.reverse) {
        const max = q.max_score || Math.max(...(q.options || []).map((o) => Number(o.score) || 0));
        score = (max + 1) - score;
      }
      total += score;
    });
    const totalEl = document.getElementById('totalScore');
    if (totalEl) totalEl.textContent = total;
    return total;
  }

  function updateDynamicLabels() {
    (instrument.questions || []).forEach((q) => {
      if (!q.dynamic_label) return;
      const sourceKey = q.dynamic_label.source_key;
      const mapping = q.dynamic_label.cases || {};
      const selected = answers[sourceKey];
      const label = mapping[selected] || q.text;
      const el = document.querySelector(`#q-${q.key} .question-text`);
      if (el) el.textContent = label;
    });
  }

  function highlightMissing(key) {
    const card = document.getElementById(`q-${key}`);
    if (card) {
      card.classList.add('border', 'border-danger');
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setTimeout(() => card.classList.remove('border', 'border-danger'), 2000);
    }
  }

  function findMissing() {
    const missing = [];
    (instrument.questions || []).forEach((q) => {
      if (answers[q.key] === undefined) missing.push(q.key);
    });
    return missing;
  }

  function debouncedSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveDraft(), 400);
  }

  async function saveDraft(nav = 'stay') {
    collectAnswers();
    try {
      await fetch(window.location.href, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify({ answers, nav }),
      });
    } catch (e) {
      console.warn('autosave failed', e);
    }
  }

  function goNext() {
    collectAnswers();
    const missing = findMissing();
    if (missing.length > 0) {
      highlightMissing(missing[0]);
      alert('未回答の設問があります。');
      return;
    }
    const nav = document.getElementById('navInput');
    if (nav) nav.value = 'next';
    document.getElementById('surveyForm').submit();
  }

  function goPrev() {
    const nav = document.getElementById('navInput');
    if (nav) nav.value = 'prev';
    document.getElementById('surveyForm').submit();
  }

  function saveOnly() {
    saveDraft('stay');
  }

  function bindEvents() {
    document.querySelectorAll('.btn-check').forEach((input) => {
      input.addEventListener('change', () => {
        answers[input.name] = input.value;
        updateDynamicLabels();
        computeTotal();
        debouncedSave();
      });
    });
  }

  function init(config) {
    cfg = config || {};
    instrument = readJson('instrumentDef') || {};
    answers = readJson('answerData') || {};
    restoreSelections();
    updateDynamicLabels();
    computeTotal();
    bindEvents();
  }

  return {
    init,
    goNext,
    goPrev,
    saveDraft: saveOnly,
  };
})();
