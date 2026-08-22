#!/usr/bin/env python
"""Run makemigrations in a subprocess"""
import subprocess
import sys

result = subprocess.run(
    [sys.executable, 'manage.py', 'makemigrations', 'rtms_app'],
    cwd='/Users/kuniyuki/rTMS',
    capture_output=True,
    text=True
)

print("=== STDOUT ===")
print(result.stdout)
print("\n=== STDERR ===")
print(result.stderr)
print(f"\n=== Return code: {result.returncode} ===")
sys.exit(result.returncode)
