# 現行システム構造報告書（rTMS支援システム）

作成日: 2026-08-20
調査範囲: リポジトリ全体（読み取り専用調査。コード変更なし）
対象アプリ: `rtms_app`（Django 5.0.14 / Python 3.12 / SQLite）

> 本報告書は「現状把握」のみを目的とする。記載した削除候補・重複候補は次フェーズでの検討材料であり、本タスクでは一切のファイル変更を行っていない。

---

## A. ディレクトリ構造

```
rTMS/
├── manage.py
├── requirements.txt
├── db.sqlite3
├── config/                      # Djangoプロジェクト設定
│   ├── settings/{base,dev,prod}.py
│   ├── urls.py                  # ルートURL（/, /patient/, /app/, /app/print/, /admin/）
│   ├── asgi.py / wsgi.py
├── rtms_app/                    # メインアプリ（ほぼ全機能がここに集約）
│   ├── models.py                # 全モデル定義（662行）
│   ├── views.py                 # 主要view（2684行、56関数）★最大の集中ファイル
│   ├── views.py.backup          # ★未追跡(git status ??)の古いバックアップ（3471行、内容差分あり）
│   ├── views_patient.py         # 患者ポータル（自己記入式アンケート）専用view
│   ├── views_health.py          # healthz/version
│   ├── views_survey_export.py   # 自己記入式検査CSV出力
│   ├── print_views.py           # 印刷/PDF専用view（597行）
│   ├── forms.py                 # Djangoフォーム
│   ├── admin.py                 # カスタムAdminSite・ModelAdmin
│   ├── urls.py / patient_urls.py / print_urls.py
│   ├── assessment_rules.py      # 改善率・寛解/反応判定ロジック
│   ├── protocols.py              # 治療プロトコル定義（保険診療プロトコル等）
│   ├── signals.py / middleware.py
│   ├── surveys/definitions.py   # 自己記入式尺度定義（BDI-II, SDS, SASS-J, PHQ-9, STAI, DAI-10）
│   ├── services/                # サービス層（一部のみ実際に利用）
│   ├── queries/patient_queries.py
│   ├── templatetags/{hamd.py, dict_extras.py, rtms_extras.py, request_context.py}
│   ├── management/commands/create_patient_users.py
│   ├── migrations/ (0001〜0039)
│   ├── templates/rtms_app/ (65 html)
│   └── static/rtms_app/ (6 js, 8 css)
├── static/ , staticfiles/        # プロジェクト直下の静的ファイル（staticfiles/はcollectstatic成果物）
├── docs/                         # 既存ドキュメント（print_inventory.md 等）
└── _attic/venv_old/              # 旧仮想環境（明らかに不要）
```

---

## B. Djangoアプリ構造

- 単一アプリ構成（`rtms_app`のみ、`INSTALLED_APPS`的にはこれ1本）。
- 機能ごとにモジュール分割はされているが、**view層はほぼ全て `views.py` に集中**（2684行、56関数）。印刷系のみ `print_views.py` に分離済み。患者ポータルのみ `views_patient.py` に分離済み。
- `services/` はあるが、実際に呼び出されているのは一部のみ：
  - 使用中: `course_summary_service.py`, `rtms_schedule.py`, `schedule_tasks.py`, `schedule.py`, `recommendation.py`, `sae_report.py`, `side_effect_schema.py`, `patient_accounts.py`
  - **部分的に未使用（scaffold止まり）**: `print_service.py`（`build_pdf_filename`と`CONTENT_LABELS`のみ実利用、`validate_print_docs`/`get_patient_for_print`/`build_print_context`/`get_clinical_path_context`は未使用）
  - **ほぼ空/未使用**: `mapping_service.py`（`get_latest_mt_percent()`が定義されているが呼び出し元なし）、`calender.py`（0行の空ファイル）

---

## C. Model構造（Patientを中心とした関係）

