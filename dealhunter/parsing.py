"""
Spec extraction from listing text, and the spec-trap validators.

Marketplace listings lie, mostly by omission. Every validator in this module
exists because a specific style of misleading listing has burned a real search:

  1. "RTX 5070" with no capacity  -> could be 8 GB or 12 GB. Never assume.
  2. Desktop specs on a laptop    -> "RTX 5070 Ti 16GB" is a desktop card.
  3. Dock storage sold as capacity-> "2TB (1TB SSD & 1TB Docking Station)".
  4. Resolution naming            -> WUXGA looks premium, is 1920x1200.
  5. Panel type absent            -> never infer OLED from a model family.
  6. Single-channel RAM           -> one SODIMM, a real performance trap.

The guiding principle throughout: **an unknown is not a low value.** When we
cannot determine something we record `None`, raise a flag, and let the scorer
apply a conservative default. We never quietly guess in the listing's favour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import hardware
from .config import Config
from .models import Flag, ParsedSpecs, PanelType

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# Capacities that are plausible as laptop VRAM. Anything outside this set that
# sits next to a GPU name is system RAM or storage, not video memory.
PLAUSIBLE_VRAM_GB = {4, 6, 8, 10, 12, 16, 24}

# For the two models that ship in more than one memory configuration, these are
# the only capacities we will accept from a listing.
VALID_VRAM_VARIANTS: dict[str, set[int]] = {
    "RTX 5070": {8, 12},
    "RTX 3080": {8, 16},
}

PLAUSIBLE_SYSTEM_RAM_GB = {8, 12, 16, 24, 32, 48, 64, 96, 128}


def _find_all_gb(text: str) -> list[tuple[int, int, int]]:
    """Every ``N GB`` mention as ``(value, start, end)``, in document order."""
    return [
        (int(m.group(1)), m.start(), m.end())
        for m in re.finditer(r"\b(\d{1,4})\s*gb\b", text)
    ]


# ---------------------------------------------------------------------------
# GPU and VRAM
# ---------------------------------------------------------------------------


def parse_gpu(text: str, specs: ParsedSpecs) -> None:
    """Identify the GPU and settle its VRAM.

    The reference table in `hardware.py` is authoritative. A capacity stated in
    the listing is used only to (a) resolve the two genuinely ambiguous models
    and (b) catch sellers quoting desktop specs. This is deliberate: a title
    reading "RTX 4060 16GB 1TB" means 16 GB of *system* RAM, and trusting the
    listing over the table would score it as if it had 16 GB of VRAM.
    """
    found = hardware.find_gpu(text)
    if not found:
        return

    gpu_name, generation = found
    specs.gpu_model = gpu_name
    specs.gpu_generation = generation
    specs.memory_bandwidth_gbs = hardware.bandwidth_for(gpu_name)
    if specs.memory_bandwidth_gbs is None:
        specs.add_flag(Flag.UNVERIFIED_BANDWIDTH)

    stated = _stated_vram_near_gpu(text, gpu_name)
    reference = hardware.vram_for(gpu_name)

    if reference is not None:
        # Unambiguous model: the table wins, full stop.
        specs.vram_gb = reference
        specs.vram_verified = True

        # ...but if the listing quotes the desktop part's capacity, say so.
        if stated is not None and hardware.looks_like_desktop_specs(gpu_name, stated):
            specs.add_flag(Flag.DESKTOP_GPU_SUSPECTED)
        return

    # Ambiguous model (RTX 5070: 8 or 12 GB; RTX 3080: 8 or 16 GB).
    valid = VALID_VRAM_VARIANTS.get(gpu_name, set())
    if stated is not None and stated in valid:
        specs.vram_gb = stated
        specs.vram_verified = True
        # The 12 GB 5070 is a new variant whose open-box pricing is untracked,
        # so a confirmed sighting is worth surfacing on its own merits.
        if gpu_name == "RTX 5070" and stated == 12:
            specs.add_flag(Flag.RTX_5070_12GB)
        return

    # No usable capacity. Score at the pessimistic tier and demand human eyes.
    specs.vram_gb = hardware.pessimistic_vram_for(gpu_name)
    specs.vram_verified = False
    specs.add_flag(Flag.UNVERIFIED_VRAM)


def _stated_vram_near_gpu(text: str, gpu_name: str) -> int | None:
    """Capacity written immediately beside the GPU name, if any.

    Accepts both orders - "RTX 5070 Ti 12GB" and "12GB RTX 5070 Ti" - and
    ignores anything that isn't a plausible VRAM figure.
    """
    # Rebuild a tolerant pattern for the specific GPU we matched, so that
    # "RTX5070Ti" and "GeForce RTX 5070 Ti" both anchor correctly.
    number = gpu_name.split()[1]
    ti = " Ti" in gpu_name
    anchor = re.compile(
        rf"\b(?:geforce\s+)?rtx\s*[-]?\s*{number}\s*{'ti' if ti else '(?!\\s*ti)'}\b",
        re.IGNORECASE,
    )
    match = anchor.search(text)
    if not match:
        return None

    # Look forward a short distance: "RTX 5070 Ti Laptop GPU 12GB GDDR7".
    after = text[match.end(): match.end() + 40]
    forward = re.search(
        r"^[\s,\-–(]*(?:laptop|mobile|gpu|graphics|video)?\s*[\s,\-–(]*(\d{1,2})\s*gb",
        after,
        re.IGNORECASE,
    )
    if forward:
        value = int(forward.group(1))
        if value in PLAUSIBLE_VRAM_GB:
            return value

    # Look back a short distance: "12GB RTX 5070 Ti".
    before = text[max(0, match.start() - 20): match.start()]
    backward = re.search(r"(\d{1,2})\s*gb\s*[\s,\-–]*$", before, re.IGNORECASE)
    if backward:
        value = int(backward.group(1))
        if value in PLAUSIBLE_VRAM_GB:
            return value

    return None


# ---------------------------------------------------------------------------
# System RAM
# ---------------------------------------------------------------------------


def parse_system_ram(text: str, specs: ParsedSpecs) -> None:
    """Extract installed system memory and detect single-channel configs.

    Strategy, in order of confidence:
      1. A capacity explicitly tied to a memory keyword ("32GB DDR5", "RAM: 32GB").
      2. Otherwise, the first plausible bare ``N GB`` that wasn't already
         consumed as VRAM. This is what makes the common terse title format
         "RTX 5060 32GB 1TB" parse correctly.
    """
    # 1. Keyword-anchored, highest confidence.
    keyword_patterns = [
        r"(\d{1,3})\s*gb\s*(?:ddr\d[a-z0-9]*|lpddr\d[a-z0-9]*|so-?dimm|ram|memory)",
        r"(?:ram|memory)\s*[:\-]?\s*(\d{1,3})\s*gb",
        r"(\d{1,3})\s*gb\s*\(\s*\d\s*x\s*\d{1,3}\s*gb\s*\)",   # "32GB (2x16GB)"
    ]
    for pattern in keyword_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if value in PLAUSIBLE_SYSTEM_RAM_GB:
                specs.system_ram_gb = value
                break

    # 2. Positional fallback. Skip the capacity that was consumed as VRAM -
    #    see the docstring on parse_gpu for why this test is `== vram_gb`.
    if specs.system_ram_gb is None:
        vram_consumed = False
        for value, _start, _end in _find_all_gb(text.lower()):
            if (
                not vram_consumed
                and specs.vram_gb is not None
                and value == specs.vram_gb
            ):
                vram_consumed = True     # this one was the video memory
                continue
            if value in PLAUSIBLE_SYSTEM_RAM_GB:
                specs.system_ram_gb = value
                break

    specs.single_channel = detect_single_channel(text)
    if specs.single_channel:
        specs.add_flag(Flag.SINGLE_CHANNEL_RAM)


# "1x16GB", "1 x 32 GB", "single channel", "one SODIMM occupied".
_SINGLE_CHANNEL_PATTERNS = [
    re.compile(r"\bsingle[\s\-]?channel\b", re.IGNORECASE),
    re.compile(r"\b1\s*x\s*\d{1,3}\s*gb\b", re.IGNORECASE),
    re.compile(r"\b(?:one|1)\s+so-?dimm\b", re.IGNORECASE),
    re.compile(r"\b1\s+of\s+2\s+(?:slots?|so-?dimms?)\b", re.IGNORECASE),
]


def detect_single_channel(text: str) -> bool:
    """True when the listing indicates a single memory stick.

    Worth a penalty rather than a rejection: it is fixable for the price of a
    SODIMM, but it halves memory bandwidth until you fix it, which directly
    slows local token generation.
    """
    return any(pattern.search(text) for pattern in _SINGLE_CHANNEL_PATTERNS)


# ---------------------------------------------------------------------------
# Storage — and the docking-station trap
# ---------------------------------------------------------------------------

# Words that mean "this capacity is an accessory, not internal M.2 storage".
_DOCK_KEYWORDS = (
    "dock", "docking", "dock set", "external", "portable drive", "hub",
    "usb drive", "flash drive", "enclosure", "expansion card", "sd card",
    "micro sd", "memory card",
)

# Leading \b matters: without it, "512GB" backtracks into a match on "12GB".
_STORAGE_TOKEN = re.compile(r"\b(\d{1,4}(?:\.\d)?)\s*(tb|gb)\b", re.IGNORECASE)

# A capacity's descriptive text runs until the next list separator. Bounding it
# this way stops "1TB SSD & 1TB Docking Station" from tagging the SSD as dock
# storage just because the word "docking" appears a few characters later.
_SEGMENT_END = re.compile(r"[&+,/;()]|\swith\s|\splus\s")

# Only sizes that make sense as an internal drive. Filters out "16GB RAM".
_MIN_INTERNAL_STORAGE_GB = 128


@dataclass
class _StorageCandidate:
    gb: int
    start: int
    end: int
    is_dock: bool
    in_parens: bool


def parse_storage(text: str, specs: ParsedSpecs) -> None:
    """Extract INTERNAL storage only, discarding accessory capacity.

    Retailers bundle a dock and then advertise the sum:

        "2TB Storage (1TB SSD & 1TB Docking Station)"
        "1TB SSD + 1TB Dock Set"

    Neither machine has 2 TB inside it. We identify dock-attributed capacity,
    drop any headline figure that is merely the sum of the parts, and keep the
    largest genuine internal drive.
    """
    lowered = text.lower()
    candidates: list[_StorageCandidate] = []

    for match in _STORAGE_TOKEN.finditer(lowered):
        value, unit = float(match.group(1)), match.group(2).lower()
        gb = int(value * 1000) if unit == "tb" else int(value)

        if gb < _MIN_INTERNAL_STORAGE_GB:
            continue    # too small to be a drive; almost certainly RAM or VRAM

        # A GB figure only counts as storage if something nearby says so, or if
        # it was written in TB (nobody measures RAM in terabytes).
        context = _segment_after(lowered, match.end())
        looks_like_storage = unit == "tb" or re.search(
            r"\b(ssd|nvme|pcie|m\.?2|storage|hdd|hard drive|emmc)\b", context
        )
        if not looks_like_storage:
            continue

        candidates.append(
            _StorageCandidate(
                gb=gb,
                start=match.start(),
                end=match.end(),
                is_dock=any(word in context for word in _DOCK_KEYWORDS),
                in_parens=_inside_parentheses(lowered, match.start()),
            )
        )

    if not candidates:
        return

    dock = [c for c in candidates if c.is_dock]
    internal = [c for c in candidates if not c.is_dock]

    if dock:
        specs.add_flag(Flag.DOCK_STORAGE_TRAP)
        specs.dock_storage_gb = sum(c.gb for c in dock)

        # "2TB Storage (1TB SSD & 1TB Docking Station)" - when the breakdown is
        # parenthesised, it is authoritative and the headline outside is just
        # the marketing sum.
        parenthesised = [c for c in internal if c.in_parens]
        if parenthesised:
            internal = parenthesised
        else:
            # No parenthesised breakdown, so look for a headline figure that is
            # both larger than every other figure AND exactly their sum. The
            # "larger than" test is what keeps "1TB SSD + 1TB Dock Set" intact:
            # there, 1 TB equals the sum of the others but does not exceed them,
            # so it is a genuine drive rather than an aggregate.
            internal = [
                c
                for c in internal
                if not _is_aggregate_of(c, candidates)
            ]

    if internal:
        specs.storage_gb = max(c.gb for c in internal)

    specs.free_m2_slot = detect_free_m2_slot(lowered)


def _is_aggregate_of(
    candidate: _StorageCandidate, all_candidates: list[_StorageCandidate]
) -> bool:
    """True when `candidate` is a marketing total rather than a real drive."""
    others = [c for c in all_candidates if c is not candidate]
    if not others:
        return False
    return candidate.gb == sum(c.gb for c in others) and candidate.gb > max(
        c.gb for c in others
    )


def _segment_after(text: str, index: int, limit: int = 40) -> str:
    """Text following `index`, truncated at the next list separator.

    Capacity descriptions in a title are a comma/ampersand-separated list, so
    honouring those boundaries stops one item's keywords bleeding into the next.
    """
    window = text[index: index + limit]
    boundary = _SEGMENT_END.search(window)
    return window[: boundary.start()] if boundary else window


def _inside_parentheses(text: str, index: int) -> bool:
    """Cheap check: is `index` inside a ( ... ) group?"""
    opens = text.count("(", 0, index)
    closes = text.count(")", 0, index)
    return opens > closes


_FREE_M2_PATTERNS = [
    re.compile(r"\b(?:free|empty|spare|open|available|extra|additional|second|2nd)\s+"
               r"m\.?2\b", re.IGNORECASE),
    re.compile(r"\bm\.?2\s+slot\s+(?:free|empty|open|available)\b", re.IGNORECASE),
    re.compile(r"\b\d\s*x\s*m\.?2\s+slots?\b.*\b(?:one|1)\s+(?:free|empty)\b",
               re.IGNORECASE),
]


def detect_free_m2_slot(text: str) -> bool:
    """True only when a spare M.2 slot is explicitly confirmed.

    Worth 2 bonus points because it means a cheap 1 TB config can be grown
    without discarding the drive it shipped with.
    """
    return any(pattern.search(text) for pattern in _FREE_M2_PATTERNS)


# ---------------------------------------------------------------------------
# Display: resolution, panel type, brightness, refresh
# ---------------------------------------------------------------------------

# Marketing names mapped to actual pixel counts. The 1920-wide entries are the
# trap: "WUXGA" and "FHD+" sit next to "16-inch gaming laptop" and read as
# premium at a glance, but they fail the 1440 vertical minimum outright.
RESOLUTION_ALIASES: dict[str, tuple[int, int]] = {
    "wqxga": (2560, 1600),
    "qhd+": (2560, 1600),
    "wqhd+": (2560, 1600),
    "2.5k": (2560, 1600),
    "2,5k": (2560, 1600),
    "qhd": (2560, 1440),
    "wqhd": (2560, 1440),
    "1440p": (2560, 1440),
    "2k": (2560, 1440),
    "uhd": (3840, 2160),
    "4k": (3840, 2160),
    "wuxga": (1920, 1200),      # FAILS - 1200 < 1440
    "fhd+": (1920, 1200),       # FAILS
    "fhd": (1920, 1080),        # FAILS
    "1080p": (1920, 1080),      # FAILS
}

_EXPLICIT_RESOLUTION = re.compile(r"\b(\d{3,4})\s*[x×*]\s*(\d{3,4})\b")


def parse_display(text: str, specs: ParsedSpecs) -> None:
    """Resolution, panel type, brightness and refresh rate.

    Explicit pixel dimensions always beat a marketing alias. When both are
    present and disagree, the pixels win - they are what you actually get.
    """
    lowered = text.lower()

    explicit = _EXPLICIT_RESOLUTION.search(lowered)
    if explicit:
        width, height = int(explicit.group(1)), int(explicit.group(2))
        # Guard against matching something that isn't a resolution at all.
        if 1000 <= width <= 4096 and 600 <= height <= 2400:
            specs.resolution_w, specs.resolution_h = width, height

    if specs.resolution_h is None:
        # Longest alias first, so "qhd+" is tried before "qhd" and "2.5k"
        # before "2k". Getting this backwards costs 160 vertical pixels.
        #
        # The trailing guard is `(?!\w)` rather than `\b` because several
        # aliases end in a non-word character: `\bqhd\+\b` requires a word
        # character immediately after the plus, so it never matches the very
        # common "QHD+ 240Hz" and silently falls through to plain "QHD".
        for alias in sorted(RESOLUTION_ALIASES, key=len, reverse=True):
            if re.search(rf"\b{re.escape(alias)}(?!\w)", lowered):
                specs.resolution_w, specs.resolution_h = RESOLUTION_ALIASES[alias]
                break

    specs.panel_type = parse_panel_type(lowered, specs)

    nits = re.search(r"(\d{3,4})\s*[- ]?nits?\b", lowered)
    if nits:
        specs.nits = int(nits.group(1))

    refresh = re.search(r"(\d{2,3})\s*hz\b", lowered)
    if refresh:
        specs.refresh_hz = int(refresh.group(1))

    inches = re.search(r"\b(1[2-8](?:\.\d)?)\s*(?:\"|''|-?\s*inch\b|-?\s*in\b)", lowered)
    if inches:
        specs.screen_inches = float(inches.group(1))


# Model families that were NEVER offered with an OLED panel. A listing claiming
# otherwise is either a copy-paste error or a deliberate embellishment; either
# way we refuse to score it as OLED.
#
# The Legion Pro 5 16IRX9 (Gen 9) is the documented case: every configuration
# shipped IPS. OLED only arrived with Gen 10.
KNOWN_NO_OLED_MODELS = ("16irx9",)


def parse_panel_type(text: str, specs: ParsedSpecs) -> PanelType:
    """Determine panel technology. Never inferred from the model family.

    An unstated panel is recorded as UNVERIFIED and scored as IPS. Model
    families do not imply OLED - the same model number frequently ships with
    both, and some families that "feel" premium never offered OLED at all.
    """
    claims_oled = bool(re.search(r"\b(?:am)?oled\b", text))

    if claims_oled and any(model in text for model in KNOWN_NO_OLED_MODELS):
        # Known-impossible combination. Treat as IPS and flag for a human.
        specs.add_flag(Flag.UNVERIFIED_PANEL)
        return PanelType.IPS

    if claims_oled:
        return PanelType.OLED
    if re.search(r"\bips\b", text):
        return PanelType.IPS
    if re.search(r"\bva\s*panel\b|\bva\s*display\b", text):
        return PanelType.VA

    specs.add_flag(Flag.UNVERIFIED_PANEL)
    return PanelType.UNVERIFIED


# ---------------------------------------------------------------------------
# GPU total graphics power (TGP)
# ---------------------------------------------------------------------------

# Wattage figures that belong to something other than the GPU.
_NON_TGP_CONTEXT = re.compile(
    r"\b(adapter|charger|psu|power supply|brick|usb-?c pd|battery|whr|wh\b)\b",
    re.IGNORECASE,
)

# TGP anchored to an explicit label, e.g. "TGP 140W", "115W TGP", "@115W".
_TGP_LABELLED = [
    re.compile(r"\btgp\s*[:\-]?\s*(\d{2,3})\s*w\b", re.IGNORECASE),
    re.compile(r"(\d{2,3})\s*w\s*tgp\b", re.IGNORECASE),
    re.compile(r"@\s*(\d{2,3})\s*w\b", re.IGNORECASE),
    re.compile(r"\btotal graphics power\s*[:\-]?\s*(\d{2,3})\s*w\b", re.IGNORECASE),
    re.compile(r"\b(?:max(?:imum)?\s+)?graphics power\s*[:\-]?\s*(\d{2,3})\s*w\b",
               re.IGNORECASE),
]

# A plausible laptop dGPU power envelope. Anything outside is a power brick.
_TGP_RANGE = (35, 200)


def parse_tgp(text: str, specs: ParsedSpecs) -> None:
    """Extract GPU total graphics power.

    TGP matters more than the model name: a 115 W xx60 genuinely beats an 85 W
    xx70 in sustained load. Unknown TGP is scored mid-range and flagged rather
    than assumed, because it swings 8 points.
    """
    for pattern in _TGP_LABELLED:
        match = pattern.search(text)
        if match:
            watts = int(match.group(1))
            if _TGP_RANGE[0] <= watts <= _TGP_RANGE[1]:
                specs.tgp_watts = watts
                return

    # Unlabelled wattage near the GPU name, e.g. "RTX 5060 115W".
    if specs.gpu_model:
        number = specs.gpu_model.split()[1]
        near = re.search(
            rf"rtx\s*{number}[^,;.]{{0,40}}?(\d{{2,3}})\s*w\b", text, re.IGNORECASE
        )
        if near and not _NON_TGP_CONTEXT.search(near.group(0)):
            watts = int(near.group(1))
            if _TGP_RANGE[0] <= watts <= _TGP_RANGE[1]:
                specs.tgp_watts = watts
                return

    specs.add_flag(Flag.UNVERIFIED_TGP)


# ---------------------------------------------------------------------------
# CPU (informational only — not scored)
# ---------------------------------------------------------------------------

# The `(?=\w*\d)` lookahead requires the model token to contain a digit. Without
# it, "Ryzen 9 RTX 5070 Ti" — a title that names the GPU but not the CPU model —
# parses as a CPU called "RTX".
_CPU_PATTERNS = [
    re.compile(r"\b(?:amd\s+)?ryzen\s+(?:ai\s+)?\d\s+(?:ai\s+)?(?=\w*\d)\w{3,7}\b",
               re.IGNORECASE),
    re.compile(r"\b(?:intel\s+)?core\s+ultra\s+\d\s+(?=\w*\d)\w{3,7}\b", re.IGNORECASE),
    re.compile(r"\bultra\s+\d\s+\d{3}[a-z]{1,2}\b", re.IGNORECASE),
    re.compile(r"\bi[3579][- ]\d{4,5}[a-z]{0,2}\b", re.IGNORECASE),
]


def parse_cpu(text: str, specs: ParsedSpecs) -> None:
    for pattern in _CPU_PATTERNS:
        match = pattern.search(text)
        if match:
            specs.cpu = " ".join(match.group(0).split()).title()
            return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_listing(text: str, config: Config | None = None) -> ParsedSpecs:
    """Run every parser over a listing's title (+ description) text.

    `config` is optional so the parsers can be unit-tested standalone; pass it
    to get `model_key` / `model_display` populated from `known_models`.
    """
    specs = ParsedSpecs()
    lowered = text.lower()

    # Order matters: GPU first, because RAM and TGP parsing both consult it.
    parse_gpu(lowered, specs)
    parse_system_ram(lowered, specs)
    parse_storage(lowered, specs)
    parse_display(lowered, specs)
    parse_tgp(lowered, specs)
    parse_cpu(text, specs)

    if config is not None:
        model = config.model_for_title(lowered)
        if model:
            specs.model_key = model.key
            specs.model_display = model.display
            if model.priority:
                specs.add_flag(Flag.PRIORITY_TARGET)

    return specs
