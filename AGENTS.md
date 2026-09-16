# MaaWH Stdio（流程编辑器工作区）— 交接与操作约定

**给后续 AI / 开发者的对照文档。开工前先看这一份，改完对着检查。**

工作区：`E:\MaaWH Stdio`（独立目录，**不属 MaaWH 仓库**）。
本目录只负责「可视化流程编辑器 + 模板框选工具」；App（`app/`）与任务包（`whmx/`）在 `E:\MaaWH`，
那边的交接文档是 `E:\MaaWH\AGENTS.md`（任务包结构、interface.json 协议、App 侧行为看它）。
两边**交界只有四处**：读负样本 `_tools/neg_frames/`、写模板图 `whmx/image/`、
回写生成物 `whmx/pipeline/vf_*.json`、推手机 `files/taskpacks/whmx/{pipeline,image,interface.json}`。

---

## 一、这堆文件是什么

| 文件 | 作用 |
|---|---|
| `flow_editor.py` | 流程编辑器本体（单文件 Tkinter，260KB+）。画布拖节点 → 连线 → 生成 `VF_*` pipeline → 同步手机 |
| `template_picker.py` | 模板框选工具（负样本校验 + 建议阈值） |
| `import_pipelines.py` | `whmx/pipeline` → `.flow.json` 反向导入（老流程是用它导进来的） |
| `project_paths.py` / `project_root.txt` | 任务包工程根探测（环境变量 `MAAWH_ROOT` → `project_root.txt` → 兄弟/上级目录含 `whmx/image` → 默认 `E:\MaaWH`） |
| `flows/*.flow.json` | **流程定义 = 唯一数据源，要备份** |
| `flows/build/`、`E:\MaaWH\whmx\pipeline\vf_*.json` | 生成物（随时可重新生成） |
| `tests/` | 三道门禁：schema / golden / selftest |
| `tools/migrate_legacy_flows.py` | 老流程迁移（手写管线 → `VF_*`）。默认**干跑**只打印计划，`--apply` 才落盘；`--out DIR` 写一份可审查副本 |
| `tools/fix_flow_edges.py` | 重建流程的**边修复**（对齐手写管线）。审计老管线 vs 画布流程的逐节点有序后继，补 `next`/`miss_next`/丢掉的候选/入口，默认干跑 |
| `启动.bat`、`框选模板.bat` | 双击启动 |

关键常量（`flow_editor.py` 顶部）：`ADB = D:\android-studio\Sdk\platform-tools\adb.exe`、
`DEVICE = "2c92e197"`、`PKG = "com.maawh.app"`、`GAME_PKG = "com.cipaishe.wuhua.bilibili"`、
帧基准 `1280x720`、卡片 `240x60`、网格 `46`。

---

## 二、铁律（都是踩过坑之后定的，按严重程度排）

### 1. 引擎做的是「整目录校验」——一个节点坏，全包全废

`PipelineChecker::check_all_validity` 里任何一项不过 → `PipelineResMgr::load` 失败 →
`Resource.Loading.Failed` → **手机上所有任务都跑不起来**，不只是出问题那一个。

已经出过一次事故：枝干候选写了占位符 `OCR:?`，`expected = ["?"]` 不是合法正则，
整包被引擎拒绝。**所以：动 OCR 文字 / 枝干候选 / 分支判定条件后，必须本地校验正则。**

### 2. OCR 的 `expected`（和 `replace` 的键）是【正则】，不是普通字符串

引擎用 `boost::wregex` 编译。`?`、`*`、`+`、`(`、`[` 单独出现都会让整包加载失败。
编辑器侧已加同口径检查 `_check_ocr_regex()`（覆盖 ocr_click 的 text、branch 的 ocr_text、
枝干候选、replace 键）——**别绕过它**，也别在文案上说"填普通文字就行"。

### 3. `#N` 是节点的【固定编号 num】，不是链序下标

编号在建节点时分配、之后永不变（pipeline 名后缀也是它）；链序随时会变。
**下拉/日志里出现的 `#N` 一律用 `_node_ref_id()` 反查节点**，绝不能拿来当链上位置
（历史上「选 #10 却连到 11」就是这个：`chain[int(n)-1]`）。

### 4. 【分支】出口只认显式连线，没有"自动走链上下一个"

- 命中/未中不连线 = 到此结束（`_emit_branch` 不再回退到链上后继）
- 老流程（schemaVersion < 3）里那条隐式边，由 `normalize_flow` 按**当时的生成结果**补成显式连线，
  所以老流程生成物一字不变（golden 基线可证）；`save_flow` 盖章 `schemaVersion = 3` 后就只认显式连线
- `cut = True`（节点的 `props` 同级字段）= **此处断开**：不接下一个。
  断开链上中间节点时脚本会给它的**前一个节点**打这个标记 —— 否则前后两个节点会自动挨上，
  等于替你连了一条没画过的线
- 画布上链上那条"直落箭头"对分支是不存在的边 → 画成 `⛔ 分支无直落出口`

### 5. 离链节点：`input` 与"未接入"的普通节点

- `input`（【输入】）**设计上就离链**：它是参数声明，不占生成节点、不参与链序，
  靠卡片右侧「注入」小球（紫色虚线）注入到目标节点；目标存在 `props.targets` 列表里（可多个）
- 其它类型的节点离链 = **没接进流程**：不生成、不执行，画布上画 `⛔ 未接入流程（不执行）`，
  校验给 `NODE_OFFCHAIN` 警告。**别把它当流程的一部分**
