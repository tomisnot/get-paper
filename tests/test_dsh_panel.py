"""dsh 面板判据（TS 侧）的**仓内入口**。

为什么由 pytest 跑那一侧：面板的两条关键路径（地址路由"绝不回落"、面板"绝不空白"）都
写在 TS 里，而本仓的门禁是 pytest。不把入口挂在这里 ⇒ 那些判据**存在于仓外**，
改坏了没人知道——正是 R10（"全绿只对判据覆盖到的路径成立"）要防的。

跑的是三份文件：
* `src/panel/monitor-url.test.ts` / `src/panel/monitor-client.test.ts` —— **共享单测**，
  与 `mecha/mecha/dsh-panel/` 的参考实现逐字一致（**不许改**）；
* `test/panel.test.ts` —— **项目自己的**判据（含反面语料："不许回落到历史默认 8080"
  "缺地址不许指向相对路径"）。

⚠ 这三份都**零 npm 依赖**（只用 `node:test` / `node:assert` / `node:fs`；Node ≥ 22.6
原生剥类型）⇒ **不需要 `dsh/node_modules`**，只需要 PATH 上有 `node`。
缺 node **即红**，不软跳过——软跳过就是"存在但从不执行"的假绿（框架侧同款纪律）。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DSH_DIR = Path(__file__).resolve().parents[1] / "dsh"
PANEL_DIR = DSH_DIR / "src" / "panel"

#: 相对 `dsh/` 的判据文件（共享件在前，项目自己那份在后）。
PANEL_TEST_FILES = (
    "src/panel/monitor-url.test.ts",
    "src/panel/monitor-client.test.ts",
    "test/panel.test.ts",
)

#: **项目自有**那份判据（不在参考指纹表里 ⇒ 它的"用例数下限"要单独守）。
PROJECT_PANEL_TEST = "test/panel.test.ts"

#: `test/panel.test.ts` 的**用例数下限**（写下限时的实测值）。
#:
#: 为什么需要它（**实测出来的洞**）：`dsh/test/panel.test.ts` 是**项目自有**文件
#: （资产 README 明确要求项目自建一份放"自家反面语料"）⇒ **不在** `REFERENCE_SHA256` 里
#: ⇒ **删掉其中若干用例时指纹守卫与仓内门禁全绿**（实测：删 1 例 ⇒ node `tests` 40→39，
#: 两条守卫都绿）——那正是"**守卫被悄悄抽空**"。
#: ⚠ 反过来，**共享**的那两份 `.test.ts` **在**指纹表里 ⇒ 改它们必红（亦已实测）。
#: ⚠ **新增用例后请同步抬高这个数**：失败信息会提醒你是"删了判据"还是"忘了抬下限"。
#:
#: 变更记录（每次**有意**增删都在这里留一行——"为什么是这个数"必须可追）：
#: * 19 → **17**（2026-09-26 第 3 批）：删了 2 条 —— `viewPath`（AI 监控不再走 iframe，
#:   没有第二个视图了）与 `assertNever`（**资产自测已覆盖**，见 `monitor-client.test.ts`：
#:   同一事实不留两个守卫）。
PANEL_TEST_FLOOR = 17

_COUNT_RE = re.compile(r"^\u2139\s+(tests|pass|fail|skipped)\s+(\d+)\s*$", re.M)


def _node_counts(output: str) -> dict[str, int]:
    """解析 `node --test` 的自证计数（`ℹ tests N` / `ℹ pass N` / …）。"""
    return {name: int(n) for name, n in _COUNT_RE.findall(output)}


def _run_node_tests(*files: str) -> tuple[int, str]:
    """跑 `node --test <files>`（cwd = `dsh/`），返回 (退出码, 合并输出)。

    ⚠ 硬依赖 `node`：缺 node **即红**，不软跳过——软跳过就是"存在但从不执行"的假绿
    （与框架侧 `dsh_panel_selfcheck` 同款纪律）。
    """
    node = shutil.which("node")
    if node is None:
        pytest.fail("PATH 里没有 node ⇒ 面板判据（TS）跑不了。装 Node ≥ 22.6（原生剥类型）后重试。")
    proc = subprocess.run(
        [node, "--test", *files],
        cwd=str(DSH_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    return proc.returncode, f"{proc.stdout}\n{proc.stderr}"

# ---------------------------------------------------------------- 副本 ⇄ 参考实现（**按提交态**）

#: 参考实现的来源版本（mecha 仓的提交）。重新复制时**必须**同步更新本行与下表。
#:
#: ⚠ **不必等上游通知**：按 mecha 的**当前提交态**取即可（`git cat-file blob <rev>:…`），
#: 取完把 rev 写在这里 ⇒ **两仓的"同步"因此不需要人来对齐**（本轮已如实走了一遍：
#: `eab7b9e` → `7931b8d`，只 README 变了）。
REFERENCE_REVISION = "mecha@7931b8d (2026-09-26) mecha/dsh-panel/"

#: 参考实现**提交态**的正文指纹（`\r\n` 归一成 `\n` 后的 sha256）。
#:
#: ⚠ 三条刻意的设计（每一条都是本工程付过代价的）：
#:
#: 1. **按提交态，不按工作树**（A8 裁决）：判据**不读** `D:\code-nosync\mecha` 的活文件。
#:    否则**参考实现那边任何在飞改动都会红本仓判据**（本轮就真发生了：M 正在改那边 README），
#:    而那不是本项目的问题——"噪声化的门禁等于没有门禁"。
#:    这里把"参考实现的提交态"钉成**指纹常量**：副本一旦被就地改动就红，且与上游在飞改动解耦。
#: 2. **归一化行尾**：`git` 的 `core.autocrlf` 会让"同一内容"在不同检出状态下字节不同
#:    （本仓提交时会 LF→CRLF）。逐**字节**比对会被行尾配置打败 ⇒ 这里比**正文**（归一 LF）。
#: 3. **`panel-config.ts` 不进表**：它是复制约定里**唯一允许项目专有**的文件（`ROUTE_PATH`
#:    /`PORT_FILE`/`TITLE` 三个值），它的正确性由 `test_cli_startup.py` 的跨语言守卫管。
#:    `README.md` 也**不进表**：prose 的就地批注不改变行为，钉它只制造噪声（复制时仍逐字抄）。
REFERENCE_SHA256 = {
    "monitor-url.ts": "c6a948106a480c67b76447d9e160f29519637a1ae5e3716a824552497efcf1ba",
    "monitor-client.ts": "eec71f3f7e47cdbbafdece1c9649bd68f3a86e6631b486e9e8e3d18cac9260c9",
    "monitor-url.test.ts": "62994cf218fc195890a7dc966f5159ee62aaf926a25a032d1a7f70bdf6d99c07",
    "monitor-client.test.ts": "754dee85d2a329a5da3c1425368e3c46595ac8605f4df88a0acc6bdf95f03616",
}


def _text_sha256(path: Path) -> str:
    """正文指纹：按 UTF-8 读、`\\r\\n` 归一成 `\\n` 后取 sha256（行尾配置不参与）。"""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_panel_copies_match_reference_commit():
    """**除参数块之外逐字一致**：副本必须等于参考实现**提交态**的正文指纹。

    红证（实测）：在 `monitor-client.ts` 里改一个字符 ⇒ 本判据红。
    """
    drifted = []
    for name, expected in REFERENCE_SHA256.items():
        path = PANEL_DIR / name
        assert path.is_file(), f"面板副本缺失：{path}（复制约定要求它逐字存在）"
        actual = _text_sha256(path)
        if actual != expected:
            drifted.append(f"{name}: 期望 {expected[:12]}… 实得 {actual[:12]}…")
    assert not drifted, (
        "面板副本与参考实现（" + REFERENCE_REVISION + "）不一致——"
        "**除 panel-config.ts 之外不许就地改**。漂移文件：\n  " + "\n  ".join(drifted) +
        "\n若确有必要（如上游已发布新版本），请**重新整份复制**并同步更新 "
        "REFERENCE_REVISION 与 REFERENCE_SHA256，而不是就地打补丁。")

    # ⚠ 这里原先还有一条 `assert _text_sha256(panel-config.ts) not in REFERENCE_SHA256.values()`，
    # 注释写的是"参数块必须是项目值"——**它不可能失败**（参数块的正文结构上不可能等于那 4 个
    # 代码/单测文件之一的正文）⇒ R11 那一类（观测量恒真），**已删**。
    #
    # 它想守的事实**已被两条能失败的守卫覆盖**，且**各有其家**（改动任一处都会红）：
    #   · 参数块的**值**（`PORT_FILE` 非空且不是参考实现的中性默认 / `ROUTE_PATH` 非中性默认 /
    #     `TITLE` 非空）→ `dsh/test/panel.test.ts` 的参数块判据。它 **import** 资产的
    #     `DEFAULT_ROUTE_PATH` 常量来比对 ⇒ 上游改了中性默认它**跟着变**；若在这里用 Python
    #     重写一遍，就得**硬编** `/mecha/monitor-url`（跨仓字面量，会陈旧）⇒ 不该在此重复。
    #   · **Python 侧写的名字** == 参数块的 `PORT_FILE` → `tests/test_cli_startup.py` 的跨语言守卫。
    # 依据：**同一事实只留一个能失败的守卫**（D1 一事实一归属；两条都在 = 噪声）。


def test_dsh_panel_criteria_pass():
    """`node --test` 跑面板判据：必须**真跑且全绿**（跳过不算过）。"""
    missing = [f for f in PANEL_TEST_FILES if not (DSH_DIR / f).is_file()]
    assert not missing, f"面板判据文件缺失：{missing}（被删掉 = 守卫消失，不是「通过」）"

    code, out = _run_node_tests(*PANEL_TEST_FILES)
    assert code == 0, f"面板判据红了（exit {code}）：\n{out}"

    # R8 非退化自证：光看 exit code 不够——**全被跳过也是 0**。要求它有通过数、零失败、零跳过。
    counts = _node_counts(out)
    assert counts.get("pass", 0) > 0, f"看不到通过计数（报告格式变了？判据要跟着改）：\n{out}"
    assert counts.get("fail", 0) == 0, f"有失败计数：\n{out}"
    assert counts.get("skipped", 0) == 0, f"有判据被跳过 ⇒ 它们没真跑：\n{out}"


def test_project_panel_cases_not_hollowed():
    """⭐ **项目自有**那份面板判据的用例数不得低于下限（防"守卫被悄悄抽空"）。

    为什么单独守它（而共享的 `.test.ts` 不用）：共享两份**在**参考指纹表里 ⇒ 改它们必红；
    而 `test/panel.test.ts` 是项目自有、**不在**表里 ⇒ 删用例没有别的守卫会红（实测确认过）。
    """
    code, out = _run_node_tests(PROJECT_PANEL_TEST)
    assert code == 0, f"项目面板判据红了（exit {code}）：\n{out}"

    ran = _node_counts(out).get("tests", 0)
    assert ran >= PANEL_TEST_FLOOR, (
        f"项目面板判据用例数 {ran} < 下限 {PANEL_TEST_FLOOR}："
        "**判据被删了吗**（守卫被抽空）？若确实是有意精简，请**同时**下调 PANEL_TEST_FLOOR "
        "并说明删的是哪一条；若是**新增**用例，请把下限抬高到新的实测值。")
