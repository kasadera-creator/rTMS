/**
 * 治療実施ガイド ウィザードモード
 * Step1〜9 を順に表示し、必要な入力を収集してDBに保存
 */

class ProcedureWizard {
  constructor() {
    this.currentStep = 1;
    this.maxSteps = 9;
    this.state = {
      // 各ステップの入力状態を保持
      step1_checks: [],
      step2_checks: [],
      step3_safety_sleep: true,
      step3_safety_alcohol: true,
      step3_safety_meds: true,
      // Step4: 説明のみ（入力なし）
      step5_mt_date: this.getTodayDate(),
      step5_mt_value: '60',
      step5_mt_a_x: '3',
      step5_mt_a_y: '1',
      step5_mt_b_x: '9',
      step5_mt_b_y: '1',
      step5_mt_note: '',
      // Step6
      step6_confirm_seconds: '2.0',
      step6_confirm_percent: '120',
      step6_discomfort: false,
      step6_movement: false,
      step6_note: '',
      // Step7
      step7_mt_percent: '',
      step7_discomfort: false,
      step7_movement: false,
      step7_note: '',
      // Step8
      step8_side_effect_opened: false,
    };
    this.mtMeasurementRequired = false; // 安全チェック結果に基づいて動的に決定
  }

  getTodayDate() {
    const today = new Date();
    return today.toISOString().split('T')[0];
  }

  /**
   * ウィザード初期化
   */
  init() {
    this.setupEventListeners();
    // Initialize Step7 default MT% from form or default to 120
    const mtField = document.getElementById('id_mt_percent');
    if (mtField && mtField.value) {
      this.state.step7_mt_percent = mtField.value;
    } else {
      this.state.step7_mt_percent = '120';
    }
    this.render();
  }

  /**
   * ボタンのイベントリスナー設定
   */
  setupEventListeners() {
    const nextBtn = document.getElementById('wizardNextBtn');
    const prevBtn = document.getElementById('wizardPrevBtn');
    const completeBtn = document.getElementById('wizardCompleteBtn');

    if (nextBtn) {
      nextBtn.addEventListener('click', () => this.goToNextStep());
    }
    if (prevBtn) {
      prevBtn.addEventListener('click', () => this.goToPreviousStep());
    }
    if (completeBtn) {
      completeBtn.addEventListener('click', () => this.complete());
    }

    // モーダルが開いたときに初期化
    const modal = document.getElementById('procedureWizardModal');
    if (modal) {
      modal.addEventListener('show.bs.modal', () => {
        this.currentStep = 1;
        this.render();
      });
    }
  }

  /**
   * 次のステップへ（バリデーション付き）
   */
  goToNextStep() {
    if (!this.validateCurrentStep()) {
      return;
    }
    if (this.currentStep < this.maxSteps) {
      this.currentStep++;
      this.render();
    }
  }

  /**
   * 前のステップへ
   */
  goToPreviousStep() {
    if (this.currentStep > 1) {
      this.currentStep--;
      this.render();
    }
  }

  /**
   * 各ステップのバリデーション
   */
  validateCurrentStep() {
    switch (this.currentStep) {
      case 1:
      case 2:
      case 4:
      case 9:
        // これらはバリデーション不要（説明のみ or optional）
        return true;

      case 3:
        // 安全確認：バリデーションなし（ただし状態を記録）
        this.mtMeasurementRequired =
          !this.state.step3_safety_sleep ||
          !this.state.step3_safety_alcohol ||
          !this.state.step3_safety_meds;
        return true;

      case 5:
        // MT測定が必要な場合のバリデーション
        if (this.mtMeasurementRequired) {
          const mtValue = parseInt(this.state.step5_mt_value);
          if (isNaN(mtValue) || mtValue < 10 || mtValue > 100) {
            alert('MT値は 10〜100 の数値を入力してください。');
            return false;
          }
        }
        return true;

      case 6:
        // 任意（注意表示のみ）
        return true;

      case 7:
        // 刺激強度は既定値がいるので基本OK
        if (!this.state.step7_mt_percent) {
          alert('実施した刺激強度（%）を入力してください。');
          return false;
        }
        return true;

      case 8:
        // 説明のみ（副作用入力ボタンは別途）
        return true;

      default:
        return true;
    }
  }

