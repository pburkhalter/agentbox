"""The HTTP side: routes, bearer token, request limits, streaming, logging."""

from __future__ import annotations

import hmac
import json
import select
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import __version__, errors, mapping, openai_format as fmt, process
from .call import ClientGone, Sink, execute
from .limiter import Limiter

KEEPALIVE_SECONDS = 15
DEBUG_LIMIT = 4000


def log(message: str) -> None:
    print(f"[agentbox/cli-bridge] {message}", file=sys.stderr, flush=True)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, cfg, registry):
        self.cfg = cfg
        self.registry = registry
        # One queue per adapter: a busy Codex does not hold up Claude.
        self.limiters = {name: Limiter(a.concurrency, cfg.queue, cfg.queue_timeout)
                         for name, a in registry.adapters.items()}
        self.procs = process.Registry()
        self._connections = threading.BoundedSemaphore(cfg.max_connections)
        super().__init__((cfg.bind, cfg.port), Handler)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


class _CollectSink(Sink):
    def __init__(self, handler):
        self.handler = handler

    def tick(self):
        if self.handler.client_gone():
            raise ClientGone()


class _StreamSink(Sink):
    """Sends SSE chunks. The response headers go out with the first piece of
    the answer, so an error before it still gets its proper status code."""

    def __init__(self, handler, cid, model_id, created, extra_headers):
        self.h = handler
        self.cid = cid
        self.model_id = model_id
        self.created = created
        self.extra_headers = extra_headers
        self.started = False
        self.last_write = time.monotonic()

    def _write(self, data: bytes) -> None:
        try:
            self.h.wfile.write(fmt.http_chunk(data))
            self.h.wfile.flush()
        except OSError:
            raise ClientGone() from None
        self.last_write = time.monotonic()

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        h = self.h
        try:
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream; charset=utf-8")
            h.send_header("Cache-Control", "no-cache")
            h.send_header("Transfer-Encoding", "chunked")
            h.send_header("X-Accel-Buffering", "no")
            h.send_header("Connection", "close")
            for k, v in self.extra_headers.items():
                h.send_header(k, v)
            h.end_headers()
        except OSError:
            raise ClientGone() from None
        h.close_connection = True
        h.status = 200
        h.streaming = True
        self._write(fmt.sse(fmt.chunk(self.cid, self.model_id, self.created, {"role": "assistant", "content": ""})))

    def text(self, delta: str) -> None:
        self.start()
        self._write(fmt.sse(fmt.chunk(self.cid, self.model_id, self.created, {"content": delta})))

    def tick(self) -> None:
        if not self.started:
            if self.h.client_gone():
                raise ClientGone()
        elif time.monotonic() - self.last_write > KEEPALIVE_SECONDS:
            self._write(fmt.sse_comment())

    def finish(self, finish_reason: str, usage, include_usage: bool) -> None:
        self.start()
        self._write(fmt.sse(fmt.chunk(self.cid, self.model_id, self.created, {}, finish_reason)))
        if include_usage:
            self._write(fmt.sse(fmt.usage_chunk(self.cid, self.model_id, self.created, usage)))
        self._write(fmt.DONE)
        self._end()

    def fail(self, err: errors.BridgeError) -> None:
        # OpenAI-style error event; no [DONE], so a client sees the stream broke.
        self._write(fmt.sse(err.body()))
        self._end()

    def _end(self) -> None:
        try:
            self.h.wfile.write(fmt.LAST_HTTP_CHUNK)
            self.h.wfile.flush()
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cli-bridge/" + __version__
    sys_version = ""
    timeout = 120  # per socket read/write

    # --- plumbing -------------------------------------------------------------

    def log_message(self, format, *args):  # noqa: A002 - replaced by one line per request
        pass

    def version_string(self):
        return self.server_version

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_PATCH(self):
        self._route("PATCH")

    def do_OPTIONS(self):  # no CORS: a browser preflight just fails
        self._route("OPTIONS")

    def _route(self, method: str) -> None:
        self.status = None
        self.streaming = False
        self.fields = {}
        self.t0 = time.monotonic()
        path = urlsplit(self.path).path.rstrip("/") or "/"
        self.cid = fmt.new_id() if path.endswith("/chat/completions") else "req-" + fmt.new_id()[9:]
        # An error answered before the body was read must close the
        # connection, or the body would be parsed as the next request.
        self.body_pending = self.headers.get("Content-Length", "0") not in ("", "0") or bool(
            self.headers.get("Transfer-Encoding")
        )
        try:
            if path == "/health":
                self._only(method, "GET")
                self._health()
            elif path in ("/v1/models", "/models"):
                self._only(method, "GET")
                self._require_token()
                self._models()
            elif path in ("/v1/chat/completions", "/chat/completions"):
                self._only(method, "POST")
                self._require_token()
                self._chat()
            else:
                raise errors.BridgeError(404, "invalid_request_error", "not_found", f"no route for {method} {path}")
        except errors.BridgeError as err:
            self._send_error(err)
        except Exception:  # noqa: BLE001 - one bad request must not kill the server
            log("internal error:\n" + traceback.format_exc().rstrip())
            self._send_error(errors.BridgeError(500, "api_error", "internal_error", "internal error in cli-bridge"))
        finally:
            self._log_line(method, "".join(c if c.isprintable() else "?" for c in path)[:200])

    def _only(self, method: str, allowed: str) -> None:
        if method != allowed:
            raise errors.BridgeError(405, "invalid_request_error", "method_not_allowed", f"use {allowed}")

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return False
        return hmac.compare_digest(value.strip().encode("utf-8"), self.server.cfg.token.encode("utf-8"))

    def _require_token(self) -> None:
        if not self._authorized():
            raise errors.unauthorized()

    def client_gone(self) -> bool:
        """True when the client closed its end (checked without reading data)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _send_json(self, status: int, obj, headers=None) -> None:
        body = (fmt.dumps(obj) + "\n").encode("utf-8")
        self.status = status
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("x-request-id", self.cid)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def _send_error(self, err: errors.BridgeError) -> None:
        self.fields["error"] = err.code
        if self.streaming:  # a stream is under way; it cannot become an error response
            self.close_connection = True
            return
        headers = {}
        if err.retry_after:
            headers["Retry-After"] = str(err.retry_after)
        if err.status == 401 and err.code == "invalid_api_key":
            headers["WWW-Authenticate"] = 'Bearer realm="cli-bridge"'
        if self.body_pending:
            self.close_connection = True
        self._send_json(err.status, err.body(), headers)

    def _log_line(self, method: str, path: str) -> None:
        f = self.fields
        parts = [str(self.status or "-"), method, path, f"id={self.cid}", f"client={self.client_address[0]}"]
        for key in ("adapter", "model", "stream", "turns", "error", "finish"):
            if key in f:
                parts.append(f"{key}={f[key]}")
        for key in ("queue", "ttfb"):
            if key in f:
                parts.append(f"{key}={f[key]:.2f}s")
        parts.append(f"duration={time.monotonic() - self.t0:.2f}s")
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "ignored"):
            if key in f:
                parts.append(f"{key}={f[key]}")
        log(" ".join(parts))

    # --- routes ---------------------------------------------------------------

    def _health(self) -> None:
        body = {"status": "ok", "service": "cli-bridge", "version": __version__}
        if self._authorized():
            body["adapters"] = {}
            for name, adapter in self.server.registry.adapters.items():
                entry = {"enabled": adapter.enabled}
                if adapter.enabled:
                    entry["logged_in"] = adapter.login_state()
                    entry["cli"] = adapter.version()
                    entry["models"] = adapter.model_ids()
                    entry["calls"] = self.server.limiters[name].snapshot()
                body["adapters"][name] = entry
            body["limits"] = dict(
                timeout_seconds=self.server.cfg.timeout,
                max_request_bytes=self.server.cfg.max_request_bytes,
                max_output_bytes=self.server.cfg.max_output_bytes,
            )
        self._send_json(200, body)

    def _models(self) -> None:
        entries = []
        for adapter in self.server.registry.enabled():
            if adapter.login_state():
                entries += [(mid, adapter.owned_by) for mid in adapter.model_ids()]
        self._send_json(200, fmt.models_list(entries))

    def _read_json(self):
        cfg = self.server.cfg
        if self.headers.get("Transfer-Encoding"):
            raise errors.BridgeError(411, "invalid_request_error", "length_required",
                                     "chunked request bodies are not supported; send Content-Length")
        length = self.headers.get("Content-Length")
        if length is None:
            raise errors.BridgeError(411, "invalid_request_error", "length_required", "Content-Length is required")
        try:
            size = int(length)
        except ValueError:
            raise errors.invalid_request("bad Content-Length") from None
        if size < 0:
            raise errors.invalid_request("bad Content-Length")
        if size > cfg.max_request_bytes:
            raise errors.too_large(f"request body of {size} bytes exceeds the limit of {cfg.max_request_bytes}")
        data = self.rfile.read(size)
        self.body_pending = False
        if len(data) != size:
            self.close_connection = True
            raise errors.invalid_request("request body ended early")
        try:
            return json.loads(data)
        except ValueError:
            raise errors.invalid_request("the request body is not valid JSON", code="invalid_json") from None

    def _chat(self) -> None:
        cfg = self.server.cfg
        req = mapping.parse_request(self._read_json())
        adapter, model, model_id = self.server.registry.resolve(req.model)
        effort = adapter.check_effort(req.reasoning_effort)
        self.fields.update(adapter=adapter.name, model=model_id, stream=int(req.stream), turns=req.turns)
        if req.ignored:
            self.fields["ignored"] = ",".join(req.ignored)
        if adapter.login_state() is False:
            raise errors.not_logged_in(
                f"{adapter.title} is not logged in for the bridge; run `agentbox cli-bridge login {adapter.name}` in the container"
            )
        extra = {"x-request-id": self.cid}
        if req.ignored:
            extra["x-cli-bridge-ignored"] = ",".join(req.ignored)
        created = fmt.now()
        with self.server.limiters[adapter.name].slot() as waited:
            self.fields["queue"] = waited
            if cfg.debug:
                log(f"debug id={self.cid} system={_clip(req.system)} prompt={_clip(req.prompt)}")
            if req.stream:
                sink = _StreamSink(self, self.cid, model_id, created, extra)
            else:
                sink = _CollectSink(self)
            out = execute(adapter, model, effort, req, cfg, self.server.procs, sink, self.cid, log)
        if out.first_text_at is not None:
            self.fields["ttfb"] = out.first_text_at
        if out.usage.reported:
            self.fields.update(prompt_tokens=out.usage.prompt_tokens, completion_tokens=out.usage.completion_tokens,
                               cached_tokens=out.usage.cached_tokens)
        if cfg.debug:
            log(f"debug id={self.cid} answer={_clip(out.text)}")
        if out.client_gone:
            self.status = 499
            self.fields["error"] = "client_closed"
            self.close_connection = True
            return
        if req.stream:
            if out.error is not None:
                if sink.started:
                    self.fields["error"] = out.error.code
                    try:
                        sink.fail(out.error)
                    except ClientGone:
                        pass
                    return
                raise out.error
            self.fields["finish"] = out.finish
            try:
                sink.finish(out.finish, out.usage, req.include_usage)
            except ClientGone:
                self.status = 499
            return
        if out.error is not None:
            raise out.error
        self.fields["finish"] = out.finish
        body = fmt.completion(self.cid, model_id, created, out.text, out.finish, out.usage)
        self._send_json(200, body, {k: v for k, v in extra.items() if k != "x-request-id"})


def _clip(text) -> str:
    if text is None:
        return "null"
    s = json.dumps(text, ensure_ascii=False)
    return s if len(s) <= DEBUG_LIMIT else s[:DEBUG_LIMIT] + "...(truncated)"
