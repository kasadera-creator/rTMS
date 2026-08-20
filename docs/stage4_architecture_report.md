# rTMS支援システム 第4段階アーキテクチャ報告書（READ-ONLY調査・コード変更なし）

作成日: 2026-08-20
前提資料: [docs/system_structure_report.md](system_structure_report.md), [docs/refactoring_plan.md](refactoring_plan.md), [docs/static_template_refactoring_report.md](static_template_refactoring_report.md)
本書の目的: 実運用に近い現行システムの「URL→View→Template→partial→JS→保存処理→Model」の全体像を再確認し、将来の大規模リファクタリングに備える。**本書作成にあたりコードは一切変更していない。**

---

## A. 現在の画面構造

```
/                          → dashboardへリダイレクト
/app/dashboard/             → ダッシュボード（ToDo一覧、患者ごとの未実施タスク）
/app/patients/               → 患者一覧
/app/patients/add/           → 患者新規登録
/app/patient/<id>/           → ① 初診・基本情報
/app/patient/<id>/basic/edit/→ 基本情報編集
/app/patient/<id>/admission/ → ② 入院手続き
/app/patient/<id>/mapping/add/→ ③ MT測定
/app/patient/<id>/treatment/add/→ ④ 治療実施
/app/patient/<id>/assessment/<timing>/add/ → ⑤ 尺度評価（hub経由）
/app/patient/<id>/assessment/hub/<timing>/ → 尺度評価hub
/app/patient/<id>/assessment/<timing>/<scale_code>/ → 個別尺度フォーム
/app/patient/<id>/path/       → ⑥ クリニカルパス
/app/patient/<id>/summary/    → ⑦ 退院準備
/app/patient/<id>/questionnaire/ → 問診票編集
/app/patient/<id>/skips/       → スキップ履歴
/app/patient/<id>/audit_logs/  → 監査ログ
/app/calendar/month/           → 月間カレンダー
/app/print/...                 → 印刷（bundle/path/admission/discharge/referral/suitability/side_effect ×プレビュー/PDF）
/app/adverse-event/...          → 有害事象報告書（プレビュー/PDF）
/patient/...                    → 患者ポータル（自己記入式検査、別ツリー）
/admin/...                       → Django管理画面（カスタムAdminSite）
```

患者の主要導線は7画面（初診→入院手続き→MT測定→治療実施→尺度評価→クリニカルパス→退院準備）。全画面が`partials/patient_inline_bar.html`で患者情報ヘッダーとタブ導線を共有している（B/C節で詳細）。

---

## B. URL→View→Template対応表

