import json
import logging
from typing import List, Dict, Any, Tuple

from .common_models import EventRecord

logger = logging.getLogger(__name__)

# User-approved initial recommended max characters per chunk for Discord
MAX_DISCORD_CHUNK_CHARS = 1800
DEFAULT_SEPARATOR = "\n" + "-" * 40 + "\n"

def build_discord_chunks(
    events: List[EventRecord],
    max_chars: int = MAX_DISCORD_CHUNK_CHARS,
    separator: str = DEFAULT_SEPARATOR
) -> Tuple[List[List[EventRecord]], List[Tuple[int, EventRecord]]]:
    """
    Builds chunks of EventRecords whose formatted messages fit within max_chars.
    Returns:
        chunks: List of grouped EventRecords.
        overlong_msgs: List of (index, EventRecord) that exceeded max_chars alone.
    """
    chunks: List[List[EventRecord]] = []
    current_chunk: List[EventRecord] = []
    current_chunk_len = 0
    overlong_msgs: List[Tuple[int, EventRecord]] = []

    for idx, ev in enumerate(events):
        # formatted_message should be pre-populated or generated
        # If it's missing, we skip or use a generic fallback.
        # In our pipeline, it's expected to be present, but we ensure string len.
        msg = getattr(ev, 'formatted_message', '') or getattr(ev, 'discord_message', '')
        if not msg:
            # Try to fetch from extracted or raw if available, else skip
            msg = f"【{ev.event_type}】{ev.ticker} {ev.title[:80]}"
        
        msg_len = len(msg)
        
        if msg_len > max_chars:
            overlong_msgs.append((idx, ev))
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_chunk_len = 0
            chunks.append([ev])
            logger.warning(f"[DISCORD_AGG_OVERLONG_MESSAGE] Event {ev.event_id[:12]} ({ev.ticker}) length {msg_len} exceeds max {max_chars}.")
            continue

        sep_len = len(separator) if current_chunk else 0
        predicted_len = current_chunk_len + sep_len + msg_len

        if predicted_len > max_chars:
            chunks.append(current_chunk)
            current_chunk = [ev]
            current_chunk_len = msg_len
        else:
            current_chunk.append(ev)
            current_chunk_len = predicted_len

    if current_chunk:
        chunks.append(current_chunk)

    return chunks, overlong_msgs


def render_discord_chunk(chunk: List[EventRecord], separator: str = DEFAULT_SEPARATOR) -> str:
    """Renders a single chunk of EventRecords into a single string."""
    msgs = []
    for ev in chunk:
        msg = getattr(ev, 'formatted_message', '') or getattr(ev, 'discord_message', '')
        if not msg:
            msg = f"【{ev.event_type}】{ev.ticker} {ev.title[:80]}"
        msgs.append(msg)
    return separator.join(msgs)


def mask_webhook_url(url: str) -> str:
    if not url: return ""
    if len(url) < 30: return "***"
    return url[:30] + "..." + url[-5:]


def validate_no_send_guard(
    dry_run: bool,
    send_discord: bool,
    enable_discord_aggregation: bool,
    batch_notify_mode: str,
    webhook_url: str,
    max_items: int
) -> bool:
    """
    Guards to ensure we never post to webhook unless explicitly authorized.
    Step D1 ensures this always blocks.
    Returns:
        True if send is authorized.
        False if blocked (must dry-run).
    """
    import os
    
    # D1 constraints (always block)
    if dry_run or not send_discord:
        return False
        
    if not enable_discord_aggregation:
        return False

    if not webhook_url:
        return False

    if os.getenv("ENABLE_DISCORD_AGG_SEND", "0") != "1":
        return False
        
    if batch_notify_mode not in ("explicit_canary", "pipeline"):
        return False

    if max_items > 5:
        return False

    # For safety in Step D1, even if all above pass, block it here just in case.
    # In Step D2+, we will remove this hardcoded False.
    return False


def dry_run_aggregate_discord_notifications(events: List[EventRecord], max_chars: int = MAX_DISCORD_CHUNK_CHARS) -> Dict[str, Any]:
    """
    Simulates chunking and generates stats for Step D1.
    No actual sending is performed.
    """
    logger.info(f"[DISCORD_AGG_DRY_RUN] Starting aggregation dry-run for {len(events)} events.")
    
    chunks, overlong_msgs = build_discord_chunks(events, max_chars)
    chunk_strings = [render_discord_chunk(c) for c in chunks]
    chunk_lengths = [len(s) for s in chunk_strings]

    input_count = len(events)
    aggregated_count = len(chunks)
    reduction_rate = ((input_count - aggregated_count) / input_count * 100) if input_count else 0
    
    stats = {
        "input_message_count": input_count,
        "current_post_count": input_count,
        "aggregated_post_count": aggregated_count,
        "reduction_count": input_count - aggregated_count,
        "reduction_rate": reduction_rate,
        "chunk_count": len(chunks),
        "rows_per_chunk": (input_count / aggregated_count) if aggregated_count else 0,
        "chars_per_chunk": chunk_lengths,
        "max_chars_per_chunk": max(chunk_lengths) if chunk_lengths else 0,
        "avg_chars_per_chunk": sum(chunk_lengths) / len(chunk_lengths) if chunk_lengths else 0,
        "min_chars_per_chunk": min(chunk_lengths) if chunk_lengths else 0,
        "overlong_message_count": len(overlong_msgs),
        "would_send": False,
        "skipped_send": True,
        "webhook_post_called": False,
        "state_update_called": False,
        "success_update_called": False,
        "preview_path": ""  # to be filled by caller
    }
    
    logger.info(
        f"[DISCORD_AGG_SUMMARY] input={input_count} aggregated={aggregated_count} "
        f"reduction={reduction_rate:.1f}% max_chars={stats['max_chars_per_chunk']} "
        f"overlong={len(overlong_msgs)}"
    )
    
    return stats, chunks, chunk_strings

