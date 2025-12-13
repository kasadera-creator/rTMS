#!/usr/bin/env python3
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # tools/ の1つ上=プロジェクトroot想定
URLS_PY = ROOT / "rtms_app" / "urls.py"
TEMPLATE_DIR = ROOT / "rtms_app" / "templates"

# urls.py から name="xxx" を抽出
name_re = re.compile(r'name\s*=\s*["\']([^"\']+)["\']')
urls_text = URLS_PY.read_text(encoding="utf-8")
defined_names = set(name_re.findall(urls_text))

# templates から {% url 'rtms_app:xxx' %} を抽出
# 例: {% url 'rtms_app:dashboard' %} / {% url "rtms_app:patient_home" patient.id %}
tpl_url_re = re.compile(r"""{%\s*url\s+["']rtms_app:([^"']+)["']""")

missing = []
for p in TEMPLATE_DIR.rglob("*.html"):
    text = p.read_text(encoding="utf-8", errors="ignore")
    for m in tpl_url_re.finditer(text):
        nm = m.group(1)
        if nm not in defined_names:
            missing.append((nm, str(p.relative_to(ROOT))))

# 結果表示
missing.sort()
print("=== URL name check (namespace rtms_app) ===")
print(f"Defined in urls.py: {len(defined_names)}")
print(f"Referenced in templates: {len(missing)} missing references\n")

if not missing:
    print("OK: templates 参照のURL名はすべて urls.py に存在します。")
else:
    print("NG: urls.py に存在しないURL名が templates にあります：\n")
    for nm, fp in missing:
        print(f"- {nm:30s}  in {fp}")

    print("\n対処: urls.py に name を追加するか、template の url 名を修正してください。")