  /**
   * ウィザード完了 → 治療フォーム同期 → 保存
   */
  complete() {
    if (!this.validateCurrentStep()) {
      return;
    }

    // フォーム同期
    this.syncToTreatmentForm();

    // 副作用データ同期確保
    if (typeof sideEffectWidgetInstance !== 'undefined' && sideEffectWidgetInstance) {
      sideEffectWidgetInstance.updateHiddenInput();
    }

    // treatment_form にアクション追加
    const treatmentForm = document.getElementById('treatmentForm');
    let actionInput = document.getElementById('actionInput');
    if (!actionInput) {
      actionInput = document.createElement('input');
      actionInput.type = 'hidden';
      actionInput.id = 'actionInput';
      actionInput.name = 'action';
      treatmentForm.appendChild(actionInput);
    }
    actionInput.value = 'save_from_wizard';
    // （完了時はHTMLは返さない）

    // チェック状態を外部フォームに伝える（safety warning等を再計算させるため）
    const sleepCheckbox = document.getElementById('id_safety_sleep');
    if (sleepCheckbox) {
      sleepCheckbox.dispatchEvent(new Event('change'));
    }
  }

  ensureHidden(name, value) {
    const form = document.getElementById('treatmentForm');
    if (!form) return;
    let input = form.querySelector(`input[name="${name}"]`);
    if (!input) {
      input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      form.appendChild(input);
    }
    input.value = value == null ? '' : String(value);
  }

  /**
   * 現在のステップをレンダリング
   */
  render() {
    const contentDiv = document.getElementById('wizardContent');
    if (!contentDiv) return;

    contentDiv.innerHTML = this.getStepHTML(this.currentStep);

    // ボタン表示制御
    this.updateButtonState();

    // イベントリスナー再附与
    this.attachStepEventListeners();
  }

  /**
   * ボタン表示制御
   */
  updateButtonState() {
    const prevBtn = document.getElementById('wizardPrevBtn');
    const nextBtn = document.getElementById('wizardNextBtn');
    const completeBtn = document.getElementById('wizardCompleteBtn');

    if (prevBtn) {
      prevBtn.style.display = this.currentStep > 1 ? 'inline-block' : 'none';
    }
    if (nextBtn) {
      nextBtn.style.display = this.currentStep < this.maxSteps ? 'inline-block' : 'none';
    }
    if (completeBtn) {
      completeBtn.style.display = this.currentStep === this.maxSteps ? 'inline-block' : 'none';
    }
  }

  /**
   * 各ステップのHTML生成
   */
  getStepHTML(step) {
    const stepContent = {
      1: this.renderStep1(),
      2: this.renderStep2(),
      3: this.renderStep3(),
      4: this.renderStep4(),
      5: this.renderStep5(),
      6: this.renderStep6(),
      7: this.renderStep7(),
      8: this.renderStep8(),
      9: this.renderStep9(),
    };

    return `
      <div class="wizard-step">
        <div class="wizard-step-header mb-3">
          <h6 class="text-muted">Step ${step} / ${this.maxSteps}</h6>
          <div class="progress" style="height: 6px;">
            <div
              class="progress-bar bg-primary"
              role="progressbar"
              style="width: ${(step / this.maxSteps) * 100}%;"
            ></div>
          </div>
        </div>
        ${stepContent[step]}
      </div>
    `;
  }

