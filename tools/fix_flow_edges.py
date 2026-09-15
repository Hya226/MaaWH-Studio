# -*- coding: utf-8 -*-
"""把这 5 个重建流程的边补回「与手写管线等价」  —— 默认【审计+干跑】，--apply 才落盘

为什么要修
----------
`刷冬谷币 / 征集 / 装卸装备 / 升好感度 / 查找器者` 这 5 个流程是上一轮从老管线重建的，
重建时**把链上的线性边整片丢了**：流程里除了【分支】的 ✓/✗、【枝干判定】的候选、
【通道】的 next 之外，普通节点一个 `next` 键都没有（v4 语义下「缺 next = 到此结束」）。
后果不是报错，而是**任务跑完第一个节点就"成功结束"** —— 2026-09-15 手机实跑征集时
就是这么暴露的：日志里 `VF_征集_01` 命中点击后直接 `task end [ret=true]`。
引擎**加载**是过的（缺 next 不是合法性问题），所以只有实跑/结构比对才看得出来。

审计口径：老管线（名字）→ 画布节点（名字），逐节点比较**有序后继集合**：
    next 列表 ↔ 普通节点的 next / 分支的 hit_next / 枝干的候选
    on_error  ↔ 分支的 miss_next / 枝干的 miss_next
三种「看着不一致、其实等价」的情况会被识别并跳过：
    · 纯跳转（DoNothing + 无识别 + 单 next）→ 重建时已折叠进后继
    · 门槛 + 识别子节点（判定 DoNothing + next=[校验(有识别)] + on_error）→ 合并成一个【分支】
    · 点击节点被包一层【分支】当枝干候选（候选必须是分支）

修复的四类
----------
  1. 缺 next / 缺 miss_next：按老管线写回（名字映射到生成名，多个目标则补一个【枝干判定】）
  2. 丢了候选（如 刷冬谷币 的 StageHome 少一条 ToEnterPage、StartTrain 少一条 DongGuBiTpl）
  3. 整条支线的节点没了（如 DongGuBiTpl 本身）→ 按老定义建节点
  4. 点击节点带 on_error 兜底（征集 ZJ_TiaoGuoCK）→ 包一层【分支】：命中点它 / 未中走兜底

用法
----
    python tools/fix_flow_edges.py                  # 审计 + 打印将要做的修改（不落盘）
    python tools/fix_flow_edges.py --apply          # 落盘（改前 .bak；同时重生成生成物）
    python tools/fix_flow_edges.py --task 征集
"""
import argparse
import copy
import datetime
import glob
import json
import os
import re
import shutil
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
EDITOR_ROOT = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, EDITOR_ROOT)
sys.path.insert(0, TOOLS_DIR)

import project_paths                                   # noqa: E402
import flow_editor as fe                               # noqa: E402
from flow_editor import jsonc_loads                    # noqa: E402


def is_legacy_name(d, nm):
    """这个名字是不是「老管线里真有的节点名」。★ 不能用「画布上有这个名字」代替 ——
    修复过程中补出来的临时节点（如 n_X_候选）也有名字，那样会被当成有效出口。
    Common_* 是项目级公共节点（定义在 common.json，不在本任务的管线文件里）。"""
    return nm in d or nm.startswith("Common_")

FLOWS_DIR = os.path.join(EDITOR_ROOT, "flows")
BUILD_DIR = os.path.join(FLOWS_DIR, "build")

# 任务 → (老管线文件, 入口节点)。老管线仍是这几个流程的**行为基准**（迁移前手机跑的就是它）
TASKS = {
    "刷冬谷币": ("grind.json", "刷冬谷币"),
    "征集": ("zhengji.json", "征集"),
    "装卸装备": ("cdzb.json", "装卸装备"),
    "升好感度": ("cdzb.json", "升好感度"),
    "查找器者": ("cdzb.json", "查找器者"),
    # 2026-09-15 第二批：同样是重建来的、同样缺边（任务还指着老管线，所以手机上暂时不炸，
    # 但一迁移就会重演"跑完第一个节点就结束"）
    "外勤": ("cdzb.json", "外勤"),
    "装备分解": ("cdzb.json", "装备分解"),
    "领取奖励": ("cdzb.json", "领取奖励"),
    "行会签到": ("qiandao.json", "签到段1"),
    "启动": ("grind.json", "到主页"),
}
# ★ 不在表里的：派遣公司事务 —— 它的流程是**手工内联的完整版**（收取资源+办公易物+喝茶），
#   比老管线 pqgs.json 多，按老管线对齐会把它拆坏。


def legacy_pipeline(bundle, fname):
    for d in (os.path.join(bundle, "pipeline"), os.path.join(bundle, "_retired")):
        p = os.path.join(d, fname)
        if os.path.isfile(p):
            return {k: v for k, v in jsonc_loads(open(p, encoding="utf-8").read()).items()
                    if not k.startswith("$")}
    raise SystemExit(f"找不到老管线 {fname}")


