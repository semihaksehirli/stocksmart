@echo off
rem Set up and start StockSmart on Windows: http://localhost:7860
cd /d "%~dp0"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if not exist stocksmart.db python -m app.seed
python main.py