| # | 画面 | URL名 | URLパターン | View関数 | 行番号 | Template |
|---|---|---|---|---|---|---|
| 1 | 初診 | `patient_first_visit` | `patient/<id>/` | `patient_first_visit` | [views.py:738-878](../rtms_app/views.py#L738-L878) | `patient_first_visit.html` |
| 2 | 入院手続き | `admission_procedure` | `patient/<id>/admission/` | `admission_procedure` | [views.py:662-677](../rtms_app/views.py#L662-L677) | `admission_procedure.html` |
| 3 | MT測定 | `mapping_add` | `patient/<id>/mapping/add/` | `mapping_add` | [views.py:678-737](../rtms_app/views.py#L678-L737) | `mapping_add.html` |
| 4 | 治療実施 | `treatment_add` | `patient/<id>/treatment/add/` | `treatment_add` | [views.py:878-1050](../rtms_app/views.py#L878-L1050) | `treatment_add.html` |
| 5 | 尺度評価 | `assessment_add`→`assessment_hub`, `assessment_scale` | `patient/<id>/assessment/<timing>/add/`, `.../<timing>/<scale_code>/` | `assessment_hub`, `assessment_scale_form` | [views.py:1843, 1971](../rtms_app/views.py#L1843) | `assessment/hub.html`, `assessment/scales/hamd.html`（または`hamd_modal.html`/`placeholder.html`） |
| 6 | クリニカルパス | `patient_clinical_path` | `patient/<id>/path/` | `patient_clinical_path` | [views.py:2611-2634](../rtms_app/views.py#L2611-L2634) | `patient_clinical_path.html` |
| 7 | 退院準備 | `patient_home` | `patient/<id>/summary/` | `patient_summary_view` | [views.py:2243-2410](../rtms_app/views.py#L2243-L2410) | `patient_summary.html` |

`print_urls`が`config/urls.py`（`/app/print/`）と`rtms_app/urls.py`（`patient/<id>/print/`）の両方からマウントされている冗長構造は既存資料通り変化なし。

---

## C. 患者画面共通構造

### C-1. 画面別フルチェーン（URL→View→Model→Template→partial→JS→保存）

**① 初診（`patient_first_visit`）**
- Model読取: `Patient`（全属性）, `Assessment`（baseline表示用）
- Model書込: `Patient`（診断名・紹介元・既往歴・日付・担当医・問診データ）
- partial: `partials/patient_inline_bar.html`
- JS: インライン3ブロック（iPad用textareaモーダル、既往歴トグル、Assessment/問診票モーダル読み込み・キーボード操作・送信）
- 保存処理: [views.py:789-807](../rtms_app/views.py#L789-L807)（フォームバリデーション→隠しフィールド補完→`Patient.save()`）
- 共通UI: `page-exit-menu`

**② 入院手続き（`admission_procedure`）**
- Model読取/書込: `Patient`（`admission_type`, `is_admission_procedure_done`）
- partial: `patient_inline_bar.html`
- JS: なし
- 保存処理: [views.py:671-675](../rtms_app/views.py#L671-L675)
- 共通UI: カード内フッターボタン（page-exit-menu/fab-stackどちらでもない独自形式）

**③ MT測定（`mapping_add`）**
- Model読取: `Patient`, `MappingSession`（履歴一覧）
- Model書込: `MappingSession`（date, week_number, resting_mt, helmet位置a/b, notes）
- partial: `patient_inline_bar.html`
- CSS: `page_actions.css`, `mapping.css`
- JS: インライン（治療開始日からの週番号自動計算）
- 保存処理: [views.py:710-729](../rtms_app/views.py#L710-L729)（"to_treatment"/"save_and_return"の2アクション分岐）
- 共通UI: `fab-stack`

**④ 治療実施（`treatment_add`）** — 最も複雑
- Model読取: `Patient`, `MappingSession`（当週）, `Assessment`（baseline/week3、寛解判定用）, `TreatmentSession`（履歴）
- Model書込: `TreatmentSession`（治療パラメータ全項目）, `SideEffectCheck`, `SeriousAdverseEvent`
- partial: `patient_inline_bar.html`（`title_controls_html=mode_switch_html`で記入/手順解説モード切替を注入）
- CSS: `page_actions.css`
- JS: `side_effect_widget_v2.js`, `wizard.js` + インライン12ブロック（F/H節で詳細）
- 保存処理: [views.py:954-1050](../rtms_app/views.py#L954-L1050)（セッション番号算出→寛解時の週間回数制約適用→`TreatmentSession`保存→`SideEffectCheck`保存→save/print/skip/cancel等の複数アクション分岐）
- 共通UI: `page-exit-menu`（印刷・保存・戻る・スキップ/中止の4ボタン）

**⑤ 尺度評価（`assessment_hub`/`assessment_scale_form`）**
- Model読取: `Patient`, `Assessment`（timing×type）, `AssessmentRecord`（timing×scale）, `ScaleDefinition`, `TimingScaleConfig`
- Model書込: `Assessment`と`AssessmentRecord`の**両方に同じHAM-Dスコアを保存**（後方互換のための二重保存、E節で詳細）
- partial: `patient_inline_bar.html`
- CSS/JS: `floating.css`/`floating.js`
- 保存処理: [views.py:1707-1730](../rtms_app/views.py#L1707-L1730)付近（q1-q17パース→合計スコア計算→レコード作成/更新）
- 共通UI: `fab-stack`

**⑥ クリニカルパス（`patient_clinical_path`）**
- Model読取のみ: `Patient`, `MappingSession`, `TreatmentSession`, `Assessment`
- partial: `patient_inline_bar.html`
- 保存処理: なし（GET専用ビュー、カレンダー生成のみ）
- 共通UI: `fab-stack`（印刷プレビュー・戻る）

**⑦ 退院準備（`patient_summary_view`）**
- Model読取: `Patient`, `TreatmentSession`（履歴表）, `Assessment`（推移表）, `PatientSurveySession`
- Model書込: `Patient`（summary_text, discharge_prescription, discharge_date）
- partial: `patient_inline_bar.html`
- 保存処理: [views.py:2253-2280](../rtms_app/views.py#L2253-L2280)（3種の印刷アクション分岐含む）
- 共通UI: `page-exit-menu`

### C-2. 共通ナビゲーション（B節・調査結果）

| 部品 | 使用状況 |
|---|---|
| `partials/patient_inline_bar.html` | **7画面全て**で使用（患者名・ID・タブ導線・見出しを提供する真の共通部品） |
| `partials/patient_nav.html` | 7画面の主要導線からは直接使われず、`patient_inline_bar.html`からのみ間接的に使用 |
| `page-exit-menu` | 初診・治療実施・退院準備の3画面（入院手続きは独自形式） |
| `fab-stack` | MT測定・尺度評価・クリニカルパスの3画面 |

→ 同じ「保存/戻る/印刷」という目的のUIが**page-exit-menu・fab-stack・入院手続き独自形式の3系統**に分かれている実態を再確認（削除・統合はしていない）。

---

## D. Dashboardから各画面への遷移

`dashboard_view`（[views.py:473-661](../rtms_app/views.py#L473-L661)）が対象日（`?date=`、省略時は当日）ごとに6種類のタスク群を生成し、`dashboard.html`（140-202行）へ渡す。

| タスク群 | 遷移先URL名 | 生成ロジック | 表示条件 |
|---|---|---|---|
| 初診 | `patient_first_visit` | views.py:540-541 | `created_at == 対象日` |
| 入院 | `admission_procedure` | views.py:543-545 | `admission_date == 対象日` かつ未完了 |
| MT測定 | `mapping_add` | views.py:546-549, 632-634 | `mapping_date == 対象日` または算出タスク |
| 治療実施 | `treatment_add` | views.py:550-560 | `generate_treatment_dates()`が生成した日付に一致 |
| 尺度評価 | `assessment_add`（timing付き） | views.py:570-595, 632-634 | 予定日≦対象日 かつ 未実施 |
| 退院準備 | `patient_home` | views.py:597-608 | `discharge_date == 対象日` または最終治療日 |

各リンクは`?dashboard_date=YYYY-MM-DD`をクエリパラメータとして各画面へ引き継ぎ、各画面の「戻る」ボタンがダッシュボードの同じ日付表示に戻れるようにしている。

`compute_dashboard_tasks`/`compute_task_definitions`（`services/schedule_tasks.py`）と`generate_treatment_dates`/`is_closed`（`services/rtms_schedule.py`）が日付計算の中枢（既存資料と一致、変化なし）。

---

## E. Assessmentデータフロー

### E-1. モデル構造（再確認、変化なし）
```
Assessment（旧、後方互換維持中）
  UniqueConstraint: (patient, course_number, timing, type)
  → total_score_17 / total_score_21

AssessmentRecord（新、研究用尺度含む正式版）
  UniqueConstraint: (patient, course_number, timing, scale)
  → improvement_rate_17 / status_label
  ↳ ScaleDefinition（code='hamd'等）
  ↳ TimingScaleConfig（timingごとの表示尺度設定）
```

### E-2. データフロー図（初診HAM-D→退院準備）

```mermaid
flowchart TD
    A["初診画面 patient_first_visit.html<br/>baseline未入力なら「入力する」ボタン"] -->|loadAssessmentModal('baseline')| B["assessment_scale_form (AJAX/modal)<br/>views.py 1971行〜"]
    B -->|同一レコードに保存| C[("Assessment + AssessmentRecord<br/>timing=baseline を保存")]
    C -->|同じレコードを参照| D["尺度評価hub (assessment_hub)<br/>baseline列に「入力済」表示"]
    D --> E["3週評価 (week3)<br/>assessment_scale_form"]
    E -->|compute_improvement_rate baseline_17 vs week3_17| F[("AssessmentRecord.improvement_rate_17<br/>status_label 更新")]
    F --> G["治療実施画面<br/>週3評価で寛解判定→週4-6の週間回数制約適用"]
    G --> H["4週評価 (week4)"]
    H --> I["6週評価 (week6)"]
    I --> J["退院準備画面 patient_summary_view<br/>course_summary_service.build_assessment_trend()"]
    J --> K["discharge/referral 印刷書類<br/>hamd_trend_cols として横断表示"]
```

**重要な事実（既存資料の再確認、実装に変化なし）**: 初診画面のHAM-D入力は「治療前評価へのコピー」ではなく、**同一の`baseline`レコード（Assessment + AssessmentRecord）を初診画面と尺度評価hub画面の双方から参照・編集している**。`assessment_scale_form`が保存の一元窓口。

### E-3. 「同じ機能が複数箇所に実装されている」候補（E節固有）
- **HAM-D重症度判定ロジックが3箇所に別実装で存在**（`assessment_rules.py`の`HAMD17_SEVERITY_BANDS`、`utils/hamd.py`は第1段階で削除済み、`course_summary_service.build_assessment_trend`内のインラインif/elif）。実際に退院準備・印刷画面で使われているのは`course_summary_service`のインライン実装のみ（既存資料と一致）。
- `Assessment`（旧）と`AssessmentRecord`（新）への**二重保存**は今回も変化なし。両モデルとも`assessment_scale_form`から毎回同時に書き込まれる。

---

## F. Treatmentデータフロー

| 項目 | デフォルト値の決定箇所 | 保存先 | 次回読み出し箇所 |
|---|---|---|---|
| 前回刺激強度 | `treatment_add` GET: 直近`TreatmentSession.intensity_percent`（無ければ旧`intensity`）を引き継ぎ | `TreatmentSession.intensity_percent` | 次回`treatment_add`のGETハンドラが同一ロジックで再度引き継ぎ |
| %MT | フォーム既定値100（前回引き継ぎなし） | `TreatmentSession.mt_percent` | 表示のみ（次回のデフォルトには使われない） |
| MT（モーター閾値） | 前回セッションが無い場合`MappingSession.resting_mt`（当週）を使用 | `MappingSession.resting_mt`（MT測定画面で別途保存） | `treatment_add`GET時に当週の`MappingSession`から都度取得 |
| 刺激位置 | `coil_type`/`target_site`はフォーム既定値固定。ヘルメット位置a/bは`TreatmentSession.meta`JSONの既存値があれば引き継ぎ、無ければ既定(3,1)/(9,1) | `TreatmentSession.meta`（JSON） | 次回GET時に同一患者・同一コースの直近`meta`から読み出し |
| 周波数/トレイン長/トレイン間隔/トレイン数/総パルス数 | `views.py`のGETハンドラでハードコード初期値（18Hz, 2秒, 20秒, 55回, 1980パルス）。`protocols.py`のプロトコル定義は現状未接続 | `TreatmentSession`の各フィールド | 毎回同じハードコード値が初期表示される（前回値の引き継ぎなし） |
| 副作用 | UIは`side_effect_widget_v2.js`、項目定義は`services/side_effect_schema.py` | `SideEffectCheck.rows`（JSON, `get_or_create(session=s)`） | 印刷（治療記録票）時に同セッションから読み出し |
| 有害事象（SAE） | `treatment_add`POSTの`sae_*`チェックボックス | `SeriousAdverseEvent`（`auto_snapshot`に当日パラメータを記録） | `AdverseEventReport`作成時・印刷時に参照 |

**手順解説モード（wizard.js連携）** はこのデータ入力の**代替入力経路**であり、Step5-7で同じフィールド（MT値・刺激位置・%MT等）を対話形式で収集し、`complete()`実行時に本フォームへ同期する設計（H節で詳細）。

---

## G. PDF/印刷データフロー

```
画面（プレビューURL） → print_views.py（プレビューView） → Template → （PDF URL） → print_views.py（_pdf View）→ render_pdf_response() → WeasyPrint
```

- 各書類（bundle/path/admission/discharge/referral/suitability/side_effect）とも「プレビュー」と「`_pdf`」の2エンドポイント構成で、**コンテキスト構築コードがビュー内でほぼ全文重複**（discharge/referral/admission/side_effectの4ペア、既存資料と一致・変化なし）。
- 共通処理: `render_pdf_response(request, template, context, filename)`がHTML→PDF変換を一元化。`services/print_service.py`の`build_pdf_filename()`/`CONTENT_LABELS`のみ実利用（他の未使用関数は第1段階調査時から変化なし）。
- HAM-D推移共有: `_hamd_cols_for_patient()`（print_views.py）→`course_summary_service.build_assessment_trend()`→bundle/discharge/referralへ`hamd_trend_cols`として供給。
- `adverse_event_report.html`（フォーム入力プレビュー）と`adverse_event_report_db.html`（DB保存済み出力）は`views.py`の`adverse_event_report_print_preview`/`_print`が担当し、`print_views.py`とは別経路（既存資料と一致）。
- **今回は`_print_base.html`/`_print_base_twocolumn.html`の統合は調査対象外**（ユーザー指示通り、内容の再確認のみ）。

---

## H. JavaScript依存関係

### H-1. wizard.js（`ProcedureWizard`クラス、約920行）
- `window.currentWizard = new ProcedureWizard()`としてグローバル公開。
- Step1〜9の状態遷移（`this.state`オブジェクトで各ステップの入力値を保持）。
- Step1/2/4/9は説明のみ、Step3は安全確認3項目、Step5はMT再測定（条件表示）、Step6は確認用刺激、Step7は治療刺激、Step8はSAE/副作用確認。
- **treatment_add.htmlから供給されるグローバル変数/関数に依存**: `window.wizardConfirmAlertMessage`, `window.isFirstSession`, `window.needsMappingToday`, `window.mappingUrl`, `window.mtValueDisplay`, `window.getTodayTrainSeconds()`, `window.getTodayMtPercent()`, `window.getCSRFToken()`。
- **treatment_add.htmlから呼び出されるwizard.jsのグローバルメソッド**: `window.currentWizard.openSideEffectModal()`, `window.currentWizard.openAdverseEventModal()`（Step8ボタンのonclick属性経由）。

### H-2. 手順解説モードの実装
[treatment_add.html:1099-1116](../rtms_app/templates/rtms_app/treatment_add.html#L1099-L1116)：`#treatModeRecord`/`#treatModeWizard`ラジオボタンの`change`イベントで`#procedureWizardModal`をBootstrap Modal APIで表示し、モーダルが閉じられたら（`hidden.bs.modal`）自動的に「記入モード」へ戻す設計。

z-index設定は[treatment_add.html:20-21](../rtms_app/templates/rtms_app/treatment_add.html#L20-L21)で`.page-treatment .modal{z-index:2060}`/`.modal-backdrop{z-index:2050}`と定義されているが、これは`.page-treatment`スコープ全体への指定であり、個別モーダルID（`#procedureWizardModal`等）への直接指定ではない。第3段階調査時点の結論（「治療実施記録票印刷ボタンの下に潜り込む」既知バグの回避策として意図的に高い値になっている可能性が高い）は今回も維持し、**統一しない**。

### H-3. 数値入力ステッパー
`treatment_add.html`・`wizard.js`ともに**カスタムの+/-ボタン実装は存在せず**、標準HTML`<input type="number" step="..." min="..." max="...">`のみを使用（%MT: step10/min80/max140、トレイン各種はstep属性のみ）。iPad等では OS標準のスピナーに依存。

### H-4. モーダル一覧（治療実施画面、5個）

| モーダルID | 用途 | 開くトリガー | 閉じるトリガー |
|---|---|---|---|
| `#procedureWizardModal` | 手順解説ウィザード | 手順解説ラジオボタン、wizard.js内`openSideEffectModal`/`openAdverseEventModal`（誤: 実際はこのモーダル自体を開く動作ではなく、下記2モーダルを開く） | 閉じるボタン、完了ボタン（後述の不具合あり） |
| `#sideEffectModal` | 副作用チェック票入力 | Step8内ボタン、画面本体の「治療実施記録票（副作用チェック票）入力」ボタン | 閉じる/保存して閉じるボタン |
| `#saeModal` | 有害事象報告書入力 | Step8内ボタン、画面本体の「有害事象入力・報告書」ボタン | 閉じるボタン、SAE保存ボタン |
| `#printMenuModal` | 印刷メニュー | （明示的トリガー箇所は未特定） | 閉じる/各印刷ボタン |
| `#skipModal` | スキップ・中止選択 | 「治療スキップ・中止」ボタン | 閉じる/スキップ確定・中止確定ボタン |

### H-5. treatment_add.htmlインラインスクリプト（12ブロック、行番号は概算）
副作用チェック表印刷ハンドラ／スキップ・中止モーダル／手順解説モード切替／SAE通知表示制御／SAEモーダルバッジ表示／SAE保存ハンドラ／副作用モーダル保存・印刷／副作用チェック表印刷／有害事象報告書印刷／SAE印刷プレビュー・プリフィル／フォーム要素初期化／グローバル変数供給。各ブロックの詳細は本調査で個別に確認済み（重複や不具合は下記J節参照）。

### H-6. 🔴 静的解析で確認した潜在的な不具合（重要・今回は修正しない）

`wizard.js`の`complete()`メソッド（[wizard.js:174](../rtms_app/static/rtms_app/wizard.js#L174)）が`this.syncToTreatmentForm()`を呼び出しているが、**このメソッドはファイル内はもちろんプロジェクト全体のどこにも定義されていない**（`grep -r "syncToTreatmentForm"`で該当行がこの呼び出し1件のみとヒット、定義箇所ゼロを確認済み）。

- `#wizardCompleteBtn`は`type="button"`（[treatment_add.html:421-425](../rtms_app/templates/rtms_app/treatment_add.html#L421-L425)）でネイティブsubmitのフォールバックがないため、このメソッド呼び出しが例外を投げた場合、`complete()`内でそれ以降の処理（`actionInput.value='save_from_wizard'`の設定、安全確認チェックボックスへの`change`イベント発火）が**実行されないまま停止する**可能性が高い。
- **本調査では実際にJSコンソールエラーが発生するかまでは実機（初回セッション条件でのモード切替UI表示）で確認できていない**（手順解説モード切替UIが本調査で開いたテスト患者の治療画面には表示されなかったため、条件付き表示である可能性がある）。**事実として確認できたのは「該当メソッドの呼び出しと定義不在」のみ**であり、この先の実際の挙動（本当に例外が発生するか、catchされているか等）は断定していない。
- **今回はコード変更禁止のため修正していない。** 第5段階以降で実際に手順解説モードの「完了」ボタンをブラウザで動作確認し、コンソールエラーの有無を確認することを強く推奨する。

---

## I. CSS依存関係（第3段階完了後の現状）

第1〜3段階の変更を反映した最新状態:
- `.q-badge`（28×28px灰色）は`static/css/rtms_theme.css`に一本化済み。`hamd_modal.html`の赤30pxバリアント、`patient.css`の36pxバリアントは意図的に別のまま。
- `.page-document-actions`（未使用）、`app.css`の`.app-page-title`/`.app-section`/`.floating-action-menu`（未使用）は削除済み。
- `admin_custom.css`は`JAZZMIN_SETTINGS["custom_css"]`から実際に読み込まれており使用中（削除しなかった）。
- `.hamd-row`/`.sticky-scorebar`/`.assessment-cell`は**第3段階時点でも統合未実施**（ページグループごとに微妙な差異があり保留中、詳細は[static_template_refactoring_report.md](static_template_refactoring_report.md)のC/I節）。
- `page-exit-menu`（3画面）と`fab-stack`（3画面）の並存は今回も変化なし（C-2節参照）。

---

## J. 重複処理候補（今回発見・既存分含む、削除も統合もしていない）

| # | 重複内容 | 箇所 | 状態 |
|---|---|---|---|
| 1 | HAM-D重症度判定ロジック | `assessment_rules.py` / `course_summary_service.build_assessment_trend`インライン（`utils/hamd.py`は第1段階で削除済み） | 実働は`course_summary_service`版のみ。既存資料通り。 |
| 2 | `Assessment`（旧）と`AssessmentRecord`（新）の二重保存 | `assessment_scale_form` | 既存資料通り、変化なし。 |
| 3 | 印刷View（プレビュー/`_pdf`）のコンテキスト構築コード | `print_views.py`のdischarge/referral/admission/side_effect各ペア | 既存資料通り、変化なし。 |
| 4 | HAM-Dキーボード操作（0-4キー） | `patient_first_visit.html`, `assessment/scales/hamd_modal.html` | 既存資料通り。 |
| 5 | モーダルフォーム送信fetch処理 | `patient_first_visit.html`内2箇所 | 既存資料通り。 |
| 6 | 「保存/戻る/印刷」ナビゲーションの3系統併存 | `page-exit-menu` / `fab-stack` / 入院手続き独自形式 | 本調査で入院手続き画面が実は**どちらでもない第3の独自形式**であることを新規確認。 |
| 7 | 手順解説モードとフォーム直接入力の二重入力経路 | `wizard.js`のStep5-7 vs 画面本体の同項目フォーム | 同じデータ（MT値・刺激位置・%MT等）を2つの異なるUIフローで収集する設計。本調査で新規に明示。 |
| 8 | `complete()`内の未定義メソッド呼び出し | `wizard.js:174` `syncToTreatmentForm()` | **本調査で新規発見**。H-6節参照。 |

---

## K. 将来統合できそうな処理（候補のみ、今回は未実施）

- 印刷View（discharge/referral/admission/side_effect）の`_build_xxx_context()`ヘルパーへの抽出（既存資料通り、中リスク）。
- HAM-D重症度判定ロジックの一本化（`assessment_rules.py`を正とする、要臨床確認の高リスク作業）。
- 「保存/戻る/印刷」ナビゲーション3系統の`page-exit-menu`への統一（入院手続き画面のみ独自形式である点を含めて第5段階以降で検討）。
- `views.py`（2684行）のドメイン別分割（既存資料通り）。
- `wizard.js`の`syncToTreatmentForm()`不具合の実機検証と修正（**ただし本段階の対象外、第5段階以降でJS変更許可が出た場合にのみ**）。

---

## L. 絶対に触らない方がよい箇所

- **`wizard.js`全体と`treatment_add.html`のインラインJS**: `window.*`グローバル変数・関数による密結合が今回の調査でさらに具体的に確認された（H-1/H-6節）。`syncToTreatmentForm()`不具合を発見したが、影響範囲の完全な把握（本当に実行時エラーになるか、手順解説モードがどの条件で表示されるか）ができていないため、**さらに危険**。触るなら実機での事前確認と回帰テスト計画が必須。
- **`Assessment`/`AssessmentRecord`の二重保存構造**: `assessment_scale_form`の保存先を変更すると、初診画面↔尺度評価hub画面の同一レコード参照関係が壊れる。
- **`treatment_add`の初期値決定ロジック**（前回刺激強度引き継ぎ、MappingSessionからのMTデフォルト）: 治療安全性に直結。
- **`_print_base.html`/`_print_base_twocolumn.html`**: 今回も調査対象外として維持（ユーザー指示通り）。
- **`partials/patient_inline_bar.html`/`patient_nav.html`**: 7画面全てが依存する共通部品。中身変更は全画面へ波及。
- **HAM-D重症度・改善率判定のしきい値**（`assessment_rules.py`）: 臨床判定基準そのもの。
- **`SeriousAdverseEvent`/`AdverseEventReport`の保存経路**: 安全管理上の記録漏れは致命的。
- **モーダルz-index（2050 vs 2060）**: 第3段階の結論を維持、統一しない。

---

## M. 将来のリファクタリング優先順位（第5段階以降の提案）

1. **低リスク・実施推奨**: 印刷View（discharge/referral/admission/side_effect）のコンテキスト構築コードを`_build_xxx_context()`ヘルパーへ抽出（プレビュー/`_pdf`間の重複解消、既存の出力結果と完全一致することを目視確認しながら実施）。
2. **低〜中リスク**: 「保存/戻る/印刷」ナビゲーションの3系統（page-exit-menu/fab-stack/入院手続き独自）を`partials/page_exit_menu.html`への共通化計画を具体化（HTMLレベルの変更を伴うため要画面ごとの目視確認）。
3. **中リスク・要実機検証が先**: `wizard.js`の`syncToTreatmentForm()`不具合の実機再現確認（ブラウザコンソール確認のみ、コード変更はまだしない）。再現確認後、影響範囲を洗い出してから修正方針を決める。
4. **高リスク・要臨床確認**: HAM-D重症度判定ロジックの一本化（`assessment_rules.py`への統一）。
5. **長期保留**: `Assessment`/`AssessmentRecord`の二重保存一本化（データ移行設計が必要、別プロジェクト化を推奨）。

---

## N. 推奨する最終ディレクトリ構造（提案のみ、今回は未実施）

[docs/refactoring_plan.md](refactoring_plan.md)の7章・[docs/static_template_refactoring_report.md](static_template_refactoring_report.md)のL節と同じ提案を維持し、本調査で新たに確認した点を追記する:

```
rtms_app/
├── views/                        # views.py分割（dashboard/patient_profile/treatment/assessment/calendar/audit）
│   └── print/                    # print_views.py分割 + _build_xxx_context()共通ヘルパー
├── services/
│   └── hamd_classification.py    # 3箇所重複の重症度判定ロジック一本化先（将来）
├── templates/rtms_app/
│   └── partials/
│       └── page_exit_menu.html   # page-exit-menu/fab-stack/入院手続き独自形式の統一先（第5段階以降で検討）
└── static/rtms_app/
    ├── css/  (第2段階提案通り)
    └── js/
        └── wizard/                # wizard.js関連（syncToTreatmentForm等の不具合修正後に分割を検討、今回は現状維持）
```

---

## まとめ

本書作成にあたり、コードは一切変更していない。7画面それぞれのURL→View→Template→partial→JS→保存→Modelのフルチェーンを直接コードから再確認し、Assessment/Treatmentのデータフローを図示した。特に`wizard.js`の`complete()`が未定義メソッド`syncToTreatmentForm()`を呼び出している点を静的解析で新規発見したが、実機での影響確認・修正は第5段階以降の判断に委ねる。
