from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
from .models import Patient, TreatmentSession

@login_required
def dashboard_view(request):
    """
    現場用ダッシュボード
    - 全患者の治療進捗一覧
    - アラート表示 (MT再測定期限、定期評価時期)
    """
    today = timezone.now().date()
    patients_data = []

    # 全患者を取得
    patients = Patient.objects.all().order_by('card_id')

    for p in patients:
        # 1. 治療開始日と経過日数の計算
        sessions = TreatmentSession.objects.filter(patient=p).order_by('date')
        first_session = sessions.first()
        last_session = sessions.last()
        
        days_elapsed = 0
        start_date = None
        
        if first_session:
            start_date = first_session.date.date()
            days_elapsed = (today - start_date).days

        # 2. アラート判定
        alerts = []
        
        # A. MT(運動閾値)の有効期限チェック (ガイドライン: 最低週1回再測定 [cite: 31])
        # 最終測定日を取得（とりあえず最終セッション日を基準とします）
        days_since_last_mt = 0
        if last_session:
            last_mt_date = last_session.date.date()
            days_since_last_mt = (today - last_mt_date).days
            
            # 7日以上空いていたら警告
            if days_since_last_mt >= 7:
                alerts.append({
                    'level': 'danger', 
                    'msg': f'MT再測定期限切れ ({days_since_last_mt}日経過)'
                })
            elif days_since_last_mt >= 5:
                alerts.append({
                    'level': 'warning', 
                    'msg': 'そろそろMT再測定'
                })

        # B. 定期評価の時期チェック (3週目=21日, 6週目=42日)
        # 前後3日間を「評価期間」として表示
        if first_session:
            if 18 <= days_elapsed <= 24:
                alerts.append({'level': 'info', 'msg': '★3週目の評価時期です'})
            elif 39 <= days_elapsed <= 45:
                alerts.append({'level': 'info', 'msg': '★6週目の評価時期です'})

        # データをリストに格納
        patients_data.append({
            'obj': p,
            'start_date': start_date,
            'days_elapsed': days_elapsed,
            'last_mt_days': days_since_last_mt,
            'alerts': alerts,
        })

    context = {
        'patients_data': patients_data,
        'today': today,
    }
    return render(request, 'rtms_app/dashboard.html', context)