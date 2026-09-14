# -*- coding: utf-8 -*-
"""定位 MaaWH 任务包工程根（含 whmx/ 的那个目录）。

流程编辑器、模板框选工具已经从 MaaWH 仓库搬到本目录（与仓库平级），
所以「工程根 = 本目录的上一级」这条老规则不再成立。工程根按以下顺序探测：

  1. 环境变量 MAAWH_ROOT —— 临时指向别的任务包时用
  2. 本目录 project_root.txt 里第一行有效路径 —— 默认已写好 E:\\MaaWH
  3. 本目录的兄弟目录 / 上级目录里带 whmx/image 的那个 —— 与仓库并排时自动命中
  4. 常见安装位置 E:\\MaaWH、D:\\MaaWH

工程根只用来读写任务包：whmx/image 模板图、whmx/pipeline 生成物、_tools/neg_frames 负样本。
编辑器自身的文件（flows/、日志、背景帧、recent.txt）一律放本目录，即 EDITOR_ROOT。
"""
import os

EDITOR_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(EDITOR_ROOT, "project_root.txt")
FALLBACK_ROOTS = (r"E:\MaaWH", r"D:\MaaWH")


def looks_like_pack(path):
    """任务包判据：该目录下存在 whmx/image"""
    return bool(path) and os.path.isdir(os.path.join(path, "whmx", "image"))


def _candidates():
    """按优先级产出 (来源说明, 候选根目录)"""
    env = os.environ.get("MAAWH_ROOT", "").strip().strip('"')
    if env:
        yield "环境变量 MAAWH_ROOT", env

    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip().strip('"')
                    if line and not line.startswith("#"):
                        yield "project_root.txt", line
                        break
        except OSError:
            pass

    # 兄弟目录（本目录旁边）与上级目录：兼容「编辑器还放在 MaaWH 内部」的旧布局
    parent = os.path.dirname(EDITOR_ROOT)
    for base, why in ((parent, "兄弟目录"), (os.path.dirname(parent), "上级目录")):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            p = os.path.join(base, name)
            if p != EDITOR_ROOT and looks_like_pack(p):
                yield why, p

    for r in FALLBACK_ROOTS:
        yield "默认位置", r


def find_project_root():
    for why, path in _candidates():
        if looks_like_pack(path):
            return path, why
    return FALLBACK_ROOTS[0], "未找到"


PROJECT_ROOT, ROOT_SOURCE = find_project_root()
PACK_OK = looks_like_pack(PROJECT_ROOT)
WHMX_DIR = os.path.join(PROJECT_ROOT, "whmx")
IMG_DIR = os.path.join(WHMX_DIR, "image")
PIPE_DIR = os.path.join(WHMX_DIR, "pipeline")
NEG_DIR = os.path.join(PROJECT_ROOT, "_tools", "neg_frames")


def describe():
    if PACK_OK:
        return f"任务包工程根：{PROJECT_ROOT}（来源：{ROOT_SOURCE}）"
    return (f"⚠ 未找到 MaaWH 任务包，当前按 {PROJECT_ROOT} 处理（来源：{ROOT_SOURCE}）。"
            f"请把 {CONFIG_FILE} 改成 MaaWH 仓库的实际路径。")


if __name__ == "__main__":
    print(describe())
    print("编辑器目录:", EDITOR_ROOT)
    print("模板图目录:", IMG_DIR)
    print("流水线目录:", PIPE_DIR)
    print("负样本目录:", NEG_DIR)
