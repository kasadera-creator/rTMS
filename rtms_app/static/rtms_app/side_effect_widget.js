/**
 * Side-Effect Widget (Updated for PDF Compliance)
 * Tracks Severity at 3 timepoints (Before/During/After) + Relatedness + Memo
 */

class SideEffectWidget {
  constructor(elementId, initialData = null) {
    this.elementId = elementId;
    this.container = document.getElementById(elementId);
    if (!this.container) {
      console.error(`Container ${elementId} not found`);
      return;
    }
    this.data = this.parseInitialData(initialData);
    this.render();
    this.attachEventListeners();
  }

  parseInitialData(initialData) {
    if (!initialData || initialData === "null" || initialData === "") {
      return this.getDefaultRows();
    }
    try {
      const parsed = JSON.parse(initialData);
      // データ形式が古い(旧バージョンの)場合はデフォルトに戻すなどのガードが必要ならここに追加
      if (parsed.length > 0 && !('before' in parsed[0])) {
        return this.getDefaultRows();
      }
      return parsed;
    } catch (e) {
      console.warn('Failed to parse initial data, using defaults', e);
      return this.getDefaultRows();
    }
  }

  getDefaultRows() {
    // PDFの項目に完全に準拠
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
    // 0:なし, 1:軽度, 2:中等度, 3:重度
    // 関連性: 1:低い, 2:あり, 3:高い (0は未選択/なしとする)
    
    const html = `
      <div class="table-responsive">
        <table class="table table-sm table-bordered se-table mb-0">
          <thead class="table-light">
            <tr>
              <th rowspan="2" style="width: 20%; vertical-align: middle; text-align: center;">症状</th>
              <th colspan="3" style="text-align: center;">重症度 (0-3)</th>
              <th rowspan="2" style="width: 12%; vertical-align: middle; text-align: center;">関連性</th>
              <th rowspan="2" style="vertical-align: middle; text-align: center;">メモ</th>
            </tr>
            <tr>
              <th style="width: 10%; text-align: center; font-size: 0.85rem;">前</th>
              <th style="width: 10%; text-align: center; font-size: 0.85rem;">中</th>
              <th style="width: 10%; text-align: center; font-size: 0.85rem;">後</th>
            </tr>
          </thead>
          <tbody>
            ${this.data.map((row, idx) => this.renderRow(row, idx)).join('')}
          </tbody>
        </table>
      </div>
      <div class="mt-2 d-flex justify-content-end gap-3 small text-muted">
        <span><span class="badge bg-light text-dark border me-1">Click</span>で数値変更</span>
        <span>重症度: 0(なし)～3(重度)</span>
        <span>関連性: 0(なし)～3(高い)</span>
      </div>
    `;
    this.container.innerHTML = html;
  }

  renderRow(row, idx) {
    // 数値をクリックでサイクルさせるボタンを生成するヘルパー
    const createCycleBtn = (field, value, max, colorClass) => {
      let btnClass = 'btn-outline-secondary';
      let style = 'background-color: white; color: #ccc;'; // 0の時
      
      if (value > 0) {
        style = ''; // デフォルトに戻す
        btnClass = colorClass; // btn-success 等
      }
      
      return `<button type="button" class="btn btn-sm ${btnClass} se-cycle-btn fw-bold" 
        data-idx="${idx}" data-field="${field}" data-max="${max}"
        style="width: 32px; height: 32px; padding: 0; ${style}">
        ${value === 0 ? '-' : value}
      </button>`;
    };

    return `
      <tr>
        <td style="font-size: 0.9rem; vertical-align: middle; font-weight: 500;">${this.escapeHtml(row.item)}</td>
        
        <td class="text-center bg-light">
          ${createCycleBtn('before', row.before, 3, 'btn-primary')}
        </td>
        
        <td class="text-center">
          ${createCycleBtn('during', row.during, 3, 'btn-danger')}
        </td>
        
        <td class="text-center bg-light">
          ${createCycleBtn('after', row.after, 3, 'btn-primary')}
        </td>

        <td class="text-center">
           ${createCycleBtn('relatedness', row.relatedness, 3, 'btn-warning')}
        </td>

        <td>
          <input type="text" class="form-control form-control-sm se-memo-input" 
            data-idx="${idx}" value="${this.escapeHtml(row.memo || '')}" placeholder="...">
        </td>
      </tr>
    `;
  }

  attachEventListeners() {
    // サイクルボタン (0 -> 1 -> 2 -> 3 -> 0)
    this.container.querySelectorAll('.se-cycle-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const idx = parseInt(btn.dataset.idx);
        const field = btn.dataset.field;
        const max = parseInt(btn.dataset.max);
        
        // 値をインクリメント、最大超えたら0に戻る
        let currentVal = this.data[idx][field];
        this.data[idx][field] = currentVal >= max ? 0 : currentVal + 1;
        
        this.syncAndRender();
      });
    });

    // メモ入力 (フォーカス外れたら保存)
    this.container.querySelectorAll('.se-memo-input').forEach(input => {
      input.addEventListener('change', (e) => {
        const idx = parseInt(input.dataset.idx);
        this.data[idx].memo = e.target.value;
        this.updateHiddenInput();
        // ここでは再レンダリングしない（フォーカスが外れるのを防ぐため）
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
      hiddenInput.value = JSON.stringify(this.data);
    }
  }

  escapeHtml(text) {
    if (!text) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return text.replace(/[&<>"']/g, (m) => map[m]);
  }
}

// Initialize
document.addEventListener('DOMContentLoaded', function() {
  const widget = document.getElementById('sideEffectWidget');
  if (widget) {
    const initialData = widget.getAttribute('data-initial');
    new SideEffectWidget('sideEffectWidget', initialData);
  }
});
