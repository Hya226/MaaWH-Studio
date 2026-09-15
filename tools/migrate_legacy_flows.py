# -*- coding: utf-8 -*-
"""老流程迁移：手写管线 → 编辑器流程（VF_*）  —— 默认【干跑】，--apply 才落盘

为什么要这一步
--------------
`刷冬谷币 / 征集 / 装卸装备 / 升好感度 / 查找器者` 这 5 个任务原来是**手写管线**
（cdzb.json / grind.json / zhengji.json）+ **手写 interface.json 参数**
（`目标角色`、`礼物次数`、`刷冬谷币次数`、`征集次数`）。这 5 个流程现在已经在编辑器里
完整重建（flows/*.flow.json），生成物节点数与手写管线等价（30 / 22 / 55 / 50 / 44 个节点，
本机引擎加载通过）。迁移 = 把运行入口从手写管线切到 `VF_*`，并把参数覆盖的**节点名**
换成生成名 —— 否则手写参数会「能填、不起作用」（老名字在生成物里不存在）。

改哪些文件（--apply 时）
------------------------
  1. <任务包>/whmx/interface.json      5 个任务的 entry + 4 项参数的 pipeline_override
  2. flows/*.flow.json                参数节点改指画布节点（保住「硬验收」，见下）
  3. <任务包>/whmx/pipeline/vf_*.json 新增 5 个生成物（+ flows/build/ 同步一份）
  4. <任务包>/whmx/_retired/          老 zhengji.json 挪走（它的 `征集段2` 与生成物重名）
改前一律写 .bak；默认只打印计划，不动任何文件。

三条容易漏的坑（脚本已经处理，不用手动补）
------------------------------------------
  ★1 `next` 的**值**也是节点名：`刷谷_调次数.next = ["刷谷段2"]`、`征集段2.next = ["ZJ_DuiGou2"]`
     里的值同样要换成生成名，否则引擎因为「引用不存在的节点」拒绝**整包**（铁律 #1）。
     老管线里的纯跳转节点（如 `刷谷段2` = 只有 next → ConfirmBattle）会顺着穿透到有对应
     流程节点的那个名字。
  ★2 老『判定+点击』一体的节点（`CDZB2_Hit` / `升2_Hit` / `查2_Hit`）在编辑器里被拆成
     **判定分支 + 点击节点**，两处存了同一份模板图 → 只改一个的话，换角色后判定不到、
     或者判定到了却点不到。脚本会把这两个节点一起写进参数（一个老名 → 两个新名）。
  ★3 生成物与老管线**重名**（`征集段2`：vf_征集.json 的通道节点 vs zhengji.json 的原节点）
     在同一个 bundle 里谁生效不确定 → 老 zhengji.json 必须挪出 bundle。

验收（脚本自己做，绿了才算迁移成功）
------------------------------------
  A. 入口都切到 VF_*，且 VF_* 在生成物里真的存在
  B. 参数的**每个**节点名都落在某个目标流程的生成物里（含跨流程的『目标角色』）
  C. 硬验收：`flow_input_options(流程) == interface.json 里那一项`（逐字段）—— 不成立
     就说明画布上的参数指向和清单不一致，下次编辑器同步会把参数静默改回去
  D. 5 个任务里不再残留任何老节点名
  E. 本机引擎加载改动后的任务包副本：`loaded == True`

用法
----
    python tools/migrate_legacy_flows.py                # 干跑（打印全部改动 + 跑验收）
    python tools/migrate_legacy_flows.py --apply        # 落盘（编辑器在跑会被拦住）
    python tools/migrate_legacy_flows.py --task 征集 刷冬谷币
    python tools/migrate_legacy_flows.py --keep-legacy  # 不挪老文件（保留重名，不推荐）
    python tools/migrate_legacy_flows.py --no-engine    # 跳过引擎预检
"""
import argparse
import copy
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
EDITOR_ROOT = os.path.dirname(TOOLS_DIR)              # 本仓库根：flows/ 在这里
sys.path.insert(0, EDITOR_ROOT)

import project_paths                                   # noqa: E402
import flow_editor as fe                               # noqa: E402

FLOWS_DIR = os.path.join(EDITOR_ROOT, "flows")
BUILD_DIR = os.path.join(FLOWS_DIR, "build")

# 本次迁移覆盖的任务 = 手写管线那一批（AGENTS.md 第二节第 6 条的名单）。
# 其它任务（启动 / 行会签到 / 装备分解 / 领取奖励 / 外勤 / 关闭游戏）仍然跑手写管线，
# 不要顺手迁：它们的生成物没被引擎加载验证过。
TARGET_TASKS = [
    # 2026-09-15 第一批（手写管线 + 手写参数）
    "刷冬谷币", "征集", "装卸装备", "升好感度", "查找器者",
    # 2026-09-15 第二批：流程的边/参数已用 tools/fix_flow_edges.py 对齐老管线，
    # 这里只把入口切过去（它们没有参数节点，切完就是 VF_*）
    "外勤", "装备分解", "领取奖励", "行会签到", "启动",
]

# 迁移前记录的生成物节点数（引擎加载验证过的那一版），用来确认「改完还是同一份生成结果」
EXPECT_NODES = {"刷冬谷币": 30, "征集": 22, "装卸装备": 55, "升好感度": 50, "查找器者": 44}

# 项目级公共节点（common.json）：跨流程同名同义，参数里原样保留，不做改名
COMMON_PREFIX = "Common_"


