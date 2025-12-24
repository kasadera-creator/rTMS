# 治療実施画面UI改修：副作用入力ボタン配置 + SAE導線統合

**実装日**: 2025年12月24日  
**対応内容**: 副作用入力ボタンの位置変更、SAE入力モーダルの追加

---

## 1. 副作用入力ボタンの配置変更

### 実装内容

#### 1.1 確認刺激セクション
- **配置**: 「過剰な不快感」「過剰な運動反応」チェックボックスの**直上**に配置
- **ボタン**: [副作用入力] ボタン（緑色、Bootstrap btn-success）
- **動作**: クリックで副作用入力モーダルを開く

**ファイル**: [treatment_add.html](rtms_app/templates/rtms_app/treatment_add.html#L240-L250)

```html
<div class="d-flex justify-content-between align-items-center mb-2">
  <div class="fw-bold text-muted small">副作用チェック（確認刺激）</div>
  <button type="button" class="btn btn-success btn-sm fw-bold"
          data-bs-toggle="modal" data-bs-target="#sideEffectModal">
    <i class="fas fa-notes-medical me-1"></i>副作用入力
  </button>
</div>
```

#### 1.2 実施した治療刺激セクション
- **配置**: 「過剰な不快感」「過剰な運動反応」チェックボックスの**直上**に配置
- **ボタン**: [副作用入力] ボタン（緑色、Bootstrap btn-success）
- **動作**: 同じモーダルを開く（副作用は両セクション共通管理）

**ファイル**: [treatment_add.html](rtms_app/templates/rtms_app/treatment_add.html#L344-L354)

#### 1.3 削除した要素
- 下部にあった重複する副作用入力ボタンを削除
- SAEセクション下の副作用ボタンを削除（導線を統一）

### UI配置イメージ

```
┌─ 確認刺激 ──────────────────────┐
│ 刺激強度、周波数など              │
├────────────────────────────────┤
│ [副作用入力] ← 新規配置           │
│ □ 過剰な不快感                   │
│ □ 過剰な運動反応                 │
│ [備考テキストエリア]              │
└────────────────────────────────┘

┌─ 実施した治療刺激 ──────────────┐
│ 刺激強度、周波数など              │
├────────────────────────────────┤
│ [副作用入力] ← 新規配置           │
│ □ 過剰な不快感                   │
│ □ 過剰な運動反応                 │
│ [備考テキストエリア]              │
└────────────────────────────────┘
```

---

## 2. SAE（重篤有害事象）導線の統合

### 2.1 UI要件

#### SAEチェックボックスセクション
- **位置**: 実施した治療刺激セクション下部（alert-danger）
- **ボタン**: [SAE入力・報告書] ボタン（赤色、alert内右上）
- **動作**: 
  - チェック状態に応じてボタン強調（btn-danger / btn-outline-danger）
  - クリックでSAE入力モーダルを開く

**ファイル**: [treatment_add.html](rtms_app/templates/rtms_app/treatment_add.html#L374-L381)

```html
<div class="alert alert-danger">
  <div class="d-flex justify-content-between align-items-center mb-2">
    <div class="fw-bold">重篤を含む有害事象（該当する場合チェック）</div>
    <button type="button" class="btn btn-danger btn-sm fw-bold" id="saeInputBtn"
            data-bs-toggle="modal" data-bs-target="#saeModal">
      <i class="fas fa-exclamation-triangle me-1"></i>SAE入力・報告書
    </button>
  </div>
  <!-- 6種類のチェックボックス -->
</div>
```

#### SAE入力モーダル
**新規追加**: `#saeModal` モーダル

**構成要素**:
1. **ヘッダー**: 赤背景、白文字（bg-danger text-white）
2. **チェック状態表示**: 選択された事象をバッジで表示
3. **その他詳細**: テキストエリア（フォームと同期）
4. **追加メモ**: 症状、対応、経過などを記載
5. **報告書セクション**: 保存済みSAEがある場合のみ表示
   - [報告書を出力（Word）] ボタン → `/app/session/<id>/sae_report.docx` へ遷移

**ファイル**: [treatment_add.html](rtms_app/templates/rtms_app/treatment_add.html#L471-L527)

### 2.2 バックエンド実装

#### TreatmentSessionモデル拡張
**ファイル**: [models.py](rtms_app/models.py#L168-L171)

```python
def has_sae(self):
    """重篤有害事象レコードが存在するかを判定"""
    return hasattr(self, 'sae_records') and self.sae_records.exists()
```

#### SAEデータの読み込み（治療画面）
**ファイル**: [views.py](rtms_app/views.py#L1527-L1543) treatment_add関数

```python
# 既存SAEデータの読み込み
existing_sae = None
sae_event_types_checked = {}
sae_other_text_value = ''
if existing_session:
    from .models import SeriousAdverseEvent
    existing_sae = SeriousAdverseEvent.objects.filter(
        patient=patient, 
        course_number=course_number, 
        session=existing_session
    ).first()
    if existing_sae:
        # チェックボックスの状態を復元
        for event_code in existing_sae.event_types:
            sae_event_types_checked[f'sae_{event_code}'] = True
        sae_other_text_value = existing_sae.other_text or ''
```

**テンプレートへ渡すコンテキスト**:
- `session`: TreatmentSessionオブジェクト（Word出力URL用）
- `existing_sae`: SeriousAdverseEventオブジェクト（あれば）
- `sae_event_types_checked`: チェックボックス復元用dict
- `sae_other_text_value`: その他テキストの値

#### チェックボックス初期値の反映
**ファイル**: [treatment_add.html](rtms_app/templates/rtms_app/treatment_add.html#L384-L391)

```django
{% if sae_event_types_checked.sae_seizure %}checked{% endif %}
```

### 2.3 JavaScript実装

#### SAE通知バナー表示制御
**機能**: チェックが1つでもONになると警告バナー表示

```javascript
function updateSAENotice() {
  const checks = document.querySelectorAll('.sae-check');
  const notice = document.getElementById('sae_notice');
  const saeBtn = document.getElementById('saeInputBtn');
  const any = Array.from(checks).some(c => c.checked);
  if (notice) notice.classList.toggle('d-none', !any);
  // ボタンを強調表示
  if (saeBtn) {
    if (any) {
      saeBtn.classList.add('btn-danger');
      saeBtn.classList.remove('btn-outline-danger');
    } else {
      saeBtn.classList.remove('btn-danger');
      saeBtn.classList.add('btn-outline-danger');
    }
  }
}
```

#### SAEモーダルのチェック状態同期
**機能**: モーダル表示時にフォームのチェック状態を読み取り、バッジ表示

```javascript
function updateSAEModal() {
  const checks = document.querySelectorAll('.sae-check');
  const checkedDiv = document.getElementById('saeCheckedEvents');
  const checkedEvents = [];
  
  checks.forEach(c => {
    if (c.checked) {
      const label = c.parentElement.querySelector('.form-check-label');
      if (label) checkedEvents.push(label.textContent);
    }
  });

  if (checkedEvents.length > 0) {
    checkedDiv.innerHTML = checkedEvents.map(e => 
      `<span class="badge bg-danger me-1">${e}</span>`
    ).join('');
  } else {
    checkedDiv.innerHTML = '<span class="text-muted small">事象をチェックしてください</span>';
  }

  // その他フィールドの同期
  const otherText = document.getElementById('sae_other_text');
  const modalOtherText = document.getElementById('saeModalOtherText');
  if (otherText && modalOtherText) {
    modalOtherText.value = otherText.value;
  }
}
```

#### 報告書セクション表示制御
**機能**: 既存SAEレコードがある場合のみ報告書ダウンロードボタン表示

```javascript
saeModal.addEventListener('show.bs.modal', () => {
  updateSAEModal();
  
  // 既存SAEレコードがある場合は報告書セクションを表示
  const sessionId = '{{ session.id|default:"" }}';
  const existingSae = {% if existing_sae %}true{% else %}false{% endif %};
  const reportSection = document.getElementById('saeReportSection');
  
  if (reportSection && sessionId && existingSae) {
    reportSection.classList.remove('d-none');
  } else if (reportSection) {
    reportSection.classList.add('d-none');
  }
});
```

#### Word出力ボタン
**機能**: 保存済みセッションのSAE報告書をダウンロード

```javascript
const saeDownloadDocx = document.getElementById('saeDownloadDocx');
if (saeDownloadDocx) {
  saeDownloadDocx.addEventListener('click', () => {
    const sessionId = '{{ session.id|default:"" }}';
    if (sessionId) {
      window.location.href = `/app/session/${sessionId}/sae_report.docx`;
    } else {
      alert('先に治療データを保存してください。');
    }
  });
}
```

---

## 3. ユーザー体験フロー

### 3.1 副作用入力フロー

1. 治療画面で「確認刺激」または「実施した治療刺激」セクションを確認
2. チェックボックス直上の **[副作用入力]** ボタンをクリック
3. 副作用入力モーダルが開く
   - 既存の副作用ウィジェットで詳細入力
   - メモ欄に過剰な不快感・運動反応の詳細を記載
4. モーダルを閉じて治療データを保存

### 3.2 SAE入力・報告書フロー

#### 新規SAE発生時
1. 治療実施中にSAEが発生
2. SAEセクションで該当事象をチェック（けいれん発作、失神など）
3. **[SAE入力・報告書]** ボタンをクリック（赤色強調）
4. SAEモーダルが開く
   - チェックした事象が赤バッジで表示される
   - 「その他 詳細」「追加メモ」を入力
5. **[SAEデータを保存]** をクリック
   - モーダルが閉じる
   - 「保存」ボタンで治療データを保存するよう案内
6. 治療画面の **[保存]** ボタンをクリック
   - SAEデータがDBに保存される

#### 報告書作成時
1. 保存済みのセッションを再度開く
2. **[SAE入力・報告書]** ボタンをクリック
3. モーダル内に「保存済みSAEデータ」セクションが表示される
4. **[報告書を出力（Word）]** ボタンをクリック
5. Word文書（.docx）がダウンロードされる
   - ファイル名: `SAE_Report_{患者ID}_{日付}.docx`
   - 送付先: brainsway_saeinfo@cmi.co.jp

---

## 4. デバッグ確認項目

### 4.1 SAEチェック保存の確認

**問題**: チェックしても報告書作成画面が出ない

**確認項目**:
1. ✅ チェックボックスの `name` がフォームフィールドと一致
   - `sae_seizure`, `sae_finger_muscle`, `sae_syncope`, `sae_mania`, `sae_suicide_attempt`, `sae_other`
2. ✅ POST先が `treatment_add` ビューに届いている
3. ✅ `views.py` でSAE保存ロジックが実行されている
   - [views.py#L1237-L1288](rtms_app/views.py#L1237-L1288)
4. ✅ 保存後のリロードでDB値が残っている
   - Django Admin: `SeriousAdverseEvent` テーブル確認
5. ✅ テンプレートで `sae_event_types_checked` 変数が正しく参照されている

### 4.2 テンプレート変数の確認

**views.pyから渡されるコンテキスト**:
- `session`: TreatmentSessionオブジェクト（既存セッション編集時のみ）
- `existing_sae`: SeriousAdverseEventオブジェクト（該当レコードがあれば）
- `sae_event_types_checked`: dict型、キーは `sae_seizure` など
- `sae_other_text_value`: str型

### 4.3 JavaScript動作確認

**ブラウザコンソールで確認**:
```javascript
// SAEチェック状態
document.querySelectorAll('.sae-check').forEach(c => console.log(c.name, c.checked));

// モーダル表示
bootstrap.Modal.getOrCreateInstance(document.getElementById('saeModal')).show();

// セッションID
console.log('{{ session.id|default:"" }}');
```

---

## 5. 変更ファイル一覧

### 新規作成
- なし（既存ファイルの修正のみ）

### 変更
1. [rtms_app/models.py](rtms_app/models.py)
   - `TreatmentSession.has_sae()` メソッド追加

2. [rtms_app/views.py](rtms_app/views.py)
   - `treatment_add` 関数: SAEデータ読み込みロジック追加
   - テンプレートコンテキストに `session`, `existing_sae`, `sae_event_types_checked`, `sae_other_text_value` 追加

3. [rtms_app/templates/rtms_app/treatment_add.html](rtms_app/templates/rtms_app/treatment_add.html)
   - 確認刺激セクションに副作用入力ボタン追加
   - 実施した治療刺激セクションに副作用入力ボタン追加
   - SAEセクションにSAE入力・報告書ボタン追加
   - SAE入力モーダル（`#saeModal`）追加
   - SAEチェックボックスに `id` と初期値設定追加
   - JavaScript: SAE通知・モーダル制御ロジック更新

---

## 6. テスト推奨項目

### 6.1 副作用入力ボタン配置
- [ ] 確認刺激セクションにボタンが表示される
- [ ] 実施した治療刺激セクションにボタンが表示される
- [ ] ボタンをクリックすると副作用入力モーダルが開く
- [ ] iPad横表示でもボタンが押しやすい位置にある

### 6.2 SAE導線
- [ ] SAEチェックボックスにチェックを入れると警告バナー表示
- [ ] SAE入力・報告書ボタンがチェック時に赤色強調
- [ ] ボタンをクリックするとSAEモーダルが開く
- [ ] モーダル内にチェックした事象が赤バッジで表示される
- [ ] 「その他詳細」がフォームと同期される
- [ ] SAEデータを保存後、メインフォーム保存でDBに記録される

### 6.3 既存セッション編集時
- [ ] 保存済みのSAEデータがあるセッションを開く
- [ ] SAEチェックボックスに初期値が復元される
- [ ] 「その他詳細」テキストが復元される
- [ ] SAEモーダルを開くと「保存済みSAEデータ」セクションが表示される
- [ ] 報告書を出力（Word）ボタンをクリックすると.docxがダウンロードされる

### 6.4 報告書出力
- [ ] Word文書が正しいファイル名でダウンロードされる
- [ ] 文書内にSAEスナップショット情報が記載されている
- [ ] 送付先メールアドレスが記載されている

---

## 7. 既知の制限事項

### 7.1 SAE入力の保存タイミング
- モーダル内の「SAEデータを保存」ボタンは、モーダルを閉じるのみ
- 実際のDB保存は、メインフォームの「保存」ボタンをクリックする必要がある
- **理由**: 治療データとSAEデータを同一トランザクションで保存するため

### 7.2 未保存セッションでの報告書出力
- 新規セッション作成中（未保存）は報告書ボタンが表示されない
- 先に治療データを保存してセッションIDを確定する必要がある

---

## 8. 今後の改善案

### 優先度: 高
1. **SAE追加メモのDB保存**
   - 現在: モーダル内の「追加メモ」はDB保存されていない
   - 改善: `SeriousAdverseEvent.memo` フィールドを追加

2. **報告書プレビュー機能**
   - Word出力前に内容をHTML表示
   - 未入力フィールドのハイライト表示

### 優先度: 中
3. **SAE履歴表示**
   - 患者サマリー画面にSAE一覧を表示
   - 過去のSAE報告書を再ダウンロード

4. **SAE保存の非同期化**
   - メインフォーム保存を待たずにSAEデータのみ先行保存
   - AJAX POST で即座にDB記録

---

**実装完了日**: 2025年12月24日  
**実装者**: GitHub Copilot
