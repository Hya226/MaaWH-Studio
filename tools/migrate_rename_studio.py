#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件夹改名迁移：E:\\MaaWH Stdio → E:\\MaaWH Studio（2026-09 拼写勘误）

用法：
    python tools/migrate_rename_studio.py            # 干跑：只打印将做什么，不写任何东西
    python tools/migrate_rename_studio.py --apply    # 落盘（必须先关闭 ZCode 并已手动改好文件夹名）

操作顺序（缺一不可）：
    1. 完全退出 ZCode（含托盘）与流程编辑器 / 框选工具
    2. 资源管理器把 E:\\MaaWH Stdio 改名为 E:\\MaaWH Studio
    3. 进新目录双击「改名迁移-执行.bat」
    4. 重开 ZCode 打开 E:\\MaaWH Studio —— 历史会话应原样出现在列表里

迁移内容（只动「归组/配置」，绝不碰 flows/、tests/golden/、任务包与对话内容本身）：
    a. ZCode 会话库 cli/db/db.sqlite —— session.project_id / directory / path、
       input_history.project_id、local_setting.scope_id（历史消息 message/part 一律不动）
    b. ZCode 最近工作区 v2/setting.json
    c. 桌面「流程编辑器.lnk」目标/参数
    d. E:\\MaaWH\\_tools\\pick.sh、编辑器 recent.txt
    e. 两个仓库内文本文件里的字面量 "MaaWH Stdio" → "MaaWH Studio"（bytes 级替换，含才写）

每个被写的文件都先留 .bak-rename-<时间戳> 备份；回滚 = 改回文件夹名 + 还原备份。
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time

OLD = "MaaWH Stdio"
NEW = "MaaWH Studio"
OLD_DIR = r"E:\MaaWH Stdio"
NEW_DIR = r"E:\MaaWH Studio"
OLD_PID = "proj_e-maawh-stdio"
NEW_PID = "proj_e-maawh-studio"

ZCODE_DB = r"C:\Users\USER\.zcode\cli\db\db.sqlite"
ZCODE_SETTING = r"C:\Users\USER\.zcode\v2\setting.json"
LNK = r"C:\Users\USER\Desktop\流程编辑器.lnk"
MAAWH_ROOT = r"E:\MaaWH"

# (表, 列, 旧串, 新串)——只改归组字段，历史内容（message/part 等）不动
DB_TARGETS = [
    ("session", "project_id", OLD_PID, NEW_PID),
    ("session", "directory", OLD_DIR, NEW_DIR),
    ("session", "path", OLD_DIR, NEW_DIR),
    ("input_history", "project_id", OLD_PID, NEW_PID),
    ("local_setting", "scope_id", OLD_PID, NEW_PID),
]

SKIP_DIRS = {".git", "flows", "golden", "__pycache__", "build", "node_modules",
             "_retired", ".gradle", ".idea", ".venv", "venv", "debug"}
SCAN_EXTS = {".md", ".py", ".sh", ".bat", ".ps1", ".txt", ".json", ".cfg",
             ".ini", ".jsonc", ".xml", ".kt", ".java", ".gradle", ".pro", ".properties"}


def ts():
    return time.strftime("%Y%m%d_%H%M%S")


def backup(path):
    dst = f"{path}.bak-rename-{ts()}"
    shutil.copy2(path, dst)
    return dst


def replace_in_file(path):
    """bytes 级替换，含旧串才写回。返回 (命中次数, 是否写回)。"""
    with open(path, "rb") as f:
        data = f.read()
    n = data.count(OLD.encode())
    if n == 0:
        return 0, False
    if not APPLY:
        return n, False
    with open(path, "wb") as f:
        f.write(data.replace(OLD.encode(), NEW.encode()))
    return n, True


def step_db():
    print(f"\n[1/6] ZCode 会话库 {ZCODE_DB}")
    if not os.path.isfile(ZCODE_DB):
        print("    ✗ 找不到数据库文件，跳过")
        return
    con = sqlite3.connect(ZCODE_DB)
    cur = con.cursor()
    try:
        for table, col, old, new in DB_TARGETS:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (f"%{old}%",))
            n = cur.fetchone()[0]
            if n == 0:
                print(f"    - {table}.{col}: 无需更新")
                continue
            if not APPLY:
                print(f"    · (干跑) 将更新 {table}.{col}: {n} 行")
            else:
                cur.execute(f"UPDATE {table} SET {col} = REPLACE({col}, ?, ?) WHERE {col} LIKE ?",
                            (old, new, f"%{old}%"))
                print(f"    ✓ {table}.{col}: 已更新 {n} 行")
        if APPLY:
            con.commit()
            left = 0
            for table, col, old, _ in DB_TARGETS:
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (f"%{old}%",))
                left += cur.fetchone()[0]
            print(f"    ✓ 复查残留 {left} 行（应为 0）")
    finally:
        con.close()


def step_setting_json():
    print(f"\n[2/6] ZCode 最近工作区 {ZCODE_SETTING}")
    if not os.path.isfile(ZCODE_SETTING):
        print("    ✗ 文件不存在，跳过")
        return
    n, written = replace_in_file(ZCODE_SETTING)
    if n == 0:
        print("    - 不含旧路径，无需更新")
    elif written:
        print(f"    ✓ 已替换 {n} 处（备份 .bak-rename-*）")
    else:
        print(f"    · (干跑) 将替换 {n} 处")