```
Patient (患者マスタ)
 ├─ user: OneToOne → User（患者ポータルログイン用）
 ├─ attending_physician: FK → User
 ├─ MappingSession[] (course_number, date, week_number, resting_mt, helmet位置)
 ├─ TreatmentSession[] (course_number, session_date, slot, mt_percent, intensity_percent,
 │    frequency_hz, train_seconds, intertrain_seconds, train_count, total_pulses, side_effects(JSON), meta(JSON), status)
 │    ├─ TreatmentSkip[] (順延/終了/中止、snapshotによるundo対応)
 │    ├─ SideEffectCheck (OneToOne, rows(JSON), memo, physician_signature)
 │    ├─ SeriousAdverseEvent[] (course_number, event_types(JSON), auto_snapshot(JSON))
 │    └─ AdverseEventReport (OneToOne, 正式な有害事象報告書。診断/転帰/併用薬等の詳細)
 ├─ Assessment[] (legacy: timing×type一意, HAM-D score, total_score_17/21)
 ├─ AssessmentRecord[] (new: timing×scale(FK ScaleDefinition)一意, improvement_rate_17, status_label)
 │    └─ ScaleDefinition (code='hamd'等) ← TimingScaleConfig (どのtimingでどの尺度を表示するか)
 ├─ PatientSurveySession[] (course_number, phase=pre/post, status)
 │    └─ PatientSurveyResponse[] (instrument=bdi2/sds/sassj/phq9/stai_x1/stai_x2/dai10, answers(JSON), total_score)
 └─ 汎用: ConsentDocument（患者非依存、最新1件運用）, AuditLog（patient任意FK、操作履歴）
```

主要な自然キー制約（`UniqueConstraint`）:
- `MappingSession`: (patient, course_number, date, stimulation_site)
- `TreatmentSession`: (patient, course_number, session_date, slot)
- `Assessment`: (patient, course_number, timing, type)
- `AssessmentRecord`: (patient, course_number, timing, scale)
- `SeriousAdverseEvent`: (patient, course_number, session)
- `PatientSurveyResponse`: (session, instrument)

**注目点**: `Assessment`（旧）と`AssessmentRecord`（新）が並存し、両方に同じHAM-Dデータを二重保存している（後述H節）。

---

## D. URL → View → Template構造

### D-1. メインルーティング（`config/urls.py`）
```
/                → dashboardへリダイレクト
/patient/...     → rtms_app.patient_urls (患者ポータル, namespace=patient_portal)
/app/...         → rtms_app.urls (namespace=rtms_app) ★職員側メイン
/app/print/...   → rtms_app.print_urls (namespace=print、config側からも直接マウント)
/admin/...       → rtms_admin_site (カスタムAdminSite)
```
※ `print_urls` は `rtms_app/urls.py` 内 `patient/<id>/print/` にも include されており、`config/urls.py` の `/app/print/` とあわせて**同一URL構造が二重にマウント**されている（実害はないが冗長）。

### D-2. 職員向け主要ページ（URL→View→Template）

| URL | View | Template |
|---|---|---|
| `/app/dashboard/` | `dashboard_view` | `dashboard.html` |
| `/app/patients/` | `patient_list_view` | `patient_list.html` |
| `/app/patients/add/` | `patient_add_view` | `patient_add.html` |
| `/app/patient/<id>/` | `patient_first_visit` | `patient_first_visit.html`（初診） |
| `/app/patient/<id>/basic/edit/` | `patient_basic_edit` | `patient_basic_edit.html` |
| `/app/patient/<id>/summary/` | `patient_summary_view` | `patient_summary.html`（退院準備、URL名は`patient_home`） |
| `/app/patient/<id>/admission/` | `admission_procedure` | `admission_procedure.html` |
| `/app/patient/<id>/mapping/add/` | `mapping_add` | `mapping_add.html`（MT測定） |
| `/app/patient/<id>/treatment/add/` | `treatment_add` | `treatment_add.html`（治療実施、1500行超のview区間） |
| `/app/patient/<id>/assessment/<timing>/add/` | `assessment_add`（実体は`assessment_hub`へ転送） | — |
| `/app/patient/<id>/assessment/hub/<timing>/` | `assessment_hub` | `assessment/hub.html` or `hub_modal.html` |
| `/app/patient/<id>/assessment/<timing>/<scale_code>/` | `assessment_scale_form` | `assessment/scales/hamd.html` or `hamd_modal.html` |
| `/app/patient/<id>/path/` | `patient_clinical_path` | `patient_clinical_path.html` |
| `/app/calendar/month/` | `calendar_month_view` | `calendar_month.html` |
| `/app/patient/<id>/questionnaire/` | `questionnaire_edit` | `questionnaire_edit.html` |
| `/app/patient/<id>/skips/` | `treatment_skip_list` | `skip_list.html` |
| `/app/patient/<id>/audit_logs/` | `audit_logs_view` | `audit_logs.html` |
| `/app/consent/latest/`, `/consent/latest/` | `latest_consent` / `consent_latest` | `consent_latest.html` |
| `/app/adverse-event/...` | `adverse_event_report_print_preview` / `_print` | `print/adverse_event_report.html` / `adverse_event_report_db.html` |

