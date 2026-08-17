"""
Test fixtures: listing titles in the style real marketplaces actually produce.

Each entry is annotated with what it is meant to exercise. Where a title looks
strange — inconsistent spacing, "Storage" used to mean a bundled dock, specs in
a different order — that is deliberate. Marketplace titles are written by
sellers optimising for search, not by anyone trying to be parseable.
"""

from __future__ import annotations

from dealhunter.models import Condition, KeyboardLayout, Listing, Region

# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

TITLES = {
    # -- clean, well-specified listings --------------------------------------
    "helios_neo_16s_openbox": (
        "Acer Predator Helios Neo 16S AI 16\" 2560x1600 240Hz OLED G-SYNC 500 nits "
        "Intel Core Ultra 9 275HX RTX 5070 Ti 12GB 140W 32GB DDR5 1TB SSD - Open Box"
    ),
    "legion_pro_5_oled": (
        "Lenovo Legion Pro 5 16 83LT000MUS 16\" WQXGA OLED 165Hz 500nit "
        "Ryzen 7 8745HX RTX 5060 8GB @115W 32GB 1TB SSD Gaming Laptop"
    ),
    "msi_vector_16hx": (
        "MSI Vector 16 HX 16-inch QHD+ 240Hz IPS Ryzen 9 7945HX "
        "GeForce RTX 5070 Ti 12GB 140W TGP 16GB DDR5 1TB NVMe"
    ),
    "legion_5i_15": (
        "Lenovo Legion 5i 15.1\" 2560x1600 OLED 165Hz Intel Core Ultra 7 255HX "
        "RTX 5060 32GB 1TB SSD"
    ),
    "legion_5_pro_16ach6h_used": (
        "Lenovo Legion 5 Pro 16ACH6H 16\" QHD+ 165Hz IPS Ryzen 7 5800H "
        "RTX 3070 8GB 16GB RAM 1TB SSD"
    ),

    # -- trap 1: ambiguous "RTX 5070" with no capacity -----------------------
    "ambiguous_5070": (
        "GIGABYTE Aero X16 16\" 2560x1600 165Hz IPS 400 nits Ryzen AI 7 350 "
        "RTX 5070 32GB 1TB SSD Open Box"
    ),
    # -- trap 1b: the 12 GB variant, explicitly stated -----------------------
    "explicit_5070_12gb": (
        "ASUS ROG Strix G16 16\" WQXGA 240Hz IPS Core Ultra 9 275HX "
        "RTX 5070 12GB 115W 32GB DDR5 1TB"
    ),

    # -- trap 2: laptop part quoted with desktop specs -----------------------
    "desktop_specs_quoted": (
        "MSI Raider 16 16\" QHD+ 240Hz Core i9 RTX 5070 Ti 16GB GDDR7 "
        "32GB DDR5 2TB SSD"
    ),

    # -- trap 5: docking-station storage sold as capacity --------------------
    "dock_storage_aggregate": (
        "HP OMEN MAX 16 16\" 2560x1600 240Hz OLED Ryzen 9 RTX 5070 Ti 12GB 32GB "
        "2TB Storage (1TB SSD & 1TB Docking Station)"
    ),
    "dock_storage_additive": (
        "Acer Nitro V 16 16\" WQXGA 180Hz IPS Ryzen 7 RTX 5060 8GB 32GB "
        "1TB SSD + 1TB Dock Set"
    ),

    # -- trap 6: premium-sounding resolutions that fail ----------------------
    "wuxga_trap": (
        "HP Omen 16 16.1\" WUXGA 1920x1200 144Hz IPS Core i7-14700HX "
        "RTX 4060 8GB 16GB 1TB SSD"
    ),
    "fhd_trap": (
        "ASUS TUF Gaming A16 16\" FHD 165Hz Ryzen 7 7435HS RTX 4060 "
        "16GB DDR5 1TB PCIe SSD"
    ),

    # -- trap 7: model family that never shipped OLED ------------------------
    "legion_pro_5_16irx9_fake_oled": (
        "Lenovo Legion Pro 5 16IRX9 16\" WQXGA OLED 240Hz i7-14650HX "
        "RTX 4070 8GB 32GB 1TB SSD"
    ),
    # -- trap 7b: panel type simply absent -----------------------------------
    "panel_unstated": (
        "Lenovo Legion 7i 16 16\" 2.5K 165Hz Core Ultra 7 255HX RTX 5060 "
        "32GB 1TB SSD"
    ),

    # -- trap 8: single-channel memory ---------------------------------------
    "single_channel": (
        "Dell G16 7630 16\" QHD+ 2560x1600 165Hz Core i7-13650HX "
        "RTX 4060 8GB 16GB (1x16GB) 1TB SSD"
    ),

    # -- junk / high-risk titles ---------------------------------------------
    "junk_read_as_is": (
        "Lenovo Legion 5 Pro 16ARH7H 16\" QHD+ 165Hz RTX 3070 16GB 1TB "
        "READ AS IS cracked bezel bad cam"
    ),
    "junk_no_battery": (
        "MSI Vector 16 HX 2560x1600 240Hz RTX 5070 Ti 12GB 32GB 1TB "
        "- no battery, FOR PARTS"
    ),

    # -- keyboard layouts -----------------------------------------------------
    "german_qwertz": (
        "Lenovo Legion Pro 5 16 16\" WQXGA OLED 165Hz Ryzen 7 8745HX "
        "RTX 5060 32GB 1TB SSD QWERTZ Tastatur"
    ),
    "german_us_layout": (
        "Lenovo Legion Pro 5 16 16\" WQXGA OLED 165Hz Ryzen 7 8745HX "
        "RTX 5060 32GB 1TB SSD - US Layout Keyboard"
    ),
    "belgian_azerty": (
        "MSI Vector 16 HX 16\" QHD+ 240Hz RTX 5070 Ti 12GB 32GB 1TB AZERTY"
    ),
    "uk_listing": (
        "Lenovo Legion 7i 16 16\" 2.5K OLED 165Hz Core Ultra 7 255HX "
        "RTX 5060 8GB 32GB 1TB SSD"
    ),
    "canadian_bilingual": (
        "Lenovo Legion 5i 15.1\" 2560x1600 OLED 165Hz Ultra 7 255HX RTX 5060 "
        "32GB 1TB - Canadian Multilingual keyboard"
    ),

    # -- free M.2 slot --------------------------------------------------------
    "free_m2": (
        "Lenovo Legion Pro 5 16 83LT000MUS WQXGA OLED 165Hz 500nit Ryzen 7 8745HX "
        "RTX 5060 115W 32GB 1TB SSD with free M.2 slot"
    ),

    # -- old GPU generation ---------------------------------------------------
    "rtx_2070": (
        "ASUS ROG Zephyrus M15 15.6\" 2560x1440 240Hz RTX 2070 8GB 16GB 1TB SSD"
    ),
}


