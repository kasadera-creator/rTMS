# rTMS Support System Feature Enhancement
## Implementation Summary

**Date**: 2025年12月24日  
**Features**: Protocol Branching, SAE Reporting, HAM-D Real-time Evaluation

---

## 1. プロトコル分岐機能 (Protocol Branching)

### 概要
患者コース単位でプロトコル（保険診療 / 市販後調査）を選択できるようにしました。

### 実装内容

#### 1.1 データモデル
**ファイル**: `rtms_app/models.py`
- `Patient` モデルに `protocol_type` フィールドを追加
  - Choices: `INSURANCE` (保険診療プロトコル), `PMS` (市販後調査プロトコル)
  - デフォルト: `INSURANCE`
  - 既存データは全て `INSURANCE` として扱われます

#### 1.2 プロトコル抽象化層
**ファイル**: `rtms_app/protocols.py`
- `ProtocolSpec` データクラス: プロトコル仕様を定義
  - 治療回数 (`total_sessions`)
  - 評価必須週 (`required_evaluation_weeks`)
  - 早期漸減可否 (`allow_early_taper`)
- `get_protocol(patient)` 関数: 患者のプロトコル仕様を取得
- 将来のプロトコル追加・変更に対応しやすい設計

#### 1.3 UI/フォーム
**ファイル**: 
- `rtms_app/forms.py`: `PatientFirstVisitForm` にプロトコル選択ラジオボタンを追加
- `rtms_app/templates/rtms_app/patient_first_visit.html`: プロトコル選択UIを表示
- `rtms_app/views.py`: `patient_first_visit` ビューで保存処理

### 使い方
1. 初診時画面で「プロトコル」セクションにラジオボタンが表示されます
2. デフォルトは「保険診療プロトコル」が選択されています
3. 選択後、保存すると患者レコードに記録されます

---

## 2. SAE (重篤有害事象) 報告書機能

### 概要
治療実施画面で重篤な有害事象をチェックすると、Brainsway社向けの報告書を自動生成できます。

### 実装内容

#### 2.1 データモデル
**ファイル**: `rtms_app/models.py`
- `SeriousAdverseEvent` モデルを新設
  - `event_types` (JSON): チェックされたイベント種別
  - `other_text` (Text): 「その他」の詳細
  - `auto_snapshot` (JSON): 発生時の治療条件スナップショット
  - 患者・コース・セッションへの外部キー

#### 2.2 UI
**ファイル**: `rtms_app/templates/rtms_app/treatment_add.html`
- 治療実施画面に「重篤を含む有害事象」セクションを追加
  - チェックボックス: けいれん発作、手指の筋収縮、失神、躁病・軽躁病、自殺企図、その他
  - いずれかがチェックされると、報告書作成が必要な旨を表示
  - 「その他」には詳細テキスト入力欄

#### 2.3 報告書生成サービス
**ファイル**: `rtms_app/services/sae_report.py`
- `build_sae_context(session, sae_record)`: 報告書用コンテキストを構築
  - 患者情報、発生日、治療条件、併用薬などを自動入力
  - スナップショットから当時の情報を復元
- `render_sae_docx(template_path, context)`: Word文書を生成 (python-docx使用)
- `get_missing_fields(context)`: 未入力フィールドの一覧を取得

**テンプレート**: `rtms_app/templates_docs/brainsway_sae_template.docx`
- プレースホルダ形式: `{PATIENT_INITIAL}`, `{EVENT_DATE}` など
- **注意**: 現在はプレースホルダ文書のみ。本番運用前に添付PDFを基にした実際の.docxテンプレートと差し替えてください。

#### 2.4 エンドポイント
**URL**: `/app/session/<session_id>/sae_report.docx`
**ビュー**: `views.sae_report_docx` (`rtms_app/views.py`)
- Word文書をダウンロード
- ファイル名: `SAE_Report_{患者ID}_{日付}.docx`
- 送付先メールアドレス: `brainsway_saeinfo@cmi.co.jp` (報告書内に記載)

