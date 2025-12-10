from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import timedelta, date
from django.http import HttpResponse, FileResponse
from django.conf import settings
import os
import csv
import json

from .models import Patient, TreatmentSession, MappingSession, Assessment
from .forms import PatientFirstVisitForm, MappingForm, TreatmentForm, PatientScheduleForm

# ------------------------------------------------------------------
# ユーティリティ: 平日計算ロジック
# ------------------------------------------------------------------
def get_session_number(start_date, target_date):
    """
    初回治療日(start_date)からtarget_dateまで、
    土日を除いて何回目の治療か（何日目か）を計算する。
    ※祝日や年末年始の判定は簡易的に手動調整が必要な場合がありますが、
      ここでは土日除外ロジックで実装します。
    戻り値:
        1以上の整数: 第N回目
        0: 治療開始前
        -1: 土日など治療日ではない
    """
    if not start_date or target_date < start_date:
        return 0
    
    # 土日なら治療日ではない (-1)
    if target_date.weekday() >= 5: # 5=Sat, 6=Sun
        return -1

    current_date = start_date
    session_count = 0
    
    # 開始日からターゲット日までループして平日をカウント
    # (日数が多いと重くなるため、運用が長期間に及ぶ場合は数式計算に切り替え推奨)
    while current_date <= target_date:
        if current_date.weekday() < 5: # 平日のみカウント
            session_count += 1
        current_date += timedelta(days=1)
        
    return session_count

def is_holiday_or_weekend(d):
    """土日判定"""
    return d.weekday() >= 5

# ------------------------------------------------------------------
# 1. 業務ダッシュボード (トップ画面)
# ------------------------------------------------------------------
@login_required
def dashboard_view(request):
    """
    業務タスク一覧を表示するトップ画面
    """
    # 日付指定がなければ今日にリダイレクト
    if 'date' not in request.GET:
        jst_now = timezone.localtime(timezone.now())
        today_str = jst_now.strftime('%Y-%m-%d')
        return redirect(f'{request.path}?date={today_str}')

    date_str = request.GET.get('date')
    try:
        target_date = parse_date(date_str)
    except:
        target_date = timezone.now().date()

    if not target_date:
        target_date = timezone.now().date()

    # ナビゲーション用
    prev_day = target_date - timedelta(days=1)
    next_day = target_date + timedelta(days=1)

    # ----------------------------------------------
    # A. 今日の初診 (登録日 = target_date)
    # ----------------------------------------------
    new_patients_query = Patient.objects.filter(created_at__date=target_date)
    new_patients = []
    for p in new_patients_query:
        # 初診情報の入力チェック（必須項目が埋まっているかなど）
        # 簡易的に「患者データが存在すれば入力済み」とみなしますが、
        # 必要なら p.life_history 等の中身を確認して分岐します。
        status_label = "実施済"
        status_color = "success"
        
        new_patients.append({
            'obj': p, 
            'status': status_label,
            'color': status_color
        })

    # ----------------------------------------------
    # B. 今日の入院 (入院予定日 = target_date)
    # ----------------------------------------------
    admissions_query = Patient.objects.filter(admission_date=target_date)
    admissions = []
    for p in admissions_query:
        admissions.append({'obj': p, 'status': "要対応", 'color': "warning"})

    # ----------------------------------------------
    # C. 今日の位置決め (位置決め予定日 = target_date)
    # ----------------------------------------------
    mappings_scheduled = Patient.objects.filter(mapping_date=target_date)
    mappings = []
    for p in mappings_scheduled:
        # 実施済みチェック
        is_done = MappingSession.objects.filter(patient=p, date=target_date).exists()
        mappings.append({
            'obj': p, 
            'status': "実施済" if is_done else "実施未",
            'color': "success" if is_done else "danger"
        })

    # ----------------------------------------------
    # D. 今日の治療実施 (治療期間中の患者)
    # ----------------------------------------------
    # 「初回治療日が設定されている」患者すべてを対象に計算
    # (終了フラグがないため、全員分計算して「30回以内」の人だけ表示する)
    active_candidates = Patient.objects.filter(first_treatment_date__isnull=False).order_by('card_id')
    treatments = []
    assessments_due = [] # 評価対象者もここで探す
    
    for p in active_candidates:
        # 今日は何回目か計算
        session_num = get_session_number(p.first_treatment_date, target_date)
        
        # 治療対象外の日（土日）または まだ開始前、あるいは30回を大幅に超えている(例えば40回以上)なら表示しない
        if session_num <= 0 or session_num > 40:
            continue
            
        # 今日の実施記録があるか
        today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first()
        is_done = today_session is not None
        
        # 表示用データ作成
        treatments.append({
            'obj': p,
            'note': f"第{session_num}回",
            'status': "実施済" if is_done else "実施未",
            'color': "success" if is_done else "danger", # 未実施は赤
            'session_num': session_num
        })

        # --- E. 状態評価の判定 (15回目と30回目) ---
        # 1. 入院日 (=治療前評価) の患者を追加 ★追加
    for p in admissions_query:
        # すでに今日評価済みかチェック
        done_assessment = Assessment.objects.filter(patient=p, date=target_date).exists()
        assessments_due.append({
            'obj': p,
            'reason': "入院時評価 (治療前)",
            'status': "実施済" if done_assessment else "実施未",
            'color': "success" if done_assessment else "danger"
        })
        
        # 「今日が15回目」または「今日が30回目」の日であれば評価リストに入れる
        if session_num == 15 or session_num == 30:
            # 評価済みかチェック
            is_assessed = Assessment.objects.filter(patient=p, date=target_date).exists()
            
            reason_text = "中間評価 (15回目)" if session_num == 15 else "終了時評価 (30回目)"
            
            assessments_due.append({
                'obj': p,
                'reason': reason_text,
                'status': "実施済" if is_assessed else "実施未",
                'color': "success" if is_assessed else "danger"
            })

    context = {
        'today': target_date,
        'prev_day': prev_day,
        'next_day': next_day,
        'new_patients': new_patients,
        'admissions': admissions,
        'mappings': mappings,
        'treatments': treatments,
        'assessments_due': assessments_due,
    }
    return render(request, 'rtms_app/dashboard.html', context)


