# rTMS支援システム 静的資産・テンプレート依存関係調査報告書（第2段階：調査のみ・コード変更なし）

作成日: 2026-08-20
前提資料: [docs/system_structure_report.md](system_structure_report.md), [docs/refactoring_plan.md](refactoring_plan.md)
本書の目的: 「整理すること」ではなく「整理しても壊れないことを確認するための地図を作ること」。**本書作成にあたりコードは一切変更していない。**

---

## A. 現在のCSS構造

CSSは実際には**4つの物理ディレクトリ**に分散している（想定より1つ多い）。

| ディレクトリ | ファイル | 備考 |
|---|---|---|
| `rtms_app/static/rtms_app/` | `app.css`(97行), `box_style.css`(68行), `floating.css`(15行), `mapping.css`(18行), `page_actions.css`(20行), `patient.css`(86行), `print.css`(269行), `print_toolbar.css`(9行) | Djangoアプリ規約通り`rtms_app/`名前空間で配置（正しい形） |
| `static/css/`（プロジェクト直下、`STATICFILES_DIRS`経由） | `rtms_theme.css`(150行), `print_a4.css`(18行), `calendar.css`(122行) | マスターデザインシステム・グローバル系 |
| `rtms_app/static/css/` | `admin_custom.css` | **名前空間なし**でapp static配下に配置（`rtms_app/`プレフィックスがない）。`/static/css/admin_custom.css`として配信される。**どのテンプレートからも参照されていないことを確認済み（未使用）**。 |
| `rtms_app/static/img/` | `logo.jpg` | 同じく名前空間なし。`base.html`が`{% static 'img/logo.jpg' %}`で参照（使用中）。 |
| `static/rtms_app/` | `.DS_Store`のみ | **空の迷子ディレクトリ**（中身なし）。 |

**各ファイルの役割（要約）**:
- `rtms_theme.css`: マスターデザインシステム。CSS変数（`--space-*`, `--font-*`, `--header-h`等）、`.rtms-shell`, `.page-title`, `.section-title`, `.card-like`, `.form-rtms`, `.table-rtms`, `.badge-rtms`(5variant), `.dashboard-grid`, `.hamd-row`, `.q-badge`(24×24px)を定義。
- `box_style.css`: `.rtms-main`スコープの`.card`/`.card-header`リセット、`.card-like`（rtms_theme.cssと重複定義）、`.card-header-accent`、`.fab-stack .fab`の影調整。
- `app.css`: `.app-card`系コンポーネント、`--card-accent`/`--card-bg`によるダッシュボードのテーマ切替、`.app-page-title`/`.app-section`/`.floating-action-menu`（**いずれも未使用**）。
- `floating.css`: `.fab-stack`（固定配置、z-index:2000）、`.fab`本体。印刷時`!important`で非表示。
- `page_actions.css`: `.page-exit-menu`（実使用）と`.page-document-actions`（**定義のみで未使用**）。
- `mapping.css` / `patient.css` / `print.css` / `print_toolbar.css` / `calendar.css` / `print_a4.css`: 各画面専用（後述B節）。

---

## B. CSS依存関係

**`base.html`から読み込まれる共通CSS（全画面共通）**:
```
css/rtms_theme.css
css/print_a4.css（media="print"）
rtms_app/box_style.css
rtms_app/app.css
```

**個別ページから読み込まれるCSS（ページ固有）**:

| CSS | 読み込み元テンプレート |
|---|---|
| `rtms_app/floating.css` | `assessment_add.html`, `assessment/_form.html`, `assessment/hub.html`, `assessment/scale_form_base.html`, `patient_add.html`, `questionnaire_edit.html` |
| `rtms_app/mapping.css` | `mapping_add.html` |
| `rtms_app/patient.css` | `patient/base_patient.html` |
| `rtms_app/page_actions.css` | `patient_clinical_path.html`, `patient_first_visit.html`, `patient_summary.html`, `treatment_add.html` |
| `rtms_app/print_toolbar.css` + `rtms_app/print.css` | `print/_print_base.html` |
| `css/calendar.css` | `calendar_month.html`, `print/calendar_month.html` |