### 使い方
1. 治療実施画面で有害事象のチェックボックスをON
2. 「その他」の場合は詳細テキストを入力
3. 保存すると `SeriousAdverseEvent` レコードとスナップショットが作成されます
4. (将来) 報告書作成ボタンから Word をダウンロード

### 今後の作業
- [ ] `brainsway_sae_template.docx` を実際のフォーマットで作成
- [ ] 報告書ダウンロードボタンを治療画面に追加 (既存セッション編集時)
- [ ] 未入力フィールド一覧の表示UI

---

## 3. HAM-D リアルタイム評価機能

### 概要
HAM-D入力時に合計点・改善率・評価判定（反応なし/反応/寛解）をリアルタイム表示します。

### 実装内容

#### 3.1 データモデル
**ファイル**: `rtms_app/models.py`
- `AssessmentRecord` モデルに追加
  - `improvement_rate_17` (Float): 改善率（baseline比）
  - `status_label` (CharField): 判定ラベル（"反応なし" / "反応" / "寛解" / "未評価"）

#### 3.2 評価ルール設定
**ファイル**: `rtms_app/assessment_rules.py`
- 閾値定義:
  - `RESPONSE_RATE_THRESHOLD = 0.50` (50%改善で「反応」)
  - `REMISSION_HAMD17_THRESHOLD = 7` (7点以下で「寛解」)
- ヘルパー関数:
  - `compute_improvement_rate(baseline, current)`: 改善率計算
  - `classify_response_status(score_17, improvement)`: ステータス判定
  - `classify_hamd17_severity(score)`: 重症度分類

#### 3.3 JavaScript拡張
**ファイル**: `rtms_app/static/rtms_app/hamd_widget.js`
- `calcHAMD17()` 関数を拡張:
  - baseline スコアと現在スコアから改善率を計算
  - ステータス判定ロジック (寛解 > 反応 > 反応なし)
  - `#hamd17Improvement`, `#hamd17Status` 要素を更新
- グローバル変数:
  - `window.HAMD_BASELINE_17`: baseline HAM-D17 スコア
  - `window.HAMD_SHOW_IMPROVEMENT`: 改善率表示フラグ (baselineでは非表示)

#### 3.4 テンプレート
**ファイル**: `rtms_app/templates/rtms_app/assessment/scales/hamd.html`
- スコアバーに改善率・判定欄を追加 (baseline以外)
- `baseline_score_17` をコンテキストから受け取り、JSに渡す

#### 3.5 バックエンド保存処理
**ファイル**: `rtms_app/views.py` (`assessment_scale_form`)
- HAM-D保存時に改善率・ステータスを計算して `AssessmentRecord` に保存
- 第3週保存時のAJAXレスポンスにメッセージを追加:
  - 「寛解」→ 漸減プロトコルへの移行を推奨
  - 「反応なし」→ 治療継続/中止を検討
  - 「反応」→ 治療継続

### 使い方
1. HAM-D入力画面 (第3週/第4週/第6週) を開く
2. 質問項目をボタンで選択
3. 画面下部のスコアバーに即座に表示:
   - **合計**: HAM-D17 合計点
   - **重症度**: 正常/軽症/中等症/重症/最重症
   - **改善率**: (baseline - 現在) / baseline × 100%
   - **判定**: 反応なし / 反応 / 寛解
4. 保存すると判定がDBに記録され、第3週の場合は画面にメッセージ表示

---

## 4. マイグレーション

**ファイル**: `rtms_app/migrations/0024_protocol_sae_report_fields.py`

適用内容:
- `Patient.protocol_type` フィールド追加
- `AssessmentRecord.improvement_rate_17`, `status_label` フィールド追加
- `SeriousAdverseEvent` モデル作成

**適用済み**: `python manage.py migrate` 実行完了

---

## 5. 受け入れ基準の達成状況

### 1) プロトコル選択
- ✅ 初診時にラジオボタンで選択可能
- ✅ デフォルトは「保険診療プロトコル」
- ✅ `Course` (Patient) に `protocol_type` が保存される
- ✅ 将来のプロトコル拡張に対応可能な設計

