# 现存问题：DSH overlay 失效条目 + mecha 实例并存残留

- **审计时间**：2026-10-10
- **范围**：本机 PaperPilot / mecha 部署（工作区 `D:\学\BaiduSyncdisk\编程\code\python\Get Paper`）
- **本文只列仍然存在的问题**；已修复项、排查过程与历史回执一律不收录。

> **当前基线**：框架聚合端点 `mcp__mecha__*` 可连、软件在册——
> `mcp__mecha__query_software` → `GP / aggregate=up / tools=61 / process=ready / port=57063 / holds_write_lease=true`；
> PaperPilot 工具经该端点以 **`mcp__mecha__gp_<工具名>`** 下发（命名律：`aggregate.py:98-100` 的 `f"{svc_id.lower()}_{tool}"`）。
> ⚠ **但带参数的工具调用不可用**（见问题 3）——只有零参工具正常。

---

## 问题 1（配置漂移 · 不阻塞但属确定性告警）：overlay 仍在补丁一个已被移除的插件

**现象**：每次 dsh 组合配置都输出

```
dsh: [D:\学\BaiduSyncdisk\编程\code\python\Get Paper\dsh\cordis.isolate.patch.yml]
     patch: entry "remote-web-ui" not found
```

**位置**：`dsh/cordis.isolate.patch.yml:15-18`

```yaml
# ① 禁用"钉端口 + 回写共享 profile"的插件。
- id: remote-web-ui
  disabled: true
```

**目标已不存在**：`@linxin666/dsh-remote-web-ui` 已从共享 profile 移除。
`C:\Users\LENOVO\.dsh\profiles\web\cordis.patch.yml:12-15` 记录了移除原因与日期：

> webserver 绑定块：原先由 `@linxin666/dsh-remote-web-ui` 插件自动维护。**该插件已于 2026-09-24 移除**
> （原因：0.1.7 起设置层改为 Profile 插件配置，插件依赖的 `settingsScope` 服务不复存在，启动时卡 pending 无法激活）。

**影响**：
- 不阻塞——组合结果中 `webserver` 与 `paperpilot` 两条健在，`paperpilot dsh-config` 退出码 0；
- 但这是**每次启动都出现的确定性告警**，且属配置语义漂移：该 overlay 仍声称要"禁用 remote-web-ui"；
- 文件头注释同步漂移：`:5`（把 remote-web-ui 的托管行为当作现存机制来描述）与 `:13`（复核标准写成"应看到 `remote-web-ui` 行带 `disabled: true`"）都已与事实不符，会误导后续维护者。

**修法**：
1. 删除 `:15-18` 整个 ① 块；
2. 同步修注释：`:5` 删去对 `remote-web-ui` 托管块与端口回写的描述；`:13` 的复核标准改为
   「应看到 `paperpilot` 条目在列、`webserver` 行带 `!!js` 表达式」；
3. **保留** ② `webserver` 还原块（`:28-41`）——它才是本 overlay 的有效职责（还原端口表达式，令 launcher 的 `--port` 生效）。

**验收**：`paperpilot dsh-config` 输出中不再出现 `remote-web-ui` 的 not-found 行，且 `webserver`／`paperpilot` 两条仍在。

**原则重申**（该文件 `:20-26` 已写过同款告诫）：overlay 只写**本实例需要的隔离**，不列举别家或已退役的 id——
本条目正是该原则的又一次违反。

---

## 问题 2（实例卫生 · 中危）：两套 launcher + 一个不占端口的残留 MCP 服务端

**当前进程事实**：

| PID | 命令行 | 监听端口 |
|---|---|---|
| 24344 | `C:\Python314\pythonw.exe D:\code-nosync\mecha\start_mecha.pyw` | 无 |
| 26828 | `D:\python310\pythonw.exe D:\code-nosync\mecha\start_mecha.pyw` | 49322、49323 |
| 3844 | `<项目>\.venv\Scripts\python.exe -m paperpilot.main mcp` | **无** |
| 26368 | `D:\python310\python.exe -m paperpilot.main mcp` | **57063**（`.mcp-port`）、57065（`.cockpit-port`）、57068（`.control-port`） |

**风险**：
- `.mcp-port` 文件值为 `57063`，持有者是 PID 26368；而 PID 3844 跑着同名服务却不监听任何端口 ⇒
  "哪个实例是权威"只能靠端口反推，排障时极易误判；
- **两个 launcher 并存** ⇒ 站点/服务的生命周期被两处维护，存在重复 attach/detach 的可能；
- GP 当前 `holds_write_lease: true`：并存实例下"谁持有写租约"存在竞态与审计歧义；
- 残留进程白占内存与句柄。