**`@import`によるCSS間参照**: なし（全てHTML側の`<link>`で個別読み込み。カスケード設計としては分かりやすいが、共通クラスの二重定義を招いている＝C節）。

**未使用CSSファイル**: `rtms_app/static/css/admin_custom.css`はどのテンプレートからも参照されていない（grep結果ゼロ）。

---

## C. CSS重複

| 重複クラス/パターン | 定義箇所 | 問題 |
|---|---|---|
| `.card-like` | `box_style.css`（45行付近）と`rtms_theme.css`（75行付近）の**両方**で定義 | プロパティ値も微妙に異なり、どちらが優先されるかは読み込み順（`rtms_theme.css`→`box_style.css`）依存。統合時は慎重な比較が必要。 |
| `.q-badge` | `rtms_theme.css`(24×24px) / `patient.css`(36×36px) / `assessment_add.html`インライン(28×28px) / `hamd_modal.html`インライン(30×30px、赤背景) / `questionnaire_edit.html`インライン(28px) | **4種類以上のサイズ・配色が併存**。最も断片化が激しい箇所。 |
| `.hamd-row` | `rtms_theme.css`で最小定義、`assessment_add.html`/`assessment/baseline.html`/`assessment/scale_form_base.html`/`assessment/week3.html`/`hamd_modal.html`の**5箇所でインライン上書き**（微妙にpadding値が異なる） | 単一ソース化されておらず、修正時に5箇所を触る必要がある。 |
| `.assessment-cell` | CSSファイルには存在せず、`assessment/hub_modal.html`と`assessment/hub.html`の**インライン`<style>`のみ**で同一定義 | 共有CSSに未昇格。 |
| `.sticky-scorebar` | CSSファイルには存在せず、**7テンプレートのインライン`<style>`**で同一定義（sticky, bottom:0, z-index:1020） | 同上。 |
| `.fab-stack` / `.fab` | `floating.css`（本体定義）＋`box_style.css`（`.fab-stack .fab`の影のみ再定義）＋`mapping_add.html`インライン（`bottom:15px; right:15px;`で位置を上書き） | 3箇所に分散、`mapping_add.html`の上書きは値が異なり意図が読み取りにくい。 |
| `.page-document-actions` | `page_actions.css`で定義 | **どのテンプレートからも未使用**（`.page-exit-menu`のみ実使用）。 |
| モーダルz-index | `hamd_modal.html`(2050) / `questionnaire_edit.html`(2050) / `treatment_add.html`(2060) | **treatment_add.htmlだけ値が異なる**（2050 vs 2060）。同時に複数モーダルが開くケースがあれば表示順が崩れる潜在バグ。 |
| `.evt-*`（カレンダーイベント色） | `calendar.css`（本体） + `patient_clinical_path.html`インライン + `print/path.html`インライン（`.evt-badge`という別名で再定義） | 画面用・印刷用で別々に色を持っており、色を変える際は3箇所を揃える必要がある。 |
| `base.html`内56行のインライン`<style>` | `base.html` | `--theme-primary`/`--color-*`/`.btn-*-primary`等の**テーマ定義そのもの**がCSSファイルではなくbase.html内にベタ書きされている。本来`rtms_theme.css`にあるべき内容。 |
| `!important`使用 | 主に`print.css`/`print_a4.css`/`calendar.css`/`floating.css`/`print_toolbar.css`の`@media print`ブロック内（印刷時の強制非表示・色調整） | 印刷用途では妥当な使用。ただし`base.html`209行付近の`.floating-actions{z-index:2000 !important;}`は`@media print`ブロック内にありながら画面表示用のプロパティで、**用途が不自然（要調査だが今回は変更しない）**。 |

**「見た目は似ているが別実装」のUIパターン**:
- **floating FAB（`fab-stack`、旧式）** vs **page-exit-menu（新式）**: 目的は同じ（保存/戻る/印刷の操作群）だが完全に別のCSS・別のDOM構造。7画面がFAB、4画面がexit-menu。
- **カードヘッダーの色帯**: `.card-header-accent`（共通、11テンプレートで正しく共有）は良好だが、`--card-accent`インライン変数指定は各テンプレートで個別に値を設定しており「共有クラス＋個別インライン変数」のハイブリッド。
- **ステータス/重症度バッジ**: `.badge-rtms`（共通、5variant）は綺麗に共有されている一方、`.q-badge`（採点番号バッジ）は前述の通り断片化。

