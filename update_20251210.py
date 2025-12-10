import os
import zipfile

# すべてのHTMLテンプレートの定義
templates = {
    # 1. 業務ダッシュボード
    "dashboard.html": """<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>笠寺精治寮病院 rTMSダッシュボード</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Noto Sans JP', sans-serif; background-color: #f0f2f5; }
        .navbar-brand { font-weight: bold; font-size: 1.2rem; }
        .card-task { border: none; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); transition: transform 0.2s; overflow: hidden; height: 100%; }
        .card-task:hover { transform: translateY(-3px); box-shadow: 0 10px 20px rgba(0,0,0,0.1); }
        .task-header { padding: 15px; color: white; display: flex; justify-content: space-between; align-items: center; font-weight: bold; }
        .bg-gradient-blue { background: linear-gradient(135deg, #0d6efd, #0a58ca); }
        .bg-gradient-orange { background: linear-gradient(135deg, #fd7e14, #e67700); }
        .bg-gradient-green { background: linear-gradient(135deg, #198754, #146c43); }
        .bg-gradient-red { background: linear-gradient(135deg, #dc3545, #b02a37); }
        .list-group-item { border: none; border-bottom: 1px solid #f0f0f0; padding: 12px 15px; }
        .list-group-item:hover { background-color: #f8f9fa; }
        .list-group-item a { text-decoration: none; color: inherit; display: block; }
        .status-badge { font-size: 0.75rem; padding: 5px 10px; border-radius: 20px; min-width: 70px; text-align: center; display: inline-block; }
        .status-success, .status-done, .status-手続済, .status-実施済, .status-登録済 { background-color: #e6f9ed; color: #198754; border: 1px solid #c3e6cb; }
        .status-warning, .status-要対応, .status-要手続 { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba; }
        .status-danger, .status-実施未 { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .status-info, .status-退院準備 { background-color: #cff4fc; color: #055160; border: 1px solid #b6effb; }
        .date-nav { background: white; border-radius: 10px; padding: 10px 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); margin-bottom: 25px; }
    </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark sticky-top shadow-sm">
    <div class="container-fluid px-4">
        <a class="navbar-brand" href="#"><i class="fas fa-hospital-alt me-2"></i>笠寺精治寮病院 rTMSダッシュボード</a>
        <div class="d-flex align-items-center">
            <span class="text-white me-3 small"><i class="fas fa-user me-1"></i> {% if user.last_name %}{{ user.last_name }} {{ user.first_name }}{% else %}{{ user.username }}{% endif %}</span>
            {% if user.is_superuser %}
            <a href="/admin/" class="btn btn-sm btn-outline-light me-2"><i class="fas fa-cog"></i> 設定</a>
            {% endif %}
            <a href="/admin/logout/" class="btn btn-sm btn-secondary"><i class="fas fa-sign-out-alt"></i></a>
        </div>
    </div>
</nav>
<div class="container py-4">
    <div class="date-nav d-flex justify-content-between align-items-center">
        <div class="d-flex align-items-center">
            <a href="?date={{ prev_day|date:'Y-m-d' }}" class="btn btn-outline-secondary btn-sm me-2"><i class="fas fa-chevron-left"></i> 前日</a>
            <input type="date" class="form-control form-control-sm d-inline-block w-auto border-0 bg-light fw-bold" value="{{ today|date:'Y-m-d' }}" onchange="location.href='?date='+this.value">
            <a href="?date={{ next_day|date:'Y-m-d' }}" class="btn btn-outline-secondary btn-sm ms-2">翌日 <i class="fas fa-chevron-right"></i></a>
        </div>
        <div>
            <a href="{% url 'patient_list' %}" class="btn btn-outline-primary btn-sm fw-bold"><i class="fas fa-users me-1"></i> 患者一覧・検索</a>
        </div>
    </div>
    <div class="row g-4">
        <div class="col-md-6 col-lg-3">
            <div class="card-task h-100 bg-white">
                <div class="task-header bg-gradient-blue">
                    <span><i class="fas fa-user-md me-2"></i>① 初診・入院</span>
                    <span class="badge bg-white text-primary rounded-pill">{{ new_patients|length|add:admissions|length }}</span>
                </div>
                <div class="p-2 border-bottom text-center bg-light">
                    <a href="{% url 'patient_add' %}" class="btn btn-primary btn-sm w-100 shadow-sm fw-bold"><i class="fas fa-plus-circle"></i> 新規患者登録</a>
                </div>
                <div class="list-group list-group-flush">
                    {% for item in new_patients %}
                    <div class="list-group-item">
                        <a href="{% url 'patient_first_visit' item.obj.id %}">
                            <div class="d-flex justify-content-between align-items-center mb-1">
                                <span class="fw-bold text-primary">{{ item.obj.name }}</span>
                                <span class="status-badge status-done">登録済</span>
                            </div>
                            <small class="text-muted">ID: {{ item.obj.card_id }} (初診)</small>
                        </a>
                    </div>
                    {% endfor %}
                    {% for item in admissions %}
                    <div class="list-group-item">
                        <a href="{% url 'admission_procedure' item.obj.id %}">
                            <div class="d-flex justify-content-between align-items-center mb-1">
                                <span class="fw-bold">{{ item.obj.name }}</span>
                                <span class="status-badge status-{{ item.color }}">{{ item.status }}</span>
                            </div>
                            <small class="text-muted">ID: {{ item.obj.card_id }} (入院)</small>
                        </a>
                    </div>
                    {% endfor %}
                    {% if not new_patients and not admissions %}
                    <div class="text-center py-5 text-muted small">予定なし</div>
                    {% endif %}
                </div>
            </div>
        </div>
        <div class="col-md-6 col-lg-3">
            <div class="card-task h-100 bg-white">
                <div class="task-header bg-gradient-orange">
                    <span><i class="fas fa-crosshairs me-2"></i>② 位置決め</span>
                    <span class="badge bg-white text-dark rounded-pill">{{ mappings|length }}</span>
                </div>
                <div class="list-group list-group-flush">
                    {% for item in mappings %}
                    <div class="list-group-item">
                        <a href="{% url 'mapping_add' item.obj.id %}?date={{ today|date:'Y-m-d' }}">
                            <div class="d-flex justify-content-between align-items-center mb-1">
                                <span class="fw-bold">{{ item.obj.name }}</span>
                                <span class="status-badge status-{{ item.color }}">{{ item.status }}</span>
                            </div>
                            <small class="text-muted">MT測定</small>
                        </a>
                    </div>
                    {% empty %}
                    <div class="text-center py-5 text-muted small">予定なし</div>
                    {% endfor %}
                </div>
            </div>
        </div>
        <div class="col-md-6 col-lg-3">
            <div class="card-task h-100 bg-white">
                <div class="task-header bg-gradient-green">
                    <span><i class="fas fa-bolt me-2"></i>③ 治療実施</span>
                    <span class="badge bg-white text-success rounded-pill">{{ treatments|length }}</span>
                </div>
                <div class="list-group list-group-flush">
                    {% for item in treatments %}
                    <div class="list-group-item {% if item.status == '実施済' %}bg-light{% endif %}">
                        <a href="{% url 'treatment_add' item.obj.id %}?date={{ today|date:'Y-m-d' }}">
                            <div class="d-flex justify-content-between align-items-center mb-1">
                                <span class="fw-bold">{{ item.obj.name }}</span>
                                <span class="status-badge status-{{ item.color }}">{{ item.status }}</span>
                            </div>
                            <div class="d-flex justify-content-between align-items-center mt-1">
                                <small class="text-muted fw-bold">{{ item.note }}</small>
                                {% if item.is_discharge %}
                                    <span class="badge bg-info text-dark"><i class="fas fa-flag-checkered"></i> 退院準備</span>
                                {% endif %}
                            </div>
                        </a>
                        {% if item.is_discharge %}
                        <div class="mt-2 text-center">
                            <a href="{% url 'patient_summary' item.obj.id %}" class="btn btn-outline-info btn-sm w-100">サマリー作成へ</a>
                        </div>
                        {% endif %}
                    </div>
                    {% empty %}
                    <div class="text-center py-5 text-muted small">本日の治療予定なし</div>
                    {% endfor %}
                </div>
            </div>
        </div>
        <div class="col-md-6 col-lg-3">
            <div class="card-task h-100 bg-white">
                <div class="task-header bg-gradient-red">
                    <span><i class="fas fa-clipboard-list me-2"></i>④ 状態評価</span>
                    <span class="badge bg-white text-danger rounded-pill">{{ assessments_due|length }}</span>
                </div>
                <div class="list-group list-group-flush">
                    {% for item in assessments_due %}
                    <div class="list-group-item">
                        <a href="{% url 'assessment_add' item.obj.id %}?date={{ today|date:'Y-m-d' }}">
                            <div class="d-flex justify-content-between align-items-center mb-1">
                                <span class="fw-bold">{{ item.obj.name }}</span>
                                <span class="status-badge status-{{ item.color }}">{{ item.status }}</span>
                            </div>
                            <small class="text-danger fw-bold"><i class="fas fa-bell"></i> {{ item.reason }}</small>
                        </a>
                    </div>
                    {% empty %}
                    <div class="text-center py-5 text-muted small">評価予定なし</div>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>
    {% if user.is_superuser %}
    <div class="mt-5 text-end pt-3 border-top">
        <small class="text-muted me-2">管理者メニュー:</small>
        <a href="{% url 'export_csv' %}" class="btn btn-sm btn-outline-secondary me-1"><i class="fas fa-file-csv"></i> CSV出力</a>
        <a href="{% url 'download_db' %}" class="btn btn-sm btn-outline-secondary"><i class="fas fa-database"></i> DBバックアップ</a>
    </div>
    {% endif %}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>""",

    # 2. 入院手続き (★エラーの原因だったファイル)
    "admission_procedure.html": """{% extends "admin/base_site.html" %}
{% block extrastyle %}<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">{% endblock %}
{% block content %}
<div class="container py-5">
    <div class="card shadow-sm col-md-8 mx-auto">
        <div class="card-header bg-primary text-white"><h4 class="mb-0 fw-bold">入院手続き: {{ patient.name }} 殿</h4></div>
        <div class="card-body p-5">
            <form method="post">
                {% csrf_token %}
                <h5 class="mb-4 border-bottom pb-2">入院形態の選択</h5>
                <div class="mb-5">
                    {% for radio in form.admission_type %}
                    <div class="form-check mb-3">
                        <span class="form-check-input" style="transform: scale(1.5); margin-right: 10px;">{{ radio.tag }}</span>
                        <label class="form-check-label fs-5" for="{{ radio.id_for_label }}">{{ radio.choice_label }}</label>
                    </div>
                    {% endfor %}
                </div>
                <div class="alert alert-info"><i class="fas fa-info-circle"></i> 入院時オリエンテーション、持ち物確認、同意書記入が完了したら、以下のボタンを押してください。</div>
                <div class="text-center mt-4">
                    <button type="submit" class="btn btn-success btn-lg px-5 fw-bold" name="is_admission_procedure_done" value="True"><i class="fas fa-check"></i> 入院対応を終えました</button>
                    <br><a href="{% url 'dashboard' %}" class="btn btn-link text-secondary mt-3">キャンセルして戻る</a>
                </div>
            </form>
        </div>
    </div>
</div>
{% endblock %}""",

    # 3. 患者一覧
    "patient_list.html": """{% extends "admin/base_site.html" %}
{% block extrastyle %}
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
{% endblock %}
{% block content %}
<div class="container py-4">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h3><i class="fas fa-users"></i> 患者一覧</h3>
        <a href="{% url 'dashboard' %}" class="btn btn-secondary">ダッシュボードへ戻る</a>
    </div>
    <div class="card shadow-sm">
        <div class="card-body p-0">
            <table class="table table-hover mb-0">
                <thead class="table-light"><tr><th>ID</th><th>氏名</th><th>生年月日 (年齢)</th><th>主治医</th><th>診断名</th><th class="text-center">操作</th></tr></thead>
                <tbody>
                    {% for patient in patients %}
                    <tr>
                        <td class="align-middle fw-bold">{{ patient.card_id }}</td>
                        <td class="align-middle">{{ patient.name }}</td>
                        <td class="align-middle">{{ patient.birth_date }} ({{ patient.age }}歳)</td>
                        <td class="align-middle">{% if patient.attending_physician %}{{ patient.attending_physician.last_name }} 先生{% else %}-{% endif %}</td>
                        <td class="align-middle">{{ patient.diagnosis }}</td>
                        <td class="text-center">
                            <a href="{% url 'patient_summary' patient.id %}" class="btn btn-sm btn-outline-info"><i class="fas fa-file-medical-alt"></i> サマリー</a>
                            <a href="{% url 'patient_first_visit' patient.id %}" class="btn btn-sm btn-outline-secondary"><i class="fas fa-edit"></i> 詳細</a>
                        </td>
                    </tr>
                    {% empty %}
                    <tr><td colspan="6" class="text-center py-4 text-muted">登録患者はいません</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
{% endblock %}""",

    # 4. 初診・基本情報
    "patient_first_visit.html": """{% extends "admin/base_site.html" %}
{% block extrastyle %}
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
    .form-section { background-color: #fff; padding: 25px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
    .section-title { border-left: 5px solid #0d6efd; padding-left: 10px; font-weight: bold; margin-bottom: 20px; color: #333; font-size: 1.2rem; }
    .q-table { width: 100%; border-collapse: collapse; }
    .q-table th { background-color: #f8f9fa; padding: 10px; border-bottom: 2px solid #dee2e6; }
    .q-table td { padding: 8px 10px; border-bottom: 1px solid #dee2e6; vertical-align: middle; }
    .q-header-row { background-color: #e9ecef; font-weight: bold; }
    @media print {
        @page { size: A4; margin: 15mm; }
        body * { visibility: hidden; }
        .printable-area, .printable-area * { visibility: visible; }
        .printable-area { position: absolute; left: 0; top: 0; width: 100%; font-family: "Hiragino Mincho ProN", "Yu Mincho", serif; color: #000; }
        .no-print, .btn, header, nav, footer { display: none !important; }
        .print-header { text-align: center; margin-bottom: 20px; border-bottom: 2px solid #000; padding-bottom: 5px; }
        .print-header h2 { font-size: 22px; margin: 0; }
        .print-row { display: flex; justify-content: space-between; margin-bottom: 10px; font-size: 12px; }
        .q-table { font-size: 11px; border: 1px solid #000; }
        .q-table th, .q-table td { border: 1px solid #000; padding: 4px; }
        .q-header-row { background-color: #ddd !important; -webkit-print-color-adjust: exact; }
        textarea, input[type="text"] { border: none; resize: none; overflow: visible; font-family: inherit; }
        .signature-section { margin-top: 30px; display: flex; justify-content: flex-end; }
        .signature-box { width: 50%; }
        .signature-line { border-bottom: 1px solid #000; margin-top: 20px; }
    }
</style>
{% endblock %}
{% block content %}
<div class="container py-4">
    <div class="d-flex justify-content-between align-items-center mb-3 no-print">
        <h3 class="fw-bold"><i class="fas fa-file-medical"></i> 初診・基本情報入力</h3>
        <div class="btn-group">
            <button type="button" class="btn btn-outline-success" onclick="printDiv('questionnaire')"><i class="fas fa-print"></i> 質問票印刷</button>
            <button type="button" class="btn btn-outline-primary" onclick="printDiv('medical-record')"><i class="fas fa-print"></i> カルテ印刷</button>
            <a href="{% url 'dashboard' %}" class="btn btn-secondary">戻る</a>
        </div>
    </div>
    <form method="post" id="mainForm">
        {% csrf_token %}
        {% if form.errors %}<div class="alert alert-danger no-print">入力エラーがあります</div>{% endif %}
        <div id="medical-record" class="printable-area form-section">
            <h5 class="section-title">基本情報・診療録</h5>
            <div class="row g-3 mb-4">
                <div class="col-md-3 col-6"><label class="form-label fw-bold">カルテID</label>{{ form.card_id }}</div>
                <div class="col-md-3 col-6"><label class="form-label fw-bold">氏名</label>{{ form.name }}</div>
                <div class="col-md-3 col-6"><label class="form-label fw-bold">生年月日</label>{{ form.birth_date }}</div>
                <div class="col-md-3 col-6"><label class="form-label fw-bold">性別</label>{{ form.gender }}</div>
                <div class="col-md-6"><label class="form-label fw-bold">紹介元</label>{{ form.referral_source }}</div>
                <div class="col-12"><label class="form-label fw-bold">主訴・診断名</label>{{ form.diagnosis }}</div>
            </div>
            <div class="row g-3">
                <div class="col-12"><label class="form-label fw-bold">生活歴</label><textarea name="{{ form.life_history.name }}" class="form-control" rows="4">{{ form.life_history.value|default_if_none:"" }}</textarea></div>
                <div class="col-12"><label class="form-label fw-bold">既往歴</label><textarea name="{{ form.past_history.name }}" class="form-control" rows="4">{{ form.past_history.value|default_if_none:"" }}</textarea></div>
                <div class="col-12"><label class="form-label fw-bold">現病歴</label><textarea name="{{ form.present_illness.name }}" class="form-control" rows="10">{{ form.present_illness.value|default_if_none:"" }}</textarea></div>
                <div class="col-12"><label class="form-label fw-bold">薬剤治療歴</label><textarea name="{{ form.medication_history.name }}" class="form-control" rows="4">{{ form.medication_history.value|default_if_none:"" }}</textarea></div>
            </div>
        </div>
        <div class="form-section no-print">
            <h5 class="section-title">スケジュール・担当設定</h5>
            <div class="row g-3">
                <div class="col-md-4"><label class="form-label fw-bold text-primary">入院予定日</label>{{ form.admission_date }}</div>
                <div class="col-md-4"><label class="form-label fw-bold text-warning">初回位置決め日</label>{{ form.mapping_date }}</div>
                <div class="col-md-4"><label class="form-label fw-bold text-success">初回治療日</label>{{ form.first_treatment_date }}</div>
                <div class="col-md-6 mt-3"><label class="form-label fw-bold">担当医</label>{{ form.attending_physician }}</div>
            </div>
        </div>
        <div id="questionnaire" class="printable-area form-section">
            <div class="print-header d-none d-print-block"><h2>rTMS 適正に関する質問票</h2><p style="text-align: right; font-size: 10px;">笠寺精治寮病院</p></div>
            <div class="print-row d-none d-print-flex">
                <div style="width: 30%;">ID: <strong>{{ patient.card_id }}</strong></div>
                <div style="width: 40%;">氏名: <strong>{{ patient.name }} 殿</strong></div>
                <div style="width: 30%; text-align: right;">生年月日: {{ patient.birth_date|date:"Y年n月j日" }}</div>
            </div>
            <table class="q-table">
                <thead class="table-light"><tr><th style="text-align: left;">質問項目</th><th width="60" class="text-center">はい</th><th width="60" class="text-center">いいえ</th></tr></thead>
                <tbody>
                    <tr class="q-header-row"><td colspan="3">これまでに、以下のことがありましたか？</td></tr>
                    <tr><td>rTMS実施経験（治験、研究を問わない）</td><td class="text-center"><input type="radio" name="q_past_rtms" value="はい"></td><td class="text-center"><input type="radio" name="q_past_rtms" value="いいえ" checked></td></tr>
                    <tr><td>rTMSのあとに副作用などの不快な経験</td><td class="text-center"><input type="radio" name="q_past_side_effect" value="はい"></td><td class="text-center"><input type="radio" name="q_past_side_effect" value="いいえ" checked></td></tr>
                    <tr><td>電気けいれん療法（副作用の有無など）</td><td class="text-center"><input type="radio" name="q_past_ect" value="はい"></td><td class="text-center"><input type="radio" name="q_past_ect" value="いいえ" checked></td></tr>
                    <tr><td>けいれん発作（てんかんの診断の有無を問わない）</td><td class="text-center"><input type="radio" name="q_past_seizure" value="はい"></td><td class="text-center"><input type="radio" name="q_past_seizure" value="いいえ" checked></td></tr>
                    <tr><td>意識消失発作</td><td class="text-center"><input type="radio" name="q_past_loc" value="はい"></td><td class="text-center"><input type="radio" name="q_past_loc" value="いいえ" checked></td></tr>
                    <tr><td>脳卒中（脳梗塞や脳出血など）</td><td class="text-center"><input type="radio" name="q_past_stroke" value="はい"></td><td class="text-center"><input type="radio" name="q_past_stroke" value="いいえ" checked></td></tr>
                    <tr><td>頭部外傷（意識がなくなるなど重度なもの）</td><td class="text-center"><input type="radio" name="q_past_trauma" value="はい"></td><td class="text-center"><input type="radio" name="q_past_trauma" value="いいえ" checked></td></tr>
                    <tr><td>頭部の手術</td><td class="text-center"><input type="radio" name="q_past_surgery" value="はい"></td><td class="text-center"><input type="radio" name="q_past_surgery" value="いいえ" checked></td></tr>
                    <tr><td>脳外科もしくは神経内科の病気</td><td class="text-center"><input type="radio" name="q_past_neuro" value="はい"></td><td class="text-center"><input type="radio" name="q_past_neuro" value="いいえ" checked></td></tr>
                    <tr><td>脳障害をおこす可能性のある内科疾患</td><td class="text-center"><input type="radio" name="q_past_internal" value="はい"></td><td class="text-center"><input type="radio" name="q_past_internal" value="いいえ" checked></td></tr>
                    <tr><td>アルコールや薬物の乱用</td><td class="text-center"><input type="radio" name="q_past_abuse" value="はい"></td><td class="text-center"><input type="radio" name="q_past_abuse" value="いいえ" checked></td></tr>
                    <tr class="q-header-row"><td colspan="3">現在、以下のことはありますか？</td></tr>
                    <tr><td>頻繁または重度な頭痛</td><td class="text-center"><input type="radio" name="q_cur_headache" value="はい"></td><td class="text-center"><input type="radio" name="q_cur_headache" value="いいえ" checked></td></tr>
                    <tr><td>頭の中に金属や磁性体（チタン製品かどうか要確認）</td><td class="text-center"><input type="radio" name="q_cur_metal" value="はい"></td><td class="text-center"><input type="radio" name="q_cur_metal" value="いいえ" checked></td></tr>
                    <tr><td>体内埋め込み式の医療機器（心臓ペースメーカーなど）</td><td class="text-center"><input type="radio" name="q_cur_device" value="はい"></td><td class="text-center"><input type="radio" name="q_cur_device" value="いいえ" checked></td></tr>
                    <tr><td>多量の飲酒や薬物の乱用</td><td class="text-center"><input type="radio" name="q_cur_abuse" value="はい"></td><td class="text-center"><input type="radio" name="q_cur_abuse" value="いいえ" checked></td></tr>
                    <tr><td>妊娠中、もしくは妊娠の可能性が否定されない</td><td class="text-center"><input type="radio" name="q_cur_preg" value="はい"></td><td class="text-center"><input type="radio" name="q_cur_preg" value="いいえ" checked></td></tr>
                    <tr><td>家族内にてんかんを持っているかた</td><td class="text-center"><input type="radio" name="q_cur_family_epilepsy" value="はい"></td><td class="text-center"><input type="radio" name="q_cur_family_epilepsy" value="いいえ" checked></td></tr>
                </tbody>
            </table>
            <div class="mt-3"><label class="form-label fw-bold small">「はい」とチェックした項目について、より詳しくおしえてください</label><textarea name="q_details" class="form-control" rows="4"></textarea></div>
            <div class="signature-section d-none d-print-flex"><div class="signature-box"><p style="font-size: 11px;">上記項目を確認し、治療適応と判断しました。</p><div class="signature-line">確認日：　　　　年　　月　　日</div><div class="signature-line">医師署名：</div></div></div>
            <input type="hidden" name="questionnaire_data" id="questionnaire_json">
        </div>
        <div class="fixed-bottom bg-white p-3 border-top shadow text-center no-print"><button type="submit" class="btn btn-primary px-5 fw-bold">保存して終了</button></div>
    </form>
</div>
<script>
    function printDiv(divId) {
        var originalContents = document.body.innerHTML; var printContents = document.getElementById(divId).outerHTML;
        document.body.innerHTML = printContents; window.print(); document.body.innerHTML = originalContents; window.location.reload();
    }
    document.querySelector('form').addEventListener('submit', function(e) {
        const data = {}; document.querySelectorAll('input[type="radio"]:checked').forEach(r => { data[r.name] = r.value; });
        data['q_details'] = document.querySelector('textarea[name="q_details"]').value;
        document.getElementById('questionnaire_json').value = JSON.stringify(data);
    });
    window.addEventListener('load', function() {
        const raw = '{{ patient.questionnaire_data|safe }}';
        if(raw && raw !== '{}' && raw !== 'None' && raw !== 'null') {
            try {
                const data = JSON.parse(raw);
                for (const [key, value] of Object.entries(data)) {
                    if (key === 'q_details') { const txt = document.querySelector('textarea[name="q_details"]'); if(txt) txt.value = value; }
                    else { const el = document.querySelector(`input[name="${key}"][value="${value}"]`); if(el) el.checked = true; }
                }
            } catch(e) {}
        }
    });
</script>
{% endblock %}""",

    # 5. 位置決め
    "mapping_add.html": """{% extends "admin/base_site.html" %}
{% block extrastyle %}<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">{% endblock %}
{% block content %}
<div class="container py-4">
    <h3>位置決め記録: {{ patient.name }} 殿</h3>
    <div class="row">
        <div class="col-md-6">
            <div class="card shadow-sm"><div class="card-header bg-warning text-dark fw-bold">新規記録</div>
                <div class="card-body"><form method="post">{% csrf_token %}<div class="mb-3">{{ form.date.label_tag }} {{ form.date }}</div><div class="mb-3">{{ form.week_number.label_tag }} {{ form.week_number }}</div><div class="mb-3">{{ form.resting_mt.label_tag }} {{ form.resting_mt }}</div><div class="mb-3">{{ form.stimulation_site.label_tag }} {{ form.stimulation_site }}</div><div class="mb-3">{{ form.notes.label_tag }} {{ form.notes }}</div><button type="submit" class="btn btn-warning w-100 fw-bold">保存</button></form></div>
            </div>
        </div>
        <div class="col-md-6">
            <div class="card shadow-sm"><div class="card-header bg-light fw-bold">履歴</div><ul class="list-group list-group-flush">{% for item in history %}<li class="list-group-item"><div class="d-flex justify-content-between"><strong>{{ item.get_week_number_display }} ({{ item.date }})</strong><span>MT: {{ item.resting_mt }}%</span></div><small class="text-muted">{{ item.stimulation_site }}</small></li>{% empty %}<li class="list-group-item text-muted">記録なし</li>{% endfor %}</ul></div>
        </div>
    </div>
</div>
{% endblock %}""",

    # 6. 治療実施
    "treatment_add.html": """{% extends "admin/base_site.html" %}
{% block extrastyle %}
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
    .section-title { background-color: #f8f9fa; padding: 10px; border-left: 5px solid #28a745; font-weight: bold; margin-bottom: 20px;}
    .check-row:hover { background-color: #f1f1f1; }
    @media print {
        @page { size: A4; margin: 15mm; }
        body * { visibility: hidden; }
        .printable-area, .printable-area * { visibility: visible; }
        .printable-area { position: absolute; left: 0; top: 0; width: 100%; color: #000; }
        .no-print, .btn, header, nav, footer { display: none !important; }
        .print-header { text-align: center; margin-bottom: 20px; border-bottom: 2px solid #000; padding-bottom: 5px; }
        .print-row { display: flex; justify-content: space-between; margin-bottom: 10px; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; font-size: 12px; }
        th, td { border: 1px solid #000 !important; padding: 5px; }
        th { background-color: #eee !important; -webkit-print-color-adjust: exact; text-align: center; }
        textarea, input[type="text"], input[type="number"], select { border: none; background: transparent; resize: none; overflow: visible; }
    }
</style>
{% endblock %}
{% block content %}
<div class="container py-4 mb-5">
    <div class="d-flex justify-content-between align-items-center mb-4 no-print">
        <h3><i class="fas fa-bolt"></i> 治療実施記録</h3>
        <div class="btn-group"><button type="button" class="btn btn-outline-success" onclick="printDiv('side-effects-sheet')"><i class="fas fa-print"></i> 副作用チェック表 印刷</button><a href="{% url 'dashboard' %}" class="btn btn-secondary">戻る</a></div>
    </div>
    <form method="post">
        {% csrf_token %}
        <div class="card mb-4 shadow-sm no-print"><div class="card-header bg-warning text-dark fw-bold">最新の位置決め情報 (Reference)</div><div class="card-body">{% if latest_mapping %}<div class="row"><div class="col-md-4"><strong>実施日:</strong> {{ latest_mapping.date }}</div><div class="col-md-4"><strong>安静時MT:</strong> {{ latest_mapping.resting_mt }} %</div><div class="col-md-4"><strong>刺激部位:</strong> {{ latest_mapping.stimulation_site }}</div></div>{% else %}<div class="text-danger">※位置決め記録がまだありません。初回治療前に位置決めを行ってください。</div>{% endif %}</div></div>
        <div class="card mb-4 shadow-sm no-print"><div class="card-body"><h5 class="section-title">実施日時・安全確認</h5><div class="row mb-3"><div class="col-md-4">{{ form.date.label_tag }} {{ form.date }}</div></div><div class="row"><div class="col-md-4"><div class="form-check form-switch py-2">{{ form.safety_sleep }} <label class="form-check-label fw-bold">睡眠不足なし</label></div></div><div class="col-md-4"><div class="form-check form-switch py-2">{{ form.safety_alcohol }} <label class="form-check-label fw-bold">アルコール・カフェイン過剰なし</label></div></div><div class="col-md-4"><div class="form-check form-switch py-2">{{ form.safety_meds }} <label class="form-check-label fw-bold">服薬変更なし</label></div></div></div></div></div>
        <div class="card mb-4 shadow-sm no-print"><div class="card-body"><h5 class="section-title">治療パラメータ</h5><div class="row g-3"><div class="col-md-4"><label class="form-label">当日のMT (%)</label>{{ form.motor_threshold }}</div><div class="col-md-4"><label class="form-label">刺激強度 (%)</label>{{ form.intensity }}<small class="text-muted">通常 120%</small></div><div class="col-md-4"><label class="form-label">総パルス数</label>{{ form.total_pulses }}</div></div></div></div>
        <div id="side-effects-sheet" class="printable-area card mb-4 shadow-sm">
            <div class="card-body">
                <div class="print-header d-none d-print-block"><h2>rTMS 副作用チェック表</h2><p style="text-align: right; font-size: 10px;">笠寺精治寮病院</p></div>
                <div class="print-row d-none d-print-flex"><div style="width: 30%;">ID: <strong>{{ patient.card_id }}</strong></div><div style="width: 40%;">氏名: <strong>{{ patient.name }} 殿</strong></div><div style="width: 30%; text-align: right;">実施日: __________________</div></div>
                <h5 class="section-title no-print">副作用チェック表</h5>
                <p class="text-muted small no-print">治療中・治療後の自覚症状について確認してください。</p>
                <table class="table table-hover table-bordered">
                    <thead class="table-light"><tr><th style="vertical-align: middle;">症状</th><th width="80" class="text-center">なし<br><span style="font-size:0.8em; font-weight:normal;">(None)</span></th><th width="80" class="text-center">軽度<br><span style="font-size:0.8em; font-weight:normal;">(Mild)</span></th><th width="80" class="text-center">中等度<br><span style="font-size:0.8em; font-weight:normal;">(Moderate)</span></th><th width="80" class="text-center">重度<br><span style="font-size:0.8em; font-weight:normal;">(Severe)</span></th></tr></thead>
                    <tbody>
                        {% for key, label in side_effect_items %}
                        <tr class="check-row"><td>{{ label }}</td><td class="text-center"><input type="radio" name="se_{{ key }}" value="0" checked></td><td class="text-center"><input type="radio" name="se_{{ key }}" value="1"></td><td class="text-center"><input type="radio" name="se_{{ key }}" value="2"></td><td class="text-center"><input type="radio" name="se_{{ key }}" value="3"></td></tr>
                        {% endfor %}
                    </tbody>
                </table>
                <div class="mt-3"><label class="form-label fw-bold">特記事項 (自由記載)</label><textarea name="se_note" class="form-control" rows="3" placeholder="けいれん発作の兆候やその他気になる点があれば記載"></textarea></div>
                <div class="d-none d-print-block mt-5 text-end"><p>実施者署名: ___________________________</p></div>
            </div>
        </div>
        <div class="text-center no-print"><button type="submit" class="btn btn-success btn-lg px-5">登録完了</button></div>
    </form>
</div>
<script>function printDiv(divId) { var originalContents = document.body.innerHTML; var printContents = document.getElementById(divId).outerHTML; document.body.innerHTML = printContents; window.print(); document.body.innerHTML = originalContents; window.location.reload(); }</script>
{% endblock %}""",

    # 7. 状態評価 (HAM-D)
    "assessment_add.html": """{% extends "admin/base_site.html" %}
{% block extrastyle %}
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
    .hamd-row { border-bottom: 1px solid #f0f0f0; padding: 12px 20px; transition: background 0.1s; }
    .hamd-row:hover { background-color: #f8f9fa; }
    .score-input { width: 80px; text-align: center; font-weight: bold; font-size: 1.1rem; }
    .history-table th, .history-table td { text-align: center; vertical-align: middle; }
    .tooltip-inner { max-width: 400px !important; text-align: left !important; white-space: pre-wrap; }
    .help-icon { color: #0d6efd; cursor: pointer; margin-left: 5px; font-size: 1.1rem; }
    .help-icon:hover { color: #0a58ca; }
</style>
{% endblock %}
{% block content %}
<div class="container py-4">
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h3><i class="fas fa-chart-bar text-danger"></i> 状態評価 (HAM-D)</h3>
        <div class="text-end"><span class="h5 fw-bold">{{ patient.name }} 殿</span><span class="text-muted ms-2 small">ID: {{ patient.card_id }}</span><a href="{% url 'dashboard' %}" class="btn btn-secondary ms-3">戻る</a></div>
    </div>
    <div class="card mb-4 shadow-sm"><div class="card-header bg-light fw-bold">過去の評価履歴</div><div class="table-responsive"><table class="table table-bordered table-sm mb-0 history-table"><thead class="table-light"><tr><th>項目</th>{% for h in history %}<th>{{ h.get_timing_display }}<br><small class="text-muted">{{ h.date|date:"Y/n/j" }}</small></th>{% endfor %}</tr></thead><tbody><tr><td class="fw-bold bg-white">HAM-D 17</td>{% for h in history %}<td class="fw-bold text-primary">{{ h.total_score_17 }}</td>{% endfor %}</tr><tr><td class="fw-bold bg-white">HAM-D 21</td>{% for h in history %}<td class="fw-bold">{{ h.total_score_21 }}</td>{% endfor %}</tr></tbody></table></div></div>
    <form method="post" class="card shadow-sm border-0">
        {% csrf_token %}
        <div class="card-header bg-primary text-white d-flex justify-content-between align-items-center p-3"><h5 class="mb-0"><i class="fas fa-pen"></i> 新規評価入力</h5><div class="d-flex gap-2 align-items-center"><label class="text-white small me-1">実施日:</label><input type="date" name="date" class="form-control form-control-sm" value="{{ today }}"><select name="timing" class="form-select form-select-sm" style="width: 140px;"><option value="baseline">治療前 (Base)</option><option value="week3">3週目</option><option value="week6">6週目</option><option value="other" selected>その他</option></select></div></div>
        <div class="card-body p-0">
            {% for key, label, max_score, text in hamd_items %}
            <div class="hamd-row"><div class="row align-items-center"><div class="col-md-9 d-flex align-items-center"><span class="fw-bold me-2">{{ label }}</span><i class="fas fa-info-circle help-icon" data-bs-toggle="tooltip" data-bs-html="true" title="{{ text }}"></i></div><div class="col-md-3 text-end d-flex justify-content-end align-items-center"><span class="text-muted small me-2">(0 - {{ max_score }})</span><input type="number" name="{{ key }}" class="form-control score-input" min="0" max="{{ max_score }}" value="0" required tabindex="{{ forloop.counter }}"></div></div></div>
            {% endfor %}
            <div class="p-4 bg-light border-top"><label class="form-label fw-bold">コメント・判定</label><textarea name="note" class="form-control" rows="2" tabindex="22" placeholder="特記事項があれば記載してください"></textarea></div>
        </div>
        <div class="card-footer text-center p-3"><button type="submit" class="btn btn-primary btn-lg px-5 fw-bold shadow-sm" tabindex="23"><i class="fas fa-save"></i> 評価を保存する</button></div>
    </form>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]')); var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) { return new bootstrap.Tooltip(tooltipTriggerEl) })</script>
{% endblock %}""",

    # 8. サマリー・紹介状
    "patient_summary.html": """{% extends "admin/base_site.html" %}
{% block extrastyle %}
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
    textarea { width: 100%; border: 1px solid #ced4da; border-radius: 4px; padding: 10px; font-family: inherit; }
    @media print {
        @page { size: A4; margin: 15mm; }
        body * { visibility: hidden; }
        .printable-area, .printable-area * { visibility: visible; }
        .printable-area { position: absolute; left: 0; top: 0; width: 100%; color: #000; font-family: "Hiragino Mincho ProN", "Yu Mincho", serif; }
        .no-print { display: none !important; }
        body.mode-summary .referral-only { display: none !important; }
        body.mode-referral .summary-only { display: none !important; }
        .doc-header { text-align: center; margin-bottom: 30px; border-bottom: 2px solid #000; padding-bottom: 10px; }
        .doc-header h1 { font-size: 24px; margin: 0; }
        .hospital-info { text-align: right; font-size: 11px; line-height: 1.4; margin-bottom: 20px; }
        .referral-address { font-size: 16px; margin-bottom: 20px; text-decoration: underline; }
        .info-row { display: flex; justify-content: space-between; margin-bottom: 5px; border-bottom: 1px solid #eee; padding-bottom: 2px;}
        .section-header { font-weight: bold; margin-top: 15px; margin-bottom: 5px; background-color: #eee; padding: 2px 5px; -webkit-print-color-adjust: exact;}
        textarea { border: none; resize: none; overflow: visible; height: auto; }
    }
</style>
{% endblock %}
{% block content %}
<div class="container py-4">
    <div class="d-flex justify-content-between align-items-center mb-4 no-print">
        <h3><i class="fas fa-file-alt"></i> サマリー・紹介状作成</h3>
        <div class="btn-group"><button type="button" class="btn btn-outline-primary" onclick="printMode('summary')"><i class="fas fa-print"></i> 退院サマリー印刷</button><button type="button" class="btn btn-outline-success" onclick="printMode('referral')"><i class="fas fa-envelope"></i> 紹介状形式で印刷</button><a href="{% url 'dashboard' %}" class="btn btn-secondary">戻る</a></div>
    </div>
    <div id="document-area" class="printable-area bg-white p-5 shadow-sm">
        <div class="referral-only"><div class="hospital-info"><strong>笠寺精治寮病院</strong><br>〒457-0051 愛知県名古屋市南区笠寺町柚ノ木３<br>TEL: 052-821-9221 FAX: 052-824-0286<br>担当医: {{ patient.attending_physician.last_name }} {{ patient.attending_physician.first_name }}</div><div class="referral-address">紹介元医療機関 御机下</div><div class="doc-header"><h1>診療情報提供書（逆紹介）</h1></div><p>下記の患者につきまして、当院でのrTMS治療が終了しましたのでご報告申し上げます。</p></div>
        <div class="summary-only"><div class="doc-header"><h1>退院時要約 (Discharge Summary)</h1><p style="text-align: right; font-size: 12px; margin:0;">笠寺精治寮病院</p></div></div>
        <div class="row mb-4"><div class="col-6 info-row"><span>ID:</span> <strong>{{ patient.card_id }}</strong></div><div class="col-6 info-row"><span>氏名:</span> <strong>{{ patient.name }}</strong></div><div class="col-6 info-row"><span>生年月日:</span> {{ patient.birth_date }} ({{ patient.age }}歳)</div><div class="col-6 info-row"><span>性別:</span> {{ patient.get_gender_display }}</div><div class="col-6 info-row"><span>入院日:</span> {{ patient.admission_date|default:"-" }}</div><div class="col-6 info-row"><span>退院日:</span> {{ today }} (予定)</div><div class="col-6 info-row"><span>担当医:</span> {{ patient.attending_physician }}</div><div class="col-6 info-row"><span>主治医:</span> {{ patient.attending_physician }}</div></div>
        <div class="mb-3"><div class="section-header">主訴または入院理由</div><p>rTMS (反復経頭蓋磁気刺激療法)</p></div>
        <div class="mb-3"><div class="section-header">紹介元</div><p>{{ patient.referral_source }}</p></div>
        <div class="mb-3"><div class="section-header">生活歴・既往歴・現病歴・薬剤歴</div><p style="white-space: pre-wrap;">{{ patient.life_history }}\n{{ patient.past_history }}\n{{ patient.present_illness }}\n{{ patient.medication_history }}</p></div>
        <div class="mb-3"><div class="section-header">入院経過・治療結果</div><textarea id="course-text" rows="10">{{ summary_text }}</textarea></div>
        <div class="referral-only mt-5"><p>今後とも何卒よろしくお願い申し上げます。</p><div style="text-align: right; margin-top: 30px;">記載日: {{ today }}<br>医師署名: ____________________</div></div>
    </div>
</div>
<script>
    function printMode(mode) {
        document.body.className = ''; document.body.classList.add('mode-' + mode);
        const tx = document.getElementById('course-text'); tx.style.height = 'auto'; tx.style.height = (tx.scrollHeight) + 'px';
        window.print();
    }
</script>
{% endblock %}""",

    # 9. 新規患者登録
    "patient_add.html": """{% extends "admin/base_site.html" %}
{% block extrastyle %}<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">{% endblock %}
{% block content %}
<div class="container py-5">
    <div class="card col-md-8 mx-auto shadow-sm">
        <div class="card-header bg-primary text-white"><h4 class="mb-0">新規患者登録</h4></div>
        <div class="card-body p-4">
            <form method="post">
                {% csrf_token %}
                {% for field in form %}
                <div class="mb-3">
                    <label class="form-label fw-bold">{{ field.label }}</label>
                    {{ field }}
                    {% if field.errors %}<div class="text-danger small">{{ field.errors }}</div>{% endif %}
                </div>
                {% endfor %}
                <div class="text-center mt-4">
                    <button type="submit" class="btn btn-primary px-5">登録する</button>
                    <a href="{% url 'dashboard' %}" class="btn btn-link text-secondary">キャンセル</a>
                </div>
            </form>
        </div>
    </div>
</div>
{% endblock %}"""
}

# Zipファイル作成
zip_filename = "templates.zip"
with zipfile.ZipFile(zip_filename, 'w') as zf:
    for filename, content in templates.items():
        # rtms_app/templates/rtms_app/ の階層に配置
        zf.writestr(f"rtms_app/templates/rtms_app/{filename}", content)

print(f"Created {zip_filename}")