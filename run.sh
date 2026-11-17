#!/usr/bin/env bash
# Set up and start StockSmart on macOS/Linux: http://localhost:7860
set -e
cd "$(dirname "$0")"
PY=python3
for v in python3.13 python3.12 python3.11 python3.10; do command -v $v >/dev/null && { PY=$v; break; }; done
[ -d .venv ] || $PY -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
[ -f stocksmart.db ] || python -m app.seed
python main.py
