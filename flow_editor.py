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
import copy
import json
import glob
import time
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
GRID = 46                   # 画布网格步长（px）：拖动吸附与网格线都用它，保证同一套对齐
CARD_MARGIN = 5 * GRID      # 画布滚动区在内容之外至少留 5 格（空地方便放/拖节点）
# 流程文件格式版本。3 =【分支】命中出口只认显式连线（不再自动走链上下一个）；
# 老文件（没有这个字段）在 normalize_flow 里会被补成显式连线，存盘时打上 3。
SCHEMA_VERSION = 4
TIDY_X = GRID               # 「整理布局」时主链的 x（贴左边一格，保证点完就能看见）


def regex_error(text):
    """把文本当正则编译，合法返回 None，否则返回引擎那句报错。
    ★ 引擎把 OCR 的 expected（以及 replace 的键）当【正则】编译，用的 boost::wregex
    （ECMAScript 语法）。一个非法表达式（典型：漏填的占位符「?」）会让
    PipelineChecker::check_all_regex 失败 → PipelineResMgr 拒绝加载整个 pipeline
    目录 → 手机上【所有任务】都跑不起来，不只是出问题这一个。所以要在本地拦下。"""
    try:
        re.compile(text)
    except re.error as ex:
        return str(ex)
    return None
# 枝干各候选分支的配色：球与它的连线同色，一眼能看出"哪个球连到哪"。
# 球上的数字只表示【判定顺序】（从左到右依次判定），与画布上下位置无关。
BRANCH_COLORS = ["#58c470", "#5b8cff", "#e8a33d", "#c884e8", "#3fc9c9", "#e0d24d"]
# 【输入】参数注入线/小球的颜色：它只表示"参数注入到哪个节点"，与流程顺序无关，
# 所以画法与流程连线区分开（虚线 + 这个专属色）。
INJECT_COLOR = "#c9a0ff"
# 取点字段的说明（属性面板、帧窗口、状态栏共用一份，免得几处说法不一致）
PICK_LABELS = {"x": "点击坐标", "x1": "滑动起点", "x2": "滑动终点"}
# 各类出口/连线端口的中文说法（日志里用，免得只说 hit_next 这种内部名）
PORT_LABELS = {"hit_next": "✓命中", "miss_next": "✗未中", "miss": "✗全未中",
               "inject": "参数注入", "body_end": "循环体末尾", "new": "新建候选",
               "next": "下一个"}


def port_label(port):
    if str(port).startswith("cand"):
        try:
            return f"候选{int(port[4:]) + 1}"
        except ValueError:
            return str(port)
    return PORT_LABELS.get(port, str(port))


def branch_color(i):
    return BRANCH_COLORS[i % len(BRANCH_COLORS)]

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
                "请改用『分支』节点（它有 ✓/✗ 两个出口）。",
    "switch": "候选按从上到下的顺序判定，命中即走该候选连到的节点，内容跑完枝干就结束；"
              "全部未中走「✗全未中」出口。★ 候选判什么 = 它连到的【分支】判什么："
              "模板图/OCR 文字只在分支里配（枝干这里不重复配）。",
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
        return ("已填 OCR 文本 → 本节点改用 OCR 判定：上面的模板图组与阈值已置灰、"
                "不生效。想改用模板图，把「OCR文本」清空即可")
    return NODE_HINTS.get(ntype)


def tpl_summary(p):
    """节点卡片摘要：模板多候选显示 '首个 +N'，单值直接显示"""
    parts = split_tpls(p.get("template", ""))
    if len(parts) > 1:
        return f"{parts[0]} +{len(parts) - 1}"
    return parts[0] if parts else "?"


def parse_switch_cands(raw):
    """解析枝干候选列表：raw 为 [{timeout, next, mergeBack}, ...]。

    ★ 候选【不再自带识别目标】（旧字段 t 已废弃、读了也不再用）：一个候选判什么，
      完全取决于它连到的那个分支节点 —— 模板图/OCR 文字只在分支里配一次。
      next = 连到的内容起点节点 id；mergeBack = 命中内容跑完后是否回到主线。
    """
    out = []
    if not raw:
        return out
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            timeout = int(float(c.get("timeout", 3000)))
        except (TypeError, ValueError):
            timeout = 3000
        item = {"timeout": max(500, timeout)}
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


def find_input_for(flow, nid, field):
    """哪个【输入】参数注入到 (节点, 字段) → (那个输入节点的编号, 标题)；没有返回 None。
    用来把"未选择模板图"这类报错说准：字段由参数注入时，节点里仍要填一个默认值，
    否则不带参数单独跑（编辑器直达入口 / 手点任务）就会失败。"""
    for other, nd in (flow.get("nodes") or {}).items():
        if not isinstance(nd, dict) or nd.get("type") != "input":
            continue
        p = nd.get("props") or {}
        if input_field(p) == field and nid in input_targets(nd):
            return node_no(flow, other, 0), nd.get("title", "输入")
    return None


def input_summary(p):
    """卡片摘要：'参数名 → 改哪个字段 ×注入数'（字段只显示中文名，卡片放得下）"""
    opt = str(p.get("option") or "").strip() or "?"
    raw = str(p.get("field") or "").strip()
    n = len(p.get("targets") or []) if isinstance(p, dict) else 0
    tail = f" ×{n}" if n > 1 else ""
    ext = parse_raw_targets(p)
    if ext:                       # 作用在手写管线的节点上：卡片上要说一声
        tail += " →" + "、".join(ext[:2]) + ("…" if len(ext) > 2 else "")
    return (f"{opt} → {raw.split(' (')[0]}{tail}" if raw else opt)


def pass_summary(p):
    """【通道/跳转】摘要：显示它运行时的名字（手写管线名）和下一步"""
    nm = str((p or {}).get("emit_name") or "").strip()
    return ("运行时名 " + nm) if nm else "纯跳转（按本流程编号命名）"


def pick_summary(p):
    """【选择】卡片摘要：'参数名（N 个选项[ · 注入 M 个节点]）'。

    选项数只数【有名字】的（没名字的进不了 interface.json，卡片上也不该显得配好了）；
    「注入 M 个节点」= 右侧小球连到的节点数（`props.targets`）—— 一条线都没拉、
    选项又只写了字段和值时，那一项生成不出来，摘要里能看到"注入 0 个节点"。"""
    p = p if isinstance(p, dict) else {}
    opt = str(p.get("option") or "").strip() or "?"
    rows = [r for r in (p.get("cases") or []) if isinstance(r, dict)]
    named = [r for r in rows if str(r.get("name") or "").strip()]
    base = {str(t or "").strip() for t in (p.get("targets") or [])}
    base.discard("")
    row_nodes = {str(r.get("node") or "").strip() for r in rows}
    row_nodes.discard("")
    n = len(base | row_nodes)
    tail = f" · 注入 {n} 个节点" if n else ""
    return f"{opt}（{len(named)} 个选项{tail}）"


# 【输入】参数能覆盖的字段：下拉里显示成"模板图 (template)"这种带中文说明的写法
# （数据里存的就是它，流程文件因此自解释；input_field() 负责抽出协议字段名）。
# 注意两点：① pipeline_override 是自由的，覆盖目标节点"自己不读"的字段不会让引擎报错，
# 只是白填 —— 所以那种情况只给警告；② 字段名写错则一定是问题，直接报错。
INPUT_FIELD_LABELS = (
    ("模板图", "template"),
    ("OCR文字", "expected"),
    ("命中次数", "repeat"),
    ("命中间隔ms", "repeat_delay"),
    ("阈值", "threshold"),
    ("识别区域ROI", "roi"),
    ("等待超时ms", "timeout"),
    ("识别间隔ms", "rate_limit"),
    ("动作前延时ms", "pre_delay"),
    ("动作后延时ms", "post_delay"),
    ("最多命中次数", "max_hit"),
    ("结果排序", "order_by"),
    ("命中第几个", "index"),
)
INPUT_FIELDS = tuple(f"{lab} ({name})" for lab, name in INPUT_FIELD_LABELS)
INPUT_FIELD_NAMES = tuple(name for _lab, name in INPUT_FIELD_LABELS)


def input_field(p):
    """「改它的哪个字段」→ 协议里的字段名（下拉里那种"模板图 (template)"写法也能取）。
    手工直接写成 template 这种纯字段名同样认。"""
    raw = str((p or {}).get("field") or "template").strip()
    m = re.search(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*$", raw)
    return m.group(1) if m else raw


# 【选择】节点的每个选项能覆盖的字段：除了【输入】能覆盖的那些，还多两个
# "按次数切分支"常用的字段（征集次数就是这么写的：选项 1 → 征集段2.next，
# 选项 2~4 → ZJ_JiaHao2.repeat）。
PICK_FIELD_LABELS = (("下一个出口", "next"), ("启用", "enabled")) + INPUT_FIELD_LABELS
PICK_FIELDS = tuple(f"{lab} ({name})" for lab, name in PICK_FIELD_LABELS)
PICK_FIELD_NAMES = tuple(name for _lab, name in PICK_FIELD_LABELS)
_PICK_FIELD_DISPLAY = {name: f"{lab} ({name})" for lab, name in PICK_FIELD_LABELS}


def pick_field_display(name):
    """协议字段名 → 下拉里显示的写法（认不出来就原样回显，方便手写奇特字段）"""
    return _PICK_FIELD_DISPLAY.get(str(name or "").strip(), str(name or "").strip())


def parse_raw_targets(p):
    """【输入】节点上「手写管线的节点名」列表 —— 老流程（cdzb.json 等手写管线）的参数
    要作用在【外面】的节点上（`查2_Hit`、`升好_礼物`…），编辑器流程里没有这些名字。
    逗号分隔，允许 #编号（指本流程画布上的节点）。"""
    raw = str((p or {}).get("raw") or "")
    return [s.strip() for s in re.split(r"[,，]", raw) if s.strip()]


def emit_name_of(flow, nid):
    """节点在生成物里的名字：默认 jname()，【通道/跳转】可以用 props.emit_name 指定
    （老流程要保留手写管线的节点名，参数覆盖才对得上）。"""
    nd = (flow.get("nodes") or {}).get(nid) or {}
    if nd.get("type") == "pass":
        nm = str((nd.get("props") or {}).get("emit_name") or "").strip()
        if nm:
            return nm
    return jname(flow, nid) if nid in (flow.get("chain") or []) else ""


def target_name_of(flow, token):
    """选项/参数的目标写的是什么 → 生成物里的节点名。
    ① 画布节点 id；② `#编号`（按固定编号反查，**不是**链序下标）；
    ③ 都不是就原样当手写管线的节点名用。"""
    token = str(token or "").strip()
    nodes = flow.get("nodes") or {}
    if token in nodes:
        return emit_name_of(flow, token) or jname(flow, token)
    m = re.match(r"^#(\d+)$", token)
    if m:
        want = int(m.group(1))
        for nid in nodes:
            if node_no(flow, nid) == want:
                return jname(flow, nid)
    return token


def case_value(field, raw, flow=None):
    """选项里那一格"值"文本 → 写进 pipeline_override 的值。
    `next` 是节点名列表（多个用逗号分隔）；`[..]` 开头按 JSON 解析；纯数字认成 int；
    其余当字符串。

    ★ `next` 的每一段再过一遍 target_name_of()：那里写 `#12`（或画布节点的标题）也能
      落到正确的**节点名**上 —— 生成物里 next 的值必须是节点名，写别的名字会让引擎
      因为"引用不存在的节点"拒绝整包（铁律 #1）。老流程写的手写管线节点名认不出来，
      target_name_of 会原样返回，不受影响。"""
    text = str(raw or "").strip()
    if text.startswith("["):
        try:
            return json.loads(text)
        except ValueError:
            pass
    if field == "next":
        items = [s.strip() for s in re.split(r"[,，]", text) if s.strip()]
        if flow is None:
            return items
        return [target_name_of(flow, v) for v in items]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text in ("true", "false"):
        return text == "true"
    return text


def pick_cases(flow, nd):
    """【选择】节点的选项 → interface.json 的 cases（形状与 App 端 TaskPack 逐字段对齐）。

    目标节点来自两处，按行优先：
      · 这一行自己写了「目标节点」（老流程的写法：征集次数的选项 1 只作用在「征集段2」上）
        → 就用它；
      · 没写 → 用卡片右侧小球注入到的**全部**节点（一条注入线都不拉就生成不出这一项）。
    生成出来的每个选项都要有「选项名」和「字段」，否则整行丢掉（App 的下拉里就没有这一项）。"""
    out = []
    nodes = flow.get("nodes") or {}
    base = [t for t in inject_targets_of(nd) if t in nodes]
    for row in ((nd or {}).get("props") or {}).get("cases") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        node = str(row.get("node") or "").strip()
        field = input_field({"field": row.get("field")})
        if not name or not field:
            continue
        targets = [node] if node else list(base)
        ov = {}
        for t in targets:
            nm = target_name_of(flow, t)
            if nm and nm not in ov:
                ov[nm] = {field: case_value(field, row.get("value"), flow)}
        if ov:
            out.append({"name": name, "pipeline_override": ov})
    return out


def case_brief(flow, row):
    """选项在列表里的一行摘要（只读回显用）。

    空着的格子要看得见（「(未命名)」「(空)」）—— 选项名没填的这一项不会写进
    interface.json。目标节点只在**这一行自己写了**的时候才显示（`@节点名`）：
    没写就是"作用在注入线连到的那些节点上"，不写出来反而更清楚。"""
    row = row if isinstance(row, dict) else {}
    name = str(row.get("name") or "").strip() or "(未命名)"
    field = input_field({"field": row.get("field")})
    text = str(row.get("value") or "").strip()
    if not text:
        val = "(空)"
    elif field == "next" and not text.startswith("["):
        val = "[" + text + "]"
    else:
        val = text
    node = str(row.get("node") or "").strip()
    at = f" @{target_name_of(flow, node) or node}" if node else ""
    return f"{name}{at}: {field} = {val}"


def cand_target(flow, c):
    """候选连到的内容起点：返回 (节点id, 节点dict)；节点不存在时 dict 为 None。"""
    tgt = c.get("next")
    nd = (flow.get("nodes") or {}).get(tgt) if tgt else None
    return tgt, (nd if isinstance(nd, dict) else None)


def branch_hit_block(flow, nid):
    """【分支】节点的判定块：recognition + 期望文本/模板图(+threshold) + action + roi。
    branch 自己生成的 *_Hit 用它，枝干候选的判定也用它 —— 条件只有一处定义，
    两处不可能算出不同结论（以前枝干候选另配一份，还会漏掉 threshold/roi）。
    条件还没配好（既没填 OCR 文字也没选模板图）返回 None。"""
    nd = (flow.get("nodes") or {}).get(nid) or {}
    if nd.get("type") != "branch":
        return None
    p = nd.get("props") or {}
    texts = split_ocr_texts(p.get("ocr_text", ""))
    tpls = split_tpls(p.get("template", ""))
    if texts:
        hd = {"recognition": "OCR", "expected": texts}
    elif tpls:
        hd = {"recognition": "TemplateMatch", "template": tpl_out(p["template"]),
              "threshold": float(p.get("threshold", 0.7))}
    else:
        return None
    hd["action"] = "DoNothing"
    roi = parse_roi(p.get("roi", ""))
    if roi:
        hd["roi"] = roi
    return hd


def switch_cand_block(flow, c):
    """枝干候选的判定块 = 它连到的分支节点的判定块 + 命中出口。
    连不到分支、或那个分支还没配条件 → None（校验/生成会给对症提示）。"""
    tgt, tnd = cand_target(flow, c)
    if tnd is None or tnd.get("type") != "branch":
        return None
    hd = branch_hit_block(flow, tgt)
    if hd is None:
        return None
    hd["next"] = [jname(flow, tgt)]
    return hd


def cand_cond_short(flow, c):
    """候选判定条件的极简说法（卡片/列表共用）：模板 x.png / OCR 文字 / 问题提示。"""
    tgt, tnd = cand_target(flow, c)
    if not tgt:
        return "未连线"
    if tnd is None:
        return "节点已删除"
    if tnd.get("type") != "branch":
        return "连到的不是分支"
    blk = branch_hit_block(flow, tgt)
    if blk is None:
        return "分支还没配判定条件"
    if blk["recognition"] == "OCR":
        return "OCR " + "、".join(blk["expected"])
    tpl = blk["template"]
    return "模板 " + (tpl if isinstance(tpl, str) else "、".join(tpl))


def cand_brief(flow, c):
    """属性面板列表里那一行：连到哪个节点 + 用它什么条件判定（只读展示）。"""
    tgt, tnd = cand_target(flow, c)
    head = f"#{node_no(flow, tgt)}「{tnd.get('title', tgt)}」" if tnd else (
        f"#{tgt}" if tgt else "")
    cond = cand_cond_short(flow, c)
    return f"{head} {cond}" if head else cond


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
        "summary": lambda p: (f"起 ({p.get('x1', 0)},{p.get('y1', 0)})"
                              f"\n终 ({p.get('x2', 0)},{p.get('y2', 0)})"),
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
        "label": "分支", "icon": "Ж", "color": "#c08a3e", "light": "#f0c68a",
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
    "pass": {
        "icon": "⇢", "label": "通道/跳转", "color": "#37514f", "light": "#9ed4cd",
        "summary": pass_summary,
        # 不做识别、不做动作的"过路"节点：只为给流程留一个可连线的位置。
        # ★ 它存在的意义是「运行时的名字」：生成名默认为本流程编号（VF_x_NN），
        #   但可以写成**手写管线里的节点名**（如 征集段2）—— 老流程的参数
        #   （interface.json 里 `征集段2.next` 这种覆盖）才有一个能连上去的卡片。
        #   生成物就是 {"next": [...]}（引擎默认 DirectHit + DoNothing），
        #   与手写管线的写法逐字段相同。
        "fields": [
            ("emit_name", "运行时名(手写管线里的节点名，空=用本流程编号)", "str_opt"),
        ],
        "defaults": {"emit_name": ""},
    },
    "input": {
        "label": "输入", "icon": "⌨", "color": "#8a6d3b", "light": "#e8cf9a",
        "summary": input_summary,
        # 这个节点不做事，只在任务的【参数】里声明一个让用户在 App 任务编辑栏里填的
        # 值，并说明它覆盖到哪个节点的哪个字段（interface.json 的 option.pipeline_override）。
        # 对应 ProjectInterface v2：type=input + inputs + pipeline_override，
        # 值里 {变量名} 会被 App 替换（如 template: "{角色}.png"）。
        "fields": [
            ("option", "参数名(进 interface.json / App 里显示)", "str"),
            ("var", "变量名(值里写 {变量名}，空=同参数名)", "str_opt"),
            ("var_label", "该参数的字段标题(App 里 '参数名 · 这里')", "str_opt"),
            ("kind", "类型", "choice", ("文本", "整数")),
            ("default", "默认值", "str_opt"),
            ("desc", "说明(App 里的提示)", "str_opt"),
            ("targets", "已注入到(拖右侧小球增删)", "inject_list"),
            ("raw", "手写管线的节点名(逗号分隔，老流程用)", "str_opt"),
            ("field", "改它的哪个字段", "choice", INPUT_FIELDS),
            ("value", "值(空=自动：模板→{变量}.png)", "str_opt"),
            ("verify", "整数校验正则(空=不校验)", "str_opt"),
            ("pattern_msg", "校验失败提示", "str_opt"),
        ],
        "defaults": {"option": "", "var": "", "var_label": "", "kind": "文本",
                     "default": "", "desc": "", "target": "", "raw": "",
                     "field": "template",
                     "value": "", "verify": "", "pattern_msg": ""},
    },
    "pick": {
        "label": "选择(下拉)", "icon": "☰", "color": "#4a4a7a", "light": "#a8b0e0",
        "summary": pick_summary,
        # 同样是"参数声明"，但值是【从几个选项里挑一个】，每个选项覆盖不同字段
        # （对应 PI v2 的 type=select + cases）：征集次数就是这么写的 ——
        # 选 1 时把「征集段2」的下一个换成 ZJ_DuiGou2，选 2~4 时改「ZJ_JiaHao2」的重复次数。
        # ★ 作用在哪个节点上由**注入小球**决定（props.targets，与【输入】同一套）；
        #   老流程把目标写死在单个选项里（row["node"]），那种写法继续支持。
        "fields": [
            ("option", "参数名(进 interface.json / App 里显示)", "str"),
            ("var_label", "该参数的字段标题", "str_opt"),
            ("default", "默认选项名(空=第一个)", "str_opt"),
            ("desc", "说明(App 里的提示)", "str_opt"),
            ("targets", "已注入到(拖右侧小球增删)", "inject_list"),
            ("cases", "选项(名称 / 字段 / 值)", "cases_list"),
        ],
        "defaults": {"option": "", "var_label": "", "default": "", "desc": "",
                     "targets": [], "cases": []},
    },
    "switch": {
        "label": "枝干判定(多路)", "icon": "☰", "color": "#7a5cb0", "light": "#c0a8f0",
        "summary": switch_summary,
        "fields": [
            ("candidates", "候选(每行: 模板名 或 OCR:文字 / 判定ms)", "switch_list"),
        ],
        "defaults": {
            "candidates": [{"timeout": 3000}],
            "miss_next": "",
        },
    },
}

TYPE_ORDER = ["ocr_click", "tpl_click", "tap", "swipe", "wait_tpl", "branch",
              "switch", "loop", "subflow", "common", "startapp", "input", "pick",
              "pass"]

