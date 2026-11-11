from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


class PingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    event_id: UUID = Field(alias="eventId")
    correlation_id: str = Field(alias="correlationId", min_length=1)
    occurred_at: datetime = Field(alias="occurredAt")
    payload: PingPayload

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurredAt must include a timezone")
        return value
