# -*- coding: utf-8 -*-
"""
可视化流程编辑器：拖拽小任务节点 → 箭头连线 → 生成 MaaFramework pipeline JSON → 一键同步手机测试。

与 template_picker.py 的分工：
  template_picker  负责从帧上框出模板图（whmx/image/*.png，只读）
  flow_editor      负责把「模板点击 / 固定点击 / 滑动 / 分支 / 等待模板 / 公共节点 / 启动游戏」
                   拼成完整流程，生成节点名带 VF_ 前缀的 pipeline 文件并推送到手机。

不改动项目文件：流程定义存 本目录/flows/，生成物存 本目录/flows/build/，
同步时只向手机 files/taskpacks/whmx/pipeline/ 新增 vf_*.json；
测试运行走宿主直达入口 `--es entry VF_<流程名> --ez vd true`（自动建虚拟屏后跑该入口）。

本工具已独立于 MaaWH 仓库（本目录 = E:/MaaWH Stdio），任务包工程根由 project_paths.py
探测（环境变量 MAAWH_ROOT → project_root.txt → 兄弟/上级目录 → 默认位置），
模板图仍写进 MaaWH 的 whmx/image，生成物仍回写 whmx/pipeline。

用法：
  双击桌面「流程编辑器」快捷方式，或 本目录/启动.bat
  python flow_editor.py [某流程.flow.json]
  python flow_editor.py --selftest
"""
import os
import re
import sys
import json
import glob
import time
import bisect
import hashlib
import datetime
import subprocess
import shutil
import threading
import queue
from dataclasses import dataclass
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

try:
    import cv2  # noqa: F401  与 template_picker 同依赖，缺失时提前报错
except ImportError:
    print("需要 opencv-python：pip install opencv-python")
    sys.exit(1)

from PIL import Image, ImageTk

# TOOLS_DIR = 本工具所在目录（编辑器自身：flows/、日志、背景帧都在这里）
# ROOT = MaaWH 任务包工程根，由 project_paths 探测，编辑器放哪里都能找到任务包
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)
import project_paths  # noqa: E402

ROOT = project_paths.PROJECT_ROOT
FLOWS_DIR = os.path.join(TOOLS_DIR, "flows")
BUILD_DIR = os.path.join(FLOWS_DIR, "build")
IMG_DIR = project_paths.IMG_DIR
NEG_DIR = project_paths.NEG_DIR
ADB = r"D:\android-studio\Sdk\platform-tools\adb.exe"
DEVICE = "2c92e197"
PKG = "com.maawh.app"
GAME_PKG = "com.cipaishe.wuhua.bilibili"

EDITOR_VERSION = "2.0"      # 写进生成物的 $meta，便于回溯是哪一版编辑器产出的

# 虚拟屏帧基准（横屏 720p，实测 1280x720）：坐标校验/显示用；载入背景帧后按实际图尺寸更新
FRAME_W, FRAME_H = 1280, 720
FRAME_DISP_H = 880          # 竖屏帧的画布显示高度；横屏帧自动改用 FRAME_DISP_H_LS
FRAME_DISP_H_LS = 540       # 横屏帧的画布显示高度（按宽度适配，约 960 宽）
CARD_W, CARD_H = 240, 60    # 节点卡片尺寸
UNDO_LIMIT = 60             # 撤销栈上限（存的是流程定义 JSON 文本）
LOOP_MAX_TIMES = 50         # 循环展开次数上限（防止一次生成把 JSON 撑到手机端加载不动）
ZOOM_MIN, ZOOM_MAX = 0.25, 2.5   # 画布缩放范围（下限小一点便于总览 40 节点的长流程）

# ---------------- 主题 ----------------

THEME = {
    "bg":       "#171a23",   # 窗口/顶栏
    "panel":    "#1e222d",   # 侧栏面板
    "canvas":   "#14161d",   # 画布
    "grid":     "#1c2030",   # 画布网格线
    "field":    "#252a38",   # 输入框底
    "card":     "#262b38",   # 节点卡片底
    "card_hi":  "#2e3444",   # hover
    "card_line":"#3a4152",   # 卡片描边
    "shadow":   "#0c0e14",   # 卡片阴影
    "text":     "#e6e9f0",
    "text_dim": "#98a1b3",
    "accent":   "#5b8cff",
    "sel":      "#ffd24d",
    "arrow":    "#5a647e",
    "ok":       "#58c470",
    "err":      "#e05a5a",
    "warn":     "#e8c94d",
}

FONT      = ("Microsoft YaHei UI", 9)
FONT_B    = ("Microsoft YaHei UI", 9, "bold")
FONT_SM   = ("Microsoft YaHei UI", 8)
FONT_TITLE= ("Microsoft YaHei UI", 10, "bold")
LOG_FONT  = ("Consolas", 9)
ARROW_SHAPE = (11, 13, 4)

# 公共节点层（whmx/pipeline/common.json）可引用的收口节点
# 公共节点层（whmx/pipeline/common.json）里可引用的收口节点。
# 这里是【回退清单】：优先用 common_node_names() 从任务包动态读，
# 免得 common.json 加了新节点还得回来改 Python。
COMMON_NODES = [
    "Common_回主页",
    "Common_关弹窗",
    "Common_点中间",
    "Common_点中间回主页",
    "Common_回主页验证",
    "Common_确认",
    "Common_返回",
    "Common_开始训练",
]


def common_node_names(root=None):
    """公共节点清单（可引用的入口节点）。
    从 whmx/pipeline/common.json 读全部 Common_* 键，剔除内部子节点：
      · 被某个「带 on_error 的分叉容器」当作 next 的（那是容器的命中子节点）
      · 被 Or/And 的 any_of/all_of 引用的（那是组合识别的子项）
      · 名字以 _完成 / _重试 结尾的（收口与兜底重试，不该直接跳进去）
    这些都是结构判定，不靠名字前缀约定 —— common.json 里
    Common_弹窗_Hit / Common_主页校验 这种子节点并不遵循前缀规律。
    读不到任务包时回退到内置 COMMON_NODES。"""
    root = root or ROOT
    path = os.path.join(root, "whmx", "pipeline", "common.json")
    keys, children = [], set()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = jsonc_loads(f.read())
        except Exception:
            data = None
        if isinstance(data, dict):
            keys = [str(k) for k in data if str(k).startswith("Common_")]
            for nd in data.values():
                if not isinstance(nd, dict):
                    continue
                nexts = nd.get("next") or []
                if nd.get("on_error"):
                    for it in (nexts if isinstance(nexts, list) else [nexts]):
                        nm = it.get("name") if isinstance(it, dict) else it
                        if nm:
                            children.add(_strip_attr(nm))
                for key in ("any_of", "all_of"):
                    for it in (nd.get(key) or []):
                        nm = it.get("name") if isinstance(it, dict) else it
                        if nm:
                            children.add(_strip_attr(nm))
    entries = [k for k in keys
               if k not in children and not k.endswith(("_完成", "_重试"))]
    return sorted(entries) or list(COMMON_NODES)


# 按节点类型给出的固定提示（与 _hide_hint 的互斥提示互补）
NODE_HINTS = {
    "wait_tpl": "本节点只「等模板出现」，不做任何动作。需要未命中时走另一条路，"
                "请改用『分支(模板在?)』节点（它有 ✓/✗ 两个出口）。",
    "switch": "候选按从上到下的顺序判定，命中即走该候选的内容，内容跑完枝干就结束；"
              "全部未中走「✗全未中」出口。",
    "common": "公共节点是「收口」：进入后本流程即终止，不会返回。放在链中间会让"
              "其后的节点执行不到。",
    "subflow": "把另一个流程的节点整段【内联】进来：生成时它的节点会带上 "
               "VF_<本流程>_<本节点key>_ 前缀，跑完接回本流程的下一个节点。"
               "子流程结尾若是公共收口节点则不会返回；不能引用自己或成环。",
    "loop": "循环体 = 链上从本节点之后一直到「循环体末尾」那一段；生成时"
            "【复制 times 份】逐个首尾相接，不依赖引擎特性（可选 1~50 次）。",
}


def _hide_hint(ntype, p):
    """互斥生效时给出说明，避免字段突然消失让人困惑；否则给出该类型的固定提示"""
    if ntype == "branch" and str(p.get("ocr_text", "")).strip():
        return "已填 OCR 文本 → 本节点改用 OCR 判定，模板图与阈值不生效"
    return NODE_HINTS.get(ntype)

def tpl_summary(p):
    """节点卡片摘要：模板多候选显示 '首个 +N'，单值直接显示"""
    parts = split_tpls(p.get("template", ""))
    if len(parts) > 1:
        return f"{parts[0]} +{len(parts) - 1}"
    return parts[0] if parts else "?"


def parse_switch_cands(raw):
    """解析枝干候选列表：
    raw 为 [{t, timeout, next, mergeBack}, ...]；t 支持 '模板名.png'（模板识别）或 'OCR:文字'。
    返回规范化列表，剔除空候选；next = 命中内容起点节点 id（可缺省）；
    mergeBack = 命中内容跑完后是否回到主线（默认 False，与历史行为一致）。"""
    out = []
    if not raw:
        return out
    for c in raw:
        if not isinstance(c, dict):
            continue
        t = str(c.get("t", "")).strip()
        if not t:
            continue
        try:
            timeout = int(float(c.get("timeout", 3000)))
        except (TypeError, ValueError):
            timeout = 3000
        item = {"t": t, "timeout": max(500, timeout)}
        if c.get("next"):
            item["next"] = c["next"]
        if c.get("mergeBack"):
            item["mergeBack"] = True
        out.append(item)
    return out


def switch_summary(p):
    """卡片摘要：'候选数 个 · 未中出口'"""
    cands = parse_switch_cands(p.get("candidates"))
    return f"{len(cands)} 路" if cands else "空枝干"


def switch_cand_spec(t):
    """候选识别规格：('OCR', text) 或 ('Template', name)；非法返回 None"""
    t = str(t).strip()
    if t.lower().startswith("ocr:"):
        return ("OCR", t[4:].strip())
    if t.endswith(".png"):
        return ("Template", t)
    return None


# ---------------- 节点类型定义 ----------------
# fields: (props键, 标签, 控件类型)
# 控件类型: tpl模板下拉 / common公共节点下拉 / bool / float / int / str / roi / pick取点 / pick2取点(终点)

NODE_TYPES = {
    "ocr_click": {
        "label": "OCR识别点击", "icon": "🔍", "color": "#3a6ea5", "light": "#8fc1f0",
        "summary": lambda p: str(p.get("text", "?")).replace("，", ","),
        "fields": [
            ("text", "识别文本(多个用,分隔;支持正则)", "str"),
            ("roi", "ROI x,y,w,h (空=全屏)", "roi"),
            ("threshold", "置信度(空=引擎默认0.3)", "float_opt"),
            ("order_by", "结果排序(空=默认)", "choice",
             ("Horizontal", "Vertical", "Area", "Length", "Random", "Expected")),
            ("index", "命中第几个(-N~N-1,空=0)", "int_opt"),
            ("replace", "易错字替换(错=对,错2=对2)", "str_opt"),
            ("only_rec", "仅识别不检测(需精确ROI)", "bool"),
            ("timeout", "等待超时ms", "int"),
            ("rate_limit", "识别间隔ms", "int"),
            ("pre_delay", "点击前延时ms", "int"),
            ("post_delay", "点击后延时ms", "int"),
        ],
        "defaults": {"text": "", "roi": "", "timeout": 8000, "rate_limit": 0,
                     "pre_delay": 0, "post_delay": 600},
    },
    "tpl_click": {
        "label": "找模板点击", "icon": "◉", "color": "#4a7dbd", "light": "#8fb8ec",
        "summary": tpl_summary,
        "fields": [
            ("template", "模板图(逗号分隔多候选)", "tpl_multi"),
            ("threshold", "阈值(0.6~0.95)", "float"),
            ("roi", "ROI x,y,w,h (空=全屏)", "roi"),
            ("order_by", "结果排序(空=最左;精确点单个选Score)", "choice",
             ("Horizontal", "Vertical", "Score", "Random")),
            ("index", "命中第几个(-N~N-1,空=0)", "int_opt"),
            ("timeout", "等待超时ms", "int"),
            ("rate_limit", "识别间隔ms(移动目标建议200)", "int"),
            ("pre_delay", "点击前延时ms", "int"),
            ("post_delay", "点击后延时ms", "int"),
            ("repeat", "重复点击次数", "int"),
            ("repeat_delay", "重复间隔ms", "int"),
            ("post_wait_freezes", "点击后等画面静止ms(0=关)", "int"),
        ],
        "defaults": {"threshold": 0.8, "roi": "", "order_by": False, "timeout": 8000,
                     "rate_limit": 0, "pre_delay": 0, "post_delay": 800,
                     "post_wait_freezes": 0, "repeat": 1, "repeat_delay": 350},
    },
    "tap": {
        "label": "固定坐标点击", "icon": "✛", "color": "#5f6fae", "light": "#a3b1e8",
        "summary": lambda p: f"({p.get('x', 0)},{p.get('y', 0)})",
        "fields": [
            ("x", "X", "pick"),
            ("y", "Y", "int"),
            ("pre_delay", "点击前延时ms", "int"),
            ("post_delay", "点击后延时ms", "int"),
            ("repeat", "重复点击次数", "int"),
            ("repeat_delay", "重复间隔ms", "int"),
            ("post_wait_freezes", "点击后等画面静止ms(0=关)", "int"),
            ("timeout", "等待超时ms(空=继承90000)", "int_opt"),
        ],
        "defaults": {"x": 640, "y": 360, "pre_delay": 0, "post_delay": 500,
                     "post_wait_freezes": 0, "repeat": 1, "repeat_delay": 350},
    },
    "swipe": {
        "label": "滑动", "icon": "⇅", "color": "#8a63b0", "light": "#c8a8ec",
        "summary": lambda p: f"({p.get('x1', 0)},{p.get('y1', 0)})→({p.get('x2', 0)},{p.get('y2', 0)})",
        "fields": [
            ("x1", "起点X", "pick"),
            ("y1", "起点Y", "int"),
            ("x2", "终点X", "pick2"),
            ("y2", "终点Y", "int"),
            ("duration", "时长ms(推荐900)", "int"),
            ("repeat", "滑动次数", "int"),
            ("repeat_delay", "滑动间隔ms", "int"),
            ("pre_wait_freezes", "滑动前等画面静止ms(0=关)", "int"),
            ("post_wait_freezes", "滑动后等画面静止ms(0=关)", "int"),
            ("post_delay", "滑动后延时ms", "int"),
            ("timeout", "等待超时ms(空=继承90000)", "int_opt"),
        ],
        "defaults": {"x1": 1000, "y1": 600, "x2": 280, "y2": 600,
                     "duration": 900, "repeat": 1, "repeat_delay": 350,
                     "pre_wait_freezes": 0, "post_wait_freezes": 0,
                     "post_delay": 600},
    },
    "wait_tpl": {
        "label": "等待模板出现", "icon": "⌛", "color": "#4a9d6e", "light": "#96dcb4",
        "summary": tpl_summary,
        "fields": [
            ("template", "模板图(逗号分隔多候选)", "tpl_multi"),
            ("threshold", "阈值", "float"),
            ("roi", "ROI x,y,w,h (空=全屏)", "roi"),
            ("timeout", "等待超时ms", "int"),
            ("rate_limit", "识别间隔ms(移动目标建议200)", "int"),
        ],
        "defaults": {"threshold": 0.8, "roi": "", "timeout": 10000, "rate_limit": 0},
    },
    "branch": {
        "label": "分支(模板在?)", "icon": "Ж", "color": "#c08a3e", "light": "#f0c68a",
        "summary": tpl_summary,
        "fields": [
            ("template", "模板图组(逗号分隔,任一命中)", "tpl_multi"),
            ("threshold", "阈值", "float"),
            ("ocr_text", "OCR文本(填则忽略模板图)", "str"),
            ("roi", "ROI x,y,w,h (空=全屏)", "roi"),
            ("timeout", "判定窗口ms", "int"),
            ("rate_limit", "识别间隔ms(移动目标建议200)", "int"),
        ],
        "defaults": {"threshold": 0.7, "ocr_text": "", "roi": "", "timeout": 3000,
                     "rate_limit": 0},
    },
    "loop": {
        "label": "循环(展开)", "icon": "↻", "color": "#b07d3e", "light": "#f0c48a",
        "summary": lambda p: f"×{p.get('times', 1)}",
        "fields": [
            ("times", f"循环次数(1~{LOOP_MAX_TIMES})", "int"),
        ],
        "defaults": {"times": 3},
    },
    "subflow": {
        "label": "子流程(内联)", "icon": "⧉", "color": "#6b7fae", "light": "#b6c4ea",
        "summary": lambda p: str(p.get("flow", "?"))[:10],
        "fields": [
            ("flow", "引用的流程(内联进来)", "flowref"),
        ],
        "defaults": {"flow": ""},
    },
    "common": {
        "label": "公共节点(收口)", "icon": "⌂", "color": "#4e8f8f", "light": "#9cdcdc",
        "summary": lambda p: p.get("node", "?"),
        "fields": [("node", "公共节点", "common"),
                   ("timeout", "等待超时ms(空=继承90000)", "int_opt")],
        "defaults": {"node": "Common_回主页"},
    },
    "startapp": {
        "label": "启动游戏", "icon": "▶", "color": "#b05f5f", "light": "#f0a8a8",
        "summary": lambda p: str(p.get("package", GAME_PKG)).split(".")[-1],
        "fields": [
            ("package", "包名", "str"),
            ("post_delay", "启动后延时ms", "int"),
            ("timeout", "等待超时ms(空=继承90000)", "int_opt"),
        ],
        "defaults": {"package": GAME_PKG, "post_delay": 1000},
    },
    "switch": {
        "label": "枝干判定(多路)", "icon": "☰", "color": "#7a5cb0", "light": "#c0a8f0",
        "summary": switch_summary,
        "fields": [
            ("candidates", "候选(每行: 模板名 或 OCR:文字 / 判定ms)", "switch_list"),
        ],
        "defaults": {
            "candidates": [{"t": "", "timeout": 3000}],
            "miss_next": "",
        },
    },
}

TYPE_ORDER = ["ocr_click", "tpl_click", "tap", "swipe", "wait_tpl", "branch",
              "switch", "loop", "subflow", "common", "startapp"]

# 所有节点类型共用的可选字段（属性面板在类型专属字段之后追加渲染）。
# notes 只是编辑器便签，不进生成物；enabled/max_hit 见 _put_common_fields。
COMMON_NODE_FIELDS = [
    ("enabled", "启用(取消勾选=跳过本节点)", "bool_opt"),
    ("max_hit", "最多命中次数(空=无限)", "int_opt"),
    ("notes", "备注(仅编辑器可见)", "str_opt"),
]

# 属性面板分组（按用途归类，避免识别/动作/时序字段混在一张平表里）
FIELD_GROUPS = [
    ("recog", "识别", {"template", "text", "ocr_text", "threshold", "roi",
                       "order_by", "index", "replace", "only_rec", "rate_limit"}),
    ("act", "动作", {"x", "y", "x1", "y1", "x2", "y2", "duration",
                     "repeat", "repeat_delay", "package"}),
    ("flow", "流程", {"node", "candidates", "timeout", "enabled", "max_hit", "notes"}),
    ("delay", "时序", {"pre_delay", "post_delay", "pre_wait_freezes",
                       "post_wait_freezes"}),
]
_GROUP_OF_KEY = {k: g for g, _label, keys in FIELD_GROUPS for k in keys}
_GROUP_LABEL = {g: label for g, label, _keys in FIELD_GROUPS}
_GROUP_ORDER = [g for g, _label, _keys in FIELD_GROUPS]


def _group_fields(fields):
    """把字段声明按分组归类，返回 [(组键, 组名, [字段声明, ...]), ...]。
    未登记的键归入「其它」；组内保持原有声明顺序，空组不返回。"""
    buckets = {}
    for f in fields:
        g = _GROUP_OF_KEY.get(f[0], "other")
        buckets.setdefault(g, []).append(f)
    out = []
    for g in _GROUP_ORDER + ["other"]:
        if buckets.get(g):
            out.append((g, _GROUP_LABEL.get(g, "其它"), buckets[g]))
    return out


# 互斥字段：满足条件时隐藏。否则「填了 A 就忽略 B」只能靠用户自己理解。
# 注意只隐藏被忽略的那一侧，触发互斥的字段本身始终可见（否则没法改回来）。
FIELD_HIDDEN_IF = {
    "branch": lambda p: ({"template", "threshold"}
                         if str(p.get("ocr_text", "")).strip() else set()),
}


# 字段提示（鼠标悬停在字段名上显示）。协议里最容易误解的几条必须写清楚。
FIELD_TIPS = {
    "timeout": "本节点 next 列表的识别超时 —— 不是本节点的识别等待。\n"
               "想缩短「本节点被识别到」的等待，请改【上一个节点】的 timeout。\n"
               "留空 = 继承 default_pipeline.json（本任务包 90000ms）。-1 = 无限等待。",
    "rate_limit": "每轮识别的最低耗时（毫秒），不足则等待。移动目标建议 200。",
    "threshold": "模板识别阈值：负样本最高分 <0.7 时 0.8 较安全。\n"
                 "OCR 时是模型置信度，引擎默认 0.3。",
    "post_wait_freezes": "动作后等画面静止（毫秒）。画面一直在变会等死，\n"
                         "点击动画中的按钮请不要用。",
    "pre_wait_freezes": "动作前等画面静止（毫秒）。",
    "post_delay": "动作后到识别 next 之间的固定延时（毫秒）。",
    "pre_delay": "识别到命中到执行动作之间的固定延时（毫秒）。",
    "order_by": "多个候选都命中时取哪一个：Horizontal=最左（引擎默认，\n"
                "注意不是最高分）、Score=分数最高、Vertical、Random。\n"
                "精确点单个目标建议选 Score 并把阈值抬到高于弱匹配。",
    "index": "命中第几个结果（0 起，可负数）。越界视为未识别。",
    "max_hit": "本节点最多被识别命中多少次，超出后会从 next 列表被跳过。\n"
               "用于给重试循环加界，防止永久空转。",
    "enabled": "取消勾选 = 生成物写 enabled:false，引擎会跳过本节点（不识别、不执行）。",
    "notes": "只存在流程定义里，不会写进生成物。",
    "repeat": "动作重复执行次数；重复过程中单次失败不中止，以最后一次为准。",
    "roi": "识别范围 x,y,w,h（帧坐标，基准 1280x720）。留空 = 全屏。",
    "replace": "OCR 易错字替换，填 '错=对,错2=对2'。",
    "only_rec": "仅识别不检测（需精确设置 ROI），可提高速度。",
    "template": "模板图（多个用逗号分隔，任一命中即算命中），取自 whmx/image。",
    "node": "公共节点层（whmx/pipeline/common.json）里的收口节点。",
    "flow": "被引用的子流程：生成时把它的节点【整段内联】进来，节点名带 "
            "VF_<本流程>_<本节点key>_ 前缀，跑完接回本流程的下一个节点。"
            "子流程结尾若是公共收口节点，则跑完不会返回。",
}


def _num(v, default=-1):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _field_spec(f):
    """字段声明 → (props键, 标签, 控件类型, 附加参数)。
    三元组是历史写法；第四元可选（例如 choice 的候选列表），故这里统一解包。"""
    return f[0], f[1], f[2], (f[3] if len(f) > 3 else None)


def normalize_flow(flow):
    """把旧版本流程文件里存成字符串的数值字段转回 int"""
    for nd in flow.get("nodes", {}).values():
        spec = NODE_TYPES.get(nd.get("type"))
        if not spec:
            continue
        for f in spec["fields"]:
            key, _label, kind, _extra = _field_spec(f)
            if kind in ("int", "int_opt", "pick", "pick2") and key in nd.get("props", {}):
                try:
                    nd["props"][key] = int(float(nd["props"][key]))
                except (TypeError, ValueError):
                    pass
        # order_by 由布尔开关升级为枚举（P1-4）：旧文件的 True 等价于 Score
        # （旧实现就是写 "Score"），False 则视为未设置，保证生成结果不变。
        props = nd.get("props") or {}
        if isinstance(props.get("order_by"), bool):
            if props["order_by"]:
                props["order_by"] = "Score"
            else:
                props.pop("order_by", None)
    return flow


def parse_roi(s):
    """'x,y,w,h' → [x,y,w,h]；非法返回 None"""
    try:
        parts = [int(v.strip()) for v in str(s).split(",")]
        if len(parts) != 4:
            return None
        return parts
    except (ValueError, TypeError):
        return None


def safe_name(name):
    return re.sub(r"[^\w\-]", "_", str(name)).strip("_") or "flow"


# ================= 核心校验/生成器（纯函数，GUI 无关） =================

# ---------- 出口规约：画布与生成器的唯一真相源 ----------
# 历史教训：过去「链上后继要不要生成 next」只在 build_pipeline 里判断，画布
# 却一律画实线箭头，于是出现「画布看着连着、生成结果却是断的」的欺骗性表现。
# 现在两处共用 linear_successor()：生成时断开，画布上也必须断开。

def _switch_content_leaves(flow):
    """被 switch 候选引用的「内容起点」节点集合 —— 命中后执行它即止，
    不沿线性链继续（防分支内容串线）。

    例外：候选勾了「命中后回并主线」(mergeBack) 时，该内容节点的链上后继
    会被保留，于是它与 branch 的命中内容行为一致（跑完继续走主线）。
    默认不勾 → 与历史行为完全一致，因此生成的 pipeline 不变。"""
    leaves = set()
    for nd in flow.get("nodes", {}).values():
        if nd.get("type") != "switch":
            continue
        for c in parse_switch_cands((nd.get("props") or {}).get("candidates")):
            if c.get("next") and not c.get("mergeBack"):
                leaves.add(c["next"])
    return leaves


def _suppress_reason(flow, nid):
    """出口被抑制的原因；None 表示出口正常（链上下一个，或本就是链尾）。
      switch    —— 协议上枝干没有「直落」出口：每个候选各有内容起点，全部未中走
                   miss 出口，所以链上下一个节点只能靠候选 next 连过去。
      common    —— 生成 {next:[Common_*]}，进入公共节点后流程即终止，不返回本流程。
      switch-content-leaf —— 分支内容叶，跑完即止。"""
    nd = flow.get("nodes", {}).get(nid)
    if nd is None:
        return None
    t = nd.get("type")
    if nid in _switch_content_leaves(flow):
        return "switch-content-leaf"
    if t == "switch":
        return "switch-no-fallthrough"
    if t == "common":
        return "common-terminal"
    chain = flow.get("chain", [])
    if nid in chain and chain.index(nid) + 1 < len(chain):
        return None          # 有后继且不被抑制
    return None              # 链尾：正常结束，不算被抑制


def linear_successor(flow, nid):
    """★ 单一真相源：该节点在生成时的链上后继节点 id；None = 出口被抑制或本就是链尾。
    调用方：build_pipeline() 与 FlowEditor.redraw()。禁止在别处自行推导链上后继。"""
    if _suppress_reason(flow, nid) is not None:
        return None
    chain = flow.get("chain", [])
    if nid not in chain:
        return None
    i = chain.index(nid)
    return chain[i + 1] if i + 1 < len(chain) else None


def exits_of(flow, nid):
    """规约一个节点的全部出口（含 suppress 原因），供生成器与画布共用。
    返回 None 表示节点不存在。候选项键名与 parse_switch_cands 一致（t/timeout/next），
    额外带 index。"""
    nd = flow.get("nodes", {}).get(nid)
    if nd is None:
        return None
    t = nd.get("type")
    p = nd.get("props") or {}
    ex = {"kind": t, "linear": linear_successor(flow, nid),
          "suppress": _suppress_reason(flow, nid),
          "hit": None, "miss": None, "body_end": None, "candidates": []}
    if t == "branch":
        ex["hit"] = nd.get("hit_next") or None
        ex["miss"] = nd.get("miss_next") or None
    elif t == "switch":
        ex["miss"] = p.get("miss_next") or None
        for i, c in enumerate(parse_switch_cands(p.get("candidates"))):
            item = dict(c)
            item["index"] = i
            ex["candidates"].append(item)
    elif t == "loop":
        ex["body_end"] = p.get("body_end") or None
    return ex


# ---------- 循环（loop）：编译期展开 ----------
# 循环不依赖任何引擎特性：生成时把循环体复制 times 份，逐份首尾相接，
# 最后一份的尾部接回链上后继。这样生成结果与「手工展开」完全等价，
# 可以纯代码推演、可逐节点比对，不需要真机验证引擎语义。
# 代价是同一个画布节点会对应多份生成节点（名字加 _L<k> 后缀），
# 所以下面的 I1/I3/I6 与反向映射都要认识这个后缀。