def names_of(flow, nid):
    """一个画布节点可能对应的老名字：id 去前缀 / 标题（去掉问号）/ 通道节点的运行时名"""
    nd = flow["nodes"].get(nid) or {}
    out = {re.sub(r"^(?:(?:n_opt_)|(?:[nbc]_))+", "", nid)}
    t = str(nd.get("title") or "").strip()
    if t:
        out.add(t)
        out.add(t.rstrip("?"))
    em = str((nd.get("props") or {}).get("emit_name") or "").strip()
    if em:
        out.add(em)
    return {x for x in out if x}


def is_dead_end(nd):
    """流末：什么都不做、没有出口的老节点（如 查2_End）—— 编辑器里就是「到此结束」，
    不需要建节点，指向它的那个节点也不写 next。"""
    return (nd.get("action", "DoNothing") == "DoNothing" and not nd.get("recognition")
            and not nd.get("on_error") and not (nd.get("next") or []))


def is_sure_gate(v, d):
    """门槛（DoNothing + next + on_error）的子节点没有识别 = DirectHit 必中 →
    timeout / on_error 永远到不了，门槛等价于「直接跑子节点」。"""
    if not (v.get("action", "DoNothing") == "DoNothing" and not v.get("recognition")
            and len(v.get("next") or []) == 1):
        return False
    child = (d.get(v["next"][0]) or {})
    return bool(d.get(v["next"][0])) and not child.get("recognition")


def is_collapsed(nd):
    """纯跳转：DoNothing + 无识别 + 无 on_error + 恰一个 next（重建时会被折叠进后继）"""
    return (nd.get("action", "DoNothing") == "DoNothing" and not nd.get("recognition")
            and not nd.get("on_error") and len(nd.get("next") or []) == 1)


