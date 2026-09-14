# -*- coding: utf-8 -*-
"""生成黄金基线（golden baseline）—— 必须在【未修改 flow_editor.py】时运行一次。

用途：证明后续重构（P0-1 单一真相源等）没有改变生成结果。
产物（写入 tests/golden/）：
  _baseline_meta.json     基线元信息（flow_editor.py 的 sha256、Python 版本、流程清单）
  vf_<流程名>.json        build_pipeline 的输出（校验通过时）
  issues_<流程名>.json     {"errors": [...], "warnings": [...], "crash": null|"..."}

约定：
  - frame_wh 固定 (1280, 720)，与编辑器默认基准一致。
  - 校验不通过的流程照样记录（errors 非空、pipeline 为 null），
    因为它本身就是基线的一部分（例如「易物所购买」的枝干为空）。
  - 基线一旦生成就不要手工编辑；要更新基线必须整份重新生成并 commit。

用法：
    python tests/make_golden.py            # 生成/覆盖基线
    python tests/make_golden.py --dry-run  # 只看会生成什么，不写文件
"""
import os
import sys
import json
import glob
import hashlib
import platform
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(HERE)
sys.path.insert(0, TOOLS_DIR)

import flow_editor as fe  # noqa: E402

GOLDEN_DIR = os.path.join(HERE, "golden")
FRAME_WH = (1280, 720)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def build_one(path):
    """返回 (name, pipeline|None, errors, warnings, crash|None, chain_len, template_set)"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    flow = fe.normalize_flow(raw)
    name = flow.get("name") or os.path.basename(path)
    errs, warns = fe.validate_flow(flow, FRAME_WH)
    tmpls = sorted(fe.flow_templates(flow))
    try:
        out = fe.build_pipeline(flow, FRAME_WH)
    except fe.FlowValidationError as ex:
        return name, None, list(ex.errors), list(warns), None, len(flow.get("chain", [])), tmpls
    except Exception as ex:                      # 未预期异常也要记录，不能静默跳过
        return name, None, [], list(warns), f"{type(ex).__name__}: {ex}", \
               len(flow.get("chain", [])), tmpls
    # validate 报错但 build 没抛 → 说明两者不一致，属于要立刻发现的异常
    if errs:
        return name, out, list(errs) + ["⚠ build 未抛错但 validate 报错（校验/生成不一致）"], \
               list(warns), None, len(flow.get("chain", [])), tmpls
    return name, out, [], list(warns), None, len(flow.get("chain", [])), tmpls


def main():
    dry = "--dry-run" in sys.argv
    flows = sorted(glob.glob(os.path.join(fe.FLOWS_DIR, "*.flow.json")))
    if not flows:
        print("✗ 没找到任何 flows/*.flow.json")
        return 1
    if not dry:
        os.makedirs(GOLDEN_DIR, exist_ok=True)

    entries, names, problems = [], {}, []
    print(f"共 {len(flows)} 个流程，frame_wh={FRAME_WH[0]}x{FRAME_WH[1]}")
    print(f"{'流程名':<22}{'链节点':>6}{'生成节点':>8}{'模板':>6}  状态")
    for path in flows:
        name, out, errs, warns, crash, n_chain, tmpls = build_one(path)
        stem = fe.safe_name(name)
        if stem in names:
            problems.append(f"流程名重复：{name}（{os.path.basename(path)} 与 {names[stem]}）"
                            f"→ 它们会共用 VF_{name} 命名空间")
        names[stem] = os.path.basename(path)

        entries.append({
            "file": os.path.basename(path), "name": name, "stem": stem,
            "chainLen": n_chain, "pipelineNodes": len(out) if out else 0,
            "templates": tmpls, "errors": errs, "warnings": warns, "crash": crash,
        })

        if crash:
            status = f"✗ 异常 {crash}"
        elif errs:
            status = f"✗ 校验失败 {len(errs)} 条（预期记录）"
        else:
            status = "✓"

        if not dry:
            _dump(os.path.join(GOLDEN_DIR, f"issues_{stem}.json"),
                  {"file": os.path.basename(path), "name": name,
                   "errors": errs, "warnings": warns, "crash": crash})
            if out is not None:
                # 基线只存 pipeline 本体，不含 $meta（$meta 里带生成时间戳，
                # 存进基线会导致每次重新生成都产生假差异）
                _dump(os.path.join(GOLDEN_DIR, f"vf_{stem}.json"),
                      {k: v for k, v in out.items() if k != "$meta"})
            else:
                p = os.path.join(GOLDEN_DIR, f"vf_{stem}.json")
                if os.path.isfile(p):
                    os.remove(p)          # 曾生成过但现在失败了 → 移除陈旧基线
        print(f"{name:<22}{n_chain:>6}{len(out) if out else 0:>8}{len(tmpls):>6}  {status}")
        for e in errs[:3]:
            print(f"{'':<38}  err: {e}")

    meta = {
        "generatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "frameWh": list(FRAME_WH),
        "flowEditorSha256": _sha256(os.path.join(TOOLS_DIR, "flow_editor.py")),
        "flowCount": len(entries),
        "flows": entries,
        "problems": problems,
    }
    if not dry:
        _dump(os.path.join(GOLDEN_DIR, "_baseline_meta.json"), meta)

    print()
    print(f"flow_editor.py sha256 = {meta['flowEditorSha256']}")
    if problems:
        print("⚠ 基线发现的问题：")
        for p in problems:
            print("   -", p)
    print(("（--dry-run，未写文件）" if dry else f"基线已写入 {GOLDEN_DIR}") )
    return 0


if __name__ == "__main__":
    sys.exit(main())
