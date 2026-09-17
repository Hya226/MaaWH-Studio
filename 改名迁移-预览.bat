@echo off
chcp 65001 >nul
title MaaWH Stdio 改名迁移（预览）
python "%~dp0tools\migrate_rename_studio.py"
echo.
pause