  renderStep1() {
    return `
      <h5 class="mb-3">Step 1: 機器の準備</h5>
      <div class="alert alert-info">
        <strong>手順</strong>
        <ul class="mb-0 mt-2">
          <li>室温を 10〜30℃に保つ</li>
          <li>機器を壁面から 20cm 以上離す</li>
          <li>通気口が塞がれていないことを確認</li>
          <li>車輪ロックをON</li>
          <li>冷却ホース・電源ケーブルを接続</li>
          <li>主電源をON</li>
          <li>タッチスクリーンでログイン</li>
        </ul>
      </div>
      <div class="text-muted small">完了後、「次へ」をクリック</div>
    `;
  }

  renderStep2() {
    const isFirst = !!window.isFirstSession;
    const firstHtml = `
      <ul class="mb-0 mt-2">
        <li>初回は「Create New Patient」を選択</li>
        <li>Depression プロトコルを登録</li>
      </ul>`;
    const nextHtml = `
      <ul class="mb-0 mt-2">
        <li>Patient Search で患者カードを選択</li>
      </ul>`;
    return `
      <h5 class="mb-3">Step 2: ソフトウェアの準備</h5>
      <div class="alert alert-info">
        <strong>手順</strong>
        ${isFirst ? firstHtml : nextHtml}
      </div>
      <div class="text-muted small">完了後、「次へ」をクリック</div>
    `;
  }

  renderStep3() {
    return `
      <h5 class="mb-3">Step 3: 患者さんの準備（安全確認）</h5>
      <div class="alert alert-info">
        <div class="fw-bold">確認事項</div>
        <div class="small">チェック外れがある場合、<b>Step5で位置決め・MT測定を再度してください。</b></div>
      </div>
      <div class="d-flex flex-column gap-2">
        <div class="form-check form-switch border rounded bg-light" style="padding-left: 3rem;">
          <input 
            class="form-check-input" 
            type="checkbox" 
            id="wizard_safety_sleep"
            ${this.state.step3_safety_sleep ? 'checked' : ''}
            onchange="window.currentWizard.state.step3_safety_sleep = this.checked;"
          >
          <label class="form-check-label fw-bold ms-2" for="wizard_safety_sleep">睡眠不足がない</label>
        </div>
        <div class="form-check form-switch border rounded bg-light" style="padding-left: 3rem;">
          <input 
            class="form-check-input" 
            type="checkbox" 
            id="wizard_safety_alcohol"
            ${this.state.step3_safety_alcohol ? 'checked' : ''}
            onchange="window.currentWizard.state.step3_safety_alcohol = this.checked;"
          >
          <label class="form-check-label fw-bold ms-2" for="wizard_safety_alcohol">アルコール・カフェイン過剰摂取がない</label>
        </div>
        <div class="form-check form-switch border rounded bg-light" style="padding-left: 3rem;">
          <input 
            class="form-check-input" 
            type="checkbox" 
            id="wizard_safety_meds"
            ${this.state.step3_safety_meds ? 'checked' : ''}
            onchange="window.currentWizard.state.step3_safety_meds = this.checked;"
          >
          <label class="form-check-label fw-bold ms-2" for="wizard_safety_meds">服薬変更がない</label>
        </div>
      </div>
    `;
  }

  renderStep4() {
    return `
      <h5 class="mb-3">Step 4: ヘッドキャップ・グリッドの装着</h5>
      <div class="alert alert-info">
        <strong>手順</strong>
        <ul class="mb-0 mt-2">
          <li>ヘッドキャップを選択します（通常：女性M、男性L）。</li>
          <li>前端が眉の近くにくるように前から被り、中央線（赤線）が頭部中央に沿っていることを確認します。</li>
          <li>耳カバーをおろし、左右のストラップを交差させながら引張り後頭部を固定します。</li>
          <li>顎ストラップを留めます。</li>
          <li>グリッドは丸めてから、先端が鼻根部に来るように取り付けます。</li>
          <li>ヘッドキャップの中央線（赤線）と一致するように貼り付けます。</li>
        </ul>
      </div>
    `;
  }

