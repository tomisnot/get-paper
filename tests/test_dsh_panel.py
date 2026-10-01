"""dsh 面板资产（TS 侧）的**仓内入口**：两张表 + 三条断言。

为什么由 pytest 跑那一侧：资产的两条关键路径（地址路由"绝不回落"、面板"绝不空白"）都写在
TS 里，而本仓的门禁是 pytest。不把入口挂在这里 ⇒ 那些判据**存在于仓外**，改坏了没人知道
——正是 R10（"全绿只对判据覆盖到的路径成立"）要防的。

⭐ **通用部分已上提 `mecha.asset_check`**（2026-10-01）：逐件比正文指纹、跑 `node --test` 并
自证"用例真跑了"（`fail==0 且 skipped==0 且 pass>=下限`）——**规则住框架，本仓只给表与路径**。
（上提的理由是实测的：EL 与本仓各写了 218 行同功能判据，形态还不同 ⇒ N 份会各自漂移的守卫。）
⚠ `run_node_suite` **硬依赖 `node`**：缺 node 即红，不软跳过（软跳过就是"存在但从不执行"的假绿）。

跑的是这两类文件：
* 资产副本的**共享单测**（面板四件 + opt-in 的 `mcp-bridge`）——与 `mecha/dsh-panel/` 的参考实现
  逐字一致，**不许改**；
* `test/panel.test.ts` —— **项目自己的**判据（GP 的参数块值 + 自家反面语料）。
"""

from __future__ import annotations

from pathlib import Path

from mecha.asset_check import audit_carriers, run_node_suite

DSH_DIR = Path(__file__).resolve().parents[1] / "dsh"
PANEL_DIR = DSH_DIR / "src" / "panel"

#: 相对 `dsh/` 的判据文件（**资产件在前**，项目自己那份在后）。
PANEL_TEST_FILES = (
    "src/panel/monitor-url.test.ts",
    "src/panel/monitor-client.test.ts",
    "src/panel/panel-data.test.ts",
    "src/panel/panel-view.test.ts",
    "src/panel/mcp-bridge.test.ts",      # opt-in 侧：桥的共享单测（2026-10-01 加）
    "test/panel.test.ts",
)

#: **项目自有**那份判据（不在指纹表里 ⇒ 它的"用例数下限"要单独守）。
PROJECT_PANEL_TEST = "test/panel.test.ts"

#: `test/panel.test.ts` 的**用例数下限**（写下限时的实测值）。
#:
#: 为什么需要它（**实测出来的洞**）：它是项目自有、**不在** `REFERENCE_SHA256` 里
#: ⇒ 删掉其中若干用例时指纹守卫与仓内门禁全绿（实测：删 1 例 ⇒ node `tests` 40→39，两条都绿）。
#: ⚠ **新增用例后请同步抬高这个数**：失败信息会提醒你是"删了判据"还是"忘了抬下限"。
#:
#: 变更记录（每次**有意**增删都在这里留一行——"为什么是这个数"必须可追）：
#: * 19 → **17**（2026-09-26 第 3 批）：删 `viewPath`（AI 监控不再走 iframe）与 `assertNever`
#:   （**资产自测已覆盖**：同一事实不留两个守卫）。
#: * 17 → **18**（2026-09-26 第 4 批）：加本项目 client 入口的 **import 闭包零 `node:`** 守卫
#:   （资产那条只走资产目录内部；消费侧的"树摇运气"要自己钉）。
#: * 18 → **10**（2026-09-26 第 5 批）：📄简报 iframe 整体退役，删 8 条盯它的用例。
#: * 10 → **6**（2026-10-01 第 6 批）：删 3 条 + 收窄 2 条 —— 通用形态已由**资产共享单测**覆盖
#:   （`monitor-url.test.ts`："端口文件在 ⇒ 200"、"每次调用现读（漂移自愈）"、"内容非数字/越界"；
#:   `monitor-client.test.ts`："doFetch 真的被调用（R16）"、"地址形状自检"），
#:   留下的全是**资产不知道的项目值/反面语料**（本项目历史默认地址、本项目 ROUTE_PATH 单一来源、
#:   参数块、client 入口闭包）。依据：同一事实只留一个能失败的守卫（R13/D1）。
PANEL_TEST_FLOOR = 6

