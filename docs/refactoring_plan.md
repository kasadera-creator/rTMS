# rTMS支援システム リファクタリング事前調査報告書（第0段階：現状確認・コード変更なし）

作成日: 2026-08-20
前提資料: [docs/system_structure_report.md](system_structure_report.md)（現状仕様書）
本書の位置づけ: 上記資料を実コードと再照合し、リファクタリング着手前の「現状・問題点・安全な実施順序」を確定するための調査結果。**本書作成にあたりコードは一切変更していない。**

---

## 0. 資料照合の結果（system_structure_report.md との差分チェック）

`system_structure_report.md` 作成後、ファイル変更通知があったため主要ファイルを再確認した。結論として**記載内容と実コードは一致**しており、以下を追加で確認・訂正した。

- `rtms_app/views.py` の関数一覧・行番号（`assessment_scale_form`が2222行目で`hamd_modal.html`のみを使用等）は現行コードと一致。**`hamd_modal_new.html`は引き続きどこからも参照されていないことを再確認**。
- 新規確認: `rtms_app/templates/rtms_app/assessment/scales/placeholder.html`（HAM-D以外の尺度用の汎用プレースホルダー、`views.py`2227行目で使用）と`rtms_app/templates/rtms_app/assessment/hub_modal.html`（`views.py`1953行目で使用）は**実際に使われている現行ファイル**であり、削除候補ではない。
- **訂正**: 前回報告で「templatetags」としていた`hamd.py`/`request_context.py`は誤りで、実際は`rtms_app/utils/`配下に存在する（`rtms_app/templatetags/`は`dict_extras.py`と`rtms_extras.py`のみ）。
- **新規発見**: `rtms_app/utils/hamd.py`（`classify_hamd_response`, `classify_hamd17_severity`）は**プロジェクト内のどこからもimportされていない完全な未使用モジュール**（後述3章）。
- **新規発見**: HAM-D17重症度判定ロジックが実質**3箇所に別実装で存在**している（`assessment_rules.py`のもの、`utils/hamd.py`のもの、`course_summary_service.build_assessment_trend`内のインライン`if/elif`）。実際に印刷・退院準備画面で使われているのは`course_summary_service`のインライン実装のみ。
- 4画面（初診/クリニカルパス/治療実施/退院準備）の`page-exit-menu`は現状のまま維持されており、他の画面（`assessment_add.html`, `assessment/hub.html`, `assessment/scale_form_base.html`, `mapping_add.html`, `patient_add.html`, `questionnaire_edit.html`）は従来通り`fab-stack`のままである（今回のリファクタリング対象外の画面には手を入れていないことを確認）。
- `git log`上の最終コミットは`52f9745`（「患者さん自己記入式検査」）で、**それ以降の大量の変更（views.pyの大部分を含む）が未コミットのまま作業ツリーに存在**している。これはリファクタリング着手前に必ず対応すべき事項（8章 第1段階参照）。

---

## 1. 現在の構造

```
rTMS/  (Django 5.0.14 / Python 3.12 / SQLite, WeasyPrintでPDF生成)
├── config/                 # プロジェクト設定・ルートURL
├── rtms_app/                # 単一アプリに全機能集約
│   ├── views.py             # 2684行・56関数（dashboard/patient/treatment/assessment/calendar/audit等が未分割で同居）
│   ├── views.py.backup      # 3471行・gitで未追跡の旧バックアップ（現行と内容差分あり）
│   ├── print_views.py       # 597行・印刷/PDF専用（既にviews.pyから分離済み）
│   ├── views_patient.py / views_health.py / views_survey_export.py
│   ├── models.py (662行) / forms.py (283行) / admin.py (274行)
│   ├── assessment_rules.py  # HAM-D改善率・寛解/反応・重症度の「正式」ルール定義
│   ├── protocols.py          # 保険診療プロトコル定義（現状ランタイムの初期値には未接続）
│   ├── services/             # 12ファイル。実利用されているものと未使用のものが混在（3章参照）
│   ├── utils/                 # hamd.py（未使用）, request_context.py（middleware/signals/viewsで使用中）
│   ├── templatetags/          # dict_extras.py, rtms_extras.py
│   ├── surveys/definitions.py # 自己記入式尺度定義
│   ├── management/commands/create_patient_users.py
│   ├── migrations/ (0001〜0039、直近0038/0039が未コミット追加)
│   ├── templates/rtms_app/ (65+テンプレート、assessment/・print/・partials/等にサブフォルダ分割済み)
│   └── static/rtms_app/ (JS 6本 + CSS 8本)
├── static/ , staticfiles/    # プロジェクト直下静的ファイルとcollectstatic成果物
└── docs/                     # 本書・system_structure_report.md 等
```