  renderStep5() {
    const mtRequired = this.mtMeasurementRequired;
    if (mtRequired) {
      return `
        <h5 class="mb-3">Step 5: MT測定（必要）</h5>
        <div class="alert alert-danger">
          <i class="fas fa-exclamation-triangle me-2"></i>
          <strong>安全確認でチェック外れがあるため、MT再測定が必要です。</strong>
        </div>
        <div class="form-group">
          <label class="form-label fw-bold">測定日</label>
          <input 
            type="date" 
            class="form-control form-control-sm"
            value="${this.state.step5_mt_date}"
            onchange="window.currentWizard.state.step5_mt_date = this.value;"
          >
        </div>
        <div class="row">
          <div class="col-md-6">
            <div class="form-group">
              <label class="form-label fw-bold">MT値（%）</label>
              <input 
                type="number" 
                class="form-control form-control-sm"
                min="10" max="100"
                value="${this.state.step5_mt_value}"
                onchange="window.currentWizard.state.step5_mt_value = this.value;"
              >
              <small class="text-muted">10〜100の範囲で入力</small>
            </div>
          </div>
        </div>
        <div class="row">
          <div class="col-6">
            <div class="form-group">
              <label class="form-label fw-bold small">a_x</label>
              <input 
                type="number" 
                class="form-control form-control-sm"
                value="${this.state.step5_mt_a_x}"
                onchange="window.currentWizard.state.step5_mt_a_x = this.value;"
              >
            </div>
          </div>
          <div class="col-6">
            <div class="form-group">
              <label class="form-label fw-bold small">a_y</label>
              <input 
                type="number" 
                class="form-control form-control-sm"
                value="${this.state.step5_mt_a_y}"
                onchange="window.currentWizard.state.step5_mt_a_y = this.value;"
              >
            </div>
          </div>
        </div>
        <div class="row">
          <div class="col-6">
            <div class="form-group">
              <label class="form-label fw-bold small">b_x</label>
              <input 
                type="number" 
                class="form-control form-control-sm"
                value="${this.state.step5_mt_b_x}"
                onchange="window.currentWizard.state.step5_mt_b_x = this.value;"
              >
            </div>
          </div>
          <div class="col-6">
            <div class="form-group">
              <label class="form-label fw-bold small">b_y</label>
              <input 
                type="number" 
                class="form-control form-control-sm"
                value="${this.state.step5_mt_b_y}"
                onchange="window.currentWizard.state.step5_mt_b_y = this.value;"
              >
            </div>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label fw-bold">備考（任意）</label>
          <textarea 
            class="form-control form-control-sm"
            rows="2"
            onchange="window.currentWizard.state.step5_mt_note = this.value;"
          >${this.state.step5_mt_note}</textarea>
        </div>
      `;
    } else {
      return `
        <h5 class="mb-3">Step 5: MT測定</h5>
        <div class="alert alert-success">
          <i class="fas fa-check-circle me-2"></i>
          安全確認がすべてOKのため、MT再測定は不要です。
        </div>
        <div class="bg-light p-3 rounded">
          <strong>今週のMT設定:</strong>
          <p class="mb-1">MT値: <span class="fw-bold">未取得</span></p>
          <p class="mb-0">位置: <span class="fw-bold">未取得</span></p>
        </div>
        <div class="text-muted small mt-2">完了後、「次へ」をクリック</div>
      `;
    }
  }

