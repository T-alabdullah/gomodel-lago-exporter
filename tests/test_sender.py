"""Tests for the sender, against a fake Lago (see tests/fake_lago.py).

Every failure the task lists is simulated here: duplicates, a bad event in a
batch, unknown subscriptions, rate limiting, server errors, Lago being down,
and a wrong API key.
"""

import uuid
from datetime import timedelta

import pytest

from exporter.contracts import LagoEvent, SendOutcome, TokenKind
from exporter.lago_client import LagoAuthError
from exporter.sender import Sender
from tests.factories import T0, settings
from tests.fake_lago import BAD_CODE, FakeLago

BATCH = ("POST", "/api/v1/events/batch")
SINGLE = ("POST", "/api/v1/events")


def event(sub: str = "sub_acme", code: str = "llm_output_tokens", row_id: str | None = None) -> LagoEvent:
    return LagoEvent(
        usage_row_id=row_id or str(uuid.uuid4()),
        kind=TokenKind.OUTPUT,
        code=code,
        external_subscription_id=sub,
        timestamp=T0,
        tokens=5,
        properties={"model": "qwen2.5:0.5b", "output_tokens": 5},
    )


@pytest.fixture
def lago():
    return FakeLago()


@pytest.fixture
def sleeps():
    return []   # every backoff delay the sender waited


def make_sender(lago, sleeps, **overrides):
    return Sender(lago.client(), settings(**overrides), sleep=sleeps.append, rand=lambda: 1.0)


def outcomes(results):
    return [r.outcome for r in results]


# --- Happy path ----------------------------------------------------------

def test_events_are_sent_in_one_batch(lago, sleeps):
    results = make_sender(lago, sleeps).send([event(), event(), event()])
    assert outcomes(results) == [SendOutcome.ACCEPTED] * 3
    assert lago.count(*BATCH) == 1 and len(lago.stored) == 3


def test_large_sends_are_split_into_batches_of_100(lago, sleeps):
    make_sender(lago, sleeps).send([event() for _ in range(250)])
    assert lago.count(*BATCH) == 3          # 100 + 100 + 50
    assert len(lago.stored) == 250


# --- Duplicates: never billed twice --------------------------------------

def test_resending_the_same_events_counts_as_sent(lago, sleeps):
    sender = make_sender(lago, sleeps)
    events = [event(), event()]
    sender.send(events)
    results = sender.send(events)            # e.g. after a restart
    assert outcomes(results) == [SendOutcome.DUPLICATE] * 2
    assert all(r.counts_as_sent for r in results)
    assert len(lago.stored) == 2             # still only two: nothing billed twice


def test_batch_with_some_duplicates_still_delivers_the_new_ones(lago, sleeps):
    sender = make_sender(lago, sleeps)
    old = event()
    sender.send([old])
    new = event()
    results = sender.send([old, new])        # Lago refuses the whole batch...
    assert outcomes(results) == [SendOutcome.DUPLICATE, SendOutcome.ACCEPTED]
    assert lago.count(*SINGLE) == 2          # ...so each was resent on its own
    assert len(lago.stored) == 2


# --- A bad event never blocks the good ones ------------------------------

def test_one_bad_event_is_rejected_and_the_rest_are_delivered(lago, sleeps):
    good1, bad, good2 = event(), event(code=BAD_CODE), event()
    results = make_sender(lago, sleeps).send([good1, bad, good2])
    assert outcomes(results) == [SendOutcome.ACCEPTED, SendOutcome.REJECTED, SendOutcome.ACCEPTED]
    assert "value_is_invalid" in results[1].error
    assert len(lago.stored) == 2


def test_unknown_subscription_is_rejected_without_sending(lago, sleeps):
    results = make_sender(lago, sleeps).send([event("sub_ghost"), event("sub_acme")])
    assert outcomes(results) == [SendOutcome.REJECTED, SendOutcome.ACCEPTED]
    assert "sub_ghost" in results[0].error
    assert all(sub != "sub_ghost" for sub, _ in lago.stored)


def test_usage_before_subscription_start_is_rejected(lago, sleeps):
    # Lago would accept this event but never bill it, so we catch it ourselves.
    lago.subscriptions["sub_acme"] = T0 + timedelta(minutes=25)     # started after the usage
    results = make_sender(lago, sleeps).send([event("sub_acme")])
    assert outcomes(results) == [SendOutcome.REJECTED]
    assert "before subscription 'sub_acme' started" in results[0].error
    assert lago.stored == {}

