"""Validates a chat completion request and turns its messages into the two
texts a CLI takes: a system prompt and one prompt.

Mapping (the caller sends the whole history every time):

- ``system`` and ``developer`` messages, wherever they appear, are joined in
  order (blank line between them) into the system prompt. Without any, the
  configured default system prompt is used, never the CLI's own.
- A single ``user`` message is the prompt, verbatim.
- A longer history is written out as a transcript: an instruction line, then
  each ``user``/``assistant`` message under a header line
  ``--- <role> <boundary> ---``. The boundary is derived from the first
  message, so it is stable across the turns of one conversation (prompt
  caching) and practically never collides with message text.
- The last message must come from the user.

Everything the CLIs cannot do is refused with 400 instead of being dropped:
tools and tool results, images, audio and files, structured output, n > 1,
log probabilities, stop sequences, predicted output, web search options.
Sampling settings (temperature, max_tokens, ...) are accepted but have no
effect; they are reported back in the ``x-cli-bridge-ignored`` header.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import errors

KNOWN = {"model", "messages", "stream", "stream_options", "reasoning_effort"}

# Refused with 400 when used (see _active_unsupported); harmless values such
# as "tools": [] or "n": 1 pass.
UNSUPPORTED = {
    "tools", "functions", "tool_choice", "function_call", "response_format", "n", "logprobs",
    "top_logprobs", "audio", "modalities", "prediction", "web_search_options", "stop",
}

# Accepted, not applied. The CLIs have no switch for them.
IGNORED = {
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "logit_bias",
    "user",
    "store",
    "metadata",
    "service_tier",
    "parallel_tool_calls",
    "prompt_cache_key",
    "safety_identifier",
    "verbosity",
}


def _active_unsupported(body: dict) -> list[tuple[str, str]]:
    """(parameter, reason) for every requested feature the bridge cannot honour."""
    found = []

    def has(name):
        return name in body and body[name] is not None

    if has("tools") and body["tools"] != []:
        found.append(("tools", "tool calls are not supported; the CLIs run without tools"))
    if has("functions") and body["functions"] != []:
        found.append(("functions", "function calls are not supported"))
    if has("tool_choice") and body["tool_choice"] not in ("none", "auto"):
        found.append(("tool_choice", "tool calls are not supported"))
    if has("function_call") and body["function_call"] not in ("none", "auto"):
        found.append(("function_call", "function calls are not supported"))
    if has("response_format"):
        fmt = body["response_format"]
        kind = fmt.get("type") if isinstance(fmt, dict) else fmt
        if kind != "text":
            found.append(("response_format", f"response_format {kind!r} is not supported; only plain text"))
    if has("n") and body["n"] != 1:
        found.append(("n", "only n=1 is supported"))
    if has("logprobs") and body["logprobs"] is not False:
        found.append(("logprobs", "log probabilities are not available from the CLIs"))
    if has("top_logprobs"):
        found.append(("top_logprobs", "log probabilities are not available from the CLIs"))
    if has("audio"):
        found.append(("audio", "audio output is not supported"))
    if has("modalities") and body["modalities"] != ["text"]:
        found.append(("modalities", "only the text modality is supported"))
    if has("prediction"):
        found.append(("prediction", "predicted outputs are not supported"))
    if has("web_search_options"):
        found.append(("web_search_options", "web search is not supported; the CLIs run without tools"))
    if has("stop") and body["stop"] not in ("", []):
        found.append(("stop", "stop sequences are not supported"))
    return found


@dataclass
class Turn:
    role: str  # "user" or "assistant"
    text: str
    name: str | None = None


@dataclass
class ChatRequest:
    model: str
    system: str | None
    prompt: str
    stream: bool = False
    include_usage: bool = False
    reasoning_effort: str | None = None
    ignored: list = field(default_factory=list)
    turns: int = 0


def _text_of(content, index: int, role: str) -> str:
    """Plain text of a message's content (string or list of parts)."""
    param = f"messages[{index}].content"
    if content is None:
        if role == "assistant":
            return ""
        raise errors.invalid_request(f"{param} is missing", param)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise errors.invalid_request(f"{param} must be a string or a list of parts", param)
    texts = []
    for j, part in enumerate(content):
        if not isinstance(part, dict) or not isinstance(part.get("type"), str):
            raise errors.invalid_request(f"{param}[{j}] must be an object with a type", param)
        kind = part["type"]
        if kind == "text":
            if not isinstance(part.get("text"), str):
                raise errors.invalid_request(f"{param}[{j}].text must be a string", param)
            texts.append(part["text"])
        elif kind == "refusal" and role == "assistant" and isinstance(part.get("refusal"), str):
            texts.append(part["refusal"])
        elif kind in ("image_url", "input_image", "image"):
            raise errors.unsupported(f"{param}[{j}]: images are not supported", param)
        elif kind in ("input_audio", "audio"):
            raise errors.unsupported(f"{param}[{j}]: audio input is not supported", param)
        elif kind in ("file", "input_file"):
            raise errors.unsupported(f"{param}[{j}]: file inputs are not supported", param)
        else:
            raise errors.unsupported(f"{param}[{j}]: content part type {kind!r} is not supported", param)
    return "\n".join(texts)


