# SPDX-License-Identifier: GPL-3.0-or-later
"""Pack registry: name your own batteries once, then charge by name.

Each pack's chemistry, cell count, capacity, and safe current live in a
local TOML file instead of being re-entered by hand every time.

This deliberately does NOT try to auto-detect which pack is plugged in -
there's no ID chip, no data pins beyond the balance taps, and capacity/
chemistry aren't electrically encoded anywhere this charger can read (see
README.md's "Can this identify the battery automatically?" section for
the full reasoning). Instead, `b6ctl start --pack NAME` reads the pack's
*registered* cell count and refuses to send anything if the charger
currently detects a different number of real cells connected - a live
cross-check against what you told it, not a replacement for telling it.

File layout: `packs.toml` is YOUR real, local pack roster - it's
gitignored, never committed, and this module falls back to the safe,
harmless `packs.example.toml` template if it doesn't exist yet.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("packs.toml")
EXAMPLE_CONFIG_PATH = Path("packs.example.toml")

VALID_CHEMISTRIES = {"lipo", "lihv"}

#: This tool will never allow a configured current above this many times a
#: pack's capacity, regardless of what its packs.toml entry claims - a
#: hard chemistry-derived ceiling enforced in code, not a preference a
#: config file typo could exceed.
MAX_SAFE_C_RATE = 1.0


class PackConfigError(Exception):
    """A pack registry file is missing, malformed, or configures an unsafe value.

    The message always names the specific pack/field that triggered it,
    so a mistake in packs.toml is quick to find and fix.
    """


@dataclass(frozen=True)
class Pack:
    """One battery pack's known-good charge settings, as configured in packs.toml.

    This is trusted operator input (you typed it, presumably from the
    numbers printed on the pack), not something inferred from the
    hardware - see the module docstring for why that distinction matters.
    """

    name: str
    description: str
    chemistry: str  #: "lipo" or "lihv" - validated in `_validate_pack`
    cells: int
    capacity_mah: int
    default_current_ma: int
    max_current_ma: int

    @property
    def is_hv(self) -> bool:
        """Whether this pack should be charged on the LiHV (4.35V/cell) profile."""
        return self.chemistry == "lihv"


@dataclass(frozen=True)
class PackRegistry:
    """Every pack loaded from one TOML file, plus where that file came from.

    `source_path`/`is_example` let callers (the CLI) warn loudly when
    running off the harmless example file instead of a real, user-
    configured one, rather than silently charging against placeholder data.
    """

    packs: dict[str, Pack]
    source_path: Path
    is_example: bool

    def get(self, name: str) -> Pack:
        """Look up a pack by name.

        Raises PackConfigError listing the packs that DO exist if `name`
        isn't one of them, rather than a bare KeyError.
        """
        try:
            return self.packs[name]
        except KeyError:
            known = ", ".join(sorted(self.packs)) or "(none configured)"
            raise PackConfigError(
                f"no pack named '{name}' in {self.source_path} - known packs: {known}"
            ) from None

    def __iter__(self):
        """Iterate over configured Pack objects (not names) in the registry."""
        return iter(self.packs.values())


def _validate_pack(name: str, raw: dict) -> Pack:
    """Build and safety-check one `Pack` from its raw TOML table.

    Enforces bounds in code, not just documentation: a typo or a copy-
    pasted bad value in packs.toml cannot make this tool exceed 1C or
    accept an invalid chemistry string, no matter what the file says.
    Raises PackConfigError (never returns a partially-valid Pack) on any
    problem.
    """
    required = {"description", "chemistry", "cells", "capacity_mah", "default_current_ma"}
    missing = required - raw.keys()
    if missing:
        raise PackConfigError(
            f"pack '{name}' is missing field(s): {', '.join(sorted(missing))}"
        )

    chemistry = str(raw["chemistry"]).lower()
    if chemistry not in VALID_CHEMISTRIES:
        raise PackConfigError(
            f"pack '{name}': chemistry must be one of {sorted(VALID_CHEMISTRIES)}, "
            f"got {raw['chemistry']!r}"
        )

    cells = int(raw["cells"])
    if not 1 <= cells <= 16:
        raise PackConfigError(f"pack '{name}': cells must be 1-16, got {cells}")

    capacity_mah = int(raw["capacity_mah"])
    if capacity_mah <= 0:
        raise PackConfigError(
            f"pack '{name}': capacity_mah must be positive, got {capacity_mah}"
        )

    max_allowed_ma = int(capacity_mah * MAX_SAFE_C_RATE)
    max_current_ma = int(raw.get("max_current_ma", max_allowed_ma))
    if max_current_ma > max_allowed_ma:
        raise PackConfigError(
            f"pack '{name}': max_current_ma={max_current_ma} exceeds "
            f"{MAX_SAFE_C_RATE:.0f}C for a {capacity_mah}mAh pack "
            f"({max_allowed_ma}mA) - this tool will not raise this ceiling "
            "even via the config file, since it's a chemistry limit, not a "
            "preference"
        )

    default_current_ma = int(raw["default_current_ma"])
    if not 0 < default_current_ma <= max_current_ma:
        raise PackConfigError(
            f"pack '{name}': default_current_ma={default_current_ma} must be "
            f"> 0 and <= max_current_ma ({max_current_ma})"
        )

    return Pack(
        name=name,
        description=str(raw["description"]),
        chemistry=chemistry,
        cells=cells,
        capacity_mah=capacity_mah,
        default_current_ma=default_current_ma,
        max_current_ma=max_current_ma,
    )


def load_registry(path: Path | None = None) -> PackRegistry:
    """Load the pack registry.

    With no `path`, prefers `packs.toml` (your real, local, gitignored
    config) and falls back to `packs.example.toml` (the safe placeholder
    shipped in the repo) if it doesn't exist yet. Pass `path` explicitly
    to load a specific file instead (mainly for tests).

    Raises PackConfigError with a specific, actionable message if the
    file is missing, malformed, or configures something unsafe - never
    silently drops or clamps a bad entry.
    """
    if path is not None:
        candidates = [(path, path == EXAMPLE_CONFIG_PATH)]
    else:
        candidates = [(DEFAULT_CONFIG_PATH, False), (EXAMPLE_CONFIG_PATH, True)]

    for candidate_path, is_example in candidates:
        if candidate_path.exists():
            return _load_from_path(candidate_path, is_example)

    raise PackConfigError(
        f"no pack registry found ({DEFAULT_CONFIG_PATH} or {EXAMPLE_CONFIG_PATH}) "
        "- see README.md's 'Configure your batteries' section"
    )


def _load_from_path(path: Path, is_example: bool) -> PackRegistry:
    """Parse and validate every `[[pack]]` entry in one TOML file."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise PackConfigError(f"{path} is not valid TOML: {e}") from e

    raw_packs = data.get("pack", [])
    if not raw_packs:
        raise PackConfigError(f"{path} has no [[pack]] entries")

    packs: dict[str, Pack] = {}
    for raw in raw_packs:
        if "name" not in raw:
            raise PackConfigError(f"{path} has a [[pack]] entry with no 'name' field")
        name = str(raw["name"])
        if name in packs:
            raise PackConfigError(f"{path} has two packs both named '{name}'")
        packs[name] = _validate_pack(name, raw)

    return PackRegistry(packs=packs, source_path=path, is_example=is_example)