  renderStep6() {
    const alertMsg = (window.wizardConfirmAlertMessage || '').trim();
    const todaySec = (window.getTodayTrainSeconds && window.getTodayTrainSeconds()) || '';
    const todayPct = (window.getTodayMtPercent && window.getTodayMtPercent()) || '';
    const mtDisp = (window.mtValueDisplay == null ? '' : String(window.mtValueDisplay));
    return `
      <h5 class="mb-3">Step 6: 確認用刺激</h5>
      <div class="mb-2 text-muted">確認用刺激をしてください。</div>
      ${alertMsg ? `<div class="alert alert-warning">${alertMsg}</div>` : ''}
      <div class="row g-3">
        <div class="col-7">
          <div class="d-flex gap-3 align-items-end mb-3">
            <div class="flex-fill">
              <label class="form-label fw-bold">確認刺激：刺激時間（秒）<span class="text-muted small">（任意）</span></label>
              <input type="number" step="0.5" min="0.5" class="form-control form-control-sm" value="${this.state.step6_confirm_seconds}"
                onchange="window.currentWizard.state.step6_confirm_seconds = this.value;">
            </div>
            <div class="flex-fill">
              <label class="form-label fw-bold">確認刺激：刺激強度（%MT）<span class="text-muted small">（任意）</span></label>
              <input type="number" step="10" min="80" max="140" class="form-control form-control-sm" value="${this.state.step6_confirm_percent}"
                onchange="window.currentWizard.state.step6_confirm_percent = this.value;">
            </div>
          </div>
          <div class="alert alert-info small">
            <strong>参考</strong>：本日の設定は <span class="fw-bold">${todaySec || '-'} 秒 / ${todayPct || '-'} %MT</span>（表示のみ）
            ${mtDisp ? `<div>MT（モーター閾値）：<span class="fw-bold">${mtDisp}</span></div>` : ''}
          </div>
          <div class="mt-3">
            <div class="form-check mt-2">
              <input class="form-check-input" type="checkbox" id="wizard_s6_discomfort" ${this.state.step6_discomfort ? 'checked' : ''}>
              <label class="form-check-label fw-bold" for="wizard_s6_discomfort">過剰な不快感（あり）</label>
            </div>
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="wizard_s6_movement" ${this.state.step6_movement ? 'checked' : ''}>
              <label class="form-check-label fw-bold" for="wizard_s6_movement">過剰な運動反応（あり）</label>
            </div>
          </div>
          <div class="form-group mt-3">
            <label class="form-label fw-bold small">メモ（任意）</label>
            <textarea class="form-control form-control-sm" rows="2" onchange="window.currentWizard.state.step6_note = this.value;">${this.state.step6_note}</textarea>
          </div>
        </div>
        <div class="col-5">
          <div id="s6_right_default" class="alert alert-light border">チェックを入れると対処方法が表示されます。</div>
          <div id="s6_box_discomfort" class="alert alert-warning" style="display:none;">
            <div class="fw-bold mb-1">過剰な不快感（あり）の場合</div>
            <ul class="mb-0">
              <li>不快感 → ヘルメットを内外軸に左右に0.5cmまたは1cm動かしてみる</li>
              <li>強い痛み → 刺激パラメータを調整
                <ul class="mb-0">
                  <li>例：0.5sec → 1sec → 2sec と段階的に</li>
                  <li>例：刺激強度 100%MT → 105%MT → 110%MT → 120%MT</li>
                </ul>
              </li>
            </ul>
          </div>
          <div id="s6_box_movement" class="alert alert-warning" style="display:none;">
            <div class="fw-bold mb-1">過剰な運動反応（あり）の場合</div>
            <ul class="mb-0">
              <li>肘から指の間に限局 → ヘルメットを1cm刻みで最大2cm前方に動かす</li>
              <li>肘から上 又は 下肢 → MT測定を再度実施する</li>
            </ul>
          </div>
        </div>
      </div>
    `;
  }

