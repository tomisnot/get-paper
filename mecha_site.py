"""Get Paper 的**站点声明**：把"这个项目怎么被起来"交给 mecha 的统一启动器。

依据：`mecha/docs/v2/统一启动器-设计.md`（§4.2 字段 / §2.2.1 命名律 / §5.6 环境变量必须钉死 /
§7.2.2 三条裁决）。**它是数据，不是逻辑**：不起线程、不连网络、不读业务状态。

## 为什么每一行是现在这样（改之前先读）

| 声明 | 依据（都实测过，不是照抄别家） |
|---|---|
| `argv = <python> -m paperpilot.main mcp` | ⭐ **只起 `mcp` 子命令**（headless 语义通道，写 `.mcp-port`）；**不用 `paperpilot ai`** —— 那个前台跑 dsh，编排已归框架 |
| `ok_codes=()` | ⚠ **实测**：本仓 MCP 端点的 `GET /mcp` 回 **406**、`POST /mcp` 回 400、`/` 与 `/healthz` 回 404 ⇒ **该端口上没有任何 2xx 的只读路由**（`/healthz` 在 Web 应用上，不在这个端点）。故只能表达"**有 HTTP 响应即算在听**"（比 `tcp=True` 强：同端口可能是别的东西在听） |
| `env["PAPERPILOT_CONFIG"]` | ⚠ **决定读哪份 settings.yaml** ⇒ 决定状态根（`data_dir` 相对它上级目录解析）。不钉死 ⇒ 继承来的环境变量可能把两腿指到**不同世界**（§5.6，ML/EL 各踩过一次） |
| `env["PAPERPILOT_DATA_DIR"]` | ⚠ 状态根本身的显式值（`config.py:223` 只在 YAML 没写 `data_dir` 时生效 ⇒ 是**弱**覆盖，仍需钉，见下方"唯一不变量"） |
| `data_root = 仓根` | GP 的运行期状态全在仓内（`data/` + 三个端口文件）⇒ 取仓根（与两家同形；比 `data/` 更强：两个实例共享仓根即互斥） |
| `patches = 交付层 + 项目层` | §7.2.2 裁决：`cordis.patch.yml`（→`lib/index.mjs`）+ `cordis.isolate.patch.yml`（GP 的项目层）；**不用** `cordis.source.patch.yml`（dev 产物，与 `plugin_artifacts` 不同源） |
| `has_gui=True` | GP 的"人手那条腿"是 **Web 界面**（`serve` + `templates/`）⇒ 算有 |
| `initial_mode=Mode.AI` | 从 mecha 启动 = 永远 AI 组（与它 `BOOT_MODE_DEFAULT="open"` 的出厂策略不冲突，见下方注释） |

## ⚠ 唯一不变量（两处必须同时改）

**状态根只有一个**：`<仓根>/config/settings.yaml` 的 `data_dir`（缺省 `data` ⇒ `<仓根>/data`）。
本文件把它按缺省钉成 `<仓根>/data`。**若你把 settings.yaml 的 `data_dir` 改到别处，
必须同时改这里的 `PAPERPILOT_DATA_DIR` 与 `data_root`** —— 否则就是 ML 那种
"切了但看着是空的"（两腿看两份状态，且**没有任何报错**）。

## 查清后**没有**发现的两类（免得后人再查一遍）

* **`*_NO_*` 开关类：GP 没有**。`settings.mcp.enabled` / `settings.web.enabled` 只影响提醒与
  是否起对应服务，**不会让 `mcp` 子命令起不来**；`ai.provider=off` 也只是无 AI 档。
* **`DSH_HOME`：刻意不钉**。它只服务 `paperpilot ai --debug-home`（用项目内 `.dsh-debug`
  隔离会话/凭据）；把它钉进声明会让**每次启动都要求重新登录**，而框架自己起 dsh 时用
  共享 `~/.dsh` 即可。`--debug-home` 仍是可以手动跑的调试开关，不进声明。
* **GP 的 dsh 插件不需要任何环境变量**（与 ML 的 `MLTB_ML_ROOT` 不同）：它读的数据根来自
  `Config.projectRoot || process.cwd()`，而框架以 `cwd=仓根` 起 dsh（`dsh/src/index.ts`）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mecha.authority import Mode
from mecha.site import DSHSpec, ReadySpec, Service, Site

#: 本仓根（声明文件就在仓根）。
_SITE_ROOT = Path(__file__).resolve().parent

#: 状态根（= settings.yaml 缺省的 `data_dir`；见模块 docstring 的"唯一不变量"）。
_DATA_DIR = _SITE_ROOT / "data"

#: 真配置的路径（不钉死内容，只钉死**读哪一份**；文件不存在时 `load_settings` 照旧回落到
#: `config/settings.example.yaml`，行为与不钉时相同）。
_CONFIG_FILE = _SITE_ROOT / "config" / "settings.yaml"

#: `.mcp-port` 由 `paperpilot mcp` 写在**数据目录的父目录**（= 仓根，`hub.py`/`cli.py` 的约定）。
_PORT_FILE = ".mcp-port"


def _python() -> str:
    """起服务用哪个解释器（本声明里唯一需要判断的一处）。

    ⚠ **`paperpilot` 装在本项目的 venv 里**（README 的装法：`python -m venv .venv` + editable），
    而**框架的 `sys.executable` 可能是另一个环境**（mecha 自己的 venv）⇒ 用错解释器
    `import paperpilot` 直接失败 ⇒ **服务根本起不来**（不是"少个功能"，是整条腿都没有）。
    ⇒ 优先本项目 venv 的解释器（存在即用）；不存在才回落到 `sys.executable`
    （那种情形说明 GP 是装进框架那个环境里的，或用的是全局安装）。

    ⚠ **不用 `paperpilot.exe` 控制台脚本**：它把 venv 路径烘进 launcher、换解释器即失效；
    而 `-m paperpilot.main` 对任何"能 import paperpilot"的解释器都成立
    （`main.py` 自带 `if __name__ == "__main__"` 守卫）。
    """
    venv_python = (_SITE_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    return str(venv_python) if venv_python.is_file() else sys.executable


def _service_argv() -> tuple[str, ...]:
    """起本项目的服务（headless 权威）：`<python> -m paperpilot.main mcp`。

    ⚠ **不写任何旗标**：`mcp` 子命令的默认值就是 GP 的项目策略（host `127.0.0.1`、port `0`＝
    自动选、`--mode` 取 `hub.BOOT_MODE_DEFAULT="open"`、cockpit 默认开）。在这里再抄一遍
    ＝同一件事两处真值（本工程反复治的病）。
    """
    return (_python(), "-m", "paperpilot.main", "mcp")


#: ⚠ **两腿同源**：这两个变量一起决定"看到哪份状态"。见模块 docstring 的"唯一不变量"。
_STATE_ENV = {
    "PAPERPILOT_CONFIG": str(_CONFIG_FILE),
    "PAPERPILOT_DATA_DIR": str(_DATA_DIR),
}

SITE = Site(
    name="Get Paper",
    services=(
        Service(
            id="GP",                      # ⚠ 暴露名会小写化成 `gp_<tool>`（§2.2.1 命名律）
            brief="论文抓取 / 评审 / 简报",
            argv=_service_argv(),
            cwd=_SITE_ROOT,
            #: 就绪 = "端口文件相对 spawn 前**变过**" 且 "**探活通过**"（框架两条一起看：
            #: 缺一条就会把陈旧端口文件当成就绪的假绿）。
            #: `ok_codes=()` 的实测依据见模块 docstring（`GET /mcp` = 406）。
            ready=ReadySpec(
                port_file=_PORT_FILE,
                probe="http://127.0.0.1:{port}/mcp",
                ok_codes=(),
                timeout_s=90.0,
            ),
            data_root=_SITE_ROOT,
            #: ⭐ 三层架构（2026-10-08 换代）：**"切手动"不是另一条要 spawn 的腿**，
            #: 而是**写权切换**（``Mode``）—— 于是旧的 ``manual: ChildSpec`` 字段已从
            #: 框架删除（传它会当场 ``TypeError``，判据 ⑬ 钉住）。
            #:
            #: ``has_gui`` = "**本项目有没有人操作的那个面**" ⇒ ⭐ **GP 是 Web 界面**
            #: （``paperpilot serve`` + ``templates/``），没有桌面窗口。
            #: ⚠ 按公理 A2（"两条腿都是客户端"），**Web 面就是人手那条腿** ⇒ 算 True。
            #: ⚠ 但 GP 与别家**结构不同**：它的 Web 与 MCP **挂在同一个 stack 上**
            #: （``cli.py`` 的 ``build_stack`` + ``make_host``）⇒ 它本来就"同门"，
            #: 不需要像别家那样把窗口搬进软件进程。
            has_gui=True,
            #: ⭐ 启动者（人）声明的初始写权。⚠ GP 的既有用户裁决是
            #: ``BOOT_MODE_DEFAULT = "open"``（**"不卡写权"**：两侧都能写，代价是后写覆盖）。
            #: ⚠ 这与新架构的"**从 mecha 启动 = 永远是 AI 组**"**不冲突** ——
            #: 前者定的是"**软件出厂策略**"，后者定的是"**从 mecha 起时给什么写权**"。
            #: ⇒ 从 mecha 起 ⇒ 声明 **AI**（框架只在 spawn 时把它送进去，应用与否由软件决定）；
            #:   而人若想"不卡写权"，那是在**面板上切**（`Mode.OPEN`）的事。
            initial_mode=Mode.AI,
            env=_STATE_ENV,
        ),
    ),
    dsh=DSHSpec(
        command=("dsh",),
        cwd=_SITE_ROOT,
        #: 入库的**交付层 + 项目层**（§7.2.2 裁决）。第三层（指向聚合端点的 overlay）
        #: 是**生成物**，由框架按聚合端点地址生成。
        patches=(
            _SITE_ROOT / "dsh" / "cordis.patch.yml",
            _SITE_ROOT / "dsh" / "cordis.isolate.patch.yml",
        ),
        #: 3080 是官方 dsh 的端口，项目实例永不占（框架从 3081 起试）。
        port_preference=3081,
        #: 插件产物：不存在 ⇒ 响亮失败（而不是起一个没有工具面的 dsh）。
        plugin_artifacts=(_SITE_ROOT / "dsh" / "lib" / "index.mjs",),
        #: GP 的插件**不需要**数据根环境变量（见模块 docstring 第三条查清项）⇒ 不编一个。
        env={},
    ),
)


__all__ = ["SITE"]