#: **六个判据文件合计**通过数的下限（写下限时的实测值；防"资产判据被整体掏空"）。
#: ⚠ 它只管"不许塌缩"：资产**加**用例不会让它红（pass 只会涨）；用例数变化时要同步改这里
#: （2026-10-01 定：资产 56 例 + 本项目 `test/panel.test.ts` 6 例 = **62**）。
PANEL_SUITE_FLOOR = 62

# ---------------------------------------------------------------- 副本 ⇄ 参考实现（**按提交态**）

#: 参考实现的来源版本（mecha 仓的提交）。重新复制时**必须**同步更新本行与下表。
#:
#: ⚠ **真守卫是下面那张行尾归一 sha256 常量表，不是这个 rev**：判据读的是常量与**本仓副本**，
#: **从不读框架仓** ⇒ 上游在飞改动不会红本仓（"噪声化的门禁等于没有门禁"）。
#: ⚠⚠ 实测（2026-10-01）：`5ee78e4`（以及本轮更早记过的 `eab7b9e`/`7931b8d`/`c44b01f`）在框架仓
#: **都不是任何对象**（`git cat-file -t` = *Not a valid object name*）⇒ rev 行只是记账，
#: 重新复制时**按框架仓当前正文取**并同步更新常量表。
REFERENCE_REVISION = "mecha@5ee78e4 (2026-09-26) 面板十件 + mecha@22e41cc (2026-10-01) opt-in 六件"

#: 参考实现**提交态**的正文指纹（`\r\n` 归一成 `\n` 后的 sha256；比对规则在 `mecha.asset_check`）。
#:
#: 三条刻意的设计：
#: 1. **按提交态，不按工作树**：判据**不读**框架仓，否则上游任何在飞改动都会红本仓。
#: 2. **归一化行尾**：`core.autocrlf` 会让"同一内容"在不同检出状态下字节不同（本仓提交时 LF→CRLF）。
#: 3. **`panel-config.ts` / `README.md` 不进表**：前者是复制约定里**唯一允许项目专有**的文件
#:    （其值由 `dsh/test/panel.test.ts` 与 `tests/test_cli_startup.py` 的跨语言守卫管）；
#:    后者是 prose，就地批注不改变行为，钉它只制造噪声。
#:    ⚠ **本仓已【不保留】副本 `README.md`（2026-10-01 删，用户裁决"README 不重要"）**：
#:    它是给项目看的资产使用说明，而权威在框架侧 `mecha/dsh-panel/README.md` ⇒ 项目内留副本
#:    就是第二真值（D1）。这是对"复制时含 `README.md` 逐字抄"那条约定的**有意偏离**——
#:    其余 **17 件仍逐字一致**（缺件会红：`_carrier_bytes` 读不到就抛）。
#:
#: ⚠ **opt-in 六件也必须在表里**（2026-10-01 补）：它们同样是逐字复制的资产件，原先只钉默认
#: 十件 ⇒ 抄进来的桥被就地改一个字符**没有任何判据会红**（正是本资产要治的"漏一个文件"，
#: 这次漏的是"漏进指纹表"）。来源提交 `mecha@22e41cc`（含本仓此前自修的两处缺陷回流资产）。
REFERENCE_SHA256 = {
    "routes.ts": "e2b5bc35d84d368548146a4c7d6c4a2ef733b3191480f6a9d0131045c1295963",
    "monitor-url.ts": "9808f71a7179857f56baa25652cfc77e2d109f3d92d643aaf2c3ebc42f95c2a3",
    "monitor-client.ts": "200a7764f2d0139c17dd91294d02dd165ad0f51ad782ad39b3bb67708153e93c",
    "panel-data.ts": "cc32f9c7692fdbf2f0495d23f2f4bab2ac3e27620795e92f734132c96a0153bc",
    "panel-view.ts": "560103c18231df5bad3d18a77f16f0ed6cceab513928ef765dbdb2afec9a1aca",
    "MonitorTabBody.tsx": "8ed1233f18e32a9c4b6af37ebd69937fe63f5522ce2a410bb0dab8ec9339d3d6",
    "monitor-url.test.ts": "90db86e3b5217b89b18b082f37c502ef6750c54b2de58ad202fb71eecd9cfd29",
    "monitor-client.test.ts": "8dcff14f8fe5fb2f505ba7e9e845adab4f8c28c4af87c0234856817cecea4487",
    "panel-data.test.ts": "b3ece6c3f436f22b5abd3f7ffb295d3799f0fadf2628d4d791312bbd4445152a",
    "panel-view.test.ts": "bd2afe86800130e9ff8c03081a5696bd2c9f4c23fe71070c3fca57c72931d1b4",
    # ---- opt-in 六件（mecha@22e41cc，2026-10-01）----
    "mcp-bridge.ts": "59c5078fce46c4610a319d2a70628ef7827ea11c01734be02eca67d35ca2d510",
    "mcp-session-http.ts": "423dca6f422491690e91c03b1576296b578162aa5be72684419bc510445fa194",
    "config.ts": "537668e2c0b4af8bc0b624ec42cd9e5c0f126205ab676e7700bc592e137c4207",
    "register-tools.ts": "4eadbf2f495f2f16e09a197db920b1662cb17ddbf051f563509fbe08e0174bf0",
    "mcp-sdk-shims.d.ts": "d2fc24f0d7131fa2c2e700e9d669c51578f0698023c7919b3b3d36f027aa331e",
    "mcp-bridge.test.ts": "4fba5f5e963da321a1b3d33a4fc67323e0506cfad18d2fdadf7df7cc7962dc7e",
}


