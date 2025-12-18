/**
 * Side-Effect Check Widget (v2)
 * Manages clickable cells for symptom tracking with before/during/after/relatedness
 */

class SideEffectWidget {
  constructor(elementId, initialData = null) {
    this.elementId = elementId;
    this.container = document.getElementById(elementId);
    
    if (!this.container) {
      console.error(`[SideEffectWidget] Container with id "${elementId}" not found`);
      return;
    }
    
    this.data = this.parseInitialData(initialData);
    
    this.render();
    this.attachEventListeners();
    
    // Sync hidden input with the loaded data.
    // IMPORTANT: parseInitialData() tries hidden input as a fallback so we don't wipe DB-backed state.
    this.updateHiddenInput();
  }

  parseInitialData(initialData) {
    const candidates = [];

    if (Array.isArray(initialData)) {
      candidates.push(initialData);
    } else if (typeof initialData === 'string') {
      candidates.push(initialData);
    }

    // Fallback: use current hidden input value (server-rendered JSON) if present.
    const hidden = document.getElementById('sideEffectRowsJson');
    if (hidden && typeof hidden.value === 'string' && hidden.value.trim() !== '') {
      candidates.push(hidden.value);
    }

    for (const candidate of candidates) {
      const parsed = this.tryParseRows(candidate);
      if (parsed) return parsed;
    }

    return this.getDefaultRows();
  }

  tryParseRows(candidate) {
    if (candidate == null) return null;

    // Already parsed
    if (Array.isArray(candidate)) {
      return this.validateRows(candidate) ? candidate : null;
    }

    if (typeof candidate !== 'string') return null;

    const s = candidate.trim();
    if (!s || s === 'null' || s === '[]') return null;

    try {
      const parsed = JSON.parse(s);
      return this.validateRows(parsed) ? parsed : null;
    } catch (_) {
      return null;
    }
  }

  validateRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) return false;
    const first = rows[0];
    return first && typeof first === 'object' && ('before' in first) && ('during' in first) && ('after' in first);
  }

  getDefaultRows() {
    return [
      { item: '頭皮痛・刺激痛', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: '顔面の不快感', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: '頸部痛・肩こり', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: '頭痛 (刺激後)', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: 'けいれん (部位・時間)', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: '失神', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: '聴覚障害', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: 'めまい・耳鳴り', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: '注意集中困難', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: '急性気分変化 (躁転など)', before: 0, during: 0, after: 0, relatedness: 0, memo: '' },
      { item: 'その他', before: 0, during: 0, after: 0, relatedness: 0, memo: '' }
    ];
  }

  render() {
    const html = `
      <div class="table-responsive">
        <table class="table table-sm table-bordered se-table mb-0" style="font-size: 0.85rem;">
          <thead class="table-light">
            <tr>
              <th rowspan="2" style="width: 16%; vertical-align: middle; text-align: center; font-size: 0.85rem; padding: 0.4rem;">症状</th>
              <th colspan="3" style="text-align: center; font-weight: bold; font-size: 0.85rem; padding: 0.4rem;">重症度</th>
              <th rowspan="2" style="width: 10%; vertical-align: middle; text-align: center; font-size: 0.85rem; padding: 0.4rem;">関連性</th>
              <th rowspan="2" style="width: 22%; vertical-align: middle; text-align: center; font-size: 0.85rem; padding: 0.4rem;">メモ</th>
            </tr>
            <tr>
              <th style="width: 9%; text-align: center; font-size: 0.8rem; padding: 0.3rem;">前</th>
              <th style="width: 9%; text-align: center; font-size: 0.8rem; padding: 0.3rem;">中</th>
              <th style="width: 9%; text-align: center; font-size: 0.8rem; padding: 0.3rem;">後</th>
            </tr>
          </thead>
          <tbody>
            ${this.data.map((row, idx) => this.renderRow(row, idx)).join('')}
          </tbody>
        </table>
      </div>
      <div class="mt-2 d-flex justify-content-end gap-3" style="font-size: 0.75rem; color: #999;">
        <span><strong>操作:</strong> クリックで 0→1→2→3→0</span>
        <span><strong>重症度:</strong> 0(なし)～3(重度)</span>
        <span><strong>関連性:</strong> 0(なし)～3(高い)</span>
      </div>
    `;
    
    this.container.innerHTML = html;
  }

  renderRow(row, idx) {
    const createCycleBtn = (field, value, max) => {
      let btnClass = 'btn-light';
      let textColor = 'text-muted';
      let displayValue = '-';
      
      if (value > 0) {
        btnClass = 'btn-success';
        textColor = 'text-white';
        displayValue = value;
      }
      
      return `
        <button type="button" class="btn btn-sm ${btnClass} ${textColor} se-cycle-btn fw-bold" 
          data-idx="${idx}" data-field="${field}" data-max="${max}"
          style="width: 30px; height: 30px; padding: 0; font-size: 0.8rem;">
          ${displayValue}
        </button>
      `;
    };

    return `
      <tr>
        <td style="font-size: 0.8rem; vertical-align: middle; font-weight: 500; text-align: left; padding: 0.4rem;">
          ${this.escapeHtml(row.item)}
        </td>
        
        <td class="text-center bg-light" style="vertical-align: middle; padding: 0.3rem;">
          ${createCycleBtn('before', row.before, 3)}
        </td>
        
        <td class="text-center" style="vertical-align: middle; padding: 0.3rem;">
          ${createCycleBtn('during', row.during, 3)}
        </td>
        
        <td class="text-center bg-light" style="vertical-align: middle; padding: 0.3rem;">
          ${createCycleBtn('after', row.after, 3)}
        </td>

        <td class="text-center" style="vertical-align: middle; padding: 0.3rem;">
          ${createCycleBtn('relatedness', row.relatedness, 3)}
        </td>

        <td style="vertical-align: middle; padding: 0.3rem;">
          <input type="text" class="form-control form-control-sm se-memo-input" 
            data-idx="${idx}" value="${this.escapeHtml(row.memo || '')}" 
            placeholder="..." style="font-size: 0.75rem; padding: 0.25rem;">
        </td>
      </tr>
    `;
  }

  attachEventListeners() {
    // Cycle buttons (0 -> 1 -> 2 -> 3 -> 0)
    this.container.querySelectorAll('.se-cycle-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        
        const idx = parseInt(btn.dataset.idx);
        const field = btn.dataset.field;
        const max = parseInt(btn.dataset.max);
        
        if (isNaN(idx) || !field || isNaN(max)) {
          console.error('[SideEffectWidget] Invalid button data:', { idx, field, max });
          return;
        }
        
        if (!this.data[idx]) {
          console.error(`[SideEffectWidget] Row index ${idx} not found`);
          return;
        }
        
        // Cycle value
        let currentVal = this.data[idx][field];
        if (typeof currentVal !== 'number') {
          currentVal = 0;
        }
        
        this.data[idx][field] = currentVal >= max ? 0 : currentVal + 1;
        
        this.syncAndRender();
      });
    });

    // Memo inputs - sync on input change (not just blur)
    this.container.querySelectorAll('.se-memo-input').forEach(input => {
      // Sync immediately on input
      input.addEventListener('input', (e) => {
        const idx = parseInt(input.dataset.idx);
        
        if (isNaN(idx) || !this.data[idx]) {
          console.error('[SideEffectWidget] Invalid memo input:', { idx });
          return;
        }
        
        this.data[idx].memo = e.target.value;
        this.updateHiddenInput(); // Sync without re-render to avoid losing focus
      });
    });
  }

  syncAndRender() {
    this.render();
    this.attachEventListeners();
    this.updateHiddenInput();
    
  }

  updateHiddenInput() {
    const hiddenInput = document.getElementById('sideEffectRowsJson');
    if (hiddenInput) {
      const jsonData = JSON.stringify(this.data);
      hiddenInput.value = jsonData;
    } else {
      console.warn('[SideEffectWidget] Hidden input #sideEffectRowsJson not found');
    }
  }

  escapeHtml(text) {
    if (!text) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return text.replace(/[&<>"']/g, (m) => map[m]);
  }
}

// Initialize on DOM ready
let sideEffectWidgetInstance = null;

document.addEventListener('DOMContentLoaded', function() {
  const widget = document.getElementById('sideEffectWidget');
  if (widget) {
    // Preferred: load initial rows from json_script referenced by data-initial-id.
    let initialData = null;

    const initialId = widget.getAttribute('data-initial-id');
    if (initialId) {
      const scriptEl = document.getElementById(initialId);
      if (scriptEl && typeof scriptEl.textContent === 'string') {
        initialData = scriptEl.textContent;
      }
    }

    // Backward compatibility: older templates may still provide data-initial.
    if (initialData == null) {
      initialData = widget.getAttribute('data-initial');
    }

    sideEffectWidgetInstance = new SideEffectWidget('sideEffectWidget', initialData);
  } else {
    console.warn('[SideEffectWidget] Widget element #sideEffectWidget not found');
  }
});
