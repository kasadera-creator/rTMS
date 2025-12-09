from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
from .models import Patient, TreatmentSession
import csv
from django.http import HttpResponse, FileResponse
from django.conf import settings
import os

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
    
@login_required
def export_treatment_csv(request):
    """治療記録をCSVで出力（研究用）"""
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig') # Excelで文字化けしないおまじない
    response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'

    writer = csv.writer(response)
    # ヘッダー
    writer.writerow(['ID', 'Patient Name', 'Date', 'MT(%)', 'Intensity(%)', 'Pulses', 'Safety(Sleep)', 'Adverse Events'])

    # データ行
    treatments = TreatmentSession.objects.all().select_related('patient').order_by('date')
    for t in treatments:
        # 有害事象JSONを文字列化して格納
        adverse_str = str(t.adverse_events) if t.adverse_events else ""
        
        writer.writerow([
            t.patient.card_id,
            t.patient.name,
            t.date.strftime('%Y-%m-%d %H:%M'),
            t.motor_threshold,
            t.intensity,
            t.total_pulses,
            'OK' if t.safety_sleep else 'NG',
            adverse_str
        ])
    return response

@login_required
def download_db(request):
    """現在のSQLiteデータベースファイルをダウンロード（簡易バックアップ）"""
    if not request.user.is_superuser:
        return HttpResponse("管理者のみ実行可能です", status=403)
        
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path):
        return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    else:
        return HttpResponse("DBファイルが見つかりません", status=404)