### D-3. 印刷（`print_urls.py` → `print_views.py`）
各書類とも「プレビュー（HTML）」と「`_pdf`（PDF応答）」の2エンドポイント構成：
`bundle`, `path`, `admission`, `discharge`, `referral`, `suitability`, `side_effect/<session_id>` の7系統×2 + `api/get-session/`（JSON API）。

### D-4. 患者ポータル（`patient_urls.py` → `views_patient.py`）
`login / logout / portal / surveys/start / surveys/<id>/review / submit / <instrument>` — 自己記入式（BDI-II等）の実施フロー。

---

## E. JavaScript構造

### E-1. 独立JSファイル（`rtms_app/static/rtms_app/`）

| ファイル | 役割 | 主な読み込み元 |
|---|---|---|
| `floating.js`（24行） | `RTMSFloating.{clickById, submitFormById, openPrintForm}` | `assessment_add.html`, `assessment/_form.html`, `assessment/hub.html`, `assessment/scale_form_base.html`, `patient_add.html`, `questionnaire_edit.html` |
| `hamd_widget.js`（約175行） | `calcHAMD17()`, `getSeverity()`, `initButtonGroups()`, `initPopovers()` | `assessment/baseline.html`, `week3/4/6.html`, `assessment/scales/hamd.html` |
| `patient_surveys.js`（約130行） | 患者ポータルのオートセーブ・回答送信 | `patient/instrument.html` |
| `side_effect_widget_v2.js`（約250行超） | 副作用チェック表グリッドUI | `treatment_add.html` |
| `wizard.js`（約850行） | 治療実施の多段階ウィザード | `treatment_add.html` |
| `calendar_focus.js` | カレンダーの日付フォーカス | `calendar_month.html` |

### E-2. インラインJS（テンプレート埋め込み、要注意箇所）

| テンプレート | 規模目安 | 内容 |
|---|---|---|
| `patient_first_visit.html` | 3ブロック・計約450行 | 紹介元/医師連携、モーダル読み込み(`loadAssessmentModal`)、HAM-D/問診票キーボード操作、モーダル送信 |
| `treatment_add.html` | 3ブロック・計約250行 | 印刷ハンドラ、スキップ/中止モーダル、SAEモーダル、モード切替 |
| `patient_summary.html` | 2ブロック・計約80行 | オートセーブ（`debounceSave`, `showToast`, `performSave`） |
| `assessment/scales/hamd_modal.html` | 約180行 | `initHamdModal()`（スコア計算・キーボード操作） |
| `assessment/scales/hamd_modal_new.html` | 約160行 | 同上とほぼ同一ロジック（**未使用テンプレートだが中身は重複コード**） |
| `mapping_add.html` | 約60行 | 治療開始日からの週数自動計算 |
| `patient_clinical_path.html` | 約15行 | フォーカス位置へのスクロール |

### E-3. 重複ロジックの所在
- **HAM-Dスコア計算/重症度判定**が `hamd_widget.js` と `hamd_modal.html`（インライン）に**別実装で重複**（`calcHAMD17`/`getSeverity` 相当のロジックがそれぞれに存在）。
- **HAM-Dキーボード操作（0-4キー）**が `patient_first_visit.html`（`getHamdGroups`/`setHamdGroupFocus`）と `hamd_modal.html` の双方に個別実装。
- **モーダルフォーム送信**が `patient_first_visit.html` と `hamd_modal.html` にそれぞれ個別のfetch実装で存在。
- `RTMSFloating`（`floating.js`）は**まだ生存**：assessment系の各画面（hub/individual form/questionnaire_edit）で使用中。**ただし今回のナビゲーション整理で `patient_first_visit.html`/`patient_clinical_path.html`/`treatment_add.html`/`patient_summary.html` からは既に呼び出しを除去済み**（別タスクで対応）。

---

## F. CSS構造

