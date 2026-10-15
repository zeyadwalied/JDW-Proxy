@echo off
title JDW Fix Proxy (AI Server Gateway)
echo ============================================================
echo Starting JDW Proxy on http://127.0.0.1:8181 ...
echo Upstream Server: https://api.justwoker.icu/v1
echo Dashboard: http://127.0.0.1:8181/
echo ============================================================
echo.
cd /d  %~dp0
python jdw_proxy.py
pause
