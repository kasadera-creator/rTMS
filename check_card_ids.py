#!/usr/bin/env python
"""
Check for duplicate card_ids and invalid formats before applying unique constraint.
"""
import os
import sys
import django

os.chdir('/Users/kuniyuki/rTMS')
sys.path.insert(0, '/Users/kuniyuki/rTMS')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from rtms_app.models import Patient
from collections import Counter
import re

print("=== Checking Patient card_id values ===\n")

# Check for invalid formats
patients = Patient.objects.all()
invalid = []
empty = []
valid_5digit = []
other = []

for p in patients:
    if not p.card_id:
        empty.append(p)
    elif re.match(r'^\d{5}$', p.card_id):
        valid_5digit.append(p)
    elif re.match(r'^\d+$', p.card_id):
        invalid.append((p, f"数字だが5桁でない: '{p.card_id}'"))
    else:
        invalid.append((p, f"数字以外の文字を含む: '{p.card_id}'"))

print(f"✓ 5桁数字の患者ID: {len(valid_5digit)} 件")
if empty:
    print(f"⚠ 空のcard_id: {len(empty)} 件")
    for p in empty[:5]:
        print(f"  - Patient ID={p.id}, name={p.name}")
    if len(empty) > 5:
        print(f"  ... 他 {len(empty)-5} 件")

if invalid:
    print(f"\n✗ 無効なフォーマット: {len(invalid)} 件")
    for p, reason in invalid[:10]:
        print(f"  - Patient ID={p.id}, name={p.name}: {reason}")
    if len(invalid) > 10:
        print(f"  ... 他 {len(invalid)-10} 件")

# Check for duplicates
card_ids = [p.card_id for p in valid_5digit if p.card_id]
duplicates = {cid: count for cid, count in Counter(card_ids).items() if count > 1}

if duplicates:
    print(f"\n✗ 重複する患者ID: {len(duplicates)} 件")
    for cid, count in list(duplicates.items())[:10]:
        dups = Patient.objects.filter(card_id=cid)
        print(f"  - card_id='{cid}' が {count} 件:")
        for p in dups:
            print(f"    Patient ID={p.id}, name={p.name}, course={p.course_number}")
    if len(duplicates) > 10:
        print(f"  ... 他 {len(duplicates)-10} 件")
else:
    print("\n✓ 重複なし")

print("\n=== Summary ===")
if empty or invalid or duplicates:
    print("⚠ マイグレーション前に修正が必要です:")
    if empty:
        print(f"  - 空のcard_idを持つ患者に5桁IDを割り当ててください")
    if invalid:
        print(f"  - 無効なフォーマットのcard_idを5桁数字に修正してください")
    if duplicates:
        print(f"  - 重複するcard_idを解消してください（クール数が異なる場合は別のIDを割り当て）")
    sys.exit(1)
else:
    print("✓ 全ての患者IDが有効です。マイグレーションを実行できます。")
    sys.exit(0)
