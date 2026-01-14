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