| ファイル | 行数目安 | 役割 | 主な読み込み元 |
|---|---|---|---|
| `app.css` | 117 | 全体テーマ変数・カード共通スタイル | `base.html` |
| `box_style.css` | 71 | `card-header-accent`、`.fab-stack .fab`の影 | `base.html`想定 |
| `floating.css` | 23 | `.fab-stack`固定配置（`position:fixed`） | assessment系4テンプレート、`patient_add.html` |
| `page_actions.css` | 19（今回追加） | `.page-exit-menu` / `.page-document-actions`（通常配置の出口メニュー） | `treatment_add.html`, `patient_clinical_path.html`, `patient_first_visit.html`, `patient_summary.html` |
| `mapping.css` | 22 | MT測定画面の文字サイズ調整 | `mapping_add.html` |
| `patient.css` | 82 | 患者ポータル用 | `patient/base_patient.html` |
| `print.css` | 340行超 | 印刷共通（A4レイアウト等） | `print/_print_base.html` |
| `print_toolbar.css` | 5 | 印刷ツールバー固定配置 | `print/_print_base.html` |

**重複/分散の所見**:
- `--card-accent` インライン style が `patient_first_visit.html`, `patient_summary.html`, `mapping_add.html`, `admission_procedure.html` など**12箇所以上で同一パターンを個別記述**。
- `.fab-stack`/`.fab` の定義が `floating.css` と `box_style.css` に分散し、さらに複数テンプレートでインライン `<style>` 上書き（`mapping_add.html`, `assessment/scale_form_base.html`）。
- モーダルの `z-index` 戦略がテンプレートごとにバラバラ（`hamd_modal.html`, `questionnaire_edit.html`, `treatment_add.html` でそれぞれ独自の値を上書き）。
- HAM-Dボタンの `.hamd-btn.active` 色指定が `patient_first_visit.html` と `hamd_modal_new.html` に重複。
- ほぼ全ページ（38テンプレート程度）が独自の `<style>` ブロックを保持しており、共通化されていないページ固有スタイルが多い。

---

## G. PDF/印刷構造

- **PDFライブラリ**: WeasyPrint（`print_views.py` 冒頭で`HAVE_WEASY`フラグにより有無を判定、未インストール時はHTMLへフォールバック）。
- **共通関数**: `render_pdf_response(request, template, context, filename)` がHTML→PDF変換を一元化。
- **命名規則ヘルパー**: `services/print_service.py` の `build_pdf_filename()` と `CONTENT_LABELS`（実際に使われているのはこの2つのみ）。
- **各書類は「プレビュー版」と「_pdf版」がペアで存在**し、`print_views.py`内でコンテキスト構築コードがほぼ丸ごと重複している（discharge/referral/admission/side_effect の4ペアすべてで該当）。
- **HAM-D推移の共有ロジック**: `_hamd_cols_for_patient()`（`print_views.py`）が`course_summary_service.build_assessment_trend()`を呼び出し、bundle/discharge/referralの各印刷画面へ`hamd_trend_cols`として渡している。
- **印刷テンプレート階層**:
  - `_print_base.html`（+`_print_toolbar.html`）→ `_print_base_twocolumn.html` → 各書類（`admission_summary.html`, `discharge_summary.html`, `referral.html`, `suitability_questionnaire.html`）
  - `discharge_summary.html` / `referral.html` は `_hamd_trend_print_compact.html` を include。
  - `bundle.html` は複数書類テンプレートを動的にループ include する束ねページ。
  - `adverse_event_report.html`（フォーム入力からのプレビュー）と `adverse_event_report_db.html`（DB保存済みレコードからの出力）は別経路（`views.py`の`adverse_event_report_print_preview`/`_print`が担当し、`print_views.py`ではない点に注意）。
  - `hamd_detail.html` と `_hamd17_trend_table.html` は他テンプレートから参照されておらず、実質使われていない可能性が高い（後述K節）。

---

## H. HAM-D等Assessmentデータフロー

**重要な事実確認**：ユーザーが想定している「初診時にHAM-D17を入力し、それが治療前評価画面に表示される」という仕様は、**「初診画面からHAM-Dデータをコピーする」処理ではない**。実際の実装は以下の通り：

