"""Single FastAPI entry point for all HTTP routes."""

import asyncio
import fcntl
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from watchdog.observers.polling import PollingObserver

from cli_agent_orchestrator.clients.database import (
    acknowledge_approval_request,
    create_inbox_message,
    get_approval_request,
    get_inbox_db_telemetry_snapshot,
    get_inbox_messages,
    init_db,
    list_approval_requests,
    resolve_approval_request,
)
from cli_agent_orchestrator.constants import (
    CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT,
    CAO_JSONL_CLAUDE_ROOT,
    CAO_JSONL_CODEX_ROOT,
    CAO_SINGLE_WRITER_ALLOW_OVERRIDE,
    CAO_SINGLE_WRITER_ENFORCED,
    CAO_SINGLE_WRITER_LOCKFILE,
    CAO_JSONL_WATCH_ENABLED,
    DEFAULT_PROVIDER,
    INBOX_POLLING_INTERVAL,
    SERVER_HOST,
    SERVER_PORT,
    SERVER_VERSION,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.approval import ApprovalStatus
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import Terminal, TerminalId
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import (
    flow_service,
    inbox_service,
    session_service,
    terminal_service,
)
from cli_agent_orchestrator.services.cleanup_service import cleanup_old_data
from cli_agent_orchestrator.services.inbox_service import JsonlFileHandler, LogFileHandler
from cli_agent_orchestrator.services.terminal_service import OutputMode
from cli_agent_orchestrator.utils.logging import setup_logging

logger = logging.getLogger(__name__)
_WRITER_LOCK_FD: Optional[int] = None


def _acquire_single_writer_lock() -> None:
    """Acquire a process lock to avoid multiple CAO writers on shared DB/watchers."""
    global _WRITER_LOCK_FD
    CAO_SINGLE_WRITER_LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CAO_SINGLE_WRITER_LOCKFILE), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        message = (
            f"Single-writer lock already held at {CAO_SINGLE_WRITER_LOCKFILE}. "
            "Another CAO API instance is active."
        )
        if CAO_SINGLE_WRITER_ENFORCED and not CAO_SINGLE_WRITER_ALLOW_OVERRIDE:
            raise RuntimeError(message)
        logger.warning("%s Override enabled; continuing without lock ownership.", message)
        return

    started_at = datetime.now(timezone.utc).isoformat()
    payload = f"pid={os.getpid()} started_at={started_at}\n"
    os.ftruncate(fd, 0)
    os.write(fd, payload.encode("utf-8"))
    os.fsync(fd)
    _WRITER_LOCK_FD = fd
    logger.info("Acquired single-writer lock: %s", CAO_SINGLE_WRITER_LOCKFILE)


