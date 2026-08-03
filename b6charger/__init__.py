# SPDX-License-Identifier: GPL-3.0-or-later
"""b6charger-ctl: read/write control for SkyRC B6-family balance chargers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("b6charger-ctl")
except PackageNotFoundError:
    # Only reachable running from a source checkout without `pip install
    # -e .` having been run yet (see README's Install section) - not a
    # real release, so there's no version string to report accurately.
    __version__ = "0.0.0+unknown"