- 拖线连到未接入节点时，`_ensure_in_chain()` 会顺手把它接进链（否则那条线等于白连，校验还报引用不存在）
- ★ **出口指着谁，谁就在流程里**：被 `hit_next/miss_next/候选/body_end` 指着、却还没在链上的节点，
  `normalize_flow()` 读文件时会自动把它接进链（紧跟指着它的那个节点之后）；反过来，
  「（不接入流程：断开）」一个还被出口指着的节点会被**拒绝**并说明原因。
  这样就不会再出现"出口指着不存在的节点 → 引擎拒绝整包"那种状态（`EXIT_OFFCHAIN` 只作兜底）

### 6. 老流程不要用编辑器同步

`刷冬谷币 / 装卸装备 / 升好感度 / 查找器者 / 征集` 这批是**手写管线**
（`cdzb.json` 等）+ **手写 `interface.json` 参数**（`目标角色`、`礼物次数`…）。
它们的 override 指向老节点名（`升2_Hit`、`CDZB2_Hit`、`升好_礼物`）。
一旦用编辑器同步，入口会被切成 `VF_<流程名>`，生成出来的节点名是 `VF_xx_01…`，
**手写参数会静默失效**（能填、不起作用）。要迁就整体重建 + 用【输入】节点重新指向生成名。

**迁移工具**（`tools/migrate_legacy_flows.py`，2026-09-15 加）：5 个流程都已重建完，
迁移 = 切入口 + 把参数里的老节点名换成生成名。默认干跑，`--apply` 才写文件。它自己跑 7 条验收
（入口真实存在 / 参数每个名字都落在生成物里 / `flow_input_options == interface.json` 那一项 /
同名参数跨流程一致 / 无老名残留 / 模拟同步幂等 / 整包引用完整性），再把改动应用到任务包**副本**
上让本机引擎加载一次（`loaded` 必须 True）。★ 落盘前必须**先关掉编辑器**（脚本自己会拦）。

迁移时有三个坑（脚本已处理，人工重做时别漏）：- ★ **`next` 的「值」也是节点名**：`刷谷_调次数.next = ["刷谷段2"]`、`征集段2.next = ["ZJ_DuiGou2"]`
  里的值同样要换 —— 引用了不存在的节点，引擎会拒绝**整包**（铁律 #1）。老管线里的纯跳转节点
  （`刷谷段2` 只有 next → `ConfirmBattle`）顺着穿透到有对应流程节点的那个名字。
- ★ **老「判定+点击」一体节点被拆成两处**：`CDZB2_Hit` / `升2_Hit` / `查2_Hit` 在编辑器里是
  「判定分支 + 点击节点」，**两处各存了一份模板图**。只改一个 → 换角色后判定不到（一直滑屏找），
  或判定到了却点不下去（点击节点还在找旧模板）。一个老名要写成**两个**新名：
  `VF_装卸装备_11_Hit`（判定）+ `VF_装卸装备_02`（点击）。
- ★ **生成物与老管线重名**：`vf_征集.json` 的通道节点 `征集段2` 与 `zhengji.json` 的原节点同名，
  同一个 bundle 里谁生效不确定 → 老 `zhengji.json` 挪到 `whmx/_retired/`（它只被「征集」用，
  迁完就是死文件；`cdzb.json`/`grind.json` 还被装备分解/领取奖励/外勤/启动用着，不能挪）。
  手机侧编辑器同步**不会删文件**，要么清一次：
  `adb -s 2c92e197 shell "run-as com.maawh.app rm -f files/taskpacks/whmx/pipeline/zhengji.json"`，
  要么重编译重装（App 会按 `version.txt` 整包重放 `whmx/`）。

### 6.1 重建流程必须「边齐全」，否则任务是**成功但什么都不做**（2026-09-15 实跑抓到的）

`刷冬谷币 / 征集 / 装卸装备 / 升好感度 / 查找器者` 这 5 个流程当时是照生成物反推重建的，
**链上的线性 `next` 一个都没写回来**。v4 语义下「缺 `next` = 到此结束」，于是每个任务跑完
**第一个节点**就 `task end [ret=true]`、`Tasker.Task.Succeeded` —— 不报错、不失败，看着像"跑完了"。
引擎**加载**照样 `loaded=True`（缺 next 不是合法性问题），所以**只有实跑或结构比对能发现**：
★ 「本机引擎 `loaded=True`」是必要条件，不是充分条件，别拿它当"流程没问题"的证据。
用 `tools/fix_flow_edges.py --apply` 修（它对老管线做逐节点**有序后继**比对，认得出折叠/合并/流末
这些等价形态，修完自己再审一遍要求归零）。修完顺手查了两条，以后重建流程也该照做：
- **从入口可达**：生成物里不该有"从入口走不到"的节点（第一轮修完 5 个流程时还剩 2~20 个死节点，
  就是入口接错/边缺失暴露出来的）。同一次全量排查发现**另外 5 个流程也同病**，已一起修：
  外勤（20 个不可达）、装备分解/领取奖励（各 15 个）、行会签到（2 个）、`启动`（**连生成都过不了**：
  枝干候选连了公共节点，`vf_启动.json` 一直是缺的，现在补上了）。这些任务**还指着老管线**，
  所以当时手机上不炸，但一迁移就会重演。唯一留着没修的是 `派遣公司事务` 的 1 个不可达节点
  （`VF_派遣公司事务_40`：末尾一个多余的【收口】节点 → `Common_回主页`）—— 那个流程是**手工内联的
  完整版**（比老管线 pqgs.json 多），按老管线对齐会拆坏它，孤儿节点本身也无害，所以保留。