class Graph:
    """一个任务的老管线图 + 画布流程，负责「名字 ↔ 节点」的来回映射与审计"""

    def __init__(self, bundle, task, legacy_file, entry, flow=None):
        self.task = task
        self.d = legacy_pipeline(bundle, legacy_file)
        self.entry = entry
        self.flow = flow if flow is not None else fe.normalize_flow(
            json.load(open(os.path.join(FLOWS_DIR, f"{task}.flow.json"), encoding="utf-8")))
        self.nodes = self.flow["nodes"]
        self.credit = {}                       # 老名字 → [画布节点 id]
        for nid in self.nodes:
            for nm in names_of(self.flow, nid):
                self.credit.setdefault(nm, []).append(nid)
        self.reachable = self._reachable()

    def _reachable(self):
        seen, stack = set(), [self.entry]
        while stack:
            x = stack.pop()
            if x in seen or x not in self.d:
                continue
            seen.add(x)
            stack += list(self.d[x].get("next") or []) + list(self.d[x].get("on_error") or [])
        return {x for x in seen if not x.startswith("Common_")}

    # ---- 老节点 → 画布节点 ------------------------------------------------
    def is_gate(self, K):
        v = self.d.get(K) or {}
        return (v.get("action", "DoNothing") == "DoNothing" and not v.get("recognition")
                and len(v.get("next") or []) == 1)

    def merged_child(self, K):
        """门槛 K 的识别子节点（子节点有识别、但画布上没有它自己的节点）= 被并进 K 的分支"""
        v = self.d.get(K) or {}
        if not self.is_gate(K) or not v.get("on_error"):
            return None
        x = (v.get("next") or [None])[0]
        if x and (self.d.get(x) or {}).get("recognition") and not self.credit.get(x):
            return x
        return None

    def merged_into(self, K):
        """K 自己是被并进某个门槛分支的识别子节点吗？→ 返回那个门槛名"""
        for g in self.reachable:
            if self.merged_child(g) == K:
                return g
        return None

    def rep(self, K):
        """老节点 K 由哪个画布节点代表（None = 折叠掉了 / 没对应）。
        带 on_error 的节点（门槛、或点击 + 兜底）由包它的那个【分支】代表 ——
        兜底出口长在分支的 miss_next 上。"""
        v = self.d.get(K) or {}
        n, b = "n_" + K, "b_" + K
        if b in self.nodes and v.get("on_error"):
            return b
        if n in self.nodes:
            return n
        if b in self.nodes:
            return b
        c = self.credit.get(K)
        if c:
            return c[0]
        return None

    def eff(self, K, which):
        """K 的 which（next/on_error）→ 折叠 / 合并之后的【有效后继】老名字（有序）"""
        out = []
        for x in (self.d.get(K, {}).get(which) or []):
            y, seen = x, set()
            while y in self.d and y not in seen:
                if is_dead_end(self.d[y]):
                    y = None                    # 流末 → 没有后继
                    break
                if is_collapsed(self.d[y]) or is_sure_gate(self.d[y], self.d):
                    seen.add(y)
                    nxt = self.d[y].get("next") or []
                    if len(nxt) != 1:
                        break
                    y = nxt[0]
                    continue
                if self.merged_into(y) == K:        # 子节点并进了同一个分支 → 继续跟随
                    seen.add(y)
                    nxt = self.d[y].get("next") or []
                    if len(nxt) != 1:
                        break
                    y = nxt[0]
                    continue
                break
            if y:
                out.append(y)
        return out

    def spec(self, K):
        """K 的期望（有序 next / on_error，老名字）。门槛有识别子节点时，next 取子节点的 next"""
        child = self.merged_child(K)
        if child:
            return (self.eff(child, "next"),
                    self.eff(K, "on_error") + self.eff(child, "on_error"))
        return self.eff(K, "next"), self.eff(K, "on_error")

    # ---- 画布节点 → 老名字 ------------------------------------------------
    def _resolve(self, nid, origin, depth=0, start=None):
        """画布节点 → 它代表的老名字（列表）。下列节点「语义透明」，要穿过去：
           · 【通道/跳转】：它在老管线里的对应节点是纯跳转（如 征集段2 → ZJ_JiaHao2）
           · 包装分支里的本体：命中后就回到 origin 自己（点击节点被包成候选/兜底时）
           · 合成节点：画布上没人认领的临时节点（补出来的【枝干判定】）
        origin = 当前在算谁的出口（老名字集合），用来识别"回到自己"。"""
        if not nid or depth > 8:
            return []
        nd = self.nodes.get(nid) or {}
        names = names_of(self.flow, nid)
        credit_names = sorted(x for x in names if is_legacy_name(self.d, x))
        if start is None:
            start = nid
        # 自环（老管线里就有，如 行会签到 HH_Loop → HH_Loop 轮询）→ 就是它自己，别当透明
        if depth > 0 and nid == start and credit_names:
            return [credit_names[0]]
        if nd.get("type") == "pass":
            return self._resolve(nd.get("next"), origin, depth + 1, start)
        if credit_names and not (set(credit_names) - set(origin)):
            return self._resolve(nd.get("hit_next") if nd.get("type") == "branch"
                                 else nd.get("next"), origin, depth + 1, start)
        if credit_names:
            return [credit_names[0]]
        # 合成节点（没人认领）→ 展开它的出口
        outs = []
        if nd.get("type") == "branch":
            outs = [nd.get("hit_next"), nd.get("miss_next")]
        elif nd.get("type") == "switch":
            outs = [c.get("next") for c in (nd.get("props") or {}).get("candidates") or []]
            outs.append((nd.get("props") or {}).get("miss_next"))
        else:
            outs = [nd.get("next")]
        res = []
        for o in outs:
            res += self._resolve(o, origin, depth + 1, start)
        return res

    def exits(self, nid, origin=None):
        """画布节点当前的 (next 老名字列表, on_error 老名字列表)"""
        nd = self.nodes[nid]
        origin = origin or {x for x in names_of(self.flow, nid)
                            if is_legacy_name(self.d, x)} or {""}
        if nd.get("type") == "branch":
            return (self._resolve(nd.get("hit_next"), origin),
                    self._resolve(nd.get("miss_next"), origin))
        if nd.get("type") == "switch":
            got = []
            for c in (nd.get("props") or {}).get("candidates") or []:
                got += self._resolve(c.get("next"), origin)
            return got, self._resolve((nd.get("props") or {}).get("miss_next"), origin)
        return self._resolve(nd.get("next"), origin), []

    # ---- 审计 --------------------------------------------------------------
    # ---- 节点参数（识别/动作/延时/次数）是否与老管线一致 ----
    # 边等价只保证"走得通"；参数不一致会表现成"跑起来了但做错事"（模板/阈值/重复次数/延时）。
    FIELDS = ("template", "threshold", "roi", "timeout", "rate_limit", "order_by",
              "repeat", "repeat_delay", "pre_delay", "post_delay", "post_wait_freezes",
              "max_hit", "index")

    def _norm(self, key, val):
        if key == "roi":
            if isinstance(val, str):
                parts = [x.strip() for x in val.split(",") if x.strip()]
                return [int(float(x)) for x in parts] if parts else None
            return [int(x) for x in val] if val else None
        if key == "order_by":
            return str(val or "").strip() or None
        return val

    def node_fields(self, nd):
        """画布节点能表达的字段（fields + defaults）"""
        t = nd.get("type")
        spec = fe.NODE_TYPES.get(t) or {}
        return ({x[0] for x in spec.get("fields", [])} | set(spec.get("defaults", {})))

    def props_audit(self):
        """逐节点比参数 → (能补的 [(K,字段,老值,画布值)], 画布表达不了的 [(K,字段,老值,类型)])"""
        out, cannot = [], []
        for K in sorted(self.reachable):
            v = self.d.get(K) or {}
            if K.startswith("Common_") or is_collapsed(v) or is_dead_end(v):
                continue
            nid = self.rep(K)
            if nid is None:
                continue
            p = self.nodes[nid].get("props") or {}
            # 枝干：timeout 长在**候选**上（每个候选一份），节点本身没有
            if self.nodes[nid].get("type") == "switch":
                got = [(c.get("timeout") or 0) for c in (p.get("candidates") or [])]
                want = int(v.get("timeout") or 0)
                if want and got and any(int(x) != want for x in got):
                    out.append((K, "候选.timeout", want, got))
                continue
            # 门槛 / 纯等待节点：识别条件长在子节点或【分支】上，这里只比 timeout
            if not v.get("recognition") and v.get("action", "DoNothing") == "DoNothing":
                if v.get("timeout") and int(v["timeout"]) != int(p.get("timeout") or 0):
                    out.append((K, "timeout", v["timeout"], p.get("timeout")))
                continue
            body = self.nodes.get("n_" + K)
            tgt_node = (body if (body is not None and body is not self.nodes[nid])
                        else self.nodes[nid])
            for f in self.FIELDS:
                lv = self._norm(f, v.get(f))
                if lv in (None, "", [], 0):
                    continue
                src = p
                if body is not None and body is not self.nodes[nid]                         and f not in ("template", "threshold", "roi", "timeout", "rate_limit"):
                    src = body.get("props") or {}   # 动作类字段在本体上
                gv = self._norm(f, src.get(f))
                if str(lv) != str(gv):
                    (out if f in self.node_fields(tgt_node) else cannot).append(
                        (K, f, lv, gv) if f in self.node_fields(tgt_node) else (K, f, lv, tgt_node.get("type")))
            # 固定坐标点击：老管线 target ↔ 画布 tap 的 x/y
            tgt = tgt_node
            if v.get("target") and tgt.get("type") == "tap":
                tp = tgt.get("props") or {}
                lx, ly = (list(v["target"]) + [None, None])[:2]
                if (int(lx), int(ly)) != (int(tp.get("x") or -1), int(tp.get("y") or -1)):
                    out.append((K, "target(x,y)", v["target"], [tp.get("x"), tp.get("y")]))
        return out, cannot

    def props_fix(self, plans):
        """把老管线的参数补回画布（只补画布能表达的字段）。
        重建时这些字段被丢了：post_wait_freezes / rate_limit / repeat_delay / tap 的 timeout…"""
        can, cannot = self.props_audit()
        for K, f, lv, gv in can:
            nid = self.rep(K)
            body = self.nodes.get("n_" + K)
            tgt = self.nodes[nid]
            if body is not None and body is not tgt and                     f not in ("template", "threshold", "roi", "timeout", "rate_limit"):
                tgt = body
            p = tgt.setdefault("props", {})
            if f == "target(x,y)":
                p["x"], p["y"] = int(lv[0]), int(lv[1])
            else:
                p[f] = ",".join(str(x) for x in lv) if isinstance(lv, list) else lv
            plans.append(f"     · {K}.{f}: {gv!r} → {lv!r}（补回老管线的值）")
        for K, f, lv, typ in cannot:
            plans.append(f"     ! {K}.{f} = {lv!r} 补不了：画布的【{typ}】没有这个字段")
        return can

    def audit(self):
        gaps, notes = [], []
        for K in sorted(self.reachable):
            nid = self.rep(K)
            exp_n, exp_e = self.spec(K)
            exp_n, exp_e = list(exp_n), list(exp_e)
            if nid is None:
                if self.merged_into(K):
                    notes.append((K, f"并入门槛分支 {self.merged_into(K)}"))
                elif is_collapsed(self.d.get(K) or {}):
                    notes.append((K, "纯跳转(已折叠)"))
                elif is_dead_end(self.d.get(K) or {}):
                    notes.append((K, "流末(到此结束)"))
                elif is_sure_gate(self.d.get(K) or {}, self.d):
                    notes.append((K, "门槛子节点必中(已折叠)"))
                else:
                    gaps.append((K, "缺节点",
                                 f"老管线有、画布上没有（next={self.d.get(K, {}).get('next')}）", ""))
                continue
            if self.nodes[nid].get("type") == "common":
                notes.append((K, "收口节点"))
                continue
            if self.nodes[nid].get("type") == "switch":
                bad = [c.get("next") for c in
                       (self.nodes[nid].get("props") or {}).get("candidates") or []
                       if (self.nodes.get(c.get("next")) or {}).get("type") != "branch"]
                if bad:
                    gaps.append((K, "候选不是分支", f"枝干 {nid} 的候选 {bad} 必须是【分支】", ""))
            got_n, got_e = self.exits(nid)
            if exp_n != got_n or exp_e != got_e:
                gaps.append((K, "边不等价",
                             f"期望 next={exp_n} on_error={exp_e}",
                             f"实际 next={got_n} on_error={got_e}  [{nid}]"))
        return gaps, notes