def loop_instances(flow):
    """解析全部循环节点。返回 [{nid, times, body:[节点id...], end, after}]，按链序。
    假定流程已通过校验；字段缺失时给保守默认，不抛异常。"""
    chain = flow.get("chain", [])
    pos = {nid: i for i, nid in enumerate(chain)}
    nodes = flow.get("nodes", {})
    out = []
    for nid in chain:
        nd = nodes.get(nid) or {}
        if nd.get("type") != "loop":
            continue
        p = nd.get("props") or {}
        try:
            times = int(float(p.get("times", 1)))
        except (TypeError, ValueError):
            times = 1
        i = pos[nid]
        j = pos.get(p.get("body_end"), -1)
        body = chain[i + 1:j + 1] if j > i else []
        after = chain[j + 1] if 0 <= j < len(chain) - 1 else None
        out.append({"nid": nid, "times": max(1, min(times, LOOP_MAX_TIMES)),
                    "body": body, "end": p.get("body_end"), "after": after})
    return out


def loop_body_nodes(flow):
    """落在任意循环体里的节点 id 集合（主发射循环要跳过它们，改由展开时逐份发射）"""
    s = set()
    for lp in loop_instances(flow):
        s |= set(lp["body"])
    return s


def loop_body_of(flow, nid):
    """某个循环节点的循环体节点列表（链上从它之后一直到 body_end）"""
    for lp in loop_instances(flow):
        if lp["nid"] == nid:
            return lp["body"]
    return []


def _strip_loop_suffix(name):
    """去掉实例后缀：VF_x_03_L2_Hit → VF_x_03_Hit"""
    return re.sub(r"(_L\d+)+(?=_Hit$|$)", "", str(name))


def _node_of_generated_name(flow, name):
    """pipeline 节点名 → 画布节点 id。认识：branch 的 _Hit、switch 展开的 _Jk/_Jk_Hit、
    以及循环展开的 _L<k> 实例后缀。"""
    if not name:
        return None
    m = _name_to_node(flow)
    cands = [name]
    if name.endswith("_Hit"):
        cands.append(name[:-4])
    stripped = _strip_loop_suffix(name)
    cands.append(stripped)
    if stripped.endswith("_Hit"):
        cands.append(stripped[:-4])
    for c in cands:
        if c and c in m:
            return m[c]
    mm = re.match(r"^(.*)_J\d+(_Hit)?$", stripped)
    if mm and f"{mm.group(1)}_J1" in m:
        return m[f"{mm.group(1)}_J1"]
    return None


@dataclass
class Issue:
    """一条校验结论。node_id 让 UI 能把问题标到具体节点上（画布/属性面板）。
    code 供程序判别（例如以后可配置「哪些检查只警告」）。"""
    level: str          # "error" | "warn"
    code: str
    message: str
    node_id: str | None = None

    def __str__(self):
        return self.message


def issues_lines(issues):
    """Issue 列表 → (errors, warnings) 字符串列表（保持出现顺序）"""
    return ([i.message for i in issues if i.level == "error"],
            [i.message for i in issues if i.level == "warn"])


def _expected_node_names(flow, _depth=0):
    """本流程将生成的【全部】pipeline 节点名（模型级预测）。
    用于 I1（同流程内名字必须互不相同）与 I6（不得与任务包内既有节点撞名）。
    循环体节点会被展开 times 份（带 _L<k> 后缀）；子流程会把被引用流程的节点
    整段内联进来（带 VF_<父>_<key>_ 前缀）。返回 [(名字, 归属说明)]。"""
    E = entry_name(flow)
    names = [(E, "流程入口")]
    nodes = flow.get("nodes", {})
    in_loop = {}
    for lp in loop_instances(flow):
        for b in lp["body"]:
            in_loop[b] = lp
    for i, nid in enumerate(flow.get("chain", [])):
        nd = nodes.get(nid) or {}
        t = nd.get("type")
        if t not in NODE_TYPES:
            continue                      # 未识别的类型不产出节点
        label = f"#{i + 1}「{nd.get('title', '?')}」"
        base = f"{E}_{node_key(flow, nid)}"
        if t == "subflow" and _depth < 3:
            names.append((base, label))       # 子流程节点自身的入口标记节点
            child, err = subflow_child(flow, nid)
            if child is not None and not err:
                c2 = dict(child)
                c2["name"] = _subflow_child_name(flow, nid)
                c_entry = entry_name(c2)
                for nm, lab in _expected_node_names(c2, _depth + 1):
                    if nm == c_entry:
                        continue          # 子流程的入口伪节点不产出
                    names.append((nm, f"{label}→{lab}"))
            continue
        lp = in_loop.get(nid)
        suffixes = ([f"_L{k + 1}" for k in range(lp["times"])] if lp else [""])
        if t == "switch":
            cands = parse_switch_cands((nd.get("props") or {}).get("candidates"))
            if not cands:
                continue                  # 空枝干不产出任何节点（已有专门的校验报错）
            for sfx in suffixes:
                for j in range(1, len(cands) + 1):
                    names.append((f"{base}{sfx}_J{j}", label))
                    names.append((f"{base}{sfx}_J{j}_Hit", label))
        elif t == "branch":
            for sfx in suffixes:
                names.append((f"{base}{sfx}", label))
                names.append((f"{base}{sfx}_Hit", label))
        else:
            for sfx in suffixes:
                names.append((f"{base}{sfx}", label))
    if _needs_end_node(flow):
        names.append((f"{E}_End", "流程收口节点"))
    return names


def _needs_end_node(flow):
    """build_pipeline 是否会在末尾补 VF_x_End（存在 switch，或有 branch 的 miss 未连线）"""
    nodes = flow.get("nodes", {})
    chain = flow.get("chain", [])
    for nid in chain:
        nd = nodes.get(nid) or {}
        if nd.get("type") == "switch":
            return True
        if nd.get("type") == "branch" and not nd.get("miss_next"):
            return True
    return False


def _check_naming(flow, issues):
    """I1：本流程生成的节点名必须互不相同。
    依据：MaaFramework 同一 Bundle 内节点重名会导致该次资源加载【整体失败】，
    不是「后者覆盖前者」——所以重名必须在编辑器里就拦住。"""
    seen = {}
    for name, label in _expected_node_names(flow):
        if name in seen:
            issues.append(Issue(
                "error", "NAME_DUP",
                f"节点名重复：{name} 同时被 {seen[name]} 与 {label} 占用"
                f"（重名会让手机端整包 pipeline 加载失败，不只是这个流程出问题）"))
        else:
            seen[name] = label
    nodes = flow.get("nodes", {})
    for i, nid in enumerate(flow.get("chain", [])):
        nd = nodes.get(nid) or {}
        key = str(nd.get("key") or "").strip()
        if key and not re.fullmatch(r"[A-Za-z0-9_]+", key):
            issues.append(Issue(
                "error", "KEY_CHARSET",
                f"#{i + 1}「{nd.get('title', '?')}」节点名(key) {key!r} 只能包含"
                f"英文字母、数字与下划线", nid))


# 这几类节点在编辑器里没有 timeout 字段，生成物里也不写 timeout，
# 于是继承 default_pipeline.json 的 timeout（本任务包为 90000ms）。
# 协议语义：timeout 管的是【本节点 next 列表的扫描超时】，
# 所以「点完一个固定坐标后等下一个节点出现」最长会空转 90 秒。
_INHERIT_TIMEOUT_KINDS = ("tap", "swipe", "startapp", "common")


def _check_timeout_declared(flow, issues):
    """I4：未声明 timeout 的节点汇总提示（一条，不刷屏）。
    已禁用的节点会被引擎跳过，不计入。"""
    lack = []
    nodes = flow.get("nodes", {})
    for i, nid in enumerate(flow.get("chain", [])):
        nd = nodes.get(nid) or {}
        if nd.get("type") not in _INHERIT_TIMEOUT_KINDS:
            continue
        p = nd.get("props") or {}
        if p.get("enabled") is False:
            continue
        if p.get("timeout") in (None, ""):
            lack.append(f"#{i + 1}「{nd.get('title', '?')}」")
    if lack:
        shown = "、".join(lack[:6]) + (" 等" if len(lack) > 6 else "")
        issues.append(Issue(
            "warn", "TIMEOUT_INHERIT",
            f"{len(lack)} 个节点未设置 timeout，将继承全局 90000ms"
            f"（点完之后等下一个节点出现，最长空转 90 秒）：{shown}"))


def _check_loops(flow, issues):
    """循环节点校验：次数上限、循环体范围、禁止嵌套、体内禁止 枝干/循环。
    次数上限是硬要求 —— 生成时要把循环体复制 times 份，填个 10000 会直接把
    生成 JSON 撑爆（手机端加载不动甚至崩）。"""
    chain = flow.get("chain", [])
    nodes = flow.get("nodes", {})
    pos = {nid: i for i, nid in enumerate(chain)}
    instances = loop_instances(flow)
    body_of = {}
    for lp in instances:
        for b in lp["body"]:
            body_of[b] = lp["nid"]
    for lp in instances:
        nid = lp["nid"]
        nd = nodes.get(nid) or {}
        i = pos[nid]
        p = nd.get("props") or {}
        no = f"#{i + 1}「{nd.get('title', '?')}」"
        try:
            times = int(float(p.get("times")))
        except (TypeError, ValueError):
            times = None
        if times is None:
            issues.append(Issue("error", "LOOP_TIMES",
                                f"{no}循环次数未填或不是数字", nid))
        elif not (1 <= times <= LOOP_MAX_TIMES):
            issues.append(Issue(
                "error", "LOOP_TIMES",
                f"{no}循环次数应为 1~{LOOP_MAX_TIMES}（当前 {times}）"
                f"—— 展开次数过大会把生成物撑到手机端加载不动", nid))
        end = p.get("body_end")
        if not end:
            issues.append(Issue(
                "error", "LOOP_NO_BODY",
                f"{no}未设置循环体末尾：从卡片右侧端口拖到循环体的最后一个节点", nid))
            continue
        if end not in pos:
            issues.append(Issue("error", "LOOP_NO_BODY",
                                f"{no}循环体末尾指向已删除节点", nid))
            continue
        j = pos[end]
        if j <= i:
            issues.append(Issue(
                "error", "LOOP_BODY_ORDER",
                f"{no}循环体末尾必须在循环节点【之后】的链上", nid))
            continue
        body = chain[i + 1:j + 1]
        if not body:
            issues.append(Issue("error", "LOOP_NO_BODY", f"{no}循环体为空", nid))
            continue
        if any(body_of.get(x, nid) != nid for x in [nid] + body):
            issues.append(Issue("error", "LOOP_NESTED",
                                f"{no}循环不能嵌套（与另一处循环共用节点）", nid))
            continue
        bad = [x for x in body
               if (nodes.get(x) or {}).get("type") in ("switch", "loop")]
        if bad:
            titles = "、".join((nodes.get(b) or {}).get("title", b) for b in bad[:3])
            issues.append(Issue(
                "error", "LOOP_BODY_KIND",
                f"{no}循环体里不能放 枝干/循环 节点（{titles}）"
                f"—— 展开时它们的子节点名会与实例后缀冲突", nid))
        if any((nodes.get(x) or {}).get("type") == "common" for x in body):
            issues.append(Issue(
                "warn", "LOOP_BODY_COMMON",
                f"{no}循环体里有公共收口节点，第一次循环就会终止流程", nid))


def _check_subflows(flow, issues, _depth=0):
    """子流程校验：引用存在、不自引用、不成环、环深有界、子流程自身不能是坏的。
    递归校验子流程只做 3 层（再深就只查引用存在），避免成环时无限递归。"""
    chain = flow.get("chain", [])
    nodes = flow.get("nodes", {})
    me = safe_name(flow.get("name") or "")
    for i, nid in enumerate(chain):
        nd = nodes.get(nid) or {}
        if nd.get("type") != "subflow":
            continue
        no = f"#{i + 1}「{nd.get('title', '?')}」"
        ref = str((nd.get("props") or {}).get("flow") or "").strip()
        if not ref:
            issues.append(Issue("error", "SUBFLOW_REF", f"{no}未选择子流程", nid))
            continue
        if safe_name(ref) == me:
            issues.append(Issue("error", "SUBFLOW_CYCLE", f"{no}子流程不能引用自己", nid))
            continue
        child, err = subflow_child(flow, nid)
        if child is None:
            issues.append(Issue("error", "SUBFLOW_REF", f"{no}{err}", nid))
            continue
        # 顺引用链走一圈：检测成环与过深
        cur, cur_nid, seen, depth, cyc = flow, nid, {me}, 0, None
        while True:
            c2, e2 = subflow_child(cur, cur_nid)
            if c2 is None:
                break
            key = safe_name(c2.get("name") or "")
            if key in seen:
                cyc = key
                break
            seen.add(key)
            depth += 1
            nxt_ids = [k for k, d in (c2.get("nodes") or {}).items()
                       if (d or {}).get("type") == "subflow"]
            if not nxt_ids or depth > 6:
                if depth > 6:
                    issues.append(Issue("error", "SUBFLOW_DEEP",
                                        f"{no}子流程嵌套过深（超过 6 层）", nid))
                break
            cur, cur_nid = c2, nxt_ids[0]
        if cyc:
            issues.append(Issue("error", "SUBFLOW_CYCLE",
                                f"{no}子流程引用成环（{cyc}）", nid))
            continue
        cch = child.get("chain") or []
        if not cch:
            issues.append(Issue("error", "SUBFLOW_REF",
                                f"{no}子流程「{ref}」里没有节点", nid))
            continue
        if (child["nodes"].get(cch[-1]) or {}).get("type") == "common":
            issues.append(Issue(
                "warn", "SUBFLOW_COMMON",
                f"{no}子流程「{ref}」的结尾是公共收口节点，跑完不会返回本流程", nid))
        if _depth < 3:
            cerr = [x for x in collect_issues(child, check_namespace=False,
                                              _depth=_depth + 1)
                    if x.level == "error"]
            if cerr:
                issues.append(Issue(
                    "error", "SUBFLOW_BAD_CHILD",
                    f"{no}子流程「{ref}」自身校验不通过（{len(cerr)} 个错误）："
                    f"{cerr[0].message[:60]}", nid))


def _check_disabled(flow, issues):
    """被禁用（enabled:false）的节点汇总提示：引擎会把它们从 next 里跳过，
    即这些节点【不会被执行】。常用于临时排查，所以只提示不报错。"""
    off = []
    nodes = flow.get("nodes", {})
    for i, nid in enumerate(flow.get("chain", [])):
        nd = nodes.get(nid) or {}
        if (nd.get("props") or {}).get("enabled") is False:
            off.append(f"#{i + 1}「{nd.get('title', '?')}」")
    if off:
        shown = "、".join(off[:6]) + (" 等" if len(off) > 6 else "")
        issues.append(Issue(
            "warn", "NODE_DISABLED",
            f"有 {len(off)} 个节点被禁用，生成物里会被引擎跳过（不会执行）：{shown}"))


def collect_issues(flow, frame_wh=(FRAME_W, FRAME_H), root=None, check_namespace=True,
                   _depth=0):
    """模型级校验（不需要生成结果）。返回 list[Issue]，按检出顺序。"""
    issues = []
    if not flow.get("name"):
        issues.append(Issue("error", "NAME_EMPTY", "流程名为空"))
    chain = flow.get("chain", [])
    nodes = flow.get("nodes", {})
    if not chain:
        issues.append(Issue("error", "NO_NODES", "流程没有节点"))
    W, H = frame_wh
    title = lambda nd: f"「{nd.get('title', '?')}」"
    for i, nid in enumerate(chain):
        nd = nodes.get(nid)
        if nd is None:
            issues.append(Issue("error", "CHAIN_REF",
                                f"链上有失效节点引用: {nid}", nid))
            continue
        t, p = nd["type"], nd.get("props", {})
        no = f"#{i+1}"
        if t == "branch" and str(p.get("ocr_text", "")).strip():
            pass   # OCR 文字判定分支无需模板图
        elif t in ("tpl_click", "wait_tpl", "branch"):
            tpls = split_tpls(p.get("template", ""))
            if not tpls:
                issues.append(Issue("error", "TPL_MISSING",
                                    f"{no}{title(nd)}未选择模板图", nid))
            else:
                for tpl in tpls:
                    if not os.path.isfile(os.path.join(IMG_DIR, tpl)):
                        issues.append(Issue(
                            "error", "TPL_MISSING",
                            f"{no}{title(nd)}模板不存在: whmx/image/{tpl}", nid))
        if t in ("tpl_click", "wait_tpl", "branch", "ocr_click") and p.get("roi"):
            roi = parse_roi(p["roi"])
            if roi is None:
                issues.append(Issue("error", "ROI_FMT",
                                    f"{no}{title(nd)}ROI 格式应为 x,y,w,h", nid))
            elif roi[0] < 0 or roi[1] < 0 or roi[0] + roi[2] > W or roi[1] + roi[3] > H:
                issues.append(Issue("warn", "ROI_OOB",
                                    f"{no}{title(nd)}ROI {p['roi']} 超出画面 {W}x{H}", nid))
        if t == "tap":
            x, y = _num(p.get("x", -1)), _num(p.get("y", -1))
            if not (0 <= x < W and 0 <= y < H):
                issues.append(Issue(
                    "error", "TAP_OOB",
                    f"{no}{title(nd)}点击坐标 ({p.get('x')},{p.get('y')}) "
                    f"非法或超出画面 {W}x{H}", nid))
        if t == "swipe":
            for k in ("x1", "y1", "x2", "y2"):
                v = _num(p.get(k, -1))
                lim = W if k.startswith("x") else H
                if not (0 <= v < lim):
                    issues.append(Issue(
                        "error", "SWIPE_OOB",
                        f"{no}{title(nd)}滑动坐标 {k}={p.get(k)} "
                        f"非法或超出画面 {W}x{H}", nid))
        if t in ("tpl_click", "wait_tpl", "branch"):
            try:
                th = float(p.get("threshold", 0))
                if not (0.3 <= th <= 0.99):
                    raise ValueError
            except (TypeError, ValueError):
                issues.append(Issue("error", "THRESHOLD",
                                    f"{no}{title(nd)}阈值应为 0.3~0.99 的数字", nid))
        if t == "branch":
            for port, label in (("hit_next", "✓命中"), ("miss_next", "✗未命中")):
                tgt = nd.get(port)
                if tgt is not None and tgt not in nodes:
                    issues.append(Issue("error", "EXIT_DEAD",
                                        f"{no}{title(nd)}{label}出口指向已删除节点", nid))
                elif tgt == nid:
                    issues.append(Issue("error", "EXIT_SELF",
                                        f"{no}{title(nd)}{label}出口不能指向自己", nid))
                elif tgt is not None and tgt in chain and chain.index(tgt) < i:
                    issues.append(Issue(
                        "warn", "EXIT_BACKWARD",
                        f"{no}{title(nd)}{label}出口跳回前面的节点（构成循环），"
                        f"请确保循环内有终止条件（如分支/收口节点）", nid))
        if t == "switch":
            cands = parse_switch_cands(p.get("candidates"))
            if not cands:
                issues.append(Issue("error", "SWITCH_EMPTY",
                                    f"{no}{title(nd)}枝干没有可用的候选", nid))
            for ci, c in enumerate(cands):
                spec = switch_cand_spec(c["t"])
                lab = f"候选{ci + 1}「{c['t']}」"
                if spec is None:
                    issues.append(Issue("error", "CAND_FMT",
                                        f"{no}{title(nd)}{lab}格式应为 模板名.png 或 OCR:文字", nid))
                elif spec[0] == "Template" and not os.path.isfile(
                        os.path.join(IMG_DIR, c["t"])):
                    issues.append(Issue(
                        "error", "CAND_TPL_MISSING",
                        f"{no}{title(nd)}{lab}模板不存在: whmx/image/{c['t']}", nid))
                nxt = c.get("next")
                if nxt is not None and nxt not in nodes:
                    issues.append(Issue("error", "CAND_EXIT_DEAD",
                                        f"{no}{title(nd)}{lab}命中出口指向已删除节点", nid))
                elif nxt == nid:
                    issues.append(Issue("error", "CAND_EXIT_SELF",
                                        f"{no}{title(nd)}{lab}命中出口不能指向自己", nid))
            mn = p.get("miss_next")
            if mn and mn not in nodes:
                issues.append(Issue("error", "MISS_EXIT_DEAD",
                                    f"{no}{title(nd)}全部未中出口指向已删除节点", nid))
        if t == "common" and i < len(chain) - 1:
            ref = str(p.get("node", ""))
            if ref.startswith("VF_"):
                issues.append(Issue(
                    "warn", "COMMON_MID",
                    f"{no}{title(nd)}为跨流程调用（进入 {ref}，"
                    f"跑完即结束，不会返回本流程）", nid))
            else:
                issues.append(Issue(
                    "warn", "COMMON_MID",
                    f"{no}{title(nd)}是公共收口节点（进入后流程即终止），"
                    f"放在链中间会导致其后的节点执行不到", nid))
    _check_naming(flow, issues)
    _check_timeout_declared(flow, issues)
    _check_disabled(flow, issues)
    _check_loops(flow, issues)
    _check_subflows(flow, issues, _depth)
    if check_namespace:
        for msg in audit_namespace(flow, root):
            issues.append(Issue("error", "NS_CLASH", msg))
    return issues


def validate_flow(flow, frame_wh=(FRAME_W, FRAME_H)):
    """返回 (errors, warnings) 字符串列表。
    兼容旧调用方（import_pipelines.py / --selftest）；需要节点归属时用 collect_issues。"""
    return issues_lines(collect_issues(flow, frame_wh))