**修法**：确定唯一权威实例（当前为 `D:\python310` 那套：launcher 26828 + 服务端 26368），
终止 PID 24344 与 PID 3844；启动器侧加**单实例约束**（目标端口被占则拒绝启动或显式接管），
避免再次出现"起了两套、只有一套占端口"。

---

## 问题 3（P0 · 阻塞全部带参调用）：聚合端点把工具参数丢了

**现象**：`mcp__mecha__gp_<工具>` 的调用**以空参数执行**——只有零参工具正常。

| 调用（实测） | 期望 | 实际回执 |
|---|---|---|
| `gp_search_papers(query="Rydberg", limit=5)` | 只回 Rydberg 相关 | 与原样 20 篇近期论文**逐条一致**（含超导结、Janus 单层膜、鞍点发现等无关篇）；`offset` 恒为 0 |
| `gp_search_papers(offset=20)` | 第二页 | 与 `offset=0` **同一页** |
| `gp_prepare_review(stage=brief, date=…)` | 粗筛池（brief 明示不套 floor） | 按**默认 `stage=full`** 执行 ⇒ 被 `review_floor` 滤空 |
| `gp_prepare_review(stage=full, arxiv_ids="…")` | 只回点名篇 | 同上报错（点名未生效） |
| `gp_set_config(key=…, value=…)` | 落值并回读 | 校验失败：`key`/`value` **Field required**（收到空 dict） |

**影响**：AI 面日常作业（日报三段、推荐流、图谱、精读）**全部不可用**；仅
`query_software` / `read_config` / `review_status` / `query_briefings` / `read_write_authority` 等零参工具可用。

**定位**：同一批能力经 CLI（`paperpilot call <tool> -p key=value`，argv 直传）**参数完全正常**
⇒ 缺陷在**聚合端点的参数通路**（mecha：`Aggregator` / `McpEndpoint` / `_LiveRegistry` 的 schema 投影与参数转发），
不在 PaperPilot 能力层。

**判据缺口**：`checks/aggregate_tools_hot_sync_selfcheck.py` 只验“经端点**调得动**一个工具”，
不验“**带参数**调得动”。建议补一条：经端点真调一个**带参**工具并断言参数生效
（例：`search_papers(query=…)` 的命中集合应显著小于全量；`set_config` 应回读新值）。

---

## 问题 4（配置错配 · 令 `stage=full` 恒空池）：`review_floor=0.4` 与 2026-09-30 新打分尺度不兼容

- **运行值**：YAML `scoring.review_floor = 0.4`（Web 表单默认值亦为 0.4：`app/web.py:704`、`app/templates/settings.html:100`）。
  该键**归 YAML 管、不在 Gate 内**（`settings.html:105`），AI 侧无工具可改。
- **尺度事实**：新分＝三维占比平均，实测最高约 0.25、中位约 0.07（本批池子上限 0.198）⇒ **0.4 恒不可达**。
- **代码事实**：`config.py:66` 中该字段默认 `0.0` 且标注 `@deprecated 由"前 N"承担`；
  但 `pipeline.py:239` 仍读取它，并在 `stage=full` 且未点名时用它过滤候选。
- **后果**：任何 `stage=full` 的 `prepare_review`（也是 MCP 面默认）**必然空池**，
  且给出误导性诊断“38 篇过规则候选全部低于 review_floor=0.4”。`stage=brief` 不套 floor，故以 brief 起始的流程可绕过。
- **修法**：把 `review_floor` 置 0（Web `/settings` 或 YAML），或直接移除该过滤
  （“前 N”已由 `_MAX_REVIEW_CANDIDATES` 承担）。

---

## 潜伏项（当前不显形，条件触发即复现）

`workspace-write` 文件策略下**无法启动任何子进程**：

```
SetNamedSecurityInfoW failed (Win32 5): grantWrite(D:\学\BaiduSyncdisk\编程\code\python\Get Paper)
```

当前会话策略为 `danger-full-access`，故不显形。项目位于 BaiduSyncdisk（同步盘）路径下，ACL 可能被同步客户端改写；
仓库自带技能 `diagnose-windows-sandbox-acl` 可做 scoped、带备份的 ACL 修复。

---

## 复核命令

```powershell
# 问题 1：应无 not-found 行，且 webserver / paperpilot 两条健在
paperpilot dsh-config 2>&1 | Select-String 'not found|id: webserver|id: paperpilot'

# 问题 2：应只剩一套 launcher + 一个占端口的 MCP 服务端
Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
    Select-Object ProcessId, CommandLine
```

工具面健康检查（经聚合端点）：`mcp__mecha__query_software` → 期望 `GP / aggregate=up / tools=61 / process=ready / port=57063`。
