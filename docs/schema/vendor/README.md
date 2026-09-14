# vendor/ —— 官方 schema 只读副本

| 文件 | 来源 | 版本 |
|---|---|---|
| `pipeline.schema.json` | `E:\MaaWH\MaaFramework\tools\pipeline.schema.json`（上游 `MaaXYZ/MaaFramework`） | 本地副本提交 `96b046d8a5d2ded98c715cf6adf87fb0fdcb7218`（2026-09-04） |

## 为什么要拷贝一份，而不是跨仓库引用

编辑器和 MaaWH 仓库是**两个独立仓库**，可以各自搬家（见 `README.md` 的路径规则）。
如果 overlay schema 用相对路径 `../../MaaWH/MaaFramework/tools/...` 引用，一旦 MaaWH
改名或移动，schema 立刻失效，而且失败方式是「校验静默跳过」——最难发现的那种。

所以这里保留一份只读副本，只在上游协议升级时手动同步（对照上游 commit 更新本文件
并在上表登记新版本）。

## 注意

- 这份文件**不要手工修改**。本项目自己的约束写在 `../pipeline.vf.overlay.schema.json`
  里，用 `allOf` 叠加在它之上。
- 官方 schema 用 `patternProperties: {"^(?!\\$).*": ...}` —— 即「不以 `$` 开头的根字段
  才当节点校验」。这与引擎行为一致（`kNodePrefix_Ignore = "$"`），也是本编辑器能把
  `$meta` 元数据放进生成物的依据。
