# -*- coding: utf-8 -*-
"""adb 路径与手机序列号的配置加载（device.json，与流程编辑器/框选工具共用）。

换电脑时不用改代码：把 device.json 里的 adb / device 改成这台机器的值即可。
文件不存在、键缺省或值为空串 → 逐项回退到下面的写死默认值（老电脑行为不变）。
device.json 是本机环境配置，已进 .gitignore，不入库；首次运行会自动生成一份
默认值的，让人打开就知道能改哪里。
"""
import json
import os

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(TOOLS_DIR, "device.json")

DEFAULT_ADB = r"D:\android-studio\Sdk\platform-tools\adb.exe"
DEFAULT_DEVICE = "2c92e197"


def load_device_config(path=CONFIG_PATH):
    """读 device.json → (adb, device)。

    每个键独立回退：只写了 device 没写 adb，adb 就用默认值。
    文件坏了（不是合法 JSON）= 整体回退默认，不让编辑器起不来。"""
    adb, device = DEFAULT_ADB, DEFAULT_DEVICE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            adb = str(data.get("adb") or "").strip() or DEFAULT_ADB
            device = str(data.get("device") or "").strip() or DEFAULT_DEVICE
    except Exception:
        pass
    return adb, device


def ensure_config_file(path=CONFIG_PATH):
    """没有 device.json 就生成一份（填当前默认值）；已有则不动。返回是否新建了。"""
    if os.path.exists(path):
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"adb": DEFAULT_ADB, "device": DEFAULT_DEVICE},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        return False
    return True
