import json
from copy import deepcopy
from datetime import timedelta

import pytest

from exporter.reconcile import Reconciler
from exporter.state.store import StateStore
from tests.factories import T0, settings
from tests.fake_lago_read import ReadFakeLago
from tests.test_runner import _databases, gomodel, state, insert, make_runner, _url, STATE_DB, GOMODEL_DB
from exporter.reader import UsageReader

DAY = T0.replace(hour=0)
END = DAY + timedelta(days=1)
NOW = END + timedelta(hours=12)


@pytest.fixture
def lago():
    return ReadFakeLago()


def make_reconciler(lago, **overrides):
    return Reconciler(settings(**overrides), lambda: StateStore(_url(STATE_DB)),
                      UsageReader(_url(GOMODEL_DB)), lago.client(), now=lambda: NOW)


def codes(report):
    return {i['code'] for i in report['issues']}


def test_three_way_match_includes_customer_model_and_all_token_kinds(gomodel, state, lago):
    first = insert(gomodel, T0)
    second = insert(gomodel, T0, key='beta')
    gomodel.execute('UPDATE usage SET raw_data = %s WHERE id = %s', (json.dumps({'cached_tokens': 10}), first))
    gomodel.execute("UPDATE usage SET model = 'other-model' WHERE id = %s", (second,))
    make_runner(lago).run_cycle()
    report = make_reconciler(lago, reconciliation_page_size=1, read_batch_size=1).run(DAY, END)
    assert report['status'] == 'matched', report['issues']
    assert {r['customer_id'] for r in report['totals']} == {'acme', 'beta'}
    assert {r['model'] for r in report['totals']} == {'qwen2.5:0.5b', 'other-model'}
    assert {r['kind'] for r in report['totals']} == {'in', 'cached', 'out'}
    assert all(r['source'] == r['acknowledged'] == r['lago'] for r in report['totals'])
    assert len(report['billing_periods']) == 2
    assert state.latest_reconciliation()['id'] == report['id']


def test_deleted_event_is_reported_with_exact_row(gomodel, state, lago):
    row = insert(gomodel, T0)
    make_runner(lago).run_cycle()
    del lago.stored[('sub_acme', row+':out')]
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'mismatch'
    assert 'missing_lago_event' in codes(report)
    assert any(i['code'] == 'missing_lago_event' and i['row_ids'] == [row] for i in report['issues'])
    assert 'billing_usage_mismatch' in codes(report)


def test_equal_total_but_changed_individual_events_is_detected(gomodel, state, lago):
    a, b = insert(gomodel, T0), insert(gomodel, T0)
    make_runner(lago).run_cycle()
    lago.stored[('sub_acme', a+':in')]['properties']['input_tokens'] += 1
    lago.stored[('sub_acme', b+':in')]['properties']['input_tokens'] -= 1
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'mismatch'
    assert 'event_payload_mismatch' in codes(report)
    assert 'token_mismatch' not in codes(report)


def test_daily_and_monthly_scopes_are_not_mixed(gomodel, state, lago):
    insert(gomodel, T0-timedelta(days=1))
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'matched', report['issues']
    daily = next(r for r in report['totals'] if r['kind'] == 'in')
    period = next(r for r in report['billing_periods'][0]['totals'] if r['kind'] == 'in')
    assert daily['lago'] == 35 and period['lago'] == 70