- **入口对得上老管线**：生成物的入口取「链首」，而老管线的入口往往先过一个**门槛**节点
  （等页面出现 / 超时兜底）。链首被排成门槛的子节点时，门槛就成死节点，那一步的 timeout 与
  `on_error` 兜底全丢（装卸装备/升好感度/查找器者 都栽在这）。

★ **重装 App 不会更新手机上的 `vf_*.json`**：`TaskPack.ensureBundledTaskpack` 整包重放时会先把
手机上的 `pipeline/vf_*.json` 备份起来、放完再盖回去（"编辑器同步的优先"）。所以改过 `vf_*.json`
之后装了包，手机上的还是**旧的那一份** —— 要么先删手机上的那几个文件再装，要么直接 adb 直推：
```
adb push <ascii临时名> /data/local/tmp/ && adb shell "run-as com.maawh.app sh -c 'cp /data/local/tmp/<临时名> files/taskpacks/whmx/pipeline/<真名>'"
```
（中文文件名在 host 侧容易 `cannot stat`，先复制成 ASCII 临时名再推。）

---

## 三、数据格式与生成

### flow 文件（`flows/*.flow.json`）

```
{ "schemaVersion": 3, "name": "易物所购买",
  "chain": ["n0014b12", ...],                  // 主链顺序（线性节点靠它决定 next）
  "nodes": { "n0014b12": { "type": "ocr_click", "x":…, "y":…, "title":…,
                           "num": 1, "props": {…},
                           // 分支专有：
                           "hit_next":…, "miss_next":… } } }
```

- `num` = 固定编号（pipeline 名后缀）；`title` 只是显示
- `props` 里有 `cut: true` 表示"此处断开"
- **`next` 是唯一的边来源**（v4 起）：`"next": "<节点id>"` = 接到那个节点，`"next": null` = **到此结束**，
  **缺这个键也是"到此结束"**。★ 只有【老文件】（schemaVersion < 4）才回退成"链上后继" ——
  这个回退以前是无条件的，于是"没写 `next` 的链上节点"会悄悄连到链序里的下一个：
  真实事故 2026-09-15「新建一个节点，画布上总莫名多出一根连到 #22 的线」——
  新建时若没选中任何节点，节点先放"未接入区"且**不写 `next`**；后来它被接进链，
  链尾正好是三个滑动节点（#22/#17/#12），于是它的"链上后继"= #22，
  **生成物里也真多一条 `VF_博物研学_36 → VF_博物研学_22` 的边**（跑起来真的会跳过去，不只是画面问题）。
  修法：`linear_successor()` 里把回退按 `schemaVersion < 4` 门控（老文件的迁移照旧由
  `normalize_flow` 的 `_materialize_next()` 完成）；selftest 有两条断言钉住（v4 不回退 + 老文件回退）
- `schemaVersion`：语义版本。当前 4 = 边是 `nd["next"]`、链序只管显示顺序（3 = 分支出口只认显式连线）。
  **改流程语义就往 `SCHEMA_VERSION` +1**，迁移写在 `normalize_flow()`（读）里，版本号在 `save_flow()`（写）里盖章

### 新增一种节点类型要动的地方（清单）

1. `NODE_TYPES`（图标/颜色/`fields`/`defaults`）+ `TYPE_ORDER`（节点库顺序）
2. `docs/schema/flow.schema.json` 的 `type` 枚举（**漏了会让 check_schema 红**）
3. `_emit_one()` 里挂发射函数（产出 pipeline 节点）
4. `collect_issues()` 里的类型校验（`_check_*` 家族）
5. 属性面板控件：`_make_var` / `_make_widget` / `_prop_changed` 三处都要认识新 kind
6. `selftest()` 里补断言
7. **如果新类型是"参数声明"（不占链序、不生成节点）**，还要像 `input`/`pick` 那样处理这 10 处：
   `_pull_exit_targets_into_chain`（别把它拽进链）、`normalize_flow` 的链清理、
   `_emit_one`（返回 None）、`_param_col_pos`、`_check_offchain`（离链不算错）、
   ⛔ 未接入徽标、`_has_next_ball`（没有"下一个"）、两处「下一个」下拉候选、`tidy_layout` 的两列

### 参数（【输入】/【选择】节点）→ `interface.json`

形状是 ProjectInterface v2：顶层 `option` 对象 + 任务的 `option` 名字列表。

```json
// 【输入】节点 → type=input：整串 {变量} 按 pipeline_type 转类型，混在文本里只做替换
"目标角色": { "type": "input", "label": "目标角色",
  "inputs": [{"name":"角色","label":"角色名","default":"蛙锣","pipeline_type":"string"}],
  "pipeline_override": {"VF_xx_03_Hit": {"template": "{角色}.png"}} }

// 【选择(下拉)】节点 → type=select：每个选项覆盖不同字段/节点（征集次数就是这么写的）
"征集次数": { "type": "select", "label": "征集次数", "default_case": "1",
  "cases": [ {"name":"1", "pipeline_override": {"征集段2":  {"next": ["ZJ_DuiGou2"]}}},
             {"name":"2", "pipeline_override": {"ZJ_JiaHao2": {"repeat": 1}}} ] }
```

- App 侧 `TaskPack.kt` 的 `buildOverride`：**整串就是一个 `{变量}`** → 按 `pipeline_type`
  转 int/bool；**混在文本里**（`{角色}.png`）→ 只做字符串替换
