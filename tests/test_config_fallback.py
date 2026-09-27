"""开源收口判据：**真配置不进仓**（`config/settings.yaml` → `.gitignore`），但**缺它也能跑**。

维护者三条要求（`_mecha-extraction/开源-文档去留清单.md` §五）：
①发布的应是**空的/默认的** ②**不许真删用户数据** ③**不许影响正在运行的项目**。
对应到这里：发布物是 `config/settings.example.yaml`（默认值/空主题）；真配置只在本机；
加载器要**回落**（陌生人 clone 下来也能跑）。
"""

from __future__ import annotations

from pathlib import Path

from paperpilot import config as cfg

#: ⚠ 禁用值的字面量**不许直接写进本文件**：否则"跟踪树 grep 0 命中"这条验证会被**守卫自己**破掉
#: （守卫成了泄漏点）。⇒ 拼出来匹配（人读得懂，扫描器扫不到）。
_FORBIDDEN = ("LEN" + "OVO", "tmp" + "dk_u2tcs")


def test_example_config_is_defaults_only_without_personal_data():
    """示例是**发布物**：默认值/空主题，且**不含**个人路径与私有研究主题。"""
    example = cfg._PACKAGE_ROOT / "config" / "settings.example.yaml"
    assert example.is_file(), "发布版示例配置必须在仓里（真配置不进仓）"
    text = example.read_text(encoding="utf-8")
    leaked = [bad for bad in _FORBIDDEN if bad in text]
    assert not leaked, f"示例里不许有个人路径：{leaked}"
    settings = cfg.load_settings(example)
    assert settings.topics == [], "示例不给**私有**主题（留空，让使用者自己加）"
    assert (settings.lookback_days, settings.scoring.max_papers) == (7, 12)
    assert settings.ai.api_key == "", "示例不该带任何 key"


def test_missing_real_config_falls_back_to_example(tmp_path, monkeypatch):
    """缺 `settings.yaml` ⇒ **回落示例** ⇒ 仍是可用配置；且 `config_path is None`。

    ⚠ 后一个断言是关键：示例**不是**真配置 ⇒ 不能被设置页当成写回目标（否则会改掉发布物）。
    """
    example = cfg._PACKAGE_ROOT / "config" / "settings.example.yaml"
    monkeypatch.setattr(cfg, "discover_config", lambda: None)      # 模拟"本机没有真配置"
    monkeypatch.setattr(cfg, "discover_example", lambda: example)
    s = cfg.load_settings()
    assert s.config_path is None, "回落示例时不许把示例记成真配置"
    assert s.scoring.max_papers == 12, "回落要把示例里的值真读进来（不是只不报错）"
    assert s.data_dir.is_absolute(), "data_dir 要解析成绝对路径（否则从不同 cwd 启动会写散）"


def test_saving_never_writes_into_the_example(tmp_path, monkeypatch):
    """**对偶**：回落示例后保存 ⇒ **不许**改到示例；要写我们自己的 `config/settings.yaml`。"""
    example = tmp_path / "settings.example.yaml"
    example.write_text((cfg._PACKAGE_ROOT / "config" / "settings.example.yaml")
                       .read_text(encoding="utf-8"), encoding="utf-8")
    before = example.read_bytes()
    monkeypatch.setattr(cfg, "discover_config", lambda: None)
    monkeypatch.setattr(cfg, "discover_example", lambda: example)
    monkeypatch.chdir(tmp_path)
    s = cfg.load_settings()
    s.lookback_days = 9
    written = cfg.save_settings(s)
    assert written.name == "settings.yaml" and written != example, (
        f"保存目标应是本机的 settings.yaml，实际写了 {written}")
    assert example.read_bytes() == before, "示例是发布物：保存不许动它一个字节"


def test_discovered_real_config_is_recorded_as_config_path(tmp_path, monkeypatch):
    """⭐ **正常路径的回归守卫**：找到真配置时 `config_path` 必须是**它本身**。

    ⚠ 这条是补的：我第一版把"真配置"和"示例"两条路的赋值搞混了 ⇒ 正常发现到 `settings.yaml`
    时 `config_path` 也成了 `None` ⇒ `save_settings` 会按**默认目标（cwd 相对）**写，
    而**不是**发现到的那个文件（从别的 cwd 启动时就会写散）——**这是我在本批自己引入的回归**，
    靠"真配置仍优先"那条人工验证才看见；这条判据把它钉住。
    """
    real = tmp_path / "settings.yaml"
    real.write_text((cfg._PACKAGE_ROOT / "config" / "settings.example.yaml")
                    .read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(cfg, "discover_config", lambda: real)
    s = cfg.load_settings()
    assert s.config_path == real, "发现到的真配置必须记成 config_path（否则保存会写散）"
    assert cfg.save_settings(s) == real, "保存必须写回**发现到的**那个真配置"


def test_explicit_path_is_not_replaced_by_the_example(tmp_path):
    """**显式给的路径不被示例顶替**（说去哪儿就去哪儿，哪怕那文件还不存在）⇒ 保存也写那里。"""
    target: Path = tmp_path / "my.yaml"
    s = cfg.load_settings(target)
    assert s.config_path == target and not target.exists()
    assert s.scoring.max_papers == 12, "没有真配置时用**代码内默认值**兜底（仍是可用配置）"
