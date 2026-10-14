"""check/ 的公共夹具：**从 tests/conftest 转借**（不复制第二份，避免两处真相）。

判据要跑在**和产品一样的装配**上，所以夹具共用是对的；区别只在"这份文件是不是守卫"。
夹具只从产品测试配置借用，模型客户端已从项目移除。
"""

from tests.conftest import (  # noqa: F401
    SAMPLE_XML,
    container,
    loaded_repo,
    make_settings,
    pipeline,
    repo,
    sample_papers,
    settings,
)