# ======================================================================
# 修复
# ======================================================================
def next_free_num(flow):
    return max([fe.node_no(flow, n, 0) for n in flow["nodes"]] or [0]) + 1


def add_node(flow, nid, nd, after=None):
    """插一个节点（不接链序 —— v4 的边只看 next；链序只影响显示，插在源节点后面便于看）"""
    nd["num"] = next_free_num(flow)
    flow["nodes"][nid] = nd
    chain = flow["chain"]
    if nid not in chain:
        chain.insert(chain.index(after) + 1 if after in chain else len(chain), nid)
    return nid


def branch_from(flow, src_nid, tag):
    """照着 src 的识别条件造一个【分支】（枝干候选必须是分支；点击节点的 on_error 也靠它兜底）"""
    src = flow["nodes"][src_nid]
    p = src.get("props") or {}
    props = dict(fe.NODE_TYPES["branch"]["defaults"])
    props.update(threshold=float(p.get("threshold") or 0.7),
                 timeout=int(p.get("timeout") or 3000))
    if p.get("template"):
        props["template"] = p["template"]
    if p.get("text"):
        props["ocr_text"] = p["text"]
    if p.get("roi"):
        props["roi"] = p["roi"]
    nid = f"b_{tag}"
    return add_node(flow, nid, {"type": "branch", "x": float(src.get("x") or 0) + 322,
                                "y": float(src.get("y") or 0), "title": tag + "?",
                                "props": props, "hit_next": src_nid, "miss_next": None},
                    after=src_nid)


