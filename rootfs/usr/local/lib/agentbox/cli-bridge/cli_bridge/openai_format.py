"""Response bodies and SSE framing in the OpenAI chat completions format."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

DONE = b"data: [DONE]\n\n"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    reported: bool = False  # False: the CLI reported nothing, all zero

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "prompt_tokens_details": {"cached_tokens": self.cached_tokens},
            "completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens},
        }


def new_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def now() -> int:
    return int(time.time())


def dumps(obj) -> str:
    # One line, always: json.dumps escapes newlines inside strings, which is
    # what keeps an SSE "data:" field from being split by the answer text.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def completion(cid: str, model: str, created: int, text: str, finish_reason: str, usage: Usage) -> dict:
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "system_fingerprint": None,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, "refusal": None},
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage.as_dict(),
    }


def chunk(cid: str, model: str, created: int, delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "system_fingerprint": None,
        "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
    }


def usage_chunk(cid: str, model: str, created: int, usage: Usage) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "system_fingerprint": None,
        "choices": [],
        "usage": usage.as_dict(),
    }


def sse(obj) -> bytes:
    """One server-sent event carrying a JSON object."""
    return b"data: " + dumps(obj).encode("utf-8") + b"\n\n"


def sse_comment(text: str = "keep-alive") -> bytes:
    return b": " + text.replace("\n", " ").encode("utf-8") + b"\n\n"


def http_chunk(data: bytes) -> bytes:
    """One HTTP/1.1 chunk (Transfer-Encoding: chunked)."""
    return b"%x\r\n%s\r\n" % (len(data), data)


LAST_HTTP_CHUNK = b"0\r\n\r\n"


def models_list(entries) -> dict:
    """entries: (id, owned_by) pairs."""
    return {
        "object": "list",
        "data": [{"id": mid, "object": "model", "created": 0, "owned_by": owner} for mid, owner in entries],
    }


def truncate_utf8(text: str, max_bytes: int) -> str:
    """The longest prefix of text whose UTF-8 encoding fits max_bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")