# ------------------------------------------------------------------
# 2. 初診・基本情報入力
# ------------------------------------------------------------------
@login_required
def patient_first_visit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    if request.method == 'POST':
        form = PatientFirstVisitForm(request.POST, instance=patient)
        if form.is_valid():
            form.save()
            return redirect('dashboard')
    else:
        form = PatientFirstVisitForm(instance=patient)
    return render(request, 'rtms_app/patient_first_visit.html', {'patient': patient, 'form': form})


# ------------------------------------------------------------------
# 3. 位置決め記録入力
# ------------------------------------------------------------------
@login_required
def mapping_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = MappingSession.objects.filter(patient=patient).order_by('date')
    
    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid():
            mapping = form.save(commit=False)
            mapping.patient = patient
            mapping.save()
            return redirect('dashboard')
    else:
        # 日付指定があればその日を初期値に
        initial_date = request.GET.get('date') or timezone.now().date()
        form = MappingForm(initial={'date': initial_date, 'week_number': 1})

    return render(request, 'rtms_app/mapping_add.html', {'patient': patient, 'form': form, 'history': history})


# ------------------------------------------------------------------
# 4. 治療実施入力
# ------------------------------------------------------------------
@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    
    # 副作用項目定義
    side_effect_items = [
        ('headache', '頭痛'), ('scalp', '頭皮痛（刺激痛）'), ('discomfort', '刺激部位の不快感'),
        ('tooth', '歯痛'), ('twitch', '顔面のけいれん'), ('dizzy', 'めまい'),
        ('nausea', '吐き気'), ('tinnitus', '耳鳴り'), ('hearing', '聴力低下'),
        ('anxiety', '不安感・焦燥感'), ('other', 'その他'),
    ]

    if request.method == 'POST':
        form = TreatmentForm(request.POST)
        if form.is_valid():
            session = form.save(commit=False)
            session.patient = patient
            session.performer = request.user
            
            # 副作用データの収集
            se_data = {}
            for key, label in side_effect_items:
                val = request.POST.get(f'se_{key}') # 0,1,2,3
                if val: se_data[key] = val
            se_data['note'] = request.POST.get('se_note', '')
            
            session.side_effects = se_data
            session.save()
            return redirect('dashboard')
    else:
        # 日付指定があればその日時を初期値に
        target_date_str = request.GET.get('date')
        if target_date_str:
            # 時間は現在時刻、日付は指定日
            now = timezone.now()
            target = parse_date(target_date_str)
            initial_date = now.replace(year=target.year, month=target.month, day=target.day)
        else:
            initial_date = timezone.now()

        initial_data = {'date': initial_date, 'total_pulses': 1980, 'intensity': 120}
        if latest_mapping:
            initial_data['motor_threshold'] = latest_mapping.resting_mt
            
        form = TreatmentForm(initial=initial_data)

    return render(request, 'rtms_app/treatment_add.html', {
        'patient': patient,
        'form': form,
        'latest_mapping': latest_mapping,
        'side_effect_items': side_effect_items,
    })


