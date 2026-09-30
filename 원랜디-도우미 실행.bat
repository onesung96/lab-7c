@echo off
chcp 65001 >nul
title ORDR auto counter
cd /d "%~dp0"
py -3 ordr_helper.py
pause