def node_from_legacy(flow, K, v, after=None, d=None):
    """按老管线定义建一个画布节点（只覆盖重建时漏掉的这些简单类型）"""
    act = v.get("action", "DoNothing")
    reco = v.get("recognition")
    # 门槛：DoNothing + next + on_error（条件在子节点身上）→ 建成【分支】（命中走子节点、
    # 未中走 on_error）。这是重建流程里既有的「合并」约定，别建成 tap —— 那会点 (0,0)。
    if act == "DoNothing" and not reco and v.get("on_error") and d is not None:
        child = (v.get("next") or [None])[0]
        cv = (d.get(child) or {}) if child else {}
        props = dict(fe.NODE_TYPES["branch"]["defaults"])
        props["timeout"] = int(v.get("timeout") or 3000)
        if cv.get("template"):
            props["template"] = cv["template"]
            props["threshold"] = float(cv.get("threshold", 0.7))
        elif cv.get("expected") or cv.get("text"):
            t = cv.get("expected") or cv.get("text")
            props["ocr_text"] = ",".join(t) if isinstance(t, list) else str(t)
            props["threshold"] = float(cv.get("threshold", 0.3))
        x = y = 46.0
        if after and after in flow["nodes"]:
            a = flow["nodes"][after]
            x, y = float(a.get("x") or 46) + 322, float(a.get("y") or 46)
        return add_node(flow, "n_" + K, {"type": "branch", "x": x, "y": y, "title": K + "?",
                                         "props": props, "hit_next": None, "miss_next": None},
                        after=after)
    props = {}
    if reco == "TemplateMatch":
        props["template"] = v.get("template", "")
        props["threshold"] = float(v.get("threshold", 0.7))
    elif reco == "OCR":
        props["text"] = ",".join(v.get("expected") or v.get("text") or [])
        props["threshold"] = float(v.get("threshold", 0.3))
    if v.get("roi"):
        props["roi"] = ",".join(map(str, v["roi"]))
    if act == "DoNothing" and reco and len(v.get("next") or []) == 1:
        # 「等这个条件出现，然后往下走」——编辑器里就是【分支】（命中往下、未中即止；
        # 当它是枝干候选时，未中由枝干的候选链兜）
        props = dict(fe.NODE_TYPES["branch"]["defaults"])
        props["timeout"] = int(v.get("timeout") or 3000)
        if reco == "TemplateMatch":
            props["template"] = v.get("template", "")
            props["threshold"] = float(v.get("threshold", 0.7))
        else:
            tt = v.get("expected") or v.get("text") or ""
            props["ocr_text"] = ",".join(tt) if isinstance(tt, list) else str(tt)
            props["threshold"] = float(v.get("threshold", 0.3))
        if v.get("roi"):
            props["roi"] = ",".join(map(str, v["roi"]))
        x = y = 46.0
        if after and after in flow["nodes"]:
            a = flow["nodes"][after]
            x, y = float(a.get("x") or 46) + 322, float(a.get("y") or 46)
        return add_node(flow, "n_" + K, {"type": "branch", "x": x, "y": y, "title": K + "?",
                                         "props": props, "hit_next": None, "miss_next": None},
                        after=after)
    if act == "DoNothing" and not reco and len(v.get("next") or []) == 1:
        x = y = 46.0
        if after and after in flow["nodes"]:
            a = flow["nodes"][after]
            x, y = float(a.get("x") or 46) + 322, float(a.get("y") or 46)
        return add_node(flow, "n_" + K, {"type": "pass", "x": x, "y": y, "title": K,
                                         "props": {"emit_name": ""}}, after=after)
    if act == "Click" and v.get("target"):
        t = "tap"
        props.update(x=int(v["target"][0]), y=int(v["target"][1]))
    elif reco == "OCR":
        t = "ocr_click"
    elif act == "Click":
        t = "tpl_click"
        props["order_by"] = "Score" if v.get("order_by") else ""
    else:
        t = "tap"
    for k in ("pre_delay", "post_delay", "repeat", "repeat_delay", "timeout"):
        if k in v:
            props[k] = v[k]
    if v.get("post_wait_freezes"):
        props["post_wait_freezes"] = v["post_wait_freezes"]
    base = dict(fe.NODE_TYPES[t]["defaults"])
    base.update(props)
    x = y = 46.0
    if after and after in flow["nodes"]:
        a = flow["nodes"][after]
        x, y = float(a.get("x") or 46) + 322, float(a.get("y") or 46)
    nid = "n_" + K
    return add_node(flow, nid, {"type": t, "x": x, "y": y, "title": K, "props": base},
                    after=after)