def _release_single_writer_lock() -> None:
    """Release process lock for shared writer resources."""
    global _WRITER_LOCK_FD
    if _WRITER_LOCK_FD is None:
        return
    try:
        fcntl.flock(_WRITER_LOCK_FD, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(_WRITER_LOCK_FD)
    except Exception:
        pass
    _WRITER_LOCK_FD = None
    logger.info("Released single-writer lock: %s", CAO_SINGLE_WRITER_LOCKFILE)


async def flow_daemon():
    """Background task to check and execute flows."""
    logger.info("Flow daemon started")
    while True:
        try:
            flows = flow_service.get_flows_to_run()
            for flow in flows:
                try:
                    executed = flow_service.execute_flow(flow.name)
                    if executed:
                        logger.info(f"Flow '{flow.name}' executed successfully")
                    else:
                        logger.info(f"Flow '{flow.name}' skipped (execute=false)")
                except Exception as e:
                    logger.error(f"Flow '{flow.name}' failed: {e}")
        except Exception as e:
            logger.error(f"Flow daemon error: {e}")

        await asyncio.sleep(60)


async def inbox_poll_daemon():
    """Background task to periodically poll pending inbox deliveries."""
    logger.info("Inbox poll daemon started")
    while True:
        try:
            delivered = await asyncio.to_thread(inbox_service.poll_pending_deliveries_once)
            if delivered:
                logger.info("Inbox poll daemon delivered %s message(s)", delivered)
        except Exception as e:
            logger.error(f"Inbox poll daemon error: {e}")
        await asyncio.sleep(INBOX_POLLING_INTERVAL)


# Response Models
class TerminalOutputResponse(BaseModel):
    output: str
    mode: str


class WorkingDirectoryResponse(BaseModel):
    """Response model for terminal working directory."""

    working_directory: Optional[str] = Field(
        description="Current working directory of the terminal, or None if unavailable"
    )


class ApprovalAckRequest(BaseModel):
    """Payload for approval acknowledgment and optional response delivery."""

    sender_id: str = Field(description="Sender/approver identity")
    response_message: Optional[str] = Field(
        default=None, description="Optional response to send to the waiting terminal"
    )
    auto_send: bool = Field(
        default=True,
        description="When true, response_message is sent immediately to the waiting terminal",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("Starting CLI Agent Orchestrator server...")
    setup_logging()
    _acquire_single_writer_lock()
    init_db()

    # Run cleanup in background
    asyncio.create_task(asyncio.to_thread(cleanup_old_data))

    # Start flow daemon as background task
    daemon_task = asyncio.create_task(flow_daemon())
    inbox_poll_task = asyncio.create_task(inbox_poll_daemon())

    # Start inbox watcher
    inbox_observer = PollingObserver(timeout=INBOX_POLLING_INTERVAL)
    inbox_observer.schedule(LogFileHandler(), str(TERMINAL_LOG_DIR), recursive=False)
    jsonl_watch_paths: List[str] = []
    if CAO_JSONL_WATCH_ENABLED:
        jsonl_handler = JsonlFileHandler()
        for root in (CAO_JSONL_CLAUDE_ROOT, CAO_JSONL_CODEX_ROOT):
            if root.exists():
                inbox_observer.schedule(jsonl_handler, str(root), recursive=True)
                jsonl_watch_paths.append(str(root))
                logger.info("JSONL watcher path added: %s", root)
            else:
                logger.info("JSONL watcher path missing, skipping: %s", root)
    inbox_observer.start()
    inbox_service.configure_watcher_telemetry(
        jsonl_watch_enabled=CAO_JSONL_WATCH_ENABLED,
        jsonl_watch_paths=jsonl_watch_paths,
        observer_started=True,
    )
    logger.info("Inbox watcher started (PollingObserver)")

    yield

    # Stop inbox observer
    inbox_observer.stop()
    inbox_observer.join()
    inbox_service.configure_watcher_telemetry(
        jsonl_watch_enabled=CAO_JSONL_WATCH_ENABLED,
        jsonl_watch_paths=jsonl_watch_paths,
        observer_started=False,
    )
    logger.info("Inbox watcher stopped")

    # Cancel daemon on shutdown
    daemon_task.cancel()
    inbox_poll_task.cancel()
    try:
        await daemon_task
    except asyncio.CancelledError:
        pass
    try:
        await inbox_poll_task
    except asyncio.CancelledError:
        pass

    logger.info("Shutting down CLI Agent Orchestrator server...")
    _release_single_writer_lock()


app = FastAPI(
    title="CLI Agent Orchestrator",
    description="Simplified CLI Agent Orchestrator API",
    version=SERVER_VERSION,
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "cli-agent-orchestrator"}


@app.get("/diagnostics/inbox/telemetry")
async def inbox_telemetry() -> Dict:
    """Return inbox delivery telemetry counters and rates."""
    payload = dict(inbox_service.get_inbox_telemetry_snapshot())
    payload["db"] = get_inbox_db_telemetry_snapshot()
    return payload


@app.get("/diagnostics/jsonl/gates")
async def jsonl_gate_diagnostics() -> Dict:
    """Return JSONL migration gate evaluation."""
    return inbox_service.evaluate_jsonl_canary_gates()


@app.post("/diagnostics/jsonl/reset")
async def jsonl_reset_diagnostics(reset_parser: bool = True) -> Dict:
    """Reset JSONL canary telemetry counters for a fresh verification window."""
    return inbox_service.reset_jsonl_canary_state(reset_parser=reset_parser)


@app.post("/sessions", response_model=Terminal, status_code=status.HTTP_201_CREATED)
async def create_session(
    agent_profile: str,
    provider: str = DEFAULT_PROVIDER,
    session_name: Optional[str] = None,
    working_directory: Optional[str] = None,
) -> Terminal:
    """Create a new session with exactly one terminal."""
    try:
        result = terminal_service.create_terminal(
            provider=provider,
            agent_profile=agent_profile,
            session_name=session_name,
            new_session=True,
            working_directory=working_directory,
        )
        return result

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create session: {str(e)}",
        )


@app.get("/sessions")
async def list_sessions() -> List[Dict]:
    try:
        return session_service.list_sessions()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sessions: {str(e)}",
        )


