from .base import *

DEBUG = True

# ▼ 修正1：ドメインを正しく記述（カンマの位置に注意）
# 以前の ",seichiryo.jp" という記述ミスを修正しました
ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "rtms.local",
    "seichiryo.jp",       # ここが重要
    "www.seichiryo.jp"    # www ありも念のため追加
]

# ▼ 修正2：SECRET_KEYを強制的に設定
# .env読み込みに失敗しても動くように、ここに直接書きます
SECRET_KEY = "3NEjM25yrLww1MiJxT_FE3yhh5eQYeZg1O47likj_YTi5Wq10fBC3mfbvQIF2F69"