- 识别类字段（template/expected/threshold/roi）作用在【分支】上时，override 自动落到它的 `*_Hit`
- 同名参数会自动**合并**（一个参数注入多个节点）；校验对"同名但定义不一致"给警告。
  **`select` 不合并**（cases 是整份替换语义）；同名的两个【选择】会以后者为准
- 目标节点名用 `jname()`（与生成同源）；`flow_input_options()` 产出的定义与手写的逐键同形状
- ★ **`raw`＝手写管线的节点名**（【输入】节点的"手写管线的节点名"字段，逗号分隔）：
  老流程（`cdzb.json`/`zhengji.json`/`grind.json` 那批）的参数作用在**编辑器流程之外的节点**上
  （`查2_Hit`、`升好_礼物`、`征集段2`…）。写在这里的名字会**原样**进 `pipeline_override` 的键 ——
  这样"编辑器里看到的参数"和"手机上真正生效的参数"是同一个，不会一边改一边静默失效。
  【选择】的选项里"目标节点"那一格同样支持写这种名字
- ★ **一条硬验收**：`flow_input_options(流程) == whmx/interface.json 里那一项`（逐字段）。
  2026-09-15 就是用它把 5 个老流程的参数（征集次数/刷冬谷币次数/目标角色/礼物次数）
  补成节点的 —— `flow_input_options()` 对不上就说明画布上缺东西或指向变了

### 「同步到手机」做的四件事（`on_sync`）

1. `write_pipeline_json()` → `flows/build/vf_<名>.json`
2. `sync_pipeline_file()` → 推 `files/taskpacks/whmx/pipeline/`
3. 复制一份到任务包 `E:\MaaWH\whmx\pipeline\`
4. `register_on_phone()` → 往**手机上的** `interface.json` 注册任务 + 写参数
   （只改手机运行副本，本地 `whmx/interface.json` 不动；改前备份 `.bak`）

---

## 四、门禁与本机验收（改完必跑）

```bash
python flow_editor.py --selftest     # 校验/生成逻辑 + GUI 构建烟测
python tests/check_schema.py         # 17 个流程定义 + 生成物 schema
python tests/check_golden.py         # 每个流程的 build 结果与基线【逐字节一致】
python tests/make_golden.py          # 有意改动后再固化基线（会写 tests/golden/）

# 最强的一条：用本机 MaaFw 引擎真的加载一遍任务包
python -c "from maa.resource import Resource; r=Resource(); j=r.post_bundle(r'E:\MaaWH\whmx'); j.wait(); print(r.loaded)"
```

- 本机 pip 包 `MaaFw` **保持与手机引擎同版本**（当前 `5.12.3`；手机上 `maafw.log` 开头会打
  `Version v5.12.3`）。版本不一致会出现"本机能加载/不能加载"的假信号。
  更老的版本（如 5.2.6）连 `recognition:"Or"` 都解析不了，整包加载走不到正则检查。
- golden 是**回归网**：改生成逻辑后它必须仍然全绿；只有"有意改变生成结果"时才重新固化，
  并在汇报里说明是哪些流程变了、为什么。

---

## 五、改 `flow_editor.py` 的注意事项（Tkinter 的坑，我都踩过）

- **改之前先看编辑器在不在跑**（它会把旧代码留在内存里，你的改动不生效）：
  `powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*flow_editor.py*'} | Select ProcessId"`
  → 跑着就**别改流程文件**（它下次保存会覆盖你），改完提示用户重启
- `pack_forget()` 之后再 `pack()` 会排到**父容器最后** → 要回到原位必须 `after=<参照控件>`（左栏分组就靠这个）
- Tk 回调的**第一个位置参数是 Event** → `def h(var=var)` 会被 Event 顶掉（`x.get()` 直接 AttributeError）。
  正确写法 `def h(_e=None, var=var)`
- `redraw()` 和 `_draw_node()` 的**局部变量不一样**：`tags` 只在 `_draw_node` 里，
  在 redraw 的循环里写 `tags=tags` 会在打开流程时崩（`name 'tags' is not defined`）。用之前先确认作用域
- clam 主题把"已勾选"的指示器画成一个像 ✗ 的图形（语义正好读反）→ 勾选类字段统一用
  `_make_toggle()`（`✓` 绿 = 开 / `✗` 灰 = 关）
- 单选/下拉：链上邻居用「连接」下拉，节点引用用 `_node_ref_id()`，参数注入用画布小球
- **画布平移**：**空白处按住左键拖 = 平移**（2026-09-15 加），中键拖动与 Ctrl+滚轮缩放照旧。
  实现是 `_pan_left` 标记 + `scan_mark/scan_dragto`，**`on_down`/`on_motion`/`on_up` 三处都要认它**
  （漏一处就会"拖动了但松手后光标收不回"或"拖动时误改流程数据"）；`on_escape` 也要能停下它。
  点节点/点球仍是选中/拖动/连线 —— `on_down` 里先 `_hit_test`，**只有命中为空**才走平移。