@app.get("/sessions/{session_name}")
async def get_session(session_name: str) -> Dict:
    try:
        return session_service.get_session(session_name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get session: {str(e)}",
        )


@app.delete("/sessions/{session_name}")
async def delete_session(session_name: str) -> Dict:
    try:
        success = session_service.delete_session(session_name)
        return {"success": success}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete session: {str(e)}",
        )


@app.post(
    "/sessions/{session_name}/terminals",
    response_model=Terminal,
    status_code=status.HTTP_201_CREATED,
)
async def create_terminal_in_session(
    session_name: str,
    agent_profile: str,
    provider: str = DEFAULT_PROVIDER,
    working_directory: Optional[str] = None,
) -> Terminal:
    """Create additional terminal in existing session."""
    try:
        result = terminal_service.create_terminal(
            provider=provider,
            agent_profile=agent_profile,
            session_name=session_name,
            new_session=False,
            working_directory=working_directory,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create terminal: {str(e)}",
        )


@app.get("/sessions/{session_name}/terminals")
async def list_terminals_in_session(session_name: str) -> List[Dict]:
    """List all terminals in a session."""
    try:
        from cli_agent_orchestrator.clients.database import list_terminals_by_session

        return list_terminals_by_session(session_name)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list terminals: {str(e)}",
        )


@app.get("/terminals/{terminal_id}", response_model=Terminal)
async def get_terminal(terminal_id: TerminalId) -> Terminal:
    try:
        terminal = terminal_service.get_terminal(terminal_id)
        return Terminal(**terminal)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get terminal: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/working-directory", response_model=WorkingDirectoryResponse)
async def get_terminal_working_directory(terminal_id: TerminalId) -> WorkingDirectoryResponse:
    """Get the current working directory of a terminal's pane."""
    try:
        working_directory = terminal_service.get_working_directory(terminal_id)
        return WorkingDirectoryResponse(working_directory=working_directory)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get working directory: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/input")
async def send_terminal_input(terminal_id: TerminalId, message: str) -> Dict:
    try:
        success = terminal_service.send_input(terminal_id, message)
        return {"success": success}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send input: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/output", response_model=TerminalOutputResponse)
async def get_terminal_output(
    terminal_id: TerminalId, mode: OutputMode = OutputMode.FULL
) -> TerminalOutputResponse:
    try:
        output = terminal_service.get_output(terminal_id, mode)
        return TerminalOutputResponse(output=output, mode=mode)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get output: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/exit")
async def exit_terminal(terminal_id: TerminalId) -> Dict:
    """Send provider-specific exit command to terminal."""
    try:
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            raise ValueError(f"Provider not found for terminal {terminal_id}")
        exit_command = provider.exit_cli()
        terminal_service.send_input(terminal_id, exit_command)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to exit terminal: {str(e)}",
        )


@app.delete("/terminals/{terminal_id}")
async def delete_terminal(terminal_id: TerminalId) -> Dict:
    """Delete a terminal."""
    try:
        success = terminal_service.delete_terminal(terminal_id)
        return {"success": success}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete terminal: {str(e)}",
        )


