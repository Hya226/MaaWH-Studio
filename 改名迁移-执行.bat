@echo off
chcp 65001 >nul
title MaaWH Studio 改名迁移（执行）
python "%~dp0tools\migrate_rename_studio.py" --apply
echo.
pause
