"""check/ 的公共夹具：**从 tests/conftest 转借**（不复制第二份，避免两处真相）。

判据要跑在**和产品一样的装配**上，所以夹具共用是对的；区别只在"这份文件是不是守卫"。
注意 `_no_llm_keys` 是 autouse 夹具且带下划线前缀——必须显式 import，`import *` 会漏掉它。
"""

from tests.conftest import (  # noqa: F401
    SAMPLE_XML,
    _no_llm_keys,
    container,
    loaded_repo,
    make_settings,
    pipeline,
    repo,
    sample_papers,
    settings,
)