- **画布上的"小球"= 拖线端口**（2026-09-15 加了 `next`：普通节点下沿正中的「下一个」球）。
  加/改一个端口要动四处，缺一处就是"看着能拖、拖了没反应"：
  ① `_port_pos(nd, port)` 坐标；② `_ball_sides(nid)` —— 带球的那条边不参与其它线的接入点挑选
  （否则别的线会贴着球穿过去）；③ `_draw_node` 里画球，**tags 必须是
  `("port", f"port:{nid}:{port}")`**（`_hit_test` 就是靠它认出"从哪个球起拖"的）；
  ④ `_set_wire(src, port, tgt)` 决定写回哪个字段（注意它按**类型**分派：switch→candidates/miss_next、
  loop→`props.body_end`，所以 loop 的 `next` 端口要单独放行，不然会误写 body_end）。
  再补 `PORT_LABELS`（日志里的中文名）。落地逻辑在 `on_up()`：拖到节点 → `_link_exit()` 保证两端
  都在链上 + `_set_wire(..., tgt)`；拖到空白 → `_set_wire(..., None)`（对 `next` 就是"到此结束"，
  顺手 `_clear_cut()`）。
  ★ **只给真有"下一个"的类型画**（`_has_next_ball()`）：【输入】靠注入线、【分支】走 ✓/✗、
  【枝干判定】走候选/✗、【公共节点(收口)】进入即终止、枝干内容叶跑完即止 —— 这些画了球，
  用户拖完会以为接上了，而生成时那条边根本不存在。
- ★ **【选择(下拉)】也有「注入」球**（2026-09-16 加）：卡片右侧中间、紫球 + "注入"标签，
  与【输入】**完全同一套**（同一个 `port:<nid>:inject` 端口、同一条 `INJECT_COLOR` 虚线、
  同一份数据 `props.targets`，取用函数 `inject_targets_of()` —— `input_targets()` 现在是它的别名）。
  线只表示"这个下拉作用在哪些节点上"（= override 的键），与链序无关。拖到已经连着的节点 = 取消。
  ★ 拉了几根线，一组选项就对几个节点生效 —— **目标节点不写在选项里**（这是用户 2026-09-16 定的：
  "通过注入小球连接其他节点进行参数设置，没必要额外加个「目标节点」"）。
  ★ 老流程（征集次数）把目标**写死在单个选项里**（`row["node"] = "征集段2"`），那种写法继续支持：
  `pick_cases()` 里**行内写的优先于注入线**，`case_brief()` 显示成 `1 @征集段2: next = [...]`。
  两代写法混在一起时，`_check_picks()` 会给 PK_MIX 提醒"这一项以行内写的为准"；面板上还会多出
  一个「⇢ 目标改用注入线」按钮（只在真有写死目标时才出现），一键把它们清成跟注入线走。
  目标解析统一走 `resolve_param_target(flow, token)`（画布 id / `#编号` / 生成名 / 节点标题）——
  **画布画线（redraw）、tidy_layout、生成口径必须共用它**，否则会出现"看着连着、生成却对不上"。
- ★ **【选择】面板是"草稿 + 提交"**（2026-09-16 按用户要求改）：上面三格
  （选项名 / 字段 / 值）是**正在填的那一项**，点「＋ 加选项」才成为列表里的一项；
  点列表里已有的一行 = 把它载进三格改，改完点到别处自动写回（`_apply`）。
  「选项名」和「值」都必填才让加（`_add` 拦）：缺名字的那项不会进 interface.json，
  空值会写出 `{"repeat": ""}` 这种类型不对的覆盖 —— 引擎校验不过会拒绝**整包**（铁律 #1）。
  「值」是**可编辑下拉**，候选随【字段】变 —— 模板图 → 模板文件列表（`list_templates()`）、
  下一个出口 → 画布节点、其余（OCR 文字 / 数字 / 阈值 / 延时）不限制，输入框一直能手填；
  点开下拉前现刷（`refresh_choices()`），才认得上刚框选出来的模板；下面那行小字
  `pick_value_hint()` 说明这一格该填什么。「字段」下拉是全量 `PICK_FIELDS`
  （曾试过只留模板图/OCR文字/命中次数三种，用户要求先不改）。
  ★ 列表刷新 `refresh()` 必须**保住选中行**：`lb.delete(0,"end")` 会清 selection，而
  `_apply/_fill` 全靠"当前选中哪一行"，一丢就变成"改了字段/值却没写回去"（得重新点那行）。
  ★ `_apply()` 只写回**真的被改过**的格（`filled` 记着填进去的原文），而且不碰 `node` ——
  点进点出把用户文件改写的事不能再发生。`set_cells()` 是程序性写格（会同步 `filled`），
  「＋ 加选项」提交后靠它清空草稿，否则刚清空的格子会在失焦时把那一行也清掉。
  ★ 写回的目标行是 `editing`（"三格现在装的是哪一行"），**不是** `lb.curselection()` ——
  点另一行时 `<<ListboxSelect>>` 到达那一刻 curselection 已经是新行，用它会把内容写到错行。
  所以 `<<ListboxSelect>>` 必须绑 `_on_select`（**先 `_apply()` 存旧的、再 `_fill()` 载入新的**）：
  直接绑 `_fill` 就是"改完一个选项、直接点下一个，刚才的修改白做"（2026-09-16 用户报的）。
  草稿状态（还没点任何选项行）时 `editing` 是 None，三格改动只通过「＋ 加选项」提交。
  selftest 有一条断言专门钉这个（改第 1 行 → 直接点第 2 行 → 第 1 行必须已改）。
  ★ 另有显式的「✔ 确认修改」按钮（在「－ 删选中」右边，`_commit_click`）：点了就把三格写回
  当前那一项并给回执 —— "已保存到选项「X」"／"没有改动"／"现在没有选中的选项（三格是草稿区）"。
  自动保存的触发点再多（失焦 / 切行 / 面板重建前），用户也需要一个"我按了就一定进去"的入口
  （2026-09-16 用户点名要的）。**改完记得重启编辑器才生效** —— 它是从源码起的进程。
  ★ **面板重建前要先提交**：`build_prop_panel()` 开头会调 `self._panel_commit`（选项编辑器在
  建面板时注册 `_apply`）—— Tk **销毁控件不会补发 `<FocusOut>`**，所以"改完选项名直接点画布上
  别的节点/空白"这条路上，光靠失焦保存会把用户刚敲的字丢掉（同一天用户报的第二次）。
  配套两点：① 这块面板的 `_rows()` **不能读 `self.sel`**，要捕获建面板时那个节点 id
  （`owner`）—— "点空白"是**先** `self.sel = None`、**后** `build_prop_panel()`，
  钩子跑的时候 sel 已经没了；② 提交钩子取用后立刻置 None（旧面板销毁后不能再被调到）。
  ★ 三格排成两行（选项名/字段并排、「值」占满一行）：面板宽只有 ~322px，格子多了会被裁，
  连下拉箭头都看不见。`_cell_key` 属性是给自测/脚本定位用的。