**規模感**: `views.py`（2684行）と`treatment_add.html`（1536行）、`patient_first_visit.html`（1024行）が突出して大きく、機能追加のたびにこの3ファイルへ集中しやすい構造になっている。

---

## 2. 問題点

| # | 問題 | 影響 |
|---|---|---|
| 1 | `views.py`に56関数・2684行が未分割で集約 | 変更時の影響範囲把握が困難、レビューコストが高い |
| 2 | HAM-D17重症度判定ロジックが3箇所に重複実装（`assessment_rules.py`, `utils/hamd.py`, `course_summary_service.py`インライン） | 将来しきい値変更時に修正漏れが起きやすい。現状は`course_summary_service`版のみが実際に稼働 |
| 3 | `Assessment`（旧）と`AssessmentRecord`（新）への二重保存 | データモデルの複雑化、将来の一本化が難しくなる負債 |
| 4 | `print_views.py`内でプレビュー版/PDF版のコンテキスト構築コードが書類種別ごとにほぼ全文重複 | バグ修正・仕様変更時に2箇所ずつ直す必要がある |
| 5 | `print_urls`が`config/urls.py`と`rtms_app/urls.py`の両方からマウントされている | 実害はないが冗長で誤解を招く |
| 6 | `views.py.backup`がgit管理外のままリポジトリに残存 | 誤って参照・編集・コミットされるリスク |
| 7 | 未使用ファイル（`hamd_modal_new.html`, `utils/hamd.py`, `export_research_csv.html`等）が本物のコードと混在 | 新規参加者が誤って「使われている」と誤解しやすい |
| 8 | ルート直下に開発中ログ的`.md`ファイルが多数散在 | ドキュメントの陳腐化・散逸 |
| 9 | 最終gitコミット以降の大量の変更が未コミット | リファクタリング作業の「安全なチェックポイント」が存在しない状態 |
| 10 | CSSのインライン`--card-accent`パターン・`.fab-stack`定義の分散・モーダルz-indexの個別上書きがテンプレート横断で重複 | 見た目の一貫性維持コストが高い |

---

## 3. 削除候補（コード全体から参照関係を検索し、直接確認済み）

> 「ファイル名から未使用と推測」ではなく、全て`grep`によるプロジェクト全体の参照検索で「参照ゼロ」を確認したもののみ記載。