def _carrier_bytes(name: str) -> bytes:
    """指纹表的读口。⚠ 缺文件**响亮**（复制约定要求它逐字存在），不吞成"符"。"""
    path = PANEL_DIR / name
    assert path.is_file(), f"资产副本缺失：{path}（被删掉 = 守卫消失，不是「通过」）"
    return path.read_bytes()


def test_asset_copies_match_reference_commit():
    """**除参数块与 README 之外逐字一致**：逐件比正文指纹。

    红证（实测）：在副本的 `monitor-client.ts` 里改一个字符 ⇒ 本判据红。
    """
    drifted = audit_carriers(REFERENCE_SHA256, _carrier_bytes)
    assert not drifted, (
        "资产副本与参考实现（" + REFERENCE_REVISION + "）不一致——"
        "**除 panel-config.ts / README.md 之外不许就地改**。漂移文件：\n  "
        + "\n  ".join(drifted) +
        "\n若确有必要（如上游已发布新版本），请**重新整份复制**并同步更新 "
        "REFERENCE_REVISION 与 REFERENCE_SHA256，而不是就地打补丁。")


def test_asset_shared_suite_really_ran():
    """资产共享单测必须**真跑且全绿**（`fail=0 · skipped=0 · pass>=下限`）。"""
    missing = [f for f in PANEL_TEST_FILES if not (DSH_DIR / f).is_file()]
    assert not missing, f"判据文件缺失：{missing}（被删掉 = 守卫消失，不是「通过」）"

    r = run_node_suite(DSH_DIR, PANEL_TEST_FILES, min_pass=PANEL_SUITE_FLOOR)
    assert r.ok, f"{r.detail}\n{r.output}"


def test_project_panel_cases_not_hollowed():
    """⭐ **项目自有**那份判据的用例数不得低于下限（防"守卫被悄悄抽空"）。"""
    r = run_node_suite(DSH_DIR, [PROJECT_PANEL_TEST], min_pass=PANEL_TEST_FLOOR)
    assert r.ok, f"{r.detail}\n{r.output}"
    ran = r.counts.get("tests", 0)
    assert ran >= PANEL_TEST_FLOOR, (
        f"项目面板判据用例数 {ran} < 下限 {PANEL_TEST_FLOOR}："
        "**判据被删了吗**（守卫被抽空）？若确实是有意精简，请**同时**下调 PANEL_TEST_FLOOR "
        "并说明删的是哪一条；若是**新增**用例，请把下限抬高到新的实测值。")