@app.post("/terminals/{receiver_id}/inbox/messages", status_code=status.HTTP_201_CREATED)
async def create_inbox_message_endpoint(
    receiver_id: TerminalId,
    sender_id: str,
    message: str,
    idempotency_key: Optional[str] = None,
    max_attempts: Optional[int] = Query(
        default=None, ge=1, le=100, description="Maximum delivery attempts before dead-letter"
    ),
    requeue_terminal_state: bool = Query(
        default=CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT,
        description="When true, dead_letter/failed rows with matching idempotency_key are reset to pending",
    ),
) -> Dict:
    """Create inbox message and attempt immediate delivery."""
    try:
        logger.info(
            "Inbox create request receiver=%s sender=%s key=%s requeue_terminal_state=%s",
            receiver_id,
            sender_id,
            idempotency_key,
            requeue_terminal_state,
        )
        inbox_msg = create_inbox_message(
            sender_id,
            receiver_id,
            message,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            requeue_terminal_state=requeue_terminal_state,
        )
        inbox_service.check_and_send_pending_messages(receiver_id)

        return {
            "success": True,
            "message_id": inbox_msg.id,
            "sender_id": inbox_msg.sender_id,
            "receiver_id": inbox_msg.receiver_id,
            "idempotency_key": getattr(inbox_msg, "idempotency_key", None),
            "status": (
                inbox_msg.status.value
                if hasattr(inbox_msg, "status") and hasattr(inbox_msg.status, "value")
                else str(getattr(inbox_msg, "status", "pending"))
            ),
            "created_at": inbox_msg.created_at.isoformat(),
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create inbox message: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/inbox/messages")
async def get_inbox_messages_endpoint(
    terminal_id: TerminalId,
    limit: int = Query(default=10, le=100, description="Maximum number of messages to retrieve"),
    status_param: Optional[str] = Query(
        default=None, alias="status", description="Filter by message status"
    ),
) -> List[Dict]:
    """Get inbox messages for a terminal.

    Args:
        terminal_id: Terminal ID to get messages for
        limit: Maximum number of messages to return (default: 10, max: 100)
        status_param: Optional filter by message status ('pending', 'delivered', 'failed')

    Returns:
        List of inbox messages with sender_id, message, created_at, status
    """
    try:
        # Convert status filter if provided
        status_filter = None
        if status_param:
            try:
                status_filter = MessageStatus(status_param)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Invalid status: {status_param}. "
                        "Valid values: pending, retrying, delivered, failed, dead_letter"
                    ),
                )

        # Get messages using existing database function
        messages = get_inbox_messages(terminal_id, limit=limit, status=status_filter)

        # Convert to response format
        result = []
        for msg in messages:
            result.append(
                {
                    "id": msg.id,
                    "sender_id": msg.sender_id,
                    "receiver_id": msg.receiver_id,
                    "message": msg.message,
                    "status": msg.status.value,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
            )

        return result

    except HTTPException:
        # Re-raise HTTPException (validation errors)
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve inbox messages: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/approvals")
async def list_approval_requests_endpoint(
    terminal_id: TerminalId,
    limit: int = Query(default=20, ge=1, le=200, description="Maximum approvals to retrieve"),
    status_param: Optional[str] = Query(
        default=None, alias="status", description="Filter by approval status"
    ),
) -> List[Dict]:
    """List approval requests for a terminal."""
    try:
        status_filter = None
        if status_param:
            try:
                status_filter = ApprovalStatus(status_param)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Invalid status: {status_param}. "
                        "Valid values: pending, acknowledged, resolved"
                    ),
                )

        approvals = list_approval_requests(terminal_id, limit=limit, status=status_filter)
        return [
            {
                "id": approval.id,
                "terminal_id": approval.terminal_id,
                "provider": approval.provider,
                "status_reason_code": approval.status_reason_code,
                "prompt_excerpt": approval.prompt_excerpt,
                "source": approval.source,
                "status": approval.status.value,
                "acknowledged_at": (
                    approval.acknowledged_at.isoformat() if approval.acknowledged_at else None
                ),
                "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
                "resolution_sender_id": approval.resolution_sender_id,
                "resolution_message": approval.resolution_message,
                "created_at": approval.created_at.isoformat(),
                "updated_at": approval.updated_at.isoformat(),
            }
            for approval in approvals
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list approvals: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/approvals/{approval_id}/ack")
async def acknowledge_approval_request_endpoint(
    terminal_id: TerminalId,
    approval_id: Annotated[int, Path(ge=1)],
    payload: ApprovalAckRequest,
) -> Dict:
    """Acknowledge an approval request and optionally send a resolving response."""
    try:
        approval = get_approval_request(terminal_id, approval_id)
        if approval is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request {approval_id} not found for terminal {terminal_id}",
            )

        if payload.auto_send and not payload.response_message:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="response_message is required when auto_send=true",
            )

        acknowledged = acknowledge_approval_request(terminal_id, approval_id)
        if acknowledged is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request {approval_id} not found for terminal {terminal_id}",
            )

        sent = False
        final_status = acknowledged.status
        if payload.auto_send and payload.response_message:
            terminal_service.send_input(terminal_id, payload.response_message)
            resolved = resolve_approval_request(
                terminal_id,
                approval_id,
                sender_id=payload.sender_id,
                resolution_message=payload.response_message,
            )
            if resolved is not None:
                final_status = resolved.status
            sent = True

        return {
            "success": True,
            "approval_id": approval_id,
            "terminal_id": terminal_id,
            "status": final_status.value,
            "sent_response": sent,
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to acknowledge approval request: {str(e)}",
        )


def main():
    """Entry point for cao-server command."""
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)


if __name__ == "__main__":
    main()
