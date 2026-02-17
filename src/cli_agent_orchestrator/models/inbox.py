"""Inbox message models."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class MessageStatus(str, Enum):
    """Message status enumeration."""

    PENDING = "pending"
    RETRYING = "retrying"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class InboxMessage(BaseModel):
    """Inbox message model."""

    id: int = Field(..., description="Message ID")
    sender_id: str = Field(..., description="Sender terminal ID")
    receiver_id: str = Field(..., description="Receiver terminal ID")
    message: str = Field(..., description="Message content")
    idempotency_key: str = Field("", description="Idempotency key for deduplication")
    status: MessageStatus = Field(..., description="Message status")
    attempt_count: int = Field(0, description="Number of send attempts")
    max_attempts: int = Field(1, description="Maximum attempts before dead-letter")
    next_attempt_at: datetime = Field(default_factory=datetime.now, description="Next eligible attempt time")
    last_attempt_at: datetime | None = Field(None, description="Most recent attempt time")
    delivered_at: datetime | None = Field(None, description="Delivery timestamp")
    failed_at: datetime | None = Field(None, description="Dead-letter/failure timestamp")
    failure_reason: str | None = Field(None, description="Last failure reason")
    created_at: datetime = Field(..., description="Creation timestamp")
