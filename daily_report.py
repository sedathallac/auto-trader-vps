#!/usr/bin/env python3
"""daily_report.py - Gunluk PnL raporu (dosya tabanli)"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPORTS_DIR = Path('/root/reports')
RETENTION_DAYS = 30

def main():
    REPORTS_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d')
    out_path = REPORTS_DIR / f'daily-{today}.txt'

    try:
        result = subprocess.run(
            ['/root/venv/bin/python', '/root/archive_report.py'],
            capture_output=True, text=True, timeout=60
        )
        content = result.stdout
        if result.returncode != 0:
            content += '\n--- STDERR ---\n' + result.stderr
    except Exception as e:
        content = f'RAPOR HATASI: {e}\n'

    out_path.write_text(content)
    print(f'Rapor yazildi: {out_path}')
    print(f'Boyut: {out_path.stat().st_size} byte')

    # Eski raporlari temizle
    cutoff = datetime.now().timestamp() - (RETENTION_DAYS * 86400)
    removed = 0
    for f in REPORTS_DIR.glob('daily-*.txt'):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    print(f'Temizlenen eski rapor: {removed}')

if __name__ == '__main__':
    main()
