"""
Hardware reference tables for **laptop** GPUs.

Rule zero: never infer a laptop part's specs from its desktop namesake. They
are different chips with different memory configurations:

    Desktop RTX 5070 Ti : 16 GB, 8,960 shaders, 300 W
    Laptop  RTX 5070 Ti : 12 GB, 6,144 shaders, 140 W

    Desktop RTX 3080 Ti : 12 GB
    Laptop  RTX 3080 Ti : 16 GB

Everything in this module is hardcoded on purpose. Nothing here is derived,
guessed, or scraped - a wrong VRAM number silently poisons the single
heaviest scoring component, so these tables are the place to be pedantic.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# VRAM, in GB, for laptop parts
# ---------------------------------------------------------------------------
# `None` means the capacity is genuinely ambiguous for that model name and MUST
# be read off the specific listing. Two cases exist today:
#
#   RTX 3080  - shipped in 8 GB and 16 GB SKUs
#   RTX 5070  - since April 2026, an 8 GB and a 12 GB variant coexist. Same
#               GB206 die, same 4,608 CUDA cores, same 128-bit bus, same
#               384 GB/s. Only the memory module density changed (2 GB/16Gb
#               chips vs 3 GB/24Gb). Nothing in a spec sheet distinguishes
#               them except the stated capacity.
# ---------------------------------------------------------------------------
LAPTOP_GPU_VRAM_GB: dict[str, int | None] = {
    # -- Ampere (30-series) --
    "RTX 3050": 4,
    "RTX 3050 Ti": 4,
    "RTX 3060": 6,
    "RTX 3070": 8,
    "RTX 3070 Ti": 8,
    "RTX 3080": None,      # 8 GB or 16 GB depending on SKU - must verify
    "RTX 3080 Ti": 16,
    # -- Ada (40-series) --
    "RTX 4050": 6,
    "RTX 4060": 8,
    "RTX 4070": 8,
    "RTX 4080": 12,
    "RTX 4090": 16,
    # -- Blackwell (50-series) --
    "RTX 5050": 8,
    "RTX 5060": 8,
    "RTX 5070": None,      # 8 GB or 12 GB - THE trap, see parsing.py
    "RTX 5070 Ti": 12,
    "RTX 5080": 16,
    "RTX 5090": 24,
}

# When a bare "RTX 5070" or "RTX 3080" appears with no capacity stated, we
# score it at the pessimistic tier and flag it for manual confirmation rather
# than guessing in the listing's favour.
AMBIGUOUS_VRAM_FALLBACK_GB: dict[str, int] = {
    "RTX 5070": 8,
    "RTX 3080": 8,
}

# ---------------------------------------------------------------------------
# Memory bandwidth, GB/s, for laptop parts
# ---------------------------------------------------------------------------
# Token generation speed on local LLMs is bandwidth-bound, not capacity-bound,
# which is why this gets its own scoring component.
#
# The entries marked [SPEC] were given explicitly in the project brief and are
# authoritative. The rest are published reference figures at stock clocks and
# are close enough for a linear 10-point score, but if you are agonising over
# two listings a few points apart, verify the exact one you care about.
# ---------------------------------------------------------------------------
LAPTOP_GPU_BANDWIDTH_GBS: dict[str, int] = {
    "RTX 3050": 192,
    "RTX 3050 Ti": 192,
    "RTX 3060": 336,
    "RTX 3070": 448,       # [SPEC]
    "RTX 3070 Ti": 448,
    "RTX 3080": 448,
    "RTX 3080 Ti": 512,    # [SPEC]
    "RTX 4050": 192,
    "RTX 4060": 256,
    "RTX 4070": 256,
    "RTX 4080": 432,
    "RTX 4090": 576,
    "RTX 5050": 384,
    "RTX 5060": 384,       # [SPEC]
    "RTX 5070": 384,       # [SPEC] - identical for both the 8 GB and 12 GB variants
    "RTX 5070 Ti": 672,    # [SPEC]
    "RTX 5080": 768,
    "RTX 5090": 896,
}

# Desktop VRAM figures. We never score against these - they exist purely so a
# listing quoting desktop specs for a laptop part can be caught and flagged.
DESKTOP_GPU_VRAM_GB: dict[str, int] = {
    "RTX 3070": 8,
    "RTX 3080": 10,
    "RTX 3080 Ti": 12,
    "RTX 4060": 8,
    "RTX 4070": 12,
    "RTX 4080": 16,
    "RTX 4090": 24,
    "RTX 5060": 8,
    "RTX 5070": 12,
    "RTX 5070 Ti": 16,
    "RTX 5080": 16,
    "RTX 5090": 32,
}


# ---------------------------------------------------------------------------
# GPU name recognition
# ---------------------------------------------------------------------------
# Matches "RTX 5070 Ti", "RTX5070Ti", "GeForce RTX 4060", "rtx 3080 ti" etc.
# The optional "laptop"/"mobile" suffix is consumed so it doesn't confuse the
# Ti detection.
_GPU_PATTERN = re.compile(
    r"\b(?:geforce\s+)?rtx\s*[-]?\s*(\d{4})\s*(ti)?\b",
    re.IGNORECASE,
)


def normalise_gpu_name(number: str, is_ti: bool) -> str:
    """Turn a matched ('5070', True) into the canonical 'RTX 5070 Ti'."""
    return f"RTX {number}" + (" Ti" if is_ti else "")


def find_gpu(text: str) -> tuple[str, int] | None:
    """Find the first GPU mentioned in `text`.

    Returns ``(canonical_name, generation)`` or ``None``. Generation is the
    leading digit-pair of the model number: 3070 -> 30, 4060 -> 40, 5070 -> 50.
    """
    match = _GPU_PATTERN.search(text)
    if not match:
        return None

    number, ti = match.group(1), bool(match.group(2))
    name = normalise_gpu_name(number, ti)

    # Only accept names we actually have reference data for. An "RTX 2070" or
    # a typo'd "RTX 5170" should not silently enter the pipeline.
    if name not in LAPTOP_GPU_VRAM_GB:
        return None

    return name, int(number[:2])


def vram_for(gpu_name: str) -> int | None:
    """Reference VRAM for a laptop GPU. ``None`` where the model is ambiguous."""
    return LAPTOP_GPU_VRAM_GB.get(gpu_name)


def bandwidth_for(gpu_name: str) -> int | None:
    """Reference memory bandwidth in GB/s, or ``None`` if we have no entry."""
    return LAPTOP_GPU_BANDWIDTH_GBS.get(gpu_name)


def is_vram_ambiguous(gpu_name: str) -> bool:
    """True for models that ship in more than one memory configuration."""
    return gpu_name in LAPTOP_GPU_VRAM_GB and LAPTOP_GPU_VRAM_GB[gpu_name] is None


def pessimistic_vram_for(gpu_name: str) -> int | None:
    """The capacity to assume for an ambiguous model when none is stated.

    Always the smaller variant. A listing that turns out to have more VRAM than
    we assumed is a pleasant surprise; the reverse costs money.
    """
    return AMBIGUOUS_VRAM_FALLBACK_GB.get(gpu_name)


def looks_like_desktop_specs(gpu_name: str, stated_vram_gb: int) -> bool:
    """True when a stated capacity matches the desktop part, not the laptop one.

    Catches sellers who copy-pasted a desktop spec sheet, e.g. "RTX 5070 Ti
    16GB" (the laptop part is 12 GB) or "RTX 3080 Ti 12GB" (laptop is 16 GB).
    """
    laptop = LAPTOP_GPU_VRAM_GB.get(gpu_name)
    desktop = DESKTOP_GPU_VRAM_GB.get(gpu_name)
    if laptop is None or desktop is None:
        return False
    return stated_vram_gb == desktop and stated_vram_gb != laptop
