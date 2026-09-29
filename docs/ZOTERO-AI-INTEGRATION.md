# 调研：AI 接入 Zotero 的可行性与姿势

> 起因：用户判断"文献阅读器是个很应用端很复杂琐碎的项目，且有许多成熟项目"，希望先总结
> **我们几个 AI 接入项目积累的"接入需要什么"**，再据此调查 Zotero 的开源程度与接入可行性。
>
> 本文件是**调研与决策依据**，不是已拍板的方案。结论见 §3。

---

## 一、先总结：AI 接入到底需要什么

从我们已落地的四个项目提炼（PaperPilot 本仓 · mecha 框架 · ML-toolbox · Energy Level），
**能用的 AI 接入 = 手脚 + 纪律 + 呈现**，三层缺一层就退化成"聊天窗口"。

### 1.1 三条经验

1. **手脚先行，与 AI 接入解耦**（信条 9）：先把能力做成"任何外部调用方都能用"的中性形态，
   再谈接哪个 AI。我们为此把 MCP 从"中心"降为"一层薄 adapter"，两个门面是 **Python API + CLI(`--json`)**。
2. **AI 不是"多一个用户"，是"多一类行为主体"**：它快、并发、会犯错、会自作主张。
   所以接入的重点不在"能不能调"，而在**边界**：能碰什么、动了留什么痕、错了能不能退。
3. **判断归 AI，机械归代码**：AI 负责"选哪句、怎么说"，代码负责"落在哪个字符、
   怎么分页、怎么渲染"。**任何让 AI 手算坐标/偏移的设计都是错的**——我们在精读里刚验证过
   （改前让 AI 给偏移必错；改后 AI 只给原句，后端算区间，一次就对）。

### 1.2 十条判据（下文逐条对照 Zotero）

| # | 判据 | 落到我们代码里的样子 |
| --- | --- | --- |
| L1-1 | **中性能力层**：typed + self-describing（name/描述/入参 schema/结构化返回/read\|write/reversible） | `capabilities/` 能力注册表 + `PARAM_DESCRIPTIONS`（漂移即启动报错） |
| L1-2 | **两个门面**：进程内可直接 import，进程外有 CLI/HTTP | `import paperpilot...` + `paperpilot call <tool> --json` |
| L1-3 | **投影即权限**：给 AI 的工具面是**显式白名单**，不是把库全开 | `mecha_adapter/tools.py::TOOL_DECLS`（不列入者 AI 不可见） |
| L1-4 | **回程能浓缩**：体积闸 + 截断必须给"怎么续读" | 65536 字节闸；`next_offset`；列表键截断 |
| L2-5 | **写权门 + 人在环**：写要确认；域名分级授权 | 写工具先提议；`scopes.py` 的 `_GRANTS` + `default_allow=False` |
| L2-6 | **人类专属能力**：有些事**故意**不给 AI（两道锁：不投影 + 只授 human） | `reset_profile` / `delete_graph_view` / `delete_mark`（`scope=marks` 只给人） |
| L2-7 | **记名 + 原因 + 前后值 + append-only + 可撤销** | `events(actor/reason/op/target/before/after/reversible)` + `undo_change` |
| L2-8 | **可教学错误**：`kind/hint/suggest`，让 AI 第一次错就能改对 | 统一信封 `{ok, error{kind,hint,suggest}}` |
| L2-9 | **判断归 AI、几何归代码** | 精读：AI 给 `quote`，后端算字符区间；图：AI 命名分层，代码算坐标 |
| L3-10 | **人机共享同一份状态，且人看得见 AI 干了什么** | 界面②（Web）+ 记录仪 + 精读页 4 秒增量上屏 |

> 附：**可自证**是 L2-8/L3-10 的延伸——机读回执（`verify_marks` 的命中原文、
> `render_diag` 的计算样式与包围盒）比"让 AI 盯着截图猜"可靠。我们刚吃过一次亏：
> 我目测截图以为"高亮没生效"，机读一查一直是对的。

---

## 二、Zotero 探查

### 2.1 开源程度

