# -*- coding: utf-8 -*-
"""Schema 校验器（P1-6 门禁）。

两件事：
  ① 17 个 flows/*.flow.json 必须符合 docs/schema/flow.schema.json
  ② 每个能生成成功的流程，其生成物必须符合
     docs/schema/pipeline.vf.overlay.schema.json（= 官方 schema + VF_ 命名空间约束）

需要 jsonschema 包；缺失时明确跳过并返回 0（不伪装成通过），同时打印跳过原因。

用法：
    python tests/check_schema.py           # 校验，失败退出码 1
    python tests/check_schema.py -v        # 打印每个流程的结论
"""
import os
import sys
import json
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(HERE)
sys.path.insert(0, TOOLS_DIR)

import flow_editor as fe  # noqa: E402

SCHEMA_DIR = os.path.join(TOOLS_DIR, "docs", "schema")
FLOW_SCHEMA = os.path.join(SCHEMA_DIR, "flow.schema.json")
PIPE_SCHEMA = os.path.join(SCHEMA_DIR, "pipeline.vf.overlay.schema.json")
FRAME_WH = (1280, 720)


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main():
    verbose = "-v" in sys.argv
    try:
        import jsonschema
    except ImportError:
        print("跳过：未安装 jsonschema（pip install jsonschema 后可启用本门禁）")
        return 0

    for p in (FLOW_SCHEMA, PIPE_SCHEMA):
        if not os.path.isfile(p):
            print(f"✗ 找不到 schema：{p}")
            return 1

    flow_schema = _load(FLOW_SCHEMA)
    pipe_schema = _load(PIPE_SCHEMA)
    # overlay 用相对路径 $ref vendor/pipeline.schema.json，交给 jsonschema 的
    # 引用解析器按 base_dir 解析（新版走 registry，旧版走 RefResolver）
    base = os.path.dirname(PIPE_SCHEMA)
    vendor_dir = os.path.join(base, "vendor")
    overlay_id = pipe_schema.get("$id") or "https://maawh.local/schema/flow.overlay.json"
    prefix = overlay_id.rsplit("/", 1)[0]
    try:
        from referencing import Registry, Resource            # jsonschema >= 4.18
        from referencing.jsonschema import DRAFT202012
        # 官方 schema 之间还互相有相对 $ref（./custom.recognition.schema.json 等），
        # 所以 vendor/ 下的每一份都要按「可能被解析出的 URI」逐一注册。
        registry = Registry()
        for fn in sorted(os.listdir(vendor_dir)):
            if not fn.endswith(".json"):
                continue
            res = Resource.from_contents(
                _load(os.path.join(vendor_dir, fn)), default_specification=DRAFT202012)
            for uri in (f"vendor/{fn}", f"./{fn}",
                        f"{prefix}/vendor/{fn}", f"{prefix}/{fn}"):
                registry = registry.with_resource(uri, res)
        pipe_validator = jsonschema.Draft202012Validator(pipe_schema, registry=registry)
    except ImportError:                                        # 老版本回退
        resolver = jsonschema.RefResolver(base_uri=prefix + "/", referrer=pipe_schema)
        for fn in sorted(os.listdir(vendor_dir)):
            if fn.endswith(".json"):
                resolver.store[f"{prefix}/vendor/{fn}"] = _load(
                    os.path.join(vendor_dir, fn))
        pipe_validator = jsonschema.Draft202012Validator(
            pipe_schema, resolver=resolver)

    flow_validator = jsonschema.Draft202012Validator(flow_schema)

    fails, n_flow, n_pipe = [], 0, 0
    for path in sorted(glob.glob(os.path.join(fe.FLOWS_DIR, "*.flow.json"))):
        name = os.path.basename(path)
        raw = _load(path)
        errs = sorted(flow_validator.iter_errors(raw),
                      key=lambda e: list(e.absolute_path))
        if errs:
            fails.append((name, "flow.schema", errs[:3]))
        n_flow += 1

        flow = fe.normalize_flow(raw)
        try:
            out = fe.build_pipeline(flow, FRAME_WH)
        except fe.FlowValidationError as ex:
            if verbose:
                print(f"  - {name:<28} 语义校验未通过，跳过生成物 schema 检查")
            continue
        errs = sorted(pipe_validator.iter_errors(out),
                      key=lambda e: list(e.absolute_path))
        if errs:
            fails.append((name, "pipeline.vf.overlay.schema", errs[:3]))
        n_pipe += 1
        if verbose:
            print(f"  {'✗' if errs else '✓'} {name:<28} 生成物 schema "
                  f"{'不通过' if errs else '通过'}")

    print(f"flow schema：检查 {n_flow} 个流程定义；"
          f"生成物 schema：检查 {n_pipe} 个生成物")
    if fails:
        print(f"\n✗ schema 校验失败（{len(fails)} 项）：")
        for name, which, errs in fails:
            print(f"  [{which}] {name}")
            for e in errs:
                loc = "/".join(str(x) for x in e.absolute_path) or "<根>"
                print(f"      {loc}: {e.message[:160]}")
        return 1
    print("✓ schema 校验全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