---

## D. 現在のJavaScript構造

**外部JSファイル（6本、`rtms_app/static/rtms_app/`）**:

| ファイル | 行数 | 役割 |
|---|---|---|
| `floating.js` | 24 | `window.RTMSFloating.{clickById, submitFormById, openPrintForm}` — FABボタンの共通クリック/送信ヘルパー |
| `hamd_widget.js` | 178 | HAM-D17項目ウィジェット（ボタン群制御、popover初期化、`calcHAMD17()`合計/重症度表示） |
| `calendar_focus.js` | 87 | URLの`?focus=YYYY-MM-DD`をもとにカレンダーの該当日へスクロール/遷移 |
| `patient_surveys.js` | 145 | 患者ポータルの自己記入式アンケート（`SurveyPage`モジュール、debounce自動保存、前後ページ送り） |
| `side_effect_widget_v2.js` | 364 | `SideEffectWidget`クラス（副作用チェック表、0→1→2→3循環ボタン） |
| `wizard.js` | 1094 | `ProcedureWizard`クラス（治療実施9ステップウィザード、副作用/SAEモーダル起動を含む） |

**インラインJSが多いテンプレート**:

| テンプレート | ブロック数 | 概算行数 | 主な内容 |
|---|---|---|---|
| `patient_first_visit.html` | 3 | 約400行 | iPad用textareaモーダル、既往歴トグル、HAM-D/問診票モーダル読み込み・送信・キーボード操作 |
| `treatment_add.html` | 2 | 約500行 | 印刷ハンドラ、スキップ/中止モーダル、フォーム二重送信防止、wizard.js連携用グローバル関数 |
| `questionnaire_edit.html` | 1 | 約100行 | Y/N問診票のキーボード自動送り |
| `assessment/scales/hamd_modal.html` / `assessment/scales/hamd.html` | 各1 | 各約100行 | HAM-D 0-4キー自動送り（**ほぼ同一内容が2ファイルに存在**） |
| `mapping_add.html` | 1 | 約30行 | 治療開始日からの週番号自動計算 |
| `patient_summary.html` | 2 | 約50行 | オートセーブ＋トースト表示 |
| `print/_print_toolbar.html` | 1 | 約60行 | 戻る/印刷/PDF保存（html2pdf.js使用） |

---

## E. JavaScript依存関係

| 観点 | 内容 |
|---|---|
| Bootstrap JS必須 | `patient_first_visit.html`, `treatment_add.html`, `wizard.js`, `hamd_widget.js`（Popover）、`base.html`（Tooltip） |
| Vanilla JSのみ | `calendar_focus.js`, `floating.js`, `patient_surveys.js`, `side_effect_widget_v2.js`, `mapping_add.html`, `assessment/hub_modal.html`, `patient_summary.html`, `questionnaire_edit.html` |
| localStorage/sessionStorage | **使用箇所なし**（状態管理は隠しinput・fetchオートセーブ・JSクラス内メモリのみ） |
| AJAX/fetch | 9箇所（GET+DOM注入3、POST JSONオートセーブ3、POST JSON+リダイレクト/リロード3） |
| print系トリガー | `window.print()`直接呼び出し2箇所、html2pdf.js 1箇所、fetch→print URL遷移2箇所 |
| 数値ステッパー | `mapping_add.html`のMT%用カスタム（±5）、`side_effect_widget_v2.js`の0→1→2→3循環ボタン、他は標準`<input type=number step=...>` |

---

## F. JavaScript重複

