"""Exact-range event audit plus independently scoped Lago billing-period checks."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from exporter.contracts import MappingOutcome, TokenKind, transaction_id
from exporter.events import FIELD_NAMES, build_events
from exporter.lago_read import EvidenceUnavailable, LagoReadAPI, instant, period_bounds, units
from exporter.mapper import map_row
from exporter.state.store import Cursor


def issue(code, rows=(), detail='', *, incomplete=False):
    return dict(code=code, row_ids=sorted(set(rows)), detail=detail, incomplete=incomplete)


def event_key(event):
    return event.external_subscription_id, event.transaction_id


def dimension(event):
    return event.external_subscription_id, event.properties['model'], event.kind.value


def comparisons(source, acknowledged, lago, row_ids, customers=None):
    result = []
    for key in sorted(set(source) | set(acknowledged) | set(lago or {})):
        a, b, c = source.get(key, 0), acknowledged.get(key, 0), lago.get(key, 0) if lago is not None else None
        result.append(dict(subscription_id=key[0], customer_id=(customers or {}).get(key[0]),
            model=key[1], kind=key[2], source=a, acknowledged=b, lago=c,
            source_minus_acknowledged=a-b, acknowledged_minus_lago=b-c if c is not None else None,
            row_ids=sorted(row_ids.get(key, set()))))
    return result


class Reconciler:
    def __init__(self, settings, open_store, reader, client, now=None):
        self.settings, self.open_store, self.reader = settings, open_store, reader
        self.api = LagoReadAPI(client, settings)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def source_rows(self, start, end):
        position = None
        result = {}
        while rows := self.reader.read_after(position, self.settings.read_batch_size, since=start, until=end):
            result.update((row.id, row) for row in rows)
            position = Cursor(rows[-1].timestamp, rows[-1].id)
        return result

    def local_evidence(self, store, start, end):
        source = self.source_rows(start, end)
        records = store.reconciliation_records(start, end)
        expected, acknowledged, intents, issues, excluded = {}, {}, {}, [], []
        codes = {self.settings.metric_input: TokenKind.INPUT, self.settings.metric_cached_input: TokenKind.CACHED,
                 self.settings.metric_output: TokenKind.OUTPUT}
        for row_id, row in source.items():
            mapping = map_row(row, self.settings)
            if mapping.outcome is MappingOutcome.NOT_BILLABLE:
                excluded.append(row_id)
            elif mapping.outcome is MappingOutcome.UNMAPPED:
                issues.append(issue('unmapped', [row_id], mapping.reason))
            else:
                for event in build_events(mapping, self.settings):
                    expected[event_key(event)] = event
        for row_id, record in records.items():
            if row_id not in source:
                issues.append(issue('missing_source', [row_id], 'Local state has no source row in this period.', incomplete=True))
            delivery = record.get('delivery')
            if delivery:
                for event in delivery.events:
                    intents[event_key(event)] = event
                    if event.code in codes and codes[event.code] != event.kind:
                        issues.append(issue('metric_code_collision', [row_id], incomplete=True))
                    codes[event.code] = event.kind
                    count = record['acknowledged'].get(event.kind.value, 0)
                    if count:
                        acknowledged[event_key(event)] = (event, count)
                if record.get('pending'):
                    issues.append(issue('pending', [row_id], 'Delivery awaits retry.'))
            elif record.get('subscription_id'):
                issues.append(issue('legacy_payload_unavailable', [row_id],
                    'Pre-outbox state has quantities but no original metric payload.', incomplete=True))
                row = source.get(row_id)
                if row:
                    mapping = store.legacy_mapping(map_row(row, self.settings))
                    for event in build_events(mapping, self.settings, include_zero=True):
                        count = record['acknowledged'].get(event.kind.value, 0)
                        if count:
                            acknowledged[event_key(event)] = (event, count)
            if record.get('status') == 'failed':
                issues.append(issue('rejected', [row_id], 'Delivery has a permanent failure.'))
        for key, event in intents.items():
            current = expected.get(key)
            if current is None or current.to_payload() != event.to_payload():
                issues.append(issue('source_or_policy_changed', [event.usage_row_id],
                    'Current source/configuration differs from the frozen billing intent.'))
        return expected, acknowledged, intents, issues, excluded, codes

    def audit(self, store, start, end):
        expected, ack, intents, issues, excluded, codes = self.local_evidence(store, start, end)
        source_totals, ack_totals, lago_totals = defaultdict(int), defaultdict(int), defaultdict(int)
        rows_by_dimension = defaultdict(set)
        for event in expected.values():
            key = dimension(event)
            source_totals[key] += event.tokens
            rows_by_dimension[key].add(event.usage_row_id)
        for event, count in ack.values():
            key = dimension(event)
            ack_totals[key] += count
            rows_by_dimension[key].add(event.usage_row_id)
        try:
            remote = self.api.events(start, end)
        except EvidenceUnavailable as error:
            issues.append(issue('lago_unavailable', detail=str(error), incomplete=True))
            return dict(issues=issues, excluded=excluded, totals=comparisons(source_totals, ack_totals, None, rows_by_dimension),
                        source=source_totals, ack=ack_totals, lago={}, rows=rows_by_dimension, remote_complete=False)
        observed = {}
        for event in remote:
            if event.get('code') not in codes:
                continue
            try:
                kind = codes[event['code']]
                sub, tx = event['external_subscription_id'], event['transaction_id']
                props = event['properties']
                model = props['model']
                if not isinstance(model, str) or not isinstance(sub, str) or not sub:
                    raise ValueError('invalid dimensions')
                count = units(props[FIELD_NAMES[kind]])
                key = (sub, model, kind.value)
                lago_totals[key] += count
                row_id = tx.rsplit(':', 1)[0]
                rows_by_dimension[key].add(row_id)
                observed[(sub, tx)] = event
            except (KeyError, TypeError, ValueError):
                issues.append(issue('malformed_lago_event', detail='Invalid token event response.', incomplete=True))
        for key, expected_event in (intents | expected).items():
            actual = observed.get(key)
            row_id = expected_event.usage_row_id
            if actual is None:
                issues.append(issue('missing_lago_event', [row_id], expected_event.transaction_id))
                continue
            # The pinned serializer truncates timestamps to milliseconds.
            ts = expected_event.timestamp.replace(microsecond=(expected_event.timestamp.microsecond // 1000) * 1000)
            if (actual['code'] != expected_event.code or actual['properties'] != expected_event.properties
                    or instant(actual['timestamp']) != ts):
                issues.append(issue('event_payload_mismatch', [row_id], expected_event.transaction_id))
        for key, event in observed.items():
            if key not in expected and key not in intents:
                issues.append(issue('unexpected_lago_event', [event['transaction_id'].rsplit(':', 1)[0]], event['transaction_id']))
        totals = comparisons(source_totals, ack_totals, lago_totals, rows_by_dimension)
        for total in totals:
            if total['source_minus_acknowledged'] or total['acknowledged_minus_lago']:
                issues.append(issue('token_mismatch', total['row_ids'], f"{total['subscription_id']}/{total['model']}/{total['kind']}"))
        return dict(issues=issues, excluded=excluded, totals=totals, source=source_totals, ack=ack_totals,
                    lago=lago_totals, rows=rows_by_dimension, remote_complete=True)

    def run_if_due(self, start, end, now):
        return self.run(start, end, scheduled_now=now)

    def run(self, start, end, *, scheduled_now=None):
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError('Reconciliation requires timezone-aware start < end.')
        now = self.now()
        report = dict(start=start.isoformat(), end=end.isoformat(), status='incomplete', issues=[],
                      totals=[], excluded_rows=[], billing_periods=[])
        with self.open_store() as store, store.writer_lock(), store.worker_run('reconcile') as heartbeat:
            if scheduled_now is not None:
                previous = store.reconciliation_for_period(start, end)
                if previous:
                    completed, status = previous
                    if (status == 'matched' and completed.date() == scheduled_now.date()) or (
                        status != 'matched' and (scheduled_now-completed).total_seconds() < self.settings.reconciliation_retry_seconds
                    ):
                        return None
            try:
                daily = self.audit(store, start, end)
                report.update(issues=daily['issues'], totals=daily['totals'], excluded_rows=daily['excluded'])
                if end > now - timedelta(seconds=self.settings.reconciliation_delay_seconds):
                    report['issues'].append(issue('settling_window', detail='Period has not settled.', incomplete=True))
                if start < now - timedelta(days=self.settings.source_retention_days):
                    report['issues'].append(issue('source_retention', detail='Period exceeds source retention.', incomplete=True))
                subscriptions = sorted({row['subscription_id'] for row in report['totals']})
                customers = {}
                for sub_id in subscriptions:
                    try:
                        sub = self.api.subscription(sub_id)
                        customers[sub_id] = sub['external_customer_id']
                        self.check_billing(store, sub, start, end, report)
                    except (EvidenceUnavailable, ValueError, KeyError, TypeError) as error:
                        report['issues'].append(issue('billing_evidence_unavailable',
                            detail=f'{sub_id}: {type(error).__name__}', incomplete=True))
                for total in report['totals']:
                    total['customer_id'] = customers.get(total['subscription_id'])
                report['status'] = ('incomplete' if any(i['incomplete'] for i in report['issues'])
                                    else 'mismatch' if report['issues'] else 'matched')
            except Exception as error:
                # Preserve an explicit failed report, without logging DSNs/API payloads.
                report['issues'].append(issue('reconciliation_error', detail=type(error).__name__, incomplete=True))
                report['status'] = 'incomplete'
            report['id'] = store.save_reconciliation(report)
            heartbeat.update(status=report['status'], reconciliation_id=report['id'])
        return report

    def check_billing(self, store, sub, start, end, report):
        periods = self.api.billing_periods(sub, start, end)
        covered = []
        for mode, period in periods:
            lo, hi = period_bounds(period)
            covered.append((max(start, lo), min(end, hi)))
            evidence = self.audit(store, lo, hi)
            if lo < self.now() - timedelta(days=self.settings.source_retention_days):
                report['issues'].append(issue('billing_period_retention', detail=sub['external_id'], incomplete=True))
            sub_id = sub['external_id']
            models = {key[1] for collection in ('source', 'ack', 'lago') for key in evidence[collection] if key[0] == sub_id}
            codes = {self.settings.metric_input: 'in', self.settings.metric_cached_input: 'cached', self.settings.metric_output: 'out'}
            billed = defaultdict(int)
            for charge in period['charges_usage']:
                if charge['billable_metric']['code'] not in codes:
                    continue
                for item in charge.get('filters', []):
                    values = item.get('values') or {}
                    models.update(values.get('model', []))
            if mode == 'current':
                for model in sorted(models):
                    fresh = self.api.current_model_usage(sub, model)
                    if period_bounds(fresh) != (lo, hi):
                        raise EvidenceUnavailable('Billing period changed during audit')
                    seen = set()
                    for charge in fresh['charges_usage']:
                        code = charge['billable_metric']['code']
                        if code not in codes:
                            continue
                        if code in seen or charge['billable_metric']['aggregation_type'] != 'sum_agg':
                            raise EvidenceUnavailable('Unsupported billing metric/charge layout')
                        seen.add(code)
                        billed[(sub_id, model, codes[code])] = units(charge['units'])
                    if seen != set(codes):
                        raise EvidenceUnavailable('Missing token metric charge')
            else:
                for charge in period['charges_usage']:
                    code = charge['billable_metric']['code']
                    if code not in codes:
                        continue
                    filters = charge.get('filters', [])
                    accounted = 0
                    for item in filters:
                        values = (item.get('values') or {}).get('model', [])
                        count = units(item['units'])
                        if len(values) != 1 and count:
                            raise EvidenceUnavailable('Historical usage lacks a single-model breakdown')
                        if len(values) == 1:
                            billed[(sub_id, values[0], codes[code])] += count
                        accounted += count
                    if accounted != units(charge['units']):
                        raise EvidenceUnavailable('Historical model breakdown is incomplete')
            source = {k: v for k, v in evidence['source'].items() if k[0] == sub_id}
            ack = {k: v for k, v in evidence['ack'].items() if k[0] == sub_id}
            rows = comparisons(source, ack, billed, evidence['rows'], {sub_id: sub['external_customer_id']})
            for row in rows:
                if row['source_minus_acknowledged'] or row['acknowledged_minus_lago']:
                    report['issues'].append(issue('billing_usage_mismatch', row['row_ids'],
                        f'{sub_id}/{row["model"]}/{row["kind"]} in {lo.isoformat()}..{hi.isoformat()}'))
            if not evidence['remote_complete'] or any(i['incomplete'] for i in evidence['issues']):
                report['issues'].append(issue('billing_period_incomplete', detail=sub_id, incomplete=True))
            report['billing_periods'].append(dict(subscription_id=sub_id, customer_id=sub['external_customer_id'],
                start=lo.isoformat(), end=hi.isoformat(), evidence=mode, totals=rows,
                checked_at=self.now().isoformat()))
        boundary = start
        for lo, hi in sorted(covered):
            if lo > boundary:
                break
            boundary = max(boundary, hi)
        if boundary < end:
            raise EvidenceUnavailable('Lago billing periods do not cover the requested range')