# ------------------------------------------------------------------
# 5. 状態評価入力
# ------------------------------------------------------------------
@login_required
def assessment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = Assessment.objects.filter(patient=patient).order_by('date')
    
    # HAM-D 21項目の定義 (PDF P13準拠)
    hamd_items = [
        ('q1', '抑うつ気分', '0=ない 1=そのことばかり言う 2=泣く 3=言葉や表情でわかる 4=極端な症状'),
        ('q2', '罪業感', '0=ない 1=自責の念 2=罪の意識 3=現在の病気は罰だと思う 4=罪悪妄想'),
        ('q3', '自殺', '0=ない 1=人生が虚しい 2=死にたい 3=自殺の動作や身振り 4=自殺企図'),
        ('q4', '入眠障害', '0=ない 1=就床後30分以上眠れない 2=一晩中眠れない'),
        ('q5', '熟眠障害', '0=ない 1=夜間に目が覚める 2=ベッドから起き出す'),
        ('q6', '早朝覚醒', '0=ない 1=早く目が覚めるが再入眠可 2=再入眠できない'),
        ('q7', '仕事と興味', '0=ない 1=倦怠感・迷い 2=興味喪失 3=活動時間の減少 4=仕事ができない'),
        ('q8', '制止', '0=ない 1=思考や会話が遅い 2=はっきりとした制止 3=会話困難 4=完全な木僵'),
        ('q9', '焦燥', '0=ない 1=落ち着きがない 2=手をもてあそぶ 3=動き回る 4=自分の手や爪を噛む'),
        ('q10', '精神的不安', '0=ない 1=緊張・過敏 2=ささいなことを心配 3=顔色や言動に表れる 4=恐怖感'),
        ('q11', '身体的不安', '0=ない 1=軽度 2=中等度 3=重度 4=極度 (胃腸症状、発汗など)'),
        ('q12', '胃腸症状', '0=ない 1=食欲不振 2=下剤が必要'),
        ('q13', '一般的身体症状', '0=ない 1=四肢・背部・頭部の重苦しさ 2=はっきりした症状'),
        ('q14', '性欲', '0=ない 1=軽度減退 2=重度減退'),
        ('q15', '心気症', '0=ない 1=自分の身体にこだわる 2=健康を心配 3=訴えが強い 4=妄想的'),
        ('q16', '体重減少', '0=ない 1=週500g以上 2=週1kg以上 (または評価不能)'),
        ('q17', '病識', '0=病気だと知っている 1=病気だが食事等のせい 2=病気だと思わない'),
        ('q18', '日内変動', '0=ない 1=軽度(朝/夕) 2=重度(朝/夕) ※悪化する時間帯を記録'),
        ('q19', '離人感・現実感消失', '0=ない 1=軽度 2=重度 3=完全な消失 4=極度'),
        ('q20', '被害妄想', '0=ない 1=疑い深い 2=被害念慮 3=被害妄想'),
        ('q21', '強迫症状', '0=ない 1=軽度 2=重度'),
    ]

    if request.method == 'POST':
        try:
            scores = {}
            for key, label, guide in hamd_items:
                scores[key] = int(request.POST.get(key, 0))

            assessment = Assessment(
                patient=patient,
                date=request.POST.get('date', timezone.now().date()),
                type='HAM-D',
                scores=scores,
                timing=request.POST.get('timing', 'other'),
                note=request.POST.get('note', '')
            )
            assessment.calculate_scores()
            assessment.save()
            return redirect('dashboard')
        except Exception as e:
            print(f"Error: {e}")
            
    initial_date = request.GET.get('date') or timezone.now().strftime('%Y-%m-%d')
            
    return render(request, 'rtms_app/assessment_add.html', {
        'patient': patient,
        'history': history,
        'today': initial_date,
        'hamd_items': hamd_items
    })


