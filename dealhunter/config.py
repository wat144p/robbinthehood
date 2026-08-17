"""
Loads ``config.yaml`` into lightly-typed objects.

The philosophy here is "thin wrapper, not a schema library": the YAML is the
source of truth and mostly passes straight through as dicts, but the pieces
that are easy to get wrong - regions, model floors - get real classes with
validation, so a typo in the config fails at startup instead of quietly
scoring everything wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import KeyboardLayout, Region

# Repo root, i.e. the directory containing config.yaml
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class RegionConfig:
    """Tax, currency and risk settings for one destination country."""

    code: Region
    enabled: bool
    display: str
    flag: str
    currency: str
    vat_in_sticker: float          # informational; never subtracted
    tax_added_at_checkout: bool
    risk_premium: float
    default_keyboard: KeyboardLayout
    checkout_tax_rates: dict[str, float]
    checkout_tax_fallback: float
    preferred_jurisdictions: list[str] = field(default_factory=list)

    def tax_rate_for(self, jurisdiction: str | None) -> tuple[float, str | None, bool]:
        """Resolve the checkout tax rate for a sub-national jurisdiction.

        Returns ``(rate, jurisdiction_used, was_assumed)``. When the source did
        not reveal a state/province we fall back to a deliberately pessimistic
        rate and mark it assumed, so an alert can say so out loud rather than
        quoting a landed figure that only holds in Alberta.
        """
        if not self.tax_added_at_checkout:
            return 0.0, None, False

        if jurisdiction:
            key = jurisdiction.strip().upper()
            if key in self.checkout_tax_rates:
                return self.checkout_tax_rates[key], key, False

        return self.checkout_tax_fallback, jurisdiction, True


@dataclass
class KnownModel:
    """A model we track a historical price floor for."""

    key: str
    display: str
    patterns: list[re.Pattern[str]]
    floor_usd: float
    priority: bool = False
    priority_alert_at_or_below_usd: float | None = None
    notes: str = ""

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in self.patterns)


@dataclass
class Config:
    """The whole configuration, as loaded from disk."""

    budget: dict[str, Any]
    hard_filters: dict[str, Any]
    regions: dict[Region, RegionConfig]
    fx: dict[str, Any]
    scoring: dict[str, Any]
    notification: dict[str, Any]
    known_models: list[KnownModel]
    priority_rules: dict[str, Any]
    # The `sources:` block, passed through as-is. Source modules read their own
    # sub-block, so adding a source never means touching this loader.
    raw_sources: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    # -- convenience accessors ---------------------------------------------

    def region(self, code: Region) -> RegionConfig:
        try:
            return self.regions[code]
        except KeyError as exc:  # pragma: no cover - config error, fails loudly
            raise KeyError(f"Region {code} is missing from config.yaml") from exc

    def enabled_regions(self) -> list[Region]:
        return [code for code, cfg in self.regions.items() if cfg.enabled]

    def model_for_title(self, text: str) -> KnownModel | None:
        """First model whose pattern matches. Config order decides precedence,
        so the most specific entries belong at the top of `known_models`."""
        lowered = text.lower()
        for model in self.known_models:
            if model.matches(lowered):
                return model
        return None

    def model_by_key(self, key: str) -> KnownModel | None:
        return next((m for m in self.known_models if m.key == key), None)


def load_config(path: str | Path | None = None) -> Config:
    """Read and validate config.yaml."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    regions: dict[Region, RegionConfig] = {}
    for code_str, block in (raw.get("regions") or {}).items():
        try:
            code = Region(code_str)
        except ValueError as exc:
            raise ValueError(
                f"Unknown region '{code_str}' in config.yaml. "
                f"Valid regions: {[r.value for r in Region]}"
            ) from exc

        # YAML 1.1 turns bare ON/OFF/NO into booleans - the Ontario key is
        # quoted in config.yaml for exactly this reason. Coerce defensively so
        # a future edit that drops the quotes doesn't produce a `True` key.
        tax_rates = {
            str(k).upper(): float(v)
            for k, v in (block.get("checkout_tax_rates") or {}).items()
        }

        regions[code] = RegionConfig(
            code=code,
            enabled=bool(block.get("enabled", True)),
            display=block.get("display", code.value),
            flag=block.get("flag", ""),
            currency=block["currency"],
            vat_in_sticker=float(block.get("vat_in_sticker", 0.0)),
            tax_added_at_checkout=bool(block.get("tax_added_at_checkout", False)),
            risk_premium=float(block.get("risk_premium", 0.0)),
            default_keyboard=KeyboardLayout(block.get("default_keyboard", "UNVERIFIED")),
            checkout_tax_rates=tax_rates,
            checkout_tax_fallback=float(block.get("checkout_tax_fallback", 0.0)),
            preferred_jurisdictions=[
                str(j).upper() for j in (block.get("preferred_jurisdictions") or [])
            ],
        )

    known_models = [
        KnownModel(
            key=entry["key"],
            display=entry["display"],
            patterns=[re.compile(p, re.IGNORECASE) for p in entry["patterns"]],
            floor_usd=float(entry["floor_usd"]),
            priority=bool(entry.get("priority", False)),
            priority_alert_at_or_below_usd=(
                float(entry["priority_alert_at_or_below_usd"])
                if entry.get("priority_alert_at_or_below_usd") is not None
                else None
            ),
            notes=entry.get("notes", ""),
        )
        for entry in (raw.get("known_models") or [])
    ]

    config = Config(
        budget=raw["budget"],
        hard_filters=raw["hard_filters"],
        regions=regions,
        fx=raw["fx"],
        scoring=raw["scoring"],
        notification=raw["notification"],
        known_models=known_models,
        priority_rules=raw.get("priority_rules") or {},
        raw_sources=raw.get("sources") or {},
        path=config_path,
    )

    _validate(config)
    return config


def _validate(config: Config) -> None:
    """Catch the config mistakes that would silently corrupt scoring."""
    # Every enabled region needs an FX fallback rate, or an offline run blows up.
    fallbacks = config.fx.get("fallback_rates_to_usd") or {}
    for code in config.enabled_regions():
        currency = config.region(code).currency
        if currency not in fallbacks:
            raise ValueError(
                f"Region {code.value} uses {currency} but fx.fallback_rates_to_usd "
                f"has no entry for it."
            )

    # The seven base scoring components are meant to total exactly 100. If a
    # weight is edited without rebalancing, say so rather than silently
    # producing scores that can never reach the alert threshold.
    scoring = config.scoring
    base_total = sum(
        [
            scoring["vram"]["max_points"],
            scoring["bandwidth"]["max_points"],
            scoring["panel"]["max_points"],
            scoring["system_ram"]["max_points"],
            scoring["storage"]["max_points"],
            scoring["tgp"]["max_points"],
            scoring["condition"]["max_points"],
        ]
    )
    if abs(base_total - 100) > 1e-6:
        raise ValueError(
            f"Base scoring components sum to {base_total}, expected 100. "
            f"Rebalance scoring.* max_points in config.yaml."
        )

    if config.budget["target_low_usd"] > config.budget["target_high_usd"]:
        raise ValueError("budget.target_low_usd is above budget.target_high_usd")

    if config.budget["target_high_usd"] > config.budget["hard_ceiling_usd"]:
        raise ValueError("budget.target_high_usd is above budget.hard_ceiling_usd")
