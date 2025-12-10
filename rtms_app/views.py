from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import timedelta
from django.http import HttpResponse, FileResponse
from django.conf import settings
import os
import csv
import json

from .models import Patient, TreatmentSession, MappingSession, Assessment
# forms.pyに定義した全てのフォームをインポート
from .forms import (
    PatientFirstVisitForm, MappingForm, TreatmentForm, 
    PatientRegistrationForm, AdmissionProcedureForm
)

# ------------------------------------------------------------------
# ユーティリティ: 平日計算ロジック
# ------------------------------------------------------------------
def get_session_number(start_date, target_date):
    """
    初回治療日(start_date)からtarget_dateまで、
    土日を除いて何回目の治療か（何日目か）を計算する。
    """
    if not start_date or target_date < start_date:
        return 0
    
    # 土日なら治療日ではない (-1)
    if target_date.weekday() >= 5: # 5=Sat, 6=Sun
        return -1

    current_date = start_date
    session_count = 0
    
    # 開始日からターゲット日までループして平日をカウント
    while current_date <= target_date:
        if current_date.weekday() < 5: # 平日のみカウント
            session_count += 1
        current_date += timedelta(days=1)
        
    return session_count

# ------------------------------------------------------------------
# 1. 業務ダッシュボード (トップ画面)
# ------------------------------------------------------------------
@login_required
def dashboard_view(request):
    """業務タスク一覧を表示するトップ画面"""
    
    # 日本時間を基準にする
    jst_now = timezone.localtime(timezone.now())
    
    # 日付指定がない場合は、今日のURLへリダイレクト
    if 'date' not in request.GET:
        today_str = jst_now.strftime('%Y-%m-%d')
        return redirect(f'{request.path}?date={today_str}')

    # 日付の取得
    date_str = request.GET.get('date')
    try:
        target_date = parse_date(date_str)
    except:
        target_date = jst_now.date()
        
    if not target_date:
        target_date = jst_now.date()

    # 前日・翌日ナビゲーション用
    prev_day = target_date - timedelta(days=1)
    next_day = target_date + timedelta(days=1)

    # --- A. 今日の初診 (登録日がターゲット日付) ---
    new_patients_query = Patient.objects.filter(created_at__date=target_date)
    new_patients = []
    for p in new_patients_query:
        new_patients.append({'obj': p, 'status': "登録済"})

    # --- B. 今日の入院 (入院予定日がターゲット日付) ---
    admissions_query = Patient.objects.filter(admission_date=target_date)
    admissions = []
    for p in admissions_query:
        # 入院手続き完了フラグでステータス分岐
        status = "手続済" if p.is_admission_procedure_done else "要手続"
        color = "success" if p.is_admission_procedure_done else "warning"
        admissions.append({'obj': p, 'status': status, 'color': color})

    # --- C. 今日の位置決め (位置決め予定日がターゲット日付) ---
    mappings_scheduled = Patient.objects.filter(mapping_date=target_date)
    mappings = []
    for p in mappings_scheduled:
        is_done = MappingSession.objects.filter(patient=p, date=target_date).exists()
        mappings.append({
            'obj': p, 
            'status': "実施済" if is_done else "実施未",
            'color': "success" if is_done else "danger"
        })

    # --- D. 今日の治療実施 & E. 状態評価 ---
    treatments = []
    assessments_due = []
    
    # 1. 入院時評価 (入院日の患者を評価リストに追加)
    for adm in admissions:
        is_done = Assessment.objects.filter(patient=adm['obj'], date=target_date).exists()
        assessments_due.append({
            'obj': adm['obj'],
            'reason': "入院時評価 (治療前)",
            'status': "実施済" if is_done else "実施未",
            'color': "success" if is_done else "danger"
        })

    # 2. 治療中の患者 (初回治療日が設定されている人)
    active_candidates = Patient.objects.filter(first_treatment_date__isnull=False).order_by('card_id')
    
    for p in active_candidates:
        session_num = get_session_number(p.first_treatment_date, target_date)
        
        # --- 退院準備フラグ (30回目終了後の表示例) ---
        if session_num == 30:
             # 治療リストにも特別な表示で出す
             treatments.append({
                'obj': p,
                'note': "第30回 (最終)",
                'status': "退院準備",
                'color': "info",
                'session_num': session_num,
                'is_discharge': True # テンプレートでボタンを出し分けるフラグ
            })
        
        # 範囲外(開始前、土日、35回以上経過)はスキップ
        if session_num <= 0 or session_num > 35:
            continue
            
        # 今日の治療記録があるか
        today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first()
        is_done = today_session is not None
        
        # リストに追加 (30回目の重複を避けるため is_discharge がない場合のみ追加する等の制御も可だが、今回は上書き)
        # 既に退院準備として追加されていなければ追加
        if not any(t['obj'] == p for t in treatments):
            treatments.append({
                'obj': p,
                'note': f"第{session_num}回",
                'status': "実施済" if is_done else "実施未",
                'color': "success" if is_done else "danger",
                'session_num': session_num,
                'is_discharge': False
            })

        # --- 評価日の判定 (15回目, 30回目) ---
        if session_num == 15 or session_num == 30:
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
# 2. 患者一覧機能
# ------------------------------------------------------------------
@login_required
def patient_list_view(request):
    """全患者のリスト表示"""
    patients = Patient.objects.all().order_by('card_id')
    return render(request, 'rtms_app/patient_list.html', {'patients': patients})