# ======================================================================
# 一、JSONC 保注释编辑
#    interface.json 里那些注释是踩坑记录（2026-09-15 的事故都写在里面），
#    不能为了改 5 个 entry 就把它压成 json.dumps。这里只替换目标片段，其余字节不动。
# ======================================================================
def _skip_junk(t, i):
    """跳过空白、// 行注释、/* */ 块注释"""
    n = len(t)
    while i < n:
        c = t[i]
        if c in " \t\r\n":
            i += 1
        elif t.startswith("//", i):
            j = t.find("\n", i)
            i = n if j < 0 else j + 1
        elif t.startswith("/*", i):
            j = t.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def _scan_string(t, i):
    """t[i] == '"'，返回字符串字面量结束后的位置"""
    i += 1
    while i < len(t):
        c = t[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise ValueError("字符串未闭合")


def _scan_value(t, i):
    """返回值的结束位置（字符串 / 对象 / 数组 / 数字 / 字面量）"""
    i = _skip_junk(t, i)
    c = t[i]
    if c == '"':
        return _scan_string(t, i)
    if c in "{[":
        stack = []                       # 括号栈：对象里套数组、数组里套对象都要算对
        while i < len(t):
            c = t[i]
            if c == '"':
                i = _scan_string(t, i)
                continue
            if t.startswith("//", i):
                j = t.find("\n", i)
                i = len(t) if j < 0 else j + 1
                continue
            if t.startswith("/*", i):
                j = t.find("*/", i)
                i = len(t) if j < 0 else j + 2
                continue
            if c in "{[":
                stack.append("}" if c == "{" else "]")
            elif c in "}]":
                if not stack or stack.pop() != c:
                    raise ValueError(f"括号不配对: {t[max(0, i - 30):i + 10]!r}")
                if not stack:
                    return i + 1
            i += 1
        raise ValueError("括号未闭合")
    j = i
    while j < len(t) and t[j] not in ",}] \t\r\n":
        j += 1
    return j


def object_members(t, start):
    """start 指向 '{' → [(key, key_start, val_start, val_end)]"""
    i = _skip_junk(t, start)
    if t[i] != "{":
        raise ValueError(f"这里不是对象: {t[i:i + 20]!r}")
    i += 1
    out = []
    while True:
        i = _skip_junk(t, i)
        if i < len(t) and t[i] == "}":
            return out
        ks = i
        i = _scan_string(t, i)
        key = json.loads(t[ks:i])
        i = _skip_junk(t, i)
        if t[i] != ":":
            raise ValueError(f"缺冒号: {t[i:i + 20]!r}")
        i = _skip_junk(t, i + 1)
        vs = i
        ve = _scan_value(t, i)
        out.append((key, ks, vs, ve))
        i = _skip_junk(t, ve)
        if i < len(t) and t[i] == ",":
            i += 1
            continue
        if i < len(t) and t[i] == "}":
            return out
        raise ValueError(f"对象结构异常: {t[i:i + 20]!r}")


def array_items(t, start):
    """start 指向 '[' → [(val_start, val_end)]"""
    i = _skip_junk(t, start)
    if t[i] != "[":
        raise ValueError(f"这里不是数组: {t[i:i + 20]!r}")
    i += 1
    out = []
    while True:
        i = _skip_junk(t, i)
        if i < len(t) and t[i] == "]":
            return out
        vs = i
        ve = _scan_value(t, i)
        out.append((vs, ve))
        i = _skip_junk(t, ve)
        if i < len(t) and t[i] == ",":
            i += 1
            continue
        if i < len(t) and t[i] == "]":
            return out
        raise ValueError(f"数组结构异常: {t[i:i + 20]!r}")


def member_span(t, obj_start, key):
    """对象里某个键的值区间（找不到返回 None）"""
    for k, _ks, vs, ve in object_members(t, obj_start):
        if k == key:
            return vs, ve
    return None


def top_span(t, key):
    """顶层键的值区间"""
    return member_span(t, _skip_junk(t, 0), key)


def span_has_comment(t, s, e):
    """片段里是否有（字符串外的）注释 —— 有就别替换，免得把注释吞掉"""
    i = s
    while i < e:
        c = t[i]
        if c == '"':
            i = _scan_string(t, i)
            continue
        if t.startswith("//", i) or t.startswith("/*", i):
            return True
        i += 1
    return False


def apply_edits(text, edits):
    """edits = [(start, end, new_text)]，从后往前替换（位置不变）"""
    for s, e, new in sorted(edits, key=lambda x: -x[0]):
        text = text[:s] + new + text[e:]
    return text


def _inline(v):
    """能挤成一行就返回那一行；挤不下（含嵌套过长）返回 None"""
    if isinstance(v, dict):
        if not v:
            return "{}"
        parts = []
        for k, val in v.items():
            s = _inline(val)
            if s is None:
                return None
            parts.append(json.dumps(k, ensure_ascii=False) + ": " + s)
        return "{ " + ", ".join(parts) + " }"
    if isinstance(v, list):
        if not v:
            return "[]"
        parts = []
        for x in v:
            s = _inline(x)
            if s is None:
                return None
            parts.append(s)
        return "[" + ", ".join(parts) + "]"
    return json.dumps(v, ensure_ascii=False)


def render(v, indent, at=None, width=118):
    """渲染成与 interface.json 风格一致的片段（够短就内联，和原文件的写法保持一致）。
    indent = 值的子行缩进；at = 值开始处的列（用于内联长度判断）。"""
    one = _inline(v)
    if one is not None and (at if at is not None else indent) + len(one) <= width:
        return one
    if isinstance(v, dict):
        lines = []
        for k, val in v.items():
            head = " " * (indent + 4) + json.dumps(k, ensure_ascii=False) + ": "
            lines.append(head + render(val, indent + 4, len(head), width))
        return "{\n" + ",\n".join(lines) + "\n" + " " * indent + "}"
    if isinstance(v, list):
        lines = [" " * (indent + 4) + render(x, indent + 4, indent + 4, width) for x in v]
        return "[\n" + ",\n".join(lines) + "\n" + " " * indent + "]"
    return json.dumps(v, ensure_ascii=False)


def line_indent(t, pos):
    """pos 所在行的缩进宽度"""
    s = t.rfind("\n", 0, pos) + 1
    return len(t[s:pos]) - len(t[s:pos].lstrip(" "))


# ======================================================================
# 二、老名 → 生成名 的解析
# ======================================================================
def gen_name(flow, nid, field=None):
    """节点在生成物里的名字。分支上放识别类字段（模板图/OCR/阈值/ROI）时落在它的 *_Hit ——
    与 fe.input_override_pairs 同一口径。"""
    nm = fe.emit_name_of(flow, nid) or fe.jname(flow, nid)
    nd = (flow.get("nodes") or {}).get(nid) or {}
    if field in fe.RECO_FIELDS and nd.get("type") == "branch":
        nm += "_Hit"
    return nm


def identity_of(nid, nd):
    """一个画布节点可能被当成哪个「老名字」：节点 id 去掉前缀 / 标题 / 通道节点的运行时名"""
    out = set()
    stem = re.sub(r"^(?:(?:n_opt_)|(?:[nbc]_))+", "", nid)
    if stem:
        out.add(stem)
    title = str(nd.get("title") or "").strip()
    if title:
        out.add(title)
        if title.endswith("?"):
            out.add(title[:-1])
    emit = str((nd.get("props") or {}).get("emit_name") or "").strip()
    if emit:
        out.add(emit)
    return out


def with_judge_branch(flow, nid, field):
    """老『判定+点击』一体的节点在编辑器里被拆成两处：判定分支（分支条件）与点击节点
    （再次识别后点击）。识别类字段两边各存了一份 → 改就必须一起改。
    返回 [判定分支, 节点自己]（没有这样的分支时只有自己）。"""
    if field not in fe.RECO_FIELDS:
        return [nid]
    nd = flow["nodes"].get(nid) or {}
    p = nd.get("props") or {}
    tpl = str(p.get("template") or "").strip()
    if not tpl:
        return [nid]
    for bid, bnd in flow["nodes"].items():
        if bnd.get("type") != "branch" or bnd.get("hit_next") != nid:
            continue
        if str((bnd.get("props") or {}).get("template") or "").strip() == tpl:
            return [bid, nid]
    return [nid]


class Unresolved(Exception):
    pass


class Ambiguous(Exception):
    pass


class LegacyMap:
    """老名字 → 生成名。**跨全部目标流程**解析：『目标角色』一个参数同时作用在 3 个流程上
    （老手写清单就是这么写的，一个参数覆盖 CDZB2_Hit / 升2_Hit / 查2_Hit）。"""

    def __init__(self, flows, gen_names=()):
        self.flows = flows
        self.gen_names = set(gen_names)     # 已经生成出来的节点名（重复跑时原样保留）
        self.hits = {}
        for task, flow in flows.items():
            for nid, nd in (flow.get("nodes") or {}).items():
                if not isinstance(nd, dict):
                    continue
                for nm in identity_of(nid, nd):
                    lst = self.hits.setdefault(nm, [])
                    if (task, nid) not in lst:
                        lst.append((task, nid))
        # 同一个老名命中多个不同节点 = 有歧义，不能猜
        self.ambiguous = {}
        for nm, lst in self.hits.items():
            if len({nid for _t, nid in lst}) > 1:
                self.ambiguous[nm] = lst

    # ---- 老管线（穿透纯跳转节点用） ----------------------------------
    def passthrough(self, name):
        """老管线里的「纯跳转」节点（只有 next，没有识别/动作）→ 顺着跟到第一个
        「有内容」的节点名。刷谷段2 = {"next":["ConfirmBattle"]} → ConfirmBattle。"""
        seen, cur = set(), name
        while cur not in seen:
            seen.add(cur)
            nd = None
            for data in self.legacy.values():
                if cur in data:
                    nd = data[cur]
                    break
            if not isinstance(nd, dict):
                return None
            if set(nd) - {"next"}:
                return cur
            nxt = nd.get("next") or []
            if len(nxt) != 1:
                return cur
            cur = nxt[0]
        return None

    def heirs_for(self, name, field, where=""):
        """老名 + 字段 → [(task, nid), ...]（识别类字段会带上判定分支）"""
        if str(name).startswith(COMMON_PREFIX):
            return []                      # 项目级公共节点：原样保留
        if name in self.gen_names:
            return []                      # 已经是生成名（重复跑 / 已迁过）→ 原样保留
        if name in self.ambiguous and not name.startswith(COMMON_PREFIX):
            raise Ambiguous(f"{where}老名「{name}」对应多个画布节点: "
                            + ", ".join(f"{t}/{n}" for t, n in self.ambiguous[name]))
        hits = self.hits.get(name) or []
        if not hits:
            eff = self.passthrough(name)
            if eff and eff != name:
                hits = self.hits.get(eff) or []
                if hits:
                    print(f"    · 老名「{name}」是纯跳转节点 → 顺着老管线跟到「{eff}」")
        if not hits:
            raise Unresolved(f"{where}老名「{name}」在 5 个流程里找不到对应节点"
                             f"（也没能在老管线里穿透到）")
        out = []
        for task, nid in hits:
            for x in with_judge_branch(self.flows[task], nid, field):
                if (task, x) not in out:
                    out.append((task, x))
        return out

    def gen_of(self, name, where=""):
        """`next` 值里的节点名 → 生成名（不做判定分支扩展；多个时取第一个）"""
        heirs = self.heirs_for(name, None, where)
        if not heirs:
            return name
        task, nid = heirs[0]
        return gen_name(self.flows[task], nid)

    def pick_target(self, name, task, where=""):
        """【选择】的「目标节点」格 → 本流程的写画布节点 id（编辑器原生，界面显示 #编号），
        别的流程的写生成名（原样透传）。"""
        heirs = self.heirs_for(name, None, where)
        if not heirs:
            return name
        t, nid = heirs[0]
        return nid if t == task else gen_name(self.flows[t], nid)

    # ---- 老管线文件 ---------------------------------------------------
    def load_legacy(self, pipe_dir):
        self.legacy = {}
        for p in sorted(glob.glob(os.path.join(pipe_dir, "*.json"))):
            base = os.path.basename(p)
            if base.startswith("vf_") or base == "default_pipeline.json":
                continue
            try:
                data = fe.jsonc_loads(open(p, encoding="utf-8").read())
            except Exception as exc:                      # noqa: BLE001
                print(f"    ! 老管线 {base} 读不了，跳过：{exc}")
                continue
            self.legacy[base] = {k: v for k, v in data.items() if not k.startswith("$")}


# ======================================================================
# 三、参数定义迁移（interface.json 侧）
# ======================================================================
def migrate_override(omap, override, where=""):
    """pipeline_override = {老节点名: {字段: 值}} → 生成名版本。
    一个老名可能落成多个新节点（判定分支 + 点击节点）。"""
    out = {}
    for node_name, fields in (override or {}).items():
        if not isinstance(fields, dict):
            continue
        for field, val in fields.items():
            # ★ `next` 的「值」也是节点名，要独立翻译 —— 键能解析、键已经是生成名（如
            #   征集段2 这种通道节点）、键是 Common_*，三种情况下值都得一起换。
            if field == "next":
                names = val if isinstance(val, list) else [val]
                val = [omap.gen_of(x, where) for x in names]
            heirs = omap.heirs_for(node_name, field, where)
            if not heirs:
                out.setdefault(node_name, {})[field] = val
                continue
            for task, nid in heirs:
                out.setdefault(gen_name(omap.flows[task], nid, field), {})[field] = val
    return out


def migrate_option(omap, opt, where=""):
    """一项参数定义 → 迁移后的定义（独立算一遍，用来和流程算出来的交叉验证）"""
    o = copy.deepcopy(opt)
    if o.get("type") == "select":
        for case in o.get("cases") or []:
            case["pipeline_override"] = migrate_override(
                omap, case.get("pipeline_override"), f"{where}cases[{case.get('name')}].")
    else:
        o["pipeline_override"] = migrate_override(omap, o.get("pipeline_override"), where)
    return o


# ======================================================================
# 四、流程文件迁移（画布侧）
# ======================================================================
def migrate_input_node(omap, task, flow, nd, changes):
    """【输入】节点：注入目标换成画布节点（本流程的）/ 生成名（别的流程的）。
    编辑器只画本流程的注入线，跨流程的写在「手写管线的节点名」那一栏里。"""
    props = nd.setdefault("props", {})
    field = fe.input_field(props)
    old_pairs = fe.input_override_pairs(flow, nd)
    own, foreign = [], []
    for name, _f in old_pairs:
        heirs = omap.heirs_for(name, field, f"【输入·{props.get('option')}】")
        if not heirs:
            foreign.append(name)
            continue
        for t, nid in heirs:
            if t == task:
                if nid not in own:
                    own.append(nid)
            else:
                nm = gen_name(omap.flows[t], nid, field)
                if nm not in foreign:
                    foreign.append(nm)
    raw_new = ", ".join(foreign)
    if list(props.get("targets") or []) != own:
        changes.append((f"  · targets", json.dumps(props.get("targets") or [], ensure_ascii=False),
                        json.dumps(own, ensure_ascii=False)))
    if str(props.get("raw") or "") != raw_new:
        changes.append((f"  · raw", str(props.get("raw") or ""), raw_new))
    props["targets"] = own
    props["raw"] = raw_new


def migrate_pick_node(omap, task, flow, nd, changes):
    """【选择】节点：选项的「目标节点」与「next 值」都换成生成名"""
    props = nd.setdefault("props", {})
    for row in props.get("cases") or []:
        if not isinstance(row, dict):
            continue
        field = fe.input_field({"field": row.get("field")})
        node_cell = str(row.get("node") or "").strip()
        if node_cell:
            new_cell = omap.pick_target(node_cell, task, f"【选择·{props.get('option')}】")
            if new_cell != node_cell:
                changes.append((f"  · 选项 {row.get('name')} 目标节点", node_cell, new_cell))
                row["node"] = new_cell
        if field == "next" and str(row.get("value") or "").strip():
            parts = [s.strip() for s in re.split(r"[,，]", str(row["value"])) if s.strip()]
            new_parts = [omap.gen_of(p, f"【选择·{props.get('option')}】") for p in parts]
            if new_parts != parts:
                changes.append((f"  · 选项 {row.get('name')} next 值",
                                ", ".join(parts), ", ".join(new_parts)))
                row["value"] = ", ".join(new_parts)


def migrate_flow(omap, task, flow):
    """就地改流程里的参数节点；返回改动清单"""
    changes = []
    for nid, nd in (flow.get("nodes") or {}).items():
        if not isinstance(nd, dict):
            continue
        if nd.get("type") == "input":
            label = f"【输入】{nid}（{(nd.get('props') or {}).get('option')}）"
            head = len(changes)
            migrate_input_node(omap, task, flow, nd, changes)
            if len(changes) > head:
                changes.insert(head, (label, "", ""))
        elif nd.get("type") == "pick":
            label = f"【选择】{nid}（{(nd.get('props') or {}).get('option')}）"
            head = len(changes)
            migrate_pick_node(omap, task, flow, nd, changes)
            if len(changes) > head:
                changes.insert(head, (label, "", ""))
    return changes


# ======================================================================
# 五、工具
# ======================================================================
def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def diff_paths(old, new, path=""):
    """两个结构化值的差异 → [(路径, 旧, 新)]"""
    out = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in list(old) + [k for k in new if k not in old]:
            p = f"{path}.{k}" if path else k
            if k not in old:
                out.append((p, "<无>", canon(new[k])))
            elif k not in new:
                out.append((p, canon(old[k]), "<删>"))
            else:
                out.extend(diff_paths(old[k], new[k], p))
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            p = f"{path}[{i}]"
            if i >= len(old):
                out.append((p, "<无>", canon(new[i])))
            elif i >= len(new):
                out.append((p, canon(old[i]), "<删>"))
            else:
                out.extend(diff_paths(old[i], new[i], p))
    elif canon(old) != canon(new):
        out.append((path, canon(old), canon(new)))
    return out


def editor_processes():
    """编辑器在不在跑（它会把流程文件在内存里留着，落盘会被它下次保存覆盖）。
    只认 python/pythonw 进程 —— 免得把「查这个」的 shell 自己也算进去。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object "
             "{$_.Name -like '*python*' -and $_.CommandLine -like '*flow_editor.py*'} | "
             "ForEach-Object {$_.ProcessId}"],
            capture_output=True, text=True, timeout=25)
    except Exception:                                       # noqa: BLE001
        return None
    pids = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip().isdigit()]
    return pids


def backup(path, stamp):
    """改前备份；已存在同一天的备份就不覆盖（多次干跑/落盘都能留痕）"""
    dst = f"{path}.bak_before_migrate_{stamp}"
    if os.path.exists(dst):
        return None
    shutil.copy2(path, dst)
    return dst


def text_unified_diff(old, new, fromfile="interface.json.old", tofile="interface.json.new"):
    """interface.json 的文字级 diff（换行后原样保留，便于审查注释有没有被动过）"""
    import difflib
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=fromfile, tofile=tofile, n=2))


def load_jsonc(path):
    with open(path, encoding="utf-8") as f:
        return fe.jsonc_loads(f.read())


# ======================================================================
# 六、引擎预检（把改动应用到任务包的一个副本上再加载）
# ======================================================================
def engine_probe(bundle_root, pipeline_files, interface_text, retired, keep=False):
    """返回 (loaded, 说明)。副本可保留（--probe-keep）用于人工比对。"""
    try:
        from maa.resource import Resource
    except ImportError as exc:                              # noqa: BLE001
        return None, f"本机没有 maa 包（{exc}），跳过引擎预检"
    tmp = tempfile.mkdtemp(prefix="maa_probe_")
    try:
        dst = os.path.join(tmp, "whmx")
        shutil.copytree(bundle_root, dst)
        for rel, data in pipeline_files.items():
            with open(os.path.join(dst, rel), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        with open(os.path.join(dst, "interface.json"), "w", encoding="utf-8") as f:
            f.write(interface_text)
        for name in retired:
            src = os.path.join(dst, "pipeline", name)
            if os.path.exists(src):
                os.makedirs(os.path.join(dst, "_retired"), exist_ok=True)
                shutil.move(src, os.path.join(dst, "_retired", name))
        res = Resource()
        job = res.post_bundle(dst)
        job.wait()
        loaded = bool(res.loaded)
        if keep:
            return loaded, f"副本留在 {dst}（--probe-keep）"
        return loaded, "副本已删除"
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================
# 七、主流程
# ======================================================================
def main():
    ap = argparse.ArgumentParser(description="老流程迁移：手写管线 → 编辑器流程（VF_*）")
    ap.add_argument("--apply", action="store_true", help="真的落盘（默认只干跑打印）")
    ap.add_argument("--task", nargs="*", default=TARGET_TASKS, help="只处理这些任务")
    ap.add_argument("--taskpack", default=None, help="任务包根（默认自动探测）")
    ap.add_argument("--keep-legacy", action="store_true", help="不挪走与生成物重名的老文件")
    ap.add_argument("--no-engine", action="store_true", help="跳过本机引擎预检")
    ap.add_argument("--probe-keep", action="store_true", help="保留引擎预检用的临时副本")
    ap.add_argument("--out", default=None,
                    help="把结果写成一份可审查的副本到 DIR（不动原位），并打印 interface.json 文字 diff")
    ap.add_argument("--force", action="store_true", help="编辑器在跑也照样落盘")
    args = ap.parse_args()

    tasks = [t for t in args.task]
    stamp = datetime.date.today().strftime("%Y%m%d")
    bundle = args.taskpack or os.path.join(project_paths.PROJECT_ROOT, "whmx")
    if not os.path.isdir(os.path.join(bundle, "pipeline")):
        raise SystemExit(f"任务包不对：{bundle} 下没有 pipeline/")
    iface_path = os.path.join(bundle, "interface.json")

    print("=" * 78)
    print("老流程迁移（手写管线 → VF_*）")
    print("=" * 78)
    print(f"编辑器工作区 : {EDITOR_ROOT}")
    print(f"任务包       : {bundle}")
    print(f"任务         : {'、'.join(tasks)}")
    print(f"模式         : {'落盘（--apply）' if args.apply else '干跑（只打印，不改文件）'}")
    print()

    # ---------- 0. 编辑器在不在跑 ----------
    pids = editor_processes()
    if pids:
        print(f"!! 编辑器正在运行（PID {', '.join(pids)}）—— 它内存里是旧流程，"
              f"落盘后它一保存就会把改动盖回去。")
        if args.apply and not args.force:
            raise SystemExit("先把编辑器关掉再 --apply（确要覆盖请加 --force）")
        print()

    # ---------- 1. 读流程 + 生成物 ----------
    flows, old_flows, generated = {}, {}, {}
    for t in tasks:
        path = os.path.join(FLOWS_DIR, f"{t}.flow.json")
        if not os.path.isfile(path):
            raise SystemExit(f"缺流程文件：{path}")
        old_flows[t] = load_jsonc(path)
        flows[t] = fe.normalize_flow(copy.deepcopy(old_flows[t]))
        data = fe.build_pipeline(flows[t])
        generated[t] = [k for k in data if not k.startswith("$")]
        n = len(generated[t])
        exp = EXPECT_NODES.get(t)
        flag = "" if exp in (None, n) else f"  ← 与记录的 {exp} 不一致，先查流程改动！"
        print(f"  流程 {t:8s} 生成节点 {n:3d} 个{flag}")
    omap = LegacyMap(flows, gen_names={k for t in tasks for k in generated[t]})
    omap.load_legacy(os.path.join(bundle, "pipeline"))
    # 任务包里已有的生成物（vf_刷活动关 / vf_博物研学 / vf_每日免费礼包 …）也是节点来源，
    # 「整包引用完整性」要把它们算进去（这些任务早就是编辑器流程了）
    existing_vf = {}
    for p in sorted(glob.glob(os.path.join(bundle, "pipeline", "vf_*.json"))):
        try:
            existing_vf[os.path.basename(p)] = fe.jsonc_loads(open(p, encoding="utf-8").read())
        except Exception as exc:                            # noqa: BLE001
            print(f"    ! 现有生成物 {os.path.basename(p)} 读不了：{exc}")
    # 重名：生成物和老管线里同名的节点（同一个 bundle 里谁生效不确定）→ 老文件必须挪走
    collisions = []
    for t in tasks:
        for base, data in omap.legacy.items():
            inter = set(generated[t]) & set(data)
            if inter:
                collisions.append((t, base, sorted(inter)))
    retire = sorted({base for _t, base, _n in collisions})
    print()

    # 已经迁完的任务直接跳过（重复跑安全）：入口已是 VF_<名>，参数里也不再出现任何老名字
    legacy_names = set()
    for data in omap.legacy.values():
        legacy_names |= set(data)
    iface_text = open(iface_path, encoding="utf-8").read()
    iface = fe.jsonc_loads(iface_text)
    task_by_name = {t.get("name"): t for t in iface.get("task") or []}
    options = iface.get("option") or {}
    skipped = []
    for t in list(tasks):
        task = task_by_name.get(t)
        if not task or task.get("entry") != f"VF_{t}":
            continue
        txt = canon([options.get(o) for o in (task.get("option") or [])])
        if any(f'"{n}"' in txt for n in legacy_names):
            continue                      # 参数里还挂着老名字 → 还没迁完，继续处理
        skipped.append(t)
        tasks.remove(t)
    if skipped:
        print(f"  已经迁过、跳过：{'、'.join(skipped)}")
    if not tasks:
        print("\n没有需要迁移的任务（都已切到 VF_*）。")
        return 0

    # ---------- 2. 流程侧：参数节点改指画布节点 ----------
    print("【1】流程文件的参数节点（画布侧）")
    flow_changes = {}
    for t in tasks:
        ch = migrate_flow(omap, t, flows[t])
        flow_changes[t] = ch
        print(f"  {t}.flow.json" + ("" if ch else "：无需改动"))
        for path, a, b in ch:
            if not a and not b:
                print(f"   {path}")
            else:
                print(f"       {path}:  {a}  →  {b}")
    print()

    # ---------- 3. 参数定义：迁移后的目标（跨流程 + 交叉验证） ----------
    print("【2】interface.json 的入口与参数")
    entry_edits, opt_targets = [], {}
    errors = []
    for t in tasks:
        task = task_by_name.get(t)
        if not task:
            errors.append(f"清单里没有任务「{t}」")
            continue
        old_entry, new_entry = task.get("entry"), f"VF_{t}"
        print(f"  任务入口 {t:8s} {old_entry}  →  {new_entry}")
        entry_edits.append((t, old_entry, new_entry))
        # 该任务的参数名（清单声明的）→ 迁移后的定义
        for opt_name in task.get("option") or []:
            opt = options.get(opt_name)
            if not isinstance(opt, dict):
                errors.append(f"{t} 声明的参数「{opt_name}」在清单里不存在")
                continue
            try:
                new_opt = migrate_option(omap, opt, f"{opt_name}.")
            except (Unresolved, Ambiguous) as exc:
                errors.append(str(exc))
                continue
            if opt_name in opt_targets and canon(opt_targets[opt_name]) != canon(new_opt):
                errors.append(f"参数「{opt_name}」在多个任务里算出的目标不一致")
            opt_targets[opt_name] = new_opt
    print()
    for name, new_opt in opt_targets.items():
        print(f"  参数 {name}")
        for path, a, b in diff_paths(options[name], new_opt):
            print(f"       {path}\n           {a}  →  {b}")
    print()

    # 迁移后的清单（结构版）：后面验收与「模拟同步」都对着它比
    iface_view = copy.deepcopy(iface)
    for t, _old, new_entry in entry_edits:
        for tt in iface_view["task"]:
            if tt.get("name") == t:
                tt["entry"] = new_entry
    for key, val in opt_targets.items():
        iface_view["option"][key] = val

    # ---------- 4. 硬验收 A/B/C/D ----------
    print("【3】验收")
    ok = True
    for e in errors:
        ok = False
        print(f"  ✗ {e}")
    if errors:
        print("\n目标名解析没过，后面的验收不可信 —— 先解决上面的问题。")
        return 1

    # A. 参数里的每个名字都落在某个目标流程的生成物里
    all_names = set()
    for t in tasks:
        all_names |= set(generated[t])
    for opt_name, opt in opt_targets.items():
        refs = []
        if opt.get("type") == "select":
            for c in opt.get("cases") or []:
                for node_name, fields in (c.get("pipeline_override") or {}).items():
                    refs.append(node_name)
                    for f, v in (fields or {}).items():
                        if f == "next":
                            refs += v if isinstance(v, list) else [v]
        else:
            for node_name, fields in (opt.get("pipeline_override") or {}).items():
                refs.append(node_name)
                for f, v in (fields or {}).items():
                    if f == "next":
                        refs += v if isinstance(v, list) else [v]
        missing = [r for r in refs if not r.startswith(COMMON_PREFIX) and r not in all_names]
        if missing:
            ok = False
            print(f"  ✗ 参数「{opt_name}」引用了生成物里不存在的节点：{missing}")
    if ok:
        print("  ✓ 参数的每个节点名都落在生成物里（含跨流程的『目标角色』）")

    # B. 入口在生成物里存在
    for t, _old, new in entry_edits:
        if new not in generated[t]:
            ok = False
            print(f"  ✗ 入口 {new} 不在 {t} 的生成物里")
    if ok:
        print("  ✓ 5 个入口都是生成物里的真实节点")

    # C. 硬验收：flow_input_options == 清单里那一项
    for t in tasks:
        task = task_by_name[t]
        got = fe.flow_input_options(flows[t])
        for opt_name in task.get("option") or []:
            if opt_name not in got:
                ok = False
                print(f"  ✗ 硬验收：{t} 的流程算不出参数「{opt_name}」（画布上缺【输入】/【选择】节点？）")
                continue
            if canon(got[opt_name]) != canon(opt_targets[opt_name]):
                ok = False
                print(f"  ✗ 硬验收：{t} 的「{opt_name}」流程算出来 ≠ 清单里那一项")
                for path, a, b in diff_paths(opt_targets[opt_name], got[opt_name]):
                    print(f"        {path}: {a} → {b}")
    if ok:
        print("  ✓ 硬验收：5 个流程算出的参数 == 迁移后的清单定义（逐字段）")

    # C2. 同名参数跨流程一致（编辑器同步会整份覆盖，不一致就会互相打架）
    for opt_name in opt_targets:
        producers = {t: fe.flow_input_options(flows[t]).get(opt_name) for t in tasks}
        producers = {t: v for t, v in producers.items() if v}
        if len(producers) > 1 and len({canon(v) for v in producers.values()}) > 1:
            ok = False
            print(f"  ✗ 「{opt_name}」由 {list(producers)} 共同声明，但算出来不一样 —— "
                  f"编辑器同步会互相覆盖")
    if ok:
        print("  ✓ 同名参数在各流程里算出同一份定义（同步幂等）")

    # D. 不再残留老节点名（判据：这个老名字既在老管线里、又不在任何生成物里 ——
    #    像 征集段2 这种「通道节点刻意保留的运行时名」两边都有，不算残留）
    legacy_names = set()
    for data in omap.legacy.values():
        legacy_names |= set(data)
    gen_all = set()
    for t in tasks:
        gen_all |= set(generated[t])
    for t in tasks:
        task = task_by_name[t]
        parts = [f"VF_{t}"]
        for o in (task.get("option") or []):
            if o in opt_targets:
                parts.append(canon(opt_targets[o]))
        txt = "\n".join(parts)
        refs = sorted(n for n in legacy_names if f'"{n}"' in txt and n not in gen_all)
        if refs:
            ok = False
            print(f"  ✗ {t} 里还残留老节点名（生成物里没有它们）：{refs}")
    if ok:
        print("  ✓ 5 个任务的入口与参数里没有残留老节点名")

    # E. 模拟编辑器同步：upsert_flow_task 跑一遍后清单必须一字不变
    #    （它是「同步到手机」写参数走的同一条路；不一致 = 下次同步就把参数静默改回去）
    sim = copy.deepcopy(iface_view)
    sim_logs = []
    for t in tasks:
        fe.upsert_flow_task(sim, t, lambda m, lvl="info": sim_logs.append(m),
                            options=fe.flow_input_options(flows[t]))
    if canon(sim) != canon(iface_view):
        ok = False
        print("  ✗ 模拟同步后清单变了（编辑器同步会把参数改回去）：")
        for path, a, b in diff_paths(iface_view, sim)[:12]:
            print(f"      {path}: {a} → {b}")
    else:
        print("  ✓ 模拟编辑器同步：清单一字不变（同步幂等）")

    # F. 迁移后整包引用完整性：清单里**每个**任务（含没迁移的老任务）的 entry 与参数里
    #    引用的节点名，都要能在「迁移后剩下的 pipeline 文件」里找到 —— 挪走 zhengji.json
    #    会不会连累别人，看的就是这一条。
    merged = set()
    for base, data in omap.legacy.items():
        if base in retire and not args.keep_legacy:
            continue
        merged |= set(data)
    for base, data in existing_vf.items():
        if base not in {f"vf_{t}.json" for t in tasks}:      # 我们要重写的那 5 个用新生成物
            merged |= {k for k in data if not k.startswith("$")}
    for t in tasks:
        merged |= set(generated[t])
    dangling = []
    for task in iface_view["task"]:
        names = [task.get("entry")]
        for opt_name in task.get("option") or []:
            opt = iface_view["option"].get(opt_name) or {}
            if opt.get("type") == "select":
                blocks = [c.get("pipeline_override") for c in opt.get("cases") or []]
            else:
                blocks = [opt.get("pipeline_override")]
            for blk in blocks:
                for node_name, fields in (blk or {}).items():
                    names.append(node_name)
                    for f, v in (fields or {}).items():
                        if f == "next":
                            names += v if isinstance(v, list) else [v]
        for n in names:
            if n and n not in merged:
                dangling.append((task.get("name"), n))
    #    一次列出（同一个名字只报一次）
    uniq = sorted({(tt, nn) for tt, nn in dangling})
    if uniq:
        ok = False
        print("  ✗ 清单里引用了任务包里不存在的节点：")
        for tt, nn in uniq[:20]:
            print(f"      {tt} → {nn}")
    else:
        print(f"  ✓ 整包引用完整性：{len(iface_view['task'])} 个任务的入口+参数都能落到节点上")

    # G. 重名检查：生成物 vs 任务包里其它 pipeline 文件
    for t, base, names in collisions:
        print(f"  ! vf_{t}.json 与 {base} 重名：{names}")
    if retire:
        print(f"  → 这些老文件必须挪出 bundle（否则同名节点谁生效不确定）")
    else:
        print("  ✓ 生成物与任务包里其它 pipeline 文件没有重名")

    print()
    if not ok:
        print("验收没过 —— 不动任何文件。")
        return 1

    # ---------- 5. 落盘 / 出可审查副本 ----------
    pipeline_out = {f"pipeline/vf_{t}.json": fe.build_pipeline(flows[t]) for t in tasks}
    out_dir = args.out
    if out_dir:
        os.makedirs(os.path.join(out_dir, "flows"), exist_ok=True)
        os.makedirs(os.path.join(out_dir, "pipeline"), exist_ok=True)

    if args.apply:
        print("【4】落盘")
    elif out_dir:
        print(f"【4】写可审查副本 → {out_dir}（原位不动）")
    else:
        print("【4】将写入的文件（干跑，未落盘）")
    print(f"  {iface_path}（{len(entry_edits)} 个入口 + {len(opt_targets)} 项参数；注释保留）")
    for t in tasks:
        src_flow = os.path.join(FLOWS_DIR, f"{t}.flow.json")
        src_build = os.path.join(BUILD_DIR, f"vf_{t}.json")
        src_pipe = os.path.join(bundle, "pipeline", f"vf_{t}.json")
        if args.apply:
            b = backup(src_flow, stamp)
            if b:
                print(f"  备份 {b}")
            fe.save_flow(flows[t])
            fe.write_pipeline_json(flows[t], (fe.FRAME_W, fe.FRAME_H))
            shutil.copy2(src_build, src_pipe)
            print(f"  写  {src_flow}")
            print(f"  写  {src_build}")
            print(f"  写  {src_pipe}")
        elif out_dir:
            flows[t]["schemaVersion"] = fe.SCHEMA_VERSION
            dst_flow = os.path.join(out_dir, "flows", f"{t}.flow.json")
            dst_pipe = os.path.join(out_dir, "pipeline", f"vf_{t}.json")
            with open(dst_flow, "w", encoding="utf-8") as f:
                json.dump(flows[t], f, ensure_ascii=False, indent=2)
            with open(dst_pipe, "w", encoding="utf-8") as f:
                json.dump(pipeline_out[f"pipeline/vf_{t}.json"], f, ensure_ascii=False, indent=4)
            print(f"  写  {dst_flow}")
            print(f"  写  {dst_pipe}")
        else:
            print(f"  {src_flow}")
            print(f"  {src_build}")
            print(f"  {src_pipe}")
    for base in retire:
        if args.keep_legacy:
            print(f"  （--keep-legacy：{base} 留在原地，重名未解决）")
        elif args.apply:
            pass                                  # 落盘时挪，见下面
        else:
            print(f"  {os.path.join(bundle, '_retired', base)}   ← 老文件挪走")

    # interface.json：只替换目标片段（注释、排版其余部分一字不动）
    new_iface_text = iface_text
    if args.apply:
        b = backup(iface_path, stamp)
        if b:
            print(f"  备份 {b}")
    edits = []
    #   入口
    tspan = top_span(new_iface_text, "task")
    for vs, ve in array_items(new_iface_text, tspan[0]):
        nm = member_span(new_iface_text, vs, "name")
        if not nm:
            continue
        tname = json.loads(new_iface_text[nm[0]:nm[1]])
        for t, old_entry, new_entry in entry_edits:
            if tname != t:
                continue
            es = member_span(new_iface_text, vs, "entry")
            if not es:
                raise SystemExit(f"任务「{t}」没有 entry 字段，脚本不猜，先看一眼清单")
            if span_has_comment(new_iface_text, es[0], es[1]):
                raise SystemExit(f"任务「{t}」的 entry 上挂着注释，脚本不替换（怕吞注释）")
            edits.append((es[0], es[1], json.dumps(new_entry, ensure_ascii=False)))
    #   参数
    ospan = top_span(new_iface_text, "option")
    for key, _ks, vs, ve in object_members(new_iface_text, ospan[0]):
        if key not in opt_targets:
            continue
        if span_has_comment(new_iface_text, vs, ve):
            raise SystemExit(f"参数「{key}」的定义里挂着注释，脚本不替换（怕吞注释）")
        indent = line_indent(new_iface_text, _ks)
        edits.append((vs, ve, render(opt_targets[key], indent, indent)))
    new_iface_text = apply_edits(new_iface_text, edits)
    #   自检：替换后的文本必须能解析，且等于我们想写的结构
    check = fe.jsonc_loads(new_iface_text)
    for t, _old, new_entry in entry_edits:
        got = [x.get("entry") for x in check["task"] if x.get("name") == t][0]
        if got != new_entry:
            raise SystemExit(f"替换后校验失败：{t}.entry = {got!r}")
    for key, val in opt_targets.items():
        if canon(check["option"][key]) != canon(val):
            raise SystemExit(f"替换后校验失败：option[{key}] 与期望不一致")
    if args.apply:
        with open(iface_path, "w", encoding="utf-8") as f:
            f.write(new_iface_text)
        print(f"  写  {iface_path}（注释保留）")
    elif out_dir:
        dst_iface = os.path.join(out_dir, "interface.json")
        with open(dst_iface, "w", encoding="utf-8") as f:
            f.write(new_iface_text)
        print(f"  写  {dst_iface}（注释保留）")
        # 文字级 diff：审查「除了该改的地方，有没有多动一个字」
        d = text_unified_diff(iface_text, new_iface_text)
        print()
        print("  ---- interface.json 文字 diff ----")
        for line in d.splitlines()[:200]:
            print("  " + line)
        rest = len(d.splitlines()) - 200
        if rest > 0:
            print(f"  …（还有 {rest} 行，完整 diff 见 {dst_iface} 与原文件）")
        print()

    # 老文件挪走
    if not args.keep_legacy:
        for base in retire:
            src = os.path.join(bundle, "pipeline", base)
            dst = os.path.join(bundle, "_retired", base)
            if args.apply:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    dst += f".{stamp}"
                shutil.move(src, dst)
                print(f"  挪  {src} → {dst}")
            else:
                print(f"  挪  {src} → {dst}")
    print()

    # ---------- 6. 引擎预检 ----------
    if not args.no_engine:
        last = pipeline_out
        if args.apply:
            last = {rel: load_jsonc(os.path.join(bundle, rel)) for rel in pipeline_out}
            iface_for_probe = open(iface_path, encoding="utf-8").read()
        else:
            iface_for_probe = new_iface_text
        loaded, note = engine_probe(bundle, last, iface_for_probe,
                                    [] if args.keep_legacy else retire, args.probe_keep)
        print(f"【5】本机引擎加载{'（干跑：改动只落在临时副本上）' if not args.apply else ''}")
        if loaded is None:
            print(f"  ~ {note}")
        else:
            print(f"  {'✓' if loaded else '✗'} loaded = {loaded}   {note}")
            if not loaded:
                print("  → 引擎拒绝了整包（铁律 #1）：任务全都跑不起来，别推手机。")
                return 1
        print()

    print("=" * 78)
    if args.apply:
        print("迁移已落盘。下一步：")
        print("  1) 手机侧清掉老文件（编辑器同步不会删文件）：")
        print("     adb -s 2c92e197 shell \"run-as com.maawh.app rm -f "
              "files/taskpacks/whmx/pipeline/zhengji.json\"")
        print("  2) 编辑器里逐个【同步到手机】（入口 + 参数 + 生成物一起推上去）")
        print("  3) 手机实跑一遍（先跑短的：查找器者 / 征集），对着 maafw.log 确认参数生效")
    else:
        print("干跑结束，没有改任何文件。确认计划无误后加 --apply 落盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