1. `patient_first_visit.html`（初診画面）には、HAM-D（治療前=baseline）の**現在の入力状況を表示するカード**があり（`baseline_assessment.total_score_17/21`を表示）、未入力なら「入力する」ボタンから**同一のbaseline用HAM-D入力モーダル**をその場で開ける（`loadAssessmentModal('baseline')` → `assessment_scale_form` を`?modal=1`でAJAX取得）。
2. モーダルで保存すると `assessment_scale_form`（`views.py` 1971行〜）が **`AssessmentRecord`（新）と`Assessment`（旧）の両方に同じスコアを保存**する（互換性維持のための二重保存）。
3. 治療前評価画面（アセスメントHub `assessment_hub`、および個別フォーム）は**その同じ`AssessmentRecord`/`Assessment`のbaselineレコード**を参照するため、初診画面で入力した内容がそのまま「治療前評価」として表示される（別データへのコピーではなく、**同一レコードを両画面から参照/編集している**）。
4. week3/week4/week6の入力時、`assessment_scale_form`は`assessment_rules.compute_improvement_rate(baseline_17, current_17)`でbaselineとの改善率を自動計算し、`classify_response_status()`で寛解/反応/反応なしを判定、`AssessmentRecord.improvement_rate_17`/`status_label`に保存する。
5. 退院準備画面・印刷書類（discharge/referral）は `course_summary_service.build_assessment_trend()` で baseline/week3/week4/week6 を横断集計し、`hamd17`, `hamd21`, `improvement_pct_17`, `severity_label`, `status_label` を返す。

**関連ファイル**:
- [rtms_app/models.py](rtms_app/models.py) — `Assessment`, `AssessmentRecord`, `ScaleDefinition`, `TimingScaleConfig`
- [rtms_app/views.py](rtms_app/views.py) — `patient_first_visit`（baseline表示）, `assessment_hub`, `assessment_scale_form`（保存・改善率計算の中心）
- [rtms_app/assessment_rules.py](rtms_app/assessment_rules.py) — `compute_improvement_rate`, `classify_response_status`
- [rtms_app/services/course_summary_service.py](rtms_app/services/course_summary_service.py) — `build_assessment_trend`
- [rtms_app/templates/rtms_app/assessment/scales/hamd_modal.html](rtms_app/templates/rtms_app/assessment/scales/hamd_modal.html) — 実際に使われているHAM-D入力モーダル（`hamd_modal_new.html`は未使用）

---

## I. 治療記録データフロー

`treatment_add`（`views.py`）が中心。GET時の初期値決定ロジックが重要：

1. **前回刺激強度の引き継ぎ**: 同一患者・同一クールで対象日より前の直近`TreatmentSession`を`intensity_percent`優先（無ければ旧`intensity`）で取得し、`initial_data['intensity_percent']`と`mt_percent`にセット。
2. **初回MT値からのデフォルト**: 前回セッションが無い場合、`MappingSession`（当日または当該週）の`resting_mt`を`intensity_percent`初期値として使用し、`mt_percent`は100固定。
3. **%MT / 刺激強度**: `TreatmentSession.mt_percent`と`intensity_percent`は自動計算式ではなく、フォームのデフォルト値（`mt_percent=100`, `intensity_percent=60`）または上記引き継ぎ値がそのまま入力初期値になる。
4. **刺激位置**: `coil_type`（既定"BrainsWay H1"）、`target_site`（既定"左DLPFC"）はフォーム既定値固定。ヘルメット位置座標（a/b各x,y）は`TreatmentSession.meta`JSONに保存され、既存値があれば引き継ぎ、無ければ既定(3,1)/(9,1)。
5. **周波数・トレイン長・トレイン間隔・トレイン数・総パルス数**: `views.py`のGETハンドラで`frequency_hz=18, train_seconds=2, intertrain_seconds=20, train_count=55, total_pulses=1980`をハードコード初期値としてセット（`protocols.py`のプロトコル定義は現状ランタイムのデフォルト値決定には未接続）。
6. **副作用**: POSTで`side_effect_rows_json`をパースし`SideEffectCheck.objects.get_or_create(session=s)`で保存。項目定義は`services/side_effect_schema.py`の`SIDE_EFFECT_ITEMS`（UIは`side_effect_widget_v2.js`が描画）。`admin.py`内の`SIDE_EFFECT_SCHEMA`（django_jsonform用）は現在フォーム描画には使われていない参考定義。
7. **重篤有害事象（SAE）**: `treatment_add`のPOSTハンドラがチェックボックス（`sae_seizure`等）を`SeriousAdverseEvent.objects.update_or_create`で保存し、`auto_snapshot`に当日の治療パラメータを記録。関連の正式な報告書は別モデル`AdverseEventReport`（`views.py`の`adverse_event_report_print_preview`/`_print`が担当、`services/sae_report.py`の`build_sae_context`が表示用コンテキストを構築）。

