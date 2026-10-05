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
        ("openai-responses", "gpt-5.6-sol"): TokenPrice(
            input_per_million=Decimal("4.00"),
            cached_input_per_million=Decimal("0.40"),
            output_per_million=Decimal("20.00"),
            cache_write_per_million=Decimal("5.00"),
            max_input_tokens=272_000,
        ),
        ("openai-responses", "gpt-6.1-sol"): TokenPrice(
            input_per_million=Decimal("2.00"),
            cached_input_per_million=Decimal("0.10"),
            output_per_million=Decimal("10.00"),
            cache_write_per_million=Decimal("2.50"),
            max_input_tokens=272_000,
        ),
        ("openai-responses", "gpt-6-luna"): TokenPrice(
            input_per_million=Decimal("0.10"),
            cached_input_per_million=Decimal("0.01"),
            output_per_million=Decimal("0.50"),
            cache_write_per_million=Decimal("0.125"),
            max_input_tokens=272_000,
        ),
        ("openai-responses", "gpt-6-astra"): TokenPrice(
            input_per_million=Decimal("10.00"),
            cached_input_per_million=Decimal("1.00"),
            output_per_million=Decimal("50.00"),
            cache_write_per_million=Decimal("12.50"),
            max_input_tokens=272_000,
        ),
    }),
    identity="public-built-in-pricing-2026-10-05",
    character_rates=MappingProxyType({
        ("elevenlabs", "eleven_flash_v2_5"): CharacterPrice(
            usd_per_thousand=Decimal("0.05")
        ),
    }),
)
