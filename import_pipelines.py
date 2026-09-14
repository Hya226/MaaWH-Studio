# -*- coding: utf-8 -*-
"""
反向导入器：把 MaaWH 的 whmx/pipeline 里手写的成熟任务转换为流程编辑器的 .flow.json。
用法：python import_pipelines.py（工程根由 project_paths.py 探测）
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flow_editor as fe

PIPE_DIR = os.path.join(fe.ROOT, "whmx", "pipeline")

TASKS = [
    ("启动", "grind.json", "到主页"),
    ("刷冬谷币", "grind.json", "刷冬谷币"),
    ("征集", "zhengji.json", "征集"),
    ("行会签到", "qiandao.json", "签到段1"),
    ("关闭游戏", "login.json", "关闭游戏"),
    ("装卸装备", "cdzb.json", "装卸装备"),
    ("升好感度", "cdzb.json", "升好感度"),
    ("查找器者", "cdzb.json", "查找器者"),
    ("装备分解", "cdzb.json", "装备分解"),
    ("领取奖励", "cdzb.json", "领取奖励"),
    ("外勤", "cdzb.json", "外勤"),
]


def import_entry(pd, entry, flow_name):
    flow = {"name": flow_name, "chain": [], "nodes": {}}
    notes = []
    idmap = {}
    visited = set()

    def convert(name, depth):
        if depth > 50:
            notes.append("嵌套过深，止于 " + name)
            return idmap.get(name)
        if name in visited:
            return idmap.get(name)
        visited.add(name)
        if name.startswith("Common_"):
            nid = "c_" + name
            flow["nodes"][nid] = {"type": "common", "x": 0, "y": 0, "title": name,
                                  "props": {"node": name}}
            flow["chain"].append(nid)
            idmap[name] = nid
            return nid
        nd = pd.get(name)
        if nd is None:
            notes.append("引用了未定义节点 " + name + "（跳过）")
            return None
        if False:
            nid = "c_" + name
            flow["nodes"][nid] = {"type": "common", "x": 0, "y": 0, "title": name,
                                  "props": {"node": name}}
            flow["chain"].append(nid)
            idmap[name] = nid
            return nid
        act = nd.get("action", "DoNothing")
        reco = nd.get("recognition")
        nxt = nd.get("next") or []
        onerr = nd.get("on_error") or []

        def mk(t, props):
            nid = "n_" + name
            flow["nodes"][nid] = {"type": t, "x": 0, "y": 0, "title": name,
                                  "props": props}
            flow["chain"].append(nid)
            idmap[name] = nid
            return nid

        if act == "DoNothing" and not reco and not onerr and len(nxt) == 1:
            return convert(nxt[0], depth + 1)
        if act == "DoNothing" and not reco and not onerr and len(nxt) > 1:
            for extra in nxt[1:]:
                notes.append(name + " 的候选项 " + extra + " 未导入（多候选仅保留第一条）")
            return convert(nxt[0], depth + 1)
        if act == "DoNothing" and len(nxt) == 1 and onerr:
            b2 = pd.get(nxt[0])
            if b2 and b2.get("recognition") == "TemplateMatch" and b2.get("action") == "Click":
                click_id = convert(nxt[0], depth + 1)
                branch_id = "n_" + name
                props = dict(fe.NODE_TYPES["branch"]["defaults"])
                props.update(template=b2["template"],
                             threshold=float(b2.get("threshold", 0.5)),
                             timeout=int(nd.get("timeout", 1500)))
                if b2.get("roi"):
                    props["roi"] = ",".join(map(str, b2["roi"]))
                flow["nodes"][branch_id] = {"type": "branch", "x": 0, "y": 0,
                                            "title": name + "?", "props": props,
                                            "hit_next": click_id, "miss_next": None}
                flow["chain"].append(branch_id)
                idmap[name] = branch_id
                miss_tail = convert(onerr[0], depth + 1)
                flow["nodes"][branch_id]["miss_next"] = miss_tail
                return branch_id
        if act == "DoNothing" and reco == "OCR" and not onerr and len(nxt) == 1:
            branch_id = "n_" + name
            props = dict(fe.NODE_TYPES["branch"]["defaults"])
            props.update(ocr_text=",".join(nd.get("text") or []),
                         timeout=int(nd.get("timeout", 8000)))
            if nd.get("roi"):
                props["roi"] = ",".join(map(str, nd["roi"]))
            flow["nodes"][branch_id] = {"type": "branch", "x": 0, "y": 0,
                                        "title": name + "?", "props": props,
                                        "hit_next": None, "miss_next": None}
            flow["chain"].append(branch_id)
            idmap[name] = branch_id
            hit_tail = convert(nxt[0], depth + 1)
            flow["nodes"][branch_id]["hit_next"] = hit_tail
            return branch_id
        if act == "DoNothing" and len(nxt) == 1 and onerr:
            H = pd.get(nxt[0])
            if H and H.get("action") == "DoNothing" and \
               H.get("recognition") in ("TemplateMatch", "OCR"):
                branch_id = "n_" + name
                props = dict(fe.NODE_TYPES["branch"]["defaults"])
                props.update(timeout=int(nd.get("timeout", 3000)))
                if H["recognition"] == "OCR":
                    props["ocr_text"] = ",".join(H.get("text") or [])
                else:
                    props["template"] = H["template"]
                    props["threshold"] = float(H.get("threshold", 0.7))
                if H.get("roi"):
                    props["roi"] = ",".join(map(str, H["roi"]))
                flow["nodes"][branch_id] = {"type": "branch", "x": 0, "y": 0,
                                            "title": name + "?", "props": props,
                                            "hit_next": None, "miss_next": None}
                flow["chain"].append(branch_id)
                idmap[name] = branch_id
                hit_tail = convert(H["next"][0], depth + 1) if H.get("next") else None
                flow["nodes"][branch_id]["hit_next"] = hit_tail
                miss_tail = convert(onerr[0], depth + 1)
                flow["nodes"][branch_id]["miss_next"] = miss_tail
                return branch_id
        if reco == "TemplateMatch" and act == "Click":
            props = dict(fe.NODE_TYPES["tpl_click"]["defaults"])
            props.update(template=nd["template"],
                         threshold=float(nd.get("threshold", 0.8)),
                         timeout=int(nd.get("timeout", 8000)),
                         post_delay=int(nd.get("post_delay", 800)),
                         pre_delay=int(nd.get("pre_delay", 0)),
                         rate_limit=int(nd.get("rate_limit", 0) or 0))
            if nd.get("roi"):
                props["roi"] = ",".join(map(str, nd["roi"]))
            if nd.get("order_by") == "Score":
                props["order_by"] = True
            rep_n = int(nd.get("repeat", 1) or 1)
            if rep_n > 1:
                props["repeat"] = rep_n
                props["repeat_delay"] = int(nd.get("repeat_delay", 350))
            nid = mk("tpl_click", props)
            if len(nxt) > 1:
                notes.append(name + " 有 " + str(len(nxt)) + " 个 next 候选，仅导入第一个")
            if nxt:
                convert(nxt[0], depth + 1)
            return nid
        if reco == "OCR" and act == "Click":
            props = dict(fe.NODE_TYPES["ocr_click"]["defaults"])
            props.update(text=",".join(nd.get("text") or []),
                         timeout=int(nd.get("timeout", 8000)),
                         post_delay=int(nd.get("post_delay", 600)),
                         pre_delay=int(nd.get("pre_delay", 0)),
                         rate_limit=int(nd.get("rate_limit", 0) or 0))
            if nd.get("roi"):
                props["roi"] = ",".join(map(str, nd["roi"]))
            nid = mk("ocr_click", props)
            if len(nxt) > 1:
                notes.append(name + " 有 " + str(len(nxt)) + " 个 next 候选，仅导入第一个")
            if nxt:
                convert(nxt[0], depth + 1)
            return nid
        if act == "Click" and "target" in nd:
            props = dict(fe.NODE_TYPES["tap"]["defaults"])
            props.update(x=nd["target"][0], y=nd["target"][1],
                         post_delay=int(nd.get("post_delay", 500)),
                         pre_delay=int(nd.get("pre_delay", 0)))
            nid = mk("tap", props)
            if len(nxt) > 1:
                notes.append(name + " 有多个 next 候选，仅导入第一个")
            if nxt:
                convert(nxt[0], depth + 1)
            return nid
        if reco == "OCR" and act == "Click":
            props = dict(fe.NODE_TYPES["ocr_click"]["defaults"])
            props.update(text=",".join(nd.get("text") or []),
                         timeout=int(nd.get("timeout", 8000)),
                         post_delay=int(nd.get("post_delay", 600)),
                         pre_delay=int(nd.get("pre_delay", 0)),
                         rate_limit=int(nd.get("rate_limit", 0) or 0))
            if nd.get("roi"):
                props["roi"] = ",".join(map(str, nd["roi"]))
            nid = mk("ocr_click", props)
            if nxt:
                convert(nxt[0], depth + 1)
            return nid
        if act == "Swipe":
            props = dict(fe.NODE_TYPES["swipe"]["defaults"])
            props.update(x1=nd["begin"][0], y1=nd["begin"][1],
                         x2=nd["end"][0], y2=nd["end"][1],
                         duration=int(nd.get("duration", 900)),
                         post_delay=int(nd.get("post_delay", 600)))
            nid = mk("swipe", props)
            if len(nxt) > 1:
                notes.append(name + " 有多个 next 候选，仅导入第一个")
            if nxt:
                convert(nxt[0], depth + 1)
            return nid
        if act == "StartApp":
            props = dict(fe.NODE_TYPES["startapp"]["defaults"])
            if nd.get("package"):
                props["package"] = nd["package"]
            props["post_delay"] = int(nd.get("post_delay", 1000))
            nid = mk("startapp", props)
            if nxt:
                convert(nxt[0], depth + 1)
            return nid
        notes.append("节点 " + name + " 无对应节点类型，已跳过其动作")
        return convert(nxt[0], depth + 1) if nxt else None

    convert(entry, 0)
    return flow, notes


def main():
    for name, pfile, entry in TASKS:
        path = os.path.join(PIPE_DIR, pfile)
        pd = fe.jsonc_loads(open(path, encoding="utf-8").read())
        if entry not in pd:
            print("SKIP " + name + ": 入口不在 " + pfile)
            continue
        flow, notes = import_entry(pd, entry, name)
        errs, warns = fe.validate_flow(flow, (1280, 720))
        fe.save_flow(flow)
        tag = "OK" if not errs else "FAIL " + str(errs)
        print(tag, name, ":", len(flow["chain"]), "节点,", len(warns), "警告,", len(notes), "备注")
        for w in warns:
            print("    warn:", w)
        for n in notes:
            print("    note:", n)


if __name__ == "__main__":
    main()
