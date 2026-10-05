"""Decide who pays for a usage row, or that nobody does.

Every row gets exactly one outcome (see contracts.MappingOutcome):

    NOT_BILLABLE  provider not in billable_providers, or a cache hit we don't bill
    MAPPED        billable, and we found the Lago subscription to bill
    UNMAPPED      billable, but nobody to bill -> dead letter + alert, never dropped

Sources are tried in config order (decisions.md D2, default: label, then user_path).
"""

from exporter.config import MappingSource, Settings
from exporter.contracts import MappingOutcome, MappingResult, UsageRow


def subscriptions_from_labels(labels: tuple[str, ...], prefix: str) -> list[str]:
    """Distinct subscription ids found in labels like 'lago:sub_acme'."""
    found: list[str] = []
    for label in labels:
        if label.startswith(prefix):
            sub = label[len(prefix):].strip()
            if sub and sub not in found:
                found.append(sub)
    return found


def subscription_from_user_path(user_path: str | None) -> str | None:
    """Last segment of the user path: '/customers/sub_beta' -> 'sub_beta' (D3).

    GoModel stores '/' when no user path is set; that means "none".
    """
    segments = [s for s in (user_path or "").strip().split("/") if s]
    return segments[-1] if segments else None


def map_row(row: UsageRow, settings: Settings) -> MappingResult:
    # 1. Is this usage billable at all?
    if row.provider_name not in settings.billable_providers:
        return MappingResult(
            row, MappingOutcome.NOT_BILLABLE,
            reason=f"provider {row.provider_name!r} is not billable",
        )
    if row.is_cache_hit and not settings.bill_cache_hits:
        return MappingResult(
            row, MappingOutcome.NOT_BILLABLE,
            reason=f"response-cache hit ({row.cache_type}) and bill_cache_hits is off",
        )

    # 2. Who pays? Try each source in the configured order.
    misses: list[str] = []
    for source in settings.mapping_order:
        if source is MappingSource.LABEL:
            subs = subscriptions_from_labels(row.labels, settings.subscription_label_prefix)
            if len(subs) == 1:
                return MappingResult(row, MappingOutcome.MAPPED, subs[0],
                                     reason=f"label {settings.subscription_label_prefix}{subs[0]}")
            if len(subs) > 1:
                # Never guess between customers: a human must look at this row.
                return MappingResult(row, MappingOutcome.UNMAPPED,
                                     reason=f"conflicting subscription labels: {', '.join(subs)}")
            misses.append(f"no {settings.subscription_label_prefix} label")

        elif source is MappingSource.USER_PATH:
            sub = subscription_from_user_path(row.user_path)
            if sub:
                return MappingResult(row, MappingOutcome.MAPPED, sub,
                                     reason=f"user_path {row.user_path}")
            misses.append("no user_path")

    # 3. Billable, but nobody to bill.
    return MappingResult(row, MappingOutcome.UNMAPPED, reason="; ".join(misses))