  renderStep7() {
    return `
      <h5 class="mb-3">Step 7: 治療刺激</h5>
      <div class="mb-2 text-muted">治療刺激をしてください。</div>
      <div class="row g-3">
        <div class="col-7">
          <div class="form-group mb-3">
            <label class="form-label fw-bold">実施した刺激強度（%MT）</label>
            <input type="number" step="10" min="80" max="140" class="form-control form-control-sm" value="${this.state.step7_mt_percent}"
              onchange="window.currentWizard.state.step7_mt_percent = this.value;">
            <small class="text-muted">初期値120、10刻み</small>
          </div>
          <div class="mt-2">
            <div class="form-check mt-2">
              <input class="form-check-input" type="checkbox" id="wizard_s7_discomfort" ${this.state.step7_discomfort ? 'checked' : ''}>
              <label class="form-check-label fw-bold" for="wizard_s7_discomfort">過剰な不快感（あり）</label>
            </div>
            <div class="form-check">
              <input class="form-check-input" type="checkbox" id="wizard_s7_movement" ${this.state.step7_movement ? 'checked' : ''}>
              <label class="form-check-label fw-bold" for="wizard_s7_movement">過剰な運動反応（あり）</label>
            </div>
          </div>
          <div class="form-group mt-3">
            <label class="form-label fw-bold small">メモ（任意）</label>
            <textarea class="form-control form-control-sm" rows="2" onchange="window.currentWizard.state.step7_note = this.value;">${this.state.step7_note}</textarea>
          </div>
        </div>
        <div class="col-5">
          <div id="s7_right_default" class="alert alert-light border">チェックを入れると対処方法が表示されます。</div>
          <div id="s7_box_discomfort" class="alert alert-warning" style="display:none;">
            <div class="fw-bold mb-1">過剰な不快感（あり）の場合</div>
            <ul class="mb-0">
              <li>不快感 → 治療を中断し、ヘルメットを内外軸に左右に0.5cmまたは1cm動かす</li>
            </ul>
          </div>
          <div id="s7_box_movement" class="alert alert-warning" style="display:none;">
            <div class="fw-bold mb-1">過剰な運動反応（あり）の場合</div>
            <ul class="mb-0">
              <li>肘から上 → 治療を中断する</li>
              <li>下肢 → 治療を中止する</li>
              <li>刺激と刺激の間にも四肢の動き → 治療を中止する</li>
              <li>肘から指の間に限局 → 治療を中断する。
                <ul class="mb-0">
                  <li>ヘルメットを1cm刻みで最大2cm前方に動かす</li>
                  <li>ヘルメットを内外軸に左右に0.5cmまたは1cm動かす</li>
                  <li>MT測定を再度実施する</li>
                  <li>→ 実施した刺激強度（%）を記入</li>
                </ul>
              </li>
            </ul>
          </div>
        </div>
      </div>
    `;
  }

  renderStep8() {
    // 有害事象チェック状態（治療画面のチェックも併用）
    const baseChecks = document.querySelectorAll('.sae-check');
    const anyChecked = Array.from(baseChecks).some(cb => cb.checked);
    const alertClass = anyChecked ? 'alert-warning' : 'alert-info';
    const alertIcon = anyChecked ? 'fa-exclamation-triangle text-danger' : 'fa-info-circle';
    const alertText = anyChecked ? '<strong class="text-danger">有害事象がチェックされています。報告書の作成・印刷を行ってください。</strong>' : '';

    return `
      <h5 class="mb-3">Step 8: 片付け・副作用／有害事象の確認</h5>
      <div class="alert alert-info small">
        <strong>片付け手順</strong>
        <ul class="mb-0 mt-2">
          <li>ヘルメット・ヘッドキャップを外す</li>
          <li>冷却装置をOFF</li>
          <li>患者さんから装置を全て外す</li>
        </ul>
      </div>

      <div class="mt-3 p-3 bg-warning bg-opacity-10 rounded">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <div class="fw-bold text-danger"><i class="fas fa-exclamation-circle me-1"></i>副作用・有害事象の確認</div>
          <div class="small text-muted">副作用チェック表 → 必要なら有害事象報告へ</div>
        </div>

        <div class="mb-3">
          <button type="button" class="btn btn-sm btn-outline-danger" onclick="window.currentWizard.openSideEffectModal();">
            <i class="fas fa-check-circle me-1"></i>副作用入力（モーダルを開く）
          </button>
          <div class="small text-muted mt-1">副作用チェック表を先に入力してください。</div>
        </div>

        <div class="alert ${alertClass} small mb-2">
          <div class="fw-bold mb-1"><i class="fas ${alertIcon} me-1"></i>重篤を含む有害事象の確認</div>
          <p class="mb-2">該当する場合はチェックし、報告書を作成・送付してください。</p>
          <div class="d-flex flex-wrap gap-3" id="wizardSaeChecks">
            ${[
              ['sae_seizure','けいれん発作'],
              ['sae_finger_muscle','手指の筋収縮'],
              ['sae_syncope','失神'],
              ['sae_mania','躁病・軽躁病の出現'],
              ['sae_suicide_attempt','自殺企図'],
              ['sae_other','その他']
            ].map(([id,label]) => {
              const checked = document.getElementById(id)?.checked ? 'checked' : '';
              return `<label class="form-check form-check-inline">
                        <input class="form-check-input wizard-sae-check" type="checkbox" data-target-id="${id}" ${checked}>
                        <span class="form-check-label">${label}</span>
                      </label>`;
            }).join('')}
          </div>
          <div class="mt-2 p-2 bg-white bg-opacity-50 rounded">
            <div class="small fw-bold">送付先メールアドレス</div>
            <code class="user-select-all">brainsway_saeinfo@cmi.co.jp</code>
          </div>
          ${alertText ? `<div class="mt-2 text-danger small">${alertText}</div>` : ''}
        </div>

        <div class="d-flex flex-column gap-2">
          <button type="button" class="btn btn-sm btn-danger" id="wizardOpenAEModalBtn" onclick="window.currentWizard.openAdverseEventModal()">
            <i class="fas fa-file-medical me-1"></i>有害事象入力・報告書を開く
          </button>
        </div>
        <div class="mt-2 small text-muted">
          ※ 有害事象チェックがある場合は報告書を作成し、印刷して送付してください。
        </div>
      </div>
    `;
  }

