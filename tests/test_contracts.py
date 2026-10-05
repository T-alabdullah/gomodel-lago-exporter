from datetime import datetime, timezone

from exporter.contracts import LagoEvent, SendOutcome, SendResult, TokenKind, transaction_id

ROW_ID = "0b7c6a8e-1d2f-4c3b-9a8e-123456789abc"


def make_event(kind: TokenKind = TokenKind.OUTPUT) -> LagoEvent:
    return LagoEvent(
        usage_row_id=ROW_ID,
        kind=kind,
        code="llm_output_tokens",
        external_subscription_id="sub_acme",
        timestamp=datetime(2025, 10, 5, 7, 40, 0, 123000, tzinfo=timezone.utc),
        tokens=412,
        properties={"model": "qwen2.5:0.5b", "output_tokens": 412},
    )


def test_transaction_id_is_row_id_plus_suffix():
    assert transaction_id(ROW_ID, TokenKind.INPUT) == f"{ROW_ID}:in"
    assert transaction_id(ROW_ID, TokenKind.CACHED) == f"{ROW_ID}:cached"
    assert transaction_id(ROW_ID, TokenKind.OUTPUT) == f"{ROW_ID}:out"


def test_same_row_always_gives_same_transaction_id():
    # This is what makes resends safe: Lago dedups on it.
    assert make_event().transaction_id == make_event().transaction_id


def test_payload_matches_lago_format():
    payload = make_event().to_payload()
    assert payload == {
        "transaction_id": f"{ROW_ID}:out",
        "external_subscription_id": "sub_acme",
        "code": "llm_output_tokens",
        "timestamp": 1759650000.123,
        "properties": {"model": "qwen2.5:0.5b", "output_tokens": 412},
    }


def test_duplicate_counts_as_sent():
    assert SendResult(make_event(), SendOutcome.DUPLICATE).counts_as_sent
    assert not SendResult(make_event(), SendOutcome.REJECTED).counts_as_sent