# -*- coding: utf-8 -*-
"""黄金基线检查器（不依赖 pytest，标准库即可运行）。

断言什么：
  A. 每个流程的 build_pipeline 结果与基线【逐字节一致】（JSON 规范化后比较）
  B. errors 与基线【完全一致】
  C. warnings 必须【包含】基线中的每一条（新增 warning 允许；已有 warning 不允许消失）
  D. 异常情况（crash）与基线一致
  E. 当前 flow_editor.py 的 sha256 若与基线不同，打印提示（不判失败）

用法：
    python tests/check_golden.py            # 检查，通过退出码 0，失败 1
    python tests/check_golden.py -v         # 打印每个流程的结果
"""
import os
import sys
import json
import glob
import hashlib

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


def _norm(obj):
    """把 JSON 规范化成可比较的字符串（键排序、缩进固定、末尾无空白）"""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _first_diff(a, b, limit=6):
    """给出行级差异摘要，便于定位（a=基线, b=实际）"""
    la = _norm(a).splitlines()
    lb = _norm(b).splitlines()
    out = []
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<缺行>"
        y = lb[i] if i < len(lb) else "<缺行>"
        if x != y:
            out.append(f"    L{i + 1}: 基线 {x.strip()!r} → 实际 {y.strip()!r}")
            if len(out) >= limit:
                out.append("    …")
                break
    return out


def check(verbose=False):
    meta_path = os.path.join(GOLDEN_DIR, "_baseline_meta.json")
    if not os.path.isfile(meta_path):
        print("✗ 找不到基线，请先运行：python tests/make_golden.py")
        return 1
    meta = _load(meta_path)

    cur_sha = _sha256(os.path.join(TOOLS_DIR, "flow_editor.py"))
    if cur_sha != meta["flowEditorSha256"]:
        print(f"ℹ flow_editor.py 已变化（基线 {meta['flowEditorSha256'][:12]}… → "
              f"当前 {cur_sha[:12]}…）—— 若这一步是纯重构，下列比较应全部一致。")
    print(f"基线生成于 {meta['generatedAt']}，共 {meta['flowCount']} 个流程\n")

    failures, checked = [], 0
    for entry in meta["flows"]:
        stem = entry["stem"]
        name = entry["name"]
        issues_path = os.path.join(GOLDEN_DIR, f"issues_{stem}.json")
        vf_path = os.path.join(GOLDEN_DIR, f"vf_{stem}.json")
        base_issues = _load(issues_path)

        with open(os.path.join(fe.FLOWS_DIR, entry["file"]), encoding="utf-8") as f:
            flow = fe.normalize_flow(json.load(f))

        errs, warns = fe.validate_flow(flow, FRAME_WH)
        crash = None
        out = None
        try:
            out = fe.build_pipeline(flow, FRAME_WH)
        except fe.FlowValidationError as ex:
            errs = list(ex.errors)
        except Exception as ex:
            crash = f"{type(ex).__name__}: {ex}"

        # D. 异常一致
        if (crash is None) != (base_issues.get("crash") is None):
            failures.append(f"[{name}] 异常情况变化：基线 {base_issues.get('crash')!r} → 实际 {crash!r}")

        # B. errors 完全一致
        if sorted(errs) != sorted(base_issues.get("errors") or []):
            failures.append(f"[{name}] errors 不一致：\n"
                            f"    基线 {base_issues.get('errors')}\n    实际 {errs}")

        # C. warnings 只增不减
        lost = [w for w in (base_issues.get("warnings") or []) if w not in warns]
        if lost:
            failures.append(f"[{name}] 基线的 warnings 丢失了 {len(lost)} 条：\n"
                            + "\n".join(f"    - {w}" for w in lost[:5]))
        added = [w for w in warns if w not in (base_issues.get("warnings") or [])]

        # A. 生成结果逐字节一致
        if out is None:
            if os.path.isfile(vf_path):
                failures.append(f"[{name}] 基线有生成结果，但现在生成失败")
        else:
            if not os.path.isfile(vf_path):
                failures.append(f"[{name}] 基线无生成结果，但现在能生成（{len(out)} 个节点）")
            else:
                base_out = _load(vf_path)
                if _norm(base_out) != _norm(out):
                    same_keys = set(base_out) == set(out)
                    failures.append(
                        f"[{name}] 生成结果与基线不一致"
                        f"（{'节点集合相同，字段/值有差异' if same_keys else '节点集合都不同'}）:\n"
                        + "\n".join(_first_diff(base_out, out)))
        checked += 1

        if verbose:
            flag = "✓"
            print(f"  {flag} {name:<22} 生成 {len(out) if out else 0:>3} 节点"
                  f"，errors {len(errs)}，warnings {len(warns)}"
                  + (f"，新增 warning {len(added)}" if added else ""))

    print(f"— 已检查 {checked} 个流程 —")
    if failures:
        print(f"\n✗ 基线检查失败（{len(failures)} 项）：\n")
        for f_ in failures:
            print(f_)
        return 1
    print("\n✓ 全部通过：生成结果逐字节一致，errors 一致，无 warning 丢失")
    return 0


if __name__ == "__main__":
    sys.exit(check(verbose="-v" in sys.argv))
