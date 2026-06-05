#!/bin/bash
# 더블클릭으로 Mac Disk Cleaner 실행. 종료는 이 창에서 Ctrl+C.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
exec /usr/bin/python3 disk_cleaner.py