### 2) SAE報告書
- ✅ 治療画面にSAEチェックボックスを配置
- ✅ チェック時に報告書作成導線を表示
- ✅ スナップショット機能で当時の情報を保存
- ✅ Word出力エンドポイント (`/app/session/<id>/sae_report.docx`)
- ✅ 送付先メールアドレスを記載
- ⚠️ テンプレート文書は仮版 (実際のフォーマットは後日作成)

### 3) HAM-D リアルタイム評価
- ✅ 入力中に合計・改善率・評価が即時更新
- ✅ DB保存時も同じ値が記録される
- ✅ 第3週保存後に判定メッセージを表示
- ✅ 閾値は `assessment_rules.py` で管理 (将来の調整容易)

---

## 6. 今後の推奨事項

### 優先度: 高
1. **SAE Word テンプレート作成**
   - 添付PDF (`brainsway_saeinfo.pdf`) を基に、実際の .docx テンプレートを作成
   - プレースホルダを配置し、`rtms_app/templates_docs/brainsway_sae_template.docx` と差し替え

2. **報告書ダウンロードボタンの実装**
   - 治療画面編集時 (既存セッション) にSAE報告書ボタンを表示
   - 未入力フィールド一覧UIの追加

### 優先度: 中
3. **プロトコル特化ロジックの拡張**
   - `protocols.py` の `ProtocolSpec` に必要な項目を追加
   - 治療回数上限、評価週強制入力などをプロトコルごとに制御

4. **HAM-D第3週メッセージのUI改善**
   - モーダルまたは固定バナーで目立たせる
   - 漸減プロトコルへの遷移導線を追加

### 優先度: 低
5. **SAE履歴表示**
   - 患者サマリー画面にSAE一覧を表示
   - 報告書再ダウンロード機能

6. **プロトコル切り替え機能**
   - コース途中でプロトコル変更が必要な場合の対応

---

## 7. テスト推奨項目

1. **プロトコル選択**
   - [ ] 初診時画面でラジオボタンが表示される
   - [ ] デフォルトで「保険診療プロトコル」が選択されている
   - [ ] PMS選択後も正常に保存される

2. **SAE機能**
   - [ ] 治療画面でSAEチェックボックスが表示される
   - [ ] チェック時に警告バナーが表示される
   - [ ] 保存後に `SeriousAdverseEvent` レコードが作成される
   - [ ] Word出力が `/app/session/<id>/sae_report.docx` からダウンロードできる

3. **HAM-D評価**
   - [ ] baseline入力時は改善率・判定が非表示
   - [ ] 第3週入力時にリアルタイムで改善率・判定が更新される
   - [ ] 保存後に `AssessmentRecord` に値が記録される
   - [ ] 第3週で「寛解」の場合、保存後にメッセージが表示される

---

## 8. ファイル一覧

### 新規作成
- `rtms_app/protocols.py`
- `rtms_app/assessment_rules.py`
- `rtms_app/services/sae_report.py`
- `rtms_app/templates_docs/brainsway_sae_template.docx` (仮版)
- `rtms_app/migrations/0024_protocol_sae_report_fields.py`

### 変更
- `rtms_app/models.py`
- `rtms_app/forms.py`
- `rtms_app/views.py`
- `rtms_app/urls.py`
- `rtms_app/static/rtms_app/hamd_widget.js`
- `rtms_app/templates/rtms_app/patient_first_visit.html`
- `rtms_app/templates/rtms_app/treatment_add.html`
- `rtms_app/templates/rtms_app/assessment/scales/hamd.html`

---

## 9. 技術的注意事項

### python-docx 依存関係
SAE報告書生成には `python-docx` が必要です。未インストールの場合:
```bash
pip install python-docx
```

### テンプレート文書の作成方法
1. Microsoft Word または LibreOffice で新規文書作成
2. 添付PDF (`brainsway_saeinfo.pdf`) と同じレイアウトを再現
3. 自動入力箇所を `{PLACEHOLDER}` 形式で記載
4. `.docx` 形式で保存
5. `rtms_app/templates_docs/brainsway_sae_template.docx` と差し替え

---

**実装完了日**: 2025年12月24日  
**実装者**: GitHub Copilot
