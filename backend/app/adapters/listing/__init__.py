"""上架适配器包（★ 红线 R2：只有本包的适配器能写店铺商品）。"""

from app.adapters.listing.base import (
    ListingAdapter,
    ListingMode,
    ListingPayload,
    ListingPublishResult,
    ListingStatus,
    ListingSkuPayload,
    ManualPackage,
    Platform,
)
from app.adapters.listing.factory import (
    LISTING_ADAPTER_REGISTRY,
    ListingAdapterFactory,
    get_listing_adapter,
    register_listing_adapter,
    resolve_listing_mode,
)
from app.adapters.listing.manual import ManualListingAdapter
from app.adapters.listing.mock import MockListingAdapter

__all__ = [
    "LISTING_ADAPTER_REGISTRY",
    "ListingAdapter",
    "ListingAdapterFactory",
    "ListingMode",
    "ListingPayload",
    "ListingPublishResult",
    "ListingStatus",
    "ListingSkuPayload",
    "ManualListingAdapter",
    "ManualPackage",
    "MockListingAdapter",
    "Platform",
    "get_listing_adapter",
    "register_listing_adapter",
    "resolve_listing_mode",
]