# 所有节点类型共用的可选字段（属性面板在类型专属字段之后追加渲染）。
# notes 只是编辑器便签，不进生成物；enabled/max_hit 见 _put_common_fields。
COMMON_NODE_FIELDS = [
    ("enabled", "启用（✓ 执行 / ✗ 跳过本节点）", "bool_opt"),
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


# 互斥字段的两种处理方式：
#   FIELD_HIDDEN_IF      —— 满足条件就整行收走（连看都看不见，目前没有节点用这条规则）
#   FIELD_OVERRIDDEN_IF  —— 保留显示，但置灰 + 标签注明被谁覆盖
# 后者比前者友好得多：字段"消失"会让人以为功能没了（branch 的模板图组就踩过这个坑 ——
# 填了 OCR 文本后模板图组整行不见，看着就是"选不了模板"）。置灰能让人看见
# "它还在、只是现在被 OCR 覆盖"，也知道清空 OCR 文本就能用回来。
FIELD_HIDDEN_IF = {}
FIELD_OVERRIDDEN_IF = {
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
    "field": "这个参数要覆盖目标节点的哪个字段（App 会在运行时按你填的值替换）：\n"
             "· 模板图 template —— 输入的文字当模板图名。值默认 {变量}.png，\n"
             "  就是「对着文字找同名模板」（装卸装备的『目标角色』）\n"
             "· OCR文字 expected —— 输入的文字当 OCR 识别文字\n"
             "· 命中次数 repeat —— 输入的数字当重复次数（礼物次数、刷冬谷币次数）\n"
             "· 其余（阈值/ROI/超时/间隔/前后延时）是进阶项，一般不用改。\n"
             "选 template 时：作用目标若是【分支】，覆盖会自动落到它的 *_Hit（识别块在那里）。",
    "option": "参数名：写进 interface.json 顶层 option 的键，也是 App 任务编辑栏里\n"
              "显示的标题（例如『目标角色』）。同名参数会覆盖清单里已有的定义。",
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


# 编辑器自己的小偏好（目前只有"删除免确认"）。放在工具目录下、和 recent.txt 同一类
# 本机状态文件（.gitignore 里忽略）。**读失败一律当默认值** —— 这文件坏了不该让
# 编辑器起不来。
PREFS_PATH = os.path.join(TOOLS_DIR, "ui_prefs.json")
DEFAULT_PREFS = {"no_confirm_delete": False}


def load_prefs():
    prefs = dict(DEFAULT_PREFS)
    try:
        with open(PREFS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            prefs.update({k: v for k, v in data.items() if k in DEFAULT_PREFS})
    except Exception:
        pass
    return prefs


def save_prefs(**kw):
    """改一项偏好就写回文件（只认识 DEFAULT_PREFS 里登记过的键）"""
    prefs = load_prefs()
    prefs.update({k: v for k, v in kw.items() if k in DEFAULT_PREFS})
    try:
        with open(PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return prefs


def card_h(nd):
    """节点卡片的【绘制高度】：枝干卡按候选数变高，其余都是 CARD_H。
    必须与画布 _draw_node 用的高度一致，否则判断重叠/留间距都会算错。"""
    if (nd or {}).get("type") == "switch":
        n = len(parse_switch_cands((nd.get("props") or {}).get("candidates")))
        return 64 + 22 * max(n, 1) + 22
    return CARD_H


def overlapping_nodes(flow):
    """画布上互相重叠的节点 id 集合（卡片矩形相交就算子）。空集 = 布局正常。
    老流程（导入进来的那些）常常所有节点同一个坐标，一打开就叠成一坨 —— 用它检出来。"""
    items = []
    for nid, nd in (flow.get("nodes") or {}).items():
        if not isinstance(nd, dict):
            continue
        items.append((nid, _num(nd.get("x"), 0), _num(nd.get("y"), 0), card_h(nd)))
    bad = set()
    for i in range(len(items)):
        n1, x1, y1, h1 = items[i]
        for j in range(i + 1, len(items)):
            n2, x2, y2, h2 = items[j]
            if x1 < x2 + CARD_W and x2 < x1 + CARD_W and y1 < y2 + h2 and y2 < y1 + h1:
                bad.add(n1)
                bad.add(n2)
    return bad


def _field_spec(f):
    """字段声明 → (props键, 标签, 控件类型, 附加参数)。
    三元组是历史写法；第四元可选（例如 choice 的候选列表），故这里统一解包。"""
    return f[0], f[1], f[2], (f[3] if len(f) > 3 else None)


def exit_targets_of(flow, nid):
    """某个节点"出口"指向的所有节点 id（分支 ✓/✗、枝干候选与全部未中、循环体末尾）。"""
    nd = (flow.get("nodes") or {}).get(nid) or {}
    if not isinstance(nd, dict):
        return []
    p = nd.get("props") or {}
    out = []
    if nd.get("type") == "branch":
        out += [nd.get("hit_next"), nd.get("miss_next")]
    elif nd.get("type") == "switch":
        out.append(p.get("miss_next"))
        for c in parse_switch_cands(p.get("candidates")):
            out.append(c.get("next"))
    elif nd.get("type") == "loop":
        out.append(p.get("body_end"))
    return [x for x in out if x]


def _materialize_next(flow):
    """v3 → v4 迁移：把当时由【链序】隐式决定的"下一个"固化成节点上的 next 字段。

    ★ 为什么要这一步：以前"下一个"不看任何显式记录，只看链序里谁排在谁后面 ——
      于是链序一被改动（或者节点被接进来/挪出去），连接就跟着变，凭空多出用户
      没画过的线（"我把 3 接到 4，#4 却自动连到 #11"就是链序给的）。
      v4 起边就是边：nd["next"] 是唯一来源；链序只决定【显示顺序】和"哪些节点参与生成"。
      这里按【当时的链序】逐条补出来，所以生成结果一字不变（golden 基线可证）。"""
    chain = flow.get("chain") or []
    nodes = flow.get("nodes") or {}
    for i, nid in enumerate(chain):
        nd = nodes.get(nid)
        if not isinstance(nd, dict):
            continue
        nxt = chain[i + 1] if i + 1 < len(chain) else None
        t = nd.get("type")
        if nd.get("cut") or t in ("branch", "switch", "input", "common"):
            # 断开标记的意思就是"没有下一个"；分支/枝干没有直落出口；
            # 【输入】是参数声明、收口节点进入后不返回 —— 都不该有链上下一个。
            nxt = None
        nd.pop("cut", None)      # 断开的意思已经由 next=None 表达了，不再需要这个标记
        nd["next"] = nxt


def _pull_exit_targets_into_chain(flow):
    """把"被出口指着、却没接进主链"的节点接进主链（跟在指着它的节点后面）。

    ★ 这是为了让"出口指着谁，谁就在流程里"这条直觉成立：以前这种节点不生成，
      而出口照样指着它的名字 —— 生成物里就是一条指向不存在节点的引用，
      本机引擎实测整包 loaded=False（手机上所有任务都跑不起来）。
    【输入】不在此列：它设计上就离链（参数声明）。"""
    chain = flow.get("chain") or []
    nodes = flow.get("nodes") or {}
    for nid in list(chain):
        for tgt in exit_targets_of(flow, nid):
            if tgt not in nodes or tgt in chain:
                continue
            if (nodes.get(tgt) or {}).get("type") in ("input", "pick"):
                continue
            chain.insert(chain.index(nid) + 1, tgt)
            flow.setdefault("nodes", {})[tgt].pop("cut", None)
    flow["chain"] = chain


def normalize_flow(flow):
    """把旧版本流程文件里存成字符串的数值字段转回 int；顺带做两处结构迁移。

    ★ 【输入】节点统一移出主链：它是"参数声明"，不是流程步骤 —— 不该占一个生成节点
      （以前要在管线里生成一个空步 DoNothing），也不该参与链序执行。
    ★ 它的注入目标从早期的单值 target 升级为 targets 列表（右侧小球可以拉多条线）。
    """
    chain = flow.get("chain") or []
    nodes = flow.get("nodes") or {}
    flow["chain"] = [n for n in chain
                     if (nodes.get(n) or {}).get("type") not in ("input", "pick")]
    for nd in nodes.values():
        if not isinstance(nd, dict) or nd.get("type") != "input":
            continue
        p = nd.setdefault("props", {})
        old = p.pop("target", None)
        if not isinstance(p.get("targets"), list):
            p["targets"] = []
        if old and old not in p["targets"]:
            p["targets"].append(old)
    # 【分支】的命中出口不再"自动走链上下一个"：老流程（文件里还没有 schemaVersion，
    # 说明是这次改动之前存的）里没连线的那些，按【当时的生成结果】补成显式连线 ——
    # 生成物因此一字不变（golden 基线可证）。补完由 save_flow 打上 schemaVersion=3，
    # 之后打开就不再补：新建的分支"没连线"就是没连线，语义才是"命中即结束"。
    if _num(flow.get("schemaVersion"), 1) < 3:
        for nid in list(chain):
            nd = nodes.get(nid) or {}
            if not isinstance(nd, dict) or nd.get("type") != "branch":
                continue
            if nd.get("hit_next"):
                continue
            succ = linear_successor(flow, nid)
            if succ:
                nd["hit_next"] = succ
    # ★ 出口指向"离链"的节点 → 把那个节点接进主链（紧跟指着它的那个节点之后）。
    #   否则生成物里会出现一条指向不存在节点的 next，引擎会拒绝加载【整个任务包】。
    #   出口既然指着它，它在用户心里就是流程的一部分 —— 不该还要他去别处手动接一次。
    #   （【输入】不在此列：它是参数声明，本来就不进链。）
    if _num(flow.get("schemaVersion"), 1) < 4:
        _materialize_next(flow)
    _pull_exit_targets_into_chain(flow)
    # 边指向已不存在的节点 → 当作"到此结束"（否则生成物里会是悬空引用）
    for _nid, _nd in (flow.get("nodes") or {}).items():
        if isinstance(_nd, dict) and "next" in _nd:
            _nx = _nd.get("next")
            if _nx and (_nx not in (flow.get("nodes") or {}) or _nx == _nid):
                _nd["next"] = None
    # ★ 参数节点的注入目标（【输入】/【选择】共用的 props.targets）里指向已删除节点的
    #   死 id 直接摘掉：那是"连过的节点被删掉"留下的残渣，画布上已经没有球可拖、
    #   面板里也没有清它的入口，留着只会让卡片摘要显示"注入 N 个节点"（实际连不上），
    #   生成时还会往一个不存在的节点名上写覆盖。_delete_nodes() 只在删除那一刻清理，
    #   老文件（或早先版本写脏的数据）里仍有残留，所以读文件时统一兜一次。
    _live = flow.get("nodes") or {}
    for _nd in _live.values():
        if not isinstance(_nd, dict) or _nd.get("type") not in ("input", "pick"):
            continue
        _p = _nd.get("props") or {}
        _tg = _p.get("targets")
        if isinstance(_tg, list):
            _p["targets"] = [t for t in _tg if t in _live]
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
        # 节点编号：给没有编号的节点按【当时的链序】补一个（旧文件因此与之前的显示一致），
        # 之后编号就固定了 —— 重排/删除/改接都不会改到别人的号。
        taken = {x.get("num") for x in flow.get("nodes", {}).values()
                 if isinstance(x.get("num"), int)}
        for _i, _nid in enumerate(flow.get("chain", [])):
            _nd = flow["nodes"].get(_nid)
            if not isinstance(_nd, dict) or isinstance(_nd.get("num"), int):
                continue
            want = _i + 1
            if want in taken:
                want = max(taken, default=0) + 1
            _nd["num"] = want
            taken.add(want)
        for _nid, _nd in flow.get("nodes", {}).items():
            if isinstance(_nd, dict) and not isinstance(_nd.get("num"), int):
                _nd["num"] = max(taken, default=0) + 1
                taken.add(_nd["num"])
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
      switch-content-leaf —— 分支内容叶，跑完即止。
      node-cut  —— 手动标记的「此处断开」：不接下一个（流程到此为止）。"""
    nd = flow.get("nodes", {}).get(nid)
    if nd is None:
        return None
    if nd.get("cut"):
        # 断开一个节点时给它的前一个节点打的标记：否则链上"移走中间那个"会让前后
        # 两个节点自动挨上，等于替你连了一条没画过的线（用户明确不要这种自动补位）。
        return "node-cut"
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
    """★ 单一真相源：该节点的"下一个"是哪个节点；None = 到此结束（没有下一个）。

    调用方：build_pipeline() 与 FlowEditor.redraw()。禁止在别处自行推导。
    ★ v4 起这是一条【真实的边】（nd["next"]）：链序只决定显示顺序，不再自动产生连接。
      老文件（还没迁移）回退到"链上后继"，保证生成结果不变。"""
    if _suppress_reason(flow, nid) is not None:
        return None
    nd = (flow.get("nodes") or {}).get(nid) or {}
    if "next" in nd:
        nxt = nd.get("next")
        nodes = flow.get("nodes") or {}
        return nxt if (nxt and nxt in nodes and nxt != nid) else None
    # ★ "回退到链上后继"只对【老文件】成立（v1~v3 的语义就是链序决定下一个，
    #   normalize_flow 里的 _materialize_next 会把它们补成显式 next）。
    #   v4 文件里缺 next 键必须当【到此结束】——无条件回退的话，一个"没写 next 的节点"
    #   （新建时没选中任何节点 → 放在未接入区 → 后来被接进链的那种）会悄悄连到链上后继：
    #   博物研学里链尾是滑动 #22，于是"新建一个节点，画布上总多出一根连到 #22 的线"，
    #   而且生成物里也真多一条 VF_博物研学_36 → VF_博物研学_22 的边（运行时真的会跳过去）。
    if _num(flow.get("schemaVersion"), 1) < 4:
        chain = flow.get("chain", [])
        if nid not in chain:
            return None
        i = chain.index(nid)
        return chain[i + 1] if i + 1 < len(chain) else None
    return None


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
        label = f"#{node_no(flow, nid, i + 1)}「{nd.get('title', '?')}」"
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
                f"#{node_no(flow, nid, i + 1)}「{nd.get('title', '?')}」"
                f"节点名(key) {key!r} 只能包含"
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
            lack.append(f"#{node_no(flow, nid, i + 1)}「{nd.get('title', '?')}」")
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
        no = f"#{node_no(flow, nid, i + 1)}「{nd.get('title', '?')}」"
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
        no = f"#{node_no(flow, nid, i + 1)}「{nd.get('title', '?')}」"
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
            off.append(f"#{node_no(flow, nid, i + 1)}「{nd.get('title', '?')}」")
    if off:
        shown = "、".join(off[:6]) + (" 等" if len(off) > 6 else "")
        issues.append(Issue(
            "warn", "NODE_DISABLED",
            f"有 {len(off)} 个节点被禁用，生成物里会被引擎跳过（不会执行）：{shown}"))


def _check_ocr_regex(flow, issues):
    """OCR 文字在引擎里是【正则】而不是普通字符串，非法就会让整个任务包加载失败。
    这里按生成器的同一套规则（split_ocr_texts / branch_hit_block / _ocr_replace）
    把每个会写进 expected、replace 的文本编译一遍，把问题在本地拦下。"""
    for nid, nd in (flow.get("nodes") or {}).items():
        if not isinstance(nd, dict):
            continue
        t, p = nd.get("type"), nd.get("props") or {}
        if t == "ocr_click":
            texts, repl = _ocr_expected(p), _ocr_replace(p)
        elif t == "branch" and str(p.get("ocr_text", "")).strip():
            texts, repl = split_ocr_texts(p["ocr_text"]), None
        elif t == "switch":
            # 枝干候选的判定块取自它连到的分支，文本也会进 expected，
            # 所以这里按生成物口径再查一遍（分支那边的检查覆盖不到派生值）
            texts, repl = [], None
            for c in parse_switch_cands(p.get("candidates")):
                blk = switch_cand_block(flow, c)
                if blk and blk.get("recognition") == "OCR":
                    texts += list(blk.get("expected") or [])
        else:
            continue
        where = f"#{node_no(flow, nid)}「{nd.get('title', '?')}」"
        for s in texts:
            err = regex_error(s)
            if err:
                issues.append(Issue(
                    "error", "OCR_REGEX",
                    f"{where}的 OCR 文字「{s}」不是合法正则"
                    f"（{err}）；引擎会因此拒绝加载整个任务包，"
                    f"手机上所有任务都跑不了。要么填普通文字，要么改成合法的正则写法",
                    nid))
        for key, _val in (repl or []):
            err = regex_error(key)
            if err:
                issues.append(Issue(
                    "error", "OCR_REGEX",
                    f"{where}的易错字替换「{key}」不是合法正则（{err}）", nid))


def _check_offchain(flow, issues):
    """离链节点：只有【输入】【选择】是设计上就离链的（参数声明，靠注入线/选项建立关系）；
    其它类型离链 = 没接进流程，既不生成也不执行 —— 报个警告，
    免得"点了节点库加了节点却没反应"找不到原因。"""
    ch = set(flow.get("chain") or [])
    nodes = flow.get("nodes") or {}
    orphan = [n for n, nd in nodes.items()
              if isinstance(nd, dict) and nd.get("type") not in ("input", "pick")
              and n not in ch]
    if not orphan:
        return
    shown = "、".join(f"#{node_no(flow, n)}「{nodes[n].get('title', n)}」"
                     for n in orphan[:6])
    issues.append(Issue(
        "warn", "NODE_OFFCHAIN",
        f"有 {len(orphan)} 个节点还没接进流程（不会生成也不会执行）：{shown}"
        f"{' 等' if len(orphan) > 6 else ''} —— 选中它，在右侧「连接」把「下一个」"
        f"选成一个节点即可接进去"))


def _check_offchain_exits(flow, issues):
    """出口指向"还在 nodes 里、但没接进主链"的节点 —— 那是个【不参与生成】的节点。

    这类引用比"指向已删除节点"更隐蔽：删除的节点 tgt 不在 nodes 里，有各自的 *_DEAD 报；
    离链的节点还在，所以以前一路绿灯，但生成物里会留下一条指向不存在节点的 next/on_error。
    本机 MaaFw 引擎实测（Resource.post_bundle）：check_next_list 报
    "Invalid next node name" → check_all_validity failed → 整包 loaded=False，
    也就是手机上所有任务都跑不起来（铁律 1）。所以这里必须报 error，不能只给警告。"""
    ch = set(flow.get("chain") or [])
    nodes = flow.get("nodes") or {}
    for nid, nd in nodes.items():
        if not isinstance(nd, dict) or nid not in ch:
            continue          # 离链节点自己不生成，它的出口不算数（由 NODE_OFFCHAIN 提醒）
        ex = exits_of(flow, nid) or {}
        targets = []
        if ex.get("hit"):
            targets.append(("✓命中出口", ex["hit"]))
        if ex.get("miss"):
            targets.append(("✗未命中出口", ex["miss"]))
        if ex.get("body_end"):
            targets.append(("循环体末尾", ex["body_end"]))
        for ci, c in enumerate(ex.get("candidates") or []):
            if c.get("next"):
                targets.append((f"候选{ci + 1}出口", c["next"]))
        for label, tgt in targets:
            tnd = nodes.get(tgt)
            if tnd is None or tgt in ch:
                continue      # 已删除 / 正常在链上
            issues.append(Issue(
                "error", "EXIT_OFFCHAIN",
                f"#{node_no(flow, nid, 0)}「{nd.get('title', '?')}」的{label}指向"
                f"「#{node_no(flow, tgt, 0)}{tnd.get('title', tgt)}」，"
                f"而它【还没接进流程】（不生成也不执行）：生成出来是一条指向不存在节点的"
                f"引用，引擎会拒绝加载整个任务包，手机上所有任务都跑不起来。"
                f"请把那个节点接回主链（选中它，在右侧「连接」里给「下一个」选一个节点），"
                f"或把这个出口改指别的节点", nid))


def _check_inputs(flow, issues):
    """【输入】节点的校验：参数名、注入目标、字段是否存在于目标、校验正则、重复。
    这些参数最终会写进 interface.json 给 App 的任务编辑栏用，填错等于参数没用。
    ★ 输入节点是离链的（声明，不是流程步骤），所以这里遍历【全部节点】而不是链。"""
    nodes = flow.get("nodes") or {}
    seen_opt = {}
    seen_slot = {}
    items = sorted((node_no(flow, nid, 0), nid, nd)
                   for nid, nd in nodes.items()
                   if isinstance(nd, dict) and nd.get("type") == "input")
    for _no, nid, nd in items:
        p = nd.get("props") or {}
        no = f"#{node_no(flow, nid, 0)}"
        where = f"{no}「{nd.get('title', '输入')}」"
        name = str(p.get("option") or "").strip()
        if not name:
            issues.append(Issue("error", "IN_NAME_EMPTY",
                                f"{where}还没填参数名（App 的【参数】里显示这个名字）", nid))
        elif name in seen_opt:
            # 同名是允许的（一个参数覆盖多个节点/字段，写进 interface.json 时合并）——
            # 但字段定义不一致就是真问题：App 只会拿到第一份 inputs 定义。
            prev = seen_opt[name]
            if (str(p.get("var") or "").strip() or name,
                    str(p.get("kind") or "文本").strip()) != prev[1]:
                issues.append(Issue(
                    "warn", "IN_NAME_MIX",
                    f"{where}和 #{prev[0]} 用了同一个参数名「{name}」但字段定义不一样"
                    f"（变量名/类型），合并后只有 #{prev[0]} 那份生效", nid))
        else:
            seen_opt[name] = (node_no(flow, nid, 0),
                              (str(p.get("var") or "").strip() or name,
                               str(p.get("kind") or "文本").strip()))
        tgts = list(input_targets(nd))
        # ★ "手写管线的节点名"（老流程的 target_raw）也算连上了：那批流程的注入目标是
        #   手写管线里的节点（查2_Hit / 升好_礼物…），编辑器流程里没有它们，
        #   只能按名字写死 —— 这种情况不该报"还没连到要注入的节点"。
        if not tgts and not parse_raw_targets(p):
            issues.append(Issue(
                "error", "IN_NO_TARGET",
                f"{where}还没连到要注入的节点：把卡片右侧的小球拖到目标节点上，"
                f"或在「手写管线的节点名」里直接写节点名"
                f"（注入线只表示参数注入，不代表流程顺序）", nid))
        field = input_field(p)
        for tgt in tgts:
            tnd = nodes.get(tgt)
            if not isinstance(tnd, dict):
                issues.append(Issue("error", "IN_TARGET_DEAD",
                                    f"{where}的注入目标指向已删除的节点", nid))
                continue
            if tgt == nid:
                issues.append(Issue("error", "IN_SELF",
                                    f"{where}不能把参数注入到自己身上", nid))
                continue
            spec = NODE_TYPES.get(tnd.get("type")) or {}
            keys = {f[0] for f in spec.get("fields", ())}
            if field not in INPUT_FIELD_NAMES:
                issues.append(Issue("error", "IN_FIELD",
                                    f"{where}要改的字段名「{field}」不认识"
                                    f"（可用：{'、'.join(INPUT_FIELD_NAMES)}）", nid))
            elif field not in keys:
                # 覆盖是自由的：字段名对、但目标节点自己不读它 → 白填，只警告
                issues.append(Issue("warn", "IN_FIELD_UNUSED",
                                    f"{where}改的「{field}」在目标"
                                    f"「{tnd.get('title', tgt)}」上没有用到"
                                    f"（它可用：{'、'.join(sorted(keys))}）", nid))
            slot = (tgt, field)
            if slot in seen_slot:
                issues.append(Issue("warn", "IN_SLOT_DUP",
                                    f"{where}和 #{seen_slot[slot]} 改的是同一个字段"
                                    f"（同一个节点 + 同一个字段），用户填两个值会互相覆盖", nid))
            else:
                seen_slot[slot] = node_no(flow, nid, 0)
        verify = str(p.get("verify") or "").strip()
        if verify:
            err = regex_error(verify)
            if err:
                issues.append(Issue("error", "IN_VERIFY",
                                    f"{where}的整数校验正则「{verify}」不是合法正则"
                                    f"（{err}）", nid))
        val = input_value_expr(p)
        if "{" not in val:
            issues.append(Issue("warn", "IN_NO_VAR",
                                f"{where}的值「{val}」里没用上参数：写 {{变量名}} 才会被"
                                f"用户在 App 里填的值替换", nid))


def _check_picks(flow, issues):
    """【选择】节点的校验：参数名 + 每个选项的「选项名 / 字段 / 值」+ 有没有节点可作用。

    ★ 这几种情况会让某一项**静默消失**（生成物照样合法、引擎加载不报错，只有同步后
      才发现手机上的下拉里没这一项）：选项名没填、字段不认识、没有任何目标节点能作用。
    ★ 目标节点优先看这一行自己写的（老流程的写法），其次看注入线连到的节点。"""
    nodes = flow.get("nodes") or {}
    items = sorted((node_no(flow, nid, 0), nid, nd)
                   for nid, nd in nodes.items()
                   if isinstance(nd, dict) and nd.get("type") == "pick")
    for _no, nid, nd in items:
        p = nd.get("props") or {}
        no = f"#{node_no(flow, nid, 0)}"
        where = f"{no}「{nd.get('title', '选择(下拉)')}」"
        rows = [r for r in (p.get("cases") or []) if isinstance(r, dict)]
        base = [t for t in (p.get("targets") or []) if t in nodes]
        if not rows and not base:
            continue            # 还没开始配（刚新建的空白节点）
        opt_name = str(p.get("option") or "").strip()
        if not opt_name:
            issues.append(Issue("error", "PK_NAME_EMPTY",
                                f"{where}还没填参数名（App 的【参数】里显示这个名字）",
                                nid))
        # 有选项行没自己写目标节点、而注入线又一条都没连 → 这些选项生成不出来
        if not base and any(not str(r.get("node") or "").strip() for r in rows):
            issues.append(Issue(
                "warn", "PK_NO_TARGET",
                f"{where}右侧的「注入」小球还没连到任何节点 —— 没写死目标的选项"
                f"不知道该改谁的字段（把小球拖到要作用的节点上，可以连好几个）", nid))
        for i, r in enumerate(rows, 1):
            rname = str(r.get("name") or "").strip()
            node = str(r.get("node") or "").strip()
            label = f"{where}的第 {i} 个选项" + (f"「{rname}」" if rname else "")
            if not rname:
                issues.append(Issue(
                    "warn", "PK_CASE_NAME",
                    f"{label}还没填选项名 —— 不填这一项不会写进 interface.json"
                    f"（同步到手机后，下拉里没有这个选项）", nid))
            field = input_field({"field": r.get("field")})
            if field not in PICK_FIELD_NAMES:
                issues.append(Issue(
                    "warn", "PK_CASE_FIELD",
                    f"{label}的字段「{field}」不认识"
                    f"（可用：{'、'.join(PICK_FIELD_NAMES)}）", nid))
            if not str(r.get("value") or "").strip():
                # 值是空的 → 写出 {"repeat": ""} 这种覆盖，类型可能对不上，
                # 引擎校验不过会拒绝【整包】（铁律 #1）
                issues.append(Issue(
                    "warn", "PK_CASE_VALUE",
                    f"{label}还没填值（空值覆盖上去可能让引擎拒绝整包加载）", nid))
            if node and base:
                issues.append(Issue(
                    "warn", "PK_MIX",
                    f"{label}自己写死了目标节点「{node}」—— 注入线对它不起作用"
                    f"（这一项以行内写的为准）", nid))


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
        no = f"#{node_no(flow, nid, i + 1)}"
        if t == "branch" and str(p.get("ocr_text", "")).strip():
            pass   # OCR 文字判定分支无需模板图
        elif t in ("tpl_click", "wait_tpl", "branch"):
            tpls = split_tpls(p.get("template", ""))
            if not tpls:
                # 字段被【输入】参数覆盖时，这里也要填一个默认模板：参数是运行期替换，
                # 不带参数单独跑（直达入口 / 手点任务）用的就是这里的值。
                who = find_input_for(flow, nid, "template")
                msg = (f"{no}{title(nd)}未选择模板图"
                       + (f"（#{who[0]}「{who[1]}」这个参数会覆盖它，"
                          f"但这里仍要填一个默认模板，否则不带参数单独跑会失败）"
                          if who else ""))
                issues.append(Issue("error", "TPL_MISSING", msg, nid))
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
                lab = f"候选{ci + 1}"
                tgt, tnd = cand_target(flow, c)
                if not tgt:
                    issues.append(Issue(
                        "error", "CAND_NO_TARGET",
                        f"{no}{title(nd)}{lab}还没连到内容起点：候选判什么就看它连到的"
                        f"分支节点（拖卡片右侧的圆点连过去；不用的候选请删掉）", nid))
                elif tnd is None:
                    issues.append(Issue("error", "CAND_EXIT_DEAD",
                                        f"{no}{title(nd)}{lab}命中出口指向已删除节点", nid))
                elif tgt == nid:
                    issues.append(Issue("error", "CAND_EXIT_SELF",
                                        f"{no}{title(nd)}{lab}命中出口不能指向自己", nid))
                elif tnd.get("type") != "branch":
                    issues.append(Issue(
                        "error", "CAND_NOT_BRANCH",
                        f"{no}{title(nd)}{lab}连到的「{tnd.get('title', tgt)}」不是分支节点："
                        f"判定条件（模板图/OCR 文字）只能在【分支】节点里配，"
                        f"请把它连到一个分支", nid))
                # 目标是分支、但那个分支还没配条件 → 由分支自己的 TPL_MISSING 报，这里不重复
            mn = p.get("miss_next")
            if mn and mn not in nodes:
                issues.append(Issue("error", "MISS_EXIT_DEAD",
                                    f"{no}{title(nd)}全部未中出口指向已删除节点", nid))
        if nd.get("cut") and i < len(chain) - 1:
            issues.append(Issue(
                "warn", "NODE_CUT",
                f"{no}{title(nd)}标了「此处断开」：它后面的 {len(chain) - i - 1} 个节点"
                f"不会执行；要接回来在右侧「连接」里给「下一个」选一个节点", nid))
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
    _check_ocr_regex(flow, issues)
    _check_inputs(flow, issues)
    _check_picks(flow, issues)
    _check_offchain(flow, issues)
    _check_offchain_exits(flow, issues)
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


def node_no(flow, nid, fallback=0):
    """节点的显示编号。用持久化的 num（新建时分配、之后永不变），
    旧文件在加载时会被 normalize_flow 按当时的链序补上 num，所以显示与以前一致。"""
    nd = (flow.get("nodes") or {}).get(nid) or {}
    num = nd.get("num")
    if isinstance(num, int) and num > 0:
        return num
    ch = flow.get("chain") or []
    return (ch.index(nid) + 1) if nid in ch else fallback


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
    num = nd.get("num")
    if isinstance(num, int) and num > 0:
        return f"{num:02d}"        # 用固定编号：重排链序时节点名不再跟着变
    return f"{flow['chain'].index(nid) + 1:02d}"


def jname(flow, nid):
    """链上节点的 pipeline 名。
    switch 节点展开为 J1..JN 级联容器，故链上前驱的 next 指向首个判定 J1。

    ★【通道/跳转】节点可以带 props.emit_name = **手写管线里的节点名**（如 征集段2）：
      这时生成名就用它 —— 老流程的参数（interface.json 里 `征集段2.next` 这种覆盖）
      才对得上。放在这里而不是各调用点，是为了让"后继/命中/未中/参数目标/枝干候选"
      全都用同一个名字。"""
    nd0 = (flow.get("nodes") or {}).get(nid) or {}
    if nd0.get("type") == "pass":
        nm = str((nd0.get("props") or {}).get("emit_name") or "").strip()
        if nm:
            return nm
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


def split_ocr_texts(raw):
    """OCR 文字字段 → 期望文本列表（中英文逗号都能分隔，逐项去空白）。
    ocr_click 的 text 与 branch 的 ocr_text 共用它，「校验」和「生成」因此看的
    是同一份结果 —— 校验说合法，生成出来就一定合法。"""
    return [s.strip() for s in str(raw).replace("，", ",").split(",") if s.strip()]


def _ocr_expected(p):
    """OCR 期望文本。props 键沿用历史名 text（旧流程文件因此无需迁移），
    读取时也兼容 expected；输出统一用协议规范字段 expected —— MaaFramework 里
    text 是「已废弃字段，兼容一下」，正式名是 expected（且支持正则）。"""
    raw = p.get("text")
    if raw is None or not str(raw).strip():
        raw = p.get("expected", "")
    return split_ocr_texts(raw)


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
    返回是否需要生成收口节点（miss 未连线时）。

    ★ 命中出口只认【显式连线】：没连就是"命中即结束"，不再自动走链上下一个。
      那条隐式边在画布上看不见，最容易让人以为"分支默认会继续往下跑"；
      老流程在 normalize_flow 里已经把当时的隐式边补成了显式连线，所以生成物不变。"""
    exits = exits_of(flow, nid)
    hit, miss = exits["hit"], exits["miss"]
    hit_ref = [jname(flow, hit)] if hit else []      # 未连线 → 命中即结束
    miss_ref = [jname(flow, miss)] if miss else [f"{entry_name(flow)}_End"]
    end_needed = not miss
    out[name] = {
        "action": "DoNothing",
        "timeout": int(p["timeout"]),
        "next": [name + "_Hit"],
        "on_error": miss_ref,
    }
    hd = branch_hit_block(flow, nid)
    if hd is None:
        raise FlowValidationError(
            [f"分支节点未配置判定条件（模板图或 OCR 文字）: {name}"])
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


def _emit_pass(out, name, p, nxt):
    """【通道/跳转】：不识别、不动作，只把控制流送到下一个（引擎默认 DirectHit+DoNothing）。
    写进生成物的就是 {"next": [...]} —— 与手写管线里那种"过路"节点逐字段相同，
    所以针对它写的 pipeline_override（如 征集段2.next）能原样生效。"""
    out[name] = {"next": list(nxt)} if nxt else {"next": []}


def _emit_input(out, name, p, nxt):
    """【输入】节点：正常情况下它【离链】（normalize_flow 会把它移出主链），
    所以根本不会被发射 —— 它是参数声明，不占管线节点。
    这里保留一条兜底：万一它还在链上（老文件、手改过），就发一个空步直落下一个，
    至少不会让它把链断开。"""
    d = {"action": "DoNothing"}
    if nxt:
        d["next"] = nxt
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

    ★ 候选判什么不在这里配：判定块直接取自候选连到的那个【分支】节点
      （见 switch_cand_block）—— 模板图/OCR 文字只在分支里配一次，
      所以「枝干判定」与「分支判定」用的永远是同一套条件。
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
        hd = switch_cand_block(flow, c)
        if hd is None:
            raise FlowValidationError([
                f"枝干节点「{name}」的候选{ci + 1}没有判定条件："
                f"把它连到一个分支节点，并在那个分支里选模板图或填 OCR 文字"])
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
    elif t == "pass":
        # 生成名可以用 emit_name 指定（老流程要保留手写管线的名字）
        _emit_pass(out, (str(p.get("emit_name") or "").strip() or name), p, nxt)
    elif t == "input":
        _emit_input(out, name, p, nxt)
    elif t == "pick":
        pass          # 【选择】是参数声明：只进 interface.json，不生成管线节点
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

    # ---------- I2.5：从入口走不到的节点 = 死节点（2026-09-15 的教训） ----------
    # 重建流程时把链上的 next 整片丢了 → 任务跑完**第一个节点**就"成功结束"
    # （日志里 task end [ret=true]，不报错不失败，看着像跑完了）。引擎照样 loaded=True
    # —— 缺边不是合法性问题，所以只有实跑或结构比对才看得出来。这里把它变成警告。
    seen, stack = set(), [E]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in out:
            continue
        seen.add(cur)
        stack += _out_edges(out, cur)
    orphans = sorted(n for n in out
                     if not n.startswith("$") and n != E and n != dock_n and n not in seen)
    if orphans:
        shown = "、".join(orphans[:5]) + ("…" if len(orphans) > 5 else "")
        issues.append(Issue(
            "warn", "NODE_UNREACHABLE",
            f"生成物里有 {len(orphans)} 个节点从入口 {E} 走不到：{shown}"
            "（不会执行；多半是重建时丢了边或入口接错 —— 症状是「跑完第一个节点就结束」）"))

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
    # 存盘即视为"已按当前语义整理过"：老文件的隐式边已在 normalize_flow 里补成显式连线，
    # 打上版本号后，下次打开就不会再补 —— 新建分支"没连线"才是真的没连线。
    flow["schemaVersion"] = SCHEMA_VERSION
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
            # 枝干候选不再自带模板：它用的模板就是它连到的分支的模板，
            # 上面那条已经把这个分支收进来了，所以这里不用再算一遍。
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


RECO_FIELDS = ("template", "expected", "threshold", "roi")


def inject_targets_of(nd):
    """参数节点（【输入】/【选择】）注入到的节点 id 列表（就地返回，可增删）。

    ★ 一个参数可以注入多个节点：卡片右侧的小球往每个目标各拉一条线（就像手写的
      『目标角色』同时覆盖 CDZB2_Hit / 升2_Hit / 查2_Hit 三个）。
    这条线只表示"参数作用在哪个节点上"，与流程顺序无关，生成时变成 interface.json
    里 pipeline_override 的键。"""
    p = (nd or {}).setdefault("props", {})
    if not isinstance(p.get("targets"), list):
        p["targets"] = []
    return p["targets"]


def input_targets(nd):
    """【输入】节点的注入目标 —— 就是上面的 inject_targets_of()（保留旧名）。"""
    return inject_targets_of(nd)


def input_override_pairs(flow, nd):
    """【输入】节点 → [(生成节点名, 字段名), ...]，每个注入目标一条。
    识别类字段（模板图/OCR文字/阈值/ROI）在【分支】上落在它的 *_Hit —— 分支的识别块
    在那里；其余字段（如 repeat）落在节点本身。生成名用 jname()，与 build_pipeline 同源。"""
    p = (nd or {}).get("props") or {}
    field = input_field(p)
    out = []
    for tgt in list(input_targets(nd)):
        tnd = (flow.get("nodes") or {}).get(tgt)
        if not isinstance(tnd, dict):
            continue
        name = emit_name_of(flow, tgt) or jname(flow, tgt)
        if tnd.get("type") == "branch" and field in RECO_FIELDS:
            name += "_Hit"
        out.append((name, field))
    # ★ 手写管线上的节点名（老流程：cdzb.json / zhengji.json 那批）。
    #   它们不在本流程里，但参数要作用在它们身上 —— 原样当节点名用，
    #   这样"编辑器里看到的参数"和"手机上真正生效的参数"是同一个。
    for raw in parse_raw_targets(p):
        nm = target_name_of(flow, raw)
        if nm and (nm, field) not in out:
            out.append((nm, field))
    return out


def cases_of(nd):
    """【选择】节点的选项行（就地返回，可增删；保证是 list）。"""
    p = (nd or {}).setdefault("props", {})
    if not isinstance(p.get("cases"), list):
        p["cases"] = []
    return p["cases"]


def case_row_blank(row):
    """选项行是不是「空壳」（没名字、没连目标、没填值）。

    断开注入线之后留下这种行没有意义（不会进 interface.json，画布上也没有线），
    直接删掉，免得面板里堆一排空行。field 有默认值，不算内容。"""
    if not isinstance(row, dict):
        return True
    return not (str(row.get("name") or "").strip()
                or str(row.get("node") or "").strip()
                or str(row.get("value") or "").strip())


def resolve_param_target(flow, token):
    """参数里写的「目标」→ 画布节点 id（认不出来返回 None）。

    三种写法都认：画布节点 id / `#编号`（按固定编号反查，不是链序下标）/
    生成名（含【通道/跳转】的运行时名）或节点标题 —— 老流程的参数常直接写手写管线里的
    节点名（`ZJ_JiaHao2`），而画布上那个节点的标题就是那个名字。

    ★ 画布上画注入线与拖线落地都用这一个函数：两边解析口径必须一致，
      否则会出现"看着连上了、松手却没写进去"。"""
    tk_ = str(token or "").strip()
    if not tk_:
        return None
    nodes = (flow or {}).get("nodes") or {}
    if tk_ in nodes:
        return tk_
    m = re.match(r"^#(\d+)$", tk_)
    if m:
        want = int(m.group(1))
        for nid, nd in nodes.items():
            if not isinstance(nd, dict) or nd.get("type") in ("input", "pick"):
                continue
            if node_no(flow, nid, 0) == want:
                return nid
    for nid, nd in nodes.items():
        if not isinstance(nd, dict) or nd.get("type") in ("input", "pick"):
            continue
        if jname(flow, nid) == tk_ or str(nd.get("title") or "") == tk_:
            return nid
    return None


def pick_case_targets(flow, nd):
    """【选择】卡片上要画线的目标节点 id 列表：注入线连到的（`props.targets`）
    加上各选项自己写死的（老流程的 `row["node"]`）。

    口径与 pick_cases() 生成时**完全一致** —— 画布上看得见的虚线，就是同步后真正生效的。"""
    out = list(inject_targets_of(nd))
    for r in ((nd or {}).get("props") or {}).get("cases") or []:
        if isinstance(r, dict):
            out.append(resolve_param_target(flow, r.get("node")))
    return out


def pick_target_choices(flow):
    """【选择】选项「目标节点」下拉的候选：画布上每个非参数节点的「#编号 标题」。

    ★ 参数节点（【输入】/【选择】）自己不作为目标；编号是节点的【固定编号】，
      写回文件时会存成节点 id，生成时换成节点名。"""
    out = []
    for nid, nd in (flow.get("nodes") or {}).items():
        if not isinstance(nd, dict) or nd.get("type") in ("input", "pick"):
            continue
        no = node_no(flow, nid, 0)
        out.append((no, f"#{no} {nd.get('title') or nid}"))
    out.sort(key=lambda p: p[0])
    return [label for _no, label in out]


def pick_value_choices(flow, field):
    """【选择】选项「值」这一格的候选，随【字段】变：

    - 模板图 (template) → 模板目录里的文件名（框选工具新存的模板立刻可选）
    - 下一个出口 (next) → 画布节点（同样给「#编号 标题」，写 `#编号` 就够）
    - 其余（OCR 文字 / 数字 / 阈值 / 延时…）不限制 —— 下拉只是省手打的入口，
      输入框仍然可以直接填任何字符串。"""
    field = input_field({"field": field})
    if field == "template":
        return list_templates()
    if field == "next":
        return pick_target_choices(flow)
    return []


def pick_value_hint(field):
    """「值」这一格随【字段】的填法说明（面板上一行小字，省得猜该填什么）"""
    field = input_field({"field": field})
    if field == "template":
        return "值：模板文件名（点下拉挑一张，或直接填）"
    if field == "next":
        return "值：要跳到的节点（点下拉挑，或写节点名/手写管线节点名，多个用逗号）"
    if field == "expected":
        return "值：OCR 文字（引擎按【正则】编译）"
    if field == "roi":
        return "值：识别区域 左,上,右,下（如 100,200,300,400）"
    if field == "threshold":
        return "值：0~1 之间的小数（如 0.8）"
    if field in ("enabled", "order_by"):
        return "值：true / false"
    if field in ("repeat", "max_hit", "index", "timeout", "rate_limit",
                 "pre_delay", "post_delay", "repeat_delay"):
        return "值：整数（次数 / 毫秒，按字段而定）"
    return "值：直接填（会原样进 interface.json 的 pipeline_override）"


def norm_ref_text(text):
    """「目标节点 / 值」格里可能留着下拉给的「#编号 标题」写法 —— 只留 `#编号`。

    ★ 带标题的那一串不是能反查的 token：生成时会被当"手写管线的节点名"原样写进
      生成物，引擎随即因为引用了不存在的节点拒绝**整包**（铁律 #1）。其余写法原样返回。"""
    t = str(text or "").strip()
    m = re.match(r"^(#\d+)[\s\u3000]+", t)
    return m.group(1) if m else t


def input_value_expr(p):
    """参数的「值表达式」：留空按字段自动 —— 模板图 → {变量}.png（就是"对着文字
    找同名模板"），其余 → {变量}。App 端：整串正好是一个 {变量} 时按 pipeline_type
    转 int/bool，混在文本里（{角色}.png）只做字符串替换。"""
    var = str(p.get("var") or "").strip() or str(p.get("option") or "").strip()
    expr = str(p.get("value") or "").strip()
    if expr:
        return expr
    if input_field(p) == "template":
        return f"{{{var}}}.png"
    return f"{{{var}}}"


def flow_input_options(flow):
    """流程里全部【输入】节点 → interface.json 顶层 option 定义。

    ★ 一个参数可以注入多个节点：右侧小球往每个目标各拉一条线，每个目标一条
      override（手写的『目标角色』就是这样同时覆盖 CDZB2_Hit / 升2_Hit / 查2_Hit）。
    ★ 同名参数自动【合并】：拆成多个【输入】节点、参数名写成一样也可以，
      合并时以第一个节点为准取 label/inputs/说明，pipeline_override 逐个并入。
    形状与 App 端 TaskPack.kt 的解析逐字段对齐：type=input + inputs + pipeline_override。
    顺序按节点编号 —— 与画布上看到的 #号一致。"""
    opts = {}
    items = sorted((node_no(flow, nid, 0), nid, nd)
                   for nid, nd in (flow.get("nodes") or {}).items()
                   if isinstance(nd, dict) and nd.get("type") == "input")
    for _no, nid, nd in items:
        p = nd.get("props") or {}
        name = str(p.get("option") or "").strip()
        pairs = input_override_pairs(flow, nd)
        if not name or not pairs:
            continue
        expr = input_value_expr(p)
        if name in opts:
            # 同名参数：把这一处的覆盖并进去（同一个节点上不同字段要并存，所以按节点深合并）
            for node_name, field in pairs:
                opts[name]["pipeline_override"].setdefault(node_name, {})[field] =                     case_value(field, expr)
            continue
        var = str(p.get("var") or "").strip() or name
        is_int = str(p.get("kind") or "文本").strip() == "整数"
        inp = {"name": var,
               "label": str(p.get("var_label") or "").strip() or var,
               "default": str(p.get("default") or ""),
               "pipeline_type": "int" if is_int else "string"}
        verify = str(p.get("verify") or "").strip()
        if is_int and verify:
            inp["verify"] = verify
            msg = str(p.get("pattern_msg") or "").strip()
            if msg:
                inp["pattern_msg"] = msg
        o = {"type": "input", "label": name, "inputs": [inp],
             "pipeline_override": {}}
        for node_name, field in pairs:
            # 值是节点名列表的字段（next）要写成数组、纯数字写 int —— 与【选择】同一套口径
            o["pipeline_override"].setdefault(node_name, {})[field] = case_value(field, expr)
        desc = str(p.get("desc") or "").strip()
        if desc:
            o["description"] = desc
        opts[name] = o
    # 【选择】节点 → type=select + cases（每个选项覆盖不同字段/节点）。
    # 不走"同名合并"（select 的 cases 是整份替换语义，合并会写出四不像）。
    picks = sorted((node_no(flow, nid, 0), nid, nd)
                   for nid, nd in (flow.get("nodes") or {}).items()
                   if isinstance(nd, dict) and nd.get("type") == "pick")
    for _no, nid, nd in picks:
        p = nd.get("props") or {}
        name = str(p.get("option") or "").strip()
        cases = pick_cases(flow, nd)
        if not name or not cases:
            continue
        label = (str(p.get("var_label") or "").strip() or name)
        o = {"type": "select", "label": name, "cases": cases}
        o["default_case"] = str(p.get("default") or "").strip() or cases[0]["name"]
        if label != name:
            o["label"] = label
        desc = str(p.get("desc") or "").strip()
        if desc:
            o["description"] = desc
        opts[name] = o
    return opts


def upsert_flow_task(data, flow_name, log=None, options=None):
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
    # 转正后移除指向同一流程的**重复**小工具条目（防重复）。
    # ★ 只有当同一个流程另外还挂着一个正式任务（非 tools 分组）时才该删 —— 否则删掉的
    #   就是任务本身，它会被重新追加到清单末尾：任务在 App 里跳到最后一行，标签页上手写的
    #   label / description / default_check 也一起丢。查找器者就是这种（它本来就在【小工具】
    #   分组里，迁移后 entry 变成 VF_查找器者，同步一次就会被挪到清单末尾）。
    if any(t.get("entry") == entry and t.get("group") != ["tools"] for t in tasks):
        tasks[:] = [t for t in tasks
                    if not (t.get("entry") == entry and t.get("group") == ["tools"])]
    if not any(t.get("entry") == entry for t in tasks):
        tasks.append({"name": flow_name, "label": flow_name, "entry": entry,
                      "group": ["tools"]})
    # 【输入】节点声明的参数 → 顶层 option + 挂到本任务上（App 的任务编辑栏显示的就是它们）。
    # 只增改、不删除：手工维护的那些参数（升好感度/装卸装备/刷冬谷币…）不能被自动清掉，
    # 流程里没有【输入】节点时连任务的 option 列表都不动。
    if options:
        book = data.setdefault("option", {})
        for nm in options:
            if nm in book and book[nm] != options[nm] and log:
                log(f"参数【{nm}】已存在，按流程里的【输入】节点覆盖它的定义", "warn")
            book[nm] = options[nm]
        for t in tasks:
            if t.get("entry") == entry:
                t["option"] = list(options)
        if log:
            log("已写入参数: " + "、".join(options))


def register_on_phone(flow_name, log, options=None):
    """把流程注册进手机端 interface.json（group=tools），重启 App 后出现在【小工具】栏。
    只改手机上的运行副本，本地 whmx/interface.json 不动；改前手机端备份 .bak。
    options = 流程里【输入】节点声明的参数（App 任务编辑栏里的可填项）。"""
    text = adb_text(adb("shell",
                        f"run-as {PKG} sh -c 'cat files/taskpacks/whmx/interface.json'"))
    if not text:
        raise RuntimeError("读取手机 interface.json 失败（任务包是否已安装?）")
    data = jsonc_loads(text)
    upsert_flow_task(data, flow_name, log, options=options)
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
        root.geometry(initial_geometry(root))
        root.configure(bg=THEME["bg"])
        self._setup_style()

        self.flow = new_flow()
        self.frame_wh = (FRAME_W, FRAME_H)
        self.bg_pil = None          # 背景帧 PIL（调暗后）
        self.bg_photo = None
        self.bg_disp = None         # (ox, oy, w, h) 帧显示区域
        self.show_bg = tk.BooleanVar(value=True)
        # 删除免确认（工具栏「✖ 删除」右边那个开关）：开着时点删除直接删，不弹询问框。
        # 记住上次的选择（ui_prefs.json）—— 连着清理一批节点时每次都要点一次"是"很烦，
        # 而误删有 Ctrl+Z 兜底。
        self.no_confirm_delete = tk.BooleanVar(
            value=bool(load_prefs().get("no_confirm_delete")))
        self.no_confirm_delete.trace_add(
            "write", lambda *_: save_prefs(
                no_confirm_delete=bool(self.no_confirm_delete.get())))
        self.sel = None
        self.templates = list_templates()
        self._frame_file = None     # 当前背景帧文件路径（联动框选工具）
        self.pick_target = None     # 取坐标模式: "x" | "x1" | "x2"
        self.drag = None
        self.wire = None
        self.prop_widgets = {}
        # 属性面板"重建前要提交"的回调（目前是【选择】的选项编辑器：三格里的改动
        # 得先落进 props，再销毁控件）。面板重建时由 build_prop_panel() 调用一次。
        self._panel_commit = None
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
        self.multi = set()          # 右键框选出来的多个节点（批量删除用）
        self.band = None            # 正在拖的框选矩形（模型坐标 x0,y0,x1,y1）
        self.roi_pick = None        # ROI 拖框状态（P2-6）
        self.tpl_win = None         # 模板管理窗口
        # 画布视图：模型坐标（world）不变，绘制时统一乘 zoom，交互时统一除 zoom。
        # 这样节点永远存在同一处，缩放/平移只是「看的方式」。
        self.zoom = 1.0
        self._pan = None            # 中键拖动平移状态
        self._pan_left = False      # 左键在空白处拖动 = 平移（见 on_down/on_motion/on_up）
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
        self._update_undo_buttons()
        self.root.after(150, lambda: self.log(
            project_paths.describe(), "" if project_paths.PACK_OK else "warn"))
        # 窗口被 WM 摆出来之后量一次真实底边，伸进任务栏就收掉（见方法注释）
        self.root.after(60, self._fit_window_to_workarea)

    # ---------- 主题 ----------

    def _fit_window_to_workarea(self):
        """窗口若伸进任务栏，就把高度/宽度收掉超出的部分。

        初始 geometry 已按工作区夹过【尺寸】，但【摆放位置】由 Windows 决定 ——
        实测它会把窗口放在 y=+40，客户区底边于是又超出工作区（状态栏再被任务栏
        压住一次）。标题栏多高、WM 爱把窗口放哪，都不用猜：布局完成后量一次
        真实底边/右边，超出工作区就收掉。"""
        wa = _windows_workarea()
        if not wa:
            return
        try:
            self.root.update_idletasks()
            if self.root.winfo_rootx() <= 1 and self.root.winfo_rooty() <= 1:
                return                      # 还没被摆出来（没映射），量了也是假的
            w, h = self.root.winfo_width(), self.root.winfo_height()
            dy = (self.root.winfo_rooty() + h) - wa[1]
            dx = (self.root.winfo_rootx() + w) - wa[0]
            if dy > 0 or dx > 0:
                self.root.geometry("%dx%d" % (w - max(dx, 0), h - max(dy, 0)))
        except tk.TclError:
            pass

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
        # 撤回/重做做成按钮：快捷键一直在，但界面上没提示等于没有
        self._btn_undo = self._flat_btn(bar, "↶ 撤回", self.undo, padx=8,
                                        font=FONT_SM, bg="#2a2f3d")
        self._btn_undo.pack(side="left", padx=(8, 2))
        self._btn_redo = self._flat_btn(bar, "↷ 重做", self.redo, padx=8,
                                        font=FONT_SM, bg="#2a2f3d")
        self._btn_redo.pack(side="left", padx=2)

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

    def _pal_group(self, parent, key, title, tip=""):
        """左栏的一个可折叠分组：点标题行展开/收起（状态记在内存里）。
        左栏东西太多，收起来就不用滚了。返回内容容器。"""
        collapsed = getattr(self, "_pal_collapsed", None)
        if collapsed is None:
            collapsed = self._pal_collapsed = {}
        head = ttk.Frame(parent)
        head.pack(fill="x", padx=6, pady=(8, 0))
        lbl = tk.Label(head, anchor="w", justify="left",
                       bg=THEME["panel"], fg=THEME["text"],
                       font=FONT_B, cursor="hand2", padx=4, pady=3)
        lbl.pack(fill="x")
        body = ttk.Frame(parent)

        def _toggle(_e=None):
            collapsed[key] = not collapsed.get(key, False)
            _apply()

        def _apply():
            if collapsed.get(key, False):
                body.pack_forget()
                lbl.config(text=f"  ▸ {title}")
            else:
                # ★ 必须 after=head：pack_forget() 之后再 pack() 会排到父容器最后，
                #   展开后内容就跑到别的分组下面去了（要回到自己标题下面才对）。
                body.pack(fill="x", pady=(2, 0), after=head)
                lbl.config(text=f"  ▾ {title}" + (f"　{tip}" if tip else ""))
        lbl.bind("<Button-1>", _toggle)
        _apply()
        return body

    def _build_palette(self):
        col = ttk.Frame(self.root, width=190)
        col.pack(side="left", fill="y")
        col.pack_propagate(False)
        # 左栏套一层画布做滚动：几段加起来比窗口高，以前最下面的按钮会被窗口底边
        # 裁掉而且滚不到。现在滚动 + 分组折叠两件都有：想全看就折起来，想找就滚。
        pc = tk.Canvas(col, bg=THEME["panel"], highlightthickness=0, width=190)
        vsb = ttk.Scrollbar(col, orient="vertical", command=pc.yview)
        pc.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        pc.pack(side="left", fill="both", expand=True)
        self._palette_canvas = pc
        left = ttk.Frame(pc)
        win = pc.create_window((0, 0), window=left, anchor="nw")

        def _pal_w(e):
            try:
                pc.itemconfigure(win, width=max(1, e.width))
            except tk.TclError:
                pass

        def _pal_scroll(e):
            try:
                pc.configure(scrollregion=pc.bbox("all"))
            except tk.TclError:
                pass

        def _pal_wheel(e):
            pc.yview_scroll(int(-e.delta / 120), "units")
        left.bind("<Configure>", _pal_scroll)
        pc.bind("<Configure>", _pal_w)
        pc.bind("<Enter>", lambda e: pc.bind_all("<MouseWheel>", _pal_wheel))
        pc.bind("<Leave>", lambda e: pc.unbind_all("<MouseWheel>"))

        # ── 分组 1：节点库 ──────────────────────────────────────────
        g = self._pal_group(left, "nodes", "节 点 库", "点击添加")
        for t in TYPE_ORDER:
            spec = NODE_TYPES[t]
            b = tk.Button(g, text=f" {spec['icon']}  {spec['label']}",
                          command=lambda tt=t: self.add_node(tt),
                          bg=THEME["panel"], fg=spec["light"],
                          activebackground=THEME["card_hi"],
                          activeforeground=spec["light"],
                          relief="flat", bd=0, anchor="w", padx=12, pady=5,
                          font=FONT, cursor="hand2", highlightthickness=0)
            b.pack(fill="x", padx=8, pady=1)
            b.bind("<Enter>", lambda e, bb=b: bb.config(bg=THEME["card_hi"]))
            b.bind("<Leave>", lambda e, bb=b: bb.config(bg=THEME["panel"]))

        # ── 分组 2：视图与布局 ──────────────────────────────────────
        g = self._pal_group(left, "view", "视图与布局")
        self._flat_btn(g, "✥  整理布局", self.tidy_layout,
                       font=FONT_SM).pack(fill="x", padx=8, pady=(2, 0))
        zrow = ttk.Frame(g)
        zrow.pack(fill="x", padx=8, pady=(4, 0))
        self._flat_btn(zrow, "－", lambda: self.zoom_out(), padx=7,
                       font=FONT_SM).pack(side="left")
        self._flat_btn(zrow, "100%", lambda: self.zoom_reset(), padx=7,
                       font=FONT_SM).pack(side="left", padx=3)
        self._flat_btn(zrow, "＋", lambda: self.zoom_in(), padx=7,
                       font=FONT_SM).pack(side="left")
        self._flat_btn(g, "⤢  适配窗口", lambda: self.zoom_fit(),
                       font=FONT_SM).pack(fill="x", padx=8, pady=1)
        self._flat_btn(left, "⤢  适配窗口", lambda: self.zoom_fit(),
                       font=FONT_SM).pack(fill="x", padx=8, pady=1)

        # ── 分组 3：帧画面与工具 ────────────────────────────────────
        g = self._pal_group(left, "frame", "帧画面 · 工具")
        self._flat_btn(g, "⟳  抓帧 (F5)", self.on_capture).pack(fill="x", padx=8, pady=1)
        self._flat_btn(g, "🔍  帧画面窗口…", lambda: self._open_frame_window()).pack(
            fill="x", padx=8, pady=1)
        self._flat_btn(g, "🩺  运行回放…", self.on_replay_open).pack(fill="x", padx=8, pady=1)
        self._flat_btn(g, "✛  框选模板…", self.on_pick_template).pack(fill="x", padx=8, pady=1)
        self._flat_btn(g, "📂  打开帧图…", self.on_open_frame).pack(fill="x", padx=8, pady=1)
        self._flat_btn(g, "🗂  模板管理…", self.on_template_manager).pack(
            fill="x", padx=8, pady=1)
        self._make_toggle(g, self.show_bg, "画布上显示背景帧").pack(
            anchor="w", padx=10, pady=3)
        self.show_bg.trace_add("write", lambda *_: self.redraw())
        self.frame_lbl = ttk.Label(g, text="", style="Dim.TLabel", justify="left")
        self.frame_lbl.pack(anchor="w", padx=12, pady=2)
        self._update_frame_label()
        ttk.Frame(left).pack(fill="x", pady=6)

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
        # 平移：空白处按住左键拖、或中键拖；缩放：Ctrl+滚轮（见 _on_mousewheel）
        # 右键拖框：快速框选多个节点（框完按 Delete 删除，Esc 取消）
        self.canvas.bind("<ButtonPress-3>", self.on_band_start)
        self.canvas.bind("<B3-Motion>", self.on_band_move)
        self.canvas.bind("<ButtonRelease-3>", self.on_band_end)
        self.canvas.bind("<Button-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_move)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_end)
        # 方向键按【整格】移动（Shift 一次四格）：与拖动吸附一致，不会一格一格歪掉
        for key, dx, dy in (("<Left>", -GRID, 0), ("<Right>", GRID, 0),
                            ("<Up>", 0, -GRID), ("<Down>", 0, GRID),
                            ("<Shift-Left>", -4 * GRID, 0),
                            ("<Shift-Right>", 4 * GRID, 0),
                            ("<Shift-Up>", 0, -4 * GRID),
                            ("<Shift-Down>", 0, 4 * GRID)):
            self.canvas.bind(key, lambda e, dx=dx, dy=dy: (self.nudge_node(dx, dy),
                                                           "break")[1])
        self.root.bind("<F5>", lambda e: self.on_capture())
        self.root.bind("<Control-s>", lambda e: (self.on_save(), "break")[1])
        self.root.bind("<Button-1>", self._maybe_close_tpl_pop, add="+")
        self.root.bind("<Control-z>", lambda e: (self.undo(), "break")[1])
        self.root.bind("<Control-Z>", lambda e: (self.redo(), "break")[1])
        self.root.bind("<Control-y>", lambda e: (self.redo(), "break")[1])
        self.root.bind("<Control-Shift-Z>", lambda e: (self.redo(), "break")[1])
        self.root.bind("<Control-Shift-z>", lambda e: (self.redo(), "break")[1])
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
        # 删除右边的开关：开着 = 点「✖ 删除」直接删，不再弹询问框（误删有 Ctrl+Z）
        del_toggle = self._make_toggle(prow, self.no_confirm_delete, "删除免确认",
                                       font=FONT_SM)
        del_toggle.pack(side="left", padx=(6, 0))
        self._bind_tip(del_toggle,
                       "✓ = 点「✖ 删除」（或按 Delete）直接删除，不再弹询问框\n"
                       "✗ = 每次删除都先问一句（默认）\n"
                       "开关状态会记住；删错了随时 Ctrl+Z 撤回")

        ttk.Separator(right).pack(side="top", fill="x", pady=2, padx=8)
        ttk.Label(right, text="  连接（下拉可直接改接）",
                  style="Title.TLabel").pack(side="top", anchor="w", padx=8)
        crow = ttk.Frame(right)
        crow.pack(side="top", fill="x", padx=12, pady=(4, 2))
        ttk.Label(crow, text="上一个", style="Dim.TLabel").pack(side="left")
        self.prev_combo = ttk.Combobox(crow, width=16, state="disabled", font=FONT_SM)
        self.prev_combo.pack(side="left", padx=(2, 8))
        self.prev_combo.bind("<<ComboboxSelected>>",
                            lambda e: self.on_conn_combo("prev"))
        ttk.Label(crow, text="下一个", style="Dim.TLabel").pack(side="left")
        self.next_combo = ttk.Combobox(crow, width=16, state="disabled", font=FONT_SM)
        self.next_combo.pack(side="left", padx=2)
        self.next_combo.bind("<<ComboboxSelected>>",
                            lambda e: self.on_conn_combo("next"))
        ttk.Label(right, text="  出口（分支/枝干/循环专用）",
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
        self.status_var = tk.StringVar(
            value="就绪 · F5 抓帧 ｜ 拖动节点排序 ｜ 拖分支端口连线 ｜ Ctrl+Z 撤回 / Ctrl+Y 重做 ｜ "
                  "Ctrl+滚轮缩放 ｜ 空白处拖动平移 ｜ Delete 删除 ｜ Ctrl+S 保存")
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
        self._update_undo_buttons()
        self.flow = new_flow("测试流程")
        self.name_var.set(self.flow["name"])
        self.sel = None
        # ★ 新建出来的流程还没有对应文件，必须把「当前打开的文件」清掉：
        #   否则第一次保存时 on_save 会以为这是「改名」，把上次打开的那个流程文件删掉
        #   （2026-09-15 事故：打开 博物研学 → 新建 → 改名 刷活动关 保存 ⇒ 博物研学.flow.json 被删）
        self._loaded_path = None
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
            self._update_undo_buttons()
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
            self._auto_layout_if_overlapping()
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
        # ★ 必须【深拷贝】：defaults 里的 targets / candidates / cases 是 list，
        #   浅拷贝会让所有同类型节点共用同一个 list —— 往其中一个 append（拉注入线、
        #   加候选、加选项）就等于改了 NODE_TYPES 的"出厂默认值"，之后新建的节点一出生
        #   就带着上一个节点连过的节点/候选（真实事故：删掉【选择】再新建一个，
        #   它自动连回之前连过的两个节点，像"删不干净"）。
        p = copy.deepcopy(spec["defaults"])
        if props:
            p.update(props)
        if ntype == "input":
            # 【输入】是"参数声明"，不进主链：放到主链右侧的参数区，不参与链序、
            # 不生成管线节点；它注入到哪些节点由右侧小球拉线决定（拖线时才建立关系）。
            x, y = pos if pos else self._param_col_pos()
            node = {"type": ntype, "x": self._snap(x), "y": self._snap(y),
                    "title": spec["label"], "props": p, "num": self._next_num()}
            self.flow["nodes"][nid] = node
            self.sel = nid
            self.build_prop_panel()
            self.redraw()
            self._scroll_to(node["y"])
            self.log("已新建【输入】节点（它不占流程顺序）：填好参数名，"
                     "再把卡片右侧的「注入」小球拖到要注入的节点上")
            return nid
        if ntype == "pick":
            # 【选择】也是参数声明：不占流程顺序，选项在属性面板里逐条填
            x, y = pos if pos else self._param_col_pos()
            node = {"type": ntype, "x": self._snap(x), "y": self._snap(y),
                    "title": spec["label"], "props": p, "num": self._next_num()}
            self.flow["nodes"][nid] = node
            self.sel = nid
            self.build_prop_panel()
            self.redraw()
            self._scroll_to(node["y"])
            self.log("已新建【选择】节点（它不占流程顺序）：填好参数名，"
                     "再在下面逐条加选项（名称 / 目标节点 / 字段 / 值）—— "
                     "目标节点也可以直接把卡片右侧的「注入」小球拖到那个节点上")
            return nid
        # ★ 新建节点挂在【当前选中节点】的出口下：链上插到它后面，
        #   而不是像以前那样一律甩到链尾（链尾决定了 next，会让新节点接到别的节点后面）。
        anchor = self.sel if (self.sel and self.sel in self.flow["nodes"]
                              and self.sel in self.flow["chain"]) else None
        # 选中的节点本身还没接进流程时，没有"插在它后面"这个位置 → 新节点也先不接
        offchain_sel = bool(self.sel and self.sel in self.flow["nodes"]
                            and anchor is None)
        standalone = False        # 没选中任何节点 → 建出来先不接进流程（见下）
        if pos:
            x, y = pos
        elif anchor:
            a = self.flow["nodes"][anchor]
            ah = self._sw_h(a) if a["type"] == "switch" else CARD_H
            x, y = a["x"], a["y"] + ah + 26
        else:
            # ★ 没选中任何节点 → 新节点【先不接进流程】：放到右侧"未接入"区，
            #   不接任何节点、不参与生成。以前是"接到链尾节点后面"，等于点一下节点库
            #   就悄悄改了已有流程的走向（那条边还是看不见的链上顺序）。
            #   要接进去：选中它，在右侧「连接」里把「下一个」选成一个节点即可。
            x, y = self._spot_in_view(card_h({"type": ntype, "props": p}))
            standalone = True
        x, y = self._snap(x), self._snap(y)
        node = {"type": ntype, "x": x, "y": y, "title": spec["label"], "props": p,
                "num": self._next_num()}
        if ntype == "branch":
            node["hit_next"] = hit_next
            node["miss_next"] = miss_next
        self.flow["nodes"][nid] = node
        if not standalone:
            if anchor:
                self._chain_insert(self.flow["chain"].index(anchor) + 1, nid)
                # 边：锚点的下一个变成新节点；新节点接上锚点原来的下一个（原样往后挪一位）
                a_nd = self.flow["nodes"][anchor]
                node["next"] = a_nd.get("next")
                a_nd["next"] = nid
                a_nd.pop("cut", None)
            else:
                self._chain_insert(len(self.flow["chain"]), nid)
                node["next"] = None
        # ★ 让位：把链上【本节点之后】的节点整体下移一行。
        #   否则新节点看着在 A 下面，链序却排在"A 后面的后面"（例如 A 与下一个节点
        #   是并排同一行时），一拖动就被按位置重排成"挂到别的节点下"。
        if anchor:
            gap = (self._sw_h(node) if ntype == "switch" else CARD_H) + 26
            anchor_y = self.flow["nodes"][anchor]["y"]   # 别用上面的局部变量 a：
            idx = self.flow["chain"].index(nid)          # 显式给了 pos 时它没被赋值
            for other in self.flow["chain"][idx + 1:]:
                od = self.flow["nodes"][other]
                if od["y"] >= anchor_y:         # 推开"在锚点这一行及以下"的
                    od["y"] = self._snap(od["y"] + gap)
        self.sel = nid
        self.build_prop_panel()
        self.redraw()
        self._scroll_to(y)
        if standalone:
            why = ("选中的节点自己还没接进流程" if offchain_sel else "此刻没有选中任何节点")
            self.log(f"已新建「{spec['label']}」：{why}，所以它【没有接进流程】"
                     f"（放在最右侧的未接入区，不连任何节点、不参与生成）。"
                     f"要接进去：选中它，在右侧「连接」把「下一个」选成一个节点"
                     f"（或选『无：到此结束』排到链尾）", "warn")
            return nid
        if anchor:
            nd_a = self.flow["nodes"][anchor]
            t_a = nd_a["type"]
            title_a = nd_a.get("title", anchor)
            linked = None
            # ★ 关键：像分支/枝干/循环这类节点，出口不是"链上下一个"而是靠端口/候选控制的。
            #   只把它插到链上，生成结果里可能根本没有从 A 到 B 的边（甚至 B 不可达）。
            #   所以这里顺手把 A【空着的出口】真的接到新节点上 —— 这才是"强制被 A 连接"。
            #   例外：【分支】不再自动接 —— 它的 ✓/✗ 出口必须由你拖球显式连；自动接上的
            #   那条边在画布上看着像"链上顺序"，其实是个决定行为的隐式连线（已经踩过坑）。
            if t_a == "switch":
                # 枝干没有直落出口：追加一个候选指向新节点（等价于把「＋」球拖到它上面）。
                # 候选判什么 = 它连到的那个节点判什么（分支里配模板图/OCR 文字）
                cands = nd_a.setdefault("props", {}).setdefault("candidates", [])
                cands.append({"timeout": 3000, "next": nid})
                linked = f"第 {len(cands)} 个候选"
            elif t_a == "loop" and not (nd_a.get("props") or {}).get("body_end"):
                nd_a.setdefault("props", {})["body_end"] = nid
                linked = "循环体末尾"
            if linked:
                self.log(f"✓ 新节点已接到「{title_a}」的{linked}上", "ok")
            else:
                why = None
                if t_a == "common":
                    why = "公共收口节点进入后不返回本流程"
                elif t_a == "branch":
                    why = ("它是【分支】：命中/未中都要拖卡片右侧的 ✓/✗ 圆点显式连线"
                           "（不连 = 命中/未中即结束）")
                elif _suppress_reason(self.flow, anchor) == "switch-content-leaf":
                    why = "它是某条枝干的分支内容叶，跑完即止"
                if why:
                    self.log(f"⚠ 新节点插在「{title_a}」之后，但{why} —— 画布上需要手动连线",
                             "warn")
                else:
                    self.log(f"已把新节点接在「{title_a}」的链上出口下")
        return nid

    def delete_selected(self):
        """删除选中的节点：批量（右键框选出来的）优先，否则删当前单选的那个。

        工具栏「删除免确认」开关开着时不弹询问框（开关状态记在 ui_prefs.json 里）——
        删错了由 `_snapshot()` + Ctrl+Z 兜底。"""
        ids = set(self.multi) if self.multi else ({self.sel} if self.sel else set())
        ids = {i for i in ids if i in self.flow.get("nodes", {})}
        if not ids:
            return
        if not self.no_confirm_delete.get():
            if len(ids) == 1:
                nid = next(iter(ids))
                if not messagebox.askyesno(
                        "删除节点", "确定删除该节点？" + self._node_ref_label(nid)):
                    return
            else:
                if not messagebox.askyesno(
                        "删除节点", f"确定删除框选的这 {len(ids)} 个节点？"):
                    return
        self._snapshot()
        # ★ 名字要在删之前取：删完 _node_ref_label 就查不到了（会打出一串空名字）
        names = "、".join(self._node_ref_label(i)
                         for i in sorted(ids, key=lambda x: node_no(self.flow, x, 0)))
        self._delete_nodes(ids)
        self.sel = None
        self.multi = set()
        self.build_prop_panel()
        self.redraw()
        self.log(f"已删除 {len(ids)} 个节点：{names}"
                 f"（指向它们的出口/候选/注入已自动清空；Ctrl+Z 可撤销）", "warn")

    def _delete_nodes(self, ids):
        """把一批节点真的从流程里去掉：出链、并把指向它们的引用全部清空。

        引用清空很重要：留下死 id 的话，校验会报"指向已删除节点"，生成物里还会是
        悬空引用（引擎会拒绝加载整个任务包）。"""
        ids = set(ids)
        for nid in ids:
            self.flow["nodes"].pop(nid, None)
        ch = self.flow["chain"]
        self.flow["chain"] = [n for n in ch if n not in ids]
        for other in self.flow["nodes"].values():
            if other.get("next") in ids:
                other["next"] = None          # 边（v4）：不能留指向已删节点的悬空边
            if other.get("hit_next") in ids:
                other["hit_next"] = None
            if other.get("miss_next") in ids:
                other["miss_next"] = None
            if other.get("type") == "switch":
                op = other.get("props", {})
                if op.get("miss_next") in ids:
                    op["miss_next"] = None
                for c in op.get("candidates", []):
                    if isinstance(c, dict) and c.get("next") in ids:
                        c["next"] = None
            if other.get("type") == "loop":
                lop = other.get("props", {})
                if lop.get("body_end") in ids:
                    lop["body_end"] = None
            if other.get("type") in ("input", "pick"):
                # 被删的节点若是某个参数的注入目标，顺手摘掉，别留个死 id。
                # ★【选择】的注入目标（props.targets）与【输入】是同一份数据 ——
                #   以前这里只清 input，删掉目标节点后【选择】那边还挂着死 id：
                #   摘要还显示"注入 N 个节点"，生成时也照旧往那个名字上写覆盖。
                tg = inject_targets_of(other)
                for nid in ids:
                    while nid in tg:
                        tg.remove(nid)
            if other.get("type") == "pick":
                # 选项里"写死的目标节点"指到被删节点 → 清空（留着会生成指向不存在节点的键）
                for r in cases_of(other):
                    if str(r.get("node") or "").strip() in ids:
                        r["node"] = ""

    # ---------- 右键拖框：批量选择 ----------

    def on_band_start(self, e):
        """右键按下：开始拖框。框完（松开）就把框到的节点置为多选。"""
        cx = self._c2w(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        self.band = (cx[0], cx[1], cx[0], cx[1])
        self.redraw()
        self.status("拖框圈住要处理的节点，松开即选中（选中后按 Delete 删除 / Esc 取消）")
        return "break"

    def on_band_move(self, e):
        if self.band is None:
            return "break"
        x, y = self._c2w(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        self.band = (self.band[0], self.band[1], x, y)
        self._band_pick()          # 边拖边高亮框到的节点
        self.redraw()
        return "break"

    def on_band_end(self, _e):
        if self.band is None:
            return "break"
        self.band = None
        n = len(self.multi)
        self.redraw()
        if n:
            self.log(f"已框选 {n} 个节点：" +
                     "、".join(self._node_ref_label(i)
                               for i in sorted(self.multi, key=lambda x: node_no(self.flow, x, 0))) +
                     "（按 Delete 删除，Esc 取消选择）", "warn")
            self.status(f"已框选 {n} 个节点 —— 按 Delete 删除")
        else:
            self.status("框选未选中任何节点")
        return "break"

    def _band_pick(self):
        """把框选矩形里的节点置为多选（与卡片矩形有交集就算选中）。"""
        if not self.band:
            self.multi = set()
            return
        x0, y0, x1, y1 = self.band
        bx0, bx1 = min(x0, x1), max(x0, x1)
        by0, by1 = min(y0, y1), max(y0, y1)
        picked = set()
        for nid, nd in self.flow["nodes"].items():
            if not isinstance(nd, dict):
                continue
            nx1 = nd["x"] + CARD_W
            ny1 = nd["y"] + card_h(nd)
            if nd["x"] < bx1 and nx1 > bx0 and nd["y"] < by1 and ny1 > by0:
                picked.add(nid)
        self.multi = picked

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
            # ★ 链序只决定【显示顺序】：上下移动一律不碰任何边（nd["next"]）。
            #   以前"移动节点"会顺手改连接，用户看到的就是"我只挪了个位置，
            #   上面那条连接却断了"。要改连接请用右侧「连接」里的上一个/下一个。
            ch[i], ch[j] = ch[j], ch[i]
            self.build_prop_panel()
            self.redraw()
            self.log("已在链序里上/下挪了一位（显示顺序，连接不变）")

    def align_node(self):
        nid = self.sel
        if not nid:
            return
        self._snapshot(f"align:{nid}")
        self.flow["nodes"][nid]["x"] = self._side_col_x()
        self.redraw()
        self._focus_node(nid)      # 挪到侧列后自动滚过去，别让节点"消失"在视野外
        self.log("已挪到侧列（画布已自动滚到该节点；Ctrl+滚轮缩放 / 空白处或中键拖动平移）")

    def _auto_layout_if_overlapping(self):
        """打开流程后：若发现卡片互相重叠就自动排开。
        老流程（早期导入进来的那些）所有节点常常是同一个坐标，一打开就叠成一坨，
        看着像"没有节点"。这里按「整理布局」的规则排成主链一列（间距按卡片高度留，
        枝干卡更高也算进去了），排完可拖动自行调整；保存后下次打开就是排好的。"""
        bad = overlapping_nodes(self.flow)
        if not bad:
            return
        self.log(f"⚠ 检测到 {len(bad)} 个节点位置重叠（旧流程缺坐标就会这样），"
                 f"已自动排开：主链一列、按卡片高度留间距；可拖动自行调整", "warn")
        self.tidy_layout()

    def _loop_target(self, nid, idx):
        """回环目标：本节点的出口（下一个 / 命中 / 未中 / 候选）里，链序位置明显更靠上
        （不是紧挨着上一个）的那一个 —— 指回上方就是"回环"。没有 → None。

        典型是重试：【分支】未中 → 【滑动】→ 回到【分支】。链序里滑动排在最底下，
        所以"未中"那条边要横跨整块画布（博物研学实测 1250px）。

        ★ 只认【纯】回环节点（全部出口都指回上方，如【滑动】只有一个"下一个"）。
          带向前出口的（分支那种：命中往下走、未中往上回）**不能挪**：分支的 ✓/✗ 端口
          在卡片右侧，挪到主列右边之后它的向前出口要绕过整张卡片才能回到主列
          （派遣公司事务实测：挪了 #30/#33 两个分支，总长 4141 → 17749px）。"""
        ex = exits_of(self.flow, nid) or {}
        cands = [ex.get("linear"), ex.get("hit"), ex.get("miss")]
        cands += [c.get("next") for c in (ex.get("candidates") or [])]
        outs = [t for t in cands if t]
        up = [t for t in outs if t in idx and idx[t] < idx[nid] - 1]
        fwd = [t for t in outs if not (t in idx and idx[t] < idx[nid])]
        if not up or fwd:
            return None
        return min(up, key=lambda t: idx[t])

    def tidy_layout(self):
        """整理布局：主链一列 + 回环列（目标旁边）+ 参数列（【输入】贴着它的目标）。
        - x 固定在最左边（TIDY_X），不再排到帧右侧 —— 那里超出可视宽，点了就像"节点全跑了"
        - 纵向间距按每张卡片的【真实高度】留（枝干卡比普通卡高一截，
          以前按固定 CARD_H 留间距会让下一张压在它身上）"
        - ★ 回环节点（出口指回链序上方的）挪到目标【右侧那一列】贴着它，而不是沉在
          主列最底下 —— 否则"未中→滑动→回到分支"这条边要跨整块画布（实测 1200~1600px）
        - ★ 离链【输入】节点按"第一个目标"的 y 对齐放参数列：注入线因此成了一条短横线
          （以前全堆在参数列顶部，注入线要跨 1300~1600px）
        - 排完把视图拉回左上角"""
        import math as _math
        self._snapshot()
        ch = self.flow["chain"]
        idx = {nid: i for i, nid in enumerate(ch)}
        # 1) 主列：链序自上而下
        y = GRID
        for nid in ch:
            nd = self.flow["nodes"][nid]
            h = self._sw_h(nd) if nd["type"] == "switch" else CARD_H
            nd["x"] = TIDY_X
            nd["y"] = self._snap(y)
            # 纵向步长向上取整到网格的整数倍，保证每张卡片都落在网格线上
            y += _math.ceil((h + 26) / GRID) * GRID
        step = _math.ceil((CARD_H + 26) / GRID) * GRID
        # 2) 回环节点：目标右侧那一列，y 贴着目标（同目标多个就往下错开）
        loops = [(nid, self._loop_target(nid, idx)) for nid in ch]
        loops = [(nid, t) for nid, t in loops if t is not None]
        if loops:
            lx = self._loop_col_x()
            taken = []
            for nid, tgt in sorted(loops, key=lambda p: idx[p[1]]):
                ly = self.flow["nodes"][tgt]["y"]
                while any(abs(ly - t) < step for t in taken):
                    ly += step
                taken.append(ly)
                nd = self.flow["nodes"][nid]
                nd["x"] = lx
                nd["y"] = self._snap(ly)
        # 3) 离链【输入】节点：对齐它的第一个目标的 y
        px = self._param_col_x()
        taken = []
        free_y = GRID
        for nid, nd in self.flow["nodes"].items():
            if nd.get("type") not in ("input", "pick") or nid in ch:
                continue
            # 【输入】看注入目标、【选择】看选项连到的节点（别对 pick 调 input_targets：
            # 那会顺手给它塞一个没用的 props.targets，而且对齐的位置也不是它连的节点）
            raw_tgts = (pick_case_targets(self.flow, nd) if nd.get("type") == "pick"
                        else list(input_targets(nd)))
            tgts = [t for t in raw_tgts if t and t in self.flow["nodes"]]
            ty = [self.flow["nodes"][t]["y"] for t in tgts]
            want = min(ty) if ty else free_y
            while any(abs(want - t) < step for t in taken):
                want += step
            taken.append(want)
            free_y = max(free_y, want + step)
            nd["x"] = px
            nd["y"] = self._snap(want)
        # 4) 未接入流程的节点：再往右一列
        dx = self._draft_col_x()
        dy = GRID
        for nid, nd in self.flow["nodes"].items():
            if nid in ch or nd.get("type") in ("input", "pick"):
                continue
            nd["x"] = dx
            nd["y"] = self._snap(dy)
            import math as _math
            dy += _math.ceil((CARD_H + 26) / GRID) * GRID
        self.redraw()
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.log(f"已整理布局：主链排成靠左一列（x={TIDY_X}）"
                 + ("，回环节点贴到目标右侧" if loops else "")
                 + "，【输入】按目标对齐排在参数列，纵向不再重叠")

    def _chain_col_x(self):
        fw = self.bg_disp[2] if self.bg_disp else 750
        return fw + 130

    def _side_col_x(self):
        """侧列 x。缩放到最小也放不下时，侧列其实「在右边」——所以 align 之后
        必须自动滚过去（见 align_node），不能让人以为节点没了。"""
        return self._snap(self._chain_col_x() + CARD_W + 100)

    def _visible_rect(self):
        """当前可见视口在【模型坐标】下的矩形 (x0, y0, x1, y1)"""
        z = self.zoom or 1.0
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        x0, y0 = self._c2w(self.canvas.canvasx(0), self.canvas.canvasy(0))
        x1, y1 = self._c2w(self.canvas.canvasx(w), self.canvas.canvasy(h))
        return x0, y0, x1, y1

    def _card_hits(self, x, y, h):
        """把卡片放在 (x, y)（高 h）会不会压住已有卡片"""
        for nd in self.flow["nodes"].values():
            if (x < nd["x"] + CARD_W and nd["x"] < x + CARD_W
                    and y < nd["y"] + card_h(nd) and nd["y"] < y + h):
                return True
        return False

    def _spot_in_view(self, h=CARD_H):
        """可见视口右上角的一个空位（模型坐标）。
        新节点要"出现在眼前"——以前是放到整个画布最右侧（内容之外），得滚半天才找得到。"""
        _x0, y0, x1, _y1 = self._visible_rect()
        x = self._snap(x1 - CARD_W - GRID)
        y = self._snap(y0 + GRID)
        for _ in range(30):
            if not self._card_hits(x, y, h):
                break
            y = self._snap(y + h + GRID)
        return x, y

    def _chain_max_x(self):
        """主列里最靠右的 x —— 各类「侧区」列的基准（参数列 / 回环列 / 草稿列）。
        不含离链节点、也不含回环节点：它俩本来就住在侧区里，
        算进来会互相推着往右跑（回环列一算完就落进自己的基准里，参数列被推出去 300+px）。"""
        ch = self.flow["chain"]
        idx = {nid: i for i, nid in enumerate(ch)}
        xs = [nd["x"] for nid, nd in self.flow["nodes"].items()
              if nid in idx and self._loop_target(nid, idx) is None]
        return max(xs) if xs else float(TIDY_X)

    def _param_col_x(self):
        """离链【输入】节点的 x：回环列（有的话）右侧的"参数区"一列"""
        base = self._loop_col_x() if self._has_loop_nodes() else self._chain_max_x()
        return self._snap(base + CARD_W + 120)

    def _has_loop_nodes(self):
        """链上有没有"回环节点"（出口指回链序上方的，见 _loop_target）"""
        ch = self.flow["chain"]
        idx = {nid: i for i, nid in enumerate(ch)}
        return any(self._loop_target(nid, idx) is not None for nid in ch)

    def _loop_col_x(self):
        """回环节点（滑动重试那种）那一列：紧挨主列右侧 —— 贴着它的目标，回环线才短"""
        return self._snap(self._chain_max_x() + CARD_W + 60)

    def _param_col_pos(self):
        """参数区里的下一个空位（排在已有【输入】节点下面）"""
        x = self._param_col_x()
        ys = [nd["y"] for nd in self.flow["nodes"].values()
              if nd.get("type") in ("input", "pick") and abs(nd["x"] - x) < CARD_W]
        y = (max(ys) + CARD_H + 26) if ys else GRID
        return x, self._snap(y)

    def _draft_col_x(self):
        """未接入流程的节点（点节点库时没选中任何节点 → 建出来先不接）的 x：
        「整理布局」把它们收到参数区右边那一列（新建时是放在可见视口右上角）"""
        return self._snap(self._param_col_x() + CARD_W + 120)

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

    # ---------- 平移：空白处左键拖 / 中键拖（scan_mark/scan_dragto，按住哪就跟着走） ----------

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
        self._open_frame_window()          # 抓帧 → 直接在独立窗口里显示，方便取点
        self.log(f"已抓帧 {path}（已在「帧画面」窗口里打开，可直接取点）")

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
        self._frame_img = img                     # 原图：帧窗口用这份（画布上用压暗的）
        self._fw_marks = []                       # 新帧 → 旧标记清掉
        dark = Image.new("RGB", img.size, (24, 26, 34))
        self.bg_pil = Image.blend(dark, img, 0.62)
        self._update_frame_label()
        self.redraw()
        self._refresh_frame_window()
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
        self._rects_cache = None      # 卡片位置可能刚变过，走线用的矩形缓存重建
        self._apply_zoom_fonts()
        ox, oy = 44, 44
        # 网格与滚动区：内容之外至少留 CARD_MARGIN（5 格），并且不小于当前视口 ——
        # 这样"空的地方"也能滚过去放节点。以前只留 3 格左右，画布看着就小。
        z0 = self.zoom or 1.0
        max_x, max_y = 1400, 1600
        try:
            max_x = max(max_x, self.canvas.winfo_width() / z0)
            max_y = max(max_y, self.canvas.winfo_height() / z0)
        except tk.TclError:
            pass
        if self.bg_pil is not None and self.show_bg.get():
            max_y = max(max_y, oy + FRAME_DISP_H + 80)
        for nd in self.flow["nodes"].values():
            max_x = max(max_x, nd["x"] + CARD_W + CARD_MARGIN)
            max_y = max(max_y, nd["y"] + card_h(nd) + CARD_MARGIN)
        step = GRID
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
            if a.get("type") == "branch":
                # 【分支】没有"直落"出口：命中/未中都要靠拖 ✓/✗ 球连线。
                # 所以链上那条箭头在这里是一条【不存在的边】—— 画成截止标记，
                # 画布所见必须等于生成结果。
                max_x = max(max_x, self._draw_cut_off(
                    a["x"] + CARD_W / 2, a_bottom + 30, y2,
                    "branch-no-fallthrough", cur))
            elif linear_successor(self.flow, cur) == nxt_id:
                ecol, ew = self._edge_style(THEME["arrow"], cur, nxt_id)
                # 接入点挑最近的边（源在侧边时就从侧边进，不用绕到顶上再折回来），
                # 走线绕开卡片（否则会被卡片盖住，看不出箭头方向）。
                side, (ax, ay) = self._anchor(nxt_id, x1, y1)
                c.create_line(*self._flat(self._route(x1, y1, side, ax, ay,
                                                      self_ids=(cur, nxt_id))),
                              smooth=False, width=ew, arrow=tk.LAST, fill=ecol,
                              arrowshape=ARROW_SHAPE, splinesteps=24)
            else:
                # ★ v4：边是显式的 nd["next"]，链序只是显示顺序 —— 所以"链序后面还有
                #   节点、但这个节点没有下一个"是【正常状态】（它到此结束），不用打标记。
                #   只有真正"被抑制"的出口（枝干无直落、分支内容叶、收口节点、老文件的
                #   断开标记）才画 ⛔ 说明。以前一律画，画布上就凭空多出一个
                #   "⛔ 此处不向下继续"，看着像"这两个节点还连着"。
                reason = _suppress_reason(self.flow, cur)
                if reason is not None:
                    cut_y = a_bottom + (30 if a["type"] == "switch" else 0)
                    max_x = max(max_x, self._draw_cut_off(
                        a["x"] + CARD_W / 2, cut_y, y2, reason, cur))
        # ★ 显式 next 的边必须【全部】画出来。v4 起边是 nd["next"]，链序只决定显示顺序，
        #   所以"下一个"指回链序上方的情形（滑动重试那种回环）在链序里配不到下一项 ——
        #   以前这种边一条都不画，画布上就是断头路（生成结果里它明明是连着的）。
        ch_next = {ch[i]: ch[i + 1] for i in range(len(ch) - 1)}
        for nid in ch:
            nd = self.flow["nodes"].get(nid) or {}
            if nd.get("type") in ("branch", "switch"):
                continue              # 它们的出口是 ✓/✗ 球与候选球，另有画法
            tgt = nd.get("next")
            if not tgt or tgt not in self.flow["nodes"]:
                continue
            if tgt == ch_next.get(nid):
                continue              # 链序紧跟着的那个：上面那段已经画了
            if _suppress_reason(self.flow, nid) is not None:
                continue              # 被抑制的出口：上面画的是 ⛔ 标记
            a = nd
            x1 = a["x"] + CARD_W / 2
            y1 = a["y"] + (self._sw_h(a) if a["type"] == "switch" else CARD_H)
            ecol, ew = self._edge_style(THEME["arrow"], nid, tgt)
            side, (ax, ay) = self._anchor(tgt, x1, y1)
            c.create_line(*self._flat(self._route(x1, y1, side, ax, ay,
                                                  self_ids=(nid, tgt))),
                          smooth=False, width=ew, arrow=tk.LAST, fill=ecol,
                          arrowshape=ARROW_SHAPE, splinesteps=24)
            max_x = max(max_x, x1)
        # 分支/枝干出口连线。
        # ★ 离链的分支/枝干【也要画】：出口是已经写进流程文件的设置，只在链上才画的话，
        #   把 ✓ 球拖到目标上（日志说"已连接"、字段也真的写进去了）画布上却什么都看不到，
        #   会以为根本没连上、反复重连。离链的用灰虚线画 —— 一眼看出"这条边现在不生效"
        #   （该节点自己还挂着 ⛔ 未接入流程 徽标，说的是同一件事）。
        exit_nodes = [n for n in ch if n in self.flow["nodes"]]
        exit_nodes += [n for n in self.flow["nodes"] if n not in ch
                       and (self.flow["nodes"][n] or {}).get("type") in ("branch",
                                                                          "switch")]
        for nid in exit_nodes:
            nd = self.flow["nodes"][nid]
            off = nid not in ch                   # 离链：边不生效 → 灰虚线
            dkw = {"dash": (6, 4)} if off else {}
            exits = exits_of(self.flow, nid)
            if nd["type"] == "branch":
                for port, target, color in (("hit_next", exits["hit"], THEME["ok"]),
                                            ("miss_next", exits["miss"], THEME["err"])):
                    sx, sy = self._port_pos(nd, port)
                    if target and target in self.flow["nodes"]:
                        t = self.flow["nodes"][target]
                        ex, ey = t["x"] + CARD_W / 2, t["y"]
                        ecol, ew = ((THEME["text_dim"], 2) if off
                                    else self._edge_style(color, nid, target))
                        # 进入目标卡片的那条边挑最近的（见 _anchor），走线绕开卡片
                        # （见 _route）：否则线会从卡片底下穿过，只看得见一截线头
                        # 和一个箭头，方向没法认。
                        side, (ax, ay) = self._anchor(target, sx, sy)
                        c.create_line(*self._flat(self._route(sx, sy, side, ax, ay,
                                                              self_ids=(nid, target))),
                                      width=ew, fill=ecol, arrow=tk.LAST,
                                      arrowshape=ARROW_SHAPE, splinesteps=24, **dkw)
                        # 球旁边标出"它连到哪个节点"（编号）：同名节点多的时候，
                        # 光看线根本认不出连的是哪一个（而且线可能跑出可视区）。
                        c.create_text(sx + 12, sy - 13 if port == "hit_next" else sy + 15,
                                      anchor="w", font=self.f_sm,
                                      fill=THEME["text_dim"] if off else color,
                                      text=f"#{node_no(self.flow, target)}")
                    else:
                        # 没连线就是"到此结束"：分支出口只认显式连线，没有"自动走下一个"
                        txt = "未连线：命中即结束" if port == "hit_next" else "未连线：未中即结束"
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
                    col = branch_color(ci)
                    if tgt and tgt in self.flow["nodes"]:
                        t = self.flow["nodes"][tgt]
                        ex, ey = t["x"] + CARD_W / 2, t["y"]
                        # 从球走到目标卡片顶部；走线绕开卡片（见 _route）。
                        # 以前是「先向下、再横向、最后扎进顶部」的固定形状：几条线容易
                        # 交叉成麻花，横向段还会从别的卡片底下穿过。
                        ecol, ew = ((THEME["text_dim"], 2) if off
                                    else self._edge_style(col, nid, tgt))
                        side, (ax, ay) = self._anchor(tgt, sx, sy)
                        pts = self._route(sx, sy, side, ax, ay, self_ids=(nid, tgt))
                        c.create_line(*self._flat(pts), width=ew, fill=ecol,
                                      arrow=tk.LAST, arrowshape=ARROW_SHAPE,
                                      splinesteps=24, **dkw)
                        lx, ly = pts[1] if len(pts) > 1 else (sx, sy)
                        c.create_text(lx + 7, ly - 9, anchor="w",
                                      fill=THEME["text_dim"] if off else col,
                                      font=self.f_sm, text=str(ci + 1))
                    else:
                        c.create_line(sx, sy, sx, sy + 22, fill=col, width=2)
                        _round_rect(c, sx - 26, sy + 24, sx + 34, sy + 46, 5,
                                    fill="#14161d", outline=THEME["card_line"])
                        c.create_text(sx + 4, sy + 35, anchor="center", fill=col,
                                      font=self.f_sm, text="→结束")
                    max_x = max(max_x, sx + 190)
                mn = exits["miss"]
                sx, sy = self._port_pos(nd, "miss")
                if mn and mn in self.flow["nodes"]:
                    t = self.flow["nodes"][mn]
                    ex, ey = t["x"] + CARD_W / 2, t["y"]
                    ecol, ew = ((THEME["text_dim"], 2) if off
                                else self._edge_style(THEME["err"], nid, mn))
                    side, (ax, ay) = self._anchor(mn, sx, sy)
                    c.create_line(*self._flat(self._route(sx, sy, side, ax, ay,
                                                          self_ids=(nid, mn))),
                                  width=ew, fill=ecol, arrow=tk.LAST,
                                  arrowshape=ARROW_SHAPE, splinesteps=24, **dkw)
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
        # 【输入】注入线 / 【选择】选项目标线：右侧小球 → 目标节点左侧。虚线 + 专属色 ——
        # 它们只表示"这个参数作用在哪个节点上"，与流程顺序无关，所以画法与流程连线区分开。
        # ★ 目标解析统一走 resolve_param_target()：拖线落地用的是同一个函数，
        #   不然会出现"画布上画着这条线、拖线却判断成没连过"这种对不上的情况。
        for nd_id, nd in self.flow["nodes"].items():
            if nd.get("type") not in ("input", "pick"):
                continue
            sx, sy = self._port_pos(nd, "inject")
            tgt_ids = []
            if nd.get("type") == "input":
                tgt_ids += list(input_targets(nd))
                for raw in parse_raw_targets(nd.get("props") or {}):
                    tgt_ids.append(resolve_param_target(self.flow, raw))
            else:
                tgt_ids += pick_case_targets(self.flow, nd)
            for tgt in [t for t in dict.fromkeys(tgt_ids) if t]:
                t = self.flow["nodes"].get(tgt)
                if not isinstance(t, dict):
                    continue
                ex, ey = t["x"], t["y"] + card_h(t) / 2
                # 接入点同样挑最近的一条边、走线同样躲开卡片
                side, (ax, ay) = self._anchor(tgt, sx, sy)
                pts = self._route(sx, sy, side, ax, ay, self_ids=(nd_id, tgt))
                c.create_line(*self._flat(pts), width=2, dash=(6, 4),
                              fill=INJECT_COLOR, arrow=tk.LAST,
                              arrowshape=ARROW_SHAPE, splinesteps=24)
                c.create_text(ex - 14, ey - 12, anchor="e", font=self.f_sm,
                              fill=INJECT_COLOR,
                              text=f"#{node_no(self.flow, tgt)}")
            max_x = max(max_x, sx + 80)
        # 链上节点按链序画，离链的【输入】节点随后画 —— 它们也要看得见
        order = [n for n in ch if n in self.flow["nodes"]]
        order += [n for n in self.flow["nodes"] if n not in ch]
        for nid in order:
            self._draw_node(nid)
        self._draw_branch_labels()
        if self.band is not None:
            # 右键拖框的矩形（画在最上层；用点阵填充假装半透明）
            bx0, by0, bx1, by1 = self.band
            c.create_rectangle(min(bx0, bx1), min(by0, by1),
                               max(bx0, bx1), max(by0, by1),
                               outline=THEME["sel"], width=1, dash=(6, 4),
                               fill=THEME["sel"], stipple="gray12")
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
        in_multi = nid in self.multi
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
        idx = node_no(self.flow, nid)
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
                # 候选没有自己的识别目标了：这行显示它从连到的分支推导出的判定条件
                line = f"{ci + 1}. {cand_cond_short(self.flow, cnd)}"
                if len(line) > 18:          # 卡片内按宽度截断，超时移到最右
                    line = line[:17] + "…"
                c.create_text(x + 16, cy, anchor="w", font=self.f_sm,
                              fill=THEME["text"], text=line, tags=tags)
                c.create_text(x + CARD_W - 12, cy, anchor="e", font=self.f_sm,
                              fill=THEME["text_dim"], text=f"{cnd['timeout']}ms",
                              tags=tags)
            # 下沿的球：候选（绿，带序号）/ 全部未中（红）/ 新增分支（＋）
            # —— 拖球到目标节点即可连出该分支；＋ 球拖出去会新建一个候选
            for ci in range(len(cands)):
                hx, hy = self._port_pos(nd, f"cand{ci}")
                c.create_oval(hx - 10, hy - 10, hx + 10, hy + 10,
                              fill="#1c2b1f", outline="")
                col = branch_color(ci)
                c.create_oval(hx - 6, hy - 6, hx + 6, hy + 6, fill=col,
                              outline="#ffffff", width=1,
                              tags=("port", f"port:{nid}:cand{ci}"))
                c.create_text(hx, hy - 15, font=self.f_sm, fill=col,
                              text=str(ci + 1), tags=tags)
            mx, my = self._port_pos(nd, "miss")
            c.create_oval(mx - 10, my - 10, mx + 10, my + 10, fill="#2e1c1e", outline="")
            c.create_oval(mx - 6, my - 6, mx + 6, my + 6, fill=THEME["err"],
                          outline="#ffffff", width=1,
                          tags=("port", f"port:{nid}:miss"))
            c.create_text(mx, my - 15, font=self.f_sm, fill=THEME["err"],
                          text="✗", tags=tags)
            # 「＋」球：拖到任意节点 → 新建一个候选并直接连过去（连完会再冒一个）
            nx, ny = self._port_pos(nd, "new")
            c.create_oval(nx - 10, ny - 10, nx + 10, ny + 10,
                          fill="#20242f", outline="")
            c.create_oval(nx - 6, ny - 6, nx + 6, ny + 6, fill="#8fa0c8",
                          outline="#ffffff", width=1,
                          tags=("port", f"port:{nid}:new"))
            c.create_text(nx, ny - 15, font=self.f_sm, fill="#b9c6e8",
                          text="＋", tags=tags)
        else:
            # 摘要不再硬截断（以前 [:12] 会把 "起 (1233,364)→(…" 直接切掉半个坐标）；
            # 太长就按卡片宽度自动换行（width= 是 Tk 的换行宽度）
            c.create_text(x + 20, y + 30, anchor="nw", fill=THEME["text_dim"],
                          font=self.f_sm, width=CARD_W - 34,
                          text=spec["summary"](nd["props"]), tags=tags)
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
        elif in_multi:
            # 右键框选出来的（还没删）：虚线黄框，与"单选"区分开
            _round_rect(c, x - 3, y - 3, x1 + 3, y1 + 3, 14, outline=THEME["sel"],
                        width=1, fill="", dash=(5, 3))
        if nid not in self.flow["chain"] and nd["type"] not in ("input", "pick"):
            # 没接进流程：既不生成也不执行 —— 画个显眼标记，别让人以为它在跑
            _round_rect(c, x + 8, y1 + 6, x + 8 + 214, y1 + 30, 5,
                        fill="#2e1c1e", outline=THEME["err"])
            c.create_text(x + 15, y1 + 18, anchor="w", fill=THEME["err"],
                          font=self.f_sm, text="⛔ 未接入流程（不执行）", tags=tags)
        if self._has_next_ball(nid):
            # 「下一个」小球（下沿正中）：拖到目标节点 = 接上/改接下一个；拖到空白 = 到此结束。
            # 以前只有【分支】的 ✓/✗ 能拖，「下一个」只能去右侧「连接」下拉里选 ——
            # 一个个画线的时候，拖球比翻下拉快得多。
            hx, hy = self._port_pos(nd, "next")
            c.create_oval(hx - 10, hy - 10, hx + 10, hy + 10,
                          fill="#1b1f2a", outline="")
            c.create_oval(hx - 6, hy - 6, hx + 6, hy + 6, fill=THEME["arrow"],
                          outline="#ffffff", width=1,
                          tags=("port", f"port:{nid}:next"))
        if nd["type"] in ("input", "pick"):
            # 「注入」小球（右侧中间）：【输入】拖到目标节点 = 这个参数注入过去；
            # 【选择】拖到目标节点 = 把这个节点挂成某个选项的「目标节点」。
            # 两者都是"拖到已经连着的节点 = 取消"，线只表示参数作用在哪，不代表流程顺序。
            hx, hy = self._port_pos(nd, "inject")
            c.create_oval(hx - 10, hy - 10, hx + 10, hy + 10,
                          fill="#241c2e", outline="")
            c.create_oval(hx - 6, hy - 6, hx + 6, hy + 6, fill=INJECT_COLOR,
                          outline="#ffffff", width=1,
                          tags=("port", f"port:{nid}:inject"))
            c.create_text(hx - 10, hy - 16, anchor="e", font=self.f_sm,
                          fill=INJECT_COLOR, text="注入", tags=tags)
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

    # ---------- 连线走线：绕开卡片 ----------

    EDGE_CLEAR = 14          # 线与卡片边缘之间留的余量

    def _flat(self, pts):
        """[(x,y), ...] → [x,y,x,y,...]（Tk create_line 要的扁平坐标）"""
        out = []
        for px, py in pts:
            out += [px, py]
        return out

    def _card_rects(self, skip=()):
        """所有卡片的矩形（模型坐标）：[(nid, x0, y0, x1, y1)]。

        缓存一次、redraw 开头重建（拖拽时每帧都要问几百次"这里被卡片挡了吗"，
        每次重算一遍所有卡片会明显卡手）。skip 参数保留兼容，判断时用 body_only。"""
        r = getattr(self, "_rects_cache", None)
        if r is None:
            r = []
            for nid, nd in self.flow["nodes"].items():
                if not isinstance(nd, dict):
                    continue
                r.append((nid, nd["x"], nd["y"], nd["x"] + CARD_W,
                          nd["y"] + card_h(nd)))
            self._rects_cache = r
        return r

    def _seg_blocked(self, x1, y1, x2, y2, body_only=()):
        """这条线段会不会压到卡片（本方法只用于轴对齐的走线，所以用包围盒判就够）。

        body_only 里的卡片（源节点、目标节点自己）只判"真的从身体里穿过去" ——
        贴边、从边上出发都是允许的；其它卡片则要留出 EDGE_CLEAR 的余量。
        ★ 判据必须是"把卡片【扩大】余量后与线段求交"：以前写反了（缩小卡片），
          于是"离卡片右边缘 14px"的竖线被判成不挡 —— 可它其实还在卡片里面
          （卡片宽 240px），画出来就是一条从整列卡片身上穿过去的线。"""
        C = getattr(self, "_clear_now", None) or self.EDGE_CLEAR
        lo_x, hi_x = min(x1, x2), max(x1, x2)
        lo_y, hi_y = min(y1, y2), max(y1, y2)
        for nid, cx0, cy0, cx1, cy1 in self._card_rects():
            m = 0.0 if nid in body_only else C
            if (lo_x < cx1 + m and hi_x > cx0 - m
                    and lo_y < cy1 + m and hi_y > cy0 - m):
                return True
        return False

    # ---------- 连线接入点：四条边里挑最近的一条 ----------

    def _ball_sides(self, nid):
        """这个节点的卡片上"带连接球"的那几条边。

        ★ 这些边不参与接入点的挑选：那条边留给球自己的连线用，否则别的线会贴着球
          穿过去（分支的 ✓/✗ 球在右边、枝干的候选球在下沿、循环和输入的球在右边、
          普通节点的「下一个」球在下沿正中）。"""
        nd = self.flow["nodes"].get(nid) or {}
        t = nd.get("type")
        sides = set()
        if t in ("branch", "loop"):
            sides.add("right")
        if t == "switch":
            sides.add("bottom")
        if t in ("input", "pick"):
            sides.add("right")
        if self._has_next_ball(nid):
            sides.add("bottom")        # 下沿正中的「下一个」球
        return sides

    def _has_next_ball(self, nid):
        """这个节点该不该画下沿的「下一个」小球。

        只有【真的有"下一个"】的类型才画：【输入】是参数声明（靠注入线）、
        【枝干判定】的出口是候选球/✗球、【分支】走 ✓/✗、【公共节点(收口)】进入即终止
        —— 这几种语义上都没有"下一个"，画个球会让人以为拖了就能接上（拖了也不生成那条边）。"""
        nd = self.flow["nodes"].get(nid) or {}
        if nd.get("type") in ("input", "pick", "switch", "branch", "common"):
            return False
        return nid not in _switch_content_leaves(self.flow)

    def _anchor(self, nid, sx, sy):
        """目标卡片上离源最近的那个接入点 —— 左右上下四条边各取一个候选点，选最近的。

        返回 (side, (x, y))。带球的那条边不参与（见 _ball_sides）。
        ★ 以前不管源在哪边，一律从目标卡片【顶部正中】进：源在卡片正侧面时，
          线要先绕到顶上再折回来，既长又容易被卡片挡住看不出方向。"""
        nd = self.flow["nodes"][nid]
        x, y = nd["x"], nd["y"]
        w, hgt = CARD_W, card_h(nd)
        pad = 14.0        # 别顶到角上，留一点余量
        cx = min(max(sx, x + pad), x + w - pad)
        cy = min(max(sy, y + pad), y + hgt - pad)
        cand = {"top": (cx, y), "bottom": (cx, y + hgt),
                "left": (x, cy), "right": (x + w, cy)}
        for s in self._ball_sides(nid):
            cand.pop(s, None)
        side = min(cand, key=lambda s: (cand[s][0] - sx) ** 2 + (cand[s][1] - sy) ** 2)
        return side, cand[side]

    def _route(self, sx, sy, side, ex, ey, self_ids=()):
        """从 (sx,sy) 走到目标卡片 side 那条边上的接入点 (ex,ey)（side=top/bottom/left/right）。

        做法：把整张画布按 side 旋转/翻转到"接入点朝上"的标准姿势，用同一套走线规则
        算完再转回来 —— 四条边共用一套逻辑，不必各写一遍。
        ★ 以前固定接顶部：源在卡片侧边时线要先绕上去再折回来，又长又容易被卡片盖住。"""
        def fwd(px, py):
            if side == "top":
                return px, py
            if side == "bottom":
                return px, -py
            if side == "left":
                return py, px
            return py, -px            # right

        def inv(px, py):
            if side == "top":
                return px, py
            if side == "bottom":
                return px, -py
            if side == "left":
                return py, px
            return -py, px            # right（(x,y)→(y,-x) 的逆）

        crects = []
        for nid, x0, y0, x1, y1 in self._card_rects():
            a, b = fwd(x0, y0), fwd(x1, y1)
            crects.append((nid, min(a[0], b[0]), min(a[1], b[1]),
                           max(a[0], b[0]), max(a[1], b[1])))
        csx, csy = fwd(sx, sy)
        cax, cay = fwd(ex, ey)
        saved = getattr(self, "_rects_cache", None)
        saved_c = getattr(self, "_clear_now", None)
        self._rects_cache = crects          # 走线期间只在标准姿势下做遮挡判断
        pts = None
        try:
            # 先按标准余量找；布局挤得很紧（卡片间只有十几像素）时，
            # 换更小的余量再试 —— 宁可贴得近一点，也别无路可走只能直穿卡片。
            for clear in (self.EDGE_CLEAR, 8.0, 4.0, 1.0):
                self._clear_now = clear
                pts = self._route_top(csx, csy, cax, cay, self_ids)
                if pts is not None:
                    break
        finally:
            self._rects_cache = saved
            self._clear_now = saved_c
        if pts is None:
            # 兜底：真无路可走（正常情况下到不了这里），宁可压一下也不能不画
            ymid = min(cay - 2, (csy + cay) / 2)
            pts = [(csx, csy), (csx, ymid), (cax, ymid), (cax, cay - 2)]
        return [inv(px, py) for px, py in pts]

    def _route_top(self, sx, sy, ex, ey, self_ids=()):
        """标准姿势下的走线：从 (sx,sy) 走到卡片【顶面】(ex,ey)（ex 是接入点的 x）。
          ① 同一列、中间空 → 一条竖线；
          ② 竖 → 横 → 竖（横向道必须落在目标顶面之上，否则扎进顶部那一竖会穿过卡片）；
          ③ 绕行：先离开源（上下退 / 横向退都试），走卡片列外侧的竖道，
             再挑一条横向道从目标上方扎进顶部；
          ④ 兜底（宁可压一下也不能不画）。
        ★ 以前固定走"中间那条横线"：横向段会从卡片底下穿过，卡片一盖就只剩一截线头
          和一个箭头，看不出箭头往哪指。"""
        C = self.EDGE_CLEAR
        end = (ex, ey - 2)
        body = tuple(self_ids)      # 源/目标：只判"穿透身体"，允许贴边和从边上出发

        def ok(pts):
            return all(not self._seg_blocked(ax, ay, bx, by, body_only=body)
                       for (ax, ay), (bx, by) in zip(pts, pts[1:]))

        # ① 同一列、中间没东西：直接竖着连
        if abs(sx - ex) < 8 and not self._seg_blocked(sx, sy, ex, ey - 2,
                                                      body_only=body):
            return [(sx, sy), end]
        # ② 竖 → 横 → 竖：横向道先按【源所在的那条】试（最省：只有一横一竖，
        #    以前压根不试它 —— 源在右侧、目标在左下时会被判"绕行"，实测同一条边
        #    1668px vs 最优 764px），不行再从中间往上找，且必须高于目标顶面（留余量）
        seen = set()
        for y in ([sy] + [(min(sy, ey) + max(sy, ey)) / 2]
                  + [ey - C * k for k in range(1, 24)]):
            if y > ey - C or y in seen:
                continue
            seen.add(y)
            pts = [(sx, sy), (sx, y), (ex, y), end]
            if ok(pts):
                return pts
        # ③ 绕行：外侧竖道 + 一条横向道
        xs = [nd["x"] for nd in self.flow["nodes"].values() if isinstance(nd, dict)]
        if xs:
            # 候选竖道：每一列的左右两侧 + 整体最外侧；按"离源节点最近"排序 ——
            # 先用近的（绕得少、也不会跑到可视区外面去）
            col_xs = sorted({float(x) for x in xs})
            lanes_x = [min(col_xs) - C * 3, max(col_xs) + CARD_W + C * 3]
            lanes_x += [x - C * 3 for x in col_xs]
            lanes_x += [x + CARD_W + C * 3 for x in col_xs]
            lanes_x = sorted(set(lanes_x), key=lambda x: abs(x - sx))
            lanes_y = [ey - C * k for k in (1, 2, 3, 5, 8)]
            # ③a 先从源上下退出去（源在卡片中间、上下都被挡时用横向退，见 ③b）
            for xl in lanes_x:
                for k in (1, 2, 4, 8):
                    for sgn in (1, -1):
                        for y_in in lanes_y:
                            pts = [(sx, sy), (sx, sy + sgn * C * k), (xl, sy + sgn * C * k),
                                   (xl, y_in), (ex, y_in), end]
                            if ok(pts):
                                return pts
            # ③b 横向退到外侧竖道（源就在卡片边缘：上下都是卡片，只能先横着出来）
            for xl in lanes_x:
                for y_in in lanes_y:
                    pts = [(sx, sy), (xl, sy), (xl, y_in), (ex, y_in), end]
                    if ok(pts):
                        return pts
        # 找不到干净走法（由 _route 换更小的余量再试，最后才兜底）
        return None

    def _port_pos(self, nd, port):
        if nd["type"] in ("input", "pick"):
            # 【输入】/【选择】的「注入」小球都在卡片右侧中间
            return nd["x"] + CARD_W, nd["y"] + CARD_H * 0.5
        if nd["type"] == "loop":
            if port == "next":
                # 「下一个」球在下沿正中（与链上箭头的起点同一个位置）
                return nd["x"] + CARD_W * 0.5, nd["y"] + CARD_H
            return nd["x"] + CARD_W, nd["y"] + CARD_H * 0.5
        if nd["type"] == "switch":
            # 枝干的球排在下沿：候选球（绿，带序号）+ 全部未中球（红）+ 新增球（＋）
            cands = parse_switch_cands(nd.get("props", {}).get("candidates"))
            n = len(cands)
            step = min(32.0, (CARD_W - 44) / max(1, n + 2))
            by = nd["y"] + self._sw_h(nd)
            if port == "miss":
                return nd["x"] + 22 + step * n, by
            if port == "new":
                return nd["x"] + 22 + step * (n + 1), by
            i = int(port[4:])   # "cand0" → 0
            return nd["x"] + 22 + step * i, by
        if port == "next":
            # 「下一个」球在下沿正中 —— 就是链上直落箭头的起点，拖它 = 接/改下一个
            return nd["x"] + CARD_W * 0.5, nd["y"] + CARD_H
        y = nd["y"] + (CARD_H * 0.32 if port == "hit_next" else CARD_H * 0.68)
        return nd["x"] + CARD_W, y

    def _sw_h(self, nd):
        """switch 卡片高度（标题 + 候选行 + 底部的球那一行）；与 card_h() 同源"""
        return card_h(nd)

    def _draw_cut_off(self, cx, y_from, y_to, reason, owner=None):
        """画「此处不向下继续」的显眼标记：红色虚线短桩 + 截止横杠 + ⛔ 徽标。
        与 build_pipeline 共用 linear_successor/_suppress_reason：生成被截断的出口，
        画布上也不得画成连上的样子。返回标记右边界（供 scrollregion 用）。"""
        c = self.canvas
        gap = max(14.0, y_to - y_from)
        stub = min(14.0, gap * 0.45)
        tip = y_from + 2 + stub
        # ★ 别伸进下一张卡片里：卡片挨得近时（步长 92、卡高 60 → 间隙只剩 32px），
        #   截止横杠和 ⛔ 徽标（高 22px）会压到下一张卡片的顶边上 —— 画布上就成了
        #   "连线与节点重合"。这里把标记整体收进两个节点之间的空隙里。
        tip = min(tip, y_to - 12.0)
        tip = max(tip, y_from - 12.0)
        hot = self.sel is not None and self.sel == owner
        ec = THEME["sel"] if hot else THEME["err"]
        c.create_line(cx, y_from + 2, cx, tip, fill=ec, width=3 if hot else 2,
                      dash=(5, 3))
        c.create_line(cx - 8, tip, cx + 8, tip, fill=ec, width=2)
        txt = {"switch-no-fallthrough": "⛔ 枝干无直落出口（走 ✓ 出口）",
               "branch-no-fallthrough": "⛔ 分支无直落出口（走 ✓/✗ 出口）",
               "node-cut": "⛔ 此处已断开（不接下一个）",
               "switch-content-leaf": "⛔ 分支内容到此结束，不接下一节点",
               "common-terminal": "⛔ 收口节点，进入后不返回本流程",
               }.get(reason, "⛔ 此处不向下继续")
        tw = 12 * len(txt) + 14
        bx = cx + 16
        _round_rect(c, bx, tip - 11, bx + tw, tip + 11, 5,
                    fill="#2a1114", outline=ec, width=2 if hot else 1)
        c.create_text(bx + tw / 2, tip, fill="#ffe9a0" if hot else "#ffb3b3",
                      font=self.f_sm, text=txt)
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
        ecol, ew = self._edge_style(THEME["warn"], nid, body[-1])
        c.create_line(sx, sy, mx, sy, mx, ey, ex + 2, ey, smooth=True, dash=(6, 4),
                      width=ew, fill=ecol, arrow=tk.LAST,
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
                # 球上的序号/✗/＋ 已在 _draw_node 里画好，这里只标一次「出口」说明
                nx, ny = self._port_pos(nd, "new")
                c.create_text(nx, ny + 14, anchor="n", fill="#b9c6e8",
                              font=self.f_sm, text="拖我加分支")

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

    def _maybe_close_tpl_pop(self, _e=None):
        """点主窗口任意位置时收起模板补全弹窗（弹窗是独立 Toplevel，
        点它自己不会走到这里，所以不影响在列表里多选）"""
        if self._tpl_pop is not None:
            self._close_tpl_pop()

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
        MAX_ROWS = 16
        lb = tk.Listbox(top, bg=THEME["field"], fg=THEME["text"],
                        selectbackground=THEME["accent"], selectforeground=THEME["text"],
                        relief="flat", highlightthickness=1,
                        highlightbackground=THEME["card_line"], font=FONT_SM,
                        activestyle="none", exportselection=False,
                        selectmode=("multiple" if multi else "browse"))
        for it in items[:MAX_ROWS]:
            lb.insert("end", it)
        if multi:
            for i, it in enumerate(items[:MAX_ROWS]):
                if it in cur_set:
                    lb.selection_set(i)
        sb = ttk.Scrollbar(top, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        if len(items) > MAX_ROWS:
            sb.pack(side="right", fill="y")
        lb.pack(side="left", fill="both", expand=True)
        lb.bind("<MouseWheel>",
                lambda e: lb.yview_scroll(int(-e.delta / 120), "units"))
        # 位置：默认贴在控件下方；下方放不下就往上方弹；最后夹进屏幕内。
        # 以前直接拿 winfo_rootx/y 定位，控件还没被映射时它们是 0 →
        # 弹窗会飘到窗口左上角（就是那个"点了「选」就跑出来"的现象）。
        self.root.update_idletasks()
        w = max(cmb.winfo_width(), 260)
        h = min(len(items), MAX_ROWS) * 21 + 6
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 锚点控件若不在可见区域（在属性面板滚动区之外时 Tk 会取消映射它），
        # winfo_rootx/y 会返回 0 —— 直接拿来定位会让弹窗飘到屏幕左上角。
        # 这种情况退回用主窗口位置，保证弹窗永远出现在人看得见的地方。
        if not cmb.winfo_ismapped() or cmb.winfo_rootx() <= 1:
            x = self.root.winfo_rootx() + 60
            y = self.root.winfo_rooty() + 90
        else:
            x = cmb.winfo_rootx()
            y = cmb.winfo_rooty() + cmb.winfo_height()
        if y + h > sh - 36:                      # 下方不够 → 放到控件上方
            y = max(0, cmb.winfo_rooty() - h) if cmb.winfo_ismapped() else y
        x = max(0, min(x, sw - w - 4))
        y = max(0, min(y, sh - h - 4))

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

        def pick_at(e):
            """点一行就【立刻生效】。

            ★ 单选：就取点中的那一行（按 y 坐标取，不看"释放时选中项生效没有"——
              以前绑在 <ButtonRelease-1> 上读 curselection()，未生效时会走"按输入框
              里的半截名字提交"再被恢复成原值，看起来就是点了没反应）。
            ★ 多选（分支的模板图）：点一行 = 勾选/取消，然后把【当前勾选集】整体写回
              节点。以前只挂"回车/失焦"提交，而弹窗是无边框窗口（不抢焦点）——
              回车和失焦都不会来，点外面又只是关掉不提交，于是"点了白点"。"""
            i = lb.nearest(e.y)
            if i is None or i < 0 or i >= len(items):
                return
            if multi:
                def flush():
                    try:
                        sel = [lb.get(k) for k in lb.curselection()]
                    except tk.TclError:
                        return
                    on_pick(",".join(sel))
                lb.after(1, flush)      # 等 Listbox 自己把这一下勾选切换完
                return
            item = items[i]
            self._close_tpl_pop()       # 关掉弹窗（之后再读 lb 就晚了）
            on_pick(item)
        lb.bind("<Button-1>", pick_at)
        if multi:
            lb.bind("<Return>", submit)
            lb.bind("<Double-Button-1>", lambda e: submit())
            lb.bind("<Escape>", lambda e: self._close_tpl_pop())
            self.log("模板候选：点一行即写进节点，可连点几行凑多个候选"
                     "（任一命中即算命中）；Esc 收起列表")
        top.bind("<Destroy>", lambda _e: setattr(self, "_tpl_pop", None)
                 if self._tpl_pop is top else None)
        top.lift()
        top.attributes("-topmost", True)
        # 定位放在最后设置，并在应用后再核一次：override-redirect 窗口偶发不生效，
        # 那时会停在 +0+0（屏幕左上角）——就是"点了选就跑出来一个飘着的小列表"的现象。
        top.wm_geometry("%dx%d+%d+%d" % (w, h, x, y))
        top.update_idletasks()
        if (top.winfo_x(), top.winfo_y()) != (x, y):
            top.wm_geometry("+%d+%d" % (x, y))
            top.update_idletasks()
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
                    var.set(val)          # ★ 输入框跟着定稿值走（失焦会把显示恢复成旧值）
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
            if cur or var.get():
                props["template"] = ""
                var.set("")
                self.redraw()
            return
        if picked:
            parts = split_tpls(picked)
            ok = all(p in self.templates for p in parts)
            if ok:
                var.set(picked)       # ★ 同上：显示与节点保持一致
                if cur != picked:
                    props["template"] = picked
                    self.redraw()
            else:
                var.set(cur)

    def _get_tpl_photo(self, name):
        """模板缩略图（节点卡片显示用）；按 (模板文件 mtime, 当前画布缩放) 缓存。

        ★ 必须按缩放重新生成图片：整个画布最后会 c.scale("all", 0, 0, z, z) 统一放大，
          而 Tk 的 canvas.scale 只缩放坐标、【不会缩放图片】。以前固定按 88×42 生成，
          放大画布时卡片框跟着变大、图却没变 —— 框的右边和下面就空出一块。
        返回 (mtime, photo, 模型宽, 模型高)：模型尺寸 = 像素尺寸 / zoom，
        这样跟着整体缩放之后，框正好贴住图。"""
        path = os.path.join(IMG_DIR, name)
        if not os.path.isfile(path):
            return None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        z = self.zoom or 1.0
        key = (name, round(z, 3))
        cached = self._tpl_img_cache.get(key)
        if cached and cached[0] == mtime:
            return cached
        try:
            im = Image.open(path).convert("RGB")
            w, h = im.size
            scale = min(42 / h, 88 / w)          # 基准显示尺寸（模型坐标）
            dw = max(1, int(round(w * scale * z)))
            dh = max(1, int(round(h * scale * z)))
            photo = ImageTk.PhotoImage(im.resize((dw, dh), Image.NEAREST))
        except Exception:
            return None
        # 换缩放就别留旧的了（一张图 × 每个缩放级别都缓存会越攒越多）
        for k in [k for k in self._tpl_img_cache if k[1] != key[1]]:
            self._tpl_img_cache.pop(k, None)
        self._tpl_img_cache[key] = (mtime, photo, dw / z, dh / z)
        return self._tpl_img_cache[key]

    # ---------- 画布视图：坐标变换 / 缩放 / 平移 ----------

    def _c2w(self, cx, cy):
        """画布坐标 → 模型坐标"""
        z = self.zoom or 1.0
        return cx / z, cy / z

    def _w2c(self, wx, wy):
        """模型坐标 → 画布坐标"""
        z = self.zoom or 1.0
        return wx * z, wy * z

    def _edge_style(self, base_color, *nids):
        """连线配色：(颜色, 线宽)。与当前选中节点相连的线加粗高亮成黄色，
        这样点一个节点就能看清"它连出去/连进来"的是哪几条，不用在麻花里找。"""
        if self.sel is not None and self.sel in nids:
            return THEME["sel"], 4
        return base_color, 2

    def _next_num(self):
        """下一个可用编号（只看已有最大号，重排/删除都不会回收号，避免两个节点同号）"""
        return max((nd.get("num", 0) for nd in self.flow["nodes"].values()
                    if isinstance(nd.get("num"), int)), default=0) + 1

    def _snap(self, v):
        """吸附到最近的网格交叉点：让节点像表格一样对齐（拖动/新建/整理布局都用它）"""
        return float(int(round(float(v) / GRID)) * GRID)

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
                     f"（可在空白处拖动平移，或用「✥ 整理布局」把节点排紧）", "warn")
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
            key = self.pick_target             # 先记住：下面会清空它
            pt = self._canvas_to_frame(cx, cy)
            self.pick_target = None          # 本次点击后一律退出取点模式
            if pt is not None:
                self._apply_pick(pt, key)
                return
            self.status("已取消取点（点击帧画面外即取消）")
            # 落到下面的正常选中/拖动逻辑，避免取点模式卡死节点编辑
        hit = self._hit_test(cx, cy)
        if hit is None:
            if self.sel or self.multi:
                self.sel = None
                self.multi = set()
                self.build_prop_panel()
                self.redraw()
            # ★ 空白处按住左键拖动 = 平移画布。以前只有中键能平移 —— 很多鼠标没有中键，
            #   触控板上更按不出来；空白处拖动本来没有别的语义，正好给它。
            #   （点一下不放=取消选中，行为和以前一样；拖动才平移。）
            self._pan_left = True
            try:
                self.canvas.scan_mark(e.x, e.y)
                self.canvas.config(cursor="fleur")
            except tk.TclError:
                pass
            return
        if self.multi:                  # 左键点节点 = 回到单选
            self.multi = set()
            self.redraw()
        if hit[0] == "port":
            self.wire = {"from": hit[1], "port": hit[2], "mx": cx, "my": cy}
            self.status(f"拖到目标节点设置「{port_label(hit[2])}」；"
                        f"拖到空白处 = 断开")
            return
        nid = hit[1]
        if self.sel != nid:
            self.sel = nid
            self.build_prop_panel()
        nd = self.flow["nodes"][nid]
        # 拖动前存档；若只是点选没真的移动，内容不变，_snapshot 会自动跳过
        self._snapshot(f"drag:{nid}")
        ch = self.flow["chain"]
        # 拖动不改链序，所以不用记链上邻居（i 只是用来判断在不在链上）
        self.drag = {"id": nid, "dx": cx - nd["x"], "dy": cy - nd["y"]}
        self.redraw()

    def on_motion(self, e):
        if self._pan_left:              # 空白处左键拖动 = 平移画布
            try:
                self.canvas.scan_dragto(e.x, e.y, gain=1)
            except tk.TclError:
                pass
            return
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
        nd["x"] = self._snap(cx - self.drag["dx"])
        nd["y"] = self._snap(cy - self.drag["dy"])
        # ★ 拖动只挪卡片，【不动链序】。以前拖动时会按纵向位置实时重排主链，
        #   等于"把节点拖到别人上面"就悄悄改了谁接到谁 —— 连接不该被拖动改掉。
        #   要改顺序：右侧「连接」里的上一个/下一个，或工具栏的 ↑ 上移 / ↓ 下移。
        self.redraw()

    def _ensure_in_chain(self, tgt_id, src_id=None):
        """确保节点在链上：不在就插在 src 后面（src 不在链上则排链尾）。
        返回 True 表示这次是新接进去的。"""
        ch = self.flow["chain"]
        if tgt_id in ch:
            return False
        i = ch.index(src_id) if src_id in ch else len(ch) - 1
        self._chain_insert(i + 1, tgt_id)
        return True

    def _clear_cut(self, *nids):
        """清掉这些节点上的「此处断开」标记。
        cut 的含义就是"我不接下一个"：一旦有节点被显式排到它后面，这个标记就不再成立。"""
        for nid in nids:
            nd = self.flow["nodes"].get(nid)
            if isinstance(nd, dict):
                nd.pop("cut", None)

    def _cut_hint(self, nid):
        """目标节点带着「此处断开」时的提醒文案（没有就返回空串）。

        连一条"指到它"的线不改变它自己的出边状态，所以那种标记仍然有效 —— 流程走到
        它就不再往下，而用户常常没意识到，以为"连上了就会往下跑"。"""
        nd = self.flow["nodes"].get(nid) or {}
        if not nd.get("cut"):
            return ""
        return (f"（注意：{self._node_ref_label(nid)}标着「此处断开」，"
                f"流程走到它就不再往下 —— 要让它继续，选中它、在「连接」里给它的"
                f"「下一个」选一个节点，或直接把卡片下沿的小球拖到目标节点）")

    def _next_hint(self, nid):
        """目标节点自己还挂着"下一个"时的提示文案（没有就返回空串）。

        拖出口线只决定"谁指到它"，不会去动它自己的下一个 —— 而它的下一个可能是
        从旧文件的链序迁移来的、用户根本没画过的那条。不说一句的话，画布上就会
        冒出一条他没连过的线（"我拉红球给 12，12 怎么还连着 11"）。"""
        nd = self.flow["nodes"].get(nid) or {}
        nxt = nd.get("next")
        if not nxt or nxt not in self.flow["nodes"]:
            return ""
        return (f"（提示：{self._node_ref_label(nid)}自己的「下一个」是"
                f"{self._node_ref_label(nxt)} —— 连接只决定'谁指到它'，不碰它自己的"
                f"下一个；不需要那条线就选中它，把「下一个」改成「(无：到此结束)」，"
                f"或把卡片下沿的小球拖到空白处）")

    def _chain_insert(self, i, nid, heal=True):
        """把 nid 插到链上第 i 位，返回实际插入位置。

        heal=True：顺手清掉"因此有了新下一个"的那个节点的「此处断开」——
        有节点被显式排到它后面，说明它接着往下走，标记不再成立。
        （"原地不动"的操作要传 heal=False：那时链序什么都没变，
          别顺手把别人身上的「此处断开」也解掉 —— 那等于替你连了一条没画过的线。）"""
        ch = self.flow["chain"]
        i = max(0, min(i, len(ch)))
        ch.insert(i, nid)
        if heal:
            if i > 0:
                self._clear_cut(ch[i - 1])
            if i + 1 < len(ch):
                self._clear_cut(nid)      # 不是链尾 = 真的有后继了
        return i

    def _link_exit(self, src_id, tgt_id):
        """连一条出口边：保证【两端都在主链上】，且目标紧跟源节点后面。

        ★ 只把"目标"塞进链是不够的（以前就是这么干的）。源节点自己还在链外时，
          目标被塞到链尾，于是画布上出现的是"链尾那个节点 → 目标"的直落箭头
          （例如 #11 → #12），看着像连错了对象；而真正想连的 "#10 → #12" 因为
          #10 不在链上根本不画（出口连线只画链上节点），结果就是
          "日志说已连接、画布上一条线都没有"。
        这里先把源节点接到链尾（正在给它连出口 = 它要参与流程），再把目标插到它后面，
        两端相邻、出口连线也画得出来。返回 (源是否新接入, 目标是否新接入)。
        ★ 只用于"出口指向某节点"的场景；【输入】的注入线不要用（输入节点设计上就离链）。"""
        ch = self.flow["chain"]
        src_added = tgt_added = False
        if src_id not in ch:
            self._chain_insert(len(ch), src_id)
            self._clear_cut(src_id)     # 我这条出口现在明确接 X，不再"到此结束"
            src_added = True
        if tgt_id not in ch:
            # 注意：不清目标自己的「此处断开」—— 那是它的【出边】状态，
            # 连一条"指到它"的线没改变它自己的出边。确实要清就用「连接」里的下一个。
            self._chain_insert(ch.index(src_id) + 1, tgt_id)
            tgt_added = True
        return src_added, tgt_added

    def _set_wire(self, src, port, tgt):
        """连线写回：branch 直接写节点字段；switch 写候选 next / 全部未中 miss_next；
        loop 写循环体末尾 body_end（「下一个」仍写在 next 上）"""
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
        elif src.get("type") == "loop" and port == "body_end":
            src.setdefault("props", {})["body_end"] = tgt
        else:
            src[port] = tgt

    def on_up(self, _e):
        if self._pan_left:              # 空白处左键拖动结束（平移，不改任何流程数据）
            self._pan_left = False
            try:
                self.canvas.config(cursor="")
            except tk.TclError:
                pass
            return
        if self.roi_pick is not None:
            self._finish_roi_pick()
            self.redraw()
            return
        if self.wire:
            hit = self._hit_test(self.wire["mx"], self.wire["my"])
            src_id = self.wire["from"]
            src = self.flow["nodes"][src_id]
            port = self.wire["port"]
            if port == "new" and src.get("type") == "switch":
                # 「＋」球：拖到目标节点 → 直接新建一个候选并连过去（不弹表单、不用手输）
                self.wire = None
                self.redraw()
                if hit and hit[0] == "node" and hit[1] != src_id:
                    self._snapshot("cand")
                    # 与其它出口一样：两端都要在主链上（见 _link_exit）—— 候选指向一个
                    # "不参与生成"的节点，生成物里就是指向不存在节点的 next。
                    src_add, tgt_add = self._link_exit(src_id, hit[1])
                    cands = src["props"].setdefault("candidates", [])
                    cands.append({"timeout": 3000, "next": hit[1]})
                    tgt = self.flow["nodes"][hit[1]]
                    self.build_prop_panel()
                    self.redraw()
                    if src_add:
                        self.log(f"（本节点「{src.get('title', src_id)}」还没接进流程，"
                                 f"已先接到链尾 —— 否则这条候选连线在画布上画不出来）",
                                 "warn")
                    if tgt_add:
                        self.log(f"（「{tgt.get('title', hit[1])}」原本还没接进流程，"
                                 f"已顺手插在本节点后面）", "warn")
                    self.log(f"✓ 已新建分支 {len(cands)} → "
                             f"「{tgt.get('title', hit[1])}」。"
                             f"判定条件取自这个节点：在里面选模板图或填 OCR 文字即可"
                             f"（校验会提示到填好为止）",
                             "ok")
                else:
                    self.status("把卡片下沿的「＋」球拖到目标节点，即可新建一条分支")
                return
            if port == "inject" and src.get("type") in ("input", "pick"):
                # 「注入」小球：【输入】= 这个参数注入过去；【选择】= 这个下拉作用在哪些节点上
                # （`props.targets`，与【输入】同一份数据）。拖到已经连着的节点 = 取消。
                # ★ 这条线只表示参数作用在哪个节点上，不代表流程顺序（生成时它变成
                #   interface.json 里的 pipeline_override / 选项的覆盖目标，与链序无关）。
                self.wire = None
                self.redraw()
                if hit and hit[0] == "node" and hit[1] != src_id:
                    self._snapshot("inject")
                    if self._ensure_in_chain(hit[1], src_id):
                        self.log(f"（「{self.flow['nodes'][hit[1]].get('title', hit[1])}」"
                                 f"原本还没接进流程，已顺手接上）", "warn")
                    tg = inject_targets_of(src)
                    tname = self.flow["nodes"][hit[1]].get("title", hit[1])
                    opt = (str((src.get("props") or {}).get("option") or "").strip()
                           or "(还没填参数名)")
                    if hit[1] in tg:
                        tg.remove(hit[1])
                        self.log(f"已取消注入：「{opt}」不再作用在「{tname}」")
                    else:
                        tg.append(hit[1])
                        self.log(f"✓ 「{opt}」已注入到「{tname}」"
                                 f"（这条线只表示参数作用在哪个节点上，不代表流程顺序）",
                                 "ok")
                        if src.get("type") == "pick":
                            # 老流程把目标写死在选项里，那种行以行内为准 —— 拖了线
                            # 却"没反应"时，得让用户知道为什么
                            fixed = [r for r in cases_of(src)
                                     if str((r or {}).get("node") or "").strip()]
                            if fixed:
                                nm = str(fixed[0].get("name") or "?")
                                self.log(
                                    f"（提示：这个【选择】有 {len(fixed)} 个选项自己写死了"
                                    f"目标节点（如「{nm}」），它们以行内写的为准 —— "
                                    f"想让它们也跟注入线走，属性面板里有「⇢ 目标改用注入线」）",
                                    "warn")
                    self.build_prop_panel()
                    self.redraw()
                else:
                    self.status("把「注入」小球拖到目标节点上（拖到已注入的节点=取消）")
                return
            if hit and hit[0] == "node" and hit[1] != self.wire["from"]:
                self._snapshot()
                tgt_id = hit[1]
                tname = self.flow["nodes"][tgt_id].get("title", tgt_id)
                # 两端都要落在主链上：目标是"不参与生成"的节点 → 生成物里是悬空引用，
                # 引擎拒绝加载整个任务包；源节点不在链上 → 这条出口边画不出来。
                src_add, tgt_add = self._link_exit(src_id, tgt_id)
                if src_add:
                    self.log(f"（「{self.flow['nodes'][src_id].get('title', src_id)}」"
                             f"自己还没接进流程，已先接到链尾 —— 否则这条连线"
                             f"在画布上画不出来）", "warn")
                if tgt_add:
                    self.log(f"（「{tname}」原本还没接进流程，已顺手插在源节点后面）", "warn")
                if port == "next":
                    self._clear_cut(src_id)   # 明确接了下一个，「此处断开」不再成立
                    self._set_wire(src, port, tgt_id)
                    self.log(f"已连接「下一个」→ #{node_no(self.flow, tgt_id)}「{tname}」"
                             + self._cut_hint(tgt_id) + self._next_hint(tgt_id), "ok")
                else:
                    self._set_wire(src, port, tgt_id)
                    self.log(f"已连接 {port_label(port)} → "
                             f"#{node_no(self.flow, tgt_id)}「{tname}」"
                             + self._cut_hint(tgt_id)
                             + self._next_hint(tgt_id), "ok")
            else:
                if src.get("type") == "switch":
                    was = (src["props"].get("miss_next") if port == "miss"
                           else parse_switch_cands(src["props"].get("candidates"))
                                .__getitem__(int(port[4:])).get("next")
                           if port.startswith("cand") else None)
                else:
                    was = src.get(port)
                if port == "next":
                    # 拖到空白 = 到此结束（v4：next=None 就是"没有下一个"）
                    src_now = self._node_ref_label(src_id)
                    if was:
                        self._snapshot()
                        self.log(f"已断开「下一个」：{src_now}到此结束"
                                 f"（要再接上：把卡片下沿的小球拖到目标节点）", "warn")
                    else:
                        self.log(f"{src_now}本来就没有「下一个」（到此结束）")
                elif was:
                    self._snapshot()
                    self.log(f"已断开 {port_label(port)}")
                self._set_wire(src, port, None)
            self.wire = None
            self.build_prop_panel()
            self.redraw()
            self.status("就绪")
            return
        if self.drag:
            # 拖动只挪卡片位置：链序不变 → 连接关系不变（排序请用「连接」下拉或 ↑↓ 按钮）
            self.drag = None
            self.build_prop_panel()   # 刷新面板里的 #序号
            self.redraw()
            # 拖完若有卡片压在别人身上，提醒一下（自动挪会跟"我就要放这里"打架）
            bad = overlapping_nodes(self.flow)
            if len(bad) > 1:
                self.status(f"⚠ 有 {len(bad)} 个节点重叠 —— 点「✥ 整理布局」可一键排开")

    # ---------- 属性面板 ----------

    def build_prop_panel(self):
        self._close_tpl_pop()      # 面板重建时收起可能开着的模板弹窗，避免残留
        # ★ 面板要重建了：先把里面"正在编辑但还没提交"的内容写回去。
        #   Tk 销毁控件**不会**补发 <FocusOut>，所以"改完选项名直接点画布上别的节点"
        #   这类操作会走到这里 —— 不提交就等于把用户刚敲的字丢掉（2026-09-16 用户报的：
        #   "改了选项名，点到其他地方，没同步进选项里"）。
        cb, self._panel_commit = self._panel_commit, None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
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
            self._sync_conn_ui()
            self._sync_branch_ui()
            return
        nd = self.flow["nodes"][nid]
        spec = NODE_TYPES[nd["type"]]
        idx = node_no(self.flow, nid)
        head = ttk.Frame(self.props_inner)
        head.grid(row=0, column=0, columnspan=2, sticky="w", pady=(2, 6))
        tk.Label(head, text=f"{spec['icon']} #{idx} {spec['label']}",
                 bg=spec["color"], fg="white", font=FONT_B, padx=8, pady=2).pack(side="left")
        row = 1
        all_fields = list(spec["fields"]) + list(COMMON_NODE_FIELDS)
        hidden = FIELD_HIDDEN_IF.get(nd["type"], lambda _p: set())(nd.get("props") or {})
        overridden = FIELD_OVERRIDDEN_IF.get(nd["type"],
                                             lambda _p: set())(nd.get("props") or {})
        for _gkey, glabel, gfields in _group_fields(all_fields):
            visible = [f for f in gfields if f[0] not in hidden]
            if not visible:
                continue
            ttk.Label(self.props_inner, text=glabel, style="Group.TLabel").grid(
                row=row, column=0, columnspan=2, sticky="we", pady=(9, 2))
            row += 1
            for f in visible:
                key, label, kind, extra = _field_spec(f)
                if kind in ("switch_list", "cases_list"):
                    # 候选/选项编辑器较宽：标签放到上方，编辑器占整行
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
                dead = key in overridden
                lab = ttk.Label(self.props_inner,
                                text=label + ("（被 OCR 文本覆盖）" if dead else ""),
                                style="Dim.TLabel", wraplength=170, justify="left")
                lab.grid(row=row, column=0, sticky="w", pady=2)
                self._bind_tip(lab, FIELD_TIPS.get(key))
                var = self._make_var(nd["props"], key, kind)
                w = self._make_widget(var, kind, key, extra)
                w.grid(row=row, column=1, sticky="we", padx=(8, 0), pady=2)
                if dead:
                    self._gray_out(w)
                self.prop_widgets[key] = var
                row += 1
        hint = _hide_hint(nd["type"], nd.get("props") or {})
        if hint:
            ttk.Label(self.props_inner, text="⚠ " + hint, style="Dim.TLabel",
                      wraplength=300, justify="left").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        # 标签列固定最小宽: 长标签折行而不是把右列控件挤出面板(填 OCR 文本后
        # 标签会追加"（被 OCR 文本覆盖）", 不固定列宽时整面板会被撑变形)
        self.props_inner.columnconfigure(0, minsize=186)
        self.props_inner.columnconfigure(1, weight=1)
        self._sync_conn_ui()
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

    def _node_ref_label(self, nid):
        """节点 id → 下拉里显示的「#编号 标题」"""
        if not nid or nid not in (self.flow.get("nodes") or {}):
            return ""
        return f"#{node_no(self.flow, nid)} {self.flow['nodes'][nid].get('title', nid)}"

    def _node_ref_id(self, label):
        """下拉里的「#编号 标题」→ 节点 id。

        ★ 编号是节点的【固定编号 num】，不是它在链上的位置 —— 两者早就不是一回事了
          （编号一旦分配就不再变，链序可以随时改）。以前「连接」和分支出口的下拉
          拿编号当链上下标用：选「#10」实际接到链上第 10 个位置的节点，只要链序
          和编号顺序不一致就会连错人。统一走这里按编号反查。"""
        m = re.search(r"#(\d+)", str(label or ""))
        if not m:
            return ""
        want = int(m.group(1))
        for n in self.flow.get("nodes", {}):
            if node_no(self.flow, n) == want:
                return n
        return ""

    def _make_toggle(self, parent, var, text="", font=None, pad=(4, 2)):
        """勾选类字段统一渲染成「✓ 绿=开 / ✗ 灰=关」的文字开关。

        为什么不用 ttk.Checkbutton：clam 主题把"已勾选"的指示器画成一个像 ✗ 的图形，
        看着像"关闭"，实际上却是开着的 —— 语义正好读反。这里把状态写成文字 + 颜色，
        没有歧义（点一下就在 ✓/✗ 之间切换）。"""
        btn = tk.Checkbutton(parent, indicatoron=False, variable=var, anchor="w",
                             justify="left", bg=THEME["panel"], fg=THEME["text"],
                             activebackground=THEME["panel"],
                             activeforeground=THEME["text"],
                             selectcolor=THEME["panel"], relief="flat", bd=0,
                             highlightthickness=0, cursor="hand2",
                             font=font or FONT_SM, padx=pad[0], pady=pad[1])

        def sync(*_):
            try:
                on = bool(var.get())
                mark, col = ("✓", THEME["ok"]) if on else ("✗", THEME["text_dim"])
                btn.config(text=(mark + "  " + text) if text else mark, fg=col)
            except tk.TclError:
                pass                      # 控件已销毁（面板重建）时的回调
        btn.config(command=sync)
        sync()
        return btn

    def _gray_out(self, widget):
        """互斥覆盖：控件保留在面板上但置灰 —— 让人看得见"它在这、只是现在不生效"，
        而不是整行消失（消失看着就像功能没了）。清空覆盖它的那一项即自动恢复。"""
        try:
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def _make_var(self, props, key, kind):
        v = props.get(key)
        if kind == "bool":
            var = tk.BooleanVar(value=bool(v))
        elif kind == "bool_opt":
            # 引擎默认 enabled=true，故未设置时勾选框应为「已勾选」
            var = tk.BooleanVar(value=True if v is None else bool(v))
        elif kind in ("switch_list", "inject_list", "cases_list"):
            # 列表型：StringVar 只放个数量，用来触发面板重绘（真值由控件/拖线维护）
            var = tk.StringVar(value=str(len(v)) if isinstance(v, list) else "0")
        elif kind == "node":
            # 「作用到哪个节点」：存的是节点 id，控件里显示成 "#编号 标题"
            var = tk.StringVar(value=self._node_ref_label(v))
        else:
            var = tk.StringVar(value="" if v is None else str(v))
        tid = var.trace_add("write", lambda *_: self._prop_changed(key, var, kind))
        self._var_traces.append((var, tid))
        return var

    def _make_widget(self, var, kind, key, extra=None):
        if kind == "switch_list":
            return self._make_switch_list(var)
        if kind == "cases_list":
            return self._make_cases_list(var)
        if kind == "inject_list":
            # 【输入】节点的注入目标：只读展示 + 提示（增删靠画布上右侧的注入小球）
            box = ttk.Frame(self.props_inner)
            nd = (self.flow.get("nodes") or {}).get(self.sel) or {}
            tgts = list(input_targets(nd))
            if tgts:
                names = "、".join(self._node_ref_label(t) or t for t in tgts)
                txt = f"已注入：{names}"
            else:
                txt = "还没连到节点 —— 把卡片右侧的「注入」小球拖到目标节点上"
            ttk.Label(box, text=txt, style="Dim.TLabel", wraplength=250,
                      justify="left").pack(anchor="w")
            ttk.Label(box, text="（这条线只表示参数注入，不代表流程顺序；"
                                "再把小球拖到已注入的节点上即取消）",
                      style="Dim.TLabel", wraplength=250,
                      justify="left").pack(anchor="w")
            return box
        if kind in ("tpl", "tpl_multi"):
            self.templates = list_templates()   # 实时刷新（框选工具新保存的模板立即可选）
            multi = (kind == "tpl_multi")
            width = 30 if multi else 26
            cmb = ttk.Combobox(self.props_inner, textvariable=var,
                               values=self.templates, width=width, font=FONT_SM)

            def _refresh_tpls(_e=None):
                # 点开下拉前重读一次模板目录：面板是选中节点时建的，之后用框选工具
                # 新做的模板不在 values 里，下拉里就找不到（"选不了模板"多因于此）。
                # 注意不能 return "break" —— 那会吃掉这次点击、下拉就不弹了。
                self.templates = list_templates()
                cmb.configure(values=self.templates)

            def _submit(picked=None, var=var):
                # ★ 第一个参数必须是 picked：弹窗回调是按 on_pick(选中的模板串) 调的。
                #   以前写成 (var=var, picked=None)，那串模板名会被塞进 var、
                #   picked 仍是 None → 走"按输入框文字提交"→ 对字符串调 .get()
                #   抛 AttributeError（被 Tk 吞掉）→ 点了候选什么都没发生。
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

            def _on_focus_out(_e=None, var=var):
                # ★ 第一个参数必须能接住事件：bind 会把 Event 当位置参数传进来，
                #   写成 (var=var) 的话 var 会被 Event 顶掉，后面 var.get()/set() 全部
                #   抛 AttributeError，失焦提交（输入名字后点别处）就永远不生效。
                self.root.after(170, self._close_tpl_pop)   # 等列表点击事件先到
                self._commit_tpl(var, None, multi)

            cmb.bind("<KeyRelease>", _on_key)
            cmb.bind("<Button-1>", _refresh_tpls)
            cmb.bind("<<ComboboxSelected>>",
                     lambda e, var=var: self._commit_tpl(var, var.get(), multi))
            cmb.bind("<FocusOut>", _on_focus_out)
            return cmb
        if kind == "common":
            return ttk.Combobox(self.props_inner, textvariable=var,
                                values=common_node_names(), width=22,
                                state="readonly", font=FONT_SM)
        if kind == "node":
            # 「作用到哪个节点」：本流程全部节点（#编号 标题）
            return ttk.Combobox(self.props_inner, textvariable=var,
                                values=[self._node_ref_label(n)
                                        for n in self.flow.get("chain", [])],
                                width=22, state="readonly", font=FONT_SM)
        if kind in ("bool", "bool_opt"):
            return self._make_toggle(self.props_inner, var)
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

    def _make_cases_list(self, var):
        """【选择】的选项编辑：上面三格是"正在填的这一项"，点「＋ 加选项」才成为列表里的一项。

        三格 = 选项名 / 字段 / 值。**目标节点不在这里**：它由卡片右侧的「注入」小球决定
        （`props.targets`，与【输入】同一套数据），选项只回答"选中时把那个节点（们）的
        哪个字段改成什么值"。点列表里已有的一行 = 把它载进三格改，改完点到别处自动写回。

        ★ 老流程（征集次数）把目标节点**写死在单个选项里**（选项 1 只作用在「征集段2」上），
          那种写法继续支持：列表行显示成 `1 @征集段2: next = [...]`，以行内写的为准。"""
        wrap = ttk.Frame(self.props_inner)
        lb = tk.Listbox(wrap, height=7, width=44, font=FONT_SM, bg=THEME["field"],
                        fg=THEME["text"], selectbackground=THEME["accent"],
                        selectforeground=THEME["text"], relief="flat",
                        highlightthickness=1, highlightbackground=THEME["card_line"])
        lb.pack(fill="x", padx=(8, 0))
        # ★ 这块面板属于哪个节点，建面板时就记下来：面板重建前 `self.sel` 可能已经被
        #   清空（点画布空白 → 先 `self.sel = None` 再 build_prop_panel），
        #   而提交钩子要在那一刻把三格里没存的内容写回**原来那个节点**。
        owner = self.sel

        def _rows():
            nd = (self.flow.get("nodes") or {}).get(owner)
            if not isinstance(nd, dict):
                return []
            return nd.setdefault("props", {}).setdefault("cases", [])

        def refresh(_e=None):
            rows = _rows()
            # ★ 重建列表时把选中行留住：`lb.delete(0,"end")` 会清掉 selection，而
            #   _apply/_fill 都靠 `lb.curselection()` 认"正在编辑哪一行" —— 以前
            #   改完一格（refresh 一次）选中行就没了，之后改「字段 / 值」静默不写回，
            #   得重新点一下那一行才行。
            keep = lb.curselection()
            keep = keep[0] if keep else None
            lb.delete(0, "end")
            for i, r in enumerate(rows):
                lb.insert("end", f"{i + 1}. {case_brief(self.flow, r)}")
            if keep is not None and keep < len(rows):
                lb.selection_set(keep)
                lb.activate(keep)
            var.set(str(len(rows)))     # 触发面板重绘钩子（摘要/校验跟着更新）

        grid = ttk.Frame(wrap)
        grid.pack(fill="x", padx=(8, 0), pady=(6, 0))
        for _c in (0, 1):
            grid.columnconfigure(_c, weight=1)
        entries = {}
        # 「选项名 / 字段」并排，「值」单独一行占满宽度（模板名和节点串比格子长）。
        # ★ 一行四格在窄面板里会超出可用宽度，最后一格被裁掉（连下拉箭头都看不见），
        #   所以这里只排三格、且分成两行。
        for col, (key, lab) in enumerate((("name", "选项名"), ("field", "字段"))):
            ttk.Label(grid, text=lab, style="Dim.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, 4), pady=(2, 0))
            if key == "field":
                e = ttk.Combobox(grid, width=15, font=FONT_SM,
                                 values=list(PICK_FIELDS))
            else:
                e = ttk.Entry(grid, width=15, font=FONT_SM)
            e.grid(row=1, column=col, sticky="we", padx=(0, 4))
            e._cell_key = key       # 定位用（纯 Python 侧属性，Tk 不认）
            entries[key] = e
        ttk.Label(grid, text="值", style="Dim.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=(0, 4), pady=(2, 0))
        ve = ttk.Combobox(grid, width=15, font=FONT_SM)   # 可编辑：候选现填，也能直接打字
        ve.grid(row=3, column=0, columnspan=2, sticky="we", padx=(0, 4))
        ve._cell_key = "value"
        entries["value"] = ve
        # 「值」这一格该填什么，随【字段】变（模板文件名 / 节点名 / 数字 / 文字…）
        hint = ttk.Label(wrap, style="Dim.TLabel", wraplength=250, justify="left")
        hint.pack(anchor="w", padx=8, pady=(2, 0))

        def refresh_choices(_e=None):
            """刷新下拉候选。

            ★ 点开下拉前现刷：才认得上刚用框选工具做出来的模板、刚改过标题的节点。
              「值」的候选还要跟着【字段】走 —— 模板图 → 模板文件名列表、
              下一个出口 → 画布节点、其余（OCR 文字/数字/阈值…）不限制，
              输入框一直能手填。"""
            field_label = entries["field"].get()
            field = input_field({"field": field_label})
            self.templates = list_templates()
            # 字段名是手写的奇特写法时，把它自己也临时列进候选，免得下拉里看不到当前值
            fvals = list(PICK_FIELDS)
            if str(field_label).strip() and field_label not in fvals:
                fvals.append(field_label)
            entries["field"].configure(values=fvals)
            entries["value"].configure(values=pick_value_choices(self.flow, field))
            hint.configure(text=pick_value_hint(field))

        editing = {"idx": None}   # 三格现在装的是**哪一行**（切行/取消选中都不丢改动）

        def _row_at(i):
            rows = _rows()
            return rows[i] if isinstance(i, int) and 0 <= i < len(rows) else None

        filled = {}     # 三格里"填进去时"的原文 —— 逐格比出哪一格真被改过

        def set_cells(name="", field="", value=""):
            """程序性写三格，并同步记进 filled —— 这样"程序写的"不算用户改动，
            失焦时不会被当成编辑写回（否则刚清空的草稿会把那一行也清空）。"""
            for k, v in (("name", name), ("field", field), ("value", value)):
                e = entries[k]
                e.delete(0, "end")
                e.insert(0, v)
                filled[k] = v

        def _fill(_e=None):
            """把列表选中那一行载进三格（这时三格=那一行，editing 记下来）"""
            sel = lb.curselection()
            editing["idx"] = sel[0] if sel else None
            r = _row_at(editing["idx"])
            if r is None:
                return
            set_cells(str(r.get("name") or ""),
                      pick_field_display(input_field({"field": r.get("field")})),
                      str(r.get("value") or ""))
            # ★ 必须在三格填完之后刷：候选和提示都跟着刚刚填进去的【字段】走
            refresh_choices()

        def _apply(_e=None):
            """把改动过的格子写回**三格所属的那一行**（`editing`）；草稿状态（没点任何
            选项行）就不写 —— 那时三格是"新选项输入区"，改动只通过「＋ 加选项」提交。

            ★ 只写真的被改过的格，也不碰这一行原有的目标节点（老流程里写死的那个）——
              点进点出就把用户文件改写的事不能再发生。
            ★ 目标行用 `editing` 而不是 `lb.curselection()`：切行时事件到达那一刻
              curselection 已经是新行了，用它会把内容写到错误的一行。"""
            r = _row_at(editing["idx"])
            if r is None:
                return
            changed = [k for k in ("name", "field", "value")
                       if entries[k].get() != filled.get(k, "")]
            if not changed:
                return
            self._snapshot("case")
            if "name" in changed:
                r["name"] = entries["name"].get().strip()
            if "field" in changed:
                r["field"] = input_field({"field": entries["field"].get()})
            if "value" in changed:
                r["value"] = norm_ref_text(entries["value"].get()).strip()
            refresh()
            refresh_choices()

        def _on_select(_e=None):
            """点列表里另一行：**先把刚才那一行的改动写回去**，再载入新行。

            ★ 顺序不能反：`<<ListboxSelect>>` 到达时 curselection 已经是新行，而正在
              编辑的还是旧行（靠 `editing` 记着）。以前直接 _fill 会先把新行载进三格，
              随后的 FocusOut 写回要么写到错行、要么判定"没改动"而跳过 ——
              用户看到的就是"改完点下一个选项，刚才的修改白做了"。"""
            _apply()
            _fill()

        for key, e in entries.items():
            e.bind("<FocusOut>", _apply)
            e.bind("<Return>", _apply)
            if key in ("field", "value"):
                # 下拉里选一项就写回；打开下拉前把候选刷一遍（模板随时在变）
                e.bind("<<ComboboxSelected>>", _apply)
                e.bind("<Button-1>", refresh_choices, add="+")

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", padx=(8, 0), pady=(6, 0))

        def _add():
            """把三格里填好的内容加成一个新选项。

            ★ 三格是草稿，选项名和值都得填：缺名字的那一项不会写进 interface.json，
              值是空的会写出类型不对的覆盖（比如 repeat: ""）—— 引擎可能因此拒绝
              **整包**加载（铁律 #1），所以两个都在这里拦下，不猜。"""
            name = entries["name"].get().strip()
            field = input_field({"field": entries["field"].get()})
            value = norm_ref_text(entries["value"].get()).strip()
            if not name:
                self.status("先填【选项名】再点「＋ 加选项」——App 的下拉里显示的就是这个名字")
                self.log("⚠ 没填【选项名】：没有名字的选项不会写进 interface.json，所以没往上加",
                         "warn")
                try:
                    entries["name"].focus_set()
                except tk.TclError:
                    pass
                return
            if not value:
                self.status("先填【值】再点「＋ 加选项」——空值覆盖可能让引擎拒绝整包")
                self.log(f"⚠ 选项「{name}」还没填【值】：空值覆盖上去可能让引擎拒绝整包加载，"
                         f"所以没往上加", "warn")
                try:
                    entries["value"].focus_set()
                except tk.TclError:
                    pass
                return
            self._snapshot("case")
            _rows().append({"name": name, "node": "", "field": field, "value": value})
            refresh()
            lb.selection_clear(0, "end")
            lb.selection_set("end")
            # 草稿清空（字段留着：连着加同一字段的选项更省事）；三格此刻不属于任何一行，
            # 之后再编辑三格不会把内容写回刚加的那一行
            editing["idx"] = None
            set_cells(name="", field=pick_field_display(field), value="")
            refresh_choices()
            self.log(f"✓ 已加选项「{name}」：{field} = {value}"
                     f"（作用在注入线连到的那些节点上）", "ok")

        def _del():
            sel = lb.curselection()
            if not sel:
                self.status("先点列表里的一行（要删的选项），再点「－ 删选中」")
                return
            self._snapshot("case")
            del _rows()[sel[0]]
            refresh()
            editing["idx"] = None   # 三格回到空白草稿状态
            set_cells()
            refresh_choices()

        def _commit_click():
            """【✔ 确认修改】：把三格的改动显式写回当前选中的那一项（不新增）。

            自动保存有好几个触发点（失焦 / 切行 / 面板重建前），但这个按钮把话说死：
            按了就一定提交，并给一句明确回执 —— 不用猜"我改的到底进去没有"。"""
            r = _row_at(editing["idx"])
            if r is None:
                self.status("先点列表里要改的那一行，改上面的三格，再点「✔ 确认修改」")
                self.log("⚠ 现在没有选中的选项：三格是「新选项」草稿区 —— "
                         "填好点「＋ 加选项」才会新增一项，或者先点列表里的一行再改", "warn")
                return
            before = (str(r.get("name") or ""), input_field({"field": r.get("field")}),
                      str(r.get("value") or ""))
            _apply()
            after = (str(r.get("name") or ""), input_field({"field": r.get("field")}),
                     str(r.get("value") or ""))
            if before == after:
                self.status(f"选项「{after[0] or '(未命名)'}」没有改动")
                self.log(f"（选项「{after[0] or '(未命名)'}」没有改动 —— "
                         f"要新增一项请用「＋ 加选项」）")
            else:
                self.status(f"已保存选项「{after[0] or '(未命名)'}」")
                self.log(f"✓ 已保存到选项「{after[0] or '(未命名)'}」："
                         f"{after[1]} = {after[2] or '(空)'}", "ok")

        def _to_inject():
            """把选项里**写死的**目标节点清掉，让它们跟着注入线走（老流程迁移用）。

            老流程（征集次数）的目标写在每个选项里，画布上的注入线对它们不起作用 ——
            以前没有入口能把这件事改过来，只能删行重建。"""
            rows = [r for r in _rows() if isinstance(r, dict)
                    and str(r.get("node") or "").strip()]
            if not rows:
                self.status("这些选项都没有写死的目标节点了")
                return
            self._snapshot("case")
            for r in rows:
                r["node"] = ""
            refresh()
            self.log(f"✓ 已把 {len(rows)} 个选项的目标改成「跟着注入线走」—— "
                     f"记得把卡片右侧的「注入」小球拖到要作用的节点上，"
                     f"不然它们生成不出来（Ctrl+Z 可撤回）", "ok")

        self._flat_btn(btns, "＋ 加选项", _add).pack(side="left")
        self._flat_btn(btns, "－ 删选中", _del).pack(side="left", padx=(6, 0))
        self._flat_btn(btns, "✔ 确认修改", _commit_click, padx=8, font=FONT_SM,
                       bg="#274a33", fg="#c9f0d5", hover="#31603f"
                       ).pack(side="left", padx=(6, 0))
        if any(str((r or {}).get("node") or "").strip() for r in _rows()):
            # 只在真有"写死目标"的老数据时才出现，平时界面不变
            self._flat_btn(btns, "⇢ 目标改用注入线", _to_inject).pack(side="left",
                                                                     padx=(6, 0))
        tip = ttk.Label(wrap, style="Dim.TLabel", wraplength=250, justify="left",
                        text="填好【选项名 / 字段 / 值】再点「＋ 加选项」，就是 App 里的一个"
                             "下拉项。作用在哪些节点上由卡片右侧的「注入」小球决定："
                             "拉几根线，这一组选项就对几个节点生效（拖到已连着的节点=取消）。"
                             "改已有选项：点列表里那一行 → 改上面三格 → 点「✔ 确认修改」"
                             "（换行或点到别处也会自动存）。")
        tip.pack(anchor="w", padx=8, pady=(4, 0))
        # ★ 绑 _on_select（先存旧的、再载入新的），不能直接绑 _fill —— 见 _on_select 的注释
        lb.bind("<<ListboxSelect>>", _on_select)
        # ★ 面板重建（点画布选别的节点、拖线、框选…）之前，让 build_prop_panel() 先
        #   把三格里还没提交的改动写回去 —— Tk 销毁控件不会补发 <FocusOut>，
        #   光靠失焦保存会丢掉"改完直接点画布"的编辑
        self._panel_commit = _apply
        refresh()
        refresh_choices()
        return wrap

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
                merge = " · 回并主线" if c.get("mergeBack") else ""
                lb.insert("end", f"{i + 1}. {cand_brief(self.flow, c)}"
                                 f" · {c.get('timeout', 3000)}ms{merge}")
            var.set(str(len(cands)))   # 触发面板重绘钩子

        row = ttk.Frame(wrap)
        row.pack(fill="x", padx=(8, 0), pady=(6, 0))
        # ★ 这里不再有「模板 / OCR 文字」二选一：候选判什么，取决于它连到的那个
        #   【分支】节点 —— 模板图/OCR 文字只在分支里配一次。枝干只负责顺序与路由，
        #   两处都配等于同一个条件写两遍，容易写歪（也就有了"枝干判定里为什么要选识别方式"的困惑）。
        ttk.Label(row, text="判定超时ms", style="Dim.TLabel").pack(side="left")
        e_d = ttk.Entry(row, width=6, font=FONT_SM)
        e_d.insert(0, "3000")
        e_d.pack(side="left", padx=4)
        e_m = tk.BooleanVar(value=False)
        self._make_toggle(wrap, e_m, "命中后回并主线（不勾=内容跑完即结束）").pack(
            anchor="w", padx=(8, 0), pady=(2, 0))
        # 只读回显：这条候选究竟会用什么条件判定（从它连到的分支推导）
        cur = ttk.Label(wrap, style="Dim.TLabel", wraplength=230, justify="left")
        cur.pack(anchor="w", padx=8, pady=(4, 0))

        def _fill_sel(_e=None):
            sel = lb.curselection()
            if not sel:
                cur.config(text="")
                return
            c = _cands()[sel[0]]
            e_d.delete(0, "end")
            e_d.insert(0, str(c.get("timeout", 3000)))
            e_m.set(bool(c.get("mergeBack")))
            cur.config(text="判定条件取自：" + cand_brief(self.flow, c))

        def _read_form():
            try:
                timeout = max(500, int(e_d.get() or 3000))
            except ValueError:
                timeout = 3000
            item = {"timeout": timeout}
            if e_m.get():
                item["mergeBack"] = True
            return item

        def on_add():
            self._snapshot("cand")
            _cands().append(_read_form())
            e_d.delete(0, "end")
            e_d.insert(0, "3000")
            e_m.set(False)
            cur.config(text="")
            refresh()
            self.log("已加一个候选：拖它右侧的圆点连到分支节点，"
                     "判定条件（模板图/OCR 文字）在那个分支里配")

        def on_update():
            sel = lb.curselection()
            if not sel:
                self.log("先在上面的候选列表里选中一条", "warn")
                return
            old = _cands()[sel[0]]
            item = _read_form()
            # 保留画布上拖出来的连线（next），否则「更新」会把连线清掉
            if old.get("next"):
                item["next"] = old["next"]
            self._snapshot("cand")
            _cands()[sel[0]] = item
            refresh()
            cur.config(text="判定条件取自：" + cand_brief(self.flow, item))

        def on_del():
            sel = lb.curselection()
            if not sel:
                self.log("先在上面的候选列表里选中一条", "warn")
                return
            self._snapshot("cand")
            del _cands()[sel[0]]
            cur.config(text="")
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
            text="顺序就是判定顺序：先试 1，命中就走 1 连到的节点；全不中走红球 ✗ 出口。\n"
                 "候选判什么 = 它连到的【分支】判什么（模板图/OCR 文字在分支里配）。",
            style="Dim.TLabel", wraplength=230, justify="left").pack(
            anchor="w", padx=8, pady=(6, 0))
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
            elif kind == "node":
                # 下拉显示的是 "#编号 标题"，存回节点 id
                got = self._node_ref_id(var.get())
                if got:
                    nd["props"][key] = got
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
            elif kind in ("switch_list", "inject_list", "cases_list"):
                # 列表型 props 由专用控件/画布拖线直接维护，StringVar 仅用于触发重绘
                pass
            else:
                nd["props"][key] = var.get()
        except (ValueError, tk.TclError):
            return
        self.redraw()

    # ---------- 分支出口 UI ----------

    def _branch_target_label(self, nid, target, is_hit):
        ch = self.flow["chain"]
        nodes = self.flow["nodes"]
        if target is None:
            return "（未连线：命中即结束）" if is_hit else "（未连线：未中即结束）"
        if target not in nodes:
            return "（指向的节点已删除）"
        label = f"#{node_no(self.flow, target)} {nodes[target].get('title', target)}"
        # ★ 未接入流程的节点：用与下拉候选【同一个写法】，否则当前值跟候选列表对不上，
        #   下拉里会显示成一串内部 id（"#n0017b54"），看着像没连上。
        return label if target in ch else label + "（未接入）"

    def _set_next(self, nid, tgt):
        """把 nid 的"下一个"设成 tgt（None = 到此结束）。只动这一条边。

        ★ v4 的语义：nd["next"] 就是那条边本身，链序不再参与。
          目标必须在主链上：不在链上的节点不生成，这条边就成了指向不存在节点的引用
          （本机引擎实测：整个任务包 loaded=False），所以顺手把它接进链。
        返回 True 表示目标这次是新接进链的。"""
        nodes = self.flow.get("nodes", {})
        nd = nodes.get(nid)
        if not isinstance(nd, dict):
            return False
        if tgt and (tgt not in nodes or tgt == nid):
            tgt = None
        nd["next"] = tgt or None
        nd.pop("cut", None)
        if tgt and tgt not in self.flow["chain"]:
            self._ensure_in_chain(tgt, nid)
            self._clear_cut(tgt)
            return True
        return False

    def _sync_conn_ui(self):
        """同步「上一个 / 下一个」下拉：它们就是【节点上那两条边】。

        「下一个」= 本节点的 nd["next"]；「上一个」= "谁的下一个写着我"（反向查）。
        ★ v4 起改它【只改这一条边】：不挪节点、不改链序、不牵动别的连接 ——
          不会再有"我只改了一个地方，别处却自动连上/断开"的情况。"""
        nid = self.sel
        ch = self.flow.get("chain", [])
        nodes = self.flow.get("nodes", {})
        if not nid or nid not in nodes:
            for cb in (getattr(self, "prev_combo", None),
                       getattr(self, "next_combo", None)):
                if cb is not None:
                    cb.config(values=[], state="disabled")
                    cb.set("")
            return
        options = [self._node_ref_label(n) for n in ch if n != nid]
        options += [self._node_ref_label(n) + "（未接入）" for n in nodes
                    if n not in ch and n != nid
                    and (nodes[n] or {}).get("type") not in ("input", "pick")]
        nd = nodes[nid]
        no_next = "(无：到此结束)"
        nxt = nd.get("next")
        self.next_combo.config(values=[no_next] + options, state="readonly")
        self.next_combo.set(self._node_ref_label(nxt) if nxt in nodes else no_next)
        prev = next((n for n in nodes
                     if n != nid and (nodes[n] or {}).get("next") == nid), None)
        no_prev = "(无：没有节点接在我前面)"
        self.prev_combo.config(values=[no_prev] + options, state="readonly")
        self.prev_combo.set(self._node_ref_label(prev) if prev else no_prev)

    def on_conn_combo(self, which):
        """改「上一个 / 下一个」= 只改这一条边。

        ★ v4：边存在节点上（nd["next"]），链序只决定显示顺序。所以改一条边绝不会
          牵动别的连接，也不会再冒出你没画过的线。
          「下一个 = X」= 我的下一个改成 X（选「(无：到此结束)」就是没有下一个）；
          「上一个 = X」= X 的下一个改成"本节点"（于是它排在我前面）；
          「（不接入流程：断开）」= 把本节点移出主链（还被别的边指着时会被拒绝）。
        """
        nid = self.sel
        ch = self.flow["chain"]
        nodes = self.flow.get("nodes", {})
        if not nid or nid not in nodes:
            return
        combo = self.prev_combo if which == "prev" else self.next_combo
        text = combo.get()
        if text.startswith(("（不接入", "(不接入")):
            if nid in ch:
                # 指着它的东西：别的节点的"下一个"、以及分支/枝干的出口。
                # 有的话不能移出主链 —— 出口指着谁，谁就在流程里。否则生成物里
                # 会出现指向不存在节点的引用，引擎会拒绝【整个任务包】。
                refs = [n for n in nodes
                        if n != nid and (nodes[n] or {}).get("next") == nid]
                refs += [n for n in ch if nid in exit_targets_of(self.flow, n)]
                if refs:
                    where = "、".join(self._node_ref_label(n)
                                      for n in sorted(set(refs)))
                    self.log(f"{self._node_ref_label(nid)}还被 {where} 指着"
                             f"（下一个 / 出口），不能移出流程 —— 指着谁，谁就在流程里；"
                             f"不然生成物会指向不存在的节点，引擎会拒绝加载整个任务包。"
                             f"要移出的话，先把那些边改成别的节点或「到此结束」。", "err")
                    self._sync_conn_ui()
                    return
                self._snapshot(f"conn:{nid}:detach")
                ch.remove(nid)
                for n in nodes:      # 兜底：别留指向它的悬空边
                    if (nodes[n] or {}).get("next") == nid:
                        nodes[n]["next"] = None
                self.build_prop_panel()
                self.redraw()
                self.log(f"已把{self._node_ref_label(nid)}移出主链"
                         f"（不参与生成，卡片留在原地）", "warn")
            return
        tgt = self._node_ref_id(text) or None
        if tgt == nid:
            tgt = None
        if which == "next":
            self._snapshot(f"conn:{nid}:next")
            pulled = self._set_next(nid, tgt)
            if nid not in ch:
                # 我在编辑它，说明它要用 → 接进链（不接进链的节点不生成）
                self._ensure_in_chain(nid, None)
                self.log(f"（{self._node_ref_label(nid)}原本还没接进流程，已接入）", "warn")
            self.build_prop_panel()
            self.redraw()
            msg = (f"已改：{self._node_ref_label(nid)}的下一个 → "
                   + (self._node_ref_label(tgt) if tgt else "（无：到此结束）"))
            if pulled:
                msg += f"；{self._node_ref_label(tgt)}原本还没接进流程，已一并接入"
            self.log(msg, "ok")
        else:
            if tgt is None:
                self.log("「上一个」得选一个节点；要把自己移出流程请选"
                         "「（不接入流程：断开）」", "warn")
                self._sync_conn_ui()
                return
            self._snapshot(f"conn:{nid}:prev")
            self._set_next(tgt, nid)
            if tgt not in ch:
                self._ensure_in_chain(tgt, None)
            if nid not in ch:
                self._ensure_in_chain(nid, tgt)
            self.build_prop_panel()
            self.redraw()
            self.log(f"已改：{self._node_ref_label(tgt)}的下一个 → "
                     f"{self._node_ref_label(nid)}（它排在我前面）", "ok")
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
        options = [f"#{node_no(self.flow, n)} {self.flow['nodes'][n].get('title', n)}"
                   for n in ch]
        if nd["type"] != "loop":
            # ★ 出口（✓/✗、全部未中）指向的是具体节点，未接入流程的同样要能在这里选到
            #   （选了就顺手接进链，见 on_branch_combo）—— 与「连接」下拉同一条规则。
            #   不列【输入】（设计上离链）；【循环】也不列：它的「循环体末尾」是链上的
            #   位置标记，不是"指向哪个节点"，离链节点在那里没有意义。
            options += [f"#{node_no(self.flow, n)} "
                        f"{self.flow['nodes'][n].get('title', n)}（未接入）"
                        for n in sorted(
                            (x for x in self.flow["nodes"]
                             if x not in ch and x != nid
                             and (self.flow["nodes"][x] or {}).get("type") not in ("input", "pick")),
                            key=lambda x: node_no(self.flow, x, 0))]
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
                text=f"候选 {n_cand} 个。★ 球上的数字 = 判定顺序：先试 1，命中就走 1 "
                     f"那条线连到的节点；不中再试 2；全不中走红球(✗)。"
                     f"每个候选判什么，取决于它连到的那个【分支】——"
                     f"模板图/OCR 文字在分支里配，这里不选识别方式。")
            return
        if nd["type"] != "branch":
            _disable_combos()
            self.branch_hint.config(text="该节点类型没有分支出口（只有「分支」/「枝干判定(多路)」节点有出口）。")
            return
        self.hit_combo.config(values=["（不指定：命中即结束）"] + options,
                               state="readonly")
        self.miss_combo.config(values=["(流程结束)"] + options, state="readonly")
        self.hit_combo.set(self._branch_target_label(nid, nd.get("hit_next"), True))
        self.miss_combo.set(self._branch_target_label(nid, nd.get("miss_next"), False))
        self.branch_hint.config(text="★ 分支默认谁也不连：✓命中/✗未中都要拖卡片右侧的圆点"
                                     "显式连线，不连就是「命中/未中即结束」，"
                                     "不会自动走链上下一个。拖到空白处断开；"
                                     "✗ 跳回前面的节点 = 循环。")

    def on_branch_combo(self, port):
        nid = self.sel
        if not nid or nid not in self.flow["nodes"]:
            return
        nd = self.flow["nodes"][nid]
        if nd["type"] not in ("branch", "switch", "loop"):
            return
        combo = self.hit_combo if port == "hit_next" else self.miss_combo
        target = self._node_ref_id(combo.get()) or None
        if target:
            if target == nid:
                self.log("分支出口不能指向自己", "warn")
                self._sync_branch_ui()
                return
            self._snapshot(f"branch:{nid}:{port}")
            # ★ 两端都要接进链（见 _link_exit）：出口指向"不参与生成"的节点，生成物里
            #   就是一条指向不存在节点的 next，引擎会拒绝加载【整个任务包】；
            #   而本节点自己不在链上时，这条出口边在画布上根本画不出来。
            src_add, tgt_add = self._link_exit(nid, target)
            if src_add:
                self.log(f"（本节点{self._node_ref_label(nid)}还没接进流程，"
                         f"已先接到链尾 —— 否则这条出口连线在画布上画不出来）", "warn")
            if tgt_add:
                self.log(f"（{self._node_ref_label(target)}原本还没接进流程，"
                         f"已顺手插在本节点后面）", "warn")
            if self.flow["nodes"][target].get("cut"):
                self.log(f"（注意：{self._node_ref_label(target)}标着「此处断开」，"
                         f"流程走到它就不再往下 —— 要让它继续，选中它、在「连接」里"
                         f"给它的「下一个」选一个节点）", "warn")
            if self.flow["nodes"][target].get("next"):
                self.log(f"（提示：{self._node_ref_label(target)}自己的「下一个」仍是"
                         f"{self._node_ref_label(self.flow['nodes'][target]['next'])}"
                         f"—— 连接只决定「谁指到它」，不碰它自己的下一个；"
                         f"不需要那条就选中它，把「下一个」改成「(无：到此结束)」）",
                         "warn")
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
        if self.bg_pil is None:
            # 只要求"有帧"，不要求"帧显示在画布上" —— 帧窗口里取点跟画布背景无关
            self.status("取点需要先有帧：先抓帧（F5）或打开帧图")
            self.log("⚠ 取点失败：还没有帧画面。先点「⟳ 抓帧 (F5)」再取点。", "warn")
            return
        self.pick_target = key
        self._open_frame_window()          # ★ 在独立帧窗口里点，节点卡片不会挡
        self.status(f"取点模式：{PICK_LABELS[key]}"
                    f"（在「帧画面」窗口里点一下即可，Esc 取消）")
        self.root.bind("<Escape>", self.on_escape)

    def _cancel_pick(self, _e):
        self.pick_target = None
        self.status("已取消取点")

    def _apply_pick(self, pt, key=None):
        """把取到的坐标写进当前选中节点的对应字段。

        ★ key 必须由调用方显式传进来：on_down 取完点先把 self.pick_target 清成了 None
          再调用这里，以前这里又去读 self.pick_target（已是 None）→ pairs[None] 直接
          KeyError，异常被 Tkinter 吞掉 → 表现就是"点了取点没反应"。"""
        key = key or self.pick_target
        if key not in PICK_LABELS or not self.sel:
            return
        x, y = pt
        props = self.flow["nodes"][self.sel]["props"]
        pairs = {"x": ("x", "y"), "x1": ("x1", "y1"), "x2": ("x2", "y2")}
        self._snapshot(f"pick:{self.sel}")
        ka, kb = pairs[key]
        props[ka], props[kb] = x, y
        # ③ 滑动要取两个点：取完起点自动接着取终点（少点一次按钮；Esc 退出）
        nxt = {"x1": "x2"}.get(key)
        if nxt in pairs:
            self.pick_target = nxt
            self.status(f"已取 {PICK_LABELS[key]} ({x},{y})；接着取 "
                        f"{PICK_LABELS[nxt]}（在帧窗口再点一下，Esc 取消）")
        else:
            self.pick_target = None
            self.status(f"已取 {PICK_LABELS[key]} ({x},{y})")
        self.build_prop_panel()
        self.redraw()
        self._refresh_frame_window()

    # ---------- 帧画面窗口：取点专用（节点卡片不会挡在帧上面） ----------
    # 以前取点要在画布上点，而画布上节点卡片是盖在帧上面的 —— 想看准一个点很难，
    # 现在单独开一个窗口显示原尺寸帧，点一下就取到坐标。

    def _open_frame_window(self):
        win = getattr(self, "frame_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            self._refresh_frame_window()
            return win
        win = tk.Toplevel(self.root)
        self.frame_win = win
        win.title("帧画面 · 取点（点一下即取坐标）")
        win.configure(bg=THEME["panel"])
        win.geometry("+%d+%d" % (self.root.winfo_rootx() + 120,
                                 self.root.winfo_rooty() + 80))
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        self._flat_btn(bar, "适配窗口", lambda: self._fw_set_scale(None),
                       padx=6, font=FONT_SM).pack(side="left")
        self._flat_btn(bar, "100%", lambda: self._fw_set_scale(1.0),
                       padx=6, font=FONT_SM).pack(side="left", padx=4)
        self._flat_btn(bar, "200%", lambda: self._fw_set_scale(2.0),
                       padx=6, font=FONT_SM).pack(side="left")
        self.fw_pos = ttk.Label(bar, text="", style="Dim.TLabel")
        self.fw_pos.pack(side="left", padx=14)
        self.fw_hint = ttk.Label(bar, text="", style="Dim.TLabel")
        self.fw_hint.pack(side="right")
        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.fw_canvas = tk.Canvas(wrap, bg="#0e1017", highlightthickness=0)
        self.fw_xsb = ttk.Scrollbar(wrap, orient="horizontal")
        self.fw_ysb = ttk.Scrollbar(wrap, orient="vertical")
        self.fw_canvas.configure(xscrollcommand=self.fw_xsb.set,
                                 yscrollcommand=self.fw_ysb.set)
        self.fw_xsb.config(command=self.fw_canvas.xview)
        self.fw_ysb.config(command=self.fw_canvas.yview)
        self.fw_ysb.pack(side="right", fill="y")
        self.fw_xsb.pack(side="bottom", fill="x")
        self.fw_canvas.pack(side="left", fill="both", expand=True)
        self.fw_canvas.bind("<Button-1>", self._fw_press)
        self.fw_canvas.bind("<B1-Motion>", self._fw_drag)
        self.fw_canvas.bind("<ButtonRelease-1>", self._fw_release)
        self.fw_canvas.bind("<Motion>", self._fw_motion)
        self._fw_box = None
        self.fw_canvas.bind("<MouseWheel>",
                            lambda e: self.fw_canvas.yview_scroll(
                                int(-e.delta / 120), "units"))
        win.bind("<Escape>", self._cancel_pick)
        win.bind("<Configure>", lambda e: self._fw_on_resize())
        self.fw_canvas.bind("<Button-3>",
                            lambda e: self.fw_canvas.scan_mark(e.x, e.y))
        self.fw_canvas.bind("<B3-Motion>",
                            lambda e: self.fw_canvas.scan_dragto(e.x, e.y, gain=1))
        self._fw_last_size = (0, 0)
        self._refresh_frame_window()
        return win

    def _fw_set_scale(self, sc):
        self.fw_scale = sc
        self._fw_last_size = (0, 0)
        self._refresh_frame_window()

    def _fw_on_resize(self):
        """窗口大小变了：适配模式下要重新缩放（否则留一片黑）"""
        if getattr(self, "fw_scale", None) is not None:
            return
        c = getattr(self, "fw_canvas", None)
        if c is None or not c.winfo_exists():
            return
        size = (c.winfo_width(), c.winfo_height())
        if size == getattr(self, "_fw_last_size", None):
            return
        self._fw_last_size = size
        self._refresh_frame_window()

    def _refresh_frame_window(self):
        if getattr(self, "frame_win", None) is None or not self.frame_win.winfo_exists():
            return
        c = self.fw_canvas
        c.delete("all")
        img = getattr(self, "_frame_img", None)
        if img is None:
            c.create_text(14, 14, anchor="nw", fill=THEME["text_dim"], font=FONT,
                          text="还没有帧画面：点左侧「⟳ 抓帧 (F5)」或「📂 打开帧图」")
            if hasattr(self, "fw_hint"):
                self.fw_hint.config(text="")
            return
        W, H = img.size
        c.update_idletasks()
        if getattr(self, "fw_scale", None) is None:
            vw, vh = max(200, c.winfo_width()), max(200, c.winfo_height())
            sc = min(vw / W, vh / H, 1.0)
        else:
            sc = float(self.fw_scale)
        self.fw_sc = max(0.05, sc)
        disp = img.resize((max(1, int(W * self.fw_sc)), max(1, int(H * self.fw_sc))),
                          Image.LANCZOS)
        self.fw_photo = ImageTk.PhotoImage(disp)
        c.create_image(0, 0, image=self.fw_photo, anchor="nw")
        c.configure(scrollregion=(0, 0, disp.size[0], disp.size[1]))
        for (mx, my) in getattr(self, "_fw_marks", []):
            px, py = mx * self.fw_sc, my * self.fw_sc
            c.create_line(px - 9, py, px + 9, py, fill="#ffd75e", width=2)
            c.create_line(px, py - 9, px, py + 9, fill="#ffd75e", width=2)
            c.create_text(px + 12, py - 12, anchor="sw", fill="#ffd75e",
                          font=FONT_SM, text=f"{mx},{my}")
        tips = self._fw_draw_overlay(c)
        if hasattr(self, "fw_hint"):
            mode = (f"取点中：点一下即取「{PICK_LABELS[self.pick_target]}」"
                    if self.pick_target else
                    ("框选中：拖出一个矩形即可（Esc 取消）"
                     if self.roi_pick else
                     "先在右侧点「✛ 取点 / ✛ 框选」再回到这里点"))
            self.fw_hint.config(
                text=f"帧 {W}x{H} · 显示 {int(self.fw_sc * 100)}% · {mode}"
                     + ("　|　" + "；".join(tips) if tips else ""))

    def _fw_to_frame(self, e):
        """窗口里的点击 → 帧原图坐标"""
        sc = getattr(self, "fw_sc", 1.0) or 1.0
        return int(self.fw_canvas.canvasx(e.x) / sc), int(self.fw_canvas.canvasy(e.y) / sc)

    def _fw_motion(self, e):
        if hasattr(self, "fw_pos") and getattr(self, "_frame_img", None) is not None:
            x, y = self._fw_to_frame(e)
            self.fw_pos.config(text=f"({x}, {y})")

    def _fw_press(self, e):
        """窗口里按下：框选模式 → 起框；取点模式 → 直接取这一点。"""
        if getattr(self, "_frame_img", None) is None:
            return
        W, H = self._frame_img.size
        x, y = self._fw_to_frame(e)
        if not (0 <= x < W and 0 <= y < H):
            return
        if self.roi_pick:                     # 框选 ROI：起框
            self._fw_box = [x, y, x, y]
            self._fw_paint_box()
            return
        key = self.pick_target
        if key:
            self._fw_marks = getattr(self, "_fw_marks", [])
            self._fw_marks.append((x, y))
            self._apply_pick((x, y), key)
            self.log(f"✓ 已取 {PICK_LABELS[key]} ({x},{y})", "ok")
        else:
            self.status(f"帧坐标 ({x},{y}) —— 先在右侧属性面板点「✛ 取点」再点这里")
        if hasattr(self, "fw_pos"):
            self.fw_pos.config(text=f"({x},{y})")

    def _fw_drag(self, e):
        if self._fw_box is not None:
            x, y = self._fw_to_frame(e)
            self._fw_box[2], self._fw_box[3] = x, y
            self._fw_paint_box()

    def _fw_release(self, e):
        box = self._fw_box
        self._fw_box = None
        if box is None:
            return
        rp = self.roi_pick
        self.roi_pick = None
        if not rp:
            self._refresh_frame_window()
            return
        if abs(box[2] - box[0]) < 4 or abs(box[3] - box[1]) < 4:
            self.log("⚠ 框太小，未写入", "warn")
            self._refresh_frame_window()
            return
        if self._write_roi(rp["key"], box[0], box[1], box[2], box[3]):
            self._refresh_frame_window()

    def _fw_paint_box(self):
        """把正在拖的框画出来（窗口像素）"""
        c = getattr(self, "fw_canvas", None)
        if c is None or self._fw_box is None:
            return
        c.delete("fwbox")
        sc = getattr(self, "fw_sc", 1.0) or 1.0
        x0, y0, x1, y1 = (v * sc for v in self._fw_box)
        c.create_rectangle(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1),
                           outline="#7fd0ff", width=2, dash=(4, 3), tags="fwbox")

    def _fw_draw_overlay(self, c):
        """把当前选中节点已填的坐标/框叠在帧上 —— 取完一眼看出对不对。
        返回说明文字（拼到窗口那行提示里）。"""
        nid = self.sel
        nd = (self.flow.get("nodes") or {}).get(nid) if nid else None
        if not isinstance(nd, dict):
            return []
        p = nd.get("props") or {}
        sc = getattr(self, "fw_sc", 1.0) or 1.0
        tips = []

        def num(k):
            try:
                return int(float(p.get(k)))
            except (TypeError, ValueError):
                return None

        roi = parse_roi(p.get("roi", ""))
        if roi:
            x, y, w, h = roi
            c.create_rectangle(x * sc, y * sc, (x + w) * sc, (y + h) * sc,
                               outline="#7fd0ff", width=2, dash=(4, 3))
            tips.append(f"ROI {x},{y},{w},{h}")
        t = nd.get("type")
        if t == "swipe":
            x1, y1, x2, y2 = num("x1"), num("y1"), num("x2"), num("y2")
            if None not in (x1, y1, x2, y2):
                c.create_line(x1 * sc, y1 * sc, x2 * sc, y2 * sc, fill="#ffd75e",
                              width=2, arrow=tk.LAST)
                for (px, py) in ((x1, y1), (x2, y2)):
                    c.create_oval(px * sc - 5, py * sc - 5, px * sc + 5, py * sc + 5,
                                  outline="#ffd75e", width=2)
                tips.append(f"滑动 ({x1},{y1})→({x2},{y2})")
        elif t == "tap":
            x, y = num("x"), num("y")
            if None not in (x, y):
                c.create_line(x * sc - 11, y * sc, x * sc + 11, y * sc,
                              fill="#ffd75e", width=2)
                c.create_line(x * sc, y * sc - 11, x * sc, y * sc + 11,
                              fill="#ffd75e", width=2)
                tips.append(f"点击 ({x},{y})")
        return tips
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
        self._update_undo_buttons()

    def _update_undo_buttons(self):
        """把可撤销/可重做的步数显示在按钮上（还能当"改了没保存"的提示）"""
        for btn, n, label in ((getattr(self, "_btn_undo", None), len(self._undo), "↶ 撤回"),
                              (getattr(self, "_btn_redo", None), len(self._redo), "↷ 重做")):
            if btn is None:
                continue
            try:
                btn.config(text=f"{label}({n})" if n else label,
                           state=("normal" if n else "disabled"))
            except tk.TclError:
                pass

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
        self._update_undo_buttons()
        self.log(f"↶ 已撤销（还可撤销 {len(self._undo)} 步，Ctrl+Y 重做）")

    def redo(self):
        if not self._redo:
            self.log("没有可重做的操作", "warn")
            return
        self._undo.append((self._flow_text(), self._redo[-1][1], time.time()))
        text = self._redo.pop()[0]
        self._restore_flow_text(text)
        self._update_undo_buttons()
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
        nd["num"] = self._next_num()      # 复制品必须换号，否则两张卡片同号
        # 出口一律清空：出口指向的是具体节点，复制品沿用会指向原来的节点
        nd["hit_next"] = None
        nd["miss_next"] = None
        if isinstance(nd.get("props", {}).get("candidates"), list):
            for c in nd["props"]["candidates"]:
                if isinstance(c, dict):
                    c.pop("next", None)
        anchor = self.sel if self.sel in self.flow["chain"] else None
        placed_in_chain = bool(anchor)
        if anchor:
            base = self.flow["nodes"][anchor]
            nd["x"] = base["x"] + 40
            nd["y"] = base["y"] + (self._sw_h(base) if base["type"] == "switch"
                                   else CARD_H) + 30
            self._chain_insert(self.flow["chain"].index(anchor) + 1, nid)
            # 边：锚点 → 复制品 → 锚点原来的下一个（原样往后挪一位）
            base_nd = self.flow["nodes"][anchor]
            nd["next"] = base_nd.get("next")
            base_nd["next"] = nid
            base_nd.pop("cut", None)
        else:
            # 与"新建节点"同一条规则：没选中链上的节点就不接进流程，放未接入区
            nd["next"] = None
            nd["x"], nd["y"] = self._spot_in_view(card_h(nd))
        self.flow["nodes"][nid] = nd
        self.sel = nid
        self.build_prop_panel()
        self.redraw()
        self._scroll_to(nd["y"])
        self.log(f"已粘贴节点「{nd.get('title', nid)}」（出口未连线，请重新连）"
                 if placed_in_chain else
                 f"已粘贴节点「{nd.get('title', nid)}」：当前没选中链上的节点，"
                 f"所以它【没有接进流程】（放在最右侧未接入区）；"
                 f"要接进去：在「连接」里把「下一个」选成一个节点",
                 None if placed_in_chain else "warn")

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
        nd["x"] = self._snap(max(0, nd["x"] + dx))
        nd["y"] = self._snap(max(0, nd["y"] + dy))
        self.redraw()
        self.status(f"节点坐标 → ({int(nd['x'])}, {int(nd['y'])})"
                    f"（网格 {GRID}px；方向键一格 / Shift 四格）")

    def on_escape(self, _e=None):
        """Esc：取消 ROI 拖框 / 取消取点 / 取消正在拖的连线 / 取消框选"""
        if self._pan_left:              # 正在用左键平移：松手前按 Esc = 停下
            self._pan_left = False
            try:
                self.canvas.config(cursor="")
            except tk.TclError:
                pass
            self.status("已停止平移")
            return
        if self.multi:
            self.multi = set()
            self.band = None
            self.redraw()
            self.status("已取消框选")
            return
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
        if self.bg_pil is None:
            self.status("框选 ROI 需要先有帧：先抓帧（F5）或打开帧图")
            self.log("⚠ 框选失败：还没有帧画面。先点「⟳ 抓帧 (F5)」。", "warn")
            return
        self.roi_pick = {"key": key, "x0": None, "y0": None, "x1": None, "y1": None}
        self._open_frame_window()          # ★ 在独立窗口里拖框，节点卡片不会挡
        self.status("拖框模式：在「帧画面」窗口里拖出识别区域（Esc 取消）")

    def _write_roi(self, key, x0, y0, x1, y1):
        """把框（帧坐标）写进 ROI 字段："x,y,w,h"。太小/越界都拦一下。"""
        if not self.sel or self.sel not in self.flow["nodes"]:
            return False
        W, H = self.frame_wh
        x0, x1 = sorted((max(0, x0), min(W, x1)))
        y0, y1 = sorted((max(0, y0), min(H, y1)))
        if x1 - x0 < 4 or y1 - y0 < 4:
            self.log("⚠ 框选太小（不足 4px），未写入", "warn")
            self.status("框选太小，未写入")
            return False
        self._snapshot(f"roi:{self.sel}")
        self.flow["nodes"][self.sel]["props"][key] = f"{x0},{y0},{x1 - x0},{y1 - y0}"
        self.build_prop_panel()
        self.redraw()
        self.log(f"✓ 已写入 ROI {x0},{y0},{x1 - x0},{y1 - y0}"
                 f"（帧坐标，基准 {W}×{H}）", "ok")
        return True

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
            self.log("⚠ 框选超出帧画面范围，未写入（建议在「帧画面」窗口里框）", "warn")
            self.status("框选无效")
            return
        self._write_roi(rp["key"], a[0], a[1], b[0], b[1])

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
                register_on_phone(self.flow["name"], tlog,
                                  options=flow_input_options(self.flow))
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
    # ★ 命中出口没连线 = 命中即结束（不再自动走链上下一个 —— 那条隐式边看不见）
    assert "next" not in out[f"{E}_03_Hit"], out[f"{E}_03_Hit"]
    assert out[f"{E}_03_Hit"]["roi"] == [100, 200, 300, 400]
    # 显式连上命中出口 → 才继续走那个节点
    flow["nodes"]["c"]["hit_next"] = "d"
    out_h = build_pipeline(flow, (1280, 720))
    assert out_h[f"{E}_03_Hit"]["next"] == [f"{E}_04"], out_h[f"{E}_03_Hit"]
    # 老流程的隐式边由 normalize_flow 补成显式连线（生成物因此一字不变）
    legacy = {"name": "老分支", "chain": ["c", "d"],
              "nodes": {"c": {"type": "branch", "x": 0, "y": 0, "title": "c",
                              "props": {"template": "yanxun.png", "threshold": 0.7,
                                        "ocr_text": "", "roi": "", "timeout": 3000,
                                        "rate_limit": 0},
                              "hit_next": None, "miss_next": None},
                        "d": {"type": "tap", "x": 0, "y": 0, "title": "d",
                              "props": {"x": 1, "y": 1, "pre_delay": 0,
                                        "post_delay": 500, "post_wait_freezes": 0,
                                        "repeat": 1, "repeat_delay": 350}}}}
    lg = normalize_flow(legacy)
    assert lg["nodes"]["c"]["hit_next"] == "d", lg["nodes"]["c"]
    out_l = build_pipeline(lg, (1280, 720))
    assert out_l[f"VF_老分支_01_Hit"]["next"] == ["VF_老分支_02"], out_l["VF_老分支_01_Hit"]
    # 已经带版本号的流程【不再补】：新建分支"没连线"就是没连线 → 生成物里没有 next
    fresh = json.loads(json.dumps(legacy))
    fresh["schemaVersion"] = SCHEMA_VERSION
    fresh["nodes"]["c"]["hit_next"] = None
    fr = normalize_flow(fresh)
    assert fr["nodes"]["c"].get("hit_next") is None, fr["nodes"]["c"]
    assert "next" not in build_pipeline(fr, (1280, 720))["VF_老分支_01_Hit"]
    flow["nodes"]["c"]["hit_next"] = None
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
    # 枝干判定（switch）：候选不自己配识别目标 —— 判定块取自它连到的【分支】节点，
    # 并且候选 t 字段（旧写法）必须被忽略
    sf = {"name": "枝干测", "chain": ["s", "c1", "c2"],
          "nodes": {
              "s": {"type": "switch", "x": 0, "y": 0, "title": "s",
                    "props": {"candidates": [
                        {"timeout": 3000, "next": "c1"},
                        {"t": "OCR:旧字段应被忽略", "timeout": 2000, "next": "c2"}],
                        "miss_next": None}},
              "c1": {"type": "branch", "x": 0, "y": 0, "title": "c1",
                     "props": {"template": "yanxun.png", "threshold": 0.65,
                               "ocr_text": "", "roi": "", "timeout": 3000,
                               "rate_limit": 0},
                     "hit_next": None, "miss_next": None},
              "c2": {"type": "branch", "x": 0, "y": 0, "title": "c2",
                     "props": {"template": "", "threshold": 0.7, "ocr_text": "确认",
                               "roi": "", "timeout": 3000, "rate_limit": 0},
                     "hit_next": None, "miss_next": None}}}
    errs, _ = validate_flow(sf, (1280, 720))
    assert not errs, f"switch 校验报错: {errs}"
    out_sw = build_pipeline(sf, (1280, 720))
    B = "VF_枝干测"
    assert out_sw[f"{B}_01_J1"]["on_error"] == [f"{B}_01_J2"]
    assert out_sw[f"{B}_01_J1_Hit"]["next"] == [f"{B}_02"]
    assert out_sw[f"{B}_01_J2"]["on_error"] == [f"{B}_End"]
    # 候选的判定块 == 它连到的分支的判定块（模板/阈值/roi 只配一处，两处永远一致）
    assert out_sw[f"{B}_01_J1_Hit"]["recognition"] == "TemplateMatch"
    assert out_sw[f"{B}_01_J1_Hit"]["template"] == "yanxun.png"
    assert out_sw[f"{B}_01_J1_Hit"]["threshold"] == 0.65, out_sw[f"{B}_01_J1_Hit"]
    assert out_sw[f"{B}_01_J2_Hit"]["recognition"] == "OCR"
    assert out_sw[f"{B}_01_J2_Hit"]["expected"] == ["确认"], out_sw[f"{B}_01_J2_Hit"]
    # 内容叶：分支容器指向自己的 _Hit，但命中后不再沿线性链继续（防分支串线）
    assert out_sw[f"{B}_02"]["next"] == [f"{B}_02_Hit"]
    assert "next" not in out_sw[f"{B}_02_Hit"] and "next" not in out_sw[f"{B}_03_Hit"], \
        "内容叶分支命中后不应沿线性链继续（防分支串线）"
    assert f"{B}_End" in out_sw
    # 候选没连线 → 取不到判定条件，校验必须报错
    sf["nodes"]["s"]["props"]["candidates"][0].pop("next")
    errs, _ = validate_flow(sf, (1280, 720))
    assert errs and any("还没连到内容起点" in e for e in errs), errs
    # 候选连到非分支节点 → 同样取不到条件，校验必须报错
    sf["nodes"]["s"]["props"]["candidates"][0]["next"] = "t1"
    sf["nodes"]["t1"] = {"type": "tap", "x": 0, "y": 0, "title": "t1",
                         "props": {"x": 1, "y": 1, "pre_delay": 0, "post_delay": 500,
                                   "post_wait_freezes": 0, "repeat": 1,
                                   "repeat_delay": 350}}
    sf["chain"].append("t1")
    errs, _ = validate_flow(sf, (1280, 720))
    assert errs and any("不是分支节点" in e for e in errs), errs
    # 【输入】节点：离链的"参数声明" —— 不生成管线节点，注入目标存在 targets 列表里
    cf = {"name": "输入测", "chain": ["s2", "br", "in1"],
          "nodes": {
              "s2": {"type": "startapp", "x": 0, "y": 0, "title": "s2",
                     "props": {"package": "a.b", "post_delay": 100}},
              "br": {"type": "branch", "x": 0, "y": 0, "title": "选角色",
                     "props": {"template": "蛙锣.png", "threshold": 0.7,
                               "ocr_text": "", "roi": "10,20,100,50",
                               "timeout": 3000, "rate_limit": 0},
                     "hit_next": None, "miss_next": None},
              "in1": {"type": "input", "x": 400.0, "y": 46.0, "title": "目标角色",
                      "props": {"option": "目标角色", "var": "角色",
                                "var_label": "角色名", "kind": "文本",
                                "default": "蛙锣", "desc": "填角色名",
                                "targets": ["br"], "field": "模板图 (template)"}}}}
    # 老文件里输入节点在链上 → normalize_flow 要把它移出主链
    cf = normalize_flow(cf)
    assert cf["chain"] == ["s2", "br"], cf["chain"]
    errs, _ = validate_flow(cf, (1280, 720))
    assert not errs, f"input 校验报错: {errs}"
    out_in = build_pipeline(cf, (1280, 720))
    J = "VF_输入测"
    assert f"{J}_03" not in out_in, "【输入】节点不该生成任何管线节点"
    opts = flow_input_options(cf)
    assert list(opts) == ["目标角色"], opts
    op = opts["目标角色"]
    assert op["type"] == "input" and op["label"] == "目标角色", op
    assert op["description"] == "填角色名", op
    assert op["inputs"] == [{"name": "角色", "label": "角色名", "default": "蛙锣",
                             "pipeline_type": "string"}], op["inputs"]
    # 识别类字段落到分支的 *_Hit（识别块在那儿），值默认 "{变量}.png"
    assert op["pipeline_override"] == {f"{J}_02_Hit": {"template": "{角色}.png"}}, op
    # ★ 一个参数可以注入多个节点：targets 加一个 → override 就多一条
    #   （手写『目标角色』就是这样同时覆盖 CDZB2_Hit/升2_Hit/查2_Hit）
    cf["nodes"]["in1"]["props"]["targets"].append("s2")
    o2 = flow_input_options(cf)["目标角色"]
    assert set(o2["pipeline_override"]) == {f"{J}_02_Hit", f"{J}_01"}, o2
    assert o2["pipeline_override"][f"{J}_01"] == {"template": "{角色}.png"}, o2
    cf["nodes"]["in1"]["props"]["targets"] = ["s2"]
    # 整数类型 + 校验正则 → repeat（"提前设置次数"的通用做法）
    cf["nodes"]["in1"]["props"].update({
        "option": "次数", "var": "次数", "var_label": "次数", "kind": "整数",
        "default": "1", "field": "repeat",
        "verify": "^([1-9]|1[0-9]|20)$", "pattern_msg": "请输入 1~20 的整数"})
    o3 = flow_input_options(cf)["次数"]
    assert o3["inputs"][0]["pipeline_type"] == "int", o3
    assert o3["inputs"][0]["verify"] == "^([1-9]|1[0-9]|20)$", o3
    assert o3["inputs"][0]["pattern_msg"] == "请输入 1~20 的整数", o3
    assert o3["pipeline_override"] == {f"{J}_01": {"repeat": "{次数}"}}, o3
    errs, _ = validate_flow(cf, (1280, 720))
    assert not errs, f"input(整数) 校验报错: {errs}"
    # 一个注入目标都没有 / 目标已删除 / 字段名不认识 → 都要报错
    cf["nodes"]["in1"]["props"]["targets"] = []
    errs, _ = validate_flow(cf, (1280, 720))
    assert errs and any("还没连到要注入的节点" in e for e in errs), errs
    cf["nodes"]["in1"]["props"]["targets"] = ["nope"]
    errs, _ = validate_flow(cf, (1280, 720))
    assert errs and any("已删除" in e for e in errs), errs
    cf["nodes"]["in1"]["props"]["targets"] = ["s2"]
    cf["nodes"]["in1"]["props"]["field"] = "nesne"
    errs, _ = validate_flow(cf, (1280, 720))
    assert errs and any("不认识" in e for e in errs), errs
    # 字段名合法但目标节点自己不读它 → 只警告、不报错
    cf["nodes"]["in1"]["props"]["field"] = "roi"          # startapp 上没有 roi
    errs, warns = validate_flow(cf, (1280, 720))
    assert not errs, errs
    assert any("没有用到" in w for w in warns), warns
    cf["nodes"]["in1"]["props"].update({"field": "repeat", "option": ""})
    errs, _ = validate_flow(cf, (1280, 720))
    assert errs and any("还没填参数名" in e for e in errs), errs
    cf["nodes"]["in1"]["props"].update({"option": "次数", "verify": "(("})
    errs, _ = validate_flow(cf, (1280, 720))
    assert errs and any("不是合法正则" in e for e in errs), errs
    # 下拉里"中文 (字段名)"的写法与纯字段名等价（两种写法都要认）
    cf["nodes"]["in1"]["props"]["verify"] = ""
    assert input_field({"field": "模板图 (template)"}) == "template"
    assert input_field({"field": "repeat"}) == "repeat"
    cf["nodes"]["in1"]["props"]["field"] = "模板图 (template)"
    assert flow_input_options(cf)["次数"]["pipeline_override"] == {
        f"{J}_01": {"template": "{次数}.png"}}, flow_input_options(cf)
    # 同名参数拆成两个节点时也会合并成一份定义
    cf["nodes"]["in2"] = {"type": "input", "x": 400.0, "y": 200.0,
                          "title": "次数(第2处)",
                          "props": {"option": "次数", "var": "次数", "kind": "整数",
                                    "targets": ["s2"], "field": "repeat"}}
    errs, warns = validate_flow(cf, (1280, 720))
    assert not errs, errs
    om = flow_input_options(cf)["次数"]
    assert om["pipeline_override"] == {
        f"{J}_01": {"template": "{次数}.png", "repeat": "{次数}"}}, om
    assert len(om["inputs"]) == 1, om          # 合并后只有一份 inputs 定义
    # 同名但字段定义不一致 → 警告（App 只认第一份 inputs）
    cf["nodes"]["in3"] = {"type": "input", "x": 400.0, "y": 300.0,
                          "title": "次数(定义不一致)",
                          "props": {"option": "次数", "var": "别的变量", "kind": "整数",
                                    "targets": ["s2"], "field": "repeat_delay"}}
    errs, warns = validate_flow(cf, (1280, 720))
    assert not errs, errs
    assert any("字段定义不一样" in w for w in warns), warns
    # 布局：卡片高度按类型算 + 重叠检测（打开旧流程要能发现"全叠在一起"）
    lay = {"name": "布局测", "chain": ["s", "b"],
           "nodes": {"s": {"type": "switch", "x": 46.0, "y": 46.0, "title": "s",
                           "props": {"candidates": [{"timeout": 3000}] * 3,
                                     "miss_next": ""}},
                     "b": {"type": "tap", "x": 46.0, "y": 46.0, "title": "b",
                           "props": {"x": 100, "y": 100, "pre_delay": 0,
                                     "post_delay": 500, "post_wait_freezes": 0,
                                     "repeat": 1, "repeat_delay": 350}}}}
    assert card_h(lay["nodes"]["s"]) > card_h(lay["nodes"]["b"]), "枝干卡应当更高"
    assert overlapping_nodes(lay) == {"s", "b"}, overlapping_nodes(lay)
    lay["nodes"]["b"]["y"] = 46.0 + card_h(lay["nodes"]["s"]) + 26
    assert overlapping_nodes(lay) == set(), overlapping_nodes(lay)
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
    # ★ 出口指向"还没接进流程"的节点：那个节点不生成，生成物里就是一条指向不存在节点的
    #   next/on_error。本机 MaaFw 引擎实测会 check_next_list "Invalid next node name"
    #   → check_all_validity failed → 整包 loaded=False（手机上所有任务都跑不起来），
    #   所以必须是 error，不能只是 NODE_OFFCHAIN 那种警告。
    of9 = {"name": "离链出口", "chain": ["b1"],
           "nodes": {
               "b1": {"type": "branch", "x": 0, "y": 0, "title": "br",
                      "props": {"template": "yanxun.png", "threshold": 0.7,
                                "ocr_text": "", "roi": "", "timeout": 3000,
                                "rate_limit": 0},
                      "hit_next": None, "miss_next": "t9"},
               "t9": {"type": "tap", "x": 0, "y": 0, "title": "没接进来",
                      "props": {"x": 1, "y": 1, "pre_delay": 0, "post_delay": 500,
                                "post_wait_freezes": 0, "repeat": 1,
                                "repeat_delay": 350}}}}
    errs, _ = validate_flow(of9, (1280, 720))
    assert any("还没接进流程" in e for e in errs), errs
    # 生成入口也会被拦下（不会生成一条指向不存在节点的 on_error 再去推手机）
    try:
        build_pipeline(of9, (1280, 720))
        raise AssertionError("悬空出口竟然照样生成了")
    except FlowValidationError as ex:
        assert any("还没接进流程" in m for m in ex.errors), ex.errors
    # 把目标接回主链 → 引用有着落，校验转绿，生成物里的 on_error 真有对应节点
    of9["chain"].append("t9")
    errs, _ = validate_flow(of9, (1280, 720))
    assert not errs, errs
    dd = build_pipeline(of9, (1280, 720))
    assert dd["VF_离链出口_01"]["on_error"] == ["VF_离链出口_02"], dd["VF_离链出口_01"]
    assert "VF_离链出口_02" in dd
    # ★ v4 文件里缺 "next" 键 = 【到此结束】，不许回退到"链上后继"（只有老文件才回退）。
    #   否则一个没写 next 的链上节点（新建时没选中任何节点 → 放进未接入区 → 后来被接进链的
    #   那种）会悄悄连到链序里的下一个：博物研学里链尾是滑动 #22，于是"新建一个节点，
    #   画布上总莫名多出一根连到 #22 的线"，而且生成物里也真多一条 VF_x_36 → VF_x_22 的边。
    def _mk_tap(nid, y):
        return {"type": "tap", "x": 0, "y": y, "title": nid,
                "props": {"x": 1, "y": 1, "pre_delay": 0, "post_delay": 500,
                          "repeat": 1}}
    def _mk_pass(emit_name):
        return {"type": "pass", "x": 0, "y": 100, "title": "通道/跳转",
                "props": {"emit_name": emit_name}}
    v4f = {"schemaVersion": SCHEMA_VERSION, "name": "无next自测", "chain": ["a", "b"],
           "nodes": {"a": _mk_tap("a", 0), "b": _mk_tap("b", 100)}}
    assert linear_successor(v4f, "a") is None, "v4 缺 next 键应视为『到此结束』"
    assert "next" not in build_pipeline(v4f, (1280, 720))["VF_无next自测_01"], \
        "v4 缺 next 键的节点不该生成 next"
    v4f["nodes"]["a"]["next"] = "b"
    assert linear_successor(v4f, "a") == "b"
    old3 = {"schemaVersion": 3, "name": "老文件自测", "chain": ["a", "b"],
            "nodes": {"a": _mk_tap("a", 0), "b": _mk_tap("b", 100)}}
    assert linear_successor(old3, "a") == "b", "老文件仍要靠链序决定下一个"
    # ★ 【通道/跳转】：不识别不动作，只为给参数留一个能连线的位置；
    #   emit_name 指定"运行时的名字"（老流程要保留手写管线的节点名）。
    pf = {"schemaVersion": SCHEMA_VERSION, "name": "通道自测",
          "chain": ["p1", "p2"],
          "nodes": {"p1": _mk_tap("p1", 0), "p2": _mk_pass("征集段2")}}
    pf["nodes"]["p1"]["next"] = "p2"          # v4：边是显式的，测试里也得写
    pp = build_pipeline(pf, (1280, 720))
    assert pp["VF_通道自测_01"]["next"] == ["征集段2"], pp
    assert pp["征集段2"] == {"next": []}, pp["征集段2"]
    # 参数覆盖到"运行时名"上时，override 的键就是它（与手写管线一致）
    pf["nodes"]["i1"] = {"type": "input", "x": 0, "y": 0, "title": "输入", "num": 3,
                         "props": {"option": "P", "var": "", "var_label": "",
                                   "kind": "文本", "default": "", "desc": "",
                                   "target": "", "raw": "", "field": "next",
                                   "value": "ZJ_DuiGou2", "verify": "", "pattern_msg": "",
                                   "targets": ["p2"]}}
    o = flow_input_options(pf)["P"]
    assert o["pipeline_override"] == {"征集段2": {"next": ["ZJ_DuiGou2"]}}, o
    print("通道/跳转节点自测通过")

    # ★ 「同步到手机」写清单走的是 upsert_flow_task，它必须是**幂等**的：只按流程里的
    #   【输入】节点写参数，跑一遍和跑两遍结果一样。以前它把「group=tools 且 entry==VF_x」
    #   的条目一律删掉再追加 —— 【小工具】分组里的任务（查找器者 / 刷活动关 / 博物研学）
    #   每同步一次就被挪到清单末尾，标签页上手写的 label/description/default_check 一起丢。
    #   （迁移后查找器者的 entry 变成 VF_查找器者，正好踩到这条。）
    udata = {"task": [
        {"name": "查找器者", "label": "查找器者", "entry": "VF_查找器者",
         "group": ["tools"], "option": ["目标角色"], "description": "手写的说明"},
        {"name": "刷冬谷币", "label": "刷冬谷币", "entry": "VF_刷冬谷币",
         "group": ["battle"], "option": ["刷冬谷币次数"]},
    ], "option": {"目标角色": {"type": "input"}, "刷冬谷币次数": {"type": "select"}}}
    before = json.dumps(udata, ensure_ascii=False, sort_keys=True)
    upsert_flow_task(udata, "查找器者", options={"目标角色": {"type": "input"}})
    upsert_flow_task(udata, "刷冬谷币", options={"刷冬谷币次数": {"type": "select"}})
    assert json.dumps(udata, ensure_ascii=False, sort_keys=True) == before, udata
    # 真正的重复条目（同一个流程既转正、又留着一个【小工具】条目）仍然要清掉
    udata["task"].append({"name": "查找器者", "label": "查找器者",
                          "entry": "VF_查找器者", "group": ["tools"]})
    udata["task"].insert(0, {"name": "查找器者", "label": "查找器者",
                             "entry": "VF_查找器者", "group": ["daily"],
                             "option": ["目标角色"]})
    upsert_flow_task(udata, "查找器者", options={"目标角色": {"type": "input"}})
    assert [t["name"] for t in udata["task"]].count("查找器者") == 1, udata
    assert udata["task"][0]["group"] == ["daily"], udata   # 留下的是正式任务、位置不动
    print("清单同步幂等自测通过")

    # ★ 从入口走不到的节点必须能报出来（2026-09-15 的教训：重建流程时把链上的 next 整片丢了，
    #   任务跑完**第一个节点**就 task end [ret=true] —— 不报错不失败，引擎 loaded 还是 True，
    #   只有实跑/结构比对能发现）。所以它要成为一条常驻警告。
    dl = {"schemaVersion": SCHEMA_VERSION, "name": "死节点自测", "chain": ["u1", "u2"],
          "nodes": {k: _mk_tap(k, i * 100) for i, k in enumerate(("u1", "u2"))}}
    dl["nodes"]["u1"]["next"] = None          # 到此结束 → u2 没人指着，从入口走不到
    _errs, _warns = validate_pipeline(dl, (1280, 720))
    assert any("走不到" in w for w in _warns), _warns
    # 接上就没事了
    dl["nodes"]["u1"]["next"] = "u2"
    _errs, _warns = validate_pipeline(dl, (1280, 720))
    assert not any("走不到" in w for w in _warns), _warns
    print("死节点（从入口走不到）自测通过")
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

            # ★ 未接入流程的节点必须能在下拉里选到 —— 选中它【上游】的节点去连它，
            #   而不是只能反过来先选中它自己往链上接。以前候选只来自主链，
            #   于是画布上写着"未接入流程（不执行）"的卡片在「下一个」里根本找不到。
            def _tap(i):
                return {"type": "tap", "x": 0, "y": i * 200, "title": f"点{i}",
                        "num": i, "props": {"x": 1, "y": 1, "pre_delay": 0,
                                            "post_delay": 500, "repeat": 1}}
            ed2 = FlowEditor(root)
            ed2.flow = {"schemaVersion": SCHEMA_VERSION, "name": "连线自测",
                        "chain": ["n1", "n2"],
                        "nodes": {"n1": _tap(1), "n2": _tap(2),
                                  "n3": {"type": "branch", "x": 0, "y": 400,
                                         "title": "分支", "num": 10,
                                         "hit_next": None, "miss_next": None,
                                         "props": {"template": "t.png",
                                                   "threshold": 0.8, "ocr_text": "",
                                                   "roi": "", "timeout": 3000,
                                                   "rate_limit": 0}},
                                  "n4": dict(_tap(11), type="swipe")}}
            ed2.sel = "n1"
            ed2._sync_conn_ui()
            vals = list(ed2.next_combo.cget("values"))
            assert any(v.startswith("#10 ") and v.endswith("（未接入）")
                       for v in vals), vals
            assert any(v.startswith("#11 ") and v.endswith("（未接入）")
                       for v in vals), vals
            # 「下一个 = #10」= 它排在我后面（并顺手把它接进流程）
            ed2.next_combo.set([v for v in vals if v.startswith("#10 ")][0])
            ed2.on_conn_combo("next")
            assert ed2.flow["chain"] == ["n1", "n3", "n2"], ed2.flow["chain"]
            # 分支出口下拉同理：✓ 能连到还没接进流程的节点，选完它就在链上了
            ed2.sel = "n3"
            ed2._sync_branch_ui()
            hvals = list(ed2.hit_combo.cget("values"))
            assert any(v.startswith("#11 ") and v.endswith("（未接入）")
                       for v in hvals), hvals
            ed2.hit_combo.set([v for v in hvals if v.startswith("#11 ")][0])
            ed2.on_branch_combo("hit_next")
            assert ed2.flow["nodes"]["n3"]["hit_next"] == "n4", ed2.flow["nodes"]["n3"]
            assert ed2.flow["chain"] == ["n1", "n3", "n4", "n2"], ed2.flow["chain"]

            # ★ 连出口时【两端都要接进链】：只补目标的话，源节点还在链外 →
            #   目标被塞到链尾，画布上出现"链尾节点→目标"的直落箭头（用户看到的是
            #   "我想连 10→12，怎么成了 11→12"）；而真正的出口边因为源节点不在链上
            #   压根不画（出口连线以前只遍历主链）→ "日志说连上了、画布上一条线都没有"。
            ed3 = FlowEditor(root)
            br = {"type": "branch", "x": 0, "y": 300, "title": "分支", "num": 1,
                  "hit_next": None, "miss_next": None,
                  "props": {"template": "t.png", "threshold": 0.8, "ocr_text": "",
                            "roi": "", "timeout": 3000, "rate_limit": 0}}
            ed3.flow = {"schemaVersion": SCHEMA_VERSION, "name": "出口自测",
                        "chain": ["nB"],
                        "nodes": {"nB": _tap(2), "br": br, "nC": _tap(12)}}
            assert ed3._link_exit("br", "nC") == (True, True), "两端都该是新接入的"
            assert ed3.flow["chain"] == ["nB", "br", "nC"], ed3.flow["chain"]
            # 目标已在链上时只补源节点，不搬动目标（分支出口是显式边，与链序无关）
            ed3.flow["chain"] = ["nB"]
            assert ed3._link_exit("br", "nB") == (True, False)
            assert ed3.flow["chain"] == ["nB", "br"], ed3.flow["chain"]
            # 源已在链上：只把目标插到它后面
            ed3.flow["chain"] = ["br"]
            assert ed3._link_exit("br", "nC") == (False, True)
            assert ed3.flow["chain"] == ["br", "nC"], ed3.flow["chain"]
            # 出口边要【画得出来】：离链的分支也要画（灰虚线），否则用户以为没连上
            ed3.flow["nodes"]["br"]["hit_next"] = "nC"
            ed3.flow["chain"] = ["nB"]              # br 离链
            ed3.redraw()
            drawn = [(ed3.canvas.itemcget(i, "text"), ed3.canvas.itemcget(i, "fill"))
                     for i in ed3.canvas.find_all() if ed3.canvas.type(i) == "text"]
            assert ("#12", THEME["text_dim"]) in drawn, \
                f"离链分支的出口边应画成灰虚线，画布上没找到: {drawn}"
            ed3.flow["chain"] = ["nB", "br"]        # br 接入链 → 换成正常配色
            ed3.redraw()
            drawn = [(ed3.canvas.itemcget(i, "text"), ed3.canvas.itemcget(i, "fill"))
                     for i in ed3.canvas.find_all() if ed3.canvas.type(i) == "text"]
            assert ("#12", THEME["ok"]) in drawn, f"接入链后应画成正常配色: {drawn}"

            # ★ 「上一个 / 下一个」现在只改【那一条边】（nd["next"]）：
            #   不挪节点、不改链序、不牵动别的连接 —— 不会冒出用户没画过的线。
            ed4 = FlowEditor(root)
            ed4.flow = {"schemaVersion": SCHEMA_VERSION, "name": "边自测",
                        "chain": ["p1", "p2", "p3", "p5", "p4", "p6"],
                        # 编号要和名字对上（#4 就是 p4）：node_ref_id 是按 num 反查的
                        "nodes": {n: _tap(int(n[1:])) for n in
                                  ("p1", "p2", "p3", "p5", "p4", "p6")}}
            for _a, _b in zip(ed4.flow["chain"], ed4.flow["chain"][1:]):
                ed4.flow["nodes"][_a]["next"] = _b

            def _pick(combo, num):
                combo.set([v for v in combo.cget("values")
                           if v.startswith(f"#{num} ")][0])

            def _next_of(nid):
                return ed4.flow["nodes"][nid].get("next")

            # 1) 「#3 的下一个 = #4」：只把 #3→#4 这条边改出来。
            #    #3 的位置、它的上一个（#2→#3）、#4 自己的下一个（#6）都不许动 ——
            #    用户原话："我把 3 接到 4，4 却自动连到 11 了"就是以前的链序副作用。
            ed4.sel = "p3"
            ed4._sync_conn_ui()
            _pick(ed4.next_combo, 4)
            ed4.on_conn_combo("next")
            assert _next_of("p3") == "p4", _next_of("p3")
            assert _next_of("p2") == "p3", "上面的连接不能动"
            assert _next_of("p4") == "p6", "#4 自己的下一个不能动"
            assert _next_of("p5") == "p4", "没被点到的边都不能动"
            assert ed4.flow["chain"] == ["p1", "p2", "p3", "p5", "p4", "p6"], \
                "链序（显示顺序）也不该动"
            # 2) 「(无：到此结束)」= 这条边没有了
            ed4.sel = "p3"
            ed4._sync_conn_ui()
            ed4.next_combo.set("(无：到此结束)")
            ed4.on_conn_combo("next")
            assert _next_of("p3") is None
            assert ed4.flow["chain"] == ["p1", "p2", "p3", "p5", "p4", "p6"]
            # 3) 「上一个 = #1」= 把 #1 的下一个改成"我"（别的都不动）
            ed4.sel = "p3"
            ed4._sync_conn_ui()
            _pick(ed4.prev_combo, 1)
            ed4.on_conn_combo("prev")
            assert _next_of("p1") == "p3", _next_of("p1")
            assert _next_of("p2") == "p3", "别人的边不受影响"
            assert _next_of("p4") == "p6"
            # 4) 生成结果跟着这条边走（不再看链序）
            out_n = build_pipeline(normalize_flow(ed4.flow), (1280, 720))
            assert out_n["VF_边自测_01"]["next"] == ["VF_边自测_03"], \
                out_n["VF_边自测_01"]
            assert "next" not in out_n["VF_边自测_03"], "没有下一个 = 到此结束"
            # 5) 断开：还有人（边/出口）指着它 → 拒绝；没人指着 → 正常移出主链
            ed4.sel = "p3"
            ed4._sync_conn_ui()
            _pick(ed4.prev_combo, 1)
            ed4.on_conn_combo("prev")          # 让 #1 指着 #3
            chain_before = list(ed4.flow["chain"])
            ed4.next_combo.set("（不接入流程：断开）")
            ed4.on_conn_combo("next")
            assert ed4.flow["chain"] == chain_before, "被边指着的节点不能移出主链"
            # 把指着 #3 的边都清掉（p1 和 p2），再断开就没人拦了
            ed4.flow["nodes"]["p1"]["next"] = None
            ed4.flow["nodes"]["p2"]["next"] = None
            ed4.sel = "p3"
            ed4._sync_conn_ui()
            ed4.next_combo.set("（不接入流程：断开）")
            ed4.on_conn_combo("next")
            assert "p3" not in ed4.flow["chain"], "没人指着就该能移出主链"

            ed5 = FlowEditor(root)
            nodes = {}
            for k, (cx, cy) in enumerate([(0, 0), (0, 120), (0, 240),
                                          (360, 60), (360, 180), (360, 300)]):
                nodes["q%d" % k] = dict(_tap(k + 1), x=float(cx), y=float(cy))
            ed5.flow = {"schemaVersion": SCHEMA_VERSION, "name": "走线自测",
                        "chain": list(nodes), "nodes": nodes}
            ed5._rects_cache = None
            pairs = 0
            for a_id, a in nodes.items():
                for b_id, b in nodes.items():
                    if a_id == b_id:
                        continue
                    ssx, ssy = a["x"] + CARD_W / 2, a["y"] + card_h(a)
                    side, (ax, ay) = ed5._anchor(b_id, ssx, ssy)
                    pts = ed5._route(ssx, ssy, side, ax, ay,
                                     self_ids=(a_id, b_id))
                    for (px, py), (qx, qy) in zip(pts, pts[1:]):
                        assert not ed5._seg_blocked(px, py, qx, qy,
                                                    body_only=(a_id, b_id)), \
                            f"{a_id}→{b_id} 的走线压到卡片: {pts}"
                    pairs += 1
            assert pairs == 30, pairs
            print(f"走线避让自测通过（{pairs} 个方向都不压卡片）")

            # ★ 新建流程不能继承「上次打开的文件」：否则新建后第一次保存时，
            #   on_save 会把它当成「改名」，顺手删掉上次打开的那个流程文件
            #   （2026-09-15 事故：打开 博物研学 → 新建 → 改名 刷活动关 保存，博物研学.flow.json 被删）
            import tempfile
            victim = os.path.join(tempfile.gettempdir(), "maa_flow_victim.flow.json")
            with open(victim, "w", encoding="utf-8") as f:
                f.write("{}")
            ed6 = FlowEditor(root)
            ed6._loaded_path = victim
            ed6.on_new()
            assert ed6._loaded_path is None, ed6._loaded_path
            assert os.path.isfile(victim), "新建流程后，上次打开的流程文件不该被牵连"
            os.remove(victim)
            print("新建流程不牵连旧文件自测通过")
            # ★ 布局/走线规则（2026-09-15）：回环边要画出来、回环节点贴到目标旁边、
            #   【输入】按目标对齐、⛔ 截止标记不许伸进下一张卡片
            ed7 = FlowEditor(root)
            base = {"pre_delay": 0, "post_delay": 500, "repeat": 1}
            n7 = {
                "a": {"type": "tap", "x": 46, "y": 46, "title": "点", "num": 1,
                      "props": dict(base, x=1, y=1), "next": "b"},
                "b": {"type": "branch", "x": 46, "y": 138, "title": "分支", "num": 2,
                      "hit_next": "c", "miss_next": "s",
                      "props": {"template": "t.png", "threshold": 0.8, "ocr_text": "",
                                "roi": "", "timeout": 3000, "rate_limit": 0}},
                "c": {"type": "tap", "x": 46, "y": 230, "title": "点", "num": 3,
                      "props": dict(base, x=1, y=1), "next": None},
                "s": {"type": "swipe", "x": 46, "y": 322, "title": "滑动", "num": 4,
                      "props": dict(base, x1=100, y1=100, x2=200, y2=100,
                                    duration=500), "next": "b"},
                "i": {"type": "input", "x": 500, "y": 46, "title": "输入", "num": 5,
                      "props": {"option": "P", "var": "", "var_label": "", "kind": "文本",
                                "default": "", "desc": "", "target": "",
                                "field": "模板图 (template)", "value": "",
                                "verify": "", "pattern_msg": "", "targets": ["c"]}},
            }
            ed7.flow = {"schemaVersion": SCHEMA_VERSION, "name": "布局自测",
                        "chain": ["a", "b", "c", "s"], "nodes": n7}
            ed7._rects_cache = None
            ed7.tidy_layout()
            # 注意：这里不能再 root.update() —— __init__ 里 after(200, 打开最近流程) 的
            # 回调会被放出来，把 ed7.flow 换成"最近那个流程"，断言就会看着别人的画布说话。
            # （create_line 是立刻生效的，读 coords 不需要 update）
            # ① 纯回环节点（只有一个"下一个"指回上方）挪到目标右侧、y 贴着目标
            assert n7["s"]["x"] > n7["b"]["x"] + CARD_W, n7["s"]
            assert n7["s"]["y"] == n7["b"]["y"], (n7["s"], n7["b"])
            # ③ 【输入】按它的目标对齐
            assert n7["i"]["y"] == n7["c"]["y"], (n7["i"], n7["c"])
            # ② 回环边（滑动 → 分支）真的画出来了：起点是滑动卡片的底边中点
            ssx, ssy = n7["s"]["x"] + CARD_W / 2, n7["s"]["y"] + CARD_H
            starts = [ed7.canvas.coords(i) for i in ed7.canvas.find_all()
                      if ed7.canvas.type(i) == "line"]
            assert any(len(p) >= 4 and abs(p[0] - ssx) < 1 and abs(p[1] - ssy) < 1
                       for p in starts), "指回上方的显式 next 边没画出来"
            # ④ ⛔ 截止标记（竖桩在分支卡片的中线）不能伸进下一张卡片里
            bx = n7["b"]["x"] + CARD_W / 2
            for i in ed7.canvas.find_all():
                if ed7.canvas.type(i) != "line":
                    continue
                co = ed7.canvas.coords(i)
                if len(co) == 4 and abs(co[0] - bx) < 1 and abs(co[0] - co[2]) < 1:
                    assert co[3] <= n7["c"]["y"], (co, n7["c"])
            print("布局与回环走线自测通过")

            # ★ 普通节点下沿的「下一个」小球：拖到目标 = 接/改下一个；拖到空白 = 到此结束。
            #   只给"真的有下一个"的类型画（分支走 ✓/✗、枝干走候选/✗、【输入】靠注入线、
            #   收口节点进入即终止 —— 它们画了球也会让人以为拖了就能接）。
            assert ed7._has_next_ball("a") and ed7._has_next_ball("s")
            assert not ed7._has_next_ball("b"), "【分支】不该有「下一个」球"
            assert not ed7._has_next_ball("i"), "【输入】不该有「下一个」球"
            assert ed7.canvas.find_withtag("port:a:next"), "普通节点应当画「下一个」球"
            assert not ed7.canvas.find_withtag("port:b:next"), "【分支】不该画「下一个」球"
            n7["a"]["next"] = None
            ed7.wire = {"from": "a", "port": "next",
                        "mx": n7["b"]["x"] + 14, "my": n7["b"]["y"] + 14}
            ed7.on_up(None)
            assert n7["a"]["next"] == "b", n7["a"]
            ed7.wire = {"from": "a", "port": "next", "mx": 9999, "my": 9999}
            ed7.on_up(None)
            assert n7["a"]["next"] is None, "拖到空白应当断成『到此结束』"
            print("「下一个」拖线小球自测通过")

            # ★【选择(下拉)】的注入球（2026-09-16）：右侧也和【输入】一样有紫色小球，
            #   拖到目标节点 = 把这个节点挂成某个选项的「目标节点」（那正是
            #   interface.json 里 pipeline_override 的键）；拖到已经连着的 = 断开。
            #   线是虚线的 INJECT_COLOR，与【输入】的注入线同一种画法。
            n7["k"] = {"type": "pick", "x": 500.0, "y": 400.0, "title": "选择(下拉)",
                       "num": 9, "props": {"option": "次数", "var_label": "",
                                           "default": "", "desc": "", "cases": []}}
            knd = n7["k"]
            assert not ed7._has_next_ball("k"), "【选择】不该有「下一个」球"
            assert "right" in ed7._ball_sides("k"), "带球的右边不该再当接入边"
            kx, ky = ed7._port_pos(knd, "inject")
            assert (kx, ky) == (500.0 + CARD_W, 400.0 + CARD_H * 0.5), (kx, ky)
            ed7.redraw()
            assert ed7.canvas.find_withtag("port:k:inject"), \
                "【选择】卡片右侧应当画出「注入」小球"
            # 拖到目标节点 → 记进 props.targets（与【输入】同一套：线只表示"参数作用在
            # 哪个节点上"），**不往选项列表里塞行** —— 选项是"选中时改成什么值"，靠面板填
            tgt_hit = (int(n7["c"]["x"]) + 10, int(n7["c"]["y"]) + 10)
            ed7.wire = {"from": "k", "port": "inject",
                        "mx": tgt_hit[0], "my": tgt_hit[1]}
            ed7.on_up(None)
            assert knd["props"]["targets"] == ["c"], knd["props"]
            assert knd["props"]["cases"] == [], "拖注入线不该往选项列表里塞行"
            dashed = [ed7.canvas.coords(i) for i in ed7.canvas.find_all()
                      if ed7.canvas.type(i) == "line"
                      and ed7.canvas.itemcget(i, "fill") == INJECT_COLOR]
            assert any(len(p) >= 4 and abs(p[0] - kx) < 1 and abs(p[1] - ky) < 1
                       for p in dashed), "【选择】的注入线没画出来（起点应是球的位置）"
            # 再拖一次同一个节点 = 断开
            ed7.wire = {"from": "k", "port": "inject",
                        "mx": tgt_hit[0], "my": tgt_hit[1]}
            ed7.on_up(None)
            assert knd["props"]["targets"] == [], knd["props"]
            # 选项列表与注入线互不干扰：连上再断开，选项一个字都不该动
            knd["props"]["cases"] = [{"name": "1", "field": "repeat", "value": "2"}]
            for _ in range(2):
                ed7.wire = {"from": "k", "port": "inject",
                            "mx": tgt_hit[0], "my": tgt_hit[1]}
                ed7.on_up(None)
            assert knd["props"]["targets"] == [], knd["props"]
            assert len(knd["props"]["cases"]) == 1, knd["props"]["cases"]
            # 生成规则：选项作用在注入线连到的节点上；行内写死目标的老写法优先
            ed7.wire = {"from": "k", "port": "inject",
                        "mx": tgt_hit[0], "my": tgt_hit[1]}
            ed7.on_up(None)
            got = pick_cases(ed7.flow, knd)
            assert got[0]["pipeline_override"] == {jname(ed7.flow, "c"): {"repeat": 2}}, got
            knd["props"]["cases"] = [{"name": "1", "node": "a", "field": "next",
                                      "value": "#3"}]
            got = pick_cases(ed7.flow, knd)
            assert list(got[0]["pipeline_override"]) == [jname(ed7.flow, "a")], got
            assert got[0]["pipeline_override"][jname(ed7.flow, "a")]["next"] == \
                [jname(ed7.flow, "c")], got
            # 校验：一条注入线都没有、值是空的，都要说清楚
            knd["props"]["cases"] = [{"name": "1", "field": "repeat", "value": ""}]
            knd["props"]["targets"] = []
            codes = {i.code for i in collect_issues(ed7.flow) if i.level == "warn"}
            assert "PK_NO_TARGET" in codes and "PK_CASE_VALUE" in codes, codes
            # ★ 选项那两格的下拉候选：「目标节点」= 画布节点（参数节点自己不列），
            #   「值」随【字段】变 —— 模板图给模板文件名、下一个出口给画布节点、
            #   数字/文字不限制（下拉只是省手打，输入框一直能手填）
            targets = pick_target_choices(ed7.flow)
            assert targets and all(s.startswith("#") for s in targets), targets
            assert "#9 选择(下拉)" not in targets, "参数节点不该出现在目标候选里"
            assert pick_value_choices(ed7.flow, "next") == targets
            assert pick_value_choices(ed7.flow, "repeat") == []
            assert all(s.endswith(".png") for s in pick_value_choices(ed7.flow, "template")
                       ) or pick_value_choices(ed7.flow, "template") == []
            assert "模板" in pick_value_hint("模板图 (template)")
            assert "节点" in pick_value_hint("next")
            # 下拉给的「#3 点3」只留 #编号；值里写 #3，生成时换成节点名
            assert norm_ref_text("#3 点3") == "#3", norm_ref_text("#3 点3")
            assert norm_ref_text("征集段2") == "征集段2"
            # ★ 面板：三格是"草稿"，点「＋ 加选项」才成为列表里的一项；界面上没有
            #   「目标节点」（那个交给卡片右侧的注入小球）；改完一格不能把列表的选中行
            #   弄丢（`lb.delete(0,"end")` 会清 selection，而写回全靠"选中哪一行"）。
            ed7.sel = "k"
            wrap2 = ed7._make_cases_list(tk.StringVar())
            kids = []

            def _collect(w):
                for c in w.winfo_children():
                    kids.append(c)
                    _collect(c)

            _collect(wrap2)
            cells = {getattr(c, "_cell_key", None): c for c in kids
                     if getattr(c, "_cell_key", None)}
            lb2 = [c for c in kids if isinstance(c, tk.Listbox)][0]
            assert "node" not in cells, "面板里不该再有「目标节点」格（它由注入球决定）"
            fld2, val2 = cells["field"], cells["value"]
            # 三种常用字段（模板图 / OCR文字 / 命中次数）都在字段下拉里
            for want in ("template", "expected", "repeat"):
                assert pick_field_display(want) in list(fld2.cget("values")), \
                    fld2.cget("values")
            lb2.selection_clear(0, "end")
            lb2.selection_set(0)
            lb2.event_generate("<<ListboxSelect>>")
            assert lb2.curselection() == (0,), lb2.curselection()
            fld2.set("模板图 (template)")
            fld2.event_generate("<FocusOut>")
            assert lb2.curselection() == (0,), "改完一格后选中行不该被清掉"
            assert knd["props"]["cases"][0]["field"] == "template", knd["props"]["cases"]
            hints = [c for c in kids if isinstance(c, ttk.Label)
                     and str(c.cget("text")).startswith("值：")]
            assert hints and "模板" in str(hints[0].cget("text")), \
                [str(h.cget("text")) for h in hints]
            assert list(val2.cget("values")) and all(
                s.endswith(".png") for s in val2.cget("values")), val2.cget("values")
            val2.set("yx_jc.png")
            val2.event_generate("<FocusOut>")
            assert knd["props"]["cases"][0]["value"] == "yx_jc.png", knd["props"]["cases"]
            # 点进点出（一个字没改）不回写；行内写死的目标节点更不许被面板碰
            knd["props"]["cases"][0]["node"] = "征集段2"
            lb2.selection_clear(0, "end")
            lb2.selection_set(0)
            lb2.event_generate("<<ListboxSelect>>")
            fld2.event_generate("<FocusOut>")
            val2.event_generate("<FocusOut>")
            assert knd["props"]["cases"][0]["node"] == "征集段2", knd["props"]["cases"]
            # ★ 填好三格 → 点「＋ 加选项」→ 列表里多出一项（用户要的就是这个顺序）
            add_btn = next(b for b in kids if isinstance(b, tk.Button)
                           and "加选项" in str(b.cget("text")))
            knd["props"]["cases"] = []
            cells["name"].delete(0, "end")
            cells["name"].insert(0, "选A")
            fld2.set("模板图 (template)")
            val2.set("yx_zb.png")
            add_btn.invoke()
            assert len(knd["props"]["cases"]) == 1, knd["props"]["cases"]
            row1 = knd["props"]["cases"][0]
            assert (row1["name"], row1["field"], row1["value"]) == \
                ("选A", "template", "yx_zb.png"), row1
            assert row1["node"] == "", "新加的选项不写死目标节点（靠注入线）"
            assert cells["name"].get() == "" and val2.get() == "", "加完要把草稿清空"
            # 只填名字没填值 → 不加（空的覆盖值可能让引擎拒绝整包）
            cells["name"].delete(0, "end")
            cells["name"].insert(0, "选B")
            add_btn.invoke()
            assert len(knd["props"]["cases"]) == 1, knd["props"]["cases"]
            # ★ 老数据「目标写死在选项里」的迁移入口：⇢ 目标改用注入线
            knd["props"]["cases"] = [{"name": "1", "node": "a", "field": "repeat",
                                      "value": "2"}]
            wrap4 = ed7._make_cases_list(tk.StringVar())
            kids4 = []

            def _c4(w):
                for c in w.winfo_children():
                    kids4.append(c)
                    _c4(c)

            _c4(wrap4)
            to_inj = next(b for b in kids4 if isinstance(b, tk.Button)
                          and "改用注入线" in str(b.cget("text")))
            to_inj.invoke()
            assert knd["props"]["cases"][0]["node"] == "", knd["props"]["cases"]
            # ★ 切行不能丢改动：改完第 1 行直接点第 2 行，第 1 行的修改必须已经存回去
            #   （`<<ListboxSelect>>` 到达时 curselection 是新行，写回要靠 editing 记住旧行）
            def _panel():
                """重建面板 + 抓一套控件。每次 build_prop_panel() 都会销毁旧控件，
                所以断言前必须重新拿 —— 复用上一轮的引用会撞上 invalid command name。"""
                ed7.sel = "k"
                ed7.build_prop_panel()
                kids = []

                def _c(w):
                    for c in w.winfo_children():
                        kids.append(c)
                        _c(c)

                _c(ed7.props_inner)
                cells_ = {getattr(c, "_cell_key", None): c for c in kids
                          if getattr(c, "_cell_key", None)}
                lb_ = next((c for c in kids if isinstance(c, tk.Listbox)), None)
                btns_ = {str(b.cget("text")): b for b in kids
                         if isinstance(b, tk.Button)}
                return cells_, lb_, btns_

            def _pick(lb_, i):
                lb_.selection_clear(0, "end")
                lb_.selection_set(i)
                lb_.event_generate("<<ListboxSelect>>")

            knd["props"]["cases"] = [
                {"name": "1", "field": "template", "value": "a.png"},
                {"name": "2", "field": "repeat", "value": "2"}]
            cells, lb, btns = _panel()
            _pick(lb, 0)
            cells["value"].set("b.png")        # 改第 1 行的值
            _pick(lb, 1)                       # 不点别处，直接切到第 2 行
            assert knd["props"]["cases"][0]["value"] == "b.png", knd["props"]["cases"]
            assert knd["props"]["cases"][1]["value"] == "2", knd["props"]["cases"]
            assert cells["value"].get() == "2", "三格该换成第 2 行的内容"
            # ★ 改完直接点画布（面板会重建、控件被销毁）也不能丢：Tk 销毁控件不会补发
            #   <FocusOut>，所以 build_prop_panel() 重建前必须先提交一次
            knd["props"]["cases"] = [{"name": "甲", "field": "repeat", "value": "1"}]
            cells, lb, btns = _panel()
            _pick(lb, 0)
            cells["name"].delete(0, "end")
            cells["name"].insert(0, "乙")
            ed7.sel = None                  # 模拟"点了画布空白"：选中先被清掉…
            ed7.build_prop_panel()          # …然后面板重建（提交钩子要写回原来那个节点）
            assert knd["props"]["cases"][0]["name"] == "乙", knd["props"]["cases"]
            # ★ 显式的「✔ 确认修改」：点了就把三格写回选中项（不依赖自动保存的时机）
            knd["props"]["cases"] = [{"name": "甲", "field": "repeat", "value": "1"}]
            cells, lb, btns = _panel()
            _pick(lb, 0)
            cells["name"].delete(0, "end")
            cells["name"].insert(0, "丙")
            btns["✔ 确认修改"].invoke()
            assert knd["props"]["cases"][0]["name"] == "丙", knd["props"]["cases"]
            # 草稿状态（还没点任何选项行）点它 → 只给提示，不新增也不改数据
            cells, lb, btns = _panel()
            n_cases = len(knd["props"]["cases"])
            btns["✔ 确认修改"].invoke()
            assert len(knd["props"]["cases"]) == n_cases, "草稿状态点「确认修改」不该动数据"
            # ★ 新建节点不能共用 defaults 里那几个 list（浅拷贝的坑）：往一个节点拉注入线
            #   不能让"出厂默认值"变脏 —— 否则删掉【选择】再新建一个，它会自动连回
            #   上一个连过的节点（用户报的"删除了还是自动连接之前连过的节点"）。
            k1 = ed7.add_node("pick", pos=(960.0, 46.0))
            k2 = ed7.add_node("pick", pos=(960.0, 220.0))
            assert n7[k1]["props"]["cases"] is not n7[k2]["props"]["cases"]
            n7[k1]["props"]["targets"].append("c")
            assert n7[k2]["props"]["targets"] == [], "新建的节点不该共用 targets"
            assert NODE_TYPES["pick"]["defaults"]["targets"] == [], \
                f"NODE_TYPES 的出厂默认值被写脏了：{NODE_TYPES['pick']['defaults']}"
            assert NODE_TYPES["pick"]["defaults"]["cases"] == []
            assert NODE_TYPES["switch"]["defaults"]["candidates"] == [{"timeout": 3000}]
            # ★ 删节点要把指向它的引用清干净：注入目标（【输入】/【选择】共用一份数据）
            #   与选项里写死的目标节点都不能留死 id
            n7[k2]["props"]["targets"] = ["c"]
            n7[k2]["props"]["cases"] = [{"name": "1", "node": "c", "field": "repeat",
                                         "value": "2"}]
            ed7._delete_nodes(["c"])
            assert n7[k2]["props"]["targets"] == [], n7[k2]["props"]
            assert n7[k2]["props"]["cases"][0]["node"] == "", n7[k2]["props"]
            # add_node 会把画布滚到新节点上；后面的平移自测按"视图在原点"算坐标，复位一下
            ed7.canvas.xview_moveto(0)
            ed7.canvas.yview_moveto(0)
            # 读文件时的兜底：注入目标里指向已删除节点的死 id 直接摘掉（画布上没有球可拖、
            # 面板里也没有清它的入口，留着只会让摘要显示"注入 N 个节点"）
            dead = {"schemaVersion": SCHEMA_VERSION, "name": "死id自测",
                    "chain": ["x"], "nodes": {
                        "x": {"type": "tap", "x": 0, "y": 0, "title": "点", "num": 1,
                              "props": dict(_tap(1)), "next": None},
                        "p": {"type": "pick", "x": 0, "y": 100, "title": "选择(下拉)",
                              "num": 2, "props": {"option": "P", "cases": [],
                                                  "targets": ["x", "n999dead"]}},
                        "i": {"type": "input", "x": 0, "y": 200, "title": "输入",
                              "num": 3, "props": {"option": "Q", "cases": [],
                                                  "targets": ["n888dead"]}}}}
            dead = normalize_flow(dead)
            assert dead["nodes"]["p"]["props"]["targets"] == ["x"], dead["nodes"]["p"]
            assert dead["nodes"]["i"]["props"]["targets"] == [], dead["nodes"]["i"]
            print("【选择】注入球自测通过")

            # ★ 空白处按住左键 = 平移画布（只动视图，改不到任何流程数据）；
            #   点在节点上仍然是选中/拖动，两者不能互相抢。
            class _Ev:                     # 假鼠标事件：on_down/on_motion 只用 .x/.y
                def __init__(self, x, y):
                    self.x, self.y = x, y
            ed7.sel = "a"
            assert not ed7._pan_left
            ed7.on_down(_Ev(3, 3))         # (3,3) 附近没有卡片
            assert ed7._pan_left, "空白处按左键应当开始平移"
            assert ed7.sel is None, "空白处按下仍应取消选中"
            txt0 = ed7._flow_text()
            ed7.on_motion(_Ev(60, 40))
            assert ed7._flow_text() == txt0, "平移不该改流程数据"
            ed7.on_up(_Ev(60, 40))
            assert not ed7._pan_left, "松手应当结束平移"
            a_nd = ed7.flow["nodes"]["a"]
            ed7.on_down(_Ev(int(a_nd["x"]) + 10, int(a_nd["y"]) + 10))
            assert not ed7._pan_left and ed7.drag, "点节点上应当是选中/拖动，不是平移"
            ed7.drag = None
            ed7.on_up(_Ev(0, 0))
            print("空白处左键拖动平移自测通过")
            # ★ 工具栏「删除免确认」开关：开着时点删除/按 Delete 不再弹询问框，直接删
            #   （关着会弹 askyesno —— 自测里会卡住等点击，所以只测开着这一半）。
            #   偏好写在 ui_prefs.json，自测跑完要还原，别改掉用户自己的设置。
            pref_before = load_prefs()
            try:
                ed7.no_confirm_delete.set(True)
                assert load_prefs()["no_confirm_delete"] is True, "开关状态没记住"
                k9 = ed7.add_node("tap", pos=(1000.0, 420.0))
                ed7.sel = k9
                ed7.delete_selected()
                assert k9 not in ed7.flow["nodes"], "免确认开关开着时应当直接删掉"
            finally:
                ed7.no_confirm_delete.set(bool(pref_before["no_confirm_delete"]))
                save_prefs(**pref_before)
            assert load_prefs() == pref_before, "自测不该改掉用户的偏好"
            print("「删除免确认」开关自测通过")
            # ★ 初始窗口尺寸不许超出屏幕工作区：写死 1640x960 在高缩放屏上比整个屏幕
            #   还大，Windows 会把窗口压满全屏、底边（状态栏）被任务栏挡住
            assert initial_geometry(root, workarea=(4000, 2000)) == "1640x960", \
                "大屏该维持原尺寸"
            assert initial_geometry(root, workarea=(1280, 664)) == "1280x664"
            # workarea=None = 自动探测；探测不出（非 Windows）才走"屏幕高-60"的兜底
            fallback = "%dx%d" % (min(1640, root.winfo_screenwidth()),
                                  min(960, max(root.winfo_screenheight() - 60, 400)))
            assert initial_geometry(root, workarea=None) in (initial_geometry(root),
                                                             fallback)
            real = initial_geometry(root)
            mw, mh = real.split("x")
            assert int(mw) <= root.winfo_screenwidth(), real
            assert int(mh) <= root.winfo_screenheight(), real
            wa = _windows_workarea()
            if wa:
                assert int(mw) <= wa[0] and int(mh) <= wa[1], (real, wa)
            print(f"初始窗口尺寸自测通过（本机 = {real}，"
                  f"工作区 = {_windows_workarea()}）")
            root.destroy()
            print("GUI 构建烟测通过")
        except tk.TclError as e:
            print(f"GUI 烟测跳过（无显示环境）: {e}")


# ================= main =================

def _windows_workarea():
    """主显示器的【工作区】（整个屏幕去掉任务栏后的可用区域）→ (宽, 高)。

    用 Win32 的 SPI_GETWORKAREA 拿精确值；非 Windows 或调用失败返回 None
    （调用方自己兜底）。DPI 未感知的进程拿到的也是虚拟化后的逻辑像素，
    和 Tk geometry 用的是同一套坐标，不用换算。"""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        # SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return (rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        pass
    return None


def initial_geometry(root, workarea=None):
    """主窗口的初始尺寸：想要 1640x960，但不能大过屏幕的工作区。

    ★ 直接写死 "1640x960" 在缩放比例高的屏幕上（如 1920x1080 @150% → 逻辑 1280x720）
      比整个屏幕还大 —— Windows 把窗口压满整屏，底边（状态栏）被任务栏挡住，
      每次打开都得手动拖。这里把尺寸夹进工作区；屏幕够大时维持原尺寸不变。"""
    if workarea is None:
        workarea = _windows_workarea()
    if not workarea or workarea[0] <= 0 or workarea[1] <= 0:
        # 拿不到精确工作区（非 Windows 等）：按"屏幕高 - 60"兜底，60 ≈ 常见任务栏高度
        workarea = (root.winfo_screenwidth(),
                    max(root.winfo_screenheight() - 60, 400))
    w, h = min(1640, workarea[0]), min(960, workarea[1])
    return "%dx%d" % (w, h)


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