def ensure_target(flow, g, K, owner_nid, timeout, wrap=False):
    """老节点 K 在画布上的「入口节点」：优先复用，其次照它造。
    ★ 返回的必须是**画布节点 id**（不是名字）；公共节点用它的【收口】节点。
    wrap=True（枝干候选）时，点击类要包一层【分支】—— 枝干的候选必须是分支。"""
    if K.startswith("Common_"):
        c = g.credit.get(K) or []
        if c:
            return c[0]
        nid = "c_" + K
        add_node(flow, nid, {"type": "common", "x": 46.0, "y": 46.0, "title": K,
                             "props": {"node": K}}, after=owner_nid)
        g.credit.setdefault(K, []).append(nid)
        return nid
    nid = "n_" + K
    if nid in flow["nodes"]:
        if wrap and flow["nodes"][nid].get("type") in ("tpl_click", "ocr_click"):
            b = "b_" + K
            if b in flow["nodes"]:
                return b
            return branch_from(flow, nid, K)
        return nid
    b = "b_" + K
    return b if b in flow["nodes"] else None


def make_switch(flow, src_nid, targets, timeout, tag):
    """给一个节点接上多个后继 → 【枝干判定】。
    ★ 候选必须是【分支】节点（判定条件只能配在分支上，校验会拦），所以点击类要先包一层。"""
    cands = []
    for K, nid in targets:
        nd = flow["nodes"].get(nid) or {}
        if nd.get("type") in ("tpl_click", "ocr_click"):
            b = "b_" + K
            nid = b if b in flow["nodes"] else branch_from(flow, nid, K)
        cands.append({"timeout": int(timeout), "next": nid})
    props = dict(fe.NODE_TYPES["switch"]["defaults"])
    props.update(candidates=cands, miss_next=None)
    src = flow["nodes"][src_nid]
    nid = add_node(flow, f"n_{tag}_候选",
                   {"type": "switch", "x": float(src.get("x") or 0) + 322,
                    "y": float(src.get("y") or 0), "title": tag + "（多候选）",
                    "props": props}, after=src_nid)
    return nid


