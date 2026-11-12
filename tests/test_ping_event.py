import pytest
from pydantic import ValidationError

from app.messaging.ping_event import PingEvent


VALID_EVENT = {
    "eventId": "4c8f73e2-f4b4-4c64-8d55-19300f0a0001",
    "correlationId": "correlation-1",
    "occurredAt": "2026-11-11T13:00:00Z",
    "payload": {"message": "ping"},
}


def test_ping_event_accepts_contract_payload():
    event = PingEvent.model_validate(VALID_EVENT)

    assert str(event.event_id) == VALID_EVENT["eventId"]
    assert event.correlation_id == "correlation-1"


@pytest.mark.parametrize(
    "update",
    [
        {"eventId": "not-a-uuid"},
        {"correlationId": ""},
        {"occurredAt": "2026-11-11T13:00:00"},
        {"payload": {"message": "ping", "extra": True}},
        {"extra": True},
    ],
)
def test_ping_event_rejects_invalid_contract_payload(update):
    with pytest.raises(ValidationError):
        PingEvent.model_validate(VALID_EVENT | update)