- ★ **连线长度几乎全由【摆放】决定，不是走线算法**（2026-09-15 实测）：把 `create_line`
  拦下来量折线长度，「绕路比 = 走线长 / 首尾直线距离」**没有一条超过 1.6** —— 所以看到
  「线太长」先查节点坐标，别急着改 `_route()`。量化手法见下一节（`_tmp/` 里跑完即删）。
  当天改的三处（都为了"不与节点重合、不过分拉长"）：
  - **`tidy_layout()` 多了两列**：① 回环节点（`_loop_target()` 非空 = 出口全部指回链序上方的
    **纯**回环节点，如【滑动】）挪到**主列右侧一列、y 贴着它的目标**；② 离链【输入】按
    「第一个目标」的 y 对齐排参数列。结果：博物研学 21 节点，连线总长 14622→6297px、
    最长 1638→764px（输入节点原来全堆在参数列顶部，注入线要跨 1300~1600px）。
    ★ 只能挪**纯**回环节点：分支（命中往下、未中往上）挪过去之后 ✓/✗ 端口朝右、出口却指回
    左侧主列，走线得绕过整张卡片（派遣公司事务实测：挪了 2 个分支，总长 4141→17749px）。
    ★ `_chain_max_x()` 必须**排除回环节点**，否则回环列一算完就把自己算进基准里，
    参数列被推出去 300+px。
  - **显式 `next` 的边全部要画**：v4 起边是 `nd["next"]`，而画布只画"链序紧跟着的那个"
    （`linear_successor(cur) == ch[i+1]`）→ 指回上方的回环边**一条都没画**，画布上是断头路。
    现在补了一段：`next` 存在、且不等于链序下一项、且出口没被 `_suppress_reason` 抑制
    （分支/枝干另有画法）→ 从底边中点画一条普通箭头。
  - **`_draw_cut_off()` 的 ⛔ 标记不许伸进下一张卡片**：步长 92、卡高 60 时两张卡之间只有
    32px，而标记（竖桩 + 横杠 + 22px 高的徽标）会压到下一张卡顶边上。现在把 `tip` 夹在
    两张卡之间（`min(y_from+2+stub, y_to-12)`，再 `max(..., y_from-12)`）。
  - **`_route_top()` 的 ② 多试一条「源所在的那条横道」**（`y = sy`）：以前只从"中间/目标上方"
    挑横道，"先沿源那行走、再直插目标"这个只有一横一竖的最优形状压根不试 ——
    同一条注入线 1668px vs 最优 764px。
- ★ **新建节点必须 `copy.deepcopy(spec["defaults"])`，不能 `dict(...)`**（2026-09-16 修 +
  selftest 断言）。defaults 里的 `targets` / `candidates` / `cases` 是 **list**，浅拷贝会让
  **所有同类型节点共用同一个 list** —— 往一个节点 append（拉注入线、加候选、加选项）等于改了
  `NODE_TYPES` 的"出厂默认值"，之后每个新建的节点一出生就带着上一个节点连过的节点/候选。
  真实事故：用户删掉一个【选择】、再新建一个，它**自动连回之前连过的那几个节点**（"删除了
  还是自动连接"）—— 诱因就是给 pick 的 defaults 加了 `targets: []`（`cases`/`candidates`
  一直有同样的隐患）。★ 当前跑着的编辑器进程里默认值可能已被写脏，**重启后才干净**。
- ★ **删节点要把引用清干净**（`_delete_nodes`）：`next`/`hit_next`/`miss_next`/枝干候选/
  `body_end` 之外，还有 **参数注入目标 `props.targets`（【输入】和【选择】共用一份数据）**、
  以及**【选择】选项里写死的 `node`**。以前只清 `input` 那一份 —— 删掉被注入的目标节点后
  【选择】那边还挂着死 id（摘要照旧显示"注入 N 个节点"，生成时往不存在的名字上写覆盖）。
  另外 `normalize_flow()` 读文件时**兜底**再清一次 `targets` 里的死 id（老文件/早先写脏的
  数据里仍有残留；画布上那种 id 没有球可拖、面板里也没有清它的入口）。