def fix_task(bundle, task, legacy_file, entry, apply_edits):
    g = Graph(bundle, task, legacy_file, entry)
    flow, nodes = g.flow, g.nodes
    plans, notes = [], []

    worklist = list(sorted(g.reachable))
    done = set()

    def target_for(K, owner_nid, timeout, wrap=False):
        """老名字 K → 画布入口节点（必要时就地造节点 / 包分支 / 建公共节点）"""
        if K.startswith("Common_"):
            c = g.credit.get(K) or []
            if c:
                return c[0], ""
            nid = "c_" + K
            add_node(flow, nid, {"type": "common", "x": 46.0, "y": 46.0, "title": K,
                                 "props": {"node": K}}, after=owner_nid)
            g.credit.setdefault(K, []).append(nid)
            plans.append(f"     + 建收口节点 {nid}（{K}）")
            return nid, ""
        v = g.d.get(K)
        if v is None:
            return None, f"老管线里没有 {K}"
        nid = ensure_target(flow, g, K, owner_nid, timeout, wrap)
        if nid:
            return nid, ""
        # 画布上没有 → 按老定义造出来（这轮漏掉的支线节点）
        node_from_legacy(flow, K, v, after=owner_nid, d=g.d)
        plans.append(f"     + 建节点 n_{K}（{v.get('action')}/{v.get('recognition')}，"
                     f"照老管线 {K} 的定义）")
        # 这个 K 可能早就被遍历过（那会儿节点还不存在 → 跳过了），所以要**重新**排到队尾
        worklist.append(K)
        return ensure_target(flow, g, K, owner_nid, timeout, wrap), ""

    def run_fix_loop():
        """按工作队列补边；建出来的新节点会重新入队，所以可以多跑几轮。"""
        for K in worklist:
            done.add(K)
            nid = g.rep(K)
            if nid is None or nodes[nid].get("type") == "common":
                continue
            if nodes[nid].get("type") == "switch":
                continue                        # 枝干由下面「候选整份对齐」那一段统一处理
            exp_n, exp_e = g.spec(K)
            if not exp_n and not exp_e:
                continue
            got_n, got_e = g.exits(nid)
            if exp_n == got_n and exp_e == got_e:
                continue
            nd = nodes[nid]
            timeout = int((nd.get("props") or {}).get("timeout") or 3000)

            # 入口：先保证节点本身有识别（点击类的 on_error 需要包一层分支）
            if nd.get("type") in ("tpl_click", "ocr_click") and exp_e:
                b = branch_from(flow, nid, K)
                plans.append(f"     + 包分支 {b}（命中 → {nid}，未中 → {exp_e[0]}）")
                nd = nodes[nid]
                bnode = nodes[b]
                tgt, why = target_for(exp_e[0], b, timeout)
                bnode["miss_next"] = tgt
                # 谁指着 n_K 就要改指 b_K（包装分支自己除外，否则 hit_next 会指回自己）
                for src in list(nodes):
                    if src == b:
                        continue
                    s = nodes[src]
                    if s.get("next") == nid:
                        s["next"] = b
                        plans.append(f"     · {src}.next: {nid} → {b}")
                    if s.get("hit_next") == nid:
                        s["hit_next"] = b
                        plans.append(f"     · {src}.hit_next: {nid} → {b}")
                    for c in (s.get("props") or {}).get("candidates") or []:
                        if c.get("next") == nid:
                            c["next"] = b
                            plans.append(f"     · {src} 的候选: {nid} → {b}")
                g.credit.setdefault(K, []).append(b)
                entry_node = b
                wrapped = True
            else:
                entry_node = nid
                wrapped = False

            # 后继（可能多个 → 补一个【枝干判定】）
            tgts = []
            for x in exp_n:
                t, why = target_for(x, entry_node, timeout)
                if not t:
                    plans.append(f"     !! 后继 {x} 解析不出来：{why}")
                    continue
                tgts.append((x, t))
            if len(tgts) == 1:
                got = tgts[0][1]
            elif len(tgts) > 1:
                got = make_switch(flow, entry_node, tgts, timeout, K)
                plans.append(f"     + 建枝干 {got}（候选 {'、'.join(x for x, _ in tgts)}）")
            else:
                got = None
            if nd.get("type") == "branch":
                nd["hit_next"] = got
            else:
                nd["next"] = got
            plans.append(f"     · {nid}({K}) 的出口: {got_n or '无'} → {exp_n}")
            if exp_e:
                if nd.get("type") == "branch":
                    t, _ = target_for(exp_e[0], nid, timeout)
                    nd["miss_next"] = t
                    plans.append(f"     · {nid}({K}) 的未中/兜底: {got_e or '无'} → {exp_e}")
                elif not wrapped:
                    plans.append(f"     !! {K} 的 on_error 没有承载点（应在前面包分支）")


    run_fix_loop()

    # 枝干候选整份对齐（顺序也要对：引擎是按顺序试的）
    for K in sorted(g.reachable):
        nid = g.rep(K)
        if nid is None or nodes[nid].get("type") != "switch":
            continue
        exp_n, _ = g.spec(K)
        ltimeout = int((g.d.get(K) or {}).get("timeout") or 3000)
        cands, names = [], []
        for x in exp_n:
            t, why = target_for(x, nid, ltimeout, wrap=True)
            if t:
                cands.append({"timeout": ltimeout, "next": t})
                names.append(x)
        if [c["next"] for c in cands] != [c.get("next") for c in
                                          (nodes[nid].get("props") or {}).get("candidates") or []]:
            nodes[nid]["props"]["candidates"] = cands
            nodes[nid]["props"]["miss_next"] = None
            plans.append(f"     · {nid}({K}) 的候选 → {names}")

    # 收尾：入口。生成物的入口取「链首」节点，而老管线的入口是 任务 → （折叠几步）→ 第一个
    # 真节点（如 装卸装备 → CDZB1 门槛）。重建时链首被排成了门槛的子节点（CDZB1_Hit），
    # 门槛就成了不可达的死节点 —— 那一步的 timeout 与 on_error 兜底全丢了。
    cur = [g.entry]
    first = None
    for _hop in range(6):
        nxt = []
        for x in cur:
            nxt += g.eff(x, "next")
        first = next((g.rep(x) for x in nxt if g.rep(x)), None)
        if first:
            break
        cur = nxt
        if not cur:
            break
    if first and flow["chain"] and flow["chain"][0] != first:
        plans.append(f"     · 链首 {flow['chain'][0]} → {first}（生成物入口取链首，"
                     f"老管线的入口指向它）")
        flow["chain"].remove(first)
        flow["chain"].insert(0, first)

    g.props_fix(plans)      # 参数按老管线补回（边的等价之外，参数也要对）
    run_fix_loop()          # 候选段里新建的节点还要轮到一次

    # 收尾：枝干候选一律包成【分支】（校验强制要求；合成枝干时容易漏，这里兜底）
    for nid, nd in list(nodes.items()):
        if nd.get("type") != "switch":
            continue
        for c in (nd.get("props") or {}).get("candidates") or []:
            t = c.get("next")
            tnd = nodes.get(t) or {}
            if tnd.get("type") in ("tpl_click", "ocr_click"):
                base = re.sub(r"^[nbc]_+", "", t)
                b = "b_" + base
                if b not in nodes:
                    b = branch_from(flow, t, base)
                c["next"] = b
                plans.append(f"     · {nid} 的候选 {t} → 包成 {b}")

    # 收尾：用**改后**的流程重建索引再审一遍，必须归零
    g2 = Graph(bundle, task, legacy_file, entry, flow=flow)
    gaps, _ = g2.audit()
    pgaps, _cannot = g2.props_audit()
    for K, f, lv, gv in pgaps:
        gaps.append((K, "参数不一致", f"老管线 {f}={lv!r}", f"画布 {gv!r}"))
    return flow, plans, gaps