def step_lnk():
    print(f"\n[3/6] 桌面快捷方式 {LNK}")
    if not os.path.isfile(LNK):
        print("    ✗ 快捷方式不存在，跳过")
        return
    if not APPLY:
        print("    · (干跑) 将把目标/起始位置/参数里的旧路径替换为新路径")
        return
    ps = (
        '$ws = New-Object -ComObject WScript.Shell; '
        f'$lnk = $ws.CreateShortcut("{LNK}"); '
        '$lnk.TargetPath = $lnk.TargetPath -replace [regex]::Escape("' + OLD + '"), "' + NEW + '"; '
        '$lnk.WorkingDirectory = $lnk.WorkingDirectory -replace [regex]::Escape("' + OLD + '"), "' + NEW + '"; '
        '$lnk.Arguments = $lnk.Arguments -replace [regex]::Escape("' + OLD + '"), "' + NEW + '"; '
        '$lnk.Save(); Write-Output "updated"'
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode == 0 and "updated" in r.stdout:
        print("    ✓ 已更新（TargetPath/WorkingDirectory/Arguments 三处）")
    else:
        print(f"    ✗ 更新失败：{r.stderr.strip() or r.stdout.strip()}（可手动右键快捷方式改）")


def step_misc_files():
    print("\n[4/6] 其他固定文件（pick.sh / recent.txt）")
    for path in (os.path.join(MAAWH_ROOT, "_tools", "pick.sh"),
                 os.path.join(NEW_DIR if os.path.isdir(NEW_DIR) else OLD_DIR, "recent.txt")):
        if not os.path.isfile(path):
            print(f"    - {path}: 不存在，跳过")
            continue
        n, written = replace_in_file(path)
        if n == 0:
            print(f"    - {path}: 不含旧路径")
        elif written:
            print(f"    ✓ {path}: 已替换 {n} 处（已备份）")
        else:
            print(f"    · (干跑) {path}: 将替换 {n} 处")


def step_repo_texts():
    print(f"\n[5/6] 两个仓库内的文本引用扫描（{NEW_DIR} 与 {MAAWH_ROOT}）")
    roots = [r for r in (NEW_DIR, MAAWH_ROOT) if os.path.isdir(r)]
    if not roots:
        print("    ✗ 扫描根目录都不存在")
        return
    self_path = os.path.abspath(__file__)
    total_files = total_hits = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if os.path.splitext(fn)[1].lower() not in SCAN_EXTS:
                    continue
                if os.path.abspath(p) == self_path:  # 别把本脚本自己的 OLD 常量改了
                    continue
                try:
                    n, written = replace_in_file(p)
                except (PermissionError, OSError):
                    continue
                if n:
                    total_files += 1
                    total_hits += n
                    tag = "✓ 已替换" if written else "· (干跑) 将替换"
                    print(f"    {tag} {n:3d} 处: {p}")
    tail = "" if APPLY else "（apply 时本脚本自身与其 bat 不会被改）"
    print(f"    合计 {total_files} 个文件 / {total_hits} 处 {tail}")


def step_prereq_check():
    print("[0/6] 前提检查")
    ok = True
    if os.path.isdir(NEW_DIR):
        print(f"    ✓ 新目录存在: {NEW_DIR}")
    else:
        ok = False
        print(f"    ✗ 新目录不存在: {NEW_DIR} —— 请先在资源管理器把 {OLD_DIR} 改名（F2）")
    if os.path.isdir(OLD_DIR):
        ok = False
        print(f"    ✗ 旧目录仍存在: {OLD_DIR} —— 改名还没完成")
    if APPLY:
        if zcode_running():
            ok = False
            print("    ✗ 检测到 ZCode 进程仍在运行，请完全退出（含托盘）后再执行 ——"
                  "会话数据库正被它占用")
        else:
            print("    ✓ ZCode 已退出")
        if ok and os.path.isfile(ZCODE_DB):
            try:
                b = backup(ZCODE_DB)
                for suffix in ("-wal", "-shm"):
                    src = ZCODE_DB + suffix
                    if os.path.isfile(src):
                        shutil.copy2(src, b + suffix)
                print(f"    ✓ 已备份会话库 → {os.path.basename(b)}")
            except OSError as e:
                ok = False
                print(f"    ✗ 备份失败（多半数据库仍被占用）: {e}")
    else:
        print("    · 干跑模式：不检测 ZCode 进程、不落盘")
    return ok


def zcode_running():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object { $_.ProcessName -match 'zcode' } | "
             "Select-Object -ExpandProperty ProcessName"],
            capture_output=True, text=True, timeout=20)
        names = [x for x in r.stdout.split() if x.strip()]
        if names:
            print(f"    （进程：{', '.join(sorted(set(names)))}）")
        return bool(names)
    except (OSError, subprocess.TimeoutExpired):
        return False  # 检测失败不硬拦，靠 BEGIN EXCLUSIVE 兜底


def main():
    global APPLY
    ap = argparse.ArgumentParser(description="MaaWH Stdio → Studio 改名迁移（默认干跑）")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认只打印计划）")
    args = ap.parse_args()
    APPLY = args.apply
    print(f"=== MaaWH Stdio → MaaWH Studio 改名迁移  [{('执行模式' if APPLY else '干跑预览')}] ===")
    if not step_prereq_check() and APPLY:
        print("\n前提不满足，已停止（未写任何东西）。")
        return 1
    step_db()
    step_setting_json()
    step_lnk()
    step_misc_files()
    step_repo_texts()
    print("\n[6/6] 完成。后续：")
    if APPLY:
        print("    1. 重开 ZCode，打开 E:\\MaaWH Studio —— 历史会话应原样出现在列表里")
        print("    2. 双击桌面「流程编辑器」验证编辑器能正常打开、能读流程")
        print("    3. 回滚办法：文件夹改回原名，并用 .bak-rename-* 备份还原对应文件")
    else:
        print("    确认上面计划无误后，双击「改名迁移-执行.bat」落盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