| 対象 | 確認方法 | 結果 |
|---|---|---|
| `rtms_app/views.py.backup` | `git status --porcelain` | `??`（未追跡）。現行`views.py`と行数・内容が異なる。**先にgitで現状をコミットしてから削除**（後述、単純削除は禁止）。 |
| `rtms_app/templates/rtms_app/dashboard_old.html` | `views.py`内`render()`呼び出し全検索 | どこからも参照なし。`dashboard_view`は`dashboard.html`のみ使用。 |
| `rtms_app/templates/rtms_app/assessment/scales/hamd_modal_new.html` | `views.py`・全テンプレートの参照検索 | `assessment_scale_form`（2222行目）は`hamd_modal.html`のみ使用。他画面からのincludeもなし。 |
| `rtms_app/utils/hamd.py` | `grep "utils.hamd\|classify_hamd_response\|classify_hamd17_severity"` | 定義以外の呼び出しがプロジェクト全体でゼロ。 |
| `rtms_app/templates/rtms_app/export_research_csv.html` | 全`.py`ファイルのrender/参照検索 | 参照なし。研究用CSVは`services/export_research.py`の`ResearchCSVExporter`がテンプレートを介さず直接生成。 |
| `rtms_app/templates/rtms_app/adverse_event_report_form.html` | render呼び出し・include検索 | 参照なし。 |
| `rtms_app/templates/rtms_app/print/hamd_detail.html` | include・render検索 | 参照なし。 |
| `rtms_app/templates/rtms_app/partials/floating_actions.html` / `plan_inline_bar.html` / `print_box.html` / `recommendation_badge.html` | 全テンプレートの`{% include %}`検索 | いずれも実際のincludeなし（`plan_inline_bar.html`はコメント内で名前が言及されるのみ）。 |
| `rtms_app/services/mapping_service.py`の`get_latest_mt_percent()` | 全`.py`ファイルの呼び出し検索 | 呼び出し元なし。 |
| `rtms_app/services/calender.py` | ファイル内容確認 | 0行の空ファイル。 |
| `rtms_app/services/print_service.py`の`validate_print_docs`/`get_patient_for_print`/`build_print_context`/`get_clinical_path_context` | `print_views.py`からの呼び出し検索 | 呼び出しなし（`build_pdf_filename`と`CONTENT_LABELS`のみ実利用）。 |
| `_attic/venv_old/` | ディレクトリ内容確認 | 旧仮想環境そのもの。 |

**削除候補から除外したもの（重要）**:
- `rtms_app/templates/admin/admin_backup.html` — `admin.py`の`RTMSAdminSite.admin_backup_view`から実際にrenderされていることを確認済み。**削除禁止**。
- `rtms_app/templates/rtms_app/assessment/scales/placeholder.html` — `assessment_scale_form`（2227行目）でHAM-D以外の尺度フォームに使用中。**削除禁止**。
- `rtms_app/templates/rtms_app/assessment/hub_modal.html` — `assessment_hub`（1953行目）でモーダル表示時に使用中。**削除禁止**。

---

## 4. 移動候補（内容変更なしでファイル配置のみ変更できるもの）

低リスク（参照がDjangoのテンプレートローダー/静的ファイルローダー経由で解決されるため、ディレクトリを移動しても`{% include %}`や`{% static %}`のパスさえ追従修正すれば安全）:

- `rtms_app/print_views.py` → `rtms_app/views/print/` へ分割移動（既にviews.pyから独立しているため着手しやすい）。
- `rtms_app/static/rtms_app/*.css`（8ファイル）→ 用途別サブフォルダ（`css/pages/`, `css/print/`等）へ再配置。
- `rtms_app/static/rtms_app/*.js`（6ファイル）→ `js/widgets/`, `js/pages/`等へ再配置。
- ルート直下の作業ログ`.md`群 → `docs/archive/`へ移動（削除ではなく整理）。

**移動時の注意点（共通）**: `{% static 'rtms_app/xxx.css' %}`や`{% include 'rtms_app/partials/xxx.html' %}`のパス文字列は**テンプレート内に直書き**されているため、移動する場合は該当箇所を機械的に一括置換する必要がある（後述5章「危険」参照）。

---

## 5. 分割候補（内容はそのまま、ファイル構造のみ分割）