| 重複機能 | 実装箇所（重複数） | 深刻度 |
|---|---|---|
| HAM-D 0-4キー自動送り | `hamd_modal.html` / `hamd.html`（ほぼ同一） / `patient_first_visit.html`（独自変数名で別実装） = **3箇所** | 高（微妙にロジックが異なり保守リスク） |
| モーダルフォーム送信（POST→JSON→reload） | `patient_first_visit.html`内`submitAssessmentModal()`と`submitQuestionnaireModal()` = **同一ファイル内2箇所** | 高（ほぼコピペ） |
| 問診票Y/N自動送り | `patient_first_visit.html`（モーダル用） / `questionnaire_edit.html`（全画面用） = **2箇所** | 中 |
| Bootstrapモーダルの開き方 | `new bootstrap.Modal().show()`（`patient_first_visit.html`, `treatment_add.html`） vs `bootstrap.Modal.getOrCreateInstance().show()`（`wizard.js`） | 中（インスタンス生成方式が不統一） |
| 印刷トリガー | `window.print()`（`_print_toolbar.html`, `adverse_event_report_db.html`） vs html2pdf.js（`_print_toolbar.html`内、別ボタン） | 低 |
| オートセーブ（fetch POST） | `patient_surveys.js`（debounce方式） / `patient_summary.html`（都度POST＋トースト） = **2箇所** | 中 |

---

## G. Template構造

**`base.html`を頂点とする階層**（現存する全17トップレベル画面＋assessment系9＋print系16＋patient系5＋partials系3＋admin系4を確認）:

```
rtms_app/templates/rtms_app/base.html （共通レイアウト・ナビ・フッター・56行のインラインテーマCSS）
├── dashboard.html / patient_list.html / patient_add.html / patient_basic_edit.html
├── admin_backup.html / audit_logs.html / skip_list.html / calendar_month.html
├── admission_procedure.html / mapping_add.html / treatment_add.html
├── patient_first_visit.html / patient_clinical_path.html / patient_summary.html
├── questionnaire_edit.html / assessment_add.html
└── assessment/
    ├── baseline.html / hub.html / week3.html / week4.html / week6.html
    └── scale_form_base.html
        └── scales/hamd.html, scales/placeholder.html （2段階extends）

rtms_app/templates/rtms_app/patient/base_patient.html （患者ポータル専用・独立ツリー）
├── login.html / portal.html / instrument.html / review.html

rtms_app/templates/rtms_app/print/_print_base.html （印刷: 単一カラム）
├── bundle.html / path.html

rtms_app/templates/rtms_app/print/_print_base_twocolumn.html （印刷: 2カラム、Bootstrap/FontAwesomeをCDNから直接読み込み）
├── admission_summary.html / discharge_summary.html / referral.html
├── side_effect_check.html / suitability_questionnaire.html

print/adverse_event_report.html, print/adverse_event_report_db.html
└── どちらもbaseをextendsしない独立フルHTML文書

print/calendar_month.html → base.html をextends（印刷名だが実体は画面表示）

【管理画面系（Django admin base_site.html/index.htmlをoverride）】
rtms_app/templates/admin/base_site.html → admin/base_site.html
├── admin_backup.html / research_export.html
rtms_app/templates/admin/rtms_index.html → admin/index.html（{{ block.super }}使用）
templates/admin/login.html → rtms_app/base.html（Django admin標準ではなくアプリ側baseを継承する独自ログイン画面）
```

**パーシャル実使用状況**:
- `partials/form_header.html`: `assessment/_form.html`のみが実際にinclude。
- `partials/patient_inline_bar.html`: **11テンプレート**が実際にinclude（`admission_procedure.html`, `assessment_add.html`, `assessment/_form.html`, `assessment/hub.html`, `assessment/scale_form_base.html`, `mapping_add.html`, `patient_clinical_path.html`, `patient_first_visit.html`, `patient_summary.html`, `skip_list.html`, `treatment_add.html`）。
- `partials/patient_nav.html`: `patient_inline_bar.html`からのみinclude（間接的に11テンプレートへ波及）。

---

## H. Template間の依存関係