# ------------------------------------------------------------------
# 6. 新規患者登録
# ------------------------------------------------------------------
@login_required
def patient_add_view(request):
    if request.method == 'POST':
        from .forms import PatientRegistrationForm
        form = PatientRegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('dashboard')
    else:
        from .forms import PatientRegistrationForm
        form = PatientRegistrationForm()
    return render(request, 'rtms_app/patient_add.html', {'form': form})


# ------------------------------------------------------------------
# 7. 管理者用機能
# ------------------------------------------------------------------
@login_required
def export_treatment_csv(request):
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', '氏名', '実施日時', 'MT(%)', '強度(%)', 'パルス数', '実施者', '副作用'])
    
    treatments = TreatmentSession.objects.all().select_related('patient', 'performer').order_by('date')
    for t in treatments:
        se_str = json.dumps(t.side_effects, ensure_ascii=False) if t.side_effects else ""
        writer.writerow([
            t.patient.card_id, t.patient.name, t.date.strftime('%Y-%m-%d %H:%M'),
            t.motor_threshold, t.intensity, t.total_pulses,
            t.performer.username if t.performer else "", se_str
        ])
    return response

@login_required
def download_db(request):
    if not request.user.is_staff: return HttpResponse("Forbidden", status=403)
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path):
        return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    return HttpResponse("Not found", status=404)
    
# ------------------------------------------------------------------
# 8. サマリー・紹介状作成画面 (★新規追加)
# ------------------------------------------------------------------
@login_required
def patient_summary_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    
    # データの取得
    sessions = TreatmentSession.objects.filter(patient=patient).order_by('date')
    assessments = Assessment.objects.filter(patient=patient).order_by('date')
    
    # 自動生成テキストの作成
    # 1. 評価スコアの取得 (入院時=最初のデータ, 3週, 6週, 最終)
    score_admin = assessments.first()
    score_end = assessments.last()
    
    # 3週目(15回前後), 6週目(30回前後) のデータを探すロジックは簡易的に日付等で行うか
    # Assessmentモデルの timing フィールドを使用します
    score_w3 = assessments.filter(timing='week3').first()
    score_w6 = assessments.filter(timing='week6').first()
    
    def fmt_score(obj):
        return f"HAMD17 {obj.total_score_17}点 HAMD21 {obj.total_score_21}点" if obj else "未評価"

    # 2. 合併症・副作用の集計
    side_effects_list = []
    for s in sessions:
        if s.side_effects:
            # note以外のキー(症状)があり、値が0(なし)以外なら拾う
            for k, v in s.side_effects.items():
                if k != 'note' and v and str(v) != '0':
                    side_effects_list.append(k) # 日本語変換が必要なら辞書でマッピング
    
    # 重複排除して文字列化
    side_effects_summary = ", ".join(list(set(side_effects_list))) if side_effects_list else "特になし"
    
    # 3. 日付フォーマット
    start_date_str = sessions.first().date.strftime('%Y年%m月%d日') if sessions.exists() else "未開始"
    end_date_str = sessions.last().date.strftime('%Y年%m月%d日') if sessions.exists() else "未終了"
    total_count = sessions.count()
    admission_date_str = patient.admission_date.strftime('%Y年%m月%d日') if patient.admission_date else "不明"
    created_at_str = patient.created_at.strftime('%Y年%m月%d日')
    
    # サマリー本文のテンプレート
    summary_text = (
        f"{created_at_str}初診、{admission_date_str}任意入院。\n"
        f"入院時{fmt_score(score_admin)}、{start_date_str}から全{total_count}回のrTMS治療を実施した。\n"
        f"3週時、{fmt_score(score_w3)}、6週時、{fmt_score(score_w6)}となった。\n"
        f"治療中の合併症：{side_effects_summary}。\n"
        f"{end_date_str}退院。紹介元へ逆紹介、抗うつ薬の治療継続を依頼した。"
    )

    if request.method == 'POST':
        # 保存機能をつける場合はここに実装
        pass

    return render(request, 'rtms_app/patient_summary.html', {
        'patient': patient,
        'summary_text': summary_text,
        'today': timezone.now().date(),
    })