- ★ **主窗口默认最大化打开，初始尺寸按【工作区】夹**（2026-09-16）：`maximize_window()`
  用 `state("zoomed")` 最大化（Windows 自动避开任务栏），这是用户定的默认打开方式。
  兜底链条：写死 `geometry("1640x960")` 在高缩放屏上（1920x1080 @125% → 逻辑 1536x864）
  比整个屏幕还大，Windows 会把窗口压满全屏、状态栏被任务栏挡住 —— 所以 `initial_geometry()`
  用 Win32 `SPI_GETWORKAREA` 把"还原后的尺寸"夹进工作区；再靠 `_fit_window_to_workarea()`
  量真实底边（WM 摆放会往下偏，实测 +40），超出就收高/收宽；从最大化「还原」时
  （`<Configure>` 里看到 zoomed→normal）再量一次。
- ★ **工具栏「✖ 删除」右边有「删除免确认」开关**（2026-09-16 加）：开着时点删除或按 Delete
  直接删，不弹 `askyesno`（删错由 `_snapshot()` + Ctrl+Z 兜底）；状态记在 `ui_prefs.json`
  （与 `recent.txt` 同类的本机状态文件，已进 .gitignore）。`load_prefs()` / `save_prefs()`
  只认 `DEFAULT_PREFS` 里登记过的键，读失败一律回默认值（这文件坏了不该让编辑器起不来）。
  开关对所有删除入口生效（都走 `delete_selected()`）。selftest 会验证它，**收尾必须把偏好还原**。
- ★ **`_loaded_path` 只在 `open_flow_file()` / `on_save()` 里赋值，`on_new()` 必须把它清成 None**
  （2026-09-15 已修 + selftest 断言）。它决定 `on_save()` 要不要删「改名前的旧文件」：
  残留着上一次打开的文件时，**新建 → 改名 → 保存会顺手删掉上次打开的那个流程文件**。
  真实事故：打开 `博物研学.flow.json` → 新建 → 改名 `刷活动关` 保存 ⇒ `博物研学.flow.json` 被删，
  编辑器列表（`list_flows()` 扫 `flows/*.flow.json`）里就找不到了。
  找回办法：`flows/build/vf_<名>.json` 是最后同步的生成物，用反导器重建 ——
  注意两件事：① 反导器把 pipeline 名写进了节点 id（`n_VF_x_10`），**用它把 `num` 钉回去**，
  否则重开时会按链序重排编号、生成物里的节点名全变（`interface.json` 的 override 会静默失效）；
  ② v4 的 `next` 是真实边，必须按旧生成物显式写回（缺 key 会回退成「链上后继」）；
  ③ 重建后**用 `build_pipeline()` 和旧生成物逐节点对比，0 差异才算还原成功**
- 改完**必须跑 `--selftest`**；行为变化要补断言（现在的 selftest 覆盖：枝干级联与候选条件来源、
  输入节点与多目标注入、离链/断开语义、布局重叠与卡片高度、OCR 正则校验、
  新建流程不牵连旧文件、布局与回环走线、「下一个」拖线小球、空白处拖动平移、
  清单同步幂等（`upsert_flow_task` 不会把【小工具】分组的任务删掉再追加到清单末尾）

### ★ 流程文件丢了 / 被覆盖了，怎么找回（2026-09-15 完整实操过一遍）

**事故本身**：恢复文件时图省事写了 `git checkout -- flows/`（**没列文件**）→ 把工作区里
**所有**未提交的流程改动一起回退了（博物研学 + 喝茶 + 易物所购买 + 派遣公司事务）。
**教训：永远 `git checkout -- <具体文件名>`**，改前先 `git status` 看清有几处在改。

**来源优先级（别跳步）**：
1. **`tests/golden/vf_<名>.json`（基线）** —— 它记着"**某次 golden 是绿的**"那一刻的生成结果。
   判断依据：`check_golden` 在出事故前是绿的 → 那时的流程文件 == 基线 → 基线就是那份。
   同时 `tests/golden/issues_<名>.json` 里有当时的**告警原文**，能捞出节点标题（见下）。
2. `flows/build/vf_<名>.json` 与 `E:\MaaWH\whmx\pipeline\vf_<名>.json`（生成物副本）——
   注意：**生成物可能已被后来的同步/回退重写过**，未必是丢掉的那版（我第一轮就栽在这：
   照生成物重建完发现"字段对不上基线"，其实生成物是回退后的版本）。先跟基线比一遍再用。
3. 手机上的 `files/taskpacks/whmx/pipeline/vf_*.json`（最后一次同步的运行副本）。

**重建步骤（照生成物反推流程）**：
1. `import_pipelines.import_entry(pd, "VF_<名>", "<名>")` 反导 → 得到流程骨架
2. **编号钉回**：反导器把 pipeline 名写进了节点 id（`n_VF_x_10`）→ 用它把 `num` 写回，
   否则重开时会按链序重排编号、生成物里的节点名全变
   （★ 反导器的 OCR 期望文字读的是协议字段 `expected`，早期误读 `text` → 带 OCR 分支的流程
   反推出来是空的、校验报"未选择模板图"；2026-09-15 已修）
3. **显式 `next` 写回**（v4 的边就是 `nd["next"]`，缺 key 会回退成"链上后继"）：
   按生成物的 `next[0]` 逐节点写，**末尾节点要显式写 None**（否则会被接成链上下一个）
4. **动作参数要补齐**：反导器只搬 `defaults`，`repeat`/`repeat_delay`/`pre_delay`/`duration`/
   `post_delay`/`max_hit`/`post_wait_freezes` 都得按生成物搬回来（喝茶就栽在这：
   只差这几个字段，"看起来像丢了结构"）
5. **只指向 `Common_*` 的节点**（`{next:[Common_回主页]}`）反导器会跳过 → 补成【公共节点(收口)】，
   并**最后再写一遍 `next`**（补 common 时那些目标还不存在，会写成"到此结束"）