  renderStep9() {
    return `
      <h5 class="mb-3">Step 9: 終業処理</h5>
      <div class="alert alert-info small">
        <strong>手順</strong>
        <ul class="mb-0 mt-2">
          <li>ソフトウェアをシャットダウン</li>
          <li>主電源をOFF</li>
          <li>週1回、USBバックアップを実施</li>
          <li>必要に応じてレポートをDL</li>
        </ul>
      </div>
      <div class="mt-4 alert alert-success">
        <i class="fas fa-check-circle me-2"></i>
        <strong>全ステップ完了です！</strong>
        <p class="mb-0 small mt-2">「完了（保存して閉じる）」をクリックすると保存します。</p>
      </div>
    `;
  }

  /**
   * ステップ固有のイベントリスナー附与
   */
  attachStepEventListeners() {
    // Step6: 対処表示のトグル
    if (this.currentStep === 6) {
      const d = document.getElementById('wizard_s6_discomfort');
      const m = document.getElementById('wizard_s6_movement');
      if (d) d.addEventListener('change', (e) => { this.state.step6_discomfort = e.target.checked; this.updateS6Boxes(); });
      if (m) m.addEventListener('change', (e) => { this.state.step6_movement = e.target.checked; this.updateS6Boxes(); });
      // initial update
      this.updateS6Boxes();
    }
    // Step7: 対処表示のトグル
    if (this.currentStep === 7) {
      const d = document.getElementById('wizard_s7_discomfort');
      const m = document.getElementById('wizard_s7_movement');
      if (d) d.addEventListener('change', (e) => { this.state.step7_discomfort = e.target.checked; this.updateS7Boxes(); });
      if (m) m.addEventListener('change', (e) => { this.state.step7_movement = e.target.checked; this.updateS7Boxes(); });
      // initial update
      this.updateS7Boxes();
    }

    // Step8: SAEチェックの同期とボタン活性/非活性
    if (this.currentStep === 8) {
      const wizardChecks = document.querySelectorAll('.wizard-sae-check');
      const update = () => {
        wizardChecks.forEach(cb => {
          const targetId = cb.dataset.targetId;
          if (targetId) {
            const target = document.getElementById(targetId);
            if (target) {
              target.checked = cb.checked;
            }
          }
        });

        const any = Array.from(wizardChecks).some(c => c.checked);
        const btnModal = document.getElementById('wizardOpenAEModalBtn');
        const btnPreview = document.getElementById('wizardAEPvwBtn');
        if (btnModal) btnModal.disabled = !any;
        if (btnPreview) btnPreview.disabled = !any;
      };

      wizardChecks.forEach(cb => cb.addEventListener('change', update));
      // 初期反映
      update();
    }
  }

