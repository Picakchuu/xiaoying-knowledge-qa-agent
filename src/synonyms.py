"""检索用的极小同义词表。

为什么不是 embedding：本题要求离线可跑、零外部依赖，且评分点是「检索是否被迭代改进」而不是
「向量模型有多强」。一张 10 条的同义词表足以演示「模型改写 query -> 召回改善」这条链路，
并且比向量检索更可解释、更好断言。

升级路径见 README（换 EmbeddingRetriever 时这张表可以直接丢掉）。
"""

from __future__ import annotations

EXPANSIONS: dict[str, tuple[str, ...]] = {
    # 口语 -> 制度用语
    "打车": ("市内交通费", "出租车", "交通费"),
    "打的": ("出租车", "交通费"),
    "报下来": ("报销",),
    "报了": ("报销",),
    "最晚": ("期限", "时限"),
    "什么时候交": ("提交时限",),
    "多久": ("期限", "时限"),
    "能留到明年": ("结转",),
    "攒": ("结转", "未休完"),
    "留到明年": ("结转",),
    "没用完": ("未休完", "结转"),
    # 产品 FAQ
    "多少钱": ("价格", "单价", "报价"),
    "单价": ("价格", "报价单"),
    "发票": ("电子发票", "发票原件"),
}


def expand_query(query: str) -> list[str]:
    """返回原查询 + 命中的同义扩展词（去重、保序）。"""
    expanded: list[str] = []
    for trigger, targets in EXPANSIONS.items():
        if trigger in query:
            for target in targets:
                if target not in expanded:
                    expanded.append(target)
    return expanded
