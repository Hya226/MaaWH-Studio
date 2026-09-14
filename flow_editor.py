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
import bisect
import subprocess
import shutil
import threading
import queue
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

# 虚拟屏帧基准（横屏 720p，实测 1280x720）：坐标校验/显示用；载入背景帧后按实际图尺寸更新
FRAME_W, FRAME_H = 1280, 720
FRAME_DISP_H = 880          # 竖屏帧的画布显示高度；横屏帧自动改用 FRAME_DISP_H_LS
FRAME_DISP_H_LS = 540       # 横屏帧的画布显示高度（按宽度适配，约 960 宽）
CARD_W, CARD_H = 240, 60    # 节点卡片尺寸

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

def tpl_summary(p):
    """节点卡片摘要：模板多候选显示 '首个 +N'，单值直接显示"""
    parts = split_tpls(p.get("template", ""))
    if len(parts) > 1:
        return f"{parts[0]} +{len(parts) - 1}"
    return parts[0] if parts else "?"


def parse_switch_cands(raw):
    """解析枝干候选列表：
    raw 为 [{t, timeout, next}, ...]；t 支持 '模板名.png'（模板识别）或 'OCR:文字'。
    返回规范化列表，剔除空候选；next = 命中内容起点节点 id（可缺省）。"""
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
            ("text", "识别文本(多个用,分隔)", "str"),
            ("roi", "ROI x,y,w,h (空=全屏)", "roi"),
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
            ("order_by", "精确单目标(Score)", "bool"),
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
            ("post_delay", "滑动后延时ms", "int"),
        ],
        "defaults": {"x1": 1000, "y1": 600, "x2": 280, "y2": 600,
                     "duration": 900, "repeat": 1, "repeat_delay": 350,
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
    "common": {
        "label": "公共节点(收口)", "icon": "⌂", "color": "#4e8f8f", "light": "#9cdcdc",
        "summary": lambda p: p.get("node", "?"),
        "fields": [("node", "公共节点", "common")],
        "defaults": {"node": "Common_回主页"},
    },
    "startapp": {
        "label": "启动游戏", "icon": "▶", "color": "#b05f5f", "light": "#f0a8a8",
        "summary": lambda p: str(p.get("package", GAME_PKG)).split(".")[-1],
        "fields": [
            ("package", "包名", "str"),
            ("post_delay", "启动后延时ms", "int"),
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
              "switch", "common", "startapp"]


def _num(v, default=-1):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def normalize_flow(flow):
    """把旧版本流程文件里存成字符串的数值字段转回 int"""
    for nd in flow.get("nodes", {}).values():
        spec = NODE_TYPES.get(nd.get("type"))
        if not spec:
            continue
        for key, _label, kind in spec["fields"]:
            if kind in ("int", "pick", "pick2") and key in nd.get("props", {}):
                try:
                    nd["props"][key] = int(float(nd["props"][key]))
                except (TypeError, ValueError):
                    pass
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
    不沿线性链继续（防分支内容串线）。与旧 build_pipeline 内的 switch_leaf 等价。"""
    leaves = set()
    for nd in flow.get("nodes", {}).values():
        if nd.get("type") != "switch":
            continue
        for c in parse_switch_cands((nd.get("props") or {}).get("candidates")):
            if c.get("next"):
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
          "hit": None, "miss": None, "candidates": []}
    if t == "branch":
        ex["hit"] = nd.get("hit_next") or None
        ex["miss"] = nd.get("miss_next") or None
    elif t == "switch":
        ex["miss"] = p.get("miss_next") or None
        for i, c in enumerate(parse_switch_cands(p.get("candidates"))):
            item = dict(c)
            item["index"] = i
            ex["candidates"].append(item)
    return ex


def validate_flow(flow, frame_wh=(FRAME_W, FRAME_H)):
    """返回 (errors, warnings)"""
    errs, warns = [], []
    if not flow.get("name"):
        errs.append("流程名为空")
    chain = flow.get("chain", [])
    nodes = flow.get("nodes", {})
    if not chain:
        errs.append("流程没有节点")
    W, H = frame_wh
    title = lambda nd: f"「{nd.get('title', '?')}」"
    for i, nid in enumerate(chain):
        nd = nodes.get(nid)
        if nd is None:
            errs.append(f"链上有失效节点引用: {nid}")
            continue
        t, p = nd["type"], nd.get("props", {})
        no = f"#{i+1}"
        if t == "branch" and str(p.get("ocr_text", "")).strip():
            pass   # OCR 文字判定分支无需模板图
        elif t in ("tpl_click", "wait_tpl", "branch"):
            tpls = split_tpls(p.get("template", ""))
            if not tpls:
                errs.append(f"{no}{title(nd)}未选择模板图")
            else:
                for tpl in tpls:
                    if not os.path.isfile(os.path.join(IMG_DIR, tpl)):
                        errs.append(f"{no}{title(nd)}模板不存在: whmx/image/{tpl}")
        if t in ("tpl_click", "wait_tpl", "branch", "ocr_click") and p.get("roi"):
            roi = parse_roi(p["roi"])
            if roi is None:
                errs.append(f"{no}{title(nd)}ROI 格式应为 x,y,w,h")
            elif roi[0] < 0 or roi[1] < 0 or roi[0] + roi[2] > W or roi[1] + roi[3] > H:
                warns.append(f"{no}{title(nd)}ROI {p['roi']} 超出画面 {W}x{H}")
        if t == "tap":
            x, y = _num(p.get("x", -1)), _num(p.get("y", -1))
            if not (0 <= x < W and 0 <= y < H):
                errs.append(f"{no}{title(nd)}点击坐标 ({p.get('x')},{p.get('y')}) "
                            f"非法或超出画面 {W}x{H}")
        if t == "swipe":
            for k in ("x1", "y1", "x2", "y2"):
                v = _num(p.get(k, -1))
                lim = W if k.startswith("x") else H
                if not (0 <= v < lim):
                    errs.append(f"{no}{title(nd)}滑动坐标 {k}={p.get(k)} "
                                f"非法或超出画面 {W}x{H}")
        if t in ("tpl_click", "wait_tpl", "branch"):
            try:
                th = float(p.get("threshold", 0))
                if not (0.3 <= th <= 0.99):
                    raise ValueError
            except (TypeError, ValueError):
                errs.append(f"{no}{title(nd)}阈值应为 0.3~0.99 的数字")
        if t == "branch":
            for port, label in (("hit_next", "✓命中"), ("miss_next", "✗未命中")):
                tgt = nd.get(port)
                if tgt is not None and tgt not in nodes:
                    errs.append(f"{no}{title(nd)}{label}出口指向已删除节点")
                elif tgt == nid:
                    errs.append(f"{no}{title(nd)}{label}出口不能指向自己")
                elif tgt is not None and tgt in chain and chain.index(tgt) < i:
                    warns.append(f"{no}{title(nd)}{label}出口跳回前面的节点（构成循环），"
                                 f"请确保循环内有终止条件（如分支/收口节点）")
        if t == "switch":
            cands = parse_switch_cands(p.get("candidates"))
            if not cands:
                errs.append(f"{no}{title(nd)}枝干没有可用的候选")
            for ci, c in enumerate(cands):
                spec = switch_cand_spec(c["t"])
                lab = f"候选{ci + 1}「{c['t']}」"
                if spec is None:
                    errs.append(f"{no}{title(nd)}{lab}格式应为 模板名.png 或 OCR:文字")
                elif spec[0] == "Template" and not os.path.isfile(
                        os.path.join(IMG_DIR, c["t"])):
                    errs.append(f"{no}{title(nd)}{lab}模板不存在: whmx/image/{c['t']}")
                nxt = c.get("next")
                if nxt is not None and nxt not in nodes:
                    errs.append(f"{no}{title(nd)}{lab}命中出口指向已删除节点")
                elif nxt == nid:
                    errs.append(f"{no}{title(nd)}{lab}命中出口不能指向自己")
            mn = p.get("miss_next")
            if mn and mn not in nodes:
                errs.append(f"{no}{title(nd)}全部未中出口指向已删除节点")
        if t == "common" and i < len(chain) - 1:
            ref = str(p.get("node", ""))
            if ref.startswith("VF_"):
                warns.append(f"{no}{title(nd)}为跨流程调用（进入 {ref}，"
                             f"跑完即结束，不会返回本流程）")
            else:
                warns.append(f"{no}{title(nd)}是公共收口节点（进入后流程即终止），"
                             f"放在链中间会导致其后的节点执行不到")
    return errs, warns


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
    if p.get("order_by"):
        d["order_by"] = "Score"
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
    texts = [s.strip() for s in str(p.get("text", "")).replace("，", ",").split(",") if s.strip()]
    if not texts:
        raise FlowValidationError([f"OCR节点未填写识别文本: {name}"])
    d = {
        "recognition": "OCR",
        "text": texts,
        "action": "Click",
        "timeout": int(p["timeout"]),
        "post_delay": int(p["post_delay"]),
    }
    roi = parse_roi(p.get("roi", ""))
    if roi:
        d["roi"] = roi
    if int(p.get("rate_limit", 0) or 0) > 0:
        d["rate_limit"] = int(p["rate_limit"])
    if int(p.get("pre_delay", 0)):
        d["pre_delay"] = int(p["pre_delay"])
    if nxt:
        d["next"] = nxt
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
            "text": [s.strip() for s in
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
    out[name] = {"next": [p["node"]]}


def _emit_startapp(out, name, p, nxt):
    d = {
        "action": "StartApp",
        "package": p["package"],
        "post_delay": int(p["post_delay"]),
    }
    if nxt:
        d["next"] = nxt
    out[name] = d


def _emit_switch(flow, out, nid, i, p):
    """枝干判定：候选从左到右级联判定，命中→走该候选内容（执行完枝干结束），
    未中→下一个候选；全部未中→ miss 出口（默认流程结束）。"""
    E = entry_name(flow)
    cands = exits_of(flow, nid)["candidates"]
    seq = f"{E}_{i + 1:02d}"
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
                  "text": [spec[1]], "action": "DoNothing", "next": go}
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


def build_pipeline(flow, frame_wh=(FRAME_W, FRAME_H)):
    """流程定义 → MaaFramework pipeline dict（VF_ 前缀命名空间）。"""
    errs, _ = validate_flow(flow, frame_wh)
    if errs:
        raise FlowValidationError(errs)
    chain = flow["chain"]
    nodes = flow["nodes"]
    E = entry_name(flow)

    out = {E: {"next": [jname(flow, chain[0])] if chain else []}}
    end_needed = False

    for i, nid in enumerate(chain):
        nd = nodes[nid]
        t, p = nd["type"], nd.get("props", {})
        base = jname(flow, nid)
        nxt = chain_next_names(flow, nid)

        if t == "tpl_click":
            _emit_tpl_click(out, base, p, nxt)

        elif t == "ocr_click":
            _emit_ocr_click(out, base, p, nxt)

        elif t == "tap":
            _emit_tap(out, base, p, nxt)

        elif t == "swipe":
            _emit_swipe(out, base, p, nxt)

        elif t == "wait_tpl":
            _emit_wait_tpl(out, base, p, nxt)

        elif t == "branch":
            end_needed |= _emit_branch(flow, out, nid, base, p, nxt)

        elif t == "common":
            _emit_common(out, base, p)

        elif t == "startapp":
            _emit_startapp(out, base, p, nxt)

        elif t == "switch":
            _emit_switch(flow, out, nid, i, p)

    if end_needed or any(nodes[n].get("type") == "switch" for n in chain):
        out[f"{E}_End"] = {"action": "DoNothing", "next": []}
    return out


class FlowValidationError(Exception):
    def __init__(self, errs):
        super().__init__("; ".join(errs))
        self.errors = errs


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

        ttk.Separator(left).pack(fill="x", pady=12, padx=8)
        ttk.Label(left, text="  背景帧 · 对照坐标", style="Title.TLabel").pack(
            anchor="w", padx=8, pady=(0, 4))
        self._flat_btn(left, "⟳  抓帧 (F5)", self.on_capture).pack(fill="x", padx=8, pady=1)
        self._flat_btn(left, "✛  框选模板…", self.on_pick_template).pack(fill="x", padx=8, pady=1)
        self._flat_btn(left, "📂  打开帧图…", self.on_open_frame).pack(fill="x", padx=8, pady=1)
        ttk.Checkbutton(left, text="显示背景帧", variable=self.show_bg,
                        command=self.redraw).pack(anchor="w", padx=12, pady=3)
        self.frame_lbl = ttk.Label(left, text="", style="Dim.TLabel", justify="left")
        self.frame_lbl.pack(anchor="w", padx=12, pady=2)
        self._update_frame_label()

    def _build_canvas(self):
        self.canvas = tk.Canvas(self.root, bg=THEME["canvas"], highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_down)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_up)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Delete>", self.on_delete_key)
        self.root.bind("<F5>", lambda e: self.on_capture())
        self.root.bind("<Control-s>", lambda e: (self.on_save(), "break")[1])

    def _build_props(self):
        right = ttk.Frame(self.root, width=360)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        ttk.Label(right, text="  节点属性", style="Title.TLabel").pack(
            anchor="w", padx=8, pady=(10, 2))

        # 属性区可滚动：枝干判定等节点字段较多，超出窗口高度时滚轮下翻
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

        # 字段区单独成容器：打开流程时只重建这里，不销毁下方的固定控件
        self.props_inner = ttk.Frame(inner)
        self.props_inner.pack(fill="x", padx=12)
        prow = ttk.Frame(inner)
        prow.pack(fill="x", padx=10, pady=6)
        self._flat_btn(prow, "↑ 上移", lambda: self.move_node(-1), padx=8).pack(side="left", padx=2)
        self._flat_btn(prow, "↓ 下移", lambda: self.move_node(1), padx=8).pack(side="left", padx=2)
        self._flat_btn(prow, "⇥ 挪到侧列", lambda: self.align_node(), padx=8,
                       font=FONT_SM).pack(side="left", padx=2)
        self._flat_btn(prow, "✖ 删除", self.delete_selected, padx=8,
                       bg="#5a2733", fg="#ffc9d2", hover="#74323f",
                       font=FONT_SM).pack(side="left", padx=(12, 0))

        ttk.Separator(inner).pack(fill="x", pady=6, padx=8)
        ttk.Label(inner, text="  分支出口（画布拖端口或下拉改接）",
                  style="Title.TLabel").pack(anchor="w", padx=8)
        brow = ttk.Frame(inner)
        brow.pack(fill="x", padx=12, pady=4)
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
        self.branch_hint = ttk.Label(inner, text="", style="Dim.TLabel",
                                     justify="left", wraplength=300)
        self.branch_hint.pack(anchor="w", padx=12, pady=(2, 0))

        # 底部日志（固定，不随属性区滚动）
        ttk.Separator(right).pack(fill="x", pady=8, padx=8)
        ttk.Label(right, text="  日志", style="Title.TLabel").pack(anchor="w", padx=8)
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
        self.status_var = tk.StringVar(value="就绪 · F5 抓帧 ｜ 拖动节点排序 ｜ 拖分支端口连线 ｜ Delete 删除选中节点")
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
            ch[i], ch[j] = ch[j], ch[i]
            self.build_prop_panel()
            self.redraw()

    def align_node(self):
        nid = self.sel
        if not nid:
            return
        self.flow["nodes"][nid]["x"] = self._side_col_x()
        self.redraw()

    def tidy_layout(self):
        x = self._chain_col_x()
        y = 50
        for nid in self.flow["chain"]:
            nd = self.flow["nodes"][nid]
            nd["x"] = x
            nd["y"] = y
            y += CARD_H + 30
        self.redraw()

    def _chain_col_x(self):
        fw = self.bg_disp[2] if self.bg_disp else 750
        return fw + 130

    def _side_col_x(self):
        return self._chain_col_x() + CARD_W + 100

    def _scroll_to(self, y):
        sr = self.canvas.cget("scrollregion").split()
        h = float(sr[3]) if len(sr) == 4 else 2000
        self.canvas.yview_moveto(max(0.0, (y - 120) / max(1.0, h)))

    def _on_mousewheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

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
        # 背景帧
        if self.bg_pil is not None and self.show_bg.get():
            disp_h = (FRAME_DISP_H if self.bg_pil.height >= self.bg_pil.width
                      else FRAME_DISP_H_LS)
            scale = disp_h / self.bg_pil.height
            dw = int(self.bg_pil.width * scale)
            self.bg_disp = (ox, oy, dw, disp_h)
            self.bg_photo = ImageTk.PhotoImage(
                self.bg_pil.resize((dw, disp_h), Image.LANCZOS))
            _round_rect(c, ox - 2, oy - 2, ox + dw + 2, oy + disp_h + 2, 8,
                        fill=THEME["card_line"], outline="")
            c.create_image(ox, oy, anchor="nw", image=self.bg_photo)
            c.create_text(ox + 10, oy + 10, anchor="nw", fill="#cfd6e6",
                          font=FONT_SM,
                          text=f" 参考帧 {self.frame_wh[0]}×{self.frame_wh[1]} ")
            self._draw_overlays()
        else:
            self.bg_disp = None
        # 主链连线（按 chain 顺序纵向连接）：与 build_pipeline 共用 linear_successor。
        # 生成时被截断的出口（枝干无直落出口 / 分支内容叶 / 收口节点）不画实线箭头，
        # 改画红色虚线 + ⛔ 标记 —— 画布所见必须等于生成结果。
        ch = self.flow["chain"]
        for i in range(len(ch) - 1):
            cur, nxt_id = ch[i], ch[i + 1]
            a, b = self.flow["nodes"][cur], self.flow["nodes"][nxt_id]
            x1, y1 = a["x"] + CARD_W / 2, a["y"] + CARD_H
            x2, y2 = b["x"] + CARD_W / 2, b["y"]
            if linear_successor(self.flow, cur) == nxt_id:
                mid = (y1 + y2) / 2
                c.create_line(x1, y1, x1, mid, x2, mid, x2, y2 - 2, smooth=True,
                              width=2, arrow=tk.LAST, fill=THEME["arrow"],
                              arrowshape=ARROW_SHAPE, splinesteps=24)
            else:
                by = a["y"] + (self._sw_h(a) if a["type"] == "switch" else CARD_H)
                max_x = max(max_x, self._draw_cut_off(
                    a["x"] + CARD_W / 2, by, y2, _suppress_reason(self.flow, cur)))
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
                                      font=FONT_SM, text=txt)
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
                                      font=FONT_SM, text="→结束")
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
                                  font=FONT_SM, text="→结束")
                max_x = max(max_x, sx + 190)
        # 节点卡片
        for nid in ch:
            self._draw_node(nid)
        self._draw_branch_labels()
        if ch:
            first = self.flow["nodes"][ch[0]]
            entry_txt = f"▶ 入口 VF_{self.flow['name']}"
            bw = 40 + 12 * len(entry_txt)
            _round_rect(c, first["x"] + 4, first["y"] - 30, first["x"] + bw,
                        first["y"] - 8, 9, fill="#3a3418", outline="#d8c86a")
            c.create_text(first["x"] + 4 + bw / 2, first["y"] - 19, fill="#ffe9a0",
                          font=FONT_SM, text=entry_txt)
        # 连线拖动临时线
        if self.wire:
            sx, sy = self._port_pos(self.flow["nodes"][self.wire["from"]], self.wire["port"])
            c.create_line(sx, sy, self.wire["mx"], self.wire["my"],
                          fill="#e8d44d", width=2, arrow=tk.LAST,
                          arrowshape=ARROW_SHAPE)
        c.config(scrollregion=(0, 0, max_x, max_y))

    def _draw_node(self, nid):
        c = self.canvas
        nd = self.flow["nodes"][nid]
        spec = NODE_TYPES[nd["type"]]
        x, y = nd["x"], nd["y"]
        h = self._sw_h(nd) if nd["type"] == "switch" else CARD_H
        x1, y1 = x + CARD_W, y + h
        selected = (nid == self.sel)
        tags = ("node", f"node:{nid}")
        # 阴影
        _round_rect(c, x + 3, y + 5, x1 + 3, y1 + 5, 12,
                    fill=THEME["shadow"], outline="")
        # 主体
        _round_rect(c, x, y, x1, y1, 12, fill=THEME["card"],
                    outline=THEME["sel"] if selected else THEME["card_line"],
                    width=2 if selected else 1, tags=tags)
        # 左侧类型色条
        _round_rect(c, x + 3, y + 5, x + 9, y1 - 5, 3,
                    fill=spec["color"], outline="", tags=tags)
        idx = self.flow["chain"].index(nid) + 1
        c.create_text(x + 20, y + 7, anchor="nw", fill="white", font=FONT_B,
                      text=f"{idx}. {nd.get('title', spec['label'])}",
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
                c.create_text(x + 16, cy, anchor="w", font=FONT_SM,
                              fill=THEME["text"], text=line, tags=tags)
                c.create_text(x + CARD_W - 12, cy, anchor="e", font=FONT_SM,
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
            c.create_text(x + 16, my, anchor="w", font=FONT_SM,
                          fill=THEME["err"], text="全部未中 ⤷", tags=tags)
            c.create_oval(mx - 9, my - 9, mx + 9, my + 9, fill="#2e1c1e", outline="")
            c.create_oval(mx - 5, my - 5, mx + 5, my + 5, fill=THEME["err"],
                          outline="#ffffff", width=1,
                          tags=("port", f"port:{nid}:miss"))
        else:
            c.create_text(x + 20, y + 32, anchor="nw", fill=THEME["text_dim"],
                          font=FONT_SM, text=spec["summary"](nd["props"])[:12], tags=tags)
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

    def _port_pos(self, nd, port):
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
        c.create_text(bx + tw / 2, tip, fill="#ffb3b3", font=FONT_SM, text=txt)
        return bx + tw

    def _draw_branch_labels(self):
        c = self.canvas
        for nid in self.flow["chain"]:
            nd = self.flow["nodes"][nid]
            if nd["type"] == "branch":
                hx, hy = self._port_pos(nd, "hit_next")
                mx, my = self._port_pos(nd, "miss_next")
                c.create_text(hx - 11, hy, anchor="e", fill=THEME["ok"],
                              font=FONT_SM, text="✓命中")
                c.create_text(mx - 11, my, anchor="e", fill=THEME["err"],
                              font=FONT_SM, text="✗未中")
            elif nd["type"] == "switch":
                cands = parse_switch_cands(nd.get("props", {}).get("candidates"))
                for ci, cnd in enumerate(cands):
                    px, py = self._port_pos(nd, f"cand{ci}")
                    _round_rect(c, px - 12, py - 11, px + 38, py + 11, 5,
                                fill="#14161d", outline=THEME["card_line"])
                    c.create_text(px - 2, py, anchor="e", fill=THEME["ok"],
                                  font=FONT_SM, text=f"✓{ci + 1}")
                mx, my = self._port_pos(nd, "miss")
                _round_rect(c, mx - 12, my - 11, mx + 44, my + 11, 5,
                            fill="#14161d", outline=THEME["card_line"])
                c.create_text(mx - 2, my, anchor="e", fill=THEME["err"],
                              font=FONT_SM, text="✗全未中")

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
                              fill=THEME["warn"], font=FONT_SM, text="ROI")
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

    # ---------- 画布交互 ----------

    def _hit_test(self, cx, cy):
        """返回 ("port", nid, port) / ("node", nid) / None"""
        for it in reversed(self.canvas.find_overlapping(cx - 2, cy - 2, cx + 2, cy + 2)):
            for tag in self.canvas.gettags(it):
                if tag.startswith("port:"):
                    _, nid, port = tag.split(":")
                    return ("port", nid, port)
                if tag.startswith("node:"):
                    return ("node", tag.split(":")[1])
        return None

    def _canvas_to_frame(self, cx, cy):
        """画布坐标 → 帧原图坐标；不在帧内返回 None"""
        if not self.bg_disp:
            return None
        ox, oy, dw, dh = self.bg_disp
        if not (ox <= cx <= ox + dw and oy <= cy <= oy + dh):
            return None
        W, H = self.frame_wh
        return (int((cx - ox) * W / dw), int((cy - oy) * H / dh))

    def on_down(self, e):
        cx = self.canvas.canvasx(e.x)
        cy = self.canvas.canvasy(e.y)
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
        self.drag = {"id": nid, "dx": cx - nd["x"], "dy": cy - nd["y"]}
        self.redraw()

    def on_motion(self, e):
        cx = self.canvas.canvasx(e.x)
        cy = self.canvas.canvasy(e.y)
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
        """连线写回：branch 直接写节点字段；switch 写候选 next / 全部未中 miss_next"""
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
        else:
            src[port] = tgt

    def on_up(self, _e):
        if self.wire:
            hit = self._hit_test(self.wire["mx"], self.wire["my"])
            src = self.flow["nodes"][self.wire["from"]]
            port = self.wire["port"]
            if hit and hit[0] == "node" and hit[1] != self.wire["from"]:
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
        for key, label, kind in spec["fields"]:
            if kind == "switch_list":
                # 候选编辑器较宽：标签放到上方，编辑器占整行
                ttk.Label(self.props_inner, text=label, style="Dim.TLabel").grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=2)
                row += 1
                var = self._make_var(nd["props"], key, kind)
                self._make_widget(var, kind, key).grid(row=row, column=0,
                                                       columnspan=2, sticky="we",
                                                       pady=2)
                self.prop_widgets[key] = var
                row += 1
                continue
            ttk.Label(self.props_inner, text=label, style="Dim.TLabel").grid(
                row=row, column=0, sticky="w", pady=2)
            var = self._make_var(nd["props"], key, kind)
            self._make_widget(var, kind, key).grid(row=row, column=1, sticky="we",
                                                   padx=(8, 0), pady=2)
            self.prop_widgets[key] = var
            row += 1
        self.props_inner.columnconfigure(1, weight=1)
        self._sync_branch_ui()

    def _make_var(self, props, key, kind):
        v = props.get(key)
        if kind == "bool":
            var = tk.BooleanVar(value=bool(v))
        elif kind == "switch_list":
            var = tk.StringVar(value=str(len(v)) if isinstance(v, list) else "0")
        else:
            var = tk.StringVar(value="" if v is None else str(v))
        tid = var.trace_add("write", lambda *_: self._prop_changed(key, var, kind))
        self._var_traces.append((var, tid))
        return var

    def _make_widget(self, var, kind, key):
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
                                values=COMMON_NODES, width=22, state="readonly",
                                font=FONT_SM)
        if kind == "bool":
            return ttk.Checkbutton(self.props_inner, variable=var, text="")
        if kind == "float":
            return ttk.Spinbox(self.props_inner, textvariable=var,
                               from_=0.3, to=0.99, increment=0.05, width=10,
                               font=FONT_SM)
        if kind in ("pick", "pick2"):
            fr = ttk.Frame(self.props_inner)
            ttk.Entry(fr, textvariable=var, width=8, font=FONT_SM).pack(side="left", ipady=2)
            self._flat_btn(fr, "✛ 取点", lambda: self._start_pick(key),
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
                lb.insert("end", f"{i + 1}. {disp} · {c.get('timeout', 3000)}ms")
            var.set(str(len(cands)))   # 触发面板重绘钩子

        row = ttk.Frame(wrap)
        row.pack(fill="x", padx=(8, 0), pady=(6, 0))
        e_t = ttk.Entry(row, font=FONT_SM, width=17)
        e_t.pack(side="left")
        e_d = ttk.Entry(row, width=6, font=FONT_SM)
        e_d.insert(0, "3000")
        e_d.pack(side="left", padx=4)

        def _fill_sel(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            c = _cands()[sel[0]]
            e_t.delete(0, "end")
            e_t.insert(0, c.get("t", ""))
            e_d.delete(0, "end")
            e_d.insert(0, str(c.get("timeout", 3000)))

        def on_add():
            t = e_t.get().strip()
            if not t:
                return
            try:
                timeout = max(500, int(e_d.get() or 3000))
            except ValueError:
                timeout = 3000
            _cands().append({"t": t, "timeout": timeout})
            e_t.delete(0, "end")
            e_d.delete(0, "end")
            e_d.insert(0, "3000")
            refresh()

        def on_update():
            sel = lb.curselection()
            if not sel:
                return
            t = e_t.get().strip()
            if not t:
                return
            try:
                timeout = max(500, int(e_d.get() or 3000))
            except ValueError:
                timeout = 3000
            _cands()[sel[0]] = {"t": t, "timeout": timeout}
            refresh()

        def on_del():
            sel = lb.curselection()
            if not sel:
                return
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
        if nd["type"] not in ("branch", "switch"):
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
            if nd["type"] == "switch" and port == "miss_next":
                nd.setdefault("props", {})["miss_next"] = target
            else:
                nd[port] = target
        else:
            if nd["type"] == "switch" and port == "miss_next":
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
        self.root.bind("<Escape>", self._cancel_pick)

    def _cancel_pick(self, _e):
        self.pick_target = None
        self.status("已取消取点")

    def _apply_pick(self, pt):
        key = self.pick_target
        x, y = pt
        props = self.flow["nodes"][self.sel]["props"]
        pairs = {"x": ("x", "y"), "x1": ("x1", "y1"), "x2": ("x2", "y2")}
        ka, kb = pairs[key]
        props[ka], props[kb] = x, y
        self.pick_target = None
        self.status(f"已取坐标 ({x},{y})")
        self.build_prop_panel()
        self.redraw()

    # ---------- 校验/生成/同步 ----------

    def _collect_issues(self):
        errs, warns = validate_flow(self.flow, self.frame_wh)
        for e in errs:
            self.log("✗ " + e, "err")
        for w in warns:
            self.log("⚠ " + w, "warn")
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
