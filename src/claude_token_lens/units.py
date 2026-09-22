"""Amounts in the units that matter for your billing mode.

A pay-per-token (API) user pays list price, so a saving is a dollar
figure. A Pro or Max user pays a flat fee and is limited by usage
windows, so a dollar figure means little; what matters is how much of
the weekly usage limit a change frees up.

:class:`Units` phrases one amount both ways. For a subscription it uses
``elasticity.express_in_window`` -- the share of the weekly window a
list-price dollar is worth, fitted from this machine's own statusline
usage-limit samples -- and falls back to "$X list-price equivalent"
with a hint on how to get the share when too few samples exist.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import elasticity as elasticity_mod
from .render.tables import format_cell

#: Shown when a subscription amount can't be given as a share of the
#: usage limits yet.
NO_LIMIT_SHARE_HINT = (
    "Log your statusline's usage-limit readings (claude-token-lens statusline) "
    "to see this as a share of your plan's weekly limit."
)


@dataclass(frozen=True, slots=True)
class Amount:
    """One amount, phrased for the billing mode. ``primary`` leads;
    ``secondary`` is the same amount in the other unit, and ``basis``
    says how it was worked out (or how to get a better figure)."""

    primary: str
    secondary: str = ""
    basis: str = ""

    def text(self) -> str:
        return f"{self.primary} ({self.secondary})" if self.secondary else self.primary


@dataclass(frozen=True, slots=True)
class Units:
    billing_mode: str = "api"
    currency: str = "USD"
    elasticity: "elasticity_mod.ElasticityStats | None" = None

    def money(self, usd: float, *, period: str = "") -> Amount | None:
        """``usd`` (a list-price amount over ``period``, e.g. "over 30
        days") phrased for the billing mode. ``None`` for a non-positive
        or non-finite amount."""
        if not isinstance(usd, (int, float)) or not math.isfinite(usd) or usd <= 0:
            return None
        suffix = f" {period}" if period else ""
        dollars = format_cell(usd, "money", self.currency)
        if self.billing_mode != "subscription":
            return Amount(primary=f"{dollars}{suffix}", basis="at list price")
        share = (
            elasticity_mod.express_in_window(usd, self.elasticity) if self.elasticity is not None else None
        )
        if share is None:
            return Amount(
                primary=f"{dollars} list-price equivalent{suffix}",
                basis=NO_LIMIT_SHARE_HINT,
            )
        # Two decimals below 1%, so a small saving doesn't read as "0.0%".
        share_text = f"{share:.2f}%" if share < 1 else format_cell(share, "pct")
        return Amount(
            primary=f"about {share_text} of your weekly usage limit{suffix}",
            secondary=f"{dollars} list-price equivalent",
            basis="from your own usage-limit readings against the tokens used between them",
        )


__all__ = ["Amount", "NO_LIMIT_SHARE_HINT", "Units"]