def test_public_usage_cache_is_bypassed_with_model_filter(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    lago.cached_delta = 1000
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'matched', report['issues']
    assert lago.filter_requests > 0


def test_ingested_events_do_not_hide_wrong_billing_usage(gomodel, state, lago):
    row = insert(gomodel, T0)
    make_runner(lago).run_cycle()
    lago.billing_delta = 9
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'mismatch'
    assert codes(report) == {'billing_usage_mismatch'}
    assert row in report['issues'][0]['row_ids']


def test_partial_failed_delivery_acknowledgements_are_included(gomodel, state, lago):
    from tests.fake_lago import BAD_CODE
    row = insert(gomodel, T0)
    make_runner(lago, metric_output=BAD_CODE).run_cycle()
    report = make_reconciler(lago, metric_output=BAD_CODE).run(DAY, END)
    inp = next(r for r in report['totals'] if r['kind'] == 'in')
    assert inp['acknowledged'] == inp['lago'] == 35
    assert 'rejected' in codes(report)
    assert report['status'] != 'matched'


def test_lago_unavailable_is_incomplete_not_zero_usage(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    lago.read_failure = 503
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'incomplete'
    assert 'lago_unavailable' in codes(report)
    assert all(r['lago'] is None and r['acknowledged_minus_lago'] is None for r in report['totals'])
    assert state.latest_reconciliation()['status'] == 'incomplete'


def test_unstable_pagination_is_incomplete(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    lago.bad_pagination = True
    assert make_reconciler(lago, reconciliation_page_size=1).run(DAY, END)['status'] == 'incomplete'


def test_page_cap_prevents_false_success(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    report = make_reconciler(lago, reconciliation_page_size=1, reconciliation_max_pages=1).run(DAY, END)
    assert report['status'] == 'incomplete'


def test_unmapped_and_excluded_rows_are_distinguished(gomodel, state, lago):
    ghost = insert(gomodel, T0, key='ghost')
    external = insert(gomodel, T0, provider='ollama-ext')
    make_runner(lago).run_cycle()
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'mismatch'
    assert report['excluded_rows'] == [external]
    assert report['issues'][0]['row_ids'] == [ghost]


def test_missing_source_and_legacy_state_are_explicit(gomodel, state, lago):
    a, b = insert(gomodel, T0), insert(gomodel, T0)
    make_runner(lago).run_cycle()
    state._conn.execute('DELETE FROM deliveries WHERE usage_row_id = %s', (a,))
    gomodel.execute('DELETE FROM usage WHERE id = %s', (b,))
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'incomplete'
    assert {'missing_source', 'legacy_payload_unavailable'} <= codes(report)


def test_source_mapping_changes_are_visible(gomodel, state, lago):
    row = insert(gomodel, T0)
    make_runner(lago).run_cycle()
    gomodel.execute('UPDATE usage SET labels = %s WHERE id = %s', (json.dumps(['lago:sub_beta']), row))
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'mismatch'
    assert 'source_or_policy_changed' in codes(report)


def test_recent_period_cannot_be_declared_settled(gomodel, state, lago):
    report = make_reconciler(lago).run(DAY, NOW)
    assert report['status'] == 'incomplete'
    assert 'settling_window' in codes(report)


def test_missing_metric_charge_is_incomplete(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    lago.missing_metric = True
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'incomplete'
    assert 'billing_evidence_unavailable' in codes(report)


def test_exclusive_end_and_millisecond_timestamp_contract(gomodel, state, lago):
    row = insert(gomodel, T0+timedelta(microseconds=123456))
    insert(gomodel, END)
    make_runner(lago).run_cycle()
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'matched', report['issues']
    assert {rid for r in report['totals'] for rid in r['row_ids']} == {row}


def test_terminated_subscription_uses_historical_invoice_evidence(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    body = lago.client()._http.get('/api/v1/customers/acme/current_usage', params={'external_subscription_id': 'sub_acme'}).json()
    lago.past_periods = [body['customer_usage']]
    lago.terminated['sub_acme'] = lago.subscriptions.pop('sub_acme')
    report = make_reconciler(lago, reconciliation_page_size=1).run(DAY, END)
    assert report['status'] == 'matched', report['issues']
    assert report['billing_periods'][0]['evidence'] == 'past'


def test_historical_missing_period_never_reports_match(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    lago.terminated['sub_acme'] = lago.subscriptions.pop('sub_acme')
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'incomplete'


def test_source_failure_persists_incomplete_report(gomodel, state, lago):
    reconciler = make_reconciler(lago)
    def fail(*args, **kwargs):
        raise RuntimeError('sensitive DSN')
    reconciler.reader.read_after = fail
    report = reconciler.run(DAY, END)
    assert report['status'] == 'incomplete'
    assert 'sensitive' not in json.dumps(report)
    assert state.latest_reconciliation()['status'] == 'incomplete'


def test_current_usage_ignores_unrelated_price_filter_bucket(gomodel, state, lago):
    import httpx
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    original = lago._handle
    def handle(request):
        response = original(request)
        if request.url.path.endswith('/current_usage') and request.url.params.get('filter_by_group'):
            body = response.json()
            for charge in body['customer_usage']['charges_usage']:
                charge['filters'].append({'values': {'model': ['unrelated-price-bucket']}, 'units': '900'})
                charge['units'] = str(int(charge['units'])+900)
            return httpx.Response(200, json=body)
        return response
    lago._handle = handle
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'matched', report['issues']


def test_recent_acknowledgement_waits_for_lago_processing(gomodel, state, lago):
    row = insert(gomodel, T0)
    make_runner(lago).run_cycle()
    state._conn.execute('UPDATE event_acknowledgements SET first_ack_at = %s', (NOW-timedelta(seconds=5),))
    del lago.stored[('sub_acme', row+':out')]
    report = make_reconciler(lago).run(DAY, END)
    assert report['status'] == 'incomplete'
    assert 'recent_delivery' in codes(report)


def test_explicit_recheck_clears_repaired_mismatch(gomodel, state, lago):
    row = insert(gomodel, T0)
    make_runner(lago).run_cycle()
    event = lago.stored.pop(('sub_acme', row+':out'))
    reconciler = make_reconciler(lago)
    assert reconciler.run(DAY, END)['status'] == 'mismatch'
    lago.stored[('sub_acme', row+':out')] = event
    assert reconciler.run(DAY, END)['status'] == 'matched'
    assert state.latest_reconciliation()['status'] == 'matched'


def test_submillisecond_period_is_incomplete_not_inaccurate(gomodel, state, lago):
    report = make_reconciler(lago).run(DAY+timedelta(microseconds=1), END)
    assert report['status'] == 'incomplete'


def test_future_billing_period_is_not_used_as_historical_evidence(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    lago.current_from += timedelta(days=31)
    lago.current_to += timedelta(days=30)
    assert make_reconciler(lago).run(DAY, END)['status'] == 'incomplete'


def test_day_spanning_two_billing_periods_compares_each_scope(gomodel, state, lago):
    insert(gomodel, T0-timedelta(hours=1))
    make_runner(lago).run_cycle()
    lago.current_from = DAY
    lago.current_to = T0
    period = lago.client()._http.get('/api/v1/customers/acme/current_usage', params={'external_subscription_id': 'sub_acme'}).json()['customer_usage']
    lago.past_periods = [period]
    lago.current_from, lago.current_to = T0, END
    insert(gomodel, T0+timedelta(hours=1))
    make_runner(lago).run_cycle()
    report = make_reconciler(lago, reconciliation_page_size=1).run(DAY, END)
    assert report['status'] == 'matched', report['issues']
    assert len(report['billing_periods']) == 2
    assert {p['evidence'] for p in report['billing_periods']} == {'past', 'current'}


def test_ambiguous_overlapping_historical_periods_are_incomplete(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    period = lago.client()._http.get('/api/v1/customers/acme/current_usage', params={'external_subscription_id': 'sub_acme'}).json()['customer_usage']
    lago.past_periods = [period, deepcopy(period)]
    lago.terminated['sub_acme'] = lago.subscriptions.pop('sub_acme')
    assert make_reconciler(lago).run(DAY, END)['status'] == 'incomplete'
