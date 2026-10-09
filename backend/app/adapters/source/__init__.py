"""货源采集适配器包。"""

from app.adapters.source.alibaba1688 import (
    Alibaba1688Adapter,
    CollectedProduct,
    CollectedSku,
    expand_spec_tree,
    normalize_spec_json,
    sku_code_from_spec,
)

__all__ = [
    "Alibaba1688Adapter",
    "CollectedProduct",
    "CollectedSku",
    "expand_spec_tree",
    "normalize_spec_json",
    "sku_code_from_spec",
]