| 项 | 事实 |
| --- | --- |
| 客户端授权 | **AGPLv3**。原文（[COPYING](https://raw.githubusercontent.com/zotero/zotero/main/COPYING)）："The Corporation for Digital Scholarship distributes the Zotero source code under the GNU Affero General Public License, version 3 (AGPLv3)"。GitHub API 报 `spdx_id: NOASSERTION`——因为该文件在 AGPL 正文前加了版权与**商标**声明，GitHub 无法自动分类，**不是**"非标准授权" |
| 商标 | "The Zotero name is a registered trademark"（[trademark 政策](http://zotero.org/trademark)）——**改代码可以，叫自己 Zotero 不行** |
| 治理 | 非营利 **Digital Scholar** 主导 + 全球社区；仓库活跃（15.4k star、1605 open issues、2026-09 仍在推提交） |
| 当前版本 | **Zotero 8**（[发布博客](https://www.zotero.org/blog/zotero-8/)、[8.0 changelog](https://www.zotero.org/support/8.0_changelog)） |
| AGPL 对插件的影响 | 社区有专门讨论（[论坛](https://forums.zotero.org/discussion/comment/495195/)）：插件通常被视为**独立作品**，但这是**工程惯例而非法律意见**，商业分发前应自行确认 |

**一句话**：客户端是**真开源**（AGPLv3 全源码 + 活跃社区 + 官方插件体系），代价是**传染性授权 + 商标约束**。

### 2.2 接入面：Zotero 其实已经给了三道门

**① 本地 API（最重要的发现）** —— `http://localhost:23119/api/`
官方文档：[Zotero Local API](https://www.zotero.org/support/dev/web_api/v3/local_api)（2026-07-29 更新）

- 语义 = **官方 Web API v3**，但服务本地数据库：离线可用、无限速、比 Web API 快得多
- **读：免鉴权**（只需在 设置→高级 勾选"允许本机其他程序与 Zotero 通信"；未勾选返回 403）
- **写：Zotero 10+** 才支持，且要**运行时申请本地密钥**：
  `POST /api/local/authorize` → Zotero 弹框（Allow / Always Allow / Deny）→ 返回 key；
  非"Always Allow"的 key **一次性**（首次写入成功即消耗）；5 次/分钟限流防弹框骚扰
- `Zotero-Server-ID` 标识实例（写请求必须带，缺失 428、不匹配 412）
- **本地版本号 + `If-Unmodified-Since-Version`** ⇒ 天然乐观并发
- 本地独有：**能真正执行保存的搜索**（`/searches/<key>/items`）、`/items/<key>/file` **302 到 `file://`**、
  能写**全文内容**（`PUT .../fulltext`）
- 数据面：条目/集合/标签/笔记/**批注**（批注就是 item，`itemType=annotation`）/全文索引

**② 插件（全权限）** —— [Zotero 7 for Developers](https://www.zotero.org/support/dev/zotero_7_for_developers)

- Zotero 7/8 插件 = `manifest.json` + `bootstrap.js`（生命周期钩子 + 窗口钩子）
- **官方明确保留全权限**："Zotero 7 plugins continue to provide full access to platform internals (XPCOM, file access, etc.)… We have no plans to make similar restrictions in Zotero"
  （即：**不像 Firefox 那样被 WebExtensions 沙箱限制**）
- 官方注册点：`Zotero.PreferencePanes.register`、`Zotero.ItemTreeManager.registerColumn`、
  `Zotero.ItemPaneManager.registerSection / registerInfoRow`、阅读器 `Zotero.Reader`
- 有 Firefox 115 devtools（`-jsdebugger`）、示例插件 [make-it-red](https://github.com/zotero/make-it-red)、
  中文社区[插件开发指南](https://zotero-chinese.github.io/plugin-dev-guide/reference/bootstrap.html)

**③ Web API（远程）** —— `api.zotero.org`，需 API key，读写都支持，但要联网 + 同步。

### 2.3 逐条对照我们的十条判据

| 判据 | Zotero 现状 | 判定 |
| --- | --- | --- |
| L1-1 中性能力层 | Local API = 官方 typed/文档化 REST + JSON；schema 版本头 `Zotero-Schema-Version` | ✅ **直接可用** |
| L1-2 两个门面 | 有 HTTP（本地/远程）；无 CLI（要自带） | ✅ 够用 |
| L1-3 投影即权限 | **无此概念**：key 不分区、不看调用方是谁；读接口把整个库摊开 | ❌ **要自己包一层** |
| L1-4 回程浓缩 | 本地 API **默认不分页**（一次返回全部命中）——对 AI 上下文**不友好** | ❌ **要自己加闸** |
| L2-5 写权门 | **厂商内建**：`POST /api/local/authorize` → 人点确认框 → 一次性 key；限流；可一键撤销全部授权 | ✅✅ **与我们的信条 4 天然对齐**（但仅 Zotero 10+） |
| L2-6 人类专属能力 | 无。谁都拦不住一个拿到 key 的程序 | ❌ 要自己造 |
| L2-7 审计 / undo | 只有**版本号**（乐观并发）与同步历史；**没有** append-only 事件流、没有 `before/after`、没有 undo，**也看不到"谁改的"** | ❌ **最大缺口** |
| L2-8 可教学错误 | 标准 HTTP 码（403/412/428/429）；无 `kind/hint/suggest` | ⚠️ 要自己映射 |
| L2-9 几何归代码 | **反例**：批注模型是 `{pageIndex, rects:[[x1,y1,x2,y2]], sortIndex, pageLabel}`——**坐标**。已有 MCP 插件正是要求 AI 自己给 `rects` | ❌ **要害**：必须自己写"原文→坐标"解析层 |
| L3-10 人机共享 + 看得见 | 现成阅读器极强（PDF 渲染、highlight/underline/note/ink/image 批注、侧栏、标签、集合、同步、Word 集成）；但**没有"AI 操作可见"的语义**，也无推送（要自己轮询/广播） | ✅ 呈现面白送 / ⚠️ 纪律面要自己补 |

### 2.4 已有 AI 接入先例（说明"能接"，也说明"缺纪律"）

社区已经有不少 Zotero × AI/MCP 项目：

- [zotero-mcp](https://pypi.org/project/zotero-mcp/)、[zotero-mcp-lite](https://pypi.org/project/zotero-mcp-lite/)、
  [zotero-mcp-server](https://pypi.org/project/zotero-mcp-server/)、[zotero-native-mcp](https://www.npmjs.com/package/zotero-native-mcp)
- [stephenstubbs/zotero-mcp](https://github.com/stephenstubbs/zotero-mcp)：**装在 Zotero 里的插件**，
  在同一个 `23119` 端口上加 `/mcp/...` 端点，可直接**创建批注**（type/text/comment/color/pageLabel/
  `position:{pageIndex, rects}`）、按 key 取条目、搜索、取子项
- 甚至有人给 AI 写了 [zotero-mcp 使用 skill](https://github.com/wentorai/research-plugins/blob/HEAD/skills/writing/citation/zotero-mcp-guide/SKILL.md)

**共性缺口**（对照 §1.2）：这些项目几乎都停在 **L1 手脚层**——能读库、能建批注；
**L2 纪律层基本空白**：无写权门（插件端点甚至不需要鉴权）、无审计/undo、无人类专属能力、
无体积闸、无 `kind/hint/suggest`；且把**坐标**直接暴露给 AI（L2-9 反例）。
官方也有人提过 [希望开放程序化批注创建](https://forums.zotero.org/discussion/comment/517989/)，
说明这块官方接口一直不够顺手。

### 2.5 代码构成与"搬过来魔改"的代价

**语言（GitHub languages 接口的实际字节数，非印象）**

| 仓库 | 构成 |
| --- | --- |
| `zotero/zotero`（客户端） | **JavaScript 10.7 MB**、Fluent 4.7 MB（⚠ 那是本地化 .ftl 文本，不是代码）、C++ 0.5 MB、HTML 0.32 MB、SCSS 0.29 MB、NSIS 0.28 MB（Windows 安装器）、Shell 0.20 MB、TypeScript 0.20 MB、Python 86 KB、Perl 54 KB（遗留）、C 28 KB、XSLT 20 KB、Java 14 KB… 合计约 **18.6 MB 源码** |
| `zotero/reader`（PDF/EPUB 阅读器） | JS 1.17 MB + TS 0.51 MB + SCSS 86 KB ≈ **1.8 MB**（很小） |
| `zotero/translators`（网页抓取器） | **JS 10.1 MB**（每站点一个文件：量大在数量，不在复杂度） |

要点：
- **Zotero 绝大部分是 JavaScript**（客户端里 JS ≈ 58%；算上本地化文本 ≈ 83%）。
- **那"很重的 C++/Rust"是 Mozilla/Firefox 平台，不是 Zotero 的代码**：你不是编译浏览器，
  而是把 Zotero 的 JS/XUL 装到**预编译的 Firefox ESR** 上（standalone build）。
- **阅读器本体很小（1.8 MB）**；Zotero 的体积在"文献管理器"（条目模型/同步/引用），不在阅读。

**"搬过来魔改"的三种深度**

| 深度 | 做法 | 代价 | 授权 |
| --- | --- | --- | --- |
| **不搬（插件）** | 全 XPCOM 权限 + 官方注册点 + 可注入 DOM/加面板/挂阅读器/在 23119 上加端点 | 低。每个大版本跟一次（7→8 强制所有插件改写，但那是**一次性改写**，不是持续分叉） | 自己那部分可另定授权（社区惯例，**需法务确认**） |
| **轻度 fork** | 改客户端细节、自己发版 | 中高：接手**构建链 + DB schema + 同步协议兼容 + 更新器**，且每次 Firefox ESR / Zotero 大版本升级都要 rebase | 分发即触发 **AGPL 义务**（提供对应源码）+ 商标限制 |
| **深度接管** | 换 UI、把 AI 当一等公民 | 高且**持续**：官方自己为 FF115 做过 "massive rewrite"；你得长期跟 | 同上 |

**现实证据（说明构建不是 `npm install` 的量级）**：官方有
[构建文档](https://www.zotero.org/support/dev/client_coding/building_the_desktop_app)（含
[Windows 注意事项](https://www.zotero.org/support/dev/client_coding/building_the_desktop_app_windows_notes)），
但 Debian 长期只到 RFP/ITP，清华 OSPP 曾把"Zotero 6 的可复现构建"当成**研究课题**；
仓库约 236 MB、18 年积累、1605 个未关 issue。

**真正值得"用"而不必"搬"的东西**（关键判据：收益 vs 维护义务）
- **本地 API 调用** —— 零授权牵连（最干净的姿势）
- **pdf.js**（Apache-2.0，宽松）—— Zotero 阅读器的渲染内核其实是 **Mozilla 的**，不是 Zotero 的
  ⇒ **想自己造 PDF 阅读层，起点应是 pdf.js，而不是 fork Zotero（AGPL）**
- `zotero/reader` —— Debian 正在按"**Zotero 的 PDF/EPUB 阅读器模块**"打包（[ITP #1149159](https://lists.debian.org/debian-wnpp/2026/09/msg00878.html)），
  说明它架构上可分离；但仍是 AGPL 系
- `zotero/translators` 与 `citeproc-js` —— 授权**未逐一核实**，要用先单独确认

**结论**：技术上可行，**工程上不建议整体搬**。Zotero 的价值在条目模型/同步/抓取/引用/批注模型，
这些**大多能"用"不必"搬"**；而我们的差异化（AI 纪律层）**根本不需要 fork**。
若确实要深改：插件能拿到全权限，成本比 fork 低一个数量级；
代价是**必须接受在 JS/XUL + Mozilla 惯用法里工作**（与我们的 Python 栈是两套心智）。

---

## 三、可行性结论

### 3.1 判断

**接得进去，而且比自建省力得多**——但省下的是"手脚"，我们真正积累的**"纪律"恰恰是 Zotero 没有的**。

这正好印证信条 9 的分工：**Zotero 承包手脚（库、PDF 渲染、批注渲染、同步、引用），
我们承包纪律（投影、写权门、审计、回执、浓缩回程、人类专属）与判断（读什么、标哪句、说什么）。**

### 3.2 三种接法

| 方案 | 做法 | 今天能做吗 | 代价 |
| --- | --- | --- | --- |
| **A. 纯外挂（推荐先做）** | 我们的能力层读 Local API（免鉴权读）；写暂缓或走 Web API | ✅ **今天就能跑** | 写受限；无本地批注 |
| **B. 插件 + 我们的纪律层** | 写 Zotero 插件补"写/本地锚定/端点"，我们仍管投影与审计 | ✅（插件全权限） | 要跟 Zotero 大版本走（7→8 已要求所有插件重写） |
| **C. 仅数据互通** | 把 Zotero 当**信号源**（已读/收藏/人工批注）与条目同步；阅读仍在我们的 `/read` | ✅ 最省 | 不共享阅读器 |

### 3.3 建议路线

1. **先做 C 的一半（立刻见效）**：把 Zotero 的**人工批注**当作**最强兴趣信号**喂画像与推荐
   ——人自己划的句子比"点开过"可信得多。这正是我们的画像/推荐流体系最缺的高质量标签。
2. **再做 A 的读通道**：`zotero_*` 能力（查条目/取批注/取全文/按保存搜索）接进中性能力层 +
   投影白名单，AI 就能"顺藤摸瓜"。**注意加体积闸与 `kind/hint/suggest` 包装**（Zotero 不提供）。
3. **写通道等 Zotero 10 的本地写 API**：届时它的 `authorize` 弹框与我们的"写权门"天然对齐，
   比现在绕 Web API 干净。**在那之前，写批注要么走插件（B），要么不做**。
4. **绝不自建阅读器**：我们的 `/read`（arXiv HTML）保留为"无 PDF / 需 HTML 锚点"时的补充，
   但 PDF 阅读这件事交给 Zotero。

### 3.4 未核实 / 风险

- **本地 API 写能力的实际可用版本**：官方文档三处标注 "Zotero 10+"，而当前稳定版是 8.x
  ⇒ **今天大概率只能用插件或 Web API 写**。落地前应在装了 Zotero 的机器上实测
  `POST /api/local/authorize`（未支持会 404/405）。
- **AGPL 边界**：仅"调用本地 HTTP API"通常不构成派生作品（最干净的姿势）；
  一旦分发**改过的 Zotero** 或**链接其源码的插件**，就要按 AGPL 提供对应源码，并遵守商标政策。
  **正式分发前请找法务确认**，本文不构成法律意见。
- **插件兼容成本**：Zotero 7→8 已要求**所有插件**改写；把重要功能压在插件上要预留维护预算。
- **坐标锚定**：Zotero 批注靠 `pageIndex + rects`，PDF 一换版就失效（我们的 HTML 方案靠
  `quote + prefix/suffix` 重锚）。若走 B，应把"原文→坐标"做成**代码层解析**（我们已有等价经验）。
- **上下文成本**：Local API 默认不分页、`/fulltext` 能返回整篇正文 ⇒ 直接接给 AI 会**吃爆上下文**，
  必须复用我们的体积闸与"分块读 + 续读游标"。

---

## 四、参考

- [Zotero Local API（官方）](https://www.zotero.org/support/dev/web_api/v3/local_api)
- [Zotero 7 for Developers（插件体系）](https://www.zotero.org/support/dev/zotero_7_for_developers)
- [Zotero 源码授权原文 COPYING（AGPLv3 + 商标）](https://raw.githubusercontent.com/zotero/zotero/main/COPYING)
- [Zotero 8 发布](https://www.zotero.org/blog/zotero-8/) · [8.0 changelog](https://www.zotero.org/support/8.0_changelog)
- [示例插件 make-it-red](https://github.com/zotero/make-it-red) · [中文插件开发指南](https://zotero-chinese.github.io/plugin-dev-guide/reference/bootstrap.html)
- [论坛：插件是否也要 AGPL](https://forums.zotero.org/discussion/comment/495195/) ·
  [论坛：希望开放程序化批注创建](https://forums.zotero.org/discussion/comment/517989/)
- [stephenstubbs/zotero-mcp（插件式 MCP）](https://github.com/stephenstubbs/zotero-mcp) ·
  [zotero-mcp](https://pypi.org/project/zotero-mcp/) · [zotero-mcp-lite](https://pypi.org/project/zotero-mcp-lite/) ·
  [zotero-mcp-server](https://pypi.org/project/zotero-mcp-server/) · [zotero-native-mcp](https://www.npmjs.com/package/zotero-native-mcp)
- [zotero/reader 批注管理](https://deepwiki.com/zotero/reader/4.2-annotation-management)
