"""Versioned, built-in token pricing used for local run estimates."""

from decimal import Decimal
from types import MappingProxyType

from embodied_runtime.observability import CharacterPrice, PricingCatalog, TokenPrice


BUILT_IN_PRICING = PricingCatalog(
    rates=MappingProxyType({
        ("openai-responses", "gpt-5.6-luna"): TokenPrice(
            input_per_million=Decimal("0.20"),
            cached_input_per_million=Decimal("0.02"),
            output_per_million=Decimal("1.20"),
            cache_write_per_million=Decimal("0.25"),
            max_input_tokens=272_000,
        ),
    }),
    identity="public-built-in-pricing-2026-10-02",
    character_rates=MappingProxyType({
        ("elevenlabs", "eleven_flash_v2_5"): CharacterPrice(
            usd_per_thousand=Decimal("0.05")
        ),
    }),
)
