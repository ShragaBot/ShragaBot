"""
Shared session resolution for PM and GM.

Determines whether to resume an existing Claude Code session or start fresh,
based on conversation history stored in Dataverse. DV is the single source
of truth for session continuity -- no local JSON files.

Usage (from PM or GM thin wrapper):

    from session_utils import resolve_session

    session_id, context_prefix, prev_path = resolve_session(
        dv, mcs_conversation_id, my_version="v19", my_role="pm"
    )
"""
from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime


def _sanitize_odata(value: str) -> str:
    """Escape single quotes for safe OData $filter interpolation."""
    return value.replace("'", "''")


def _find_session_file(session_id: str) -> str | None:
    """Look for a Claude session JSONL file on disk.

    Claude stores sessions under ~/.claude/projects/{encoded-cwd}/{session_id}.jsonl
    We search all project dirs since we don't know the exact encoded cwd.
    """
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return None  # Guard against path traversal
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.is_dir():
        return None
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return str(candidate)
    return None


def _format_conversation_history(rows: list[dict], dir_in: str = "Inbound") -> str:
    """Format DV conversation rows as human-readable history.

    Rows should be in chronological order (oldest first).
    """
    lines = []
    for row in rows:
        created = row.get("createdon", "")
        # Format timestamp nicely if available
        ts = ""
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                ts = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                ts = created[:19]
        direction = row.get("cr_direction", "")
        if direction == dir_in:
            speaker = "User"
        else:
            role_info = row.get("cr_processed_by", "")
            if role_info:
                parts = role_info.split(":")
                speaker = parts[0].upper() if parts else "Assistant"
            else:
                speaker = "Assistant"
        msg = (row.get("cr_message") or "")[:2000]
        if ts:
            lines.append(f"{ts} {speaker}: {msg}")
        else:
            lines.append(f"{speaker}: {msg}")
    return "\n".join(lines)


def _build_context_with_history(rows: list[dict], dir_in: str, note: str,
                                prev_session_id: str | None = None) -> tuple[str, str | None]:
    """Build context prefix from conversation history rows.

    Returns (context_prefix, prev_session_path_or_none).
    """
    history_rows = list(reversed(rows[:20]))  # chronological order
    history_text = _format_conversation_history(history_rows, dir_in=dir_in)
    context = ""
    prev_path = _find_session_file(prev_session_id) if prev_session_id else None
    if history_text:
        n = len(history_rows)
        context = (
            f"[Previous conversation context -- {n} recent messages]\n"
            f"{history_text}\n"
            f"[End of context]\n"
            f"[{note}"
        )
        if prev_path:
            context += f" Previous session file may be at: {prev_path}"
        context += "]\n\n"
    elif prev_path:
        context = f"[Previous session file may be at: {prev_path}]\n\n"
    return context, prev_path


