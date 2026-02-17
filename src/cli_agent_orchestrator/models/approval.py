"""Approval request models for explicit waiting-user queueing."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ApprovalStatus(str, Enum):
    """Approval request lifecycle."""

    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class ApprovalRequest(BaseModel):
    """Approval request model."""

    id: int = Field(..., description="Approval request ID")
    terminal_id: str = Field(..., description="Terminal requiring user input")
    provider: str | None = Field(None, description="Provider type at detection time")
    status_reason_code: str | None = Field(None, description="Status reason code for diagnostics")
    prompt_excerpt: str | None = Field(None, description="Recent prompt excerpt")
    source: str = Field(..., description="Source of detection")
    status: ApprovalStatus = Field(..., description="Approval queue status")
    acknowledged_at: datetime | None = Field(None, description="Ack timestamp")
    resolved_at: datetime | None = Field(None, description="Resolution timestamp")
    resolution_sender_id: str | None = Field(None, description="Sender that resolved")
    resolution_message: str | None = Field(None, description="Resolution payload sent")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