6. **含【枝干判定】的流程**要按 `_Jk` 结构重建：候选 `timeout` 取自 `_Jk` 容器、
   候选的 `next` = `_Jk_Hit.next[0]` 指向的那个**分支**节点、`miss_next` 取自最后一个容器的 `on_error`
7. **节点标题**：基线告警里带着你起过的名字（`#13「退出办公室按钮还在?」…`）→ 用
   `#(\d+)「([^」]+)」` 捞回来；没出现在告警里的只能用类型默认名（**标题只影响显示与告警文本**）
8. **画布坐标找不回**（生成物里没有 x/y）→ 重排即可（「✚ 整理布局」）
9. **验收**：`build_pipeline()` 与来源**逐节点 0 差异**（含 `next`/`recognition`/阈值/延时），
   再跑 `check_golden` 看**告警文本**是否也一致 —— 链序对不上的话会有"收口节点放在链中间"
   这类外观告警，而链序只影响显示不影响生成，可以据告警原文把顺序调回来（易物所购买就是
   把 `07/08` 换个位置后告警逐字对上的）

**⚠️ 保存流程文件前必须 `normalize_flow()`**：老流程（v1~v3）的"下一个"是靠链序隐含的，
直接把原文件读出来再 `save_flow()`（盖章 v4）会把那些边**全丢掉**（我据此把
刷冬谷币/升好感度 的生成结果改烂过一次，golden 立刻发现）。即：
`flow = fe.normalize_flow(json.load(...))` → 改 → `fe.save_flow(flow)`

---

## 六、程序化验证 GUI 的常用手法（比截图可靠）

```python
root = tk.Tk(); root.deiconify()
ed = fe.FlowEditor(root)
root.update(); time.sleep(0.35); root.update()   # ★ 让 __init__ 里 after(200, 打开最近流程) 先跑掉
# 断言"画布上画出来了什么"：读图元，不靠眼睛
texts = [ed.canvas.itemcget(i, "text") for i in ed.canvas.find_all()
         if ed.canvas.type(i) == "text"]
lines = [ed.canvas.coords(i) for i in ed.canvas.find_all()
         if ed.canvas.type(i) == "line" and ed.canvas.itemcget(i, "fill") == fe.THEME["ok"]]
# 模拟交互
btn.invoke()                       # 按钮
label.event_generate("<Button-1>") # 鼠标事件
ed.wire = {"from": "b", "port": "hit_next", "mx": x, "my": y}; ed.on_up(None)  # 拖线落点
```

**量连线（判断"线太长/在绕路"，2026-09-15 用过一次）**：把 `create_line` 换成记账版，
`redraw()` 后拿每条折线的长度和首尾直线距离比：

```python
lines = []
real = ed.canvas.create_line
def spy(*a, **k):
    if k.get("fill") == fe.THEME["grid"]:   # 画布网格不算连线
        return real(*a, **k)
    it = real(*a, **k); lines.append((k.get("fill"), list(ed.canvas.coords(it)))); return it
ed.canvas.create_line = spy
ed.redraw()
# 绕路比 = 折线长 / 首尾直线距离；>1.6 才说明走线有问题，否则去查节点坐标
# 压卡片：折线段采样后逐个判是否落在别的卡片矩形内（端点所在的两张卡不算）
```

★ 两个坑：① 每次 `redraw()` 会把同一条边画多次（高亮/选中重画），统计前按
`(颜色, 折点元组)` 去重；② 量之前先 `root.update()` 把 `__init__` 里
`after(200, 打开最近流程)` 放掉，否则它会把 `open_flow_file()` 打开的流程换掉。

临时脚本放 `_tmp/`（**跑完删掉**，别留在仓库里）。

---

## 七、手机侧排障（为什么任务失败，第一手证据在引擎日志）

```bash
ADB=D:/android-studio/Sdk/platform-tools/adb.exe
$ADB -s 2c92e197 shell "run-as com.maawh.app cat files/maa_logs/maafw.log"   # 引擎日志
$ADB -s 2c92e197 shell "run-as com.maawh.app ls -la files/taskpacks/whmx/pipeline/"
```

- 日志开头 `Version v5.12.3` = 手机引擎版本
- `Resource.Loading.Succeeded / Failed` = 任务包加载结果；Failed 前面几行一定写着原因
- `regex invalid [name=…]` / `check_all_validity failed` = 第 2 条铁律
- App 侧对应代码：`E:\MaaWH\app\src\main\java\com\maawh\app\MaaBridge.kt`
  （`资源加载: status=…` 那行之后若失败会直接 `return false`，引擎任务根本没下发）
- 任务每次运行都会**重新加载 bundle** → 改 pipeline 文件不用重启 App；改 `interface.json` 要重启

---

## 八、沟通与交付约定

- 全程**简体中文**（代码/标识符/路径保持原样）
- 汇报先给结论，再给证据：**实测输出 / 门禁结果 / 引擎加载结果**，不要只写"已修复"
- 改动尽量小而可验证；遇到"要不要动用户的数据/流程文件"——先确认编辑器没在跑，能不动就不动
- 收尾必说三件事：**改了哪些文件、验证到什么程度、需不需要重启编辑器/重开 App**
- 已知缺口（做之前先和用户确认）：老流程迁移到编辑器管理、折叠状态不持久化、
  帧窗口位置不记忆、参数只写手机副本（用本地 `interface.json` 重建任务包会丢参数）
