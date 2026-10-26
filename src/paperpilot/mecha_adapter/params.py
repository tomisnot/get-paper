"""参数描述的兜底：给参数名补一句人话，别让模型对着 `draft_id` 猜。

为什么要有这一层：schema 里的 `description` 是**模型唯一的用法来源**（旧版撞过：
没描述的参数全靠试错）。手写每处描述容易漏，于是：

- 每条声明**优先用自己写的**；
- 没写的，从这张常用表里补；
- 表里也没有的，**构建期直接报错**（飘到运行期就是模型瞎猜）。

这样"参数没描述"在装配时就炸，而不是等模型用错才发现。
"""

from __future__ import annotations

from mecha.errors import MechaError

#: 常用参数名的默认描述。只放"看名字看不出语义差别"的那些。
COMMON = {
    "arxiv_id": "arXiv 编号，如 2610.25020",
    "days": "回溯天数（windows 省略时生效）",
    "windows": "日期段，如 2026-03-01~2026-03-07（多段用逗号隔开）",
    "regions": "arXiv 分类，逗号分隔；省略＝画像 top-N",
    "queries": "检索短语，逗号分隔（之间是 OR）",
    "limit": "本次最多返回多少条",
    "offset": "从第几条开始（续读用上次回执的 next_offset）",
    "top": "每一维取前几名，默认 12",
    "n": "抽多少篇",
    "mode": "模式：random / recent / focused / far",
    "exclude_recent_drafts": "true＝排除近期已出现在别的草稿里的论文",
    "ident": "草稿 id 或标题；省略＝最近修改的那份",
    "kind": "linear / network",
    "title": "草稿名＝情景，例如「1019-1024」",
    "note": "一句话备注（渲染在标题下面）",
    "payload": "内容对象",
    "draft_id": "草稿 id（query_drafts 可查；0＝不指定）",
    "dimension": "category / term / author",
    "key": "键名（该维度里的取值，如 quant-ph / rydberg）",
    "weight": "权重（正数；锚点不衰减、不参与重算）",
    "value": "新值",
    "signal": "信号名：view/outbound/download/read/star/skip/uninterested",
    "read": "true＝已读，false＝未读",
    "starred": "true＝收藏，false＝取消收藏",
    "content": "笔记正文",
    "min_count": "被至少几篇库内论文引用才算一个簇",
    "op": "按 op 前缀过滤，如 scan / signal:",
    "actor": "按操作者过滤：human / ai / system",
    # 卡的五段（写卡用）；terms 同时喂画像的词面维
    "tldr": "一句话总括（人看卡片时先读这句）",
    "problem": "这篇要解决什么问题",
    "method": "用了什么方法（关键机制/参数）",
    "results": "关键结果，尽量带数字",
    "novelty": "贡献是什么、为什么算新",
    "terms": "语义关键词，逗号分隔（画像的词面维吃这个）",
    "query": "检索词（库内做包含匹配；单个词比短语召得准）",
}


def describe(params: dict, *, owner: str = "") -> dict:
    """给缺描述的参数补默认描述；补不出来就响亮失败。"""
    out = {}
    for name, info in (params or {}).items():
        entry = dict(info)
        if not entry.get("description"):
            fallback = COMMON.get(name)
            if not fallback:
                raise MechaError(
                    f"{owner} 的参数 {name!r} 没有 description，常用表里也没有",
                    kind="bad_param_doc",
                    hint="在声明处写一句，或把它加进 mecha_adapter/params.py 的 COMMON",
                )
            entry["description"] = fallback
        out[name] = entry
    return out
