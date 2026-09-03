(function () {
  'use strict';

  const table = document.querySelector('[data-reschedule-url]');
  if (!table) return;

  const csrfInput = document.querySelector('#mainForm input[name="csrfmiddlewaretoken"]');
  const csrfToken = csrfInput ? csrfInput.value : '';
  const eventLinks = table.querySelectorAll('[data-draggable="true"]');
  let draggedEvent = null;
  let suppressClickUntil = 0;

  function clearDropTargets() {
    table.querySelectorAll('.is-drop-target').forEach((cell) => {
      cell.classList.remove('is-drop-target');
    });
  }

  function clearDragState() {
    if (draggedEvent) draggedEvent.classList.remove('is-dragging');
    draggedEvent = null;
    clearDropTargets();
  }

  function buildPayload(link, targetDate) {
    const payload = {
      event_type: link.dataset.eventType,
      target_date: targetDate,
      course_number: table.dataset.courseNumber,
    };

    if (link.dataset.eventType === 'admission' || link.dataset.eventType === 'discharge') {
      return payload;
    }

    if (link.dataset.eventType === 'treatment' || link.dataset.eventType === 'mapping') {
      payload.source_date = link.dataset.sourceDate;
      if (link.dataset.status) payload.status = link.dataset.status;
      if (link.dataset.sessionId) payload.session_id = link.dataset.sessionId;
      if (link.dataset.eventType === 'mapping' && link.dataset.weekNumber) {
        payload.week_number = link.dataset.weekNumber;
      }
      return payload;
    }

    if (link.dataset.eventType === 'assessment') {
      payload.timing = link.dataset.timing;
      payload.scale_code = link.dataset.scaleCode;
    }
    return payload;
  }

  function responseMessage(response, body) {
    if (body && body.error) return body.error;
    return `予定変更に失敗しました（HTTP ${response.status}）`;
  }

  async function submitMove(link, targetDate, exceptionalDay) {
    const sourceDate = link.dataset.sourceDate;
    if (!sourceDate || sourceDate === targetDate) return;

    const confirmationMessages = [];
    if (exceptionalDay && link.dataset.eventType === 'treatment') {
      confirmationMessages.push(
        '通常の治療日ではありません。この日を例外的な治療日に設定しますか？'
      );
    }
    if (link.dataset.firstTreatment === 'true') {
      confirmationMessages.push(
        '治療開始日を変更すると、現在の治療予定を新しい開始日を基準に再構成します。よろしいですか？'
      );
    }
    if (confirmationMessages.length && !window.confirm(confirmationMessages.join('\n\n'))) {
      return;
    }

    const payload = buildPayload(link, targetDate);
    try {
      const response = await fetch(table.dataset.rescheduleUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRFToken': csrfToken,
        },
        body: JSON.stringify(payload),
      });

      let body = null;
      try {
        body = await response.json();
      } catch (_error) {
        // Keep the HTTP status as the fallback message for non-JSON responses.
      }

      if (!response.ok) {
        throw new Error(responseMessage(response, body));
      }

      window.location.reload();
    } catch (error) {
      window.alert(error.message || '予定変更に失敗しました');
    }
  }

  eventLinks.forEach((link) => {
    link.addEventListener('dragstart', (event) => {
      draggedEvent = link;
      link.classList.add('is-dragging');
      suppressClickUntil = Date.now() + 800;
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', link.dataset.eventType || 'schedule-event');
    });

    link.addEventListener('dragend', () => {
      suppressClickUntil = Date.now() + 500;
      clearDragState();
    });
  });

  table.addEventListener('dragover', (event) => {
    if (!draggedEvent) return;
    const cell = event.target.closest('td[data-date]');
    if (!cell || !table.contains(cell)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    clearDropTargets();
    cell.classList.add('is-drop-target');
  });

  table.addEventListener('drop', (event) => {
    if (!draggedEvent) return;
    const cell = event.target.closest('td[data-date]');
    if (!cell || !table.contains(cell)) return;
    event.preventDefault();
    suppressClickUntil = Date.now() + 800;
    const link = draggedEvent;
    const targetDate = cell.dataset.date;
    const exceptionalDay = cell.dataset.nonBusinessDay === 'true';
    clearDragState();
    submitMove(link, targetDate, exceptionalDay);
  });

  document.addEventListener('click', (event) => {
    if (Date.now() < suppressClickUntil) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
})();