---

## J. ダッシュボードのToDo生成ロジック

- `dashboard_view`（`views.py`）は`?date=`パラメータ（無指定時は当日）に基づき、`services/schedule_tasks.py`の`compute_dashboard_tasks(patient, today, holidays)`を全対象患者に対して呼び出し、`dashboard_tasks`として`dashboard.html`へ渡す。
- `compute_task_definitions(patient)` が MT測定・治療前評価・3週目・4週目（`is_all_case_survey`時のみ）・6週目評価の予定日・実施可能ウィンドウ・実施済み日を算出し、`compute_dashboard_tasks`が「予定日≦今日 かつ 未実施」のタスクのみ抽出する。
- 日付計算・営業日判定は`services/rtms_schedule.py`（`generate_treatment_dates`, `is_closed`, `next_open_day`等）に集約。
- `dashboard_old.html`は**現在どのビューからもrenderされていないデッドコード**（views.pyに`'dashboard_old.html'`のrender呼び出しなし）。

---

## K. 削除候補（次フェーズで検討・本タスクでは未削除）

| 対象 | 種別 | 根拠 |
|---|---|---|
| `rtms_app/views.py.backup` | Pythonファイル | gitで未追跡（`??`）、現行`views.py`と内容が異なる（3471行 vs 2684行）の古いバックアップ。リポジトリに残す理由なし。 |
| `rtms_app/templates/rtms_app/dashboard_old.html` | テンプレート | どのviewからもrenderされていない。`dashboard.html`が現行版。 |
| `rtms_app/templates/rtms_app/assessment/scales/hamd_modal_new.html` | テンプレート | `views.py`の`assessment_scale_form`（2222行目）は`hamd_modal.html`のみを参照。中身は`hamd_modal.html`とほぼ同一のHAM-D入力ロジックの重複コード。 |
| `rtms_app/templates/rtms_app/export_research_csv.html` | テンプレート | どのview/URLからも参照なし。研究用CSV出力は`services/export_research.py`の`ResearchCSVExporter`が別経路（テンプレート不使用）で担当。 |
| `rtms_app/templates/rtms_app/adverse_event_report_form.html` | テンプレート | 独立した`<html>`構造で、render呼び出し・include元が見つからない。 |
| `rtms_app/templates/rtms_app/print/hamd_detail.html` | テンプレート | 他テンプレートからのinclude、view側のrenderともに確認できず。 |
| `rtms_app/templates/rtms_app/print/_hamd17_trend_table.html` | テンプレート | `_hamd_trend_print_compact.html`と役割が重複しており、実際の呼び出し元が確認できない。要再確認。 |
| `rtms_app/templates/rtms_app/partials/floating_actions.html` | パーシャル | どのテンプレートからも`{% include %}`されていない。 |
| `rtms_app/templates/rtms_app/partials/plan_inline_bar.html` | パーシャル | コメント内言及のみで実際のincludeなし。 |
| `rtms_app/templates/rtms_app/partials/print_box.html` | パーシャル | includeされている箇所が見つからない。 |
| `rtms_app/templates/rtms_app/partials/recommendation_badge.html` | パーシャル | includeされている箇所が見つからない（`recommendation.py`の結果は別の場所でインライン表示されている可能性）。 |
| `rtms_app/services/mapping_service.py` の `get_latest_mt_percent()` | 関数 | 定義のみで呼び出し元なし。 |
| `rtms_app/services/calender.py` | ファイル | 中身0行の空ファイル。 |
| `rtms_app/services/print_service.py` の `validate_print_docs`, `get_patient_for_print`, `build_print_context`, `get_clinical_path_context` | 関数群 | scaffoldされたが実際のprint_viewsからは呼ばれていない。 |
| `_attic/venv_old/` | ディレクトリ | 明らかに旧仮想環境。リポジトリ管理対象として不要。 |
| ルート直下の各種`.md`（`ADVERSE_EVENT_IMPLEMENTATION.md`, `cleanup_report.md`, `DEPLOYMENT_READY.md`, `FEATURE_IMPLEMENTATION_2024Dec.md`, `IMPLEMENTATION_SUMMARY.md`, `PR_CHECKLIST.md`, `PRODUCTION_MIGRATION_FIX.md`, `QUICK_START_GUIDE.md`, `UI_IMPROVEMENT_SAE_SIDEEFFECT.md`等） | ドキュメント | 開発途中の作業ログ的ドキュメントが多数ルート直下に散在。内容の陳腐化リスクあり。`docs/`配下への集約を推奨（削除ではなく整理）。 |