# ------------------------------------------------------------------
# 3. 入院手続き入力
# ------------------------------------------------------------------
@login_required
def admission_procedure(request, patient_id):
    """入院形態の選択と手続き完了フラグの管理"""
    patient = get_object_or_404(Patient, pk=patient_id)
    if request.method == 'POST':
        form = AdmissionProcedureForm(request.POST, instance=patient)
        if form.is_valid():
            proc = form.save(commit=False)
            proc.is_admission_procedure_done = True
            proc.save()
            return redirect('dashboard')
    else:
        form = AdmissionProcedureForm(instance=patient)
    
    return render(request, 'rtms_app/admission_procedure.html', {
        'patient': patient, 'form': form
    })


# ------------------------------------------------------------------
# 4. 初診・基本情報入力 (適正質問票含む)
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
    
    return render(request, 'rtms_app/patient_first_visit.html', {
        'patient': patient, 'form': form
    })


# ------------------------------------------------------------------
# 5. 位置決め記録入力
# ------------------------------------------------------------------
@login_required
def mapping_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = MappingSession.objects.filter(patient=patient).order_by('date')
    
    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid():
            m = form.save(commit=False)
            m.patient = patient
            m.save()
            return redirect('dashboard')
    else:
        initial_date = request.GET.get('date') or timezone.now().date()
        form = MappingForm(initial={'date': initial_date, 'week_number': 1})

    return render(request, 'rtms_app/mapping_add.html', {
        'patient': patient, 'form': form, 'history': history
    })


# ------------------------------------------------------------------
# 6. 治療実施入力 (副作用チェック含む)
# ------------------------------------------------------------------
@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    
    # 副作用チェック項目
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
                val = request.POST.get(f'se_{key}')
                if val: se_data[key] = val
            se_data['note'] = request.POST.get('se_note', '')
            
            session.side_effects = se_data
            session.save()
            return redirect('dashboard')
    else:
        # 日付指定の処理
        target_date_str = request.GET.get('date')
        now = timezone.now()
        if target_date_str:
            target = parse_date(target_date_str)
            if target:
                initial_date = now.replace(year=target.year, month=target.month, day=target.day)
            else:
                initial_date = now
        else:
            initial_date = now

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
# 7. 状態評価入力 (HAM-D)
# ------------------------------------------------------------------
@login_required
def assessment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = Assessment.objects.filter(patient=patient).order_by('date')
    
    # HAM-D 21項目定義
    # ... (前略)

