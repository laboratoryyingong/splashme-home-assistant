"""Pool light brand protocols and colour tables.

SplashMe never talks to a light: a colour change is a relay off-pulse of a
brand-specific length, run by the firmware (`set_pool_light_switch_mode`).
Tables follow docs/ha_light_control_reference.md in the firmware repo.
"""

from __future__ import annotations

from dataclasses import dataclass

# aux_type_code groups: the device keeps one colour preference per group.
POOL_LIGHT_TYPE_CODES = frozenset({12, 13, 14, 15, 16, 35})
SPA_LIGHT_TYPE_CODES = frozenset({26, 27, 28, 29, 30, 36})

# light_type (firmware LightTypeMode)
LIGHT_UNKNOWN = 0
AQUAQUIP_INSTA_TOUCH = 1
SPA_ELECTRICS_MULTI_PLUS = 2
SPA_ELECTRICS_MULTI_COLOUR = 3
AQUAQUIP_MULTI_COLOUR = 4
PAL_COLOUR_TOUCH = 5

COLOUR_RESET = 0
COLOUR_UNSET = 99  # g_*_light_mode when never set

# Brands with absolute colour selection: light_color index -> effect name.
COLOUR_TABLES: dict[int, dict[int, str]] = {
    AQUAQUIP_INSTA_TOUCH: {
        1: "Blue", 2: "Aqua", 3: "Green", 4: "Gold", 5: "Magenta", 6: "Red",
        7: "White", 8: "Seaside", 9: "Slow Scroll", 10: "Rapid Scroll",
        11: "Fireworks", 12: "Disco", 13: "Flash",
    },
    SPA_ELECTRICS_MULTI_PLUS: {
        1: "Blue", 2: "Magenta", 3: "Red", 4: "Lime", 5: "Green", 6: "Aqua",
        7: "White", 8: "Oceanic Views", 9: "Transcendence",
        10: "Outback Australia", 11: "Spring Equinox",
    },
    PAL_COLOUR_TOUCH: {
        1: "Deep Blue", 2: "Aqua/Teal", 3: "Emerald Green", 4: "White 4000K",
        5: "Soft Violet", 6: "SunLover Sunset", 7: "Northern Lights",
        8: "Candela", 9: "Enchanted Forest", 10: "Lunar Wonder", 11: "Romance",
        12: "Halloween", 13: "Symphony",
    },
}

# How long the relay sequence blocks before the light is settled (seconds,
# with margin). Colour picks on the three absolute brands take <= 3 s.
COLOUR_SETTLE_SECONDS = 5


@dataclass(frozen=True, slots=True)
class LightAction:
    """A one-shot light command exposed as a button."""

    key: str
    name: str
    colour: int
    settle_seconds: int


# Brands without absolute selection get buttons instead of an effect list;
# extras for brands that also have effects.
LIGHT_ACTIONS: dict[int, tuple[LightAction, ...]] = {
    SPA_ELECTRICS_MULTI_PLUS: (
        LightAction("brightness_down", "Brightness -", 12, 5),
        LightAction("resync", "Resync Colours", COLOUR_RESET, 110),
    ),
    SPA_ELECTRICS_MULTI_COLOUR: (
        LightAction("next_colour", "Next Colour", 1, 4),
        LightAction("sync", "Sync Colours", COLOUR_RESET, 8),
    ),
    AQUAQUIP_MULTI_COLOUR: (
        LightAction("next_colour", "Next Colour", 1, 3),
        LightAction("save_colour", "Save Colour", 2, 13),
        LightAction("sync", "Sync Colours", COLOUR_RESET, 18),
    ),
}


def light_group(type_code: int) -> str | None:
    """Return "pool" / "spa" for a light slot, else None."""
    if type_code in POOL_LIGHT_TYPE_CODES:
        return "pool"
    if type_code in SPA_LIGHT_TYPE_CODES:
        return "spa"
    return None
