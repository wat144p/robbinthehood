"""
Foreign exchange rates, with a 12-hour on-disk cache.

Design notes:

* Rates are always expressed as **local -> USD**, i.e. ``local * rate = USD``.
  GBP is about 1.27, SEK about 0.095. USD is always exactly 1.0.
* Every fetch records where it came from and when. That triple (rate, source,
  timestamp) gets written onto every stored listing so a comparison made weeks
  later against today's record is still honest.
* Nothing here raises on a network failure. We degrade: live -> cache (even a
  stale one) -> hardcoded fallback, and mark the run FX_STALE so you can tell.
* `FxRates` is a plain value object, so the scoring tests can construct fixed
  rates and never touch the network.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Currencies we actually need, derived from the seven forwarding regions.
SUPPORTED_CURRENCIES = ("USD", "CAD", "GBP", "EUR", "SEK", "AUD")


@dataclass
class FxRates:
    """A snapshot of local-currency -> USD rates."""

    rates: dict[str, float]
    source: str
    fetched_at: datetime
    is_stale: bool = False       # true when hardcoded fallbacks were used

    def to_usd(self, currency: str) -> float:
        currency = currency.upper()
        if currency == "USD":
            return 1.0
        try:
            return self.rates[currency]
        except KeyError as exc:
            raise KeyError(
                f"No FX rate for {currency}. Add it to fx.fallback_rates_to_usd "
                f"in config.yaml, or check the provider response."
            ) from exc

    def convert(self, amount: float, currency: str) -> float:
        return amount * self.to_usd(currency)

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(timezone.utc)) - self.fetched_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "rates": self.rates,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FxRates":
        return cls(
            rates={k: float(v) for k, v in data["rates"].items()},
            source=data["source"],
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
        )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
# Each provider is a function returning ``{currency: rate_to_usd}`` or raising.
# Adding a new one is a matter of writing the function and listing its name in
# `fx.providers` in config.yaml.
# ---------------------------------------------------------------------------


def _fetch_exchangerate_host(timeout: float = 10.0) -> dict[str, float]:
    """exchangerate.host — free, keyless, USD-based.

    Their payload is USD -> X, so we invert to get X -> USD.
    """
    import requests  # imported lazily so unit tests never need the dependency

    response = requests.get(
        "https://api.exchangerate.host/latest",
        params={"base": "USD", "symbols": ",".join(SUPPORTED_CURRENCIES)},
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    payload = response.json()

    usd_to_x = payload.get("rates") or {}
    if not usd_to_x:
        raise ValueError("exchangerate.host returned no rates")

    rates = {"USD": 1.0}
    for currency in SUPPORTED_CURRENCIES:
        if currency == "USD":
            continue
        value = usd_to_x.get(currency)
        if not value:
            raise ValueError(f"exchangerate.host omitted {currency}")
        rates[currency] = 1.0 / float(value)    # invert USD->X into X->USD
    return rates


def _fetch_ecb(timeout: float = 10.0) -> dict[str, float]:
    """European Central Bank daily reference feed — free, keyless, EUR-based.

    Published as EUR -> X. To get X -> USD we go via EUR:

        X -> USD  =  (EUR -> USD) / (EUR -> X)
    """
    import xml.etree.ElementTree as ET

    import requests

    response = requests.get(
        "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    root = ET.fromstring(response.content)
    # The rates live in nested <Cube currency="X" rate="Y"/> elements.
    eur_to: dict[str, float] = {"EUR": 1.0}
    for cube in root.iter():
        currency = cube.attrib.get("currency")
        rate = cube.attrib.get("rate")
        if currency and rate:
            eur_to[currency] = float(rate)

    if "USD" not in eur_to:
        raise ValueError("ECB feed had no USD rate")

    eur_to_usd = eur_to["USD"]
    rates = {"USD": 1.0}
    for currency in SUPPORTED_CURRENCIES:
        if currency == "USD":
            continue
        if currency not in eur_to:
            raise ValueError(f"ECB feed omitted {currency}")
        rates[currency] = eur_to_usd / eur_to[currency]
    return rates


PROVIDERS = {
    "exchangerate.host": _fetch_exchangerate_host,
    "ecb": _fetch_ecb,
}

USER_AGENT = (
    "robbin-the-hood/0.1 (personal gaming-laptop price tracker; "
    "contact via repo issues)"
)


# ---------------------------------------------------------------------------
# The cache-aware entry point
# ---------------------------------------------------------------------------


@dataclass
class FxService:
    """Fetches rates, honouring a 12-hour cache.

    Typical use::

        fx = FxService.from_config(config)
        rates = fx.get_rates()
        landed = compute_landed_cost(listing, config, rates)
    """

    cache_path: Path
    cache_hours: int = 12
    provider_names: list[str] = field(default_factory=lambda: ["exchangerate.host", "ecb"])
    fallback_rates: dict[str, float] = field(default_factory=dict)
    _cached: FxRates | None = None

    @classmethod
    def from_config(cls, config: Any) -> "FxService":
        from .config import PROJECT_ROOT

        block = config.fx
        cache_path = Path(block.get("cache_path", "data/fx_cache.json"))
        if not cache_path.is_absolute():
            cache_path = PROJECT_ROOT / cache_path

        return cls(
            cache_path=cache_path,
            cache_hours=int(block.get("cache_hours", 12)),
            provider_names=list(block.get("providers") or ["exchangerate.host"]),
            fallback_rates={
                k: float(v) for k, v in (block.get("fallback_rates_to_usd") or {}).items()
            },
        )

    # -- public ------------------------------------------------------------

    def get_rates(self, *, force_refresh: bool = False, offline: bool = False) -> FxRates:
        """Return usable rates, refreshing only when the cache has expired.

        `offline=True` skips the network entirely - handy for `--dry-run` and
        for tests.
        """
        if self._cached and not force_refresh and self._is_fresh(self._cached):
            return self._cached

        disk = self._read_cache()
        if disk and not force_refresh and self._is_fresh(disk):
            self._cached = disk
            return disk

        if not offline:
            fetched = self._fetch_live()
            if fetched:
                self._write_cache(fetched)
                self._cached = fetched
                return fetched

        # Every provider failed. A stale cache is still far better than a
        # hardcoded guess, so try that before falling back.
        if disk:
            log.warning(
                "FX providers unavailable; using cached rates from %s (age %s)",
                disk.fetched_at.isoformat(), disk.age(),
            )
            disk.is_stale = True
            self._cached = disk
            return disk

        reason = "offline mode" if offline else "every provider failed"
        log.warning(
            "FX: %s and no usable cache; falling back to the rates in config.yaml. "
            "Landed figures will be approximate and every listing is flagged FX_STALE.",
            reason,
        )
        fallback = FxRates(
            rates=dict(self.fallback_rates),
            source="config-fallback",
            fetched_at=datetime.now(timezone.utc),
            is_stale=True,
        )
        self._cached = fallback
        return fallback

    # -- internals ---------------------------------------------------------

    def _is_fresh(self, rates: FxRates) -> bool:
        return rates.age() < timedelta(hours=self.cache_hours)

    def _fetch_live(self) -> FxRates | None:
        """Try each configured provider in order; the first success wins."""
        for name in self.provider_names:
            provider = PROVIDERS.get(name)
            if not provider:
                log.warning("Unknown FX provider %r in config; skipping", name)
                continue
            try:
                rates = provider()
                log.info("Fetched FX rates from %s", name)
                return FxRates(
                    rates=rates, source=name, fetched_at=datetime.now(timezone.utc)
                )
            except Exception as exc:  # noqa: BLE001 - a bad FX run must not kill the run
                log.warning("FX provider %s failed: %s", name, exc)
        return None

    def _read_cache(self) -> FxRates | None:
        if not self.cache_path.exists():
            return None
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                return FxRates.from_dict(json.load(handle))
        except Exception as exc:  # noqa: BLE001 - a corrupt cache is not fatal
            log.warning("Could not read FX cache %s: %s", self.cache_path, exc)
            return None

    def _write_cache(self, rates: FxRates) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as handle:
                json.dump(rates.to_dict(), handle, indent=2)
        except OSError as exc:
            log.warning("Could not write FX cache %s: %s", self.cache_path, exc)


def static_rates(rates: dict[str, float], source: str = "test-fixture") -> FxRates:
    """Build a fixed rate set. Used by the unit tests and by `--dry-run`."""
    merged = {"USD": 1.0, **rates}
    return FxRates(rates=merged, source=source, fetched_at=datetime.now(timezone.utc))
