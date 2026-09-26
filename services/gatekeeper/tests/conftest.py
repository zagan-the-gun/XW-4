"""テストはタイムゾーンに依存する集計（夜間判定・日別集計）を含むので、本番と同じ JST に固定する。"""
import os
import time

os.environ["TZ"] = "Asia/Tokyo"
time.tzset()
