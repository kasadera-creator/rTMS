# UI 統一化プロジェクト レポート

## 実施日
2026年1月14日

## A. テンプレート棚卸し結果

### 全体ページ（患者に紐づかない）- 対象修正ページ

| ページ名 | テンプレ | URL | 主要UI構成 | 修正優先度 |
|---------|---------|-----|----------|-----------|
| ダッシュボード | dashboard.html | /app/dashboard/ | タスク区分カード（6種） | ⭐⭐⭐ |
| 月間カレンダー | calendar_month.html | /app/calendar/ | 年月選択 + イベント表示 | ⭐⭐⭐ |
| 患者一覧 | patient_list.html | /app/patient/list/ | 検索/フィルタ + テーブル | ⭐⭐⭐ |
| CSVエクスポート | export_research_csv.html | /app/export/ | フォーム + ボタン | ⭐⭐ |
| 変更履歴 | audit_logs.html | /app/patient/<id>/audit/ | テーブル表示 | ⭐ |

### 患者個別ページ（既に修正済or対象外）

| ページ名 | テンプレ | 状態 | 備考 |
|---------|---------|------|------|
| 初診・基本情報 | patient_first_visit.html | ✅ カード化済 | 患者ページ |
| 患者追加 | patient_add.html | 個別UI | 入力フォーム専用 |
| 基本情報編集 | patient_basic_edit.html | 個別UI | 入力フォーム専用 |
| クリニカルパス | patient_clinical_path.html | ✅ カード化済 | 患者ページ |
| 治療実施 | treatment_add.html | ✅ カード化済 | 患者ページ |
| 位置決め | mapping_add.html | ✅ カード化済 | 患者ページ |
| 評価尺度 | assessment_add.html | ✅ カード化済 | 患者ページ |
| 入院手続き | admission_procedure.html | ✅ カード化済 | 患者ページ |
| 治療経過・退院準備 | patient_summary.html | ✅ カード化済 | 患者ページ |
| 適正問診票 | questionnaire_edit.html | 個別UI | 入力フォーム専用 |
| 副作用報告 | adverse_event_report_form.html | 個別UI | 入力フォーム専用 |
| 説明同意書 | consent_latest.html | 個別UI | PDF表示 |
| 管理機能 | admin_backup.html | 管理UI | 専用設定 |

---

## B. 共通CSS設計（案）

予定位置: `rtms_app/static/rtms_app/app.css`

```css
/* === カード統一クラス === */
.app-card {
  border: 0;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(25, 38, 54, 0.06);
  background: #fff;
  margin-bottom: 1rem;
}

.app-card__header {
  padding: 0.75rem 1rem;
  border-left: 6px solid var(--card-accent, #7b1fa2);
  background: var(--card-bg, #f3e5f5);
  color: var(--card-accent, #7b1fa2);
  font-weight: 700;
  border-bottom: 1px solid rgba(0, 0, 0, 0.04);
}

.app-card__body {
  padding: 1rem;
}

/* === ページレイアウト === */
.app-page-title {
  font-size: 1.5rem;
  font-weight: 700;
  margin-bottom: 1.5rem;
  color: #333;
}

.app-section {
  margin-bottom: 2rem;
}

/* === 色管理（ページ別） === */
[data-theme="dashboard"] { --card-accent: #388e3c; --card-bg: #e8f5e9; }
[data-theme="calendar"] { --card-accent: #1976d2; --card-bg: #e3f2fd; }
[data-theme="patient-list"] { --card-accent: #546e7a; --card-bg: #eceff1; }
```

---

## C. トップバー改修

### 対象: `base.html` navbar

**修正内容:**
- flex 整理: `flex-wrap: nowrap`
- 左側（ブランド）: `text-overflow: ellipsis` で收縮
- 右側: アイコン優先、テキスト非表示 (md以下)
- body padding-top: `--topbar-height` 固定

---

## D. 修正予定ファイル一覧

### 共通ファイル
- [ ] `rtms_app/static/rtms_app/app.css` (新規作成)
- [ ] `rtms_app/templates/rtms_app/base.html`

### 全体ページテンプレ
- [ ] `rtms_app/templates/rtms_app/dashboard.html`
- [ ] `rtms_app/templates/rtms_app/calendar_month.html`
- [ ] `rtms_app/templates/rtms_app/patient_list.html`

### 補助ページ（優先度低）
- [ ] `rtms_app/templates/rtms_app/export_research_csv.html`
- [ ] `rtms_app/templates/rtms_app/audit_logs.html`

---

## E. 進行状況

- [ ] テンプレート棚卸し完了
- [ ] 共通CSS作成完了
- [ ] base.html 改修完了
- [ ] dashboard.html 改修完了
- [ ] calendar_month.html 改修完了
- [ ] patient_list.html 改修完了
- [ ] 検証（狭幅/iPad/floating干渉）完了
- [ ] レポート最終化完了

---

## F. 検証チェックリスト

- [ ] 全体ページ（dashboard/month/patient list）でカードヘッダ統一
- [ ] 余白（padding）が統一（上下1rem）
- [ ] トップバー幅768px以下でも2行にならない
- [ ] floating menu とトップバーが重ならない
- [ ] iPad 幅（834px）で見栄え確認

---

*更新日: 2026-01-14*