def test_subscription_lookups_are_cached(lago, sleeps):
    sender = make_sender(lago, sleeps)
    sender.send([event()])
    sender.send([event()])
    assert lago.count("GET", "/api/v1/subscriptions/sub_acme") == 1


# --- Temporary failures are retried --------------------------------------

def test_server_error_is_retried_then_succeeds(lago, sleeps):
    sender = make_sender(lago, sleeps)
    sender.send([event()])                   # warm the subscription cache
    lago.fail_next = [503, 503]
    results = sender.send([event()])
    assert outcomes(results) == [SendOutcome.ACCEPTED]
    assert sleeps == [0.5, 1.0]              # waited, then waited longer


def test_lago_down_is_retried_then_succeeds(lago, sleeps):
    sender = make_sender(lago, sleeps)
    sender.send([event()])
    lago.fail_next = ["network", "network", "network"]
    assert outcomes(sender.send([event()])) == [SendOutcome.ACCEPTED]
    assert len(sleeps) == 3


def test_rate_limit_that_never_ends_gives_retry_not_loss(lago, sleeps):
    sender = make_sender(lago, sleeps, max_retries=3)
    sender.send([event()])
    lago.fail_next = [429] * 10
    results = sender.send([event(), event()])
    assert outcomes(results) == [SendOutcome.RETRY] * 2      # the runner will try again
    assert "gave up after 4 attempts" in results[0].error
    assert len(sleeps) == 3


def test_lago_down_during_subscription_check_gives_retry(lago, sleeps):
    lago.fail_next = ["network"] * 10
    results = make_sender(lago, sleeps, max_retries=2).send([event()])
    assert outcomes(results) == [SendOutcome.RETRY]


def test_backoff_grows_and_is_capped(lago, sleeps):
    sender = Sender(lago.client(), settings(backoff_base_seconds=1, backoff_max_seconds=5),
                    sleep=sleeps.append, rand=lambda: 1.0)
    assert [sender.backoff_delay(a) for a in range(5)] == [1, 2, 4, 5, 5]


def test_jitter_keeps_delay_between_half_and_full(lago, sleeps):
    low = Sender(lago.client(), settings(), rand=lambda: 0.0).backoff_delay(3)
    high = Sender(lago.client(), settings(), rand=lambda: 1.0).backoff_delay(3)
    assert (low, high) == (2.0, 4.0)


# --- A wrong API key stops everything ------------------------------------

def test_wrong_api_key_raises_instead_of_rejecting_events(lago, sleeps):
    lago.fail_next = [401]
    with pytest.raises(LagoAuthError):
        make_sender(lago, sleeps).send([event()])

def test_mixed_duplicate_and_other_errors_are_not_success():
    import httpx
    from exporter.lago_client import classify_single
    response = httpx.Response(422, json={'error_details': {
        'transaction_id': ['value_already_exist', 'value_is_invalid'],
    }})
    assert classify_single(response)[0] is SendOutcome.REJECTED


@pytest.mark.parametrize('body', [{}, {'subscription': {}}, {'subscription': {'started_at': None}},
                                   {'subscription': {'started_at': '2026-01-01'}}])
def test_invalid_subscription_response_is_retryable(body):
    import httpx
    from exporter.lago_client import LagoClient
    client = LagoClient('http://lago.test', 'test', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body),
    ))
    try:
        results = Sender(client, settings(), sleep=lambda _: None).send([event()])
        assert outcomes(results) == [SendOutcome.RETRY]
    finally:
        client.close()


def test_unexpected_subscription_status_is_not_permanent_rejection(lago, sleeps):
    lago.fail_next = [400]
    assert outcomes(make_sender(lago, sleeps).send([event()])) == [SendOutcome.RETRY]


def test_subscription_id_is_encoded_as_single_path_segment():
    import httpx
    from exporter.lago_client import LagoClient
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(404, json={})
    client = LagoClient('http://lago.test', 'key', transport=httpx.MockTransport(handle))
    try:
        client.get_subscription('sub/one?status=terminated')
        assert requests[0].url.raw_path == b'/api/v1/subscriptions/sub%2Fone%3Fstatus%3Dterminated'
    finally:
        client.close()
