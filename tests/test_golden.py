# -*- coding: utf-8 -*-
"""pytest 版黄金基线测试（本机暂未安装 pytest，可先跑 tests/check_golden.py）。

安装后即可用：  python -m pytest tests/ -v
未安装 pytest 时本文件不会被收集，不影响其它任何东西。
"""
import os
import sys
import json
import hashlib

import pytest

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


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _norm(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


_META_PATH = os.path.join(GOLDEN_DIR, "_baseline_meta.json")
if not os.path.isfile(_META_PATH):
    pytest.skip("未生成黄金基线：先运行 python tests/make_golden.py",
                allow_module_level=True)

META = _load(_META_PATH)


@pytest.mark.parametrize("entry", META["flows"],
                         ids=[e["stem"] for e in META["flows"]])
def test_golden(entry):
    """P0 系列重构的硬性不变量：生成结果与基线逐字节一致。"""
    stem, name = entry["stem"], entry["name"]
    base_issues = _load(os.path.join(GOLDEN_DIR, f"issues_{stem}.json"))

    with open(os.path.join(fe.FLOWS_DIR, entry["file"]), encoding="utf-8") as f:
        flow = fe.normalize_flow(json.load(f))

    errs, warns = fe.validate_flow(flow, FRAME_WH)
    try:
        out = fe.build_pipeline(flow, FRAME_WH)
        crash = None
    except fe.FlowValidationError as ex:
        out, errs, crash = None, list(ex.errors), None
    except Exception as ex:                       # noqa: BLE001
        out, crash = None, f"{type(ex).__name__}: {ex}"

    assert (crash is None) == (base_issues.get("crash") is None), \
        f"{name}: 异常情况变化（基线 {base_issues.get('crash')!r} → 实际 {crash!r}）"
    assert sorted(errs) == sorted(base_issues.get("errors") or []), \
        f"{name}: errors 不一致"

    lost = [w for w in (base_issues.get("warnings") or []) if w not in warns]
    assert not lost, f"{name}: 基线 warnings 丢失 {lost}"

    vf_path = os.path.join(GOLDEN_DIR, f"vf_{stem}.json")
    if out is None:
        assert not os.path.isfile(vf_path), f"{name}: 基线有生成结果，现在却生成失败"
    else:
        assert os.path.isfile(vf_path), f"{name}: 基线无生成结果，现在却能生成"
        got = {k: v for k, v in out.items() if k != "$meta"}
        assert _norm(_load(vf_path)) == _norm(got), f"{name}: 生成结果与基线不一致"
        # $meta 含时间戳，不参与基线比对，但两个指纹必须能重算出来
        meta = out.get("$meta")
        assert isinstance(meta, dict), f"{name}: 生成物缺少 $meta"
        assert meta.get("flowHash") == fe.flow_fingerprint(flow), \
            f"{name}: $meta.flowHash 与流程定义对不上"
        assert meta.get("pipelineHash") == fe.pipeline_fingerprint(out), \
            f"{name}: $meta.pipelineHash 与生成物对不上"


def test_flow_namespace_unique():
    """流程名（生成命名空间 VF_<name>）必须互不相同，否则节点会互相冲突。"""
    assert not META["problems"], f"基线已记录问题：{META['problems']}"


def test_baseline_matches_current_editor():
    """提示性检查：基线与当前 flow_editor.py 是否同一版本（仅 warn，不失败）。"""
    cur = _sha256(os.path.join(TOOLS_DIR, "flow_editor.py"))
    if cur != META["flowEditorSha256"]:
        pytest.skip(f"flow_editor.py 已变化（基线 {META['flowEditorSha256'][:12]}… "
                    f"→ 当前 {cur[:12]}…），diff 检查仍已执行")