  updateS6Boxes() {
    const def = document.getElementById('s6_right_default');
    const b1 = document.getElementById('s6_box_discomfort');
    const b2 = document.getElementById('s6_box_movement');
    if (!def || !b1 || !b2) return;
    const any = !!this.state.step6_discomfort || !!this.state.step6_movement;
    def.style.display = any ? 'none' : 'block';
    b1.style.display = this.state.step6_discomfort ? 'block' : 'none';
    b2.style.display = this.state.step6_movement ? 'block' : 'none';
  }

  updateS7Boxes() {
    const def = document.getElementById('s7_right_default');
    const b1 = document.getElementById('s7_box_discomfort');
    const b2 = document.getElementById('s7_box_movement');
    if (!def || !b1 || !b2) return;
    const any = !!this.state.step7_discomfort || !!this.state.step7_movement;
    def.style.display = any ? 'none' : 'block';
    b1.style.display = this.state.step7_discomfort ? 'block' : 'none';
    b2.style.display = this.state.step7_movement ? 'block' : 'none';
  }

  /**
   * 副作用モーダルを開く
   */
  openSideEffectModal() {
    this.state.step8_side_effect_opened = true;
    const sideEffectModal = document.getElementById('sideEffectModal');
    if (sideEffectModal) {
      const modal = bootstrap.Modal.getOrCreateInstance(sideEffectModal);
      modal.show();
    }
  }

  /**
   * 有害事象報告書モーダルを開く
   */
  openAdverseEventModal() {
    const saeModal = document.getElementById('saeModal');
    if (saeModal) {
      const modal = bootstrap.Modal.getOrCreateInstance(saeModal);
      modal.show();
    }
  }

  /**
   * 有害事象報告書印刷プレビューを開く
   */
  openAdverseEventPreview() {
    const modal = document.getElementById('procedureWizardModal');
    const sessionId = modal?.dataset?.sessionId;
    if (!sessionId) {
      alert('セッション情報が見つかりません。');
      return;
    }
    // 存在確認
    fetch(`/app/session/${sessionId}/adverse-event/print/`)
      .then(res => {
        if (res.ok) {
          window.open(`/app/session/${sessionId}/adverse-event/print/`, '_blank');
        } else {
          alert('有害事象報告書がまだ作成されていません。先に「有害事象入力・報告書を開く」から作成してください。');
        }
      })
      .catch(() => {
        alert('報告書の確認に失敗しました。');
      });
  }

  /**
   * ユーティリティ
   */
  getPatientIdFromURL() {
    const match = window.location.pathname.match(/\/patient\/(\d+)\//);
    return match ? match[1] : null;
  }

  getCourseNumberFromForm() {
    // フォーム内から course_number を取得（hidden input or from patient info）
    const form = document.getElementById('treatmentForm');
    if (form) {
      const courseInput = form.querySelector('input[name="course_number"]');
      if (courseInput) {
        return courseInput.value;
      }
    }
    // フォーム内にない場合、ページから抽出
    const courseText = document.querySelector('.patient-name')?.textContent;
    // クール数がページタイトルに含まれているなら抽出
    const match = document.body.textContent.match(/(\d+)クール/);
    return match ? match[1] : '1';
  }

  getCsrfToken() {
    return document.querySelector('[name="csrfmiddlewaretoken"]')?.value || '';
  }
}

// グローバルインスタンス
window.currentWizard = new ProcedureWizard();

// DOMReady時に初期化
document.addEventListener('DOMContentLoaded', () => {
  window.currentWizard.init();
});