# ------------------------------------------------------------------
# 7. 状態評価入力 (HAM-D)
# ------------------------------------------------------------------
@login_required
def assessment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = Assessment.objects.filter(patient=patient).order_by('date')
    
    # HAM-D 21項目定義 (STAR*D版 SIGH-D)
    # 形式: (key, title, max_score, detailed_text)
    hamd_items = [
        ('q1', '1. 抑うつ気分', 4, 
         "0. なし<br>1. 質問をされた時のみ示される（一時的、軽度のうつ状態）<br>2. 自ら言葉で訴える（持続的、軽度から中等度のうつ状態）<br>3. 言葉を使わなくとも伝わる（例えば、表情・姿勢・声・涙もろさ）（持続的、中等度から重度のうつ状態）<br>4. 言語的にも、非言語的にも、事実上こうした気分の状態のみが、自然に表現される（持続的、極めて重度のうつ状態、希望のなさや涙もろさが顕著）"),
        
        ('q2', '2. 罪責感', 4,
         "0. なし<br>1. 自己非難、他人をがっかりさせたという思い（生産性の低下に対する自責感のみ）<br>2. 過去の過ちや罪深い行為に対する、罪責観念や思考の反復（罪責、後悔、あるいは恥の感情）<br>3. 現在の病気は自分への罰であると考える、罪責妄想（重度で広範な罪責感）<br>4. 非難や弾劾するような声が聞こえ、そして（あるいは）脅されるような幻視を体験する"),
        
        ('q3', '3. 自殺', 4,
         "0. なし<br>1. 生きる価値がないと感じる<br>2. 死ねたらという願望、または自己の死の可能性を考える<br>3. 自殺念慮、自殺をほのめかす行動をとる<br>4. 自殺を企図する"),
        
        ('q4', '4. 入眠障害', 2,
         "0. 入眠困難はない<br>1. 時々寝つけない、と訴える（すなわち、30分以上、週に2-3日）<br>2. 夜ごと寝つけない、と訴える（すなわち、30分以上、週に4日以上）"),
        
        ('q5', '5. 熟眠障害', 2,
         "0. 熟眠困難はない<br>1. 夜間、睡眠が不安定で、妨げられると訴える（または、時々、すなわち週に2-3日、夜中に30分以上覚醒している）<br>2. 夜中に目が覚めてしまう―トイレ以外で、寝床から出てしまういかなる場合も含む（しばしば、すなわち週に4日以上、夜中に30分以上覚醒している）"),
        
        ('q6', '6. 早朝睡眠障害', 2,
         "0. 早朝睡眠に困難はない<br>1. 早朝に目が覚めるが、再び寝つける（時々、すなわち、週に2～3日、早朝に30分以上目が覚める）<br>2. 一度起き出すと、再び寝つくことはできない（しばしば、すなわち、週に4日以上、早朝に30分以上目が覚める）"),
        
        ('q7', '7. 仕事と活動', 4,
         "0. 困難なくできる<br>1. 活動、仕事、あるいは趣味に関連して、それができない、疲れる、弱気であるといった思いがある（興味や喜びは軽度減退しているが、機能障害は明らかではない）<br>2. 活動・趣味・仕事に対する興味の喪失―患者が直接訴える、あるいは、気乗りのなさ、優柔不断、気迷いから間接的に判断される（仕事や活動をするのに無理せざるを得ないと感じる興味や喜び、機能は明らかに減退している）<br>3. 活動に費やす実時間の減少、あるいは生産性の低下（興味や喜び、機能の深刻な減退）<br>4. 現在の病気のために、働くことをやめた（病気のために仕事あるいは主要な役割を果たすことができない、そして興味も完全に喪失している）"),
        
        ('q8', '8. 精神運動抑制', 4,
         "0. 発話・思考は正常である<br>1. 面接時に軽度の遅滞が認められる（または、軽度の精神運動抑制）<br>2. 面接時に明らかな遅滞が認められる（すなわち、中等度、面接はいくらか困難；話は途切れがちで、思考速度は遅い）<br>3. 面接は困難である（重度の精神運動抑制、話はかなり長く途切れてしまい、面接は非常に困難）<br>4. 完全な昏迷（極めて重度の精神運動抑制：昏迷：面接はほとんど不可能）"),
        
        ('q9', '9. 精神運動激越', 4,
         "0. なし（正常範囲内の動作）<br>1. そわそわする<br>2. 手や髪などをいじくる<br>3. 動き回る、じっと座っていられない<br>4. 手を握りしめる、爪を噛む、髪を引っ張る、唇を噛む（面接は不可能）"),
        
        ('q10', '10. 不安, 精神症状', 4,
         "0. 問題なし<br>1. 主観的な緊張とイライラ感（軽度、一時的）<br>2. 些細な事柄について悩む（中等度、多少の苦痛をもたらす、あるいは実在する問題に過度に悩んでいる）<br>3. 心配な態度が顔つきや話し方から明らかである（重度：不安のために機能障害が生じている）<br>4. 疑問の余地なく恐怖が表出されている（何もできない程の症状）"),
        
        ('q11', '11. 不安, 身体症状', 4,
         "0. なし<br>1. 軽度（症状は時々出現するのみ、機能の障害はない。わずかな苦痛）<br>2. 中等度（症状はより持続する、普段の活動に多少の支障をきたす、中等度の苦痛）<br>3. 重度（顕著な機能の障害）<br>4. 何もできなくなる"),
        
        ('q12', '12. 身体症状, 消化器系', 2,
         "0. なし<br>1. 食欲はないが、促されなくても食べている（普段より食欲はいくらか低下）<br>2. 促されないと食事摂取が困難（あるいは、無理して食べなければならないかどうかに関わらず、食欲は顕著に低下している）"),
        
        ('q13', '13. 身体症状, 一般的', 2,
         "0. なし<br>1. 手足や背中、あるいは頭の重苦しさ。背部痛、頭痛、筋肉痛。元気のなさや易疲労性（普段より気力はいくらか低下：軽度で一時的な、気力の喪失や筋肉の痛み／重苦しさ）<br>2. 何らかの明白な症状（持続的で顕著な、気力の喪失や筋肉の痛み／重苦しさ）"),
        
        ('q14', '14. 生殖器症状', 2,
         "0. なし<br>1. 軽度（普段よりいくらか関心が低下）<br>2. 重度（普段よりかなり関心が低下）"),
        
        ('q15', '15. 心気症', 4,
         "0. なし（不適切な心配はない、あるいは完全に安心できる）<br>1. 体のことが気がかりである（自分の健康に関する多少の不適切な心配、または大丈夫だと言われているにも関わらず、わずかに心配している）<br>2. 健康にこだわっている（しばしば自身の健康に対し過剰に心配する、あるいは医学的に大丈夫だと明言されているにも関わらず、特別な病気があると思い込んでいる）<br>3. 訴えや助けを求めること等が頻繁にみられる（医師が確認できていない身体的問題があると確信している：身体的な健康についての誇張された、現実的でない心配）<br>4. 心気妄想（例えば、体の一部が衰え、腐ってしまうと感じる、など、外来患者ではまれである）"),
        
        ('q16', '16. 体重減少', 2,
         "0. 体重減少なし、あるいは今回の病気による減少ではない<br>1. 今回のうつ病により、おそらく体重が減少している<br>2. （患者によると）うつ病により、明らかに体重が減少している"),
        
        ('q17', '17. 病識', 2,
         "0. うつ状態であり病気であることを認める、または現在うつ状態でない<br>1. 病気であることを認めるが、原因を粗食、働き過ぎ、ウィルス、休息の必要性などのせいにする（病気を否定するが、病気である可能性は認める）<br>2. 病気であることを全く認めない（病気であることを完全に否定する）"),
        
        ('q18', '18. 日内変動', 2,
         "<strong>A. 変動の有無・方向</strong><br>0: なし, 1: 午前, 2: 午後<br><br><strong>B. 変動の程度 (※こちらをスコアとして入力)</strong><br>0. なし<br>1. 軽度<br>2. 重度"),
        
        ('q19', '19. 現実感喪失, 離人症', 4,
         "0. なし<br>1. 軽度<br>2. 中等度<br>3. 重度<br>4. 何もできなくなる"),
        
        ('q20', '20. 妄想症状', 3,
         "0. なし<br>1. 疑念をもっている<br>2. 関係念慮<br>3. 被害関係妄想"),
        
        ('q21', '21. 強迫症状', 2,
         "0. なし<br>1. 軽度<br>2. 重度"),
    ]

    if request.method == 'POST':
        try:
            scores = {}
            # 項目数分のループ処理に変更
            for key, label, max_score, text in hamd_items:
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
# 8. サマリー・紹介状作成画面
# ------------------------------------------------------------------
@login_required
def patient_summary_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    sessions = TreatmentSession.objects.filter(patient=patient).order_by('date')
    assessments = Assessment.objects.filter(patient=patient).order_by('date')
    
    # スコア取得
    score_admin = assessments.first()
    score_end = assessments.last()
    score_w3 = assessments.filter(timing='week3').first()
    score_w6 = assessments.filter(timing='week6').first()
    
    def fmt_score(obj):
        return f"HAMD17 {obj.total_score_17}点 HAMD21 {obj.total_score_21}点" if obj else "未評価"

    # 副作用集計
    side_effects_list = []
    for s in sessions:
        if s.side_effects:
            for k, v in s.side_effects.items():
                if k != 'note' and v and str(v) != '0':
                    side_effects_list.append(k)
    side_effects_summary = ", ".join(list(set(side_effects_list))) if side_effects_list else "特になし"
    
    start_date_str = sessions.first().date.strftime('%Y年%m月%d日') if sessions.exists() else "未開始"
    end_date_str = sessions.last().date.strftime('%Y年%m月%d日') if sessions.exists() else "未終了"
    total_count = sessions.count()
    admission_date_str = patient.admission_date.strftime('%Y年%m月%d日') if patient.admission_date else "不明"
    created_at_str = patient.created_at.strftime('%Y年%m月%d日')
    
    summary_text = (
        f"{created_at_str}初診、{admission_date_str}任意入院。\n"
        f"入院時{fmt_score(score_admin)}、{start_date_str}から全{total_count}回のrTMS治療を実施した。\n"
        f"3週時、{fmt_score(score_w3)}、6週時、{fmt_score(score_w6)}となった。\n"
        f"治療中の合併症：{side_effects_summary}。\n"
        f"{end_date_str}退院。紹介元へ逆紹介、抗うつ薬の治療継続を依頼した。"
    )

    return render(request, 'rtms_app/patient_summary.html', {
        'patient': patient, 'summary_text': summary_text, 'today': timezone.now().date(),
    })


# ------------------------------------------------------------------
# 9. 新規患者登録 (現場用)
# ------------------------------------------------------------------
@login_required
def patient_add_view(request):
    if request.method == 'POST':
        form = PatientRegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('dashboard')
    else:
        form = PatientRegistrationForm()
    return render(request, 'rtms_app/patient_add.html', {'form': form})


# ------------------------------------------------------------------
# 10. 管理者用機能
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
    if not request.user.is_staff: return HttpResponse("権限がありません", status=403)
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path):
        return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    return HttpResponse("DBファイルが見つかりません", status=404)