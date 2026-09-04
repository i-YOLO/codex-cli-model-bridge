#!/usr/bin/env python3
"""Pure helpers for Codex compaction compatibility.

The helpers deliberately keep summaries in memory. They never log request
bodies, account data, credentials, or generated summaries.
"""

from __future__ import annotations

import base64
import binascii
import copy
import gzip
import json
import time
import uuid
import zlib

try:
    from compression import zstd as _zstd  # Python 3.14+ (PEP 784)
except ImportError:  # pragma: no cover - depends on interpreter version
    _zstd = None


OCX_COMPACTION_PREFIX = "ocx1:"
COMPACTION_ITEM_TYPES = {"compaction", "compaction_summary", "context_compaction"}
NATIVE_MODEL_PREFIXES = ("gpt-", "codex-", "o1-", "o3-", "o4-")
COMPACT_V1_RETAINED_CHAR_BUDGET = 20_000 * 4
MAX_COMPACTION_BODY_BYTES = 32 * 1024 * 1024

COMPACT_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, exact markers, identifiers, or references needed to continue

Preserve exact marker strings and identifiers verbatim. Be concise, structured, and focused on helping the next LLM continue without duplicating completed work. Return only the summary."""

SUMMARY_PREFIX = "Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"
OPAQUE_COMPACTION_NOTE = "[earlier conversation was compacted; the summary is stored in a format this model cannot read]"


class CompactionError(RuntimeError):
    """Raised when a compaction payload cannot be handled safely."""


def encode_compaction_summary(summary: str) -> str:
    value = summary.strip()
    if not value:
        raise CompactionError("summary is empty")
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return OCX_COMPACTION_PREFIX + encoded


def decode_compaction_summary(encrypted_content: object) -> str | None:
    if not isinstance(encrypted_content, str) or not encrypted_content.startswith(OCX_COMPACTION_PREFIX):
        return None
    raw = encrypted_content[len(OCX_COMPACTION_PREFIX) :]
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded if decoded.strip() else None


def is_compaction_item(value: object) -> bool:
    return isinstance(value, dict) and value.get("type") in COMPACTION_ITEM_TYPES


def is_native_compaction_model(model: object) -> bool:
    return isinstance(model, str) and model.startswith(NATIVE_MODEL_PREFIXES)


def is_v2_compaction_request(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    items = payload.get("input")
    return isinstance(items, list) and any(
        isinstance(item, dict) and item.get("type") == "compaction_trigger" for item in items
    )


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    values: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"input_image", "image", "image_url", "computer_screenshot"}:
            continue
        value = block.get("text")
        if isinstance(value, str):
            values.append(value)
    return "\n".join(value for value in values if value.strip())


def _summary_text(summary: object) -> str:
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return ""
    values: list[str] = []
    for item in summary:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            values.append(item["text"])
        elif isinstance(item, str):
            values.append(item)
    return "\n".join(value for value in values if value.strip())


def render_summary_transcript(payload: dict) -> str:
    """Convert accepted Responses items to a provider-neutral text transcript."""

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        return raw_input
    if not isinstance(raw_input, list):
        return ""
    sections: list[str] = []
    for item in raw_input:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "compaction_trigger":
            continue
        if item_type in COMPACTION_ITEM_TYPES:
            decoded = decode_compaction_summary(item.get("encrypted_content"))
            sections.append(f"[prior compacted context]\n{decoded or OPAQUE_COMPACTION_NOTE}")
            continue
        if item_type == "message" or isinstance(item.get("role"), str):
            text = _content_text(item.get("content"))
            if text.strip():
                sections.append(f"[{item.get('role', 'message')}]\n{text}")
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            name = item.get("name") if isinstance(item.get("name"), str) else "tool"
            arguments = item.get("arguments") or item.get("input")
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if isinstance(arguments, str) and arguments.strip():
                sections.append(f"[tool call: {name}]\n{arguments}")
            continue
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            output = item.get("output") if "output" in item else item.get("content")
            if isinstance(output, (dict, list)):
                output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            if isinstance(output, str) and output.strip():
                sections.append(f"[tool result]\n{output}")
            continue
        if item_type == "reasoning":
            text = _summary_text(item.get("summary"))
            if text.strip():
                sections.append(f"[reasoning summary]\n{text}")
            continue
        for key in ("message", "text", "task"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                sections.append(f"[{item_type or 'context'}]\n{value}")
                break
    return "\n\n".join(sections)


def build_summary_payload(payload: dict) -> dict:
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise CompactionError("model is missing")
    transcript = render_summary_transcript(payload)
    if not transcript.strip():
        transcript = "(No readable text remained after removing tools and image payloads.)"
    prompt = f"{COMPACT_PROMPT}\n\nCONVERSATION TO SUMMARIZE:\n{transcript}"
    return {
        "model": model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "max_output_tokens": 4096,
        "store": False,
        "stream": False,
    }


def extract_output_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    values: list[str] = []
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                values.append(block["text"])
    return "\n".join(value for value in values if value.strip()).strip()


def extract_user_messages(input_items: object) -> list[str]:
    if not isinstance(input_items, list):
        return []
    values: list[str] = []
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {None, "message"} or item.get("role") != "user":
            continue
        text = _content_text(item.get("content"))
        if text.strip():
            values.append(text)
    return values


def _message_item(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def build_v1_output(input_items: object, summary: str) -> list[dict]:
    selected: list[str] = []
    remaining = COMPACT_V1_RETAINED_CHAR_BUDGET
    for message in reversed(extract_user_messages(input_items)):
        if remaining <= 0:
            break
        if len(message) <= remaining:
            selected.append(message)
            remaining -= len(message)
        else:
            selected.append(message[-remaining:])
            remaining = 0
    selected.reverse()
    summary_text = f"{SUMMARY_PREFIX}\n{summary.strip()}"
    return [*(_message_item(text) for text in selected), _message_item(summary_text)]


def prepare_forward_payload(payload: dict) -> tuple[dict, dict[str, int]]:
    """Decode synthetic summaries and clean only invalid native reasoning fields."""

    updated = copy.deepcopy(payload)
    decoded_count = 0
    reasoning_cleaned = 0
    items = updated.get("input")
    if not isinstance(items, list):
        return updated, {"decoded_compactions": 0, "reasoning_items_cleaned": 0}
    native = is_native_compaction_model(updated.get("model"))
    rendered: list[object] = []
    for item in items:
        if not isinstance(item, dict):
            rendered.append(item)
            continue
        if item.get("type") in COMPACTION_ITEM_TYPES:
            decoded = decode_compaction_summary(item.get("encrypted_content"))
            if decoded is not None:
                rendered.append(_message_item(f"{SUMMARY_PREFIX}\n\n{decoded}"))
                decoded_count += 1
                continue
        clone = copy.deepcopy(item)
        if native and clone.get("type") == "reasoning" and "content" in clone:
            clone.pop("content", None)
            reasoning_cleaned += 1
        rendered.append(clone)
    updated["input"] = rendered
    return updated, {
        "decoded_compactions": decoded_count,
        "reasoning_items_cleaned": reasoning_cleaned,
    }


def encoding_supported(encoding: str) -> bool:
    lowered = (encoding or "identity").strip().lower()
    if lowered in {"", "identity", "gzip", "x-gzip", "deflate"}:
        return True
    if lowered in {"zstd", "zstandard"}:
        return _zstd is not None
    return False


def decode_request_body(body: bytes, encoding: str, max_bytes: int = MAX_COMPACTION_BODY_BYTES) -> bytes:
    lowered = (encoding or "identity").strip().lower()
    if lowered in {"", "identity"}:
        decoded = body
    elif lowered in {"gzip", "x-gzip"}:
        decoded = gzip.decompress(body)
    elif lowered == "deflate":
        try:
            decoded = zlib.decompress(body)
        except zlib.error:
            decoded = zlib.decompress(body, -zlib.MAX_WBITS)
    elif lowered in {"zstd", "zstandard"} and _zstd is not None:
        decoded = _zstd.decompress(body)
    else:
        raise CompactionError(f"unsupported content encoding: {lowered}")
    if len(decoded) > max_bytes:
        raise CompactionError("request body exceeds 32 MiB after decoding")
    return decoded


def encode_request_body(body: bytes, encoding: str) -> bytes:
    lowered = (encoding or "identity").strip().lower()
    if lowered in {"", "identity"}:
        return body
    if lowered in {"gzip", "x-gzip"}:
        return gzip.compress(body, mtime=0)
    if lowered == "deflate":
        return zlib.compress(body)
    if lowered in {"zstd", "zstandard"} and _zstd is not None:
        return _zstd.compress(body)
    raise CompactionError(f"unsupported content encoding: {lowered}")


def _response_shell(response_id: str, model: str, status: str, output: list[dict]) -> dict:
    now = int(time.time())
    return {
        "id": response_id,
        "object": "response",
        "created_at": now,
        "completed_at": now if status == "completed" else None,
        "status": status,
        "background": False,
        "error": None,
        "frequency_penalty": 0.0,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "max_tool_calls": None,
        "metadata": {},
        "model": model,
        "moderation": None,
        "output": output,
        "parallel_tool_calls": True,
        "presence_penalty": 0.0,
        "previous_response_id": None,
        "prompt_cache_key": None,
        "prompt_cache_retention": None,
        "reasoning": {"context": "all_turns", "effort": "medium", "mode": "standard", "summary": None},
        "safety_identifier": None,
        "service_tier": "default",
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}, "verbosity": "medium"},
        "tool_choice": "none",
        "tool_usage": {},
        "tools": [],
        "top_logprobs": 0,
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": (
            {
                "input_tokens": 0,
                "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
                "output_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 0,
            }
            if status == "completed"
            else None
        ),
        "user": None,
    }


def build_v2_response(model: str, summary: str) -> tuple[dict, list[dict]]:
    response_id = f"resp_bridge_{uuid.uuid4().hex}"
    item = {
        "id": f"cmp_bridge_{uuid.uuid4().hex}",
        "type": "compaction",
        "encrypted_content": encode_compaction_summary(summary),
    }
    created = _response_shell(response_id, model, "in_progress", [])
    completed = _response_shell(response_id, model, "completed", [item])
    events = [
        {"type": "response.created", "sequence_number": 0, "response": created},
        {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": item},
        {"type": "response.output_item.done", "sequence_number": 2, "output_index": 0, "item": item},
        {"type": "response.completed", "sequence_number": 3, "response": completed},
    ]
    return completed, events


def build_v2_failure_events(model: str, error_type: str = "compaction_error") -> list[dict]:
    response_id = f"resp_bridge_{uuid.uuid4().hex}"
    created = _response_shell(response_id, model, "in_progress", [])
    failed = _response_shell(response_id, model, "failed", [])
    failed["error"] = {"type": error_type, "code": error_type, "message": "Compaction failed in the current model route."}
    return [
        {"type": "response.created", "sequence_number": 0, "response": created},
        {"type": "response.failed", "sequence_number": 1, "response": failed},
    ]


def render_sse(events: list[dict]) -> bytes:
    chunks: list[bytes] = []
    for event in events:
        event_type = str(event.get("type", "message"))
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        chunks.append(f"event: {event_type}\ndata: {data}\n\n".encode("utf-8"))
    chunks.append(b"data: [DONE]\n\n")
    return b"".join(chunks)