- **`page-exit-menu`（新式）を使う4画面**: `patient_first_visit.html`, `patient_clinical_path.html`, `patient_summary.html`, `treatment_add.html`。
- **`fab-stack`（旧式）を使う7画面**: `assessment_add.html`, `assessment/_form.html`, `assessment/hub.html`, `assessment/scale_form_base.html`, `patient_add.html`, `questionnaire_edit.html`, `mapping_add.html`（CSS定義のみで実際のHTML使用は要目視確認）。
- **`patient_inline_bar.html`共有ヘッダー**を使う11画面のうち、`questionnaire_edit.html`だけは**独自の簡易ヘッダー**を個別実装しており仲間外れ（統一漏れの可能性）。
- **印刷テンプレートは2つの独立したbaseに分裂**（`_print_base.html`＝単一カラム／`_print_base_twocolumn.html`＝2カラム＋CDN読み込みのBootstrap/FontAwesome）。さらに`adverse_event_report.html`/`adverse_event_report_db.html`はどちらのbaseも使わない完全独立HTML。
- **`_hamd17_trend_table.html`**: 今回の直接grep（テンプレート・Pythonの両方）で**プロジェクト全体から参照ゼロを再確認**。前回報告の「要確認」を「未使用確定」に格上げできる。
- **`_hamd_trend_print_compact.html`**: `discharge_summary.html`と`referral.html`から実際にincludeされている（実使用）。`_hamd17_trend_table.html`とは役割が重複するが、後者が死んでいるため実質的な重複問題は今は発生していない。
- **静的ファイル参照の影響範囲**: `{% static %}`参照は24テンプレートに46箇所。`rtms_app/`名前空間配下の再配置は16テンプレート、`css/`直下（rtms_theme.css等）の再配置は4テンプレートに影響。

---

## I. 共通UI候補（優先度A/B/C）

| UI要素 | 現状 | 優先度 | 理由 |
|---|---|---|---|
| ページ下部アクションメニュー（保存/戻る/印刷） | `page-exit-menu`(4画面) と `fab-stack`(7画面)に分裂 | **B（将来的に共通化）** | 見た目・挙動の統一は価値があるが、7画面のUI変更を伴うため今回のスコープ外。第3段階でCSSだけ触るなら影響小、HTML統一は別途計画要。 |
| `.q-badge`（採点番号バッジ） | 4種以上のサイズ・色が併存 | **A（すぐ共通化すべき）** | 見た目のズレが実際に発生している可能性が高く、CSS変数化のみで対応可能・リスクが低い。 |
| `.hamd-row` / `.sticky-scorebar` / `.assessment-cell` | インラインでの再定義が5〜7箇所 | **A** | CSSクラスとして1箇所に集約するだけで見た目は変わらない（純粋なコード整理）。 |
| 患者情報ヘッダー | `patient_inline_bar.html`が11画面で共有済み、`questionnaire_edit.html`のみ独自 | **B** | `questionnaire_edit.html`を合わせるかは仕様判断が必要（デザイン差が意図的か要確認）。 |
| モーダルのz-index | 2050 vs 2060 で不統一 | **A** | 値を1つに揃えるだけ。ただし変更後は全モーダルの重なり順を目視確認。 |
| HAM-Dキーボード操作・モーダル送信ロジック | JSが2〜3箇所に重複 | **C（現状のままが安全）** | 業務ロジックに直結し、動作の可視化・回帰テストの準備が整うまでは触らない方が安全。 |
| 数値ステッパー（MT%等） | mapping_add.html独自実装 | **C** | 治療パラメータに関わるため、単独では触らない。 |
| `.card-header-accent` | 既に共通化済み | **C（現状維持）** | 既に単一ソースであり触る必要なし。 |
| `.badge-rtms`（ステータスバッジ） | 既に共通化済み（5variant） | **C** | 同上。 |
| 印刷ページのtoolbar/base | `_print_base.html` / `_print_base_twocolumn.html`に分裂 | **B** | 統合の価値はあるが、印刷レイアウト・WeasyPrint出力への影響が大きく、慎重な検証が必要。 |

---

## J. 移動・変更時のリスク（単純移動で壊れる可能性が高いもの）

