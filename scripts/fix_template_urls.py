#!/usr/bin/env python3
import re
import shutil
from pathlib import Path
from datetime import datetime

TEMPLATES_DIR = Path("rtms_app/templates")

# 1) 単純に namespace を付ける対象（存在するURL名のみ）
NAMESPACE_PREFIX = "rtms_app:"
VALID_NAMES = {
    "dashboard",
    "patient_list",
    "patient_add",
    "patient_home",
    "patient_first_visit",
    "admission_procedure",
    "mapping_add",
    "treatment_add",
    "assessment_add",
    "patient_clinical_path",
    "print_clinical_path",
    "patient_print_bundle",
}

# 2) 存在しない name を、現行設計に合わせて置換するマップ
# - patient_detail -> patient_home
# - patient_print_path -> print_clinical_path
RENAME_MAP = {
    "patient_detail": "patient_home",
    "patient_print_path": "print_clinical_path",
    # patient_print_preview / patient_print_summary は「URL名置換」では済まないので別処理（後述）
}

# 3) patient_print_preview を bundle に寄せる（具体パターン置換）
#   {% url 'patient_print_preview' patient.id %}?mode=summary
# → {% url 'rtms_app:patient_print_bundle' patient.id %}?docs=admission
PRINT_PREVIEW_PATTERNS = [
    # summary -> admission
    (
        re.compile(r"""{%\s*url\s*'patient_print_preview'\s+([^%]+?)\s*%}\?mode=summary"""),
        r"""{% url 'rtms_app:patient_print_bundle' \1 %}?docs=admission""",
    ),
    # questionnaire -> suitability
    (
        re.compile(r"""{%\s*url\s*'patient_print_preview'\s+([^%]+?)\s*%}\?mode=questionnaire"""),
        r"""{% url 'rtms_app:patient_print_bundle' \1 %}?docs=suitability""",
    ),
]

# 4) patient_print_summary（JS内）を bundle に寄せる（最小修正）
#   const printUrl = "{% url 'patient_print_summary' patient.id %}?mode=" + mode;
# → const printUrl = "{% url 'rtms_app:patient_print_bundle' patient.id %}?docs=" + mode;
PRINT_SUMMARY_PATTERN = (
    re.compile(r"""{%\s*url\s*'patient_print_summary'\s+([^%]+?)\s*%}\?mode="""),
    r"""{% url 'rtms_app:patient_print_bundle' \1 %}?docs=""",
)

# 5) custom_logout は urls.py に無いので、最短復旧として /admin/logout/ に置換（reverse不要）
CUSTOM_LOGOUT_PATTERN = (
    re.compile(r"""{%\s*url\s*'custom_logout'\s*%}"""),
    r"""/admin/logout/""",
)

URL_TAG_RE = re.compile(r"""{%\s*url\s*'([^']+)'\s*([^%]*?)%}""")

def needs_namespace(name: str) -> bool:
    return (name in VALID_NAMES) and (not name.startswith(NAMESPACE_PREFIX))

def apply_namespace(name: str) -> str:
    return f"{NAMESPACE_PREFIX}{name}"

def process_text(text: str):
    changed = False
    logs = []

    # A) patient_print_preview の特殊置換（先にやる）
    for cre, rep in PRINT_PREVIEW_PATTERNS:
        new_text, n = cre.subn(rep, text)
        if n:
            text = new_text
            changed = True
            logs.append(f"patient_print_preview pattern replaced x{n}")

    # B) patient_print_summary の最小修正（JS内）
    new_text, n = PRINT_SUMMARY_PATTERN[0].subn(PRINT_SUMMARY_PATTERN[1], text)
    if n:
        text = new_text
        changed = True
        logs.append(f"patient_print_summary -> patient_print_bundle (?docs=) x{n}")

    # C) custom_logout を /admin/logout/ に（最短復旧）
    new_text, n = CUSTOM_LOGOUT_PATTERN[0].subn(CUSTOM_LOGOUT_PATTERN[1], text)
    if n:
        text = new_text
        changed = True
        logs.append(f"custom_logout -> /admin/logout/ x{n}")

    # D) URLタグの name を rename / namespace 付与
    def repl(m: re.Match):
        nonlocal changed
        name = m.group(1)
        args = m.group(2)

        orig_name = name

        # rename map
        if name in RENAME_MAP:
            name = RENAME_MAP[name]
            changed = True
            logs.append(f"rename: {orig_name} -> {name}")

        # namespace付与（VALID_NAMESのみ）
        if needs_namespace(name):
            name = apply_namespace(name)
            changed = True
            logs.append(f"namespace: {orig_name} -> {name}")

        return "{% url '" + name + "' " + args + "%}"

    # 注意：上の repl は args を末尾スペース込みで持つので、整形はしない
    new_text = URL_TAG_RE.sub(repl, text)
    if new_text != text:
        text = new_text
        changed = True

    return text, changed, logs

def main():
    if not TEMPLATES_DIR.exists():
        raise SystemExit(f"Templates dir not found: {TEMPLATES_DIR.resolve()}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(f"_backup_templates_{ts}")
    shutil.copytree(TEMPLATES_DIR, backup_dir)
    print(f"[OK] Backup created: {backup_dir.resolve()}")

    target_files = list(TEMPLATES_DIR.rglob("*.html"))
    modified = 0
    report = []

    for f in target_files:
        text = f.read_text(encoding="utf-8")
        new_text, changed, logs = process_text(text)
        if changed and new_text != text:
            f.write_text(new_text, encoding="utf-8")
            modified += 1
            report.append((str(f), logs))

    print(f"[DONE] Modified files: {modified} / {len(target_files)}")
    if report:
        print("\n--- Changes report ---")
        for path, logs in report:
            uniq = []
            for x in logs:
                if x not in uniq:
                    uniq.append(x)
            print(f"\n{path}")
            for x in uniq:
                print(f"  - {x}")

    # 残存チェック：未namespacedの dashboard など
    leftovers = []
    for f in target_files:
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"""{%\s*url\s*'([^']+)'\s*""", text):
            name = m.group(1)
            if name in VALID_NAMES:
                # VALID_NAMES なのに namespace が付いてないのは漏れ
                if not name.startswith(NAMESPACE_PREFIX):
                    leftovers.append((str(f), name))
    if leftovers:
        print("\n[WARN] Leftover non-namespaced url tags found:")
        for p, n in leftovers[:50]:
            print(f"  {p}: {n}")
        print("  ... (showing up to 50)")
        print("Please re-run grep and check manually.")
    else:
        print("\n[OK] No leftover non-namespaced url tags for VALID_NAMES.")

if __name__ == "__main__":
    main()