# ---------------------------------------------------------------------------
# Listing builder
# ---------------------------------------------------------------------------


def make_listing(
    title_key: str | None = None,
    *,
    title: str | None = None,
    region: Region = Region.US,
    currency: str | None = None,
    price: float = 1100.0,
    shipping: float = 0.0,
    condition: Condition = Condition.NEW,
    seller_name: str = "Best Buy",
    feedback: int | None = None,
    percent: float | None = None,
    jurisdiction: str | None = None,
    keyboard: KeyboardLayout | None = None,
    source: str = "test",
    listing_id: str = "",
    ships_domestically: bool = True,
    description: str = "",
) -> Listing:
    """Build a `Listing` from a fixture title with sensible defaults.

    Pass `title_key` to use one of the fixtures above, or `title` for a one-off.
    Currency defaults to the region's own currency, matching what a real source
    module is required to report.
    """
    default_currency = {
        Region.US: "USD",
        Region.CA: "CAD",
        Region.GB: "GBP",
        Region.DE: "EUR",
        Region.BE: "EUR",
        Region.SE: "SEK",
        Region.AU: "AUD",
    }[region]

    resolved_title = title if title is not None else TITLES[title_key]

    return Listing(
        source=source,
        listing_id=listing_id or f"{title_key or 'adhoc'}-{region.value}",
        title=resolved_title,
        url=f"https://example.test/{title_key or 'adhoc'}",
        region=region,
        currency=currency or default_currency,
        sticker_price_local=price,
        domestic_shipping_local=shipping,
        ships_domestically=ships_domestically,
        condition=condition,
        seller_name=seller_name,
        seller_feedback_count=feedback,
        seller_feedback_percent=percent,
        jurisdiction=jurisdiction,
        stated_keyboard_layout=keyboard,
        description=description,
    )
