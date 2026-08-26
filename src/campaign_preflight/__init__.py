"""Campaign Preflight: a read-only linter for outbound email campaigns.

Campaign Preflight inspects a campaign's configuration, contacts, personalization,
suppression exposure, schedule, and sender readiness, and returns a readiness
decision with evidence for every finding. It never writes to a provider and never
activates a campaign.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