def _canon(obj):
    """规范化 JSON 文本，用于稳定哈希"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def flow_fingerprint(flow):
    """流程定义的稳定指纹（sha256）"""
    return "sha256:" + hashlib.sha256(_canon(flow).encode("utf-8")).hexdigest()


def pipeline_fingerprint(out):
    """生成物的稳定指纹（不含 $meta 本身，避免自引用）。
    与 $meta.pipelineHash 应当一致；回放面板可用它把手机上的 vf_*.json
    去掉 $meta 后重算，回答「手机上跑的是不是本地这一版」。"""
    body = {k: v for k, v in out.items() if k != "$meta"}
    return "sha256:" + hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()


def _inject_meta(out, flow, frame_wh):
    """在生成物里插入 $meta 元数据。
    位置是安全的：MaaFramework 会跳过以 $ 开头的根字段
    （PipelineTypes.h 的 kNodePrefix_Ignore = "$"），引擎不会把它当节点解析。"""
    body = {k: v for k, v in out.items() if k != "$meta"}
    meta = {
        "generatedBy": "MaaWH Studio",
        "editorVersion": EDITOR_VERSION,
        "generatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "flowFile": f"{safe_name(flow.get('name') or 'flow')}.flow.json",
        "flowHash": flow_fingerprint(flow),
        "pipelineHash": pipeline_fingerprint(body),
        "frameW": int(frame_wh[0]),
        "frameH": int(frame_wh[1]),
    }
    return {"$meta": meta, **body}


def entry_name(flow):
    """流程的命名空间前缀：VF_<流程名>"""
    return f"VF_{flow['name']}"


def node_key(flow, nid):
    """节点名后缀 —— 节点身份，与它在链上的位置解耦。

    缺省（节点没有 key 字段）= 两位链序号，与旧版 jname 的位置化命名逐字符相同，
    因此 v1 流程文件不需要任何迁移动作，生成结果也不变。
    只有显式写了 key（用户主动改名）才会偏离位置化命名 —— 那是有意为之。

    key 的字符集与同流程内唯一性由 validate_flow 负责校验（见 P0-4 不变量）。"""
    nd = flow["nodes"].get(nid) or {}
    k = str(nd.get("key") or "").strip()
    if k:
        return k
    return f"{flow['chain'].index(nid) + 1:02d}"


def jname(flow, nid):
    """链上节点的 pipeline 名。
    switch 节点展开为 J1..JN 级联容器，故链上前驱的 next 指向首个判定 J1。"""
    base = f"{entry_name(flow)}_{node_key(flow, nid)}"
    if flow["nodes"][nid].get("type") == "switch":
        return f"{base}_J1"
    return base


def tpl_out(s):
    """模板字段输出：多候选 → 数组（任一命中），单值 → 字符串"""
    parts = split_tpls(s)
    return parts if len(parts) > 1 else (parts[0] if parts else "")


def chain_next_names(flow, nid):
    """链上后继的 pipeline 名列表；★ 与画布共用 linear_successor"""
    tgt = linear_successor(flow, nid)
    return [jname(flow, tgt)] if tgt else []


def _put_timeout(d, p):
    """timeout 是可选字段：留空则不写入，让引擎继承 default_pipeline.json 的值
    （本任务包为 90000ms）。旧流程没有这个字段，所以默认路径的生成结果不变。
    语义提醒：timeout 管的是【本节点 next 列表的扫描超时】，不是本节点的识别等待。
    协议 v5.5 起 -1 表示无限等待；0 会让首轮未命中立即超时，故下限取 1。"""
    v = p.get("timeout")
    if v is None or v == "":
        return
    try:
        iv = int(float(v))
    except (TypeError, ValueError):
        return
    d["timeout"] = -1 if iv < 0 else max(1, iv)


def _put_common_fields(d, p):
    """所有节点类型通用的可选流程字段。
    - enabled: 引擎默认 true，所以只有显式关闭时才写 false（旧流程生成结果不变）
    - max_hit: 留空不写（引擎默认无限次）
    - notes  : 只是编辑器便签，【不写入生成物】
    约定：加在「被前驱 next 引用的那一层」上（branch=容器、switch=首个判定 J1），
    因为引擎跳过/限次都是对 next 列表里的候选生效的。"""
    if d is None:
        return
    if p.get("enabled") is False or str(p.get("enabled")).strip().lower() == "false":
        d["enabled"] = False
    _put_opt_int(d, p, "max_hit", 1)


def _put_opt_int(d, p, key, lo=None, hi=None):
    """可选整数字段：留空不写入（旧流程没有该键 → 生成结果不变）"""
    v = p.get(key)
    if v is None or v == "":
        return
    try:
        iv = int(float(v))
    except (TypeError, ValueError):
        return
    if lo is not None:
        iv = max(lo, iv)
    if hi is not None:
        iv = min(hi, iv)
    d[key] = iv


def _put_opt_float(d, p, key, lo=None, hi=None):
    """可选浮点字段：留空不写入"""
    v = p.get(key)
    if v is None or v == "":
        return
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return
    if lo is not None:
        fv = max(lo, fv)
    if hi is not None:
        fv = min(hi, fv)
    d[key] = fv


def _ocr_replace(p):
    """OCR 易错字替换：props 里填 '错=对,错2=对2'（也接受 -> 与 →），
    生成协议的 replace: [["错","对"], ...]。留空则不写字段。"""
    raw = str(p.get("replace") or "").strip()
    if not raw:
        return None
    pairs = []
    for item in raw.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        for sep in ("=", "->", "→"):
            if sep in item:
                a, b = item.split(sep, 1)
                pairs.append([a.strip(), b.strip()])
                break
    return pairs or None


def _ocr_expected(p):
    """OCR 期望文本。props 键沿用历史名 text（旧流程文件因此无需迁移），
    读取时也兼容 expected；输出统一用协议规范字段 expected —— MaaFramework 里
    text 是「已废弃字段，兼容一下」，正式名是 expected（且支持正则）。"""
    raw = p.get("text")
    if raw is None or not str(raw).strip():
        raw = p.get("expected", "")
    return [s.strip() for s in str(raw).replace("，", ",").split(",") if s.strip()]


# ---------- 各节点类型的产出体 ----------
# 一个节点 = 识别块 + 动作块 + 流程/时序块 + 出口块。每种节点只产出一个 pipeline
# 节点（switch 展开为多个），这里按类型分开，新增字段时改对应一个函数即可。
# 注意：这些函数只负责「产出」，出口一律经 chain_next_names / exits_of 取得。

def _emit_tpl_click(out, name, p, nxt):
    d = {
        "recognition": "TemplateMatch",
        "template": tpl_out(p["template"]),
        "threshold": float(p["threshold"]),
        "action": "Click",
        "timeout": int(p["timeout"]),
        "post_delay": int(p["post_delay"]),
    }
    roi = parse_roi(p.get("roi", ""))
    if roi:
        d["roi"] = roi
    ob = p.get("order_by")
    if ob is True:
        d["order_by"] = "Score"          # 历史布尔写法：True 等价于 Score（旧实现如此）
    elif ob is not None and ob is not False and str(ob).strip():
        d["order_by"] = str(ob).strip()
    _put_opt_int(d, p, "index")
    if int(p.get("rate_limit", 0) or 0) > 0:
        d["rate_limit"] = int(p["rate_limit"])
    if int(p.get("pre_delay", 0)):
        d["pre_delay"] = int(p["pre_delay"])
    if int(p.get("post_wait_freezes", 0) or 0) > 0:
        d["post_wait_freezes"] = int(p["post_wait_freezes"])
    rep = int(p.get("repeat", 1) or 1)
    if rep > 1:
        d["repeat"] = rep
        d["repeat_delay"] = int(p.get("repeat_delay", 350))
    if nxt:
        d["next"] = nxt
    out[name] = d


def _emit_ocr_click(out, name, p, nxt):
    texts = _ocr_expected(p)
    if not texts:
        raise FlowValidationError([f"OCR节点未填写识别文本: {name}"])
    d = {
        "recognition": "OCR",
        "expected": texts,
        "action": "Click",
        "timeout": int(p["timeout"]),
        "post_delay": int(p["post_delay"]),
    }
    _put_opt_float(d, p, "threshold", 0.0, 1.0)
    if str(p.get("order_by") or "").strip():
        d["order_by"] = str(p["order_by"]).strip()
    _put_opt_int(d, p, "index")
    roi = parse_roi(p.get("roi", ""))
    if roi:
        d["roi"] = roi
    if int(p.get("rate_limit", 0) or 0) > 0:
        d["rate_limit"] = int(p["rate_limit"])
    if int(p.get("pre_delay", 0)):
        d["pre_delay"] = int(p["pre_delay"])
    if nxt:
        d["next"] = nxt
    if p.get("only_rec") is True:
        d["only_rec"] = True
    pairs = _ocr_replace(p)
    if pairs:
        d["replace"] = pairs
    out[name] = d


def _emit_tap(out, name, p, nxt):
    d = {
        "action": "Click",
        "target": [int(p["x"]), int(p["y"])],
        "post_delay": int(p["post_delay"]),
    }
    if int(p.get("pre_delay", 0)):
        d["pre_delay"] = int(p["pre_delay"])
    if int(p.get("post_wait_freezes", 0) or 0) > 0:
        d["post_wait_freezes"] = int(p["post_wait_freezes"])
    _put_timeout(d, p)
    rep = int(p.get("repeat", 1) or 1)
    if rep > 1:
        d["repeat"] = rep
        d["repeat_delay"] = int(p.get("repeat_delay", 350))
    if nxt:
        d["next"] = nxt
    out[name] = d


def _emit_swipe(out, name, p, nxt):
    d = {
        "action": "Swipe",
        "begin": [int(p["x1"]), int(p["y1"])],
        "end": [int(p["x2"]), int(p["y2"])],
        "duration": int(p["duration"]),
        "post_delay": int(p["post_delay"]),
    }
    if int(p.get("pre_wait_freezes", 0) or 0) > 0:
        d["pre_wait_freezes"] = int(p["pre_wait_freezes"])
    if int(p.get("post_wait_freezes", 0) or 0) > 0:
        d["post_wait_freezes"] = int(p["post_wait_freezes"])
    _put_timeout(d, p)
    rep = int(p.get("repeat", 1) or 1)
    if rep > 1:
        d["repeat"] = rep
        d["repeat_delay"] = int(p.get("repeat_delay", 350))
    if nxt:
        d["next"] = nxt
    out[name] = d


def _emit_wait_tpl(out, name, p, nxt):
    d = {
        "recognition": "TemplateMatch",
        "template": tpl_out(p["template"]),
        "threshold": float(p["threshold"]),
        "action": "DoNothing",
        "timeout": int(p["timeout"]),
    }
    roi = parse_roi(p.get("roi", ""))
    if roi:
        d["roi"] = roi
    if int(p.get("rate_limit", 0) or 0) > 0:
        d["rate_limit"] = int(p["rate_limit"])
    if nxt:
        d["next"] = nxt
    out[name] = d


def _emit_branch(flow, out, nid, name, p, nxt):
    """分叉容器（项目踩坑结论：on_error 只挂在容器节点上才生效）。
    返回是否需要生成收口节点（miss 未连线时）。"""
    exits = exits_of(flow, nid)
    hit, miss = exits["hit"], exits["miss"]
    hit_ref = [jname(flow, hit)] if hit else nxt      # 未连线 → 自动链中下一个
    miss_ref = [jname(flow, miss)] if miss else [f"{entry_name(flow)}_End"]
    end_needed = not miss
    out[name] = {
        "action": "DoNothing",
        "timeout": int(p["timeout"]),
        "next": [name + "_Hit"],
        "on_error": miss_ref,
    }
    if str(p.get("ocr_text", "")).strip():
        hd = {
            "recognition": "OCR",
            "expected": [s.strip() for s in
                         str(p["ocr_text"]).replace("，", ",").split(",") if s.strip()],
            "action": "DoNothing",
        }
    else:
        hd = {
            "recognition": "TemplateMatch",
            "template": tpl_out(p["template"]),
            "threshold": float(p["threshold"]),
            "action": "DoNothing",
        }
    roi = parse_roi(p.get("roi", ""))
    if roi:
        hd["roi"] = roi
    if int(p.get("rate_limit", 0) or 0) > 0:
        hd["rate_limit"] = int(p["rate_limit"])
    if hit_ref:
        hd["next"] = hit_ref
    out[name + "_Hit"] = hd
    return end_needed


def _emit_common(out, name, p):
    d = {"next": [p["node"]]}
    _put_timeout(d, p)
    out[name] = d


def _emit_startapp(out, name, p, nxt):
    d = {
        "action": "StartApp",
        "package": p["package"],
        "post_delay": int(p["post_delay"]),
    }
    _put_timeout(d, p)
    if nxt:
        d["next"] = nxt
    out[name] = d


def _emit_switch(flow, out, nid, name, p):
    """枝干判定：候选从左到右级联判定，命中→走该候选内容（执行完枝干结束），
    未中→下一个候选；全部未中→ miss 出口（默认流程结束）。
    name 是 jname(flow, nid)（形如 ..._J1），展开名由它推导 —— 这样带显式 key
    的枝干节点也能得到一致的名字。"""
    E = entry_name(flow)
    seq = name[:-3] if name.endswith("_J1") else name
    cands = exits_of(flow, nid)["candidates"]
    if not cands:
        return
    miss_ref = [jname(flow, p["miss_next"])] if p.get("miss_next") else [f"{E}_End"]
    for ci, c in enumerate(cands):
        cur = f"{seq}_J{ci + 1}"
        cur_hit = f"{cur}_Hit"
        spec = switch_cand_spec(c["t"])
        # 命中→内容起点（未连则结束）；下一个判定作为未中出口（末位→miss_ref）
        go = [jname(flow, c["next"])] if c.get("next") else [f"{E}_End"]
        if spec[0] == "OCR":
            hd = {"recognition": "OCR",
                  "expected": [spec[1]], "action": "DoNothing", "next": go}
        else:
            hd = {"recognition": "TemplateMatch", "template": spec[1],
                  "action": "DoNothing", "next": go}
        if ci + 1 < len(cands):
            on_err = [f"{seq}_J{ci + 2}"]
        else:
            on_err = miss_ref
        out[cur] = {"action": "DoNothing", "timeout": c["timeout"],
                    "next": [cur_hit], "on_error": on_err}
        out[cur_hit] = hd


def _emit_subflow(flow, out, nid, name, p, stack):
    """子流程：编译期把被引用流程的节点集【整段内联】进来。
    做法是给子流程一个「假流程名」<父流程名>_<节点key>，然后递归生成，
    于是子流程的节点名自动带上 VF_<父>_<key>_ 前缀，不会与父流程的节点撞名。
    子流程节点自身只是个入口标记；子流程的「结束」（链尾与收口 End）
    会被接回父流程的链上后继，等于「调用完继续往下走」。"""
    child, err = subflow_child(flow, nid)
    fake = _subflow_child_name(flow, nid)
    if child is None or err or fake in stack or len(stack) > 8:
        out[name] = {"action": "DoNothing"}     # 校验阶段已报错，这里只保证不崩
        return False
    child = dict(child)
    child["name"] = fake
    sub = _build_nodes(child, stack + (fake,))
    sub_entry = entry_name(child)
    ch = child.get("chain") or []
    first = jname(child, ch[0]) if ch else None
    # 合并子流程生成的节点（丢掉它的入口伪节点）
    for k, v in sub.items():
        if k == sub_entry:
            continue
        out[k] = v
    after = chain_next_names(flow, nid)          # 子流程节点的链上后继
    # 子流程的收口 End 改接父流程后继（而不是真的结束整个任务）
    end_name = f"{sub_entry}_End"
    if end_name in out and after:
        out[end_name] = {"action": "DoNothing", "next": list(after)}
    # 子流程链尾：原本 next 为空 → 接父流程后继
    if ch:
        tail = ch[-1]
        tail_name = jname(child, tail)
        d = out.get(tail_name)
        tail_type = (child["nodes"].get(tail) or {}).get("type")
        if isinstance(d, dict) and not d.get("next") and tail_type != "common" and after:
            d["next"] = list(after)
    out[name] = {"action": "DoNothing", "next": [first]} if first else {"action": "DoNothing"}
    return False


def _subflow_child_name(flow, nid):
    """子流程内联时使用的假流程名（决定子流程节点的命名前缀）"""
    return f"{flow.get('name') or 'flow'}_{node_key(flow, nid)}"


def subflow_child(flow, nid):
    """读取子流程节点引用的流程定义。返回 (child_flow, error)"""
    p = (flow.get("nodes", {}).get(nid) or {}).get("props") or {}
    ref = str(p.get("flow") or "").strip()
    if not ref:
        return None, "未选择子流程"
    path = os.path.join(FLOWS_DIR, safe_name(ref) + ".flow.json")
    if not os.path.isfile(path):
        return None, f"找不到子流程文件 flows/{safe_name(ref)}.flow.json"
    try:
        with open(path, encoding="utf-8") as f:
            child = normalize_flow(json.load(f))
    except Exception as ex:                                  # noqa: BLE001
        return None, f"子流程读不出来：{ex}"
    return child, None


def flow_name_list(exclude=None):
    """本目录所有流程名（供子流程下拉），可排除一个（自己）"""
    names = []
    for path in sorted(glob.glob(os.path.join(FLOWS_DIR, "*.flow.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                nm = str(json.load(f).get("name") or "").strip()
        except Exception:                                    # noqa: BLE001
            continue
        if nm and nm != exclude:
            names.append(nm)
    return names


def _emit_one(flow, out, nid, name, nxt, stack=()):
    """按节点类型发射一个生成节点。name 由调用方给出 —— 循环展开时同一画布节点
    会带着 _L<k> 实例后缀被发射多次。返回是否需要生成收口节点。"""
    nd = flow["nodes"][nid]
    t, p = nd["type"], nd.get("props", {})
    if t == "tpl_click":
        _emit_tpl_click(out, name, p, nxt)
    elif t == "ocr_click":
        _emit_ocr_click(out, name, p, nxt)
    elif t == "tap":
        _emit_tap(out, name, p, nxt)
    elif t == "swipe":
        _emit_swipe(out, name, p, nxt)
    elif t == "wait_tpl":
        _emit_wait_tpl(out, name, p, nxt)
    elif t == "branch":
        return _emit_branch(flow, out, nid, name, p, nxt)
    elif t == "common":
        _emit_common(out, name, p)
    elif t == "startapp":
        _emit_startapp(out, name, p, nxt)
    elif t == "switch":
        _emit_switch(flow, out, nid, name, p)
    elif t == "loop":
        _emit_loop(flow, out, nid, name)
    elif t == "subflow":
        return _emit_subflow(flow, out, nid, name, p, stack)
    return False


def _emit_loop(flow, out, nid, name):
    """循环节点自身只是「入口标记」：展开后它唯一的作用是把控制流送进第一份循环体。
    复制发生在 _expand_loop（编译期展开，不用引擎的循环特性）。"""
    inst = next((lp for lp in loop_instances(flow) if lp["nid"] == nid), None)
    d = {"action": "DoNothing"}
    if inst and inst["body"]:
        d["next"] = [f"{jname(flow, inst['body'][0])}_L1"]
    out[name] = d


def _expand_loop(flow, out, lp, stack=()):
    """把循环体复制 times 份：每份的节点名带 _L<k> 后缀，逐份首尾相接，
    最后一份的尾部接回链上后继（= 循环体末尾节点的链上后继）。
    返回是否需要生成收口节点。"""
    times, body = lp["times"], lp["body"]
    if not body:
        return False
    end_needed = False
    for k in range(1, times + 1):
        sfx = f"_L{k}"
        for idx, nid in enumerate(body):
            name = f"{jname(flow, nid)}{sfx}"
            if idx + 1 < len(body):
                nxt = [f"{jname(flow, body[idx + 1])}{sfx}"]
            elif k < times:
                nxt = [f"{jname(flow, body[0])}_L{k + 1}"]
            else:
                nxt = chain_next_names(flow, body[-1])    # 循环结束后回到链上后继
            end_needed |= _emit_one(flow, out, nid, name, nxt, stack)
            _put_common_fields(out.get(name),
                               flow["nodes"][nid].get("props") or {})
    return end_needed


def generated_names_for(flow, nid):
    """该画布节点在生成物里对应的全部名字（循环体节点会展开成 times 份）"""
    base = jname(flow, nid)
    for lp in loop_instances(flow):
        if nid in lp["body"]:
            return [f"{base}_L{k + 1}" for k in range(lp["times"])]
    return [base]


def build_pipeline(flow, frame_wh=(FRAME_W, FRAME_H)):
    """流程定义 → MaaFramework pipeline dict（VF_ 前缀命名空间）。"""
    errs, _ = validate_flow(flow, frame_wh)
    if errs:
        raise FlowValidationError(errs)
    return _inject_meta(_build_nodes(flow), flow, frame_wh)


def _build_nodes(flow, stack=()):
    """生成节点图（不含 $meta、不校验）。子流程内联时会带着假流程名递归进入。"""
    chain = flow["chain"]
    nodes = flow["nodes"]
    E = entry_name(flow)

    out = {E: {"next": [jname(flow, chain[0])] if chain else []}}
    end_needed = False
    body_nodes = loop_body_nodes(flow)      # 循环体节点由展开阶段逐份发射

    for i, nid in enumerate(chain):
        nd = nodes[nid]
        t, p = nd["type"], nd.get("props", {})
        base = jname(flow, nid)
        nxt = chain_next_names(flow, nid)
        if nid in body_nodes:
            continue

        end_needed |= _emit_one(flow, out, nid, base, nxt, stack)

        # 通用可选字段（enabled / max_hit）加在被前驱 next 引用的那一层上
        _put_common_fields(out.get(base), p)

    # 循环：编译期把循环体复制 times 份（不依赖引擎的循环特性）
    for lp in loop_instances(flow):
        end_needed |= _expand_loop(flow, out, lp, stack)

    if end_needed or any(nodes[n].get("type") == "switch" for n in chain):
        out[f"{E}_End"] = {"action": "DoNothing", "next": []}
    return out


class FlowValidationError(Exception):
    def __init__(self, errs):
        super().__init__("; ".join(errs))
        self.errors = errs


# ================= 生成结果级不变量（P0-4） =================
# 模型级校验（collect_issues）只看流程定义；这里几条必须看【生成结果】才能判：
#   I2 引用存在   —— 生成物里 next/on_error 指向的节点必须真的存在
#   I3 画布=生成  —— 生成物实际连的边，必须与画布/模型表达的边完全一致
#   I5 无保护环   —— 从入口可达的环上必须至少有一个保护点（max_hit/enabled:false）
# 只有加载过 pipeline 的引擎才知道 I2，只有画布/生成器自己知道 I3，只有把它们
# 放在一起比对，才能防止「画布看着对、生成结果不一样」这类静默错误再出现。

def _strip_attr(name):
    """去掉 NodeAttr 前缀：'[JumpBack]X' / '[Anchor]X' → 'X'"""
    s = str(name)
    return s.lstrip("[").split("]", 1)[-1] if s.startswith("[") else s


def _out_edges(out, name):
    """生成节点 name 的全部出边目标名（next + on_error）"""
    d = out.get(name) or {}
    for key in ("next", "on_error"):
        for item in (d.get(key) or []):
            nm = item.get("name") if isinstance(item, dict) else item
            if nm:
                yield _strip_attr(nm)


def _name_to_node(flow):
    """生成节点名 → 链上节点 id（switch 的 jname 就是它的 _J1 名）"""
    return {jname(flow, nid): nid for nid in flow.get("chain", [])}


def _own_artifact_name(flow):
    """本流程在任务包里对应的生成物文件名"""
    return f"vf_{safe_name(flow.get('name') or 'flow')}.json"


def bundle_node_keys(root=None, exclude_files=()):
    """读取任务包 pipeline 目录里所有 JSON 的节点键（纯本地文件，不连手机）。
    依据：同一 Bundle 内节点重名会让整次资源加载失败（不是覆盖）。"""
    root = root or ROOT
    pipe_dir = os.path.join(root, "whmx", "pipeline")
    keys = set()
    if not os.path.isdir(pipe_dir):
        return keys
    skip = {os.path.basename(p) for p in exclude_files}
    paths = sorted(glob.glob(os.path.join(pipe_dir, "*.json")) +
                   glob.glob(os.path.join(pipe_dir, "*.jsonc")))
    for path in paths:
        if os.path.basename(path) in skip:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = jsonc_loads(f.read())
        except Exception:
            continue                     # 单个文件坏掉不阻断查重
        if isinstance(data, dict):
            keys |= {str(k) for k in data if not str(k).startswith("$")}
    return keys


def audit_namespace(flow, root=None):
    """I6：本流程生成的名字不得与任务包内既有节点撞名。返回错误消息列表。
    必须排除本流程自己的生成物，否则每次校验都会自撞。"""
    root = root or ROOT
    pipe_dir = os.path.join(root, "whmx", "pipeline")
    if not os.path.isdir(pipe_dir):
        return []          # 找不到任务包时不误报（启动日志已提示工程根探测结果）
    mine = {n for n, _ in _expected_node_names(flow)}
    other = bundle_node_keys(root, exclude_files=[os.path.join(pipe_dir, _own_artifact_name(flow))])
    clash = sorted(mine & other)
    if not clash:
        return []
    shown = "、".join(clash[:6]) + (" 等" if len(clash) > 6 else "")
    return [f"节点名与任务包内既有节点冲突（重名会让手机端整包 pipeline 加载失败）："
            f"{shown}"]


def find_unprotected_cycles(out, entry):
    """I5：从入口可达且环上没有任何保护点（max_hit / enabled:false）的环。
    依据：节点默认 recognition=DirectHit（永远命中），一个指回自己的 next 会按
    rate_limit 的节奏永久空转；编辑器当前也不产出 max_hit，所以这种环只可能是意外。"""
    if entry not in out:
        return []
    color, stack, cycles = {}, [], []

    def protected(nm):
        d = out.get(nm) or {}
        return bool(d.get("max_hit")) or d.get("enabled") is False

    def dfs(nm):
        color[nm] = 1
        stack.append(nm)
        for tgt in _out_edges(out, nm):
            if tgt not in out:
                continue
            c = color.get(tgt, 0)
            if c == 1:
                cyc = stack[stack.index(tgt):]
                if not any(protected(x) for x in cyc):
                    cycles.append(list(cyc))
            elif c == 0:
                dfs(tgt)
        stack.pop()
        color[nm] = 2

    dfs(entry)
    # 同一片环会被不同回边报成多条（长度不同、互相包含），只保留最小环，避免刷屏
    uniq = []
    for c in sorted(cycles, key=len):
        s = set(c)
        if any(set(u) <= s for u in uniq):
            continue
        uniq.append(c)
    return uniq


def collect_pipeline_issues(flow, out, frame_wh=(FRAME_W, FRAME_H), root=None):
    """生成结果级校验。只在模型级无 error 时调用（否则 out 不可信）。"""
    issues = []
    E = entry_name(flow)
    nodes = flow.get("nodes", {})

    # ---------- I2：生成物里引用的节点必须存在（或属于任务包其它文件） ----------
    known = set(out)
    external = bundle_node_keys(root)
    dock_n = f"{E}_End"
    for name in sorted(out):
        for tgt in _out_edges(out, name):
            if tgt in known or tgt in external or tgt == dock_n:
                continue
            issues.append(Issue(
                "error", "REF_MISSING",
                f"{name} 引用了不存在的节点 {tgt}"
                f"（引擎加载时会拒绝整个任务包，不只是这个流程）"))

    # ---------- I3：画布/模型表达的边 == 生成结果实际连的边 ----------
    # 循环展开后一个画布节点对应多份生成节点，所以「实际边」要把各份并起来；
    # 相应地循环体末尾的期望边要同时含「回到循环体首节点」和「接回链上后继」。
    loops = loop_instances(flow)

    def tgt_of(nm):
        nm = _strip_attr(nm)
        if nm == dock_n:
            return ("end", None)
        hit = _node_of_generated_name(flow, nm)
        if hit is not None:
            return ("node", hit)
        return ("ext", nm)

    def gen_targets(names, key):
        """把若干生成节点的 next/on_error 目标并成一个集合"""
        s = set()
        for nm in names:
            d = out.get(nm) or {}
            for item in (d.get(key) or []):
                t = item.get("name") if isinstance(item, dict) else item
                if t:
                    s.add(tgt_of(t))
        return s

    def show(t):
        kind, val = t
        if kind == "end":
            return "<收口 End>"
        if kind == "ext":
            return val
        return jname(flow, val)

    def show_set(s):
        return "、".join(sorted(show(t) for t in s)) if s else "（无）"

    for nid in flow.get("chain", []):
        nd = nodes.get(nid) or {}
        t = nd["type"]
        base = jname(flow, nid)
        names = generated_names_for(flow, nid)
        ex = exits_of(flow, nid)
        expect, actual = {}, {}
        if t == "branch":
            hit = ex["hit"] or ex["linear"]
            expect["hit"] = {("node", hit)} if hit else set()
            expect["miss"] = {("node", ex["miss"])} if ex["miss"] else {("end", None)}
            actual["miss"] = gen_targets(names, "on_error")
            actual["hit"] = gen_targets([f"{nm}_Hit" for nm in names], "next")
        elif t == "switch":
            cands = ex["candidates"]
            if not cands:
                continue                 # 空枝干不产出节点（模型级已报错）
            for c in cands:
                expect[f"cand{c['index']}"] = ({("node", c["next"])} if c.get("next")
                                               else {("end", None)})
            expect["miss"] = {("node", ex["miss"])} if ex["miss"] else {("end", None)}
            kb = f"{E}_{node_key(flow, nid)}"
            j = 1
            while f"{kb}_J{j}" in out:
                hd = out.get(f"{kb}_J{j}_Hit") or {}
                actual[f"cand{j - 1}"] = {tgt_of(n) for n in (hd.get("next") or [])}
                oe = out[f"{kb}_J{j}"].get("on_error") or []
                nxt_j = f"{kb}_J{j + 1}"
                is_cascade = len(oe) == 1 and _strip_attr(
                    oe[0].get("name") if isinstance(oe[0], dict) else oe[0]) == nxt_j
                if not is_cascade:
                    actual["miss"] = {tgt_of(n) for n in oe}
                j += 1
        elif t == "subflow":
            # 子流程节点唯一出口 = 进入被内联进来的那个流程的首节点；
            # 那个首节点不属于本流程的画布节点，故按 ext 记录（名字可核）
            child, err = subflow_child(flow, nid)
            first = None
            if child is not None and not err:
                c2 = dict(child)
                c2["name"] = _subflow_child_name(flow, nid)
                cch = c2.get("chain") or []
                if cch:
                    first = jname(c2, cch[0])
            expect["next"] = {("ext", first)} if first else set()
            actual["next"] = gen_targets(names, "next")
        elif t == "common":
            p = nd.get("props") or {}
            expect["next"] = {("ext", str(p.get("node")))}
            actual["next"] = gen_targets(names, "next")
        else:
            expect["next"] = {("node", ex["linear"])} if ex["linear"] else set()
            # 循环体末尾：展开后前 times-1 份的 next 是「回到循环体首节点」
            for lp in loops:
                if nid == lp["end"] and lp["times"] >= 2 and lp["body"]:
                    expect["next"].add(("node", lp["body"][0]))
            actual["next"] = gen_targets(names, "next")

        for role in sorted(set(expect) | set(actual)):
            e, a = expect.get(role, set()), actual.get(role, set())
            if e != a:
                issues.append(Issue(
                    "error", "EDGE_MISMATCH",
                    f"「{nd.get('title', '?')}」({base}) 的 {role} 出口不一致："
                    f"画布= {show_set(e)} ≠ 生成结果= {show_set(a)}", nid))

    # ---------- I5：无保护循环 ----------
    # 判为【警告】而不是错误：实测本仓库有 4 个流程用「点击 → 判定 → 未中则回去再点」
    # 的重试循环，这是有意的写法（等到目标出现为止），编辑器无权判定它是 bug。
    # 但它确实是无界的：一旦条件永远不成立就会永久空转，所以必须明确提示出来。
    cycles = find_unprotected_cycles(out, E)
    if cycles:
        involved = set()
        for c in cycles:
            involved |= set(c)
        examples = "；".join(" → ".join(c[:5]) + (" → …" if len(c) > 5 else "")
                            for c in cycles[:2])
        more = f" 等 {len(cycles)} 处" if len(cycles) > 2 else ""
        issues.append(Issue(
            "warn", "CYCLE_UNPROTECTED",
            f"检测到 {len(cycles)} 处无保护循环（涉及 {len(involved)} 个节点）："
            f"{examples}{more}。节点默认 recognition=DirectHit（永远命中），环上没有 "
            f"max_hit 时，一旦循环条件永远不成立就会按 rate_limit 的节奏永久空转。"
            f"若这是有意的「重试直到目标出现」可忽略；否则建议给环上任一节点设置 "
            f"max_hit 或缩短 timeout"))
    return issues


def collect_all_issues(flow, frame_wh=(FRAME_W, FRAME_H), root=None):
    """完整校验（模型级 + 生成结果级），返回 list[Issue]。"""
    issues = collect_issues(flow, frame_wh, root)
    if any(i.level == "error" for i in issues):
        return issues          # 模型级就有错时生成的图不可信，不再做结果级检查
    try:
        out = build_pipeline(flow, frame_wh)
    except FlowValidationError as ex:
        for m in ex.errors:
            issues.append(Issue("error", "BUILD_FAIL", m))
        return issues
    issues += collect_pipeline_issues(flow, out, frame_wh, root)
    return issues


def validate_pipeline(flow, frame_wh=(FRAME_W, FRAME_H), root=None):
    """完整校验的字符串版：返回 (errors, warnings)"""
    return issues_lines(collect_all_issues(flow, frame_wh, root))


# ================= 流程文件读写 =================

def new_flow(name="测试流程"):
    return {"name": name, "chain": [], "nodes": {}}


def flow_path(name):
    return os.path.join(FLOWS_DIR, safe_name(name) + ".flow.json")


def save_flow(flow):
    os.makedirs(FLOWS_DIR, exist_ok=True)
    path = flow_path(flow["name"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(flow, f, ensure_ascii=False, indent=2)
    return path


def list_flows():
    if not os.path.isdir(FLOWS_DIR):
        return []
    return sorted(glob.glob(os.path.join(FLOWS_DIR, "*.flow.json")))


def list_templates():
    files = glob.glob(os.path.join(IMG_DIR, "*.png"))
    files.sort(key=os.path.getmtime, reverse=True)   # 越晚存入越靠前
    return [os.path.basename(p) for p in files]


def template_usage(root=None):
    """{模板文件名: [引用它的流程名, ...]} —— 遍历本目录所有 flows/*.flow.json。
    用于「这张模板还有没有用」「改这张模板会影响哪些流程」。"""
    usage = {}
    for path in sorted(glob.glob(os.path.join(FLOWS_DIR, "*.flow.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                flow = normalize_flow(json.load(f))
        except Exception:
            continue
        fname = flow.get("name") or os.path.basename(path)
        for nd in flow.get("nodes", {}).values():
            t = nd.get("type")
            props = nd.get("props") or {}
            tpls = []
            if t in ("tpl_click", "wait_tpl", "branch"):
                tpls += split_tpls(props.get("template"))
            if t == "switch":
                tpls += [c["t"] for c in parse_switch_cands(props.get("candidates"))
                         if str(c["t"]).endswith(".png")]
            for name in tpls:
                usage.setdefault(name, [])
                if fname not in usage[name]:
                    usage[name].append(fname)
    return usage


def _tpl_candidates(templates, typed):
    """模板下拉过滤：子串匹配，前缀命中者优先（中文名也能搜中间片段）"""
    typed = str(typed).strip().lower()
    if not typed:
        return list(templates)
    hit = [t for t in templates if typed in t.lower()]
    return sorted(hit, key=lambda t: (not t.lower().startswith(typed), t))


def split_tpls(s):
    """模板多值：'a.png,b.png' → ['a.png','b.png']（兼容中文逗号，去空）"""
    return [t.strip() for t in str(s or "").replace("，", ",").split(",") if t.strip()]


def tpl_value_ok(s):
    """校验模板字段当前输入：全部为已存在的模板（多值逗号分隔）"""
    parts = split_tpls(s)
    return bool(parts) and all(
        t in list_templates() for t in parts)


def write_pipeline_json(flow, frame_wh):
    """生成并写盘 build/vf_<名>.json，返回 (path, data)"""
    os.makedirs(BUILD_DIR, exist_ok=True)
    data = build_pipeline(flow, frame_wh)
    path = os.path.join(BUILD_DIR, f"vf_{safe_name(flow['name'])}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    return path, data


# ================= 引擎日志解析（运行回放的唯一数据源） =================
# 手机端 files/maa_logs/maafw.log 里带着逐节点的结构化事件，PC 侧只读解析即可实现
# 「日志回传 → 节点高亮 → 识别框可视化」，不需要改安卓端、不需要新协议。
# 日志是追加写、不轮转的，所以用 tail -c +<offset> 做增量读取。

NODE_EVENT_RE = re.compile(r"\[msg=Node\.(?P<kind>[A-Za-z.]+)\]\s*\[details=(?P<json>\{.*\})\]\s*$")
TASK_TIMEOUT_RE = re.compile(r"Task timeout \[pretask\.name=(?P<name>[^\]]+)\]"
                             r" \[duration_since\(start_clock\)=(?P<elapsed>-?\d+)ms\]"
                             r" \[pretask\.reco_timeout=(?P<timeout>-?\d+)ms\]")
TASK_END_RE = re.compile(r"task end: \[cb_detail=(?P<json>\{.*?\})\]\s*\[ret=(?P<ret>true|false)\]")
ENGINE_SIZE_RE = re.compile(r"\[image_target_width_=(?P<w>\d+)\] "
                            r"\[image_target_height_=(?P<h>\d+)\]")
TS_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]")


@dataclass
class NodeEvent:
    """一条运行事件。box/point 都是【引擎识别帧】坐标（虚拟屏原生帧）。"""
    ts: str = ""
    kind: str = ""            # 例如 Recognition.Succeeded / NextList.Failed / TaskTimeout
    name: str = ""            # pipeline 节点名
    level: str = "info"       # info | ok | warn | err
    box: list | None = None   # 识别命中区域 [x,y,w,h]
    score: float | None = None
    point: list | None = None # 动作落点 [x,y]
    size: list | None = None  # 引擎识别帧尺寸 [w,h]（EngineFrame 事件）
    elapsed: int | None = None
    timeout: int | None = None
    detail: str = ""          # 界面上一行摘要
    raw: str = ""

    def summary(self):
        base = self.name or "-"
        if self.kind == "EngineFrame":
            return f"引擎识别帧 {self.size[0]}×{self.size[1]}"
        if self.kind == "Recognition.Succeeded":
            s = f"{self.score:.3f}" if self.score is not None else "?"
            return f"识别命中 {base}  分数 {s}"
        if self.kind == "Recognition.Failed":
            s = f"{self.score:.3f}" if self.score is not None else "-"
            return f"识别未命中 {base}  最高 {s}"
        if self.kind in ("Action.Starting", "Action.Succeeded", "Action.Failed"):
            tail = f" → {self.point}" if self.point else ""
            return f"动作 {self.kind.split('.')[1]} {base}{tail}"
        if self.kind == "NextList.Starting":
            return f"开始尝试 {base} 的 next"
        if self.kind == "NextList.Succeeded":
            return f"{base} 的 next 命中"
        if self.kind == "NextList.Failed":
            return f"{base} 的 next 全部未命中"
        if self.kind == "TaskTimeout":
            return (f"超时：{base} 等待 {self.elapsed}ms"
                    f"（该节点 timeout={self.timeout}ms）")
        if self.kind == "TaskEnd":
            return f"任务结束 entry={base} ret={self.detail}"
        if self.kind == "PipelineNode.Starting":
            return f"节点开始 {base}"
        if self.kind == "PipelineNode.Succeeded":
            return f"节点完成 {base}"
        if self.kind == "PipelineNode.Failed":
            return f"节点失败 {base}"
        if self.kind.startswith("WaitFreezes."):
            return f"等待画面静止 {self.kind.split('.')[1]} {base}"
        return f"{self.kind} {base}"


def _deep_get(obj, path):
    cur = obj
    for k in path:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int) and -len(cur) <= k < len(cur):
            cur = cur[k]
        else:
            return None
    return cur


def parse_engine_log(text):
    """把引擎日志文本解析成 NodeEvent 列表（纯函数，可离线测）。
    不认识的行直接跳过，不抛异常 —— 引擎日志格式跨版本会变。"""
    events = []
    for line in text.splitlines():
        if not line:
            continue
        ts = TS_RE.match(line)
        ts = ts.group("ts") if ts else ""

        m = NODE_EVENT_RE.search(line)
        if m:
            kind = m.group("kind")
            try:
                details = json.loads(m.group("json"))
            except ValueError:
                continue
            name = str(details.get("name") or "")
            ev = NodeEvent(ts=ts, kind=kind, name=name, raw=line)
            reco = details.get("reco_details") or {}
            act = details.get("action_details") or {}
            box = reco.get("box")
            if isinstance(box, list) and len(box) == 4:
                ev.box = box
            best = _deep_get(reco, ["detail", "best"])
            if isinstance(best, dict) and isinstance(best.get("score"), (int, float)):
                ev.score = float(best["score"])
            if ev.score is None:
                allr = _deep_get(reco, ["detail", "all"])
                if isinstance(allr, list) and allr:
                    sc = [r.get("score") for r in allr
                          if isinstance(r, dict) and isinstance(r.get("score"), (int, float))]
                    if sc:
                        ev.score = float(max(sc))
            pt = _deep_get(act, ["detail", "point"])
            if isinstance(pt, list) and len(pt) == 2:
                ev.point = pt
            if kind.endswith("Succeeded"):
                ev.level = "ok"
            elif kind.endswith("Failed"):
                ev.level = "err" if kind.startswith(("PipelineNode", "Action")) else "warn"
            if kind == "Recognition.Succeeded":
                ev.level = "ok"
            ev.detail = ev.summary()
            events.append(ev)
            continue

        m = TASK_TIMEOUT_RE.search(line)
        if m:
            ev = NodeEvent(ts=ts, kind="TaskTimeout", name=m.group("name"),
                           elapsed=int(m.group("elapsed")), timeout=int(m.group("timeout")),
                           level="warn", raw=line)
            ev.detail = ev.summary()
            events.append(ev)
            continue

        m = ENGINE_SIZE_RE.search(line)
        if m:
            ev = NodeEvent(ts=ts, kind="EngineFrame",
                           size=[int(m.group("w")), int(m.group("h"))],
                           level="info", raw=line)
            ev.detail = ev.summary()
            events.append(ev)
            continue

        m = TASK_END_RE.search(line)
        if m:
            try:
                cb = json.loads(m.group("json"))
            except ValueError:
                cb = {}
            ev = NodeEvent(ts=ts, kind="TaskEnd", name=str(cb.get("entry") or ""),
                           level="ok" if m.group("ret") == "true" else "err", raw=line)
            ev.detail = m.group("ret")
            ev.detail = f"entry={ev.name} ret={m.group('ret')}"
            events.append(ev)
    return events


def node_id_of_pipeline_name(flow, name):
    """pipeline 节点名 → 画布节点 id。
    branch 的 _Hit、switch 展开的 _Jk/_Jk_Hit 都归到所属的那个画布节点；
    收口 VF_x_End 与流程外节点（Common_*）返回 None。"""
    if not name:
        return None
    m = _name_to_node(flow)
    if name in m:
        return m[name]
    if name.endswith("_Hit") and name[:-4] in m:
        return m[name[:-4]]
    mm = re.match(r"^(.*)_J\d+(_Hit)?$", name)
    if mm:
        cand = f"{mm.group(1)}_J1"
        if cand in m:
            return m[cand]
    return None


class EngineLogReader:
    """增量读手机上的引擎日志。只读，不写手机任何文件。"""
    REMOTE = "files/maa_logs/maafw.log"

    def __init__(self, adb_call=None):
        self._adb_call = adb_call or adb

    def mark(self):
        """记录当前日志字节数，作为增量起点"""
        r = self._adb_call("shell",
                           f"run-as {PKG} sh -c 'wc -c < {self.REMOTE}'", timeout=30)
        out = adb_text(r)
        try:
            return int(out.split()[0])
        except (IndexError, ValueError):
            return 0

    def read_since(self, offset):
        """只取新增部分：tail -c +<offset+1>（设备上 tail 支持该用法，已实测）"""
        r = self._adb_call("shell",
                           f"run-as {PKG} sh -c 'tail -c +{offset + 1} {self.REMOTE}'",
                           timeout=60)
        return adb_text(r) + "\n"

    def read_all(self):
        r = self._adb_call("exec-out", "run-as", PKG, "cat", self.REMOTE, timeout=120)
        data = r.stdout or b""
        return data.decode("utf-8", "replace")

    def read_tail(self, max_bytes=2_000_000):
        """只取日志末尾若干字节。历史日志有十几 MB，离线回放全量解析既慢又没必要。"""
        r = self._adb_call("shell",
                           f"run-as {PKG} sh -c 'tail -c -{max_bytes} {self.REMOTE}'",
                           timeout=90)
        return adb_text(r) + "\n"


def remote_pipeline_hash(name, adb_call=None):
    """取手机上的 vf_<name>.json，去掉 $meta 后重算指纹。
    与本地 $meta.pipelineHash 比对即可回答「手机上跑的是不是本地这一版」。"""
    adb_call = adb_call or adb
    remote = f"files/taskpacks/whmx/pipeline/vf_{safe_name(name)}.json"
    r = adb_call("exec-out", "run-as", PKG, "cat", remote, timeout=60)
    raw = (r.stdout or b"").decode("utf-8", "replace")
    if not raw.strip():
        return None, "读不到手机上的 " + remote
    try:
        data = jsonc_loads(raw)
    except ValueError as ex:
        return None, f"手机上的 {remote} 解析失败: {ex}"
    return pipeline_fingerprint(data), None


# ================= ADB =================

def adb(*args, timeout=90):
    return subprocess.run([ADB, "-s", DEVICE, *args],
                          capture_output=True, timeout=timeout)


def adb_text(r):
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def jsonc_loads(text):
    """解析带 // 与 /* */ 注释的 JSON（字符串感知）"""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return json.loads("".join(out))


def upsert_flow_task(data, flow_name, log=None):
    """注册可视化流程到清单：
      - 清单里已有同名正式任务 → 转正（entry 切到 VF_ 流程，主队列直接生效），
        并移除之前的独立小工具条目（避免重复）
      - 否则 → 追加为【小工具】分组的独立任务
      其它 VF_ 条目不受影响"""
    entry = f"VF_{flow_name}"
    tasks = data.setdefault("task", [])
    promoted = False
    for t in tasks:
        if t.get("name") == flow_name and t.get("entry") != entry:
            old = t.get("entry")
            t["entry"] = entry
            promoted = True
            if log:
                log(f"清单任务【{flow_name}】入口已切换 → {entry}"
                    + (f"（原 {old}）" if old else "") + "，主队列生效")
    # 转正后移除指向同一流程的独立小工具条目（防重复）
    tasks[:] = [t for t in tasks
                if not (t.get("entry") == entry and t.get("group") == ["tools"])]
    if not any(t.get("entry") == entry for t in tasks):
        tasks.append({"name": flow_name, "label": flow_name, "entry": entry,
                      "group": ["tools"]})


def register_on_phone(flow_name, log):
    """把流程注册进手机端 interface.json（group=tools），重启 App 后出现在【小工具】栏。
    只改手机上的运行副本，本地 whmx/interface.json 不动；改前手机端备份 .bak。"""
    text = adb_text(adb("shell",
                        f"run-as {PKG} sh -c 'cat files/taskpacks/whmx/interface.json'"))
    if not text:
        raise RuntimeError("读取手机 interface.json 失败（任务包是否已安装?）")
    data = jsonc_loads(text)
    upsert_flow_task(data, flow_name, log)
    adb("shell",
        f"run-as {PKG} sh -c 'cp files/taskpacks/whmx/interface.json"
        f" files/taskpacks/whmx/interface.json.bak'")
    local = os.path.join(BUILD_DIR, "_interface.json")
    os.makedirs(BUILD_DIR, exist_ok=True)
    with open(local, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    remote = "/data/local/tmp/_vf_interface.json"
    r = adb("push", local, remote, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("push interface.json 失败: " +
                           (r.stderr.decode("utf-8", "replace") or adb_text(r)))
    adb("shell", "chmod", "644", remote)
    adb("shell",
        f"run-as {PKG} sh -c 'cp {remote} files/taskpacks/whmx/interface.json'")
    check = adb_text(adb("shell",
                         f"run-as {PKG} sh -c 'cat files/taskpacks/whmx/interface.json'"))
    if f"VF_{flow_name}" not in check:
        raise RuntimeError("写回 interface.json 后验证失败（未看到新流程条目）")
    log(f"已注册到手机【小工具】清单: {flow_name} → entry VF_{flow_name}"
        f"（原清单已备份为 interface.json.bak）")


def grab_frame_to(path):
    """FrameSave 三步抓帧 → 存到 path；返回错误信息或 None"""
    import time as _t
    adb("shell", f"run-as {PKG} sh -c 'rm -f files/cur_frame.jpg'")
    adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity")
    _t.sleep(1.5)
    adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity",
        "--activity-single-top", "--es", "entry", "FrameSave")
    _t.sleep(5)
    out = adb("exec-out", "run-as", PKG, "cat", "files/cur_frame.jpg")
    data = out.stdout
    if not data or len(data) < 2000:
        return "抓帧失败：帧为空。请确认 App 在前台、虚拟屏已启动、游戏画面正常。"
    with open(path, "wb") as f:
        f.write(data)
    return None


def sync_pipeline_file(json_path, log):
    """推送生成物到手机 pipeline 目录（只新增 vf_*.json，不动项目文件）。"""
    base = os.path.basename(json_path)
    remote = "/data/local/tmp/" + base
    log(f"推送 {base} …")
    r = adb("push", json_path, remote, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("push 失败: " +
                           (r.stderr.decode("utf-8", "replace") or adb_text(r)))
    adb("shell", "chmod", "644", remote)
    adb("shell",
        f"run-as {PKG} sh -c 'cp {remote} files/taskpacks/whmx/pipeline/{base}'",
        timeout=60)
    names = adb_text(adb("shell",
                         f"run-as {PKG} sh -c 'ls files/taskpacks/whmx/pipeline/'")).splitlines()
    if base not in names:
        raise RuntimeError("同步后未在手机上找到该文件（cp 失败，App 是否有 run-as 权限?）")
    log(f"已同步到 files/taskpacks/whmx/pipeline/{base}")


def flow_templates(flow):
    """收集流程引用的全部模板图文件名（多候选逗号分隔逐个收集）"""
    return sorted({t for nd in flow["nodes"].values()
                   if nd.get("type") in ("tpl_click", "wait_tpl", "branch")
                   for t in split_tpls(nd["props"].get("template"))})


def sync_templates(flow, log, progress=None):
    """把流程引用的模板图推到手机 image 目录（缺哪张补哪张，最后验证）"""
    tpls = flow_templates(flow)
    if not tpls:
        return
    missing_pc = [t for t in tpls if not os.path.isfile(os.path.join(IMG_DIR, t))]
    if missing_pc:
        raise RuntimeError("模板文件不存在，无法同步: " + ", ".join(missing_pc))
    have = set(adb_text(adb("shell",
                f"run-as {PKG} sh -c 'ls files/taskpacks/whmx/image/'")).splitlines())
    todo = [t for t in tpls if t not in have]
    if not todo:
        log(f"模板图 {len(tpls)} 张手机上已齐，跳过推送")
        return
    log(f"同步模板图 {len(todo)}/{len(tpls)} 张…")
    for i, name in enumerate(todo):
        if progress:
            progress(30 + 25 * i // len(todo),
                     f"同步模板图 {i + 1}/{len(todo)}：{name}")
        remote = "/data/local/tmp/_vt_" + name
        r = adb("push", os.path.join(IMG_DIR, name), remote, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"push 模板 {name} 失败")
        adb("shell", "chmod", "644", remote)
        adb("shell", f"run-as {PKG} sh -c 'cp {remote} files/taskpacks/whmx/image/{name}'")
    names = adb_text(adb("shell",
                         f"run-as {PKG} sh -c 'ls files/taskpacks/whmx/image/'")).splitlines()
    missing = [t for t in tpls if t not in names]
    if missing:
        raise RuntimeError("手机 image 目录仍缺模板: " + ", ".join(missing))
    log(f"✓ 模板图已同步 {len(todo)} 张")


def launch_on_phone(entry_name, log, status, ask=True):
    """重启 App（引擎重新加载 pipeline）并用直达入口运行；vd=true 自动先建虚拟屏。"""
    if ask and not messagebox.askyesno(
            "同步并运行",
            "将 force-stop 重启 App 以加载新流程（虚拟屏会被清掉，运行时会自动重建），并立即运行：\n\n"
            f"  {entry_name}\n\n继续？"):
        return
    import time as _t
    log("重启 App（force-stop 会清虚拟屏，稍后自动重建）…")
    status("重启 App 中…")
    adb("shell", "am", "force-stop", PKG)
    _t.sleep(1.5)
    adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity")
    _t.sleep(2)
    adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity", "--activity-single-top",
        "--es", "entry", entry_name, "--ez", "vd", "true")
    log(f"已下发直达入口: entry={entry_name} vd=true（任务进入小工具队列自动执行，虚拟屏会先重建）")
    log("查看运行日志：手机端 App 日志区，或 adb logcat -s MaaWH")


# ================= GUI =================

def _round_rect(c, x0, y0, x1, y1, r, tags=(), **kw):
    """圆角矩形（smooth polygon）"""
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
           x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, tags=tags, **kw)


class SyncDialog(tk.Toplevel):
    """同步进度小窗：步骤文字 + 进度条；完成（成功 2.5s 自动关）/失败停留"""

    def __init__(self, parent, title):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=THEME["panel"])
        self.resizable(False, False)
        self.transient(parent)
        self.attributes("-topmost", True)
        self.geometry("+%d+%d" % (parent.winfo_rootx() + parent.winfo_width() // 2 - 190,
                                  parent.winfo_rooty() + parent.winfo_height() // 2 - 60))
        ttk.Label(self, text=title, style="Title.TLabel").pack(pady=(14, 4))
        self.var_text = tk.StringVar(value="准备中…")
        ttk.Label(self, textvariable=self.var_text, style="Dim.TLabel").pack()
        self.bar = ttk.Progressbar(self, length=320, maximum=100, value=2,
                                   mode="determinate")
        self.bar.pack(padx=26, pady=14)
        self.btn = ttk.Button(self, text="关闭", command=self.destroy, state="disabled")
        self.btn.pack(pady=(0, 14))
        self._done = False
        self.protocol("WM_DELETE_WINDOW", self._try_close)
        self.lift()
        self.focus_force()

    def set_progress(self, pct, text):
        self.var_text.set(text)
        self.bar.configure(value=pct)

    def finish(self, ok, text):
        self._done = True
        self.var_text.set(text)
        self.bar.configure(value=100)
        self.btn.config(state="normal")
        if ok:
            self.after(2500, self.destroy)

    def _try_close(self):
        if self._done:
            self.destroy()


class FlowEditor:
    def __init__(self, root, load_path=None):
        self.root = root
        root.title("流程编辑器 · MaaWH")
        root.geometry("1640x960")
        root.configure(bg=THEME["bg"])
        self._setup_style()

        self.flow = new_flow()
        self.frame_wh = (FRAME_W, FRAME_H)
        self.bg_pil = None          # 背景帧 PIL（调暗后）
        self.bg_photo = None
        self.bg_disp = None         # (ox, oy, w, h) 帧显示区域
        self.show_bg = tk.BooleanVar(value=True)
        self.sel = None
        self.templates = list_templates()
        self._frame_file = None     # 当前背景帧文件路径（联动框选工具）
        self.pick_target = None     # 取坐标模式: "x" | "x1" | "x2"
        self.drag = None
        self.wire = None
        self.prop_widgets = {}
        self._tpl_img_cache = {}   # 模板名 -> (mtime, PhotoImage, dw, dh)
        self._loaded_path = None   # 当前打开的流程文件路径
        self._var_traces = []      # (StringVar, trace_id)，面板重建时统一解除
        self._tpl_pop = None       # 模板自动补全弹出列表
        self._nid = 0
        self._syncing = False
        self._issues_by_node = {}  # node_id -> [Issue]，供画布/属性面板标注问题节点
        # 运行回放（P1-8）状态：事件流 + 当前高亮节点/识别框/动作落点
        self._replay = {"events": [], "offset": 0, "running": False}
        self._replay_hl = None
        self._replay_box = None
        self._replay_point = None
        self._engine_frame = None   # 引擎识别帧尺寸（从日志的 EngineFrame 事件得知）
        self.replay_win = None
        self._undo = []             # 撤销栈：[(flow 文本, 合并标签, 时间)]（P2-4）
        self._redo = []
        self._clipboard = None      # 复制的节点（P2-5）
        self.roi_pick = None        # ROI 拖框状态（P2-6）
        self.tpl_win = None         # 模板管理窗口
        # 画布视图：模型坐标（world）不变，绘制时统一乘 zoom，交互时统一除 zoom。
        # 这样节点永远存在同一处，缩放/平移只是「看的方式」。
        self.zoom = 1.0
        self._pan = None            # 中键拖动平移状态
        self._bg_cache = None       # 背景帧 PhotoImage 缓存（按 zoom 失效）

        self._build_toolbar()
        self._build_statusbar()
        self._build_palette()
        self._build_canvas()
        self._build_props()
        self.redraw()

        if load_path and not os.path.isfile(load_path):
            load_path = None   # 启动参数指向的文件不存在（如已改名），回退最近流程
        if not load_path:
            recent = os.path.join(TOOLS_DIR, "recent.txt")
            if os.path.isfile(recent):
                try:
                    rp = open(recent, encoding="utf-8").read().strip()
                    if rp and os.path.isfile(rp):
                        load_path = rp
                except Exception:
                    pass
        if not load_path and list_flows():
            load_path = list_flows()[0]
        if load_path:
            self.root.after(200, lambda: self.open_flow_file(load_path))
        # 启动就把工程根打在日志里：模板图/生成物落到哪个任务包一目了然
        self.root.after(150, lambda: self.log(
            project_paths.describe(), "" if project_paths.PACK_OK else "warn"))

    # ---------- 主题 ----------

    def _setup_style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        T = THEME
        s.configure(".", background=T["panel"], foreground=T["text"], font=FONT)
        s.configure("TFrame", background=T["panel"])
        s.configure("Bar.TFrame", background=T["bg"])
        s.configure("TLabel", background=T["panel"], foreground=T["text"])
        s.configure("Title.TLabel", font=FONT_TITLE, foreground=T["text"])
        s.configure("Group.TLabel", background=T["panel"], foreground=T["accent"],
                    font=FONT_B)
        s.configure("Dim.TLabel", foreground=T["text_dim"], font=FONT_SM)
        s.configure("TEntry", fieldbackground=T["field"], foreground=T["text"],
                    insertcolor=T["text"], bordercolor=T["card_line"],
                    lightcolor=T["field"], darkcolor=T["field"])
        s.map("TEntry", bordercolor=[("focus", T["accent"])],
              lightcolor=[("focus", T["field"])], darkcolor=[("focus", T["field"])])
        s.configure("TCombobox", fieldbackground=T["field"], foreground=T["text"],
                    arrowcolor=T["text_dim"], bordercolor=T["card_line"],
                    lightcolor=T["field"], darkcolor=T["field"],
                    selectbackground=T["accent"], selectforeground=T["text"])
        s.map("TCombobox",
              fieldbackground=[("readonly", T["field"])],
              foreground=[("readonly", T["text"])],
              bordercolor=[("focus", T["accent"])])
        self.root.option_add("*TCombobox*Listbox.background", T["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", T["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", T["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", T["text"])
        s.configure("TSpinbox", fieldbackground=T["field"], foreground=T["text"],
                    arrowcolor=T["text_dim"], bordercolor=T["card_line"],
                    lightcolor=T["field"], darkcolor=T["field"])
        s.configure("TCheckbutton", background=T["panel"], foreground=T["text"],
                    focuscolor=T["panel"])
        s.map("TCheckbutton",
              background=[("active", T["panel"])],
              indicatorcolor=[("selected", T["accent"]), ("!selected", T["field"])])
        s.configure("TSeparator", background="#323949")
        s.configure("Horizontal.TProgressbar", background=T["accent"],
                    troughcolor=T["field"], bordercolor=T["panel"],
                    lightcolor=T["accent"], darkcolor=T["accent"])

    def _flat_btn(self, parent, text, cmd, bg=None, fg=None, hover=None,
                  font=FONT, padx=12):
        T = THEME
        bg = bg or T["card"]
        fg = fg or T["text"]
        hover = hover or T["card_hi"]
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=hover, activeforeground=fg,
                      relief="flat", bd=0, padx=padx, pady=4,
                      font=font, cursor="hand2", highlightthickness=0)
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    def _vsep(self, parent):
        bar = tk.Frame(parent, width=1, bg="#333a4a")
        bar.pack(side="left", fill="y", padx=9, pady=6)
        return bar

    # ---------- 布局构建 ----------

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, style="Bar.TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="流程名", style="Dim.TLabel",
                  background=THEME["bg"]).pack(side="left", padx=(12, 4))
        self.name_var = tk.StringVar(value=self.flow["name"])
        ent = ttk.Entry(bar, textvariable=self.name_var, width=15, font=FONT)
        ent.pack(side="left", ipady=3)
        ent.bind("<KeyRelease>", lambda e: self._on_name_change())

        self._flat_btn(bar, "✚ 新建", self.on_new).pack(side="left", padx=(12, 3))
        self.flow_combo = ttk.Combobox(bar, width=18, state="readonly", font=FONT,
                                       values=[os.path.basename(p) for p in list_flows()])
        self.flow_combo.pack(side="left", padx=3, ipady=2)
        self._flat_btn(bar, "打开 ▶", self.on_open_selected).pack(side="left", padx=3)
        self._flat_btn(bar, "保存", self.on_save).pack(side="left", padx=3)

        self._vsep(bar)
        self._flat_btn(bar, "✓ 校验", self.on_validate).pack(side="left", padx=3)
        self._flat_btn(bar, "⤓ 生成 JSON", self.on_build).pack(side="left", padx=3)

        self._vsep(bar)
        self._btn_sync = self._flat_btn(
            bar, "⇲ 同步到手机", lambda: self.on_sync(run_after=False),
            bg="#2c4a86", fg="#eaf1ff", hover="#3a5da8", font=FONT_B)
        self._btn_sync.pack(side="left", padx=3, ipady=1)
        self._btn_run = self._flat_btn(
            bar, "▶ 同步并运行", lambda: self.on_sync(run_after=True),
            bg="#2c6e48", fg="#eafff2", hover="#3a8a5c", font=FONT_B)
        self._btn_run.pack(side="left", padx=3, ipady=1)
        ttk.Label(bar, text="  同步后需重启 App 生效",
                  style="Dim.TLabel", background=THEME["bg"]).pack(side="left")

    def _build_palette(self):
        left = ttk.Frame(self.root, width=172)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ttk.Label(left, text="  节 点 库", style="Title.TLabel").pack(
            anchor="w", padx=8, pady=(10, 4))
        ttk.Label(left, text="  点击添加到流程末尾", style="Dim.TLabel").pack(
            anchor="w", padx=8, pady=(0, 6))
        for t in TYPE_ORDER:
            spec = NODE_TYPES[t]
            b = tk.Button(left, text=f" {spec['icon']}  {spec['label']}",
                          command=lambda tt=t: self.add_node(tt),
                          bg=THEME["panel"], fg=spec["light"],
                          activebackground=THEME["card_hi"],
                          activeforeground=spec["light"],
                          relief="flat", bd=0, anchor="w", padx=14, pady=6,
                          font=FONT, cursor="hand2", highlightthickness=0)
            b.pack(fill="x", padx=8, pady=1)
            b.bind("<Enter>", lambda e, bb=b: bb.config(bg=THEME["card_hi"]))
            b.bind("<Leave>", lambda e, bb=b: bb.config(bg=THEME["panel"]))
        self._flat_btn(left, "✥  整理布局", self.tidy_layout,
                       font=FONT_SM).pack(fill="x", padx=8, pady=(12, 0))
        zrow = ttk.Frame(left)
        zrow.pack(fill="x", padx=8, pady=(4, 0))
        self._flat_btn(zrow, "－", lambda: self.zoom_out(), padx=7,
                       font=FONT_SM).pack(side="left")
        self._flat_btn(zrow, "100%", lambda: self.zoom_reset(), padx=7,
                       font=FONT_SM).pack(side="left", padx=3)
        self._flat_btn(zrow, "＋", lambda: self.zoom_in(), padx=7,
                       font=FONT_SM).pack(side="left")
        self._flat_btn(left, "⤢  适配窗口", lambda: self.zoom_fit(),
                       font=FONT_SM).pack(fill="x", padx=8, pady=1)

        ttk.Separator(left).pack(fill="x", pady=12, padx=8)
        ttk.Label(left, text="  背景帧 · 对照坐标", style="Title.TLabel").pack(
            anchor="w", padx=8, pady=(0, 4))
        self._flat_btn(left, "⟳  抓帧 (F5)", self.on_capture).pack(fill="x", padx=8, pady=1)
        self._flat_btn(left, "🩺  运行回放…", self.on_replay_open).pack(fill="x", padx=8, pady=1)
        self._flat_btn(left, "✛  框选模板…", self.on_pick_template).pack(fill="x", padx=8, pady=1)
        self._flat_btn(left, "📂  打开帧图…", self.on_open_frame).pack(fill="x", padx=8, pady=1)
        self._flat_btn(left, "🗂  模板管理…", self.on_template_manager).pack(
            fill="x", padx=8, pady=1)
        ttk.Checkbutton(left, text="显示背景帧", variable=self.show_bg,
                        command=self.redraw).pack(anchor="w", padx=12, pady=3)
        self.frame_lbl = ttk.Label(left, text="", style="Dim.TLabel", justify="left")
        self.frame_lbl.pack(anchor="w", padx=12, pady=2)
        self._update_frame_label()

    def _build_canvas(self):
        # 画布外套一层容器，好在下沿挂横向滚动条：流程 40 个节点时主链列在
        # 帧右边缘之外，没有横向滚动就够不到（以前只有 MouseWheel→yview）
        wrap = ttk.Frame(self.root)
        wrap.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=THEME["canvas"], highlightthickness=0)
        self.xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self.xsb.set)
        self.xsb.pack(side="bottom", fill="x")
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.configure(takefocus=1)      # 让方向键微调能落到画布上
        self.canvas.bind("<ButtonPress-1>", self.on_down)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_up)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Delete>", self.on_delete_key)
        # 中键拖动平移 / Ctrl+滚轮缩放（见 _on_mousewheel）
        self.canvas.bind("<Button-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_move)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_end)
        for key, dx, dy in (("<Left>", -4, 0), ("<Right>", 4, 0),
                            ("<Up>", 0, -4), ("<Down>", 0, 4),
                            ("<Shift-Left>", -20, 0), ("<Shift-Right>", 20, 0),
                            ("<Shift-Up>", 0, -20), ("<Shift-Down>", 0, 20)):
            self.canvas.bind(key, lambda e, dx=dx, dy=dy: (self.nudge_node(dx, dy),
                                                           "break")[1])
        self.root.bind("<F5>", lambda e: self.on_capture())
        self.root.bind("<Control-s>", lambda e: (self.on_save(), "break")[1])
        self.root.bind("<Control-z>", lambda e: (self.undo(), "break")[1])
        self.root.bind("<Control-Z>", lambda e: (self.redo(), "break")[1])
        self.root.bind("<Control-y>", lambda e: (self.redo(), "break")[1])
        self.root.bind("<Control-c>", lambda e: (self.on_copy(), "break")[1])
        self.root.bind("<Control-v>", lambda e: (self.on_paste(), "break")[1])
        self.root.bind("<Control-d>", lambda e: (self.on_duplicate(), "break")[1])
        self.root.bind("<Escape>", self.on_escape)
        self.root.bind("<Control-plus>", lambda e: (self.zoom_in(), "break")[1])
        self.root.bind("<Control-equal>", lambda e: (self.zoom_in(), "break")[1])
        self.root.bind("<Control-minus>", lambda e: (self.zoom_out(), "break")[1])
        self.root.bind("<Control-Key-0>", lambda e: (self.zoom_reset(), "break")[1])

    def _build_props(self):
        right = ttk.Frame(self.root, width=360)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        ttk.Label(right, text="  节点属性", style="Title.TLabel").pack(
            anchor="w", padx=8, pady=(10, 2))

        # 操作按钮与分支出口固定在顶部、不随字段滚动：
        # 字段多的节点（OCR 现在有 14 个字段 + 分组标题）会把滚动区撑得很长，
        # 以前按钮排在字段下面，结果被顶出可视区 —— 删除按钮就"消失"了。
        prow = ttk.Frame(right)
        prow.pack(side="top", fill="x", padx=10, pady=(2, 4))
        self._flat_btn(prow, "↑ 上移", lambda: self.move_node(-1), padx=8).pack(side="left", padx=2)
        self._flat_btn(prow, "↓ 下移", lambda: self.move_node(1), padx=8).pack(side="left", padx=2)
        self._flat_btn(prow, "⇥ 挪到侧列", lambda: self.align_node(), padx=8,
                       font=FONT_SM).pack(side="left", padx=2)
        self._flat_btn(prow, "✖ 删除", self.delete_selected, padx=8,
                       bg="#5a2733", fg="#ffc9d2", hover="#74323f",
                       font=FONT_SM).pack(side="left", padx=(12, 0))

        ttk.Separator(right).pack(side="top", fill="x", pady=2, padx=8)
        ttk.Label(right, text="  分支出口（画布拖端口或下拉改接）",
                  style="Title.TLabel").pack(side="top", anchor="w", padx=8)
        brow = ttk.Frame(right)
        brow.pack(side="top", fill="x", padx=12, pady=4)
        ttk.Label(brow, text="✓", foreground=THEME["ok"],
                  background=THEME["panel"]).pack(side="left")
        self.hit_combo = ttk.Combobox(brow, width=19, state="disabled", font=FONT_SM)
        self.hit_combo.pack(side="left", padx=(2, 8))
        self.hit_combo.bind("<<ComboboxSelected>>", lambda e: self.on_branch_combo("hit_next"))
        ttk.Label(brow, text="✗", foreground=THEME["err"],
                  background=THEME["panel"]).pack(side="left")
        self.miss_combo = ttk.Combobox(brow, width=19, state="disabled", font=FONT_SM)
        self.miss_combo.pack(side="left", padx=2)
        self.miss_combo.bind("<<ComboboxSelected>>", lambda e: self.on_branch_combo("miss_next"))
        self.branch_hint = ttk.Label(right, text="", style="Dim.TLabel",
                                     justify="left", wraplength=320)
        self.branch_hint.pack(side="top", anchor="w", padx=12, pady=(2, 0))
        ttk.Separator(right).pack(side="top", fill="x", pady=4, padx=8)

        # 属性区可滚动：字段多时滚轮下翻；上面的按钮与出口始终可见
        wrap = ttk.Frame(right)
        wrap.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=THEME["panel"], highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._props_canvas = canvas
        inner = ttk.Frame(canvas)
        self._props_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_width(e):
            try:
                canvas.itemconfigure(self._props_win, width=max(1, e.width))
            except tk.TclError:
                pass   # 窗口销毁过程中的回调，忽略
        def _sync_scroll(e):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except tk.TclError:
                pass
        inner.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)

        def _wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        wrap.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        wrap.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # 字段区单独成容器：打开流程时只重建这里
        self.props_inner = ttk.Frame(inner)
        self.props_inner.pack(fill="x", padx=12)

        # 底部日志（固定，不随属性区滚动）
        ttk.Separator(right).pack(side="bottom", fill="x", pady=8, padx=8)
        ttk.Label(right, text="  日志", style="Title.TLabel").pack(side="bottom", anchor="w", padx=8)
        logf = tk.Frame(right, bg=THEME["card_line"])
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = tk.Text(logf, height=12, bg="#12141c", fg="#c6cede",
                                font=LOG_FONT, state="disabled", relief="flat",
                                padx=8, pady=6, selectbackground=THEME["accent"],
                                insertbackground=THEME["text"])
        self.log_text.tag_config("err", foreground="#ff8a8a")
        self.log_text.tag_config("warn", foreground=THEME["warn"])
        self.log_text.tag_config("ok", foreground=THEME["ok"])
        self.log_text.pack(fill="both", expand=True, padx=1, pady=1)

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="就绪 · F5 抓帧 ｜ 拖动节点排序 ｜ 拖分支端口连线 ｜ "
                                             "Ctrl+滚轮缩放 ｜ 中键拖动平移 ｜ Delete 删除")
        tk.Label(self.root, textvariable=self.status_var, bg=THEME["bg"],
                 fg=THEME["text_dim"], anchor="w", padx=10, pady=3,
                 font=FONT_SM).pack(fill="x", side="bottom")

    # ---------- 日志/状态 ----------

    def log(self, msg, tag=None):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        print(msg)

    def status(self, s):
        self.status_var.set(s)

    # ---------- 流程文件操作 ----------

    def _on_name_change(self):
        self.flow["name"] = self.name_var.get().strip() or "未命名"

    def on_new(self):
        self._undo.clear()
        self._redo.clear()
        self.flow = new_flow("测试流程")
        self.name_var.set(self.flow["name"])
        self.sel = None
        self.build_prop_panel()
        self.redraw()
        self.log("已新建空白流程")

    def on_open_selected(self, *_):
        self.flow_combo.config(values=[os.path.basename(p) for p in list_flows()])
        sel = self.flow_combo.get()
        if sel:
            self.open_flow_file(os.path.join(FLOWS_DIR, sel))

    def open_flow_file(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if "chain" not in data or "nodes" not in data:
                raise ValueError("不是流程定义文件")
            self._undo.clear()
            self._redo.clear()
            self.flow = normalize_flow(data)
            self.flow.setdefault("chain", [])
            self.flow.setdefault("nodes", {})
            self.name_var.set(self.flow.get("name", "未命名"))
            self.sel = None
            self.flow_combo.set(os.path.basename(path))
            self._loaded_path = path
            try:
                open(os.path.join(TOOLS_DIR, "recent.txt"), "w",
                     encoding="utf-8").write(path)
            except OSError:
                pass
            self.build_prop_panel()
            self.redraw()
            self.canvas.yview_moveto(0)
            self.canvas.xview_moveto(0)
            self.log(f"已打开 {os.path.basename(path)}（{len(self.flow['chain'])} 个节点）")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            try:
                open(os.path.join(TOOLS_DIR, "ui_error.log"), "w",
                     encoding="utf-8").write(tb)
            except OSError:
                pass
            self.log("✗ 打开异常（详情已写入 FlowEditor/ui_error.log）: " + str(e), "err")
            messagebox.showerror("打开失败", str(e) +
                                 "（详情见 FlowEditor/ui_error.log）")

    def on_save(self):
        self._on_name_change()
        old = self._loaded_path
        path = save_flow(self.flow)
        if old and os.path.abspath(old) != os.path.abspath(path) and os.path.isfile(old):
            try:
                os.remove(old)
                self.log("已移除改名前的旧文件 " + old)
            except OSError:
                pass
        self._loaded_path = path
        try:
            open(os.path.join(TOOLS_DIR, "recent.txt"), "w",
                 encoding="utf-8").write(path)
        except OSError:
            pass
        self.flow_combo.config(values=[os.path.basename(p) for p in list_flows()])
        self.flow_combo.set(os.path.basename(path))
        self.log("✓ 已保存 " + path, "ok")

    # ---------- 节点增删改 ----------

    def add_node(self, ntype, props=None, hit_next=None, miss_next=None, pos=None):
        self._snapshot()
        self._nid += 1
        nid = f"n{self._nid:03d}{os.urandom(2).hex()}"
        spec = NODE_TYPES[ntype]
        p = dict(spec["defaults"])
        if props:
            p.update(props)
        if pos:
            x, y = pos
        else:
            last = self.flow["chain"][-1] if self.flow["chain"] else None
            if last:
                nd = self.flow["nodes"][last]
                x, y = nd["x"], nd["y"] + CARD_H + 30
            else:
                x, y = self._chain_col_x(), 60
        node = {"type": ntype, "x": x, "y": y, "title": spec["label"], "props": p}
        if ntype == "branch":
            node["hit_next"] = hit_next
            node["miss_next"] = miss_next
        self.flow["nodes"][nid] = node
        self.flow["chain"].append(nid)
        self.sel = nid
        self.build_prop_panel()
        self.redraw()
        self._scroll_to(y)
        return nid

    def delete_selected(self):
        nid = self.sel
        if not nid:
            return
        if not messagebox.askyesno("删除节点", "确定删除该节点？"):
            return
        self._snapshot()
        self.flow["nodes"].pop(nid, None)
        if nid in self.flow["chain"]:
            self.flow["chain"].remove(nid)
        for other in self.flow["nodes"].values():
            if other.get("hit_next") == nid:
                other["hit_next"] = None
            if other.get("miss_next") == nid:
                other["miss_next"] = None
            if other.get("type") == "switch":
                op = other.get("props", {})
                if op.get("miss_next") == nid:
                    op["miss_next"] = None
                for c in op.get("candidates", []):
                    if isinstance(c, dict) and c.get("next") == nid:
                        c["next"] = None
            if other.get("type") == "loop":
                lop = other.get("props", {})
                if lop.get("body_end") == nid:
                    lop["body_end"] = None
        self.sel = None
        self.build_prop_panel()
        self.redraw()
        self.log("已删除节点")

    def on_delete_key(self, _e):
        self.delete_selected()

    def move_node(self, d):
        nid = self.sel
        if not nid or nid not in self.flow["chain"]:
            return
        ch = self.flow["chain"]
        i = ch.index(nid)
        j = i + d
        if 0 <= j < len(ch):
            self._snapshot()
            ch[i], ch[j] = ch[j], ch[i]
            self.build_prop_panel()
            self.redraw()

    def align_node(self):
        nid = self.sel
        if not nid:
            return
        self._snapshot(f"align:{nid}")
        self.flow["nodes"][nid]["x"] = self._side_col_x()
        self.redraw()
        self._focus_node(nid)      # 挪到侧列后自动滚过去，别让节点"消失"在视野外
        self.log("已挪到侧列（画布已自动滚到该节点；Ctrl+滚轮缩放 / 中键拖动平移）")

    def tidy_layout(self):
        self._snapshot()
        x = self._chain_col_x()
        y = 50
        for nid in self.flow["chain"]:
            nd = self.flow["nodes"][nid]
            nd["x"] = x
            nd["y"] = y
            y += CARD_H + 30
        self.redraw()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def _chain_col_x(self):
        fw = self.bg_disp[2] if self.bg_disp else 750
        return fw + 130

    def _side_col_x(self):
        """侧列 x。缩放到最小也放不下时，侧列其实「在右边」——所以 align 之后
        必须自动滚过去（见 align_node），不能让人以为节点没了。"""
        return self._chain_col_x() + CARD_W + 100

    def _scroll_to(self, y):
        sr = self.canvas.cget("scrollregion").split()
        h = float(sr[3]) if len(sr) == 4 else 2000
        self.canvas.yview_moveto(max(0.0, (y * (self.zoom or 1.0) - 120) / max(1.0, h)))

    def _on_mousewheel(self, e):
        """滚轮纵向；Shift+滚轮 横向；Ctrl+滚轮 缩放（以鼠标位置为锚点）"""
        if e.state & 0x0004:
            self.set_zoom(self.zoom * (1.12 if e.delta > 0 else 1 / 1.12),
                          (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)))
        elif e.state & 0x0001:
            self.canvas.xview_scroll(int(-e.delta / 120), "units")
        else:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    # ---------- 平移：中键拖动（scan_mark/scan_dragto，鼠标按住哪就跟着走） ----------

    def on_pan_start(self, e):
        try:
            self.canvas.scan_mark(e.x, e.y)
            self.canvas.config(cursor="fleur")
        except tk.TclError:
            pass

    def on_pan_move(self, e):
        try:
            self.canvas.scan_dragto(e.x, e.y, gain=1)
        except tk.TclError:
            pass

    def on_pan_end(self, _e):
        try:
            self.canvas.config(cursor="")
        except tk.TclError:
            pass

    # ---------- 背景帧 ----------

    def on_capture(self):
        self.status("抓帧中…")
        self.root.update()
        path = os.path.join(TOOLS_DIR, "pick_frame.jpg")
        err = grab_frame_to(path)
        if err:
            self.log(err, "err")
            self.status("抓帧失败")
            return
        self.load_frame(path)
        self.log(f"已抓帧 {path}")

    def on_pick_template(self):
        """唤起模板框选工具（带负样本校验）；保存到 whmx/image 后模板下拉即可选到"""
        tool = os.path.join(TOOLS_DIR, "template_picker.py")
        if not os.path.isfile(tool):
            messagebox.showerror("找不到工具", tool)
            return
        os.makedirs(NEG_DIR, exist_ok=True)
        args = [sys.executable, tool]
        if self._frame_file and os.path.isfile(self._frame_file):
            args.append(self._frame_file)          # 联动当前背景帧
        args += ["--neg", NEG_DIR]
        logf = open(os.path.join(TOOLS_DIR, "template_picker.log"), "a", encoding="utf-8")
        subprocess.Popen(args, stdout=logf, stderr=logf, cwd=TOOLS_DIR)
        self.log("已打开模板框选工具。框完保存后，选中节点在「模板图」下拉里直接选新模板。")

    def on_open_frame(self):
        p = filedialog.askopenfilename(title="选择帧图", initialdir=TOOLS_DIR,
                                       filetypes=[("图片", "*.png *.jpg *.jpeg")])
        if p:
            self.load_frame(p)

    def load_frame(self, path):
        img = Image.open(path).convert("RGB")
        self._frame_file = path
        self.frame_wh = img.size
        dark = Image.new("RGB", img.size, (24, 26, 34))
        self.bg_pil = Image.blend(dark, img, 0.62)
        self._update_frame_label()
        self.redraw()
        self.log(f"背景帧 {os.path.basename(path)}  {img.size[0]}x{img.size[1]}")

    def _update_frame_label(self):
        self.frame_lbl.config(text=f"  当前 {self.frame_wh[0]}×{self.frame_wh[1]}"
                                   f"（基准 {FRAME_W}×{FRAME_H}）")

    # ---------- 绘制 ----------

    def redraw(self):
        try:
            if not self.canvas.winfo_exists():
                return
        except tk.TclError:
            return
        c = self.canvas
        c.delete("all")
        self._apply_zoom_fonts()
        ox, oy = 44, 44
        # 网格
        max_x, max_y = 1400, 1600
        if self.bg_pil is not None and self.show_bg.get():
            max_y = max(max_y, oy + FRAME_DISP_H + 80)
        for nd in self.flow["nodes"].values():
            max_x = max(max_x, nd["x"] + CARD_W + 160)
            max_y = max(max_y, nd["y"] + CARD_H + 140)
        step = 46
        gx = step
        while gx < max_x:
            c.create_line(gx, 0, gx, max_y, fill=THEME["grid"])
            gx += step
        gy = step
        while gy < max_y:
            c.create_line(0, gy, max_x, gy, fill=THEME["grid"])
            gy += step
        # 背景帧（bg_disp 存模型坐标；图片按 zoom 重新生成 —— Tk 的 canvas.scale
        # 只缩放坐标、不会缩放图片，所以图片得自己按缩放后尺寸重建）
        z = self.zoom or 1.0
        if self.bg_pil is not None and self.show_bg.get():
            disp_h = (FRAME_DISP_H if self.bg_pil.height >= self.bg_pil.width
                      else FRAME_DISP_H_LS)
            scale = disp_h / self.bg_pil.height
            dw = int(self.bg_pil.width * scale)
            self.bg_disp = (ox, oy, dw, disp_h)
            self.bg_photo = self._bg_photo_for(dw * z, disp_h * z)
            _round_rect(c, ox - 2, oy - 2, ox + dw + 2, oy + disp_h + 2, 8,
                        fill=THEME["card_line"], outline="")
            c.create_image(ox, oy, anchor="nw", image=self.bg_photo)
            c.create_text(ox + 10, oy + 10, anchor="nw", fill="#cfd6e6",
                          font=self.f_sm,
                          text=f" 参考帧 {self.frame_wh[0]}×{self.frame_wh[1]} ")
            max_x = max(max_x, ox + dw + 40)     # 帧也能横向滚到（以前只算了纵向）
            max_y = max(max_y, oy + disp_h + 40)
        else:
            self.bg_disp = None
        # 主链连线（按 chain 顺序纵向连接）：与 build_pipeline 共用 linear_successor。
        # 生成时被截断的出口（枝干无直落出口 / 分支内容叶 / 收口节点）不画实线箭头，
        # 改画红色虚线 + ⛔ 标记 —— 画布所见必须等于生成结果。
        ch = self.flow["chain"]
        for i in range(len(ch) - 1):
            cur, nxt_id = ch[i], ch[i + 1]
            a, b = self.flow["nodes"][cur], self.flow["nodes"][nxt_id]
            a_bottom = a["y"] + (self._sw_h(a) if a["type"] == "switch" else CARD_H)
            x1, y1 = a["x"] + CARD_W / 2, a_bottom
            x2, y2 = b["x"] + CARD_W / 2, b["y"]
            if linear_successor(self.flow, cur) == nxt_id:
                mid = (y1 + y2) / 2
                c.create_line(x1, y1, x1, mid, x2, mid, x2, y2 - 2, smooth=True,
                              width=2, arrow=tk.LAST, fill=THEME["arrow"],
                              arrowshape=ARROW_SHAPE, splinesteps=24)
            else:
                max_x = max(max_x, self._draw_cut_off(
                    a["x"] + CARD_W / 2, a_bottom, y2, _suppress_reason(self.flow, cur)))
        # 分支/枝干出口连线
        for nid in ch:
            nd = self.flow["nodes"][nid]
            exits = exits_of(self.flow, nid)
            if nd["type"] == "branch":
                for port, target, color in (("hit_next", exits["hit"], THEME["ok"]),
                                            ("miss_next", exits["miss"], THEME["err"])):
                    sx, sy = self._port_pos(nd, port)
                    if target and target in self.flow["nodes"]:
                        t = self.flow["nodes"][target]
                        ex, ey = t["x"] + CARD_W / 2, t["y"]
                        if abs(ex - sx) < 8:
                            c.create_line(sx, sy, ex, ey - 2, width=2, fill=color,
                                          arrow=tk.LAST, smooth=True,
                                          arrowshape=ARROW_SHAPE, splinesteps=24)
                        else:
                            mx = max(sx, ex) + 52
                            c.create_line(sx, sy, mx, sy, mx, ey, ex, ey - 2, smooth=True,
                                          width=2, fill=color, arrow=tk.LAST,
                                          arrowshape=ARROW_SHAPE, splinesteps=24)
                    else:
                        if port == "hit_next":
                            i = ch.index(nid)
                            auto = ch[i + 1] if i + 1 < len(ch) else None
                            txt = "自动→下一个" if auto else "→结束"
                        else:
                            txt = "→结束"
                        c.create_line(sx, sy, sx + 26, sy, fill=color, width=2)
                        tw = 12 * len(txt) + 14
                        _round_rect(c, sx + 28, sy - 11, sx + 28 + tw, sy + 11, 5,
                                    fill="#14161d", outline=THEME["card_line"])
                        c.create_text(sx + 35, sy, anchor="w", fill=color,
                                      font=self.f_sm, text=txt)
                    max_x = max(max_x, sx + 190)
            elif nd["type"] == "switch":
                cands = exits["candidates"]
                for ci, cand in enumerate(cands):
                    tgt = cand.get("next")
                    sx, sy = self._port_pos(nd, f"cand{ci}")
                    if tgt and tgt in self.flow["nodes"]:
                        t = self.flow["nodes"][tgt]
                        ex, ey = t["x"] + CARD_W / 2, t["y"]
                        mx = max(sx, ex) + 52
                        c.create_line(sx, sy, mx, sy, mx, ey, ex, ey - 2, smooth=True,
                                      width=2, fill=THEME["ok"], arrow=tk.LAST,
                                      arrowshape=ARROW_SHAPE, splinesteps=24)
                    else:
                        c.create_line(sx, sy, sx + 26, sy, fill=THEME["ok"], width=2)
                        _round_rect(c, sx + 28, sy - 11, sx + 60, sy + 11, 5,
                                    fill="#14161d", outline=THEME["card_line"])
                        c.create_text(sx + 35, sy, anchor="w", fill=THEME["ok"],
                                      font=self.f_sm, text="→结束")
                    max_x = max(max_x, sx + 190)
                mn = exits["miss"]
                sx, sy = self._port_pos(nd, "miss")
                if mn and mn in self.flow["nodes"]:
                    t = self.flow["nodes"][mn]
                    ex, ey = t["x"] + CARD_W / 2, t["y"]
                    px2 = max(sx, ex) + 52
                    c.create_line(sx, sy, px2, sy, px2, ey, ex, ey - 2, smooth=True,
                                  width=2, fill=THEME["err"], arrow=tk.LAST,
                                  arrowshape=ARROW_SHAPE, splinesteps=24)
                else:
                    c.create_line(sx, sy, sx + 26, sy, fill=THEME["err"], width=2)
                    _round_rect(c, sx + 28, sy - 11, sx + 60, sy + 11, 5,
                                fill="#14161d", outline=THEME["card_line"])
                    c.create_text(sx + 35, sy, anchor="w", fill=THEME["err"],
                                  font=self.f_sm, text="→结束")
                max_x = max(max_x, sx + 190)
            elif nd["type"] == "loop":
                # 循环体括线 + 回边：循环体末尾 → 循环节点（虚线），
                # 并给括线标出次数。与生成结果一致：展开后前 times-1 份的尾部回边。
                self._draw_loop_marks(nid)
        for nid in ch:
            self._draw_node(nid)
        self._draw_branch_labels()
        # 叠加层（ROI / 点击点 / 滑动线）放在最后画：它是「拿帧对照参数」的依据，
        # 被节点卡片盖住就失去意义了
        self._draw_overlays()
        # 回放高亮与识别框（有回放事件时才有东西画）
        self._draw_replay_overlay()
        # ROI 拖框中的临时矩形
        if self.roi_pick and self.roi_pick.get("x0") is not None:
            rp = self.roi_pick
            c.create_rectangle(rp["x0"], rp["y0"], rp["x1"], rp["y1"],
                               outline=THEME["warn"], width=2, dash=(6, 4))
            c.create_text(rp["x0"], rp["y0"] - 8, anchor="sw", fill=THEME["warn"],
                          font=self.f_sm, text="新 ROI")
        if ch:
            first = self.flow["nodes"][ch[0]]
            entry_txt = f"▶ 入口 VF_{self.flow['name']}"
            bw = 40 + 12 * len(entry_txt)
            _round_rect(c, first["x"] + 4, first["y"] - 30, first["x"] + bw,
                        first["y"] - 8, 9, fill="#3a3418", outline="#d8c86a")
            c.create_text(first["x"] + 4 + bw / 2, first["y"] - 19, fill="#ffe9a0",
                          font=self.f_sm, text=entry_txt)
        # 连线拖动临时线
        if self.wire:
            sx, sy = self._port_pos(self.flow["nodes"][self.wire["from"]], self.wire["port"])
            c.create_line(sx, sy, self.wire["mx"], self.wire["my"],
                          fill="#e8d44d", width=2, arrow=tk.LAST,
                          arrowshape=ARROW_SHAPE)
        # 视图变换：整体按 zoom 缩放（模型坐标不变），再按缩放后的范围设滚动区
        if abs(z - 1.0) > 1e-6:
            c.scale("all", 0, 0, z, z)
        c.config(scrollregion=(0, 0, max_x * z, max_y * z))

    def _draw_node(self, nid):
        c = self.canvas
        nd = self.flow["nodes"][nid]
        spec = NODE_TYPES[nd["type"]]
        x, y = nd["x"], nd["y"]
        h = self._sw_h(nd) if nd["type"] == "switch" else CARD_H
        x1, y1 = x + CARD_W, y + h
        selected = (nid == self.sel)
        err_now = any(i.level == "error" for i in self._issues_by_node.get(nid, ()))
        disabled = (nd.get("props") or {}).get("enabled") is False
        tags = ("node", f"node:{nid}")
        # 阴影
        _round_rect(c, x + 3, y + 5, x1 + 3, y1 + 5, 12,
                    fill=THEME["shadow"], outline="")
        # 主体（选中=黄框；有 error 级校验问题=红框；被禁用=压暗，让问题节点一眼可见）
        _round_rect(c, x, y, x1, y1, 12,
                    fill=THEME["panel"] if disabled else THEME["card"],
                    outline=THEME["sel"] if selected
                    else (THEME["err"] if err_now else THEME["card_line"]),
                    width=2 if (selected or err_now) else 1, tags=tags)
        # 左侧类型色条
        _round_rect(c, x + 3, y + 5, x + 9, y1 - 5, 3,
                    fill=THEME["card_line"] if disabled else spec["color"],
                    outline="", tags=tags)
        idx = self.flow["chain"].index(nid) + 1
        c.create_text(x + 20, y + 7, anchor="nw",
                      fill=THEME["text_dim"] if disabled else "white", font=self.f_title,
                      text=f"{idx}. {nd.get('title', spec['label'])}"
                           + ("（已禁用）" if disabled else ""),
                      tags=tags)
        if nd["type"] == "switch":
            # 候选行 + 出口 port
            cands = parse_switch_cands(nd.get("props", {}).get("candidates"))
            for ci, cnd in enumerate(cands):
                cy = y + 40 + 22 * ci + 12
                t = cnd["t"]
                disp = ("OCR:" + t[4:]) if t.lower().startswith("ocr:") else t
                line = f"{ci + 1}. {disp}"
                if len(line) > 18:          # 卡片内按宽度截断，超时移到最右
                    line = line[:17] + "…"
                c.create_text(x + 16, cy, anchor="w", font=self.f_sm,
                              fill=THEME["text"], text=line, tags=tags)
                c.create_text(x + CARD_W - 12, cy, anchor="e", font=self.f_sm,
                              fill=THEME["text_dim"], text=f"{cnd['timeout']}ms",
                              tags=tags)
                hx, hy = self._port_pos(nd, f"cand{ci}")
                c.create_oval(hx - 9, hy - 9, hx + 9, hy + 9,
                              fill="#1c2b1f", outline="")
                c.create_oval(hx - 5, hy - 5, hx + 5, hy + 5, fill=THEME["ok"],
                              outline="#ffffff", width=1,
                              tags=("port", f"port:{nid}:cand{ci}"))
            # 全部未中出口
            mx, my = self._port_pos(nd, "miss")
            c.create_text(x + 16, my, anchor="w", font=self.f_sm,
                          fill=THEME["err"], text="全部未中 ⤷", tags=tags)
            c.create_oval(mx - 9, my - 9, mx + 9, my + 9, fill="#2e1c1e", outline="")
            c.create_oval(mx - 5, my - 5, mx + 5, my + 5, fill=THEME["err"],
                          outline="#ffffff", width=1,
                          tags=("port", f"port:{nid}:miss"))
        else:
            c.create_text(x + 20, y + 32, anchor="nw", fill=THEME["text_dim"],
                          font=self.f_sm, text=spec["summary"](nd["props"])[:12], tags=tags)
            if nd["props"].get("template"):
                got = self._get_tpl_photo(nd["props"]["template"])
                if got:
                    _, photo, dw, dh = got
                    ix = x1 - 10 - dw
                    iy = y + (CARD_H - dh) // 2
                    c.create_rectangle(ix - 2, iy - 2, ix + dw + 2, iy + dh + 2,
                                       fill="#1a1d26", outline=THEME["card_line"],
                                       tags=tags)
                    c.create_image(ix, iy, image=photo, anchor="nw", tags=tags)
        if selected:
            _round_rect(c, x - 3, y - 3, x1 + 3, y1 + 3, 14, outline=THEME["sel"],
                        width=1, fill="")
        if nd["type"] == "branch":
            for port, color in (("hit_next", THEME["ok"]), ("miss_next", THEME["err"])):
                hx, hy = self._port_pos(nd, port)
                c.create_oval(hx - 10, hy - 10, hx + 10, hy + 10,
                              fill="#1c2b1f" if port == "hit_next" else "#2e1c1e",
                              outline="")
                c.create_oval(hx - 6, hy - 6, hx + 6, hy + 6, fill=color,
                              outline="#ffffff", width=1,
                              tags=("port", f"port:{nid}:{port}"))
        if nd["type"] == "loop":
            hx, hy = self._port_pos(nd, "body_end")
            c.create_oval(hx - 10, hy - 10, hx + 10, hy + 10,
                          fill="#2b2416", outline="")
            c.create_oval(hx - 6, hy - 6, hx + 6, hy + 6, fill=THEME["warn"],
                          outline="#ffffff", width=1,
                          tags=("port", f"port:{nid}:body_end"))
            body = loop_body_of(self.flow, nid)
            info = (f"循环体 {len(body)} 个节点" if body else "循环体未设置")
            c.create_text(x + 20, y + 32, anchor="nw", fill=THEME["text_dim"],
                          font=self.f_sm, text=info, tags=tags)
        if nd["type"] == "subflow":
            child, err = subflow_child(self.flow, nid)
            info = (f"内联 {len(child.get('chain') or [])} 个节点"
                    if child is not None else "子流程未设置")
            c.create_text(x + 20, y + 32, anchor="nw", fill=THEME["text_dim"],
                          font=self.f_sm, text=info, tags=tags)

    def _port_pos(self, nd, port):
        if nd["type"] == "loop":
            return nd["x"] + CARD_W, nd["y"] + CARD_H * 0.5
        if nd["type"] == "switch":
            cands = parse_switch_cands(nd.get("props", {}).get("candidates"))
            n = max(len(cands), 1)
            if port == "miss":
                return nd["x"] + CARD_W, nd["y"] + 40 + 22 * n + 6
            i = int(port[4:])   # "cand0" → 0
            return nd["x"] + CARD_W, nd["y"] + 40 + 22 * i + 12
        y = nd["y"] + (CARD_H * 0.32 if port == "hit_next" else CARD_H * 0.68)
        return nd["x"] + CARD_W, y

    def _sw_h(self, nd):
        """switch 卡片高度（标题+候选行+全部未中区）"""
        n = len(parse_switch_cands(nd.get("props", {}).get("candidates")))
        return 64 + 22 * max(n, 1)

    def _draw_cut_off(self, cx, y_from, y_to, reason):
        """画「此处不向下继续」的显眼标记：红色虚线短桩 + 截止横杠 + ⛔ 徽标。
        与 build_pipeline 共用 linear_successor/_suppress_reason：生成被截断的出口，
        画布上也不得画成连上的样子。返回标记右边界（供 scrollregion 用）。"""
        c = self.canvas
        gap = max(14.0, y_to - y_from)
        stub = min(14.0, gap * 0.45)
        tip = y_from + 2 + stub
        c.create_line(cx, y_from + 2, cx, tip, fill=THEME["err"], width=2, dash=(5, 3))
        c.create_line(cx - 8, tip, cx + 8, tip, fill=THEME["err"], width=2)
        txt = {"switch-no-fallthrough": "⛔ 枝干无直落出口（走 ✓ 出口）",
               "switch-content-leaf": "⛔ 分支内容到此结束，不接下一节点",
               "common-terminal": "⛔ 收口节点，进入后不返回本流程",
               }.get(reason, "⛔ 此处不向下继续")
        tw = 12 * len(txt) + 14
        bx = cx + 16
        _round_rect(c, bx, tip - 11, bx + tw, tip + 11, 5,
                    fill="#2a1114", outline=THEME["err"])
        c.create_text(bx + tw / 2, tip, fill="#ffb3b3", font=self.f_sm, text=txt)
        return bx + tw

    def _draw_loop_marks(self, nid):
        """画循环的「循环体括线 + 次数」与「回边」。
        与生成结果对应：展开后前 times-1 份的尾部会回到循环体首节点，
        最后一份才接回链上后继 —— 所以这里画虚线回边，并标注次数。"""
        c = self.canvas
        nd = self.flow["nodes"][nid]
        p = nd.get("props") or {}
        times = p.get("times", 1)
        body = loop_body_of(self.flow, nid)
        end = p.get("body_end")
        if not body or not end or end not in self.flow["nodes"]:
            sx, sy = self._port_pos(nd, "body_end")
            c.create_line(sx, sy, sx + 26, sy, fill=THEME["warn"], width=2, dash=(5, 3))
            _round_rect(c, sx + 28, sy - 11, sx + 28 + 12 * len("未设置循环体") + 14,
                        sy + 11, 5, fill="#14161d", outline=THEME["card_line"])
            c.create_text(sx + 35, sy, anchor="w", fill=THEME["warn"], font=self.f_sm,
                          text="设置循环体末尾")
            return
        first = self.flow["nodes"][body[0]]
        last = self.flow["nodes"][body[-1]]
        # 循环体括线（画在循环体卡片的左侧）
        bx = min(self.flow["nodes"][b]["x"] for b in body) - 16
        top = first["y"] + 4
        bot = last["y"] + (self._sw_h(last) if last["type"] == "switch" else CARD_H) - 4
        c.create_line(bx, top, bx, bot, fill=THEME["warn"], width=2)
        c.create_line(bx, top, bx + 9, top, fill=THEME["warn"], width=2)
        c.create_line(bx, bot, bx + 9, bot, fill=THEME["warn"], width=2)
        c.create_text(bx - 4, (top + bot) / 2, anchor="e", fill=THEME["warn"],
                      font=self.f_sm, text=f"循环体 ×{times}")
        # 回边：循环体末尾右下 → 绕到循环节点右侧端口（虚线）
        sx, sy = self._port_pos(nd, "body_end")
        ex = last["x"] + CARD_W
        ey = last["y"] + (self._sw_h(last) if last["type"] == "switch" else CARD_H) / 2
        mx = max(sx, ex) + 70
        c.create_line(sx, sy, mx, sy, mx, ey, ex + 2, ey, smooth=True, dash=(6, 4),
                      width=2, fill=THEME["warn"], arrow=tk.LAST,
                      arrowshape=ARROW_SHAPE, splinesteps=24)
        _round_rect(c, mx - 34, (sy + ey) / 2 - 11, mx + 40, (sy + ey) / 2 + 11, 5,
                    fill="#14161d", outline=THEME["card_line"])
        c.create_text(mx + 3, (sy + ey) / 2, fill=THEME["warn"], font=self.f_sm,
                      text=f"×{times} 次")

    def _draw_branch_labels(self):
        c = self.canvas
        for nid in self.flow["chain"]:
            nd = self.flow["nodes"][nid]
            if nd["type"] == "branch":
                hx, hy = self._port_pos(nd, "hit_next")
                mx, my = self._port_pos(nd, "miss_next")
                c.create_text(hx - 11, hy, anchor="e", fill=THEME["ok"],
                              font=self.f_sm, text="✓命中")
                c.create_text(mx - 11, my, anchor="e", fill=THEME["err"],
                              font=self.f_sm, text="✗未中")
            elif nd["type"] == "switch":
                cands = parse_switch_cands(nd.get("props", {}).get("candidates"))
                for ci, cnd in enumerate(cands):
                    px, py = self._port_pos(nd, f"cand{ci}")
                    _round_rect(c, px - 12, py - 11, px + 38, py + 11, 5,
                                fill="#14161d", outline=THEME["card_line"])
                    c.create_text(px - 2, py, anchor="e", fill=THEME["ok"],
                                  font=self.f_sm, text=f"✓{ci + 1}")
                mx, my = self._port_pos(nd, "miss")
                _round_rect(c, mx - 12, my - 11, mx + 44, my + 11, 5,
                            fill="#14161d", outline=THEME["card_line"])
                c.create_text(mx - 2, my, anchor="e", fill=THEME["err"],
                              font=self.f_sm, text="✗全未中")

    def _draw_overlays(self):
        """背景帧上叠加显示选中节点的 ROI / 点击点 / 滑动线"""
        if not self.bg_disp:
            return
        nid = self.sel
        if not nid or nid not in self.flow["nodes"]:
            return
        c = self.canvas
        ox, oy, dw, dh = self.bg_disp
        W, H = self.frame_wh
        nd = self.flow["nodes"][nid]
        p = nd["props"]

        def px(x):
            return ox + x * dw / W

        def py(y):
            return oy + y * dh / H

        if nd["type"] in ("tpl_click", "wait_tpl", "branch") and p.get("roi"):
            roi = parse_roi(p["roi"])
            if roi:
                c.create_rectangle(px(roi[0]), py(roi[1]),
                                   px(roi[0] + roi[2]), py(roi[1] + roi[3]),
                                   outline=THEME["warn"], width=2, dash=(5, 3))
                c.create_text(px(roi[0]), py(roi[1]) - 8, anchor="sw",
                              fill=THEME["warn"], font=self.f_sm, text="ROI")
        if nd["type"] == "tap":
            c.create_line(px(p["x"]) - 12, py(p["y"]), px(p["x"]) + 12, py(p["y"]),
                          fill="#ff6a6a")
            c.create_line(px(p["x"]), py(p["y"]) - 12, px(p["x"]), py(p["y"]) + 12,
                          fill="#ff6a6a")
            c.create_oval(px(p["x"]) - 7, py(p["y"]) - 7, px(p["x"]) + 7, py(p["y"]) + 7,
                          outline="#ff6a6a", width=2)
        if nd["type"] == "swipe":
            c.create_line(px(p["x1"]), py(p["y1"]), px(p["x2"]), py(p["y2"]),
                          fill="#cf9ae8", width=2, arrow=tk.LAST,
                          arrowshape=ARROW_SHAPE)
            for (kx, ky) in ((p["x1"], p["y1"]), (p["x2"], p["y2"])):
                c.create_oval(px(kx) - 5, py(ky) - 5, px(kx) + 5, py(ky) + 5,
                              outline="#cf9ae8", width=2)

    def _close_tpl_pop(self):
        if self._tpl_pop is not None:
            try:
                self._tpl_pop.destroy()
            except tk.TclError:
                pass
            self._tpl_pop = None

    def _show_tpl_pop(self, cmb, items, on_pick, multi=False, cur_set=()):
        """输入框下方弹出过滤列表（焦点留在输入框，可继续输入）。
        multi=False：点选/回车单选；multi=True：多选模板候选（任一出线即命中）：
        点击行切换勾选（弹窗保持不关），回车/点空白提交，Esc 取消。"""
        self._close_tpl_pop()
        if not items:
            return
        top = tk.Toplevel(self.root)
        top.wm_overrideredirect(True)
        lb = tk.Listbox(top, bg=THEME["field"], fg=THEME["text"],
                        selectbackground=THEME["accent"], selectforeground=THEME["text"],
                        relief="flat", highlightthickness=1,
                        highlightbackground=THEME["card_line"], font=FONT_SM,
                        activestyle="none", exportselection=False,
                        selectmode=("multiple" if multi else "browse"))
        for it in items[:14]:
            lb.insert("end", it)
        if multi:
            for i, it in enumerate(items[:14]):
                if it in cur_set:
                    lb.selection_set(i)
        lb.pack(fill="both", expand=True)
        w = max(cmb.winfo_width(), 240)
        h = min(len(items), 14) * 21 + 6
        top.wm_geometry("%dx%d+%d+%d" % (w, h,
                     cmb.winfo_rootx(), cmb.winfo_rooty() + cmb.winfo_height()))

        def submit(_e=None):
            sel = lb.curselection()
            picked = [lb.get(i) for i in sel]
            self._tpl_pop = None
            top.destroy()
            if picked:
                on_pick(",".join(picked))
            elif multi:
                on_pick("")          # 一个不选=清空
            else:
                on_pick(None)

        if multi:
            lb.bind("<Return>", submit)
            top.bind("<FocusOut>", submit)          # 点弹窗外部（焦点移走）提交
            lb.bind("<Escape>", lambda e: self._close_tpl_pop())
        else:
            lb.bind("<ButtonRelease-1>", submit)
            lb.bind("<Double-Button-1>", submit)
        top.bind("<Destroy>", lambda _e: setattr(self, "_tpl_pop", None)
                 if self._tpl_pop is top else None)
        top.lift()
        top.attributes("-topmost", True)
        self._tpl_pop = top

    def _commit_tpl(self, var, picked, multi=False):
        """模板最终值写回节点；非法输入恢复原值。
        picked：None=按输入框现值提交（失焦）；""=清空；否则为选中模板（可含逗号多值）"""
        nid = self.sel
        if not nid or nid not in self.flow["nodes"]:
            return
        props = self.flow["nodes"][nid]["props"]
        cur = props.get("template", "")
        if picked is None:
            val = str(var.get()).strip()
            if not val:
                if cur:
                    props["template"] = ""
                    self.redraw()
                return
            if val.find(",") >= 0 or multi:
                # 多值：全部合法才写入，否则恢复
                parts = split_tpls(val)
                ok = bool(parts) and all(p in self.templates for p in parts)
                if ok:
                    if cur != val:
                        props["template"] = val
                        self.redraw()
                else:
                    var.set(cur)
            else:
                if val in self.templates:
                    if cur != val:
                        props["template"] = val
                        self.redraw()
                else:
                    var.set(cur)
            return
        if picked == "" and multi:
            if cur:
                props["template"] = ""
                self.redraw()
            return
        if picked:
            parts = split_tpls(picked)
            ok = all(p in self.templates for p in parts)
            if ok and cur != picked:
                props["template"] = picked
                self.redraw()
            elif not ok:
                var.set(cur)

    def _get_tpl_photo(self, name):
        """模板缩略图（节点卡片显示用）；按 mtime 缓存"""
        path = os.path.join(IMG_DIR, name)
        if not os.path.isfile(path):
            return None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cached = self._tpl_img_cache.get(name)
        if cached and cached[0] == mtime:
            return cached
        try:
            im = Image.open(path).convert("RGB")
            w, h = im.size
            scale = min(42 / h, 88 / w)
            dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
            photo = ImageTk.PhotoImage(im.resize((dw, dh), Image.NEAREST))
        except Exception:
            return None
        self._tpl_img_cache[name] = (mtime, photo, dw, dh)
        return self._tpl_img_cache[name]

    # ---------- 画布视图：坐标变换 / 缩放 / 平移 ----------

    def _c2w(self, cx, cy):
        """画布坐标 → 模型坐标"""
        z = self.zoom or 1.0
        return cx / z, cy / z

    def _w2c(self, wx, wy):
        """模型坐标 → 画布坐标"""
        z = self.zoom or 1.0
        return wx * z, wy * z

    def _apply_zoom_fonts(self):
        """字号跟着缩放走：不缩字号的话，缩小后卡片变小而字不变，会糊成一团。"""
        def scaled(base):
            try:
                size = int(round(base[1] * self.zoom))
            except (IndexError, TypeError):
                return base
            return (base[0], max(6, size)) + tuple(base[2:])
        self.f_sm = scaled(FONT_SM)
        self.f_title = scaled(FONT_B)
        self.f_ui = scaled(FONT)

    def set_zoom(self, z, anchor=None):
        """设置缩放（下限 0.25 便于总览长流程，上限 2.5）。anchor 是画布坐标，
        缩放后尽量让该点停在原处。"""
        z = max(ZOOM_MIN, min(ZOOM_MAX, float(z)))
        if abs(z - self.zoom) < 1e-6:
            return
        old = self.zoom
        if anchor:
            wx, wy = self._c2w(*anchor)
        self.zoom = z
        self._bg_cache = None
        self.redraw()
        sr = [float(v) for v in (self.canvas.cget("scrollregion").split() or [0, 0, 1, 1])]
        if anchor and len(sr) == 4 and sr[2] > 0 and sr[3] > 0:
            # 让 anchor 处的模型点缩放后仍落在同一个屏幕位置
            cx, cy = self._w2c(wx, wy)
            self.canvas.xview_moveto(max(0.0, (cx - anchor[0]) / sr[2]))
            self.canvas.yview_moveto(max(0.0, (cy - anchor[1]) / sr[3]))
        self.status(f"缩放 {int(self.zoom * 100)}%")

    def zoom_in(self, anchor=None):
        self.set_zoom(self.zoom * 1.2, anchor)

    def zoom_out(self, anchor=None):
        self.set_zoom(self.zoom / 1.2, anchor)

    def zoom_reset(self):
        self.set_zoom(1.0)

    def zoom_fit(self):
        """缩到内容能一屏装下（受 ZOOM_MIN 限制）；装不下就如实说明，不假装适配了"""
        pts = [(nd["x"], nd["y"]) for nd in self.flow["nodes"].values()]
        if not pts:
            self.set_zoom(1.0)
            return
        w = max(p[0] for p in pts) + CARD_W + 80
        h = max(p[1] for p in pts) + CARD_H + 80
        cw = max(200, self.canvas.winfo_width() - 20)
        chh = max(200, self.canvas.winfo_height() - 20)
        want = min(cw / w, chh / h)
        self.set_zoom(want)
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        if want < ZOOM_MIN:
            self.log(f"⚠ 内容高 {int(h)}px，缩到下限 {int(ZOOM_MIN * 100)}% 仍超出窗口"
                     f"（可中键拖动平移，或用「✥ 整理布局」把节点排紧）", "warn")
        else:
            self.status(f"已适配窗口（{int(self.zoom * 100)}%）")

    def _bg_photo_for(self, dw, dh):
        """背景帧的 PhotoImage：尺寸随 zoom 变，所以要按 (帧, 显示高度, zoom) 缓存。
        以前每次 redraw 都重做 LANCZOS 缩放 + 构造 PhotoImage，是重绘耗时的大头。"""
        key = (self._frame_file, getattr(self, "_frame_mtime", None),
               int(dw), int(dh))
        if self._bg_cache and self._bg_cache[0] == key:
            return self._bg_cache[1]
        photo = ImageTk.PhotoImage(self.bg_pil.resize((max(1, int(dw)),
                                                       max(1, int(dh))),
                                                      Image.LANCZOS))
        self._bg_cache = (key, photo)
        return photo

    # ---------- 画布交互 ----------

    def _hit_test(self, wx, wy):
        """返回 ("port", nid, port) / ("node", nid) / None。
        传入模型坐标（画布坐标已除过 zoom），命中测试再换算回画布坐标。"""
        cx, cy = self._w2c(wx, wy)
        for it in reversed(self.canvas.find_overlapping(cx - 2, cy - 2, cx + 2, cy + 2)):
            for tag in self.canvas.gettags(it):
                if tag.startswith("port:"):
                    _, nid, port = tag.split(":")
                    return ("port", nid, port)
                if tag.startswith("node:"):
                    return ("node", tag.split(":")[1])
        return None

    def _canvas_to_frame(self, wx, wy):
        """模型坐标 → 帧原图坐标；不在帧内返回 None"""
        if not self.bg_disp:
            return None
        ox, oy, dw, dh = self.bg_disp
        if not (ox <= wx <= ox + dw and oy <= wy <= oy + dh):
            return None
        W, H = self.frame_wh
        return (int((wx - ox) * W / dw), int((wy - oy) * H / dh))

    def on_down(self, e):
        cx, cy = self._c2w(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        try:
            self.canvas.focus_set()          # 让方向键微调落到画布而不是别处
        except tk.TclError:
            pass
        if self.roi_pick is not None:
            if self._canvas_to_frame(cx, cy) is None:
                self.status("框选要落在帧画面上（Esc 取消）")
                return
            self.roi_pick["x0"], self.roi_pick["y0"] = cx, cy
            self.roi_pick["x1"], self.roi_pick["y1"] = cx, cy
            self.redraw()
            return
        if self.pick_target:
            pt = self._canvas_to_frame(cx, cy)
            self.pick_target = None          # 本次点击后一律退出取点模式
            if pt is not None:
                self._apply_pick(pt)
                return
            self.status("已取消取点（点击帧画面外即取消）")
            # 落到下面的正常选中/拖动逻辑，避免取点模式卡死节点编辑
        hit = self._hit_test(cx, cy)
        if hit is None:
            if self.sel:
                self.sel = None
                self.build_prop_panel()
                self.redraw()
            return
        if hit[0] == "port":
            self.wire = {"from": hit[1], "port": hit[2], "mx": cx, "my": cy}
            self.status(f"拖到目标节点设置 {hit[2]} 出口；拖到空白处=断开")
            return
        nid = hit[1]
        if self.sel != nid:
            self.sel = nid
            self.build_prop_panel()
        nd = self.flow["nodes"][nid]
        # 拖动前存档；若只是点选没真的移动，内容不变，_snapshot 会自动跳过
        self._snapshot(f"drag:{nid}")
        self.drag = {"id": nid, "dx": cx - nd["x"], "dy": cy - nd["y"]}
        self.redraw()

    def on_motion(self, e):
        cx, cy = self._c2w(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        if self.roi_pick is not None and self.roi_pick.get("x0") is not None:
            self.roi_pick["x1"], self.roi_pick["y1"] = cx, cy
            self.redraw()
            return
        if self.wire:
            self.wire["mx"], self.wire["my"] = cx, cy
            self.redraw()
            return
        if not self.drag:
            return
        nd = self.flow["nodes"][self.drag["id"]]
        nd["x"] = cx - self.drag["dx"]
        nd["y"] = cy - self.drag["dy"]
        self._reorder_on_drag(self.drag["id"])
        self.redraw()

    def _reorder_on_drag(self, drag_id):
        """拖动中按纵向位置实时重排主链（排序即拖动）"""
        ch = self.flow["chain"]
        others = sorted(self.flow["nodes"][n]["y"] + CARD_H / 2 for n in ch if n != drag_id)
        dy = self.flow["nodes"][drag_id]["y"] + CARD_H / 2
        idx = bisect.bisect_left(others, dy)
        cur = ch.index(drag_id)
        if cur != idx:
            ch.remove(drag_id)
            ch.insert(min(idx, len(ch)), drag_id)

    def _set_wire(self, src, port, tgt):
        """连线写回：branch 直接写节点字段；switch 写候选 next / 全部未中 miss_next；
        loop 写循环体末尾 body_end"""
        if src.get("type") == "switch":
            props = src["props"]
            if port == "miss":
                props["miss_next"] = tgt
            else:
                cands = parse_switch_cands(props.get("candidates"))
                i = int(port[4:])
                if i < len(cands):
                    cands[i]["next"] = tgt
                    props["candidates"] = cands
        elif src.get("type") == "loop":
            src.setdefault("props", {})["body_end"] = tgt
        else:
            src[port] = tgt

    def on_up(self, _e):
        if self.roi_pick is not None:
            self._finish_roi_pick()
            self.redraw()
            return
        if self.wire:
            hit = self._hit_test(self.wire["mx"], self.wire["my"])
            src = self.flow["nodes"][self.wire["from"]]
            port = self.wire["port"]
            if hit and hit[0] == "node" and hit[1] != self.wire["from"]:
                self._snapshot()
                self._set_wire(src, port, hit[1])
                self.log(f"已连接 {port} → {self.flow['nodes'][hit[1]].get('title', hit[1])}")
            else:
                if src.get("type") == "switch":
                    was = (src["props"].get("miss_next") if port == "miss"
                           else parse_switch_cands(src["props"].get("candidates"))
                                .__getitem__(int(port[4:])).get("next")
                           if port.startswith("cand") else None)
                else:
                    was = src.get(port)
                if was:
                    self._snapshot()
                    self.log(f"已断开 {port}")
                self._set_wire(src, port, None)
            self.wire = None
            self.build_prop_panel()
            self.redraw()
            self.status("就绪")
            return
        if self.drag:
            self.drag = None
            self.build_prop_panel()   # 刷新面板里的 #序号
            self.redraw()

    # ---------- 属性面板 ----------

    def build_prop_panel(self):
        # 先解除旧控件变量上的回调（控件销毁时会触发 var 变化，
        # 回调若访问已销毁控件会抛 invalid command）
        for var, tid in self._var_traces:
            try:
                var.trace_remove("write", tid)
            except Exception:
                pass
        self._var_traces.clear()
        for w in self.props_inner.winfo_children():
            try:
                for seq in ("<KeyRelease>", "<FocusOut>", "<Configure>"):
                    w.unbind(seq)
            except Exception:
                pass
            w.destroy()
        self._var_traces.clear()
        nid = self.sel
        if not nid or nid not in self.flow["nodes"]:
            ttk.Label(self.props_inner, text="点击画布上的节点，在这里编辑参数",
                      style="Dim.TLabel").grid(row=0, column=0, columnspan=2,
                                               sticky="w", pady=(2, 0))
            self._sync_branch_ui()
            return
        nd = self.flow["nodes"][nid]
        spec = NODE_TYPES[nd["type"]]
        idx = self.flow["chain"].index(nid) + 1
        head = ttk.Frame(self.props_inner)
        head.grid(row=0, column=0, columnspan=2, sticky="w", pady=(2, 6))
        tk.Label(head, text=f"{spec['icon']} #{idx} {spec['label']}",
                 bg=spec["color"], fg="white", font=FONT_B, padx=8, pady=2).pack(side="left")
        row = 1
        all_fields = list(spec["fields"]) + list(COMMON_NODE_FIELDS)
        hidden = FIELD_HIDDEN_IF.get(nd["type"], lambda _p: set())(nd.get("props") or {})
        for _gkey, glabel, gfields in _group_fields(all_fields):
            visible = [f for f in gfields if f[0] not in hidden]
            if not visible:
                continue
            ttk.Label(self.props_inner, text=glabel, style="Group.TLabel").grid(
                row=row, column=0, columnspan=2, sticky="we", pady=(9, 2))
            row += 1
            for f in visible:
                key, label, kind, extra = _field_spec(f)
                if kind == "switch_list":
                    # 候选编辑器较宽：标签放到上方，编辑器占整行
                    lab = ttk.Label(self.props_inner, text=label, style="Dim.TLabel")
                    lab.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
                    self._bind_tip(lab, FIELD_TIPS.get(key))
                    row += 1
                    var = self._make_var(nd["props"], key, kind)
                    self._make_widget(var, kind, key, extra).grid(
                        row=row, column=0, columnspan=2, sticky="we", pady=2)
                    self.prop_widgets[key] = var
                    row += 1
                    continue
                lab = ttk.Label(self.props_inner, text=label, style="Dim.TLabel")
                lab.grid(row=row, column=0, sticky="w", pady=2)
                self._bind_tip(lab, FIELD_TIPS.get(key))
                var = self._make_var(nd["props"], key, kind)
                self._make_widget(var, kind, key, extra).grid(
                    row=row, column=1, sticky="we", padx=(8, 0), pady=2)
                self.prop_widgets[key] = var
                row += 1
        hint = _hide_hint(nd["type"], nd.get("props") or {})
        if hint:
            ttk.Label(self.props_inner, text="⚠ " + hint, style="Dim.TLabel",
                      wraplength=300, justify="left").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.props_inner.columnconfigure(1, weight=1)
        self._sync_branch_ui()

    def _bind_tip(self, widget, text):
        """轻量 tooltip（tkinter 无原生支持）：悬停显示，离开或控件销毁即关闭"""
        if not text:
            return
        state = {"win": None}

        def hide(_e=None):
            if state["win"] is not None:
                try:
                    state["win"].destroy()
                except tk.TclError:
                    pass
                state["win"] = None

        def show(_e=None):
            if state["win"] is not None:
                return
            try:
                w = tk.Toplevel(self.root)
            except tk.TclError:
                return
            w.wm_overrideredirect(True)
            w.attributes("-topmost", True)
            tk.Label(w, text=text, justify="left", bg="#2b3245", fg=THEME["text"],
                     font=FONT_SM, padx=8, pady=5, relief="solid", bd=1,
                     wraplength=340).pack()
            w.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 14,
                                      widget.winfo_rooty()
                                      + widget.winfo_height() + 4))
            state["win"] = w

        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<Destroy>", hide, add="+")

    def _make_var(self, props, key, kind):
        v = props.get(key)
        if kind == "bool":
            var = tk.BooleanVar(value=bool(v))
        elif kind == "bool_opt":
            # 引擎默认 enabled=true，故未设置时勾选框应为「已勾选」
            var = tk.BooleanVar(value=True if v is None else bool(v))
        elif kind == "switch_list":
            var = tk.StringVar(value=str(len(v)) if isinstance(v, list) else "0")
        else:
            var = tk.StringVar(value="" if v is None else str(v))
        tid = var.trace_add("write", lambda *_: self._prop_changed(key, var, kind))
        self._var_traces.append((var, tid))
        return var

    def _make_widget(self, var, kind, key, extra=None):
        if kind == "switch_list":
            return self._make_switch_list(var)
        if kind in ("tpl", "tpl_multi"):
            self.templates = list_templates()   # 实时刷新（框选工具新保存的模板立即可选）
            multi = (kind == "tpl_multi")
            width = 30 if multi else 26
            cmb = ttk.Combobox(self.props_inner, textvariable=var,
                               values=self.templates, width=width, font=FONT_SM)

            def _submit(var=var, picked=None):
                # picked: 弹窗提交的多选串（None=用当前输入框值）
                val = picked if picked is not None else str(var.get()).strip()
                self._commit_tpl(var, val, multi)

            def _on_key(e, cmb=cmb, var=var):
                if e.keysym == "Return":
                    self._close_tpl_pop()
                    if multi:
                        # 多选：回车确认当前输入串（全部合法才写，非法恢复）
                        self._commit_tpl(var, var.get(), True)
                    else:
                        items = _tpl_candidates(self.templates, var.get())
                        if items:
                            var.set(items[0])
                            self._commit_tpl(var, items[0], False)
                    return
                if e.keysym == "Escape":
                    self._close_tpl_pop()
                    return
                if e.keysym in ("Down", "Up", "Left", "Right", "Tab"):
                    return
                items = _tpl_candidates(self.templates, var.get())
                if multi:
                    cur_set = set(split_tpls(var.get()))
                    self._show_tpl_pop(cmb, items, _submit, multi=True,
                                       cur_set=cur_set)
                else:
                    self._show_tpl_pop(
                        cmb, items,
                        lambda picked, var=var: (var.set(picked),
                                                 self._commit_tpl(var, picked, False)))

            def _on_focus_out(var=var):
                self.root.after(170, self._close_tpl_pop)   # 等列表点击事件先到
                self._commit_tpl(var, None, multi)

            cmb.bind("<KeyRelease>", _on_key)
            cmb.bind("<<ComboboxSelected>>",
                     lambda e, var=var: self._commit_tpl(var, var.get(), multi))
            cmb.bind("<FocusOut>", _on_focus_out)
            return cmb
        if kind == "common":
            return ttk.Combobox(self.props_inner, textvariable=var,
                                values=common_node_names(), width=22,
                                state="readonly", font=FONT_SM)
        if kind == "bool":
            return ttk.Checkbutton(self.props_inner, variable=var, text="")
        if kind == "bool_opt":
            return ttk.Checkbutton(self.props_inner, variable=var, text="")
        if kind == "float":
            return ttk.Spinbox(self.props_inner, textvariable=var,
                               from_=0.3, to=0.99, increment=0.05, width=10,
                               font=FONT_SM)
        if kind == "int_opt":
            return ttk.Entry(self.props_inner, textvariable=var, width=10,
                             font=FONT_SM)
        if kind == "float_opt":
            return ttk.Entry(self.props_inner, textvariable=var, width=10,
                             font=FONT_SM)
        if kind == "choice":
            return ttk.Combobox(self.props_inner, textvariable=var,
                                values=tuple(extra or ()), width=16,
                                state="readonly", font=FONT_SM)
        if kind == "flowref":
            return ttk.Combobox(
                self.props_inner, textvariable=var,
                values=flow_name_list(exclude=(self.flow or {}).get("name")),
                width=16, state="readonly", font=FONT_SM)
        if kind in ("pick", "pick2"):
            fr = ttk.Frame(self.props_inner)
            ttk.Entry(fr, textvariable=var, width=8, font=FONT_SM).pack(side="left", ipady=2)
            self._flat_btn(fr, "✛ 取点", lambda: self._start_pick(key),
                           padx=6, font=FONT_SM).pack(side="left", padx=4)
            return fr
        if kind == "roi":
            fr = ttk.Frame(self.props_inner)
            ttk.Entry(fr, textvariable=var, width=13, font=FONT_SM).pack(side="left", ipady=2)
            self._flat_btn(fr, "✛ 框选", lambda: self._start_roi_pick(key),
                           padx=6, font=FONT_SM).pack(side="left", padx=4)
            return fr
        return ttk.Entry(self.props_inner, textvariable=var, width=22, font=FONT_SM)

    def _make_switch_list(self, var):
        """枝干候选编辑：列表 + 文本/时长输入 + 增删改；候选的命中出口用画布连线"""
        wrap = ttk.Frame(self.props_inner)
        lb = tk.Listbox(wrap, height=5, width=34, font=FONT_SM, bg=THEME["field"],
                        fg=THEME["text"], selectbackground=THEME["accent"],
                        selectforeground=THEME["text"], relief="flat",
                        highlightthickness=1, highlightbackground=THEME["card_line"])
        lb.pack(fill="x", padx=(8, 0))

        def _cands():
            return self.flow["nodes"][self.sel]["props"].setdefault("candidates", [])

        def refresh():
            cands = _cands()
            lb.delete(0, "end")
            for i, c in enumerate(cands):
                t = str(c.get("t", ""))
                disp = "OCR:" + t[4:] if t.lower().startswith("ocr:") else t
                merge = " · 回并主线" if c.get("mergeBack") else ""
                lb.insert("end", f"{i + 1}. {disp} · {c.get('timeout', 3000)}ms{merge}")
            var.set(str(len(cands)))   # 触发面板重绘钩子

        row = ttk.Frame(wrap)
        row.pack(fill="x", padx=(8, 0), pady=(6, 0))
        e_t = ttk.Entry(row, font=FONT_SM, width=17)
        e_t.pack(side="left")
        e_d = ttk.Entry(row, width=6, font=FONT_SM)
        e_d.insert(0, "3000")
        e_d.pack(side="left", padx=4)
        e_m = tk.BooleanVar(value=False)
        ttk.Checkbutton(wrap, text="命中后回并主线（不勾=内容跑完即结束）",
                        variable=e_m).pack(anchor="w", padx=(8, 0), pady=(2, 0))

        def _fill_sel(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            c = _cands()[sel[0]]
            e_t.delete(0, "end")
            e_t.insert(0, c.get("t", ""))
            e_d.delete(0, "end")
            e_d.insert(0, str(c.get("timeout", 3000)))
            e_m.set(bool(c.get("mergeBack")))

        def _read_form():
            t = e_t.get().strip()
            if not t:
                return None
            try:
                timeout = max(500, int(e_d.get() or 3000))
            except ValueError:
                timeout = 3000
            item = {"t": t, "timeout": timeout}
            if e_m.get():
                item["mergeBack"] = True
            return item

        def on_add():
            item = _read_form()
            if item is None:
                return
            self._snapshot("cand")
            _cands().append(item)
            e_t.delete(0, "end")
            e_d.delete(0, "end")
            e_d.insert(0, "3000")
            e_m.set(False)
            refresh()

        def on_update():
            sel = lb.curselection()
            if not sel:
                return
            item = _read_form()
            if item is None:
                return
            old = _cands()[sel[0]]
            self._snapshot("cand")
            # 保留画布上拖出来的命中出口（next），否则「更新」会把连线清掉
            if old.get("next"):
                item["next"] = old["next"]
            _cands()[sel[0]] = item
            refresh()

        def on_del():
            sel = lb.curselection()
            if not sel:
                return
            self._snapshot("cand")
            del _cands()[sel[0]]
            refresh()

        lb.bind("<<ListboxSelect>>", _fill_sel)
        lb.bind("<Double-Button-1>", _fill_sel)
        btns = ttk.Frame(wrap)
        btns.pack(fill="x", padx=(8, 0), pady=(4, 0))
        for text, cmd in (("+ 添加", on_add), ("更新", on_update), ("删除", on_del)):
            ttk.Button(btns, text=text, command=cmd, width=7,
                       style="Toolbutton").pack(side="left", padx=(0, 6))
        ttk.Label(
            wrap,
            text="候选出口：拖卡片右侧 ✓1/✓2… 圆点到内容起点；顺序就是判定顺序。",
            style="Dim.TLabel", wraplength=230).pack(anchor="w", padx=8, pady=(6, 0))
        refresh()
        return wrap

    def _prop_changed(self, key, var, kind):
        nid = self.sel
        if not nid or nid not in self.flow["nodes"]:
            return
        nd = self.flow["nodes"][nid]
        self._snapshot(f"prop:{nid}:{key}")   # 同一处连续敲键会合并成一条
        try:
            if kind == "bool":
                nd["props"][key] = bool(var.get())
            elif kind == "float":
                nd["props"][key] = float(var.get())
            elif kind in ("tpl", "tpl_multi"):
                if tpl_value_ok(var.get()):
                    nd["props"][key] = var.get()
            elif kind in ("int", "pick", "pick2"):
                nd["props"][key] = int(float(var.get() or 0))
            elif kind == "int_opt":
                # 可选整数：留空 = 从 props 里移除该键 = 生成物不写该字段（继承全局默认）
                v = str(var.get()).strip()
                if v:
                    nd["props"][key] = int(float(v))
                else:
                    nd["props"].pop(key, None)
            elif kind == "float_opt":
                v = str(var.get()).strip()
                if v:
                    nd["props"][key] = float(v)
                else:
                    nd["props"].pop(key, None)
            elif kind in ("choice", "flowref"):
                v = str(var.get()).strip()
                if v:
                    nd["props"][key] = v
                else:
                    nd["props"].pop(key, None)
            elif kind == "bool_opt":
                # 勾选 = 引擎默认值 → 不写字段；取消勾选才写 false
                if bool(var.get()):
                    nd["props"].pop(key, None)
                else:
                    nd["props"][key] = False
            elif kind == "str_opt":
                v = str(var.get()).strip()
                if v:
                    nd["props"][key] = v
                else:
                    nd["props"].pop(key, None)
            elif kind == "switch_list":
                pass    # 列表型 props 由候选编辑控件直接维护，StringVar 仅用于触发重绘
            else:
                nd["props"][key] = var.get()
        except (ValueError, tk.TclError):
            return
        self.redraw()

    # ---------- 分支出口 UI ----------

    def _branch_target_label(self, nid, target, is_hit):
        ch = self.flow["chain"]
        if target is None:
            return "(自动→下一个)" if is_hit else "(流程结束)"
        i = ch.index(target) + 1 if target in ch else "?"
        return f"#{i} {self.flow['nodes'][target].get('title', target)}"

    def _sync_branch_ui(self):
        nid = self.sel
        def _disable_combos():
            self.hit_combo.set("")
            self.miss_combo.set("")
            self.hit_combo.config(values=[], state="disabled")
            self.miss_combo.config(values=[], state="disabled")
        if not nid or nid not in self.flow["nodes"]:
            _disable_combos()
            self.branch_hint.config(text="选中分支节点后可在此改接出口；"
                                         "✗ 跳回前面的节点 = 循环。")
            return
        nd = self.flow["nodes"][nid]
        ch = self.flow["chain"]
        options = [f"#{i+1} {self.flow['nodes'][n].get('title', n)}" for i, n in enumerate(ch)]
        if nd["type"] == "loop":
            # 循环复用「✗」那一行的下拉来选择循环体末尾（也可在画布上拖端口）
            self.hit_combo.config(values=[], state="disabled")
            self.hit_combo.set("")
            self.miss_combo.config(values=["(未设置)"] + options, state="readonly")
            be = nd.get("props", {}).get("body_end")
            self.miss_combo.set("(未设置)" if not be
                                else self._branch_target_label(nid, be, False))
            n_body = len(loop_body_of(self.flow, nid))
            self.branch_hint.config(
                text=f"循环体 {n_body} 个节点（从本节点之后到上面选中的那个节点）。"
                     f"生成时循环体【复制 times 份】逐个首尾相接，不依赖引擎特性；"
                     f"也可直接拖卡片右侧圆点连线。当前次数 "
                     f"×{nd.get('props', {}).get('times', 1)}。")
            return
        if nd["type"] == "switch":
            self.hit_combo.config(values=[], state="disabled")
            self.hit_combo.set("")
            self.miss_combo.config(values=["(流程结束)"] + options, state="readonly")
            mn = nd.get("props", {}).get("miss_next")
            self.miss_combo.set("(流程结束)" if not mn else
                                self._branch_target_label(nid, mn, False))
            n_cand = len(parse_switch_cands(nd.get("props", {}).get("candidates")))
            self.branch_hint.config(
                text=f"候选 {n_cand} 个：从左到右依次判定，命中即走各自内容(拖卡片右侧✓圆点连到内容起点)；"
                     f"全部未中走下方「✗全未中」→ 在右下拉改接。")
            return
        if nd["type"] != "branch":
            _disable_combos()
            self.branch_hint.config(text="该节点类型没有分支出口（只有「分支(模板在?)」/「枝干判定(多路)」节点有出口）。")
            return
        self.hit_combo.config(values=["(自动→下一个)"] + options, state="readonly")
        self.miss_combo.config(values=["(流程结束)"] + options, state="readonly")
        self.hit_combo.set(self._branch_target_label(nid, nd.get("hit_next"), True))
        self.miss_combo.set(self._branch_target_label(nid, nd.get("miss_next"), False))
        self.branch_hint.config(text="拖动分支卡片右侧 ✓/✗ 圆点到目标节点即可连线；"
                                     "拖到空白处断开。✗ 跳回前面的节点 = 循环。")

    def on_branch_combo(self, port):
        nid = self.sel
        if not nid or nid not in self.flow["nodes"]:
            return
        nd = self.flow["nodes"][nid]
        if nd["type"] not in ("branch", "switch", "loop"):
            return
        combo = self.hit_combo if port == "hit_next" else self.miss_combo
        text = combo.get()
        m = re.match(r"#(\d+)", text)
        if m and 1 <= int(m.group(1)) <= len(self.flow["chain"]):
            target = self.flow["chain"][int(m.group(1)) - 1]
            if target == nid:
                self.log("分支出口不能指向自己", "warn")
                self._sync_branch_ui()
                return
            self._snapshot(f"branch:{nid}:{port}")
            if nd["type"] == "loop":
                nd.setdefault("props", {})["body_end"] = target
            elif nd["type"] == "switch" and port == "miss_next":
                nd.setdefault("props", {})["miss_next"] = target
            else:
                nd[port] = target
        else:
            if nd["type"] == "loop":
                nd.setdefault("props", {})["body_end"] = None
            elif nd["type"] == "switch" and port == "miss_next":
                nd.setdefault("props", {})["miss_next"] = None
            else:
                nd[port] = None
        self.redraw()
        self._sync_branch_ui()

    # ---------- 取坐标 ----------

    def _start_pick(self, key):
        if not self.sel:
            return
        if self.bg_disp is None:
            self.status("取点需要先有背景帧：先抓帧（F5）或打开帧图")
            self.log("⚠ 取点失败：画布上没有背景帧。先点「⟳ 抓帧 (F5)」再取点。", "warn")
            return
        self.pick_target = key
        mode = {"x": "点击取点击坐标", "x1": "点击取滑动起点", "x2": "点击取滑动终点"}[key]
        self.status(f"取点模式：{mode}（在左侧帧画面上点击，Esc 取消）")
        self.root.bind("<Escape>", self.on_escape)

    def _cancel_pick(self, _e):
        self.pick_target = None
        self.status("已取消取点")

    def _apply_pick(self, pt):
        key = self.pick_target
        x, y = pt
        props = self.flow["nodes"][self.sel]["props"]
        pairs = {"x": ("x", "y"), "x1": ("x1", "y1"), "x2": ("x2", "y2")}
        self._snapshot(f"pick:{self.sel}")
        ka, kb = pairs[key]
        props[ka], props[kb] = x, y
        self.pick_target = None
        self.status(f"已取坐标 ({x},{y})")
        self.build_prop_panel()
        self.redraw()

    # ---------- 运行回放（P1-8） ----------
    # 数据来源全部是手机上已经存在的引擎日志（只读），不改安卓端、不加新协议。
    # 触发运行用 am start --es entry：App 每次 runTask 都会重新加载任务包，
    # 所以新增/更新的 pipeline 会被带上，不需要 force-stop（也就不会清掉虚拟屏）。

    def on_replay_open(self):
        if getattr(self, "replay_win", None) and self.replay_win.winfo_exists():
            self.replay_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.replay_win = win
        win.title("运行回放 · 节点高亮 / 识别框")
        win.configure(bg=THEME["panel"])
        win.geometry("720x520+%d+%d" % (self.root.winfo_rootx() + 120,
                                        self.root.winfo_rooty() + 160))
        win.transient(self.root)

        top = ttk.Frame(win)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.replay_entry_var = tk.StringVar(value=entry_name(self.flow))
        ttk.Label(top, text="入口", style="Dim.TLabel").pack(side="left")
        ttk.Entry(top, textvariable=self.replay_entry_var, width=26,
                  font=FONT_SM).pack(side="left", padx=6)
        self._flat_btn(top, "▶ 运行并回放", self.replay_start, bg="#2c6e48",
                       fg="#eafff2", hover="#3a8a5c", font=FONT_B).pack(side="left", padx=4)
        self._flat_btn(top, "⟳ 只读日志（不运行）", self.replay_readonly,
                       font=FONT_SM).pack(side="left", padx=4)
        self._flat_btn(top, "⌫ 清空", self.replay_clear, font=FONT_SM).pack(side="left", padx=4)

        self.replay_status = tk.StringVar(value="未开始")
        ttk.Label(win, textvariable=self.replay_status, style="Dim.TLabel",
                  wraplength=690, justify="left").pack(anchor="w", padx=12)

        cols = ("ts", "node", "kind", "detail")
        self.replay_tree = ttk.Treeview(win, columns=cols, show="headings", height=18)
        for c, w, t in (("ts", 96, "时间"), ("node", 190, "节点"),
                        ("kind", 130, "事件"), ("detail", 260, "摘要")):
            self.replay_tree.heading(c, text=t)
            self.replay_tree.column(c, width=w, anchor="w")
        self.replay_tree.tag_configure("ok", foreground="#8fe0a8")
        self.replay_tree.tag_configure("warn", foreground=THEME["warn"])
        self.replay_tree.tag_configure("err", foreground="#ff9a9a")
        vsb = ttk.Scrollbar(win, orient="vertical", command=self.replay_tree.yview)
        self.replay_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", pady=(0, 10))
        self.replay_tree.pack(fill="both", expand=True, padx=(10, 0), pady=(6, 0))
        self.replay_tree.bind("<<TreeviewSelect>>", self.replay_select)

        self.replay_hint = ttk.Label(
            win, style="Dim.TLabel", justify="left", wraplength=690,
            text="点时间轴任一行 → 画布聚焦该节点，并把当次的识别框/动作落点画到帧上。"
                 "识别框坐标与背景帧同一坐标系，可直接与 ROI 对照。")
        self.replay_hint.pack(anchor="w", padx=12, pady=(4, 10))
        win.protocol("WM_DELETE_WINDOW", self.replay_close)

    def replay_clear(self):
        self._replay = {"events": [], "offset": 0, "running": False}
        self._replay_hl = None
        self._replay_box = None
        self._replay_point = None
        if getattr(self, "replay_tree", None) and self.replay_tree.winfo_exists():
            for iid in self.replay_tree.get_children():
                self.replay_tree.delete(iid)
        self._replay_mark = None
        self.redraw()

    def replay_readonly(self):
        """只读现有日志做离线回放（不动手机、不运行任务）—— 调试解析器最方便"""
        self.replay_clear()
        self.replay_worker(run=False)

    def replay_start(self):
        self.replay_clear()
        # Tk 变量只能在主线程读，先取出来再交给工作线程
        self.replay_worker(run=True, entry=self.replay_entry_var.get().strip())

    def replay_worker(self, run, entry=""):
        q = queue.Queue()
        self._replay_q = q
        threading.Thread(target=self._replay_worker, args=(run, entry, q),
                         daemon=True).start()
        self.root.after(80, lambda: self._replay_poll(q))

    def _replay_worker(self, run, entry, q):
        reader = EngineLogReader()
        MAX_OFFLINE = 800          # 离线回放最多展示多少条（历史日志有几万条事件）
        try:
            if not project_paths.PACK_OK:
                q.put(("status", "找不到任务包，无法回放", ""))
                q.put(("done", None, ""))
                return
            if run:
                if not entry:
                    q.put(("status", "入口名为空", ""))
                    q.put(("done", None, ""))
                    return
                # 版本核对：手机上跑的是不是本地这一版
                want = pipeline_fingerprint(build_pipeline(self.flow, self.frame_wh))
                q.put(("status", "核对手机上该流程的版本…", ""))
                got, err = remote_pipeline_hash(self.flow["name"])
                if err:
                    q.put(("note", f"⚠ 版本核对失败：{err}", "warn"))
                elif got != want:
                    q.put(("note", "⚠ 手机上的 pipeline 与本地当前版本不一致 —— "
                                   "这次回放跑的是手机上那一份。要先同步请点工具栏"
                                   "「⇲ 同步到手机」再回放。", "warn"))
                else:
                    q.put(("note", "✓ 手机上的 pipeline 与本地当前版本一致", "ok"))
                q.put(("status", "记录日志起点…", ""))
                offset = reader.mark()
                q.put(("status", f"触发入口 {entry}（不 force-stop，虚拟屏不受影响）…", ""))
                self._adb_run("shell", "am", "start", "-n", f"{PKG}/.MainActivity")
                time.sleep(1.2)
                self._adb_run("shell", "am", "start", "-n", f"{PKG}/.MainActivity",
                              "--activity-single-top", "--es", "entry", entry)
                q.put(("offset", offset, ""))
            else:
                q.put(("status", "只读回放：读取手机日志并按当前流程筛选…", ""))
                events = parse_engine_log(reader.read_all())
                picked = [e for e in events
                          if node_id_of_pipeline_name(self.flow, e.name)]
                if picked:
                    picked = picked[-MAX_OFFLINE:]
                    q.put(("note", f"已从 {len(events)} 条历史事件里筛出本流程的 "
                                   f"{len(picked)} 条（只读回放，未运行任务）", "ok"))
                else:
                    picked = events[-MAX_OFFLINE:]
                    q.put(("note", f"历史日志里没有本流程（{self.flow['name']}）的事件，"
                                   f"已载入最后 {len(picked)} 条作参考", "warn"))
                q.put(("events", picked, ""))
                q.put(("status", f"只读回放：载入 {len(picked)} 条事件", ""))
                q.put(("done", None, ""))
                return

            deadline = time.time() + 300
            while time.time() < deadline:
                text = reader.read_since(offset)
                if text.strip():
                    offset += len(text.encode("utf-8", "replace"))
                    events = parse_engine_log(text)
                    q.put(("offset", offset, ""))
                    if events:
                        q.put(("events", events, ""))
                    if any(e.kind == "TaskEnd" for e in events):
                        q.put(("status", "任务已结束", ""))
                        break
                time.sleep(1.5)
            else:
                q.put(("status", "回放窗口已到 300 秒上限，停止监听", ""))
        except Exception as ex:                                  # noqa: BLE001
            q.put(("note", f"✗ 回放失败：{ex}", "err"))
        finally:
            q.put(("done", None, ""))

    def _adb_run(self, *args):
        return subprocess.run([ADB, "-s", DEVICE, *args], capture_output=True, timeout=90)

    def _replay_poll(self, q):
        if not (getattr(self, "replay_win", None) and self.replay_win.winfo_exists()):
            return
        got_events = False
        try:
            while True:
                kind, a, b = q.get_nowait()
                if kind == "status":
                    self.replay_status.set(a)
                elif kind == "note":
                    self.log((("⚠ " if b == "warn" else "") + a), b or "ok")
                    self.replay_status.set(a)
                elif kind == "offset":
                    self._replay["offset"] = a
                elif kind == "events":
                    if a:
                        self._replay["events"].extend(a)
                        for e in a:
                            if e.kind == "EngineFrame" and e.size:
                                self._engine_frame = tuple(e.size)
                        self._replay_add_rows(a)
                        self._replay_update_frame_note()
                        got_events = True
                elif kind == "done":
                    if got_events:
                        self._replay_show_last()
                    return
        except queue.Empty:
            pass
        if got_events:
            self._replay_show_last()
        self.root.after(150, lambda: self._replay_poll(q))

    def _replay_update_frame_note(self):
        """把「引擎识别帧 vs 背景帧」的一致性显示在面板上。
        虚拟屏分辨率/方向变了的时候，模板与固定坐标全部会失准，这里一眼可见。"""
        if not (getattr(self, "replay_win", None) and self.replay_win.winfo_exists()):
            return
        eng = getattr(self, "_engine_frame", None)
        W, H = self.frame_wh
        if not eng:
            return
        if eng[0] == W and eng[1] == H:
            self.replay_hint.config(
                text=f"引擎识别帧 {eng[0]}×{eng[1]} 与背景帧 {W}×{H} 一致 → "
                     f"识别框/落点可直接叠加对照。点时间轴任一行可聚焦节点。")
        else:
            self.replay_hint.config(
                text=f"⚠ 引擎识别帧 {eng[0]}×{eng[1]} 与背景帧 {W}×{H} 不一致："
                     f"说明虚拟屏分辨率或方向变了，此时模板与固定坐标都会失准；"
                     f"为避免画出错误位置的框，本面板不叠加识别框。"
                     f"请重抓帧（F5）确认画面，并检查虚拟屏是否横屏 1280x720。")

    def _replay_add_rows(self, events):
        tr = self.replay_tree
        base = len(self._replay["events"]) - len(events)
        for i, e in enumerate(events):
            try:
                tr.insert("", "end", iid=str(base + i),
                          values=(e.ts[-12:], e.name or "-", e.kind, e.detail),
                          tags=(e.level,))
            except tk.TclError:
                pass
        kids = tr.get_children()
        if kids:
            tr.see(kids[-1])

    def _replay_show_last(self):
        """自动跟随：停到最后一个「能映射到画布节点」的事件上。
        末尾常见 TaskEnd / 入口节点这类映射不到的事件，若直接取最后一条会丢掉高亮。"""
        evs = self._replay["events"]
        for e in reversed(evs):
            if node_id_of_pipeline_name(self.flow, e.name):
                self.replay_select(None, event=e)
                return
        if evs:
            self.replay_select(None, event=evs[-1])

    def replay_select(self, _e, event=None):
        """点时间轴某行（或传入事件）→ 画布聚焦 + 帧上画识别框/落点"""
        if event is None:
            sel = self.replay_tree.selection()
            if not sel:
                return
            try:
                event = self._replay["events"][int(sel[0])]
            except (ValueError, IndexError):
                return
        nid = node_id_of_pipeline_name(self.flow, event.name)
        self._replay_hl = nid
        self._replay_box = event.box
        self._replay_point = event.point
        if nid:
            self.sel = nid
            self.build_prop_panel()
            self._focus_node(nid)
        self.status(event.detail)
        self.redraw()

    def _focus_node(self, nid):
        """把画布滚到该节点（纵向居中，横向尽量露出）"""
        nd = self.flow["nodes"].get(nid)
        if not nd:
            return
        sr = self.canvas.cget("scrollregion").split()
        if len(sr) != 4:
            return
        x0, y0, x1, y1 = (float(v) for v in sr)
        cw = max(1, self.canvas.winfo_width())
        chh = max(1, self.canvas.winfo_height())
        z = self.zoom or 1.0
        if y1 > y0:
            self.canvas.yview_moveto(max(0.0, (nd["y"] * z - chh / 3) / (y1 - y0)))
        if x1 > x0:
            self.canvas.xview_moveto(max(0.0, (nd["x"] * z - cw / 3) / (x1 - x0)))

    def replay_close(self):
        win = getattr(self, "replay_win", None)
        self.replay_win = None
        if win:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self._replay_hl = None
        self._replay_box = None
        self._replay_point = None
        self.redraw()

    def _draw_replay_overlay(self):
        """把当前回放事件的识别框/落点画到帧上，并给命中的节点加高亮圈。
        ★ 坐标系守卫：只有当【引擎识别帧】与【当前背景帧】尺寸一致时才叠加 ——
        尺寸不一致说明虚拟屏分辨率/方向变了（例如竖屏 720x1608 vs 横屏 1280x720），
        这时按帧坐标画的框会落在错误位置，宁可不画并明确提示。"""
        nid = getattr(self, "_replay_hl", None)
        box = getattr(self, "_replay_box", None)
        point = getattr(self, "_replay_point", None)
        if not nid and not box and not point:
            return
        c = self.canvas
        nd = self.flow["nodes"].get(nid) if nid else None
        if nd is not None:
            h = self._sw_h(nd) if nd["type"] == "switch" else CARD_H
            _round_rect(c, nd["x"] - 5, nd["y"] - 5, nd["x"] + CARD_W + 5,
                        nd["y"] + h + 5, 14, outline="#41d1a0", width=2, fill="")
            c.create_text(nd["x"] + CARD_W + 10, nd["y"] - 5, anchor="nw",
                          fill="#41d1a0", font=self.f_sm, text="◀ 回放")
        if not self.bg_disp:
            return
        ox, oy, dw, dh = self.bg_disp
        W, H = self.frame_wh
        eng = getattr(self, "_engine_frame", None)
        mismatch = None
        if eng and (eng[0] != W or eng[1] != H):
            mismatch = (eng, (W, H))
        if box and (box[0] + box[2] > W or box[1] + box[3] > H):
            mismatch = mismatch or ((box[2], box[3]), (W, H))
        px = lambda x: ox + x * dw / W
        py = lambda y: oy + y * dh / H
        if mismatch:
            (ew, eh), (bw, bh) = mismatch
            txt = (f"⚠ 引擎识别帧 {ew}×{eh} ≠ 当前背景帧 {bw}×{bh}，"
                   f"识别框/落点无法定位，未叠加（请重抓帧确认虚拟屏分辨率）")
            tw = min(dw - 20, 13 * len(txt) + 20)
            _round_rect(c, ox + 6, oy + 6, ox + 6 + tw, oy + 34, 6,
                        fill="#2a1114", outline=THEME["err"])
            c.create_text(ox + 16, oy + 20, anchor="w", fill="#ffb3b3",
                          font=self.f_sm, text=txt)
            return
        if box:
            x, y, w, hh = box
            c.create_rectangle(px(x), py(y), px(x + w), py(y + hh),
                               outline="#41d1a0", width=2)
            c.create_text(px(x), py(y) - 8, anchor="sw", fill="#41d1a0",
                          font=self.f_sm, text="识别框")
        if point:
            x, y = point
            c.create_oval(px(x) - 8, py(y) - 8, px(x) + 8, py(y) + 8,
                          outline="#41d1a0", width=2)
            c.create_line(px(x) - 14, py(y), px(x) + 14, py(y), fill="#41d1a0")
            c.create_line(px(x), py(y) - 14, px(x), py(y) + 14, fill="#41d1a0")

    # ---------- 撤销/重做（P2-4）与复制粘贴（P2-5） ----------

    def _flow_text(self):
        return json.dumps(self.flow, ensure_ascii=False, sort_keys=True)

    def _snapshot(self, tag=""):
        """改动前存档。
        内容没变就不入栈（选择节点之类的空操作不会污染撤销栈）；
        同一处连续编辑（tag 相同且在 3 秒内）合并成一条，避免每敲一个键都存一次。"""
        cur = self._flow_text()
        now = time.time()
        if self._undo and self._undo[-1][0] == cur:
            return
        if (tag and self._undo and self._undo[-1][1] == tag
                and now - self._undo[-1][2] < 3.0):
            self._undo[-1] = (self._undo[-1][0], tag, now)
            self._redo.clear()
            return
        self._undo.append((cur, tag, now))
        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)
        self._redo.clear()

    def _restore_flow_text(self, text):
        self.flow = json.loads(text)
        self.flow.setdefault("chain", [])
        self.flow.setdefault("nodes", {})
        self.sel = None
        self.name_var.set(self.flow.get("name", "未命名"))
        self.build_prop_panel()
        self.redraw()

    def undo(self):
        if not self._undo:
            self.log("没有可撤销的操作", "warn")
            return
        self._redo.append((self._flow_text(), self._undo[-1][1], time.time()))
        text = self._undo.pop()[0]
        self._restore_flow_text(text)
        self.log(f"↶ 已撤销（还可撤销 {len(self._undo)} 步）")

    def redo(self):
        if not self._redo:
            self.log("没有可重做的操作", "warn")
            return
        self._undo.append((self._flow_text(), self._redo[-1][1], time.time()))
        text = self._redo.pop()[0]
        self._restore_flow_text(text)
        self.log(f"↷ 已重做（还可重做 {len(self._redo)} 步）")

    def on_copy(self, _e=None):
        nid = self.sel
        if not nid or nid not in self.flow["nodes"]:
            return
        self._clipboard = json.loads(json.dumps(self.flow["nodes"][nid]))
        self.log(f"已复制节点「{self._clipboard.get('title', nid)}」"
                 f"（{self._clipboard.get('type')}）")

    def on_paste(self, _e=None):
        if not self._clipboard:
            self.log("剪贴板为空：先选中节点按 Ctrl+C", "warn")
            return
        src = self._clipboard
        self._snapshot()
        self._nid += 1
        nid = f"n{self._nid:03d}{os.urandom(2).hex()}"
        nd = json.loads(json.dumps(src))
        # 出口一律清空：出口指向的是具体节点，复制品沿用会指向原来的节点
        nd["hit_next"] = None
        nd["miss_next"] = None
        if isinstance(nd.get("props", {}).get("candidates"), list):
            for c in nd["props"]["candidates"]:
                if isinstance(c, dict):
                    c.pop("next", None)
        anchor = self.sel if self.sel in self.flow["chain"] else None
        if anchor:
            base = self.flow["nodes"][anchor]
            nd["x"] = base["x"] + 40
            nd["y"] = base["y"] + (self._sw_h(base) if base["type"] == "switch"
                                   else CARD_H) + 30
            self.flow["chain"].insert(self.flow["chain"].index(anchor) + 1, nid)
        else:
            ch = self.flow["chain"]
            if ch:
                last = self.flow["nodes"][ch[-1]]
                nd["x"], nd["y"] = last["x"], last["y"] + CARD_H + 30
            else:
                nd["x"], nd["y"] = self._chain_col_x(), 60
            self.flow["chain"].append(nid)
        self.flow["nodes"][nid] = nd
        self.sel = nid
        self.build_prop_panel()
        self.redraw()
        self._scroll_to(nd["y"])
        self.log(f"已粘贴节点「{nd.get('title', nid)}」（出口未连线，请重新连）")

    def on_duplicate(self, _e=None):
        self.on_copy()
        self.on_paste()

    def nudge_node(self, dx, dy):
        """方向键微调选中节点的画布坐标"""
        nid = self.sel
        if not nid or nid not in self.flow["nodes"]:
            return
        self._snapshot(f"nudge:{nid}")
        nd = self.flow["nodes"][nid]
        nd["x"] = max(0, nd["x"] + dx)
        nd["y"] = max(0, nd["y"] + dy)
        self.redraw()
        self.status(f"节点坐标 → ({int(nd['x'])}, {int(nd['y'])})")

    def on_escape(self, _e=None):
        """Esc：取消 ROI 拖框 / 取消取点 / 取消正在拖的连线"""
        if self.roi_pick is not None:
            self.roi_pick = None
            self.redraw()
            self.status("已取消框选 ROI")
        elif self.pick_target:
            self.pick_target = None
            self.status("已取消取点")
        elif self.wire:
            self.wire = None
            self.redraw()
            self.status("已取消连线")

    def _start_roi_pick(self, key):
        """在背景帧上拖框来填 ROI（x,y,w,h），省得手敲坐标"""
        if not self.sel:
            return
        if self.bg_disp is None:
            self.status("框选 ROI 需要先有背景帧：先抓帧（F5）或打开帧图")
            self.log("⚠ 框选失败：画布上没有背景帧。先点「⟳ 抓帧 (F5)」。", "warn")
            return
        self.roi_pick = {"key": key, "x0": None, "y0": None, "x1": None, "y1": None}
        self.status("拖框模式：在左侧帧画面上拖出识别区域（Esc 取消）")

    def _finish_roi_pick(self):
        rp = self.roi_pick
        self.roi_pick = None
        if not rp or None in (rp["x0"], rp["x1"]):
            self.status("已取消框选")
            return
        if not self.sel or self.sel not in self.flow["nodes"]:
            return
        a = self._canvas_to_frame(rp["x0"], rp["y0"])
        b = self._canvas_to_frame(rp["x1"], rp["y1"])
        if a is None or b is None:
            self.log("⚠ 框选超出帧画面范围，未写入", "warn")
            self.status("框选无效")
            return
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        W, H = self.frame_wh
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, x1), min(H, y1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            self.log("⚠ 框选太小，未写入", "warn")
            return
        self._snapshot(f"roi:{self.sel}")
        self.flow["nodes"][self.sel]["props"][rp["key"]] = f"{x0},{y0},{x1 - x0},{y1 - y0}"
        self.build_prop_panel()
        self.redraw()
        self.log(f"✓ 已写入 ROI {x0},{y0},{x1 - x0},{y1 - y0}"
                 f"（帧坐标，基准 {W}×{H}）", "ok")

    # ---------- 模板管理（P2-6） ----------

    def on_template_manager(self):
        """模板清单：尺寸 + 被哪些流程引用；顺便暴露没被任何流程引用的模板"""
        if getattr(self, "tpl_win", None) and self.tpl_win.winfo_exists():
            self.tpl_win.lift()
            return
        usage = template_usage()
        win = tk.Toplevel(self.root)
        self.tpl_win = win
        win.title("模板管理")
        win.configure(bg=THEME["panel"])
        win.geometry("620x560+%d+%d" % (self.root.winfo_rootx() + 200,
                                        self.root.winfo_rooty() + 160))
        win.transient(self.root)
        names = list_templates()
        used = [t for t in names if usage.get(t)]
        free = [t for t in names if not usage.get(t)]
        ttk.Label(win, style="Title.TLabel",
                  text=f"  模板 {len(names)} 张 · 被引用 {len(used)} 张 · "
                       f"未被任何流程引用 {len(free)} 张").pack(anchor="w", pady=(10, 4))
        tip = ("未被引用的模板不一定是垃圾（可能是给别的任务包或以后用的），"
               "所以这里只列出不自动删。模板图目录：whmx/image/")
        ttk.Label(win, text=tip, style="Dim.TLabel", wraplength=590,
                  justify="left").pack(anchor="w", padx=12)

        cols = ("name", "size", "n", "flows")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=20)
        for c, w, t in (("name", 220, "模板名"), ("size", 80, "尺寸"),
                        ("n", 46, "引用"), ("flows", 240, "被哪些流程引用")):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.tag_configure("free", foreground=THEME["warn"])
        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y", pady=(6, 10))
        tree.pack(fill="both", expand=True, padx=(12, 0), pady=(6, 0))
        for t in names:
            size = "-"
            path = os.path.join(IMG_DIR, t)
            try:
                with Image.open(path) as im:
                    size = f"{im.width}×{im.height}"
            except Exception:
                size = "读取失败"
            fs = usage.get(t) or []
            tree.insert("", "end", values=(t, size, len(fs), "、".join(fs)),
                        tags=() if fs else ("free",))
        ttk.Label(win, style="Dim.TLabel", justify="left", wraplength=590,
                  text="黄色行 = 没有被任何 flows/*.flow.json 引用。"
                       "选中节点的模板下拉会实时刷新，框好新模板保存后即可选到。").pack(
            anchor="w", padx=12, pady=(4, 10))
        win.protocol("WM_DELETE_WINDOW", self._close_tpl_win)

    def _close_tpl_win(self):
        win = getattr(self, "tpl_win", None)
        self.tpl_win = None
        if win:
            try:
                win.destroy()
            except tk.TclError:
                pass

    # ---------- 校验/生成/同步 ----------

    def _collect_issues(self):
        """完整校验（模型级 + 生成结果级），把问题按节点归档并写日志。
        返回 (errors, warnings) 字符串列表，供 on_build/on_sync 判断是否阻断。"""
        issues = collect_all_issues(self.flow, self.frame_wh)
        self._issues_by_node = {}
        for it in issues:
            if it.node_id:
                self._issues_by_node.setdefault(it.node_id, []).append(it)
        for it in issues:
            if it.level == "error":
                self.log("✗ " + it.message, "err")
            else:
                self.log("⚠ " + it.message, "warn")
        errs = [i.message for i in issues if i.level == "error"]
        warns = [i.message for i in issues if i.level == "warn"]
        if not errs:
            self.log("✓ 校验通过" + (f"（{len(warns)} 条警告）" if warns else ""), "ok")
        return errs, warns

    def on_validate(self):
        self._collect_issues()

    def on_build(self):
        self._on_name_change()
        errs, _ = self._collect_issues()
        if errs:
            messagebox.showerror("校验未通过", "请先修复错误（见日志）")
            return
        try:
            path, data = write_pipeline_json(self.flow, self.frame_wh)
        except FlowValidationError as ex:
            messagebox.showerror("生成失败", "\n".join(ex.errors))
            return
        self.log(f"✓ 已生成 {path}（{len(data)} 个节点）", "ok")
        self.status("生成完成: " + os.path.basename(path))
        return path

    def on_sync(self, run_after):
        self._on_name_change()
        errs, _ = self._collect_issues()
        if errs:
            messagebox.showerror("校验未通过", "请先修复错误（见日志）")
            return
        if self._syncing:
            return
        if run_after and not messagebox.askyesno(
                "同步并运行",
                "将 force-stop 重启 App 以加载新流程（虚拟屏会自动重建），"
                "然后立即运行 VF_" + self.flow["name"] + "。继续？"):
            return
        # adb 在后台线程执行；tk 只能主线程碰 → 线程只往队列放消息，主线程轮询刷新
        q = queue.Queue()
        dlg = SyncDialog(self.root, "同步并运行" if run_after else "同步到手机")
        self._syncing = True
        self._btn_sync.config(state="disabled")
        self._btn_run.config(state="disabled")
        threading.Thread(target=self._sync_worker,
                         args=(run_after, dlg, q), daemon=True).start()
        self._poll_sync(q, dlg)

    def _poll_sync(self, q, dlg):
        """主线程轮询同步消息队列：prog/log/status/done；窗口销毁后停止并恢复按钮"""
        try:
            while True:
                kind, a, b = q.get_nowait()
                if kind == "prog":
                    dlg.set_progress(a, b)
                elif kind == "log":
                    self.log(a, b or None)
                elif kind == "status":
                    self.status(a)
                elif kind == "done":
                    dlg.finish(a, b)
                    self._syncing = False
                    self._btn_sync.config(state="normal")
                    self._btn_run.config(state="normal")
                    return
        except queue.Empty:
            pass
        if dlg.winfo_exists():
            self.root.after(60, lambda: self._poll_sync(q, dlg))
            return
        self._syncing = False
        self._btn_sync.config(state="normal")
        self._btn_run.config(state="normal")

    def _sync_worker(self, run_after, dlg, q):
        def prog(pct, text):
            q.put(("prog", pct, text))

        def tlog(msg, tag=""):
            q.put(("log", msg, tag))

        def tstatus(s):
            q.put(("status", s, ""))

        try:
            prog(8, "生成 pipeline JSON…")
            path, _ = write_pipeline_json(self.flow, self.frame_wh)
            tlog("✓ 已生成 " + path)
            prog(25, "推送到手机…")
            sync_pipeline_file(path, tlog)
            # 同步更新项目包内的 vf_*.json 拷贝（派遣/喝茶集成与后续打包依赖它）
            try:
                proj_copy = os.path.join(ROOT, "whmx", "pipeline", os.path.basename(path))
                shutil.copy2(path, proj_copy)
                tlog("✓ 项目包副本已更新 whmx/pipeline/" + os.path.basename(path))
            except OSError as ex:
                tlog("⚠ 项目包副本更新失败: " + str(ex), "warn")
            sync_templates(self.flow, tlog,
                           lambda pct, text: prog(pct, text))
            prog(60, "注册到【小工具】清单…")
            try:
                register_on_phone(self.flow["name"], tlog)
            except Exception as ex:
                tlog("⚠ 注册【小工具】清单失败（不影响直达入口运行）: " + str(ex), "warn")
            entry = "VF_" + self.flow["name"]
            if run_after:
                prog(80, "重启 App 并运行（虚拟屏会自动重建）…")
                launch_on_phone(entry, tlog, tstatus, ask=False)
                prog(96, "已下发运行指令")
                q.put(("done", True, "✓ 已同步并在手机上启动：" + entry))
            else:
                q.put(("done", True,
                       "✓ 同步完成，重启 App 后在【小工具】栏可见：" + self.flow["name"]))
        except Exception as ex:
            tlog("✗ 同步失败: " + str(ex), "err")
            q.put(("done", False, "✗ 同步失败：" + str(ex)))


# ================= selftest =================

def selftest():
    """无 GUI 校验核心生成逻辑"""
    print(project_paths.describe())
    flow = {"name": "自测流程", "chain": [], "nodes": {}}

    def add(nid, t, props, **kw):
        nd = {"type": t, "x": 0, "y": 0, "title": t, "props": props}
        nd.update(kw)
        flow["nodes"][nid] = nd
        flow["chain"].append(nid)

    add("a", "startapp", {"package": "x.y", "post_delay": 1000})
    add("b", "tpl_click", {"template": "qizhe.png", "threshold": 0.8, "roi": "",
                           "timeout": 8000, "pre_delay": 0, "post_delay": 800,
                           "repeat": 1, "repeat_delay": 350, "order_by": True})
    add("c", "branch", {"template": "sutong_dialog.png", "threshold": 0.7,
                        "roi": "100,200,300,400", "timeout": 4000},
        hit_next=None, miss_next="e")
    add("d", "swipe", {"x1": 1000, "y1": 600, "x2": 280, "y2": 600,
                       "duration": 900, "post_delay": 600})
    add("e", "tap", {"x": 640, "y": 360, "pre_delay": 0, "post_delay": 500,
                     "repeat": 3, "repeat_delay": 350})
    add("f", "common", {"node": "Common_回主页"})

    errs, warns = validate_flow(flow, (1280, 720))
    assert not errs, f"校验意外报错: {errs}"
    out = build_pipeline(flow, (1280, 720))
    E = "VF_自测流程"
    assert out[E] == {"next": [f"{E}_01"]}, out.get(E)
    assert out[f"{E}_02"]["order_by"] == "Score"
    assert "rate_limit" not in out[f"{E}_02"], "rate_limit=0 不应写入"
    assert out[f"{E}_02"]["next"] == [f"{E}_03"]
    # 分支：hit 自动→04（swipe），miss 显式→e(05 tap)
    assert out[f"{E}_03"]["on_error"] == [f"{E}_05"], out[f"{E}_03"]
    assert out[f"{E}_03"]["next"] == [f"{E}_03_Hit"]
    assert out[f"{E}_03_Hit"]["next"] == [f"{E}_04"]
    assert out[f"{E}_03_Hit"]["roi"] == [100, 200, 300, 400]
    assert out[f"{E}_05"]["repeat"] == 3
    assert out[f"{E}_05"]["next"] == [f"{E}_06"]
    assert out[f"{E}_06"] == {"next": ["Common_回主页"]}
    assert f"{E}_End" not in out, "miss 已显式连线则不需要 End"
    # miss 未连线的分支要生成 End 收口
    flow["nodes"]["c"]["miss_next"] = None
    out2 = build_pipeline(flow, (1280, 720))
    assert out2[f"{E}_03"]["on_error"] == [f"{E}_End"]
    assert out2[f"{E}_End"] == {"action": "DoNothing", "next": []}
    # swipe 支持 repeat（v5.3 通用 action 字段）
    flow["nodes"]["d"]["props"]["repeat"] = 2
    flow["nodes"]["d"]["props"]["repeat_delay"] = 450
    out3 = build_pipeline(flow, (1280, 720))
    assert out3[f"{E}_04"]["repeat"] == 2
    assert out3[f"{E}_04"]["repeat_delay"] == 450
    # 默认 repeat=1 不写字段
    flow["nodes"]["d"]["props"]["repeat"] = 1
    out4 = build_pipeline(flow, (1280, 720))
    assert "repeat" not in out4[f"{E}_04"]
    flow["nodes"]["d"]["props"].pop("repeat", None)
    flow["nodes"]["d"]["props"].pop("repeat_delay", None)
    # 多候选模板（任一命中即命中）：多值 → 生成数组；缺一个 → 校验报错
    flow["nodes"]["c"]["props"]["template"] = "sutong_dialog.png,yanxun.png"
    out5 = build_pipeline(flow, (1280, 720))
    assert out5[f"{E}_03_Hit"]["template"] == ["sutong_dialog.png", "yanxun.png"], out5[f"{E}_03_Hit"]
    flow["nodes"]["c"]["props"]["template"] = "sutong_dialog.png,不存在.png"
    errs, _ = validate_flow(flow, (1280, 720))
    assert errs and any("模板不存在" in e for e in errs), errs
    flow["nodes"]["c"]["props"]["template"] = "sutong_dialog.png"
    # 枝干判定（switch）：级联展开 + 内容叶节点剥离线性 next
    sf = {"name": "枝干测", "chain": ["s", "c1", "c2"],
          "nodes": {
              "s": {"type": "switch", "x": 0, "y": 0, "title": "s",
                    "props": {"candidates": [
                        {"t": "yanxun.png", "timeout": 3000, "next": "c1"},
                        {"t": "OCR:确认", "timeout": 2000, "next": "c2"}],
                        "miss_next": None}},
              "c1": {"type": "tap", "x": 0, "y": 0, "title": "c1",
                     "props": {"x": 100, "y": 100, "pre_delay": 0, "post_delay": 500,
                               "post_wait_freezes": 0, "repeat": 1, "repeat_delay": 350}},
              "c2": {"type": "tap", "x": 0, "y": 0, "title": "c2",
                     "props": {"x": 200, "y": 200, "pre_delay": 0, "post_delay": 500,
                               "post_wait_freezes": 0, "repeat": 1, "repeat_delay": 350}}}}
    errs, _ = validate_flow(sf, (1280, 720))
    assert not errs, f"switch 校验报错: {errs}"
    out_sw = build_pipeline(sf, (1280, 720))
    B = "VF_枝干测"
    assert out_sw[f"{B}_01_J1"]["on_error"] == [f"{B}_01_J2"]
    assert out_sw[f"{B}_01_J1_Hit"]["next"] == [f"{B}_02"]
    assert out_sw[f"{B}_01_J2"]["on_error"] == [f"{B}_End"]
    assert out_sw[f"{B}_01_J2_Hit"]["recognition"] == "OCR"
    assert "next" not in out_sw[f"{B}_02"] and "next" not in out_sw[f"{B}_03"], \
        "内容叶节点不应沿线性链继续（防分支串线）"
    assert f"{B}_End" in out_sw
    # 坐标越界报错（1280 宽画布 x=1400）
    flow["nodes"]["d"]["props"]["x1"] = 1400
    errs, _ = validate_flow(flow, (1280, 720))
    assert errs and "滑动坐标" in errs[0], errs
    # 模板不存在报错
    flow["nodes"]["d"]["props"]["x1"] = 600
    flow["nodes"]["b"]["props"]["template"] = "不存在.png"
    errs, _ = validate_flow(flow, (1280, 720))
    assert errs and "模板不存在" in errs[0], errs
    # JSON 可序列化
    json.dumps(out2, ensure_ascii=False)
    print("selftest OK：校验/生成逻辑全部通过")

    # GUI 构建烟测（有显示环境时）
    if "--no-gui" not in sys.argv:
        try:
            root = tk.Tk()
            root.withdraw()
            editor = FlowEditor(root)
            editor.add_node("tap")
            editor.add_node("swipe")
            editor.add_node("branch")
            editor.redraw()
            root.update()
            root.destroy()
            print("GUI 构建烟测通过")
        except tk.TclError as e:
            print(f"GUI 烟测跳过（无显示环境）: {e}")


# ================= main =================

def main():
    # pythonw（无控制台）下 stdout/stderr 为 None，print 会崩 → 重定向到日志文件
    if sys.stdout is None or sys.stderr is None:
        _lp = os.path.join(TOOLS_DIR, "flow_editor.log")
        if os.path.isfile(_lp) and os.path.getsize(_lp) > 512 * 1024:
            os.remove(_lp)
        logf = open(_lp, "a", encoding="utf-8")
        sys.stdout = sys.stdout or logf
        sys.stderr = sys.stderr or logf
    os.makedirs(FLOWS_DIR, exist_ok=True)
    if "--selftest" in sys.argv:
        selftest()
        return
    root = tk.Tk()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    load = None
    if args:
        p = args[0]
        if p.startswith("/") and len(p) > 2 and p[2] == "/":
            p = p[1].upper() + ":" + p[2:]   # Git Bash 风格路径 → Windows
        load = p
    FlowEditor(root, load_path=load)
    root.mainloop()


if __name__ == "__main__":
    main()