def main():
    ap = argparse.ArgumentParser(description="把这 5 个重建流程的边补回「与手写管线等价」")
    ap.add_argument("--apply", action="store_true", help="落盘（默认只审计+干跑）")
    ap.add_argument("--task", nargs="*", default=list(TASKS), help="只处理这些任务")
    ap.add_argument("--taskpack", default=None)
    ap.add_argument("--audit-only", action="store_true", help="只看审计，不做修复")
    args = ap.parse_args()
    bundle = args.taskpack or os.path.join(project_paths.PROJECT_ROOT, "whmx")
    stamp = datetime.date.today().strftime("%Y%m%d")

    print("=" * 78)
    print("重建流程的边修复（对齐手写管线）")
    print("=" * 78)
    print(f"任务包: {bundle}    模式: {'落盘' if args.apply else '干跑'}\n")

    bad = 0
    todo = {}
    for task in args.task:
        legacy_file, entry = TASKS[task]
        g = Graph(bundle, task, legacy_file, entry)
        gaps, notes = g.audit()
        print(f"=== {task}（老管线 {legacy_file}）：可达老节点 {len(g.reachable)} 个，"
              f"缺口 {len(gaps)} 处 ===")
        for K, kind, a, b in gaps:
            print(f"   [{kind}] {K}\n        {a}\n        {b}")
        if notes:
            print("   （等价、无需改：" + "、".join(f"{K}={w}" for K, w in sorted(notes)) + "）")
        if args.audit_only:
            print()
            bad += len(gaps)
            continue
        flow, plans, left = fix_task(bundle, task, legacy_file, entry, args.apply)
        print("   —— 修复 ——" if plans else "   —— 无需修复 ——")
        for p in plans:
            print(p)
        if left:
            bad += len(left)
            print(f"   !! 修完还剩 {len(left)} 处不一致：")
            for K, kind, a, b in left:
                print(f"      [{kind}] {K}\n        {a}\n        {b}")
        else:
            print("   ✓ 修完审计归零")
        todo[task] = flow
        print()

    if args.audit_only:
        print(f"审计结束：共 {bad} 处缺口。")
        return 1 if bad else 0

    if bad:
        print("还有没修干净的，先别落盘。")
        return 1
    if not args.apply:
        print("干跑结束（未落盘）。确认后加 --apply。")
        return 0

    print("落盘中…")
    for task, flow in todo.items():
        src = os.path.join(FLOWS_DIR, f"{task}.flow.json")
        bak = f"{src}.bak_before_edgefix_{stamp}"
        if not os.path.exists(bak):
            shutil.copy2(src, bak)
            print(f"  备份 {bak}")
        fe.save_flow(flow)
        path, _ = fe.write_pipeline_json(flow, (fe.FRAME_W, fe.FRAME_H))
        dst = os.path.join(bundle, "pipeline", f"vf_{task}.json")
        shutil.copy2(path, dst)
        print(f"  写  {src}\n  写  {path}\n  写  {dst}")
    print("\n下一步：本机引擎加载 → 重编译重装 → 手机实跑")
    return 0


if __name__ == "__main__":
    sys.exit(main())