- **`views.py`（2684行）** → ドメイン別に分割:
  - `views/dashboard.py`（`dashboard_view`他、68-473行台のヘルパー含む）
  - `views/patient_profile.py`（`patient_first_visit`, `patient_basic_edit`, `patient_summary_view`, `patient_add_view`, `patient_list_view`）
  - `views/treatment.py`（`treatment_add`, `treatment_skip_list`, `treatment_skip_undo`, `mapping_add`, `admission_procedure`）
  - `views/assessment.py`（`assessment_hub`, `assessment_scale_form`, `assessment_add*`系, `_hamd_items`）
  - `views/calendar.py`（`_build_month_calendar`, `calendar_month_view`, `calendar_month_print_view`）
  - `views/audit.py`（`audit_logs_view`, `adverse_event_report_print*`）
  - 共通ヘルパー（`is_holiday`, `get_session_number`等の日付計算群）→ `views/_scheduling_helpers.py`または既存の`services/rtms_schedule.py`へ統合
- **`print_views.py`（597行）** → `_build_xxx_context()`ヘルパーを`services/print_service.py`に集約した上で、書類種別ごとに`print/discharge.py`, `print/referral.py`等へ分割（4章と連動）。
- **`treatment_add.html`（1536行）・`patient_first_visit.html`（1024行）** → インラインJS（3ブロックずつ）を`static/rtms_app/js/`へ外出し。HTML本体とスクリプトを分離するだけでも可読性が大幅に向上する。

---

## 6. 絶対に維持すべき依存関係（本リファクタリングで一切変更しない）

ユーザー指定の12項目を実コードの根拠とともに再確認した。

1. **初診画面↔治療前評価のHAM-D17連携**: `patient_first_visit.html`の`loadAssessmentModal('baseline')` → `assessment_scale_form`（`views.py`1971行）が`Assessment`と`AssessmentRecord`の**同一baselineレコード**を保存・参照。単純な数値コピーではない。**モデルの参照関係・保存先を変更しない**。
2. **前回刺激強度の引き継ぎ**: `treatment_add`（`views.py`1256-1268行付近）が直近`TreatmentSession`の`intensity_percent`/`mt_percent`を引き継ぐロジック。初回は60%・100%MTがフォーム既定値（`forms.py`のwidget属性）。
3. **MT測定値からの初期値**: `MappingSession.resting_mt` → `treatment_add`の`intensity_percent`初期値（前回セッションがない場合のフォールバック、1271-1274行付近）。
4. **副作用・SAE記録**: `SideEffectCheck`（`side_effect_rows_json`経由）、`SeriousAdverseEvent`（`sae_*`チェックボックス経由、1318-1356行付近）、正式報告書`AdverseEventReport`。`services/side_effect_schema.py`のスキーマと`side_effect_widget_v2.js`の契約。
5. **ダッシュボードToDo生成**: `services/schedule_tasks.compute_dashboard_tasks` ← `compute_task_definitions` ← `services/rtms_schedule.py`の営業日計算。
6. **クリニカルパスと治療予定日の連携**: `patient_clinical_path`ビュー・`_build_month_calendar`・`rtms_schedule.generate_treatment_dates`が共有する日付生成ロジック。
7. **HAM-D17/21の評価時点（治療前/3週/4週/6週）とその他尺度（治療前/治療後）**: `TimingScaleConfig`・`ScaleDefinition`・`AssessmentRecord.timing`choicesおよび`PatientSurveySession.phase`(pre/post)。
8. **PDF/印刷機能**: WeasyPrintパイプライン（`render_pdf_response`）、プレビュー/`_pdf`のURL命名規約、`_hamd_cols_for_patient`経由の`build_assessment_trend`連携。
9. **既存データの保存**: 全モデルの`UniqueConstraint`（`MappingSession`, `TreatmentSession`, `Assessment`, `AssessmentRecord`, `SeriousAdverseEvent`, `PatientSurveyResponse`ほか）。
10. **現在のURLと画面遷移**: `{% url 'rtms_app:xxx' %}`のURL名（`urls.py`/`patient_urls.py`/`print_urls.py`）。ファイル分割時も**URL名・`app_name`・namespace文字列は変更しない**。
11. **管理画面**: カスタム`RTMSAdminSite`（`admin.py`）の`get_urls()`拡張（研究データ出力・バックアップ）。
12. **`admin_backup.html`など実際に参照されているファイル**: 3章の「除外」リストの通り、`admin_backup.html`・`placeholder.html`・`hub_modal.html`は実使用中と確認済み。