**注記**: `admin_backup.html`は`admin.py`の`RTMSAdminSite.admin_backup_view`から実際にrenderされており、**削除候補ではない**（初回調査では未使用と誤判定されたため、直接grepで裏取り済み）。

---

## L. 重複コード候補

1. **HAM-Dスコア計算/重症度判定ロジック**: `hamd_widget.js`（`calcHAMD17`/`getSeverity`）と`hamd_modal.html`インラインJS（`updateHAMDScores`相当）で別実装が重複。
2. **HAM-Dキーボード操作（0-4キー）**: `patient_first_visit.html`と`hamd_modal.html`にそれぞれ個別実装。
3. **モーダルフォームのfetch送信処理**: `patient_first_visit.html`（`submitAssessmentModal`, `submitHamdModalForm`等）と`hamd_modal.html`（`submitHamdModalForm`）で類似のfetch-to-JSONロジックが重複。
4. **印刷view のコンテキスト構築**: `print_views.py`内で`patient_print_discharge`/`_pdf`、`patient_print_referral`/`_pdf`、`patient_print_admission`/`_pdf`、`print_side_effect_check`/`_pdf`の各ペアが、render方式（HTML/PDF）以外ほぼ同一のコンテキスト構築コードを重複保持。
5. **`--card-accent`インラインstyle**: 12箇所以上のテンプレートで同一パターンを個別記述（CSSクラス化されていない）。
6. **`.fab-stack`/`.fab`定義の分散**: `floating.css`・`box_style.css`・複数テンプレートのインラインstyleに分散。
7. **モーダルz-indexの個別上書き**: `hamd_modal.html`, `questionnaire_edit.html`, `treatment_add.html`でそれぞれ異なる値を独自定義。
8. **Assessment（旧）とAssessmentRecord（新）の二重保存**: `assessment_scale_form`が両モデルに同じHAM-Dスコアを毎回書き込んでいる（機能上は必要な後方互換だが、恒久的な二重管理はリスク）。

---

## M. リファクタリング候補（今後の方向性、今回は未実施）

- **views.py の分割**: 2684行を機能単位（dashboard/assessment/treatment/patient-profile等）で複数ファイルに分割し、`print_views.py`と同様の構成に揃える。
- **共通「出口メニュー」パーシャル化**: 直近の対応で`page_actions.css`は共通化したが、HTML側（保存・戻る・印刷ボタンの並び）はテンプレートごとに個別記述のまま。`partials/page_exit_menu.html`のようなincludeテンプレートへ将来的に統合可能。
- **`--card-accent`のクラス化**: インラインstyleを`.card-accent-first-visit`等の意味のあるクラスへ置換。
- **印刷viewのコンテキスト共通化**: discharge/referral/admission/side_effectの各ペアについて、`_build_xxx_context(patient)`ヘルパーへ抽出（PART Bの調査で具体案を確認済み）。
- **HAM-D関連JSの一本化**: `hamd_widget.js`に計算・キーボード操作ロジックを集約し、`hamd_modal.html`・`patient_first_visit.html`側は関数呼び出しのみにする。
- **Assessment(旧)の段階的縮退計画の明文化**: 新規機能は`AssessmentRecord`のみを正とし、旧`Assessment`は読み取り専用の後方互換ビューに限定する方針を検討（今回は変更しない）。
- **サービス層の整理**: `print_service.py`の未使用関数、`mapping_service.py`の未使用関数を、実際に使う形へ改修するか、明確に「未使用」であることをコメントで明示。

---

## N. 絶対に壊してはいけない機能・依存関係