def split_messages(messages) -> tuple[str | None, list[Turn]]:
    """System text and the user/assistant turns, validated."""
    if not isinstance(messages, list) or not messages:
        raise errors.invalid_request("messages must be a non-empty list", "messages")
    system_parts: list[str] = []
    turns: list[Turn] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise errors.invalid_request(f"messages[{i}] must be an object", "messages")
        role = msg.get("role")
        if role in ("system", "developer"):
            text = _text_of(msg.get("content"), i, role)
            if text.strip():
                system_parts.append(text)
        elif role == "user":
            name = msg.get("name")
            turns.append(Turn("user", _text_of(msg.get("content"), i, role), _clean_name(name)))
        elif role == "assistant":
            if msg.get("tool_calls") or msg.get("function_call"):
                raise errors.unsupported(
                    f"messages[{i}] carries tool calls; tool calls are not supported", "messages"
                )
            if msg.get("audio"):
                raise errors.unsupported(f"messages[{i}]: audio is not supported", "messages")
            turns.append(Turn("assistant", _text_of(msg.get("content"), i, role), _clean_name(msg.get("name"))))
        elif role in ("tool", "function"):
            raise errors.unsupported(
                f"messages[{i}] has role {role!r}; tool results are not supported", "messages"
            )
        else:
            raise errors.invalid_request(f"messages[{i}].role {role!r} is not a known role", "messages")
    if not turns:
        raise errors.invalid_request("at least one user message is required", "messages")
    if turns[-1].role != "user":
        raise errors.invalid_request("the last message must come from the user", "messages")
    if not turns[-1].text.strip():
        raise errors.invalid_request("the last user message is empty", "messages")
    system = "\n\n".join(system_parts) if system_parts else None
    return system, turns


def _clean_name(name) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return " ".join(name.split())[:64]


TRANSCRIPT_INTRO = (
    "Below is a conversation between a user and an assistant, oldest message first. "
    "Each message starts with a header line of the form \"--- <role> {b} ---\". "
    "Write the assistant's next reply to the last user message. "
    "Answer with the text of that reply only, without a header line or role label."
)


def boundary_for(turns: list[Turn]) -> str:
    return hashlib.sha256(turns[0].text.encode("utf-8")).hexdigest()[:10]


def render_prompt(turns: list[Turn]) -> str:
    """The prompt text a CLI receives on stdin."""
    if len(turns) == 1:
        return turns[0].text
    b = boundary_for(turns)
    out = [TRANSCRIPT_INTRO.format(b=b), ""]
    for turn in turns:
        label = turn.role if not turn.name else f"{turn.role} ({turn.name})"
        out.append(f"--- {label} {b} ---")
        out.append(turn.text)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def parse_request(body) -> ChatRequest:
    """A validated ChatRequest, or a BridgeError (400)."""
    if not isinstance(body, dict):
        raise errors.invalid_request("the request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise errors.invalid_request("model is required", "model")
    problems = _active_unsupported(body)
    if problems:
        param, reason = problems[0]
        raise errors.unsupported(reason, param)
    stream = body.get("stream", False)
    if stream is None:
        stream = False
    if not isinstance(stream, bool):
        raise errors.invalid_request("stream must be a boolean", "stream")
    options = body.get("stream_options") or {}
    if not isinstance(options, dict):
        raise errors.invalid_request("stream_options must be an object", "stream_options")
    effort = body.get("reasoning_effort")
    if effort is not None and not isinstance(effort, str):
        raise errors.invalid_request("reasoning_effort must be a string", "reasoning_effort")
    system, turns = split_messages(body.get("messages"))
    # Everything else that was sent with a value: the IGNORED names, and
    # parameters this version does not know (newer API fields).
    ignored = sorted(
        name for name, value in body.items()
        if name not in KNOWN and name not in UNSUPPORTED and value is not None
    )
    return ChatRequest(
        model=model,
        system=system,
        prompt=render_prompt(turns),
        stream=stream,
        include_usage=bool(stream and options.get("include_usage")),
        reasoning_effort=effort,
        ignored=ignored,
        turns=len(turns),
    )
