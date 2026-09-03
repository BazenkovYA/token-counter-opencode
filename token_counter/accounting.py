from __future__ import annotations

import codecs
import json
import time
import uuid
from datetime import datetime, timezone

MAX_TOKEN = 2**53 - 1


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def integer(value):
    return value if type(value) is int and 0 <= value <= MAX_TOKEN else None


def identifier(value, limit=256):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Некорректный идентификатор")
    return value


class Usage:
    def __init__(self):
        self.fields = {}
        self.details = {}
        self.observations = []
        self.flags = set()
        self.finish_reason = None
        self.response_model = None
        self.response_id = None
        self.done = False
        self.error = False

    def observe(self, payload):
        if not isinstance(payload, dict):
            return
        if payload.get("error"):
            self.error = True
        for name in ("model", "id"):
            try:
                value = identifier(payload.get(name))
            except ValueError:
                value = None
            if value:
                setattr(self, "response_model" if name == "model" else "response_id", value)
        for choice in payload.get("choices") or []:
            if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                # Never persist arbitrary upstream text as a status.
                reason = choice["finish_reason"]
                self.finish_reason = reason if reason in {"stop", "length", "tool_calls", "function_call", "content_filter", "error"} else "other"
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return
        snapshot = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key not in usage or usage[key] is None:
                continue
            value = integer(usage[key])
            if value is None:
                self.flags.add("invalid_usage")
                continue
            if key in self.fields and value < self.fields[key]:
                self.flags.add("non_monotonic_usage")
            self.fields[key] = snapshot[key] = value
        for group, keys in {
            "prompt_tokens_details": ("cached_tokens", "audio_tokens"),
            "completion_tokens_details": ("reasoning_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"),
        }.items():
            obj = usage.get(group)
            if isinstance(obj, dict):
                for key in keys:
                    value = integer(obj.get(key))
                    if value is not None:
                        self.details[f"{group}.{key}"] = value
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            value = integer(usage.get(key))
            if value is not None:
                self.details[key] = value
        if snapshot and (not self.observations or self.observations[-1] != snapshot):
            self.observations.append(snapshot)
            self.observations = self.observations[-16:]

    def apply(self, event):
        event.update({key: self.fields.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")})
        p, c, t = event["prompt_tokens"], event["completion_tokens"], event["total_tokens"]
        event["total_tokens_source"] = "reported" if t is not None else "unknown"
        if p is not None and c is not None:
            if t is None:
                event["total_tokens"] = p + c
                event["total_tokens_source"] = "derived"
            elif t != p + c:
                self.flags.add("total_mismatch")
            event["usage_status"] = "complete"
        else:
            event["usage_status"] = "partial" if self.fields else "unknown"
        if event["request_status"] != "completed" and self.fields:
            event["usage_status"] = "partial"
        event["usage_details"] = self.details
        event["usage_observations"] = self.observations
        event["flags"] = sorted(self.flags | set(event.get("flags", [])))
        event["finish_reason"] = self.finish_reason
        event["response_model"] = self.response_model
        event["upstream_request_id"] = event.get("upstream_request_id") or self.response_id


class SSEParser:
    """Incremental bounded parser. Forwarded bytes are never rewritten by this parser."""

    def __init__(self, usage: Usage, max_event=1024 * 1024):
        self.usage = usage
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.buffer = ""
        self.data = []
        self.event_size = 0
        self.max_event = max_event
        self.dropping = False

    def feed(self, chunk: bytes):
        self.buffer += self.decoder.decode(chunk)
        while True:
            indexes = [i for i in (self.buffer.find("\n"), self.buffer.find("\r")) if i >= 0]
            if not indexes:
                break
            end = min(indexes)
            if self.buffer[end] == "\r" and end == len(self.buffer) - 1:
                break
            step = 2 if self.buffer[end:end + 2] == "\r\n" else 1
            line, self.buffer = self.buffer[:end], self.buffer[end + step:]
            self.line(line)
        if len(self.buffer) > self.max_event:
            self.usage.flags.add("sse_event_too_large")
            self.buffer = ""
            self.data = []
            self.dropping = True

    def line(self, line):
        if line == "":
            if self.data and not self.dropping:
                value = "\n".join(self.data)
                if value == "[DONE]":
                    self.usage.done = True
                else:
                    try:
                        self.usage.observe(json.loads(value))
                    except (ValueError, TypeError):
                        self.usage.flags.add("invalid_sse_json")
            self.data, self.event_size, self.dropping = [], 0, False
        elif line.startswith("data:") and not self.dropping:
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            self.event_size += len(value)
            if self.event_size > self.max_event:
                self.usage.flags.add("sse_event_too_large")
                self.data = []
                self.dropping = True
            else:
                self.data.append(value)

    def finish(self):
        # An event without a blank-line terminator is incomplete, not final usage.
        if self.buffer or self.data:
            self.usage.flags.add("truncated_sse_event")


def metrics(event):
    event = dict(event)
    p, c, limit = (event.get(k) for k in ("prompt_tokens", "completion_tokens", "context_limit"))
    occupied = p + c if p is not None and c is not None else None
    valid_limit = limit and event.get("context_limit_status") not in {"conflict", "unknown"}
    event.update(
        occupied_tokens=occupied,
        remaining_before_response=max(0, limit - p) if valid_limit and p is not None else None,
        remaining_after_response=max(0, limit - occupied) if valid_limit and occupied is not None else None,
        utilization_percent=100 * occupied / limit if valid_limit and occupied is not None else None,
        overflow_tokens=max(0, occupied - limit) if valid_limit and occupied is not None else None,
    )
    return event


def new_event(settings, payload, headers, started_ns):
    get = lambda key: identifier(headers.get(key))
    session = get("x-token-counter-session-id")
    source = get("x-token-counter-client") or "opencode"
    if source != "opencode":
        raise ValueError("Эта сборка принимает только запросы OpenCode")
    kind = headers.get("x-token-counter-request-kind", "unknown")
    if kind == "chat":
        kind = "main"
    if kind not in {"main", "auxiliary", "compaction", "unknown"}:
        raise ValueError("Некорректный request kind")
    model = identifier(payload.get("model"))
    if not model:
        raise ValueError("Требуется model")
    return dict(
        event_uuid=str(uuid.uuid4()), created_at=utc_now(), created_epoch=started_ns / 1e9,
        started_at=utc_now(), started_ns=started_ns,
        client_source=source, client_instance_id=get("x-token-counter-instance-id") or settings.instance_id,
        conversation_id=session, session_id=session,
        chat_id=None, message_id=get("x-token-counter-message-id"),
        parent_session_id=get("x-token-counter-parent-session-id"), project_id=get("x-token-counter-project-id"),
        client_request_id=get("x-request-id"), upstream_request_id=None,
        request_kind=kind, agent_id=get("x-token-counter-agent"),
        integration_profile_id=settings.profile, provider_id=settings.provider,
        requested_model=model, response_model=None, deployment_id=None,
        endpoint="/v1/chat/completions", is_stream=bool(payload.get("stream", False)),
        message_count=len(payload.get("messages", [])),
        usage_source="litellm_response", usage_accuracy="gateway_reported",
        request_status="pending", flags=[], retry_count=None, fallback_count=None, cache_hit=None,
    )