**追加で維持すべき事項（今回の調査で新たに判明）**:
- 3箇所に重複するHAM-D17重症度判定ロジックのうち、**実際に画面・印刷に反映されているのは`course_summary_service.build_assessment_trend`のインライン実装のみ**。これを一本化する際は、必ず`assessment_rules.HAMD17_SEVERITY_BANDS`のしきい値（0-7/8-13/14-18/19-22/23+）と完全一致させ、置換後に退院準備画面・discharge/referral印刷の表示を目視比較すること。
- `print_urls`の二重マウント（`config/urls.py`の`/app/print/`と`rtms_app/urls.py`内`patient/<id>/print/`）は、URL逆引き名が衝突していないため実害はないが、**どちらかを削除する場合は両方の実URLパスが使われていないか事前に確認**すること。

---

## 7. 推奨ディレクトリ構造（提案・今回は未実施）

```
rtms_app/
├── models/                          # models.py を機能別分割（patient / treatment / assessment / survey / audit）
├── views/
│   ├── dashboard.py
│   ├── patient_profile.py
│   ├── treatment.py
│   ├── mapping.py
│   ├── assessment.py
│   ├── calendar.py
│   ├── audit.py
│   └── print/                       # print_views.py をここへ分割移動
│       ├── discharge.py / referral.py / admission.py / suitability.py
│       ├── bundle.py / path.py / side_effect.py
│       └── _pdf_common.py           # render_pdf_response, _build_*_context 共通ヘルパー
├── services/
│   ├── (既存ファイルを維持しつつ、未使用関数は削除 or 明示コメント)
│   └── hamd_classification.py       # 3箇所に重複する重症度/反応判定ロジックの一本化先（将来）
├── static/rtms_app/
│   ├── css/pages/  ・css/print/ ・css/widgets/
│   └── js/pages/   ・js/widgets/
├── templates/rtms_app/
│   ├── partials/page_exit_menu.html # 4画面で重複する出口メニューHTMLの共通化
│   └── (既存の未使用テンプレートを削除後、assessment/scales配下を整理)
└── docs/archive/                    # ルート直下の作業ログ的.mdを集約
```

---

## 8. リファクタリング実施順序（安全に実施できる作業単位への分解）

### 第0段階（完了・本書）
- 現状調査・実コードとの照合・削除候補の参照検索による裏取り。**コード変更なし**。

### 第1段階：リポジトリ衛生（最低リスク、コード動作に無影響）
1. 現在の未コミット変更（views.py含む全変更）を**まずgitコミット**し、安全な復元ポイントを作る（`git add -A && git commit -m "..."`）。※本書ではコマンドを提案するのみで実行しない。
2. コミット後、`rtms_app/views.py.backup`を削除（gitで復元可能な状態を確保してから実施）。
3. `rtms_app/_attic/venv_old/`、確定済み未使用テンプレート（`dashboard_old.html`, `hamd_modal_new.html`, `export_research_csv.html`, `adverse_event_report_form.html`, `print/hamd_detail.html`, 4つの未使用partial）、`utils/hamd.py`、`services/calender.py`、`services/mapping_service.get_latest_mt_percent()`を削除。
4. ルート直下の作業ログ`.md`を`docs/archive/`へ移動。

### 第2段階：静的ファイルの整理（低リスク）
1. CSS/JSを`static/rtms_app/css/`・`js/`のサブフォルダへ再配置し、テンプレート側の`{% static %}`パスを一括更新。
2. `.fab-stack`定義を`floating.css`に一本化し、`box_style.css`・各テンプレートのインライン上書きを削除。

### 第3段階：テンプレート共通化（中リスク・画面ごとに目視確認必須）
1. 4画面（初診/クリニカルパス/治療実施/退院準備）の`page-exit-menu`ブロックを`partials/page_exit_menu.html`へ抽出し、各テンプレートから`{% include %}`に置換。
2. `--card-accent`インラインstyleをクラス化。

### 第4段階：`views.py`の分割（中〜高リスク・importパス変更を伴う）
1. `views/`パッケージを新設し、関数単位でファイルへ移動（1関数ずつ移動→`manage.py check`→動作確認、を繰り返す）。
2. `urls.py`のimport文を新パッケージ構造に合わせて更新（URL名自体は変更しない）。

### 第5段階：`print_views.py`の分割とコンテキスト共通化（中リスク）
1. discharge/referral/admission/side_effectの各ペアについて`_build_xxx_context()`を抽出。
2. プレビュー版と`_pdf`版が同じヘルパーを呼ぶ形に統一。

### 第6段階：HAM-D重症度判定ロジックの一本化（高リスク・要臨床確認）
1. `assessment_rules.py`を正とし、`course_summary_service.build_assessment_trend`のインライン実装を`assessment_rules.classify_hamd17_severity`呼び出しに置換。
2. 置換前後で退院準備画面・discharge/referral印刷のHAM-D推移表示を全患者サンプルで目視比較。

### 第7段階（長期・本リファクタリング範囲外として保留）
- `Assessment`（旧）と`AssessmentRecord`（新）の二重保存の一本化は、データ移行設計が必要なため別プロジェクトとして切り出すことを推奨。今回は着手しない。

---

## 9. 各段階で確認すべき動作（検証チェックリスト）

**全段階共通**:
- [ ] `python manage.py check`（エラーなし）
- [ ] `python manage.py test rtms_app`（既存テスト全件成功）
- [ ] `git diff`で意図しない変更が混入していないか確認

**第1段階（削除系)**:
- [ ] 削除前に対象ファイルパスを再度grepし、参照ゼロを再確認
- [ ] 削除後、ダッシュボード・初診・MT測定・治療実施・評価尺度hub・退院準備・クリニカルパス・各種印刷プレビュー/PDFをブラウザで一通り開いて500エラーが出ないことを確認

**第2〜3段階（静的ファイル・テンプレート共通化）**:
- [ ] 対象4画面（初診/クリニカルパス/治療実施/退院準備）の出口メニューが従来通り表示・動作すること（保存/印刷/戻るボタンの並び含む）
- [ ] iPad幅（1024px前後）でのレイアウト崩れがないこと
- [ ] 印刷CSS（`print.css`）に影響していないこと（実際に印刷プレビューを開いて確認）

**第4段階（views.py分割）**:
- [ ] 分割後も全URL（`urls.py`記載の全パス）が同じview関数に到達すること（`python manage.py check`に加え、主要URLをテストクライアントで叩く）
- [ ] `{% url 'rtms_app:xxx' %}`を使う全テンプレートでNoReverseMatchが発生しないこと

**第5段階（print_views分割）**:
- [ ] 7書類×プレビュー/PDFの全14エンドポイントを実際にリクエストし、HTTP 200とPDFバイナリ生成を確認
- [ ] `_hamd_cols_for_patient`経由のHAM-D推移表がdischarge/referral/bundleで従来と同じ値を表示すること

**第6段階（重症度判定の一本化）**:
- [ ] 複数患者（正常/軽症/中等症/重症/最重症の全バンドを含むサンプル）で置換前後の表示値が完全一致すること
- [ ] `assessment_rules.classify_response_status`（寛解/反応/反応なし）の判定結果も併せて回帰確認

---

## まとめ

現時点でコードは一切変更していない。次のアクションとして提案するのは、**第1段階の最初の一歩＝「現在の未コミット変更を git commit してから、確定済みの未使用ファイルを削除する」**という、最もリスクの低い作業単位です。実施の許可をいただければ、この第1段階から着手します。