def resolve_session(
    dv,
    mcs_conversation_id: str,
    my_version: str,
    my_role: str,
    *,
    log_fn=None,
    dv_api: str = "",
    conv_table: str = "cr_shraga_conversations",
    dir_in: str = "Inbound",
    dir_out: str = "Outbound",
    st_processed: str = "Processed",
    request_timeout: int = 30,
) -> tuple[str | None, str, str | None]:
    """Determine whether to resume an existing Claude session or start fresh.

    Args:
        dv: DataverseClient instance (from dv_client.py, has .get() method)
        mcs_conversation_id: the MCS conversation ID for this user
        my_version: current release version (e.g., "v19")
        my_role: "pm" or "gm"
        log_fn: optional logging callback (str) -> None
        dv_api: Dataverse API base URL (if empty, uses dv.api_base)
        conv_table: conversations table name
        dir_in/dir_out: direction string values
        st_processed: status string for processed messages
        request_timeout: HTTP timeout for DV queries

    Returns:
        (session_id_or_none, context_prefix, prev_session_path_or_none)

        - session_id_or_none: Claude session ID to resume, or None for fresh
        - context_prefix: string to prepend to the user message
        - prev_session_path_or_none: path to previous session JSONL, or None
    """
    _log = log_fn or (lambda msg: None)
    api_base = dv_api or getattr(dv, "api_base", "")

    my_role_lower = my_role.lower()

    # Guard: empty mcs_conversation_id
    if not mcs_conversation_id:
        _log("[SESSION] Empty mcs_conversation_id -> new session, no context")
        return (None, "", None)

    # ── Step 1: Fetch conversation history ──────────────────────────────
    # We need enough rows to find 10 outbound messages. Fetch up to 50 rows
    # (mix of inbound + outbound) to be safe.
    safe_mcs_id = _sanitize_odata(mcs_conversation_id)
    rows = []
    try:
        url = (
            f"{api_base}/{conv_table}"
            f"?$filter=cr_mcs_conversation_id eq '{safe_mcs_id}'"
            f" and cr_status eq '{st_processed}'"
            f"&$orderby=createdon desc"
            f"&$top=50"
            f"&$select=cr_direction,cr_processed_by,cr_message,createdon"
        )
        resp = dv.get(url, timeout=request_timeout)
        rows = resp.json().get("value", [])
    except Exception as e:
        _log(f"[SESSION] Failed to fetch history for {mcs_conversation_id[:20]}: {e}")
        # Fall through -- no history means new session with no context
        return (None, "", None)

    if not rows:
        _log(f"[SESSION] No previous messages for {mcs_conversation_id[:20]} -> new session")
        return (None, "", None)

    # ── Step 2: Parse most recent outbound's cr_processed_by ────────────
    outbound_rows = [r for r in rows if r.get("cr_direction") == dir_out]
    if not outbound_rows:
        _log(f"[SESSION] No outbound messages found -> new session")
        return (None, "", None)

    most_recent_outbound = outbound_rows[0]  # rows are desc by createdon
    processed_by = most_recent_outbound.get("cr_processed_by") or ""

    if not processed_by:
        # Old message without cr_processed_by -- treat as no previous session
        _log(f"[SESSION] Most recent outbound has no cr_processed_by -> new session with context")
        context, prev_path = _build_context_with_history(
            rows, dir_in, "Note: You are starting a fresh session. Here is recent conversation context.")
        return (None, context, prev_path)

    # Parse: {role}:{version}:{session_id}
    parts = processed_by.split(":", 2)
    if len(parts) < 3:
        _log(f"[SESSION] Malformed cr_processed_by '{processed_by}' -> new session with context")
        # Treat malformed the same as empty -- provide context
        context, prev_path = _build_context_with_history(
            rows, dir_in, "Note: You are starting a fresh session. Here is recent conversation context.")
        return (None, context, prev_path)

    prev_role, prev_version, prev_session_id = parts[0], parts[1], parts[2]

    # ── Step 3: Decision matrix ─────────────────────────────────────────
    same_version = (prev_version == my_version)
    same_role = (prev_role.lower() == my_role_lower)

    if same_version and same_role:
        # RESUME the previous session
        _log(f"[SESSION] Same version+role -> resume {prev_session_id[:8]}...")

        # Step 4: Disk fallback -- check if session file exists
        session_path = _find_session_file(prev_session_id)
        if session_path:
            # Session file exists on disk -- resume
            return (prev_session_id, "", session_path)
        else:
            # Session file not found -- fall back to new session with context
            _log(f"[SESSION] Session file not found for {prev_session_id[:8]} -> fallback to new session")
            context, prev_path = _build_context_with_history(
                rows, dir_in,
                "Note: Your previous session could not be resumed. Starting fresh with context from the last conversation.",
                prev_session_id=prev_session_id)
            return (None, context, prev_path)

    elif same_version and not same_role:
        # Cross-agent handoff: new session + inject messages since last time my role responded
        _log(f"[SESSION] Same version, different role ({prev_role}->{my_role_lower}) -> cross-agent handoff")

        # Find messages since the last time my role responded
        my_last_idx = None
        for i, row in enumerate(outbound_rows):
            pb = row.get("cr_processed_by") or ""
            pb_parts = pb.split(":", 2)
            if len(pb_parts) >= 1 and pb_parts[0].lower() == my_role_lower:
                my_last_idx = i
                break

        # Get the relevant rows (everything after my last response, in the full list)
        if my_last_idx is not None:
            # Find the createdon of my last outbound message
            my_last_created = outbound_rows[my_last_idx].get("createdon", "")
            relevant = [r for r in rows if r.get("createdon", "") > my_last_created]
            relevant = list(reversed(relevant))  # chronological
        else:
            # I never responded in this conversation -- use all history
            relevant = list(reversed(rows[:30]))

        history_text = _format_conversation_history(relevant, dir_in=dir_in)
        context = ""
        prev_path = _find_session_file(prev_session_id)
        if history_text:
            n = len(relevant)
            context = (
                f"[Previous conversation context -- {n} recent messages]\n"
                f"{history_text}\n"
                f"[End of context]\n"
                f"[Note: The previous messages were handled by the {prev_role}. You are the {my_role_lower} picking up this conversation.]\n"
            )
        if prev_path:
            context += f"[Your previous session transcript is available at: {prev_path}]\n"
        context += "\n"
        return (None, context, prev_path)

    else:
        # Different version (regardless of role): new session + context
        _log(f"[SESSION] Version change ({prev_version}->{my_version}) -> new session with context")

        context_rows = list(reversed(rows[:30]))  # chronological
        history_text = _format_conversation_history(context_rows, dir_in=dir_in)
        context = ""
        prev_path = _find_session_file(prev_session_id)
        if history_text:
            n = len(context_rows)
            context = (
                f"[Previous conversation context -- {n} recent messages]\n"
                f"{history_text}\n"
                f"[End of context]\n"
                f"[Note: You are starting a fresh session after a version update. Here is recent conversation context.]\n"
            )
        if prev_path:
            context += f"[Your previous session transcript is available at: {prev_path}]\n"
        context += "\n"
        return (None, context, prev_path)
