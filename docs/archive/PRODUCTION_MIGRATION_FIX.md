# 本番環境でのデータベースマイグレーション修正手順

## エラー内容
```
OperationalError at /app/dashboard/
no such column: rtms_app_patient.is_all_case_survey
```

## 原因
ローカル環境で新しいマイグレーション（`0028_remove_patient_protocol_type_and_more.py`）が作成されましたが、本番環境ではまだこのマイグレーションが実行されていません。

## 解決方法

### 方法1: SSH接続での実行（推奨）

本番サーバーに SSH で接続して以下を実行：

```bash
# プロジェクトディレクトリに移動
cd /home/seichiryo/apps/rTMS

# 仮想環境をアクティベート
source /home/seichiryo/miniconda3/envs/rtms311/bin/activate

# ペンディング中のマイグレーションを確認
python manage.py showmigrations rtms_app

# マイグレーション実行
python manage.py migrate rtms_app

# Django check で確認
python manage.py check
```

### 方法2: cPanel/Webホスト経由での実行

1. cPanel -> Terminal/SSH にアクセス
2. 上記のコマンドを実行

### 方法3: 自動修正スクリプト

ローカルで以下を実行し、`fix_production_migration.sh` をサーバーにアップロード：

```bash
chmod +x fix_production_migration.sh
scp fix_production_migration.sh user@seichiryo.jp:/home/seichiryo/apps/rTMS/

# サーバーで実行
ssh user@seichiryo.jp "cd /home/seichiryo/apps/rTMS && bash fix_production_migration.sh"
```

## 実行後の確認

```bash
# ブラウザでダッシュボードにアクセス
https://seichiryo.jp/app/dashboard/

# エラーが解決したことを確認
```

## データベーススキーマ確認（SQLite）

問題がまだ解決しない場合は、データベースダンプで確認：

```bash
# SQLite3 で直接確認
sqlite3 /path/to/db.sqlite3
> .schema rtms_app_patient
> SELECT COUNT(*) FROM rtms_app_patient WHERE is_all_case_survey IS NOT NULL;
```

## 新規フィールド情報

マイグレーション 0028 で追加されたカラム：
- `is_all_case_survey` (BooleanField): デフォルト値 False
- `estimated_onset_year` (IntegerField): 推定発症年（オプション）
- `estimated_onset_month` (IntegerField): 推定発症月（オプション）
- `has_other_psychiatric_history` (CharField): 他の精神疾患既往
- `psychiatric_history` (JSONField): 既往精神疾患
- `psychiatric_history_other_text` (TextField): その他の既往

## よくある問題

### Q: マイグレーション 0027 は適用されているが 0028 が見つからない
**A:** サーバーに最新のコードがアップロードされているか確認。`rtms_app/migrations/0028_*.py` が存在することを確認してください。

```bash
ls -la /home/seichiryo/apps/rTMS/rtms_app/migrations/
```

### Q: Permission Denied でマイグレーション実行できない
**A:** スーパーユーザー権限で実行：
```bash
sudo -u www-data python manage.py migrate rtms_app
```

### Q: 「No such table」エラーが出た
**A:** 0001_initial.py から順に全てのマイグレーションを実行：
```bash
python manage.py migrate rtms_app --plan
python manage.py migrate rtms_app
```

## サポート

問題が解決しない場合は、以下の情報をログに記録してください：

```bash
python manage.py showmigrations rtms_app | head -50
python manage.py migrate rtms_app --verbosity 3 2>&1 | tail -50
sqlite3 /path/to/db.sqlite3 ".schema rtms_app_patient" | grep is_all_case_survey
```