| 対象 | リスク理由 |
|---|---|
| `rtms_app/static/rtms_app/*.js` / `*.css` | 24テンプレート・46箇所の`{% static %}`直書きパスに依存。移動する場合は全参照を機械的に一括置換する必要がある。 |
| `print/_print_base.html`, `print/_print_base_twocolumn.html` | 7つの印刷テンプレートがそれぞれ`{% extends %}`しており、WeasyPrintのレンダリング・A4レイアウトに直結。統合や移動は印刷物の見た目を必ず崩す。 |
| `print/_print_toolbar.html` | html2pdf.js呼び出し・戻るURL・印刷ボタンの制御ロジックを内包。単純な移動でも`{% include %}`パスの追従漏れがあれば全印刷画面で壊れる。 |
| `assessment/scales/hamd.html` / `hamd_modal.html` | `views.py`の`assessment_scale_form`が文字列でテンプレートパスを直接指定（`'rtms_app/assessment/scales/hamd_modal.html'`等）。ファイル名・パスの変更はPython側の修正と完全同期が必要。 |
| `patient/` 配下（患者ポータル） | `views_patient.py`・`patient_urls.py`・`surveys/definitions.py`と密結合。移動時は自己記入式検査のスコア計算契約（instrumentコード）を壊さないよう特に注意。 |
| `rtms_app/templates/admin/` 配下 | Django標準admin（`admin/base_site.html`, `admin/index.html`）のテンプレート探索パス規約に依存。ディレクトリ名`admin/`は移動不可（Djangoの規約上固定）。 |
| `rtms_app/static/css/admin_custom.css`, `rtms_app/static/img/logo.jpg` | 名前空間なし（`rtms_app/`プレフィックスなし）でapp static配下に配置されているため、他アプリや将来の`static/css/`直下ファイルと**衝突する可能性がある**。移動そのものは低リスクだが、移動先を`rtms_app/static/rtms_app/`配下に正規化するなら参照更新が必須。 |
| `partials/patient_inline_bar.html` / `patient_nav.html` | 11テンプレートが依存。中身を変えずにファイルを移動するだけでも11箇所の`{% include %}`パス更新が必要。 |
| `wizard.js`（1094行） | `treatment_add.html`のグローバル関数（`getTodayTrainSeconds()`等）・`sideEffectWidgetInstance`・`bootstrap.Modal`と密結合。単独でのファイル分割は状態管理の破綻リスクが高い。 |

---

## K. 削除候補（今回は削除しない・第3段階の候補としてのみ記載）

| 対象 | 根拠 |
|---|---|
| `rtms_app/static/css/admin_custom.css` | 全テンプレートgrepで参照ゼロ（今回新たに確認）。 |
| `rtms_app/templates/rtms_app/print/_hamd17_trend_table.html` | テンプレート・Python両方で参照ゼロを直接grepで再確認（前回「要確認」だったものを確定）。 |
| `rtms_app/static/rtms_app/page_actions.css`内の`.page-document-actions`ルール | クラス自体が未使用（ファイル自体は`.page-exit-menu`のため削除しない。ルールのみ削除候補）。 |
| `static/rtms_app/`（空ディレクトリ、`.DS_Store`のみ） | 中身が存在しない迷子ディレクトリ。 |
| `rtms_app/static/app.css`内の`.app-page-title` / `.app-section` / `.floating-action-menu` | いずれも対応するHTML側の使用が見つからず（前回報告と一致）。 |

**削除しない（誤判定防止のため明記）**: `rtms_app/static/css/`自体、`rtms_app/static/img/logo.jpg`は現役（base.htmlが参照）。

---

## L. 最終的な推奨ディレクトリ構造（現状ファイルの移動先マッピング）

```
static/
├── css/
│   ├── core/            # rtms_theme.css, box_style.css, app.css（共通基盤・base.htmlが読む）
│   ├── components/       # page_actions.css, floating.css（複数画面で使う部品）
│   ├── pages/            # mapping.css, patient.css（単一画面専用）
│   └── print/            # print.css, print_toolbar.css, print_a4.css, calendar.css（印刷/カレンダー専用）
├── js/
│   ├── core/              # floating.js（共通ユーティリティ）
│   ├── components/        # hamd_widget.js, side_effect_widget_v2.js（部品的ウィジェット）
│   └── pages/             # wizard.js, patient_surveys.js, calendar_focus.js（特定画面専用）
└── img/                    # logo.jpg（現状維持）

templates/rtms_app/
├── base.html
├── components/            # partials/を改名・統合（patient_inline_bar.html, patient_nav.html, form_header.html）
├── dashboard/              # dashboard.html
├── patients/               # patient_list.html, patient_add.html, patient_basic_edit.html
├── first_visit/            # patient_first_visit.html
├── mt/                     # mapping_add.html
├── treatment/              # treatment_add.html, skip_list.html
├── assessment/              # 既存のまま（内部構造は健全）
├── clinical_path/           # patient_clinical_path.html
├── discharge/               # patient_summary.html
├── print/                   # 既存のまま（_print_base.html系はそのまま維持）
└── patient/                  # 既存のまま（患者ポータル）
```

※ 現状の`rtms_app/static/css/`・`rtms_app/static/img/`（名前空間なし）は将来的に`rtms_app/static/rtms_app/`配下へ統合し、Djangoのapp static命名規約に揃えることを推奨（ただし`admin_custom.css`は先に未使用確認→削除が先）。

---

## M. 第3段階で実施すべき作業（安全な順序）

1. **確定済み未使用ファイルの追加削除**（K節）: `admin_custom.css`, `_hamd17_trend_table.html`, `static/rtms_app/`空ディレクトリ, `app.css`内の3つの未使用ルール, `page_actions.css`内の`.page-document-actions`ルール。削除前に必ず再grepし、削除後は`manage.py check` + `manage.py test rtms_app` + 主要画面の目視確認。
2. **`.q-badge`のCSS変数化**（優先度A）: 4箇所のインライン定義を1つの共通クラス＋modifier（例: `.q-badge--sm/--lg/--urgent`）に統合。見た目を変えないよう、まず現状の見た目を画面ごとにスクリーンショットで記録してから着手。
3. **`.hamd-row`/`.sticky-scorebar`/`.assessment-cell`の共通CSS化**（優先度A）: インライン`<style>`を`rtms_theme.css`または新規`components.css`に集約。
4. **モーダルz-index値の統一**（優先度A、ただし要全モーダル目視確認）: `treatment_add.html`の2060を他と合わせて2050に統一するか、意図的な差か仕様確認してから実施。
5. **静的ファイルの物理移動（第4段階以降が妥当）**: L節の構造へ移動する場合は、1ファイルずつ「移動→46箇所中の該当`{% static %}`参照を更新→`manage.py check`→該当画面を目視→コミット」を繰り返す。一括移動は禁止。

---

## N. 第3段階で絶対に触らない方がよい部分

- **`wizard.js`（1094行）とtreatment_add.htmlのグローバル関数連携**: 状態管理が密結合しており、分割・移動は治療記録の安全性に直結するリスクが高い。
- **`print/_print_base.html` と `print/_print_base_twocolumn.html` の統合**: WeasyPrint出力・A4レイアウトに直結。2つのbaseを1つに統合する提案自体は将来検討可だが、今回は着手しない。
- **HAM-Dキーボード操作・モーダル送信ロジックの重複解消**（F節）: 業務ロジックに直結し、3箇所の微妙な差異を安易に1本化すると特定画面でのみ発生する不具合を見逃すリスクがある。
- **`views.py`の`assessment_scale_form`が文字列で直接指定するテンプレートパス**（`hamd_modal.html`等）: テンプレートのリネーム・移動は必ずPython側の文字列と同時に変更する必要があり、単独では絶対に行わない。
- **`patient_inline_bar.html`/`patient_nav.html`の中身変更**: 11画面に影響するため、CSSクラス整理（I節A項目）とは切り離し、中身のHTML構造自体は今回のスコープでは変更しない。
- **`questionnaire_edit.html`の独自ヘッダー**: 他画面と統一すべきかは意匠上の意図が不明なため、第3段階では判断保留（削除・統一をしない）。
- **モーダルのz-index差分（2050 vs 2060）**: 「揃えるべき」と分析はしたが、実際に揃える作業は全モーダルの重なり合いパターンを目視確認できる体制が整うまで保留するのが安全。

---

## まとめ

本書作成にあたり、コードは一切変更していない。実施したのはCSS 11ファイル・外部JS 6ファイル・テンプレート約55ファイルの参照関係の洗い出しのみである。
