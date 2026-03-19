@echo off
setlocal
cd /d %~dp0
chcp 65001 >nul
echo Starting mobile workflow tool on http://127.0.0.1:5000
python app.py