- **`Assessment`と`AssessmentRecord`の二重保存**（`assessment_scale_form`）: 既存の集計（`course_summary_service`、印刷書類）は両モデルを前提に動作しており、どちらか一方だけを更新する変更は破壊的。
- **`UniqueConstraint`群**（`MappingSession`, `TreatmentSession`, `Assessment`, `AssessmentRecord`, `SeriousAdverseEvent`, `PatientSurveyResponse`）: 自然キーの一意性はデータ整合性の根幹。変更時は既存データのマイグレーション設計が必須。
- **`TreatmentSession.save()`内の`intensity`/`intensity_percent`相互補完ロジック**: 新旧フィールドの互換性維持に必須。
- **`treatment_add`の初期値決定ロジック**（前回刺激強度引き継ぎ、MappingSessionからのMTデフォルト）: 治療安全性に直結するため変更は要ドクター確認レベルの慎重さが必要。
- **`assessment_rules.compute_improvement_rate` / `classify_response_status`のしきい値**（寛解7点、反応50%など）: 臨床判定基準そのもの。
- **WeasyPrintベースのPDF生成パイプライン**（`render_pdf_response`）と各印刷URLの命名（`_pdf`サフィックス規約）。
- **`SeriousAdverseEvent`/`AdverseEventReport`の保存経路**: 重篤有害事象の記録漏れは安全管理上致命的。
- **`services/rtms_schedule.py`の日付計算ロジック**（`generate_treatment_dates`, `is_closed`等）: ダッシュボードToDoと治療スケジュール全体の基盤。
- **`db.sqlite3`および全マイグレーション履歴（0001〜0039）**: 本番相当データを含む前提のため、マイグレーション改変は特に慎重に。
- **患者ポータル側（`views_patient.py`, `patient_urls.py`, `surveys/definitions.py`）の自己記入式スコア計算**（`calculate_score`）: 研究データの正確性に直結。
- **カスタムAdminSite経由の研究用データ出力・バックアップ機能**（`admin.py`の`research_export_view`/`admin_backup_view`）: 一見「backup」という名前だが実運用機能。

---

## O. 今後推奨するディレクトリ構造（提案のみ、今回は未実施）

```
rtms_app/
├── models/                      # models.pyを機能別に分割（patient.py, treatment.py, assessment.py, survey.py, audit.py）
├── views/
│   ├── dashboard.py
│   ├── patient_profile.py       # 初診・基本情報・退院準備
│   ├── treatment.py             # 治療実施・スキップ
│   ├── mapping.py               # MT測定
│   ├── assessment.py            # hub/scale_form
│   ├── calendar.py
│   └── audit.py
├── print/                       # print_views.py を分割し、print/配下に集約（views.pyから独立済みの延長）
├── services/                    # 既存を維持しつつ未使用関数を整理
├── static/rtms_app/
│   ├── css/ (現行8ファイルをここへ集約)
│   └── js/  (現行6ファイルをここへ集約)
├── templates/rtms_app/
│   ├── partials/
│   │   ├── page_exit_menu.html  # 出口メニュー共通化（保存/戻る/印刷ボタン群）
│   │   └── (既存パーシャルの未使用分は削除後に整理)
│   └── assessment/scales/       # hamd_modal_new.html等の重複を削除後、hamd用/他尺度用で明確に分離
└── docs/                        # ルート直下に散在する作業ログ的.mdファイルをここへ集約
```

※ ディレクトリ構造の変更自体は影響範囲が大きいため、実施する場合は「1機能ずつ移動→テスト→コミット」の段階的移行を推奨。

---

## 付録: 検証済みの事実確認（矛盾解消メモ）

調査中にサブエージェント間で判断が割れた点を、直接grepで裏取りした結果:

- `hamd_modal_new.html` は `views.py` 内のどこからも参照されていない（`assessment_scale_form`は`hamd_modal.html`のみ使用） → **未使用と確定**。
- `admin_backup.html` は `admin.py` の `RTMSAdminSite.admin_backup_view` から実際に render されている → **使用中、削除候補から除外**。
- `export_research_csv.html` はどのPythonファイルからも参照されていない → **未使用と確定**（CSV出力は`services/export_research.py`が担当）。
- `partials/floating_actions.html`, `plan_inline_bar.html`, `print_box.html`, `recommendation_badge.html` は実際の`{% include %}`が存在しない（`plan_inline_bar.html`はコメントで名前が言及されるのみ） → **未使用と確定**。
- `rtms_app/views.py.backup` は git 管理外（`git status --porcelain`で`??`）で、現行`views.py`と行数・内容が異なる → **削除候補として妥当**。
