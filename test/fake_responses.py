#!/usr/bin/env python3
"""A minimal stand-in for the OpenAI Responses API (streaming), so the real,
pinned `codex exec` can run in CI without a login. test/fixtures/
codex-config.toml points Codex at it. Every request body is appended to LOG
as one JSON line, for the smoke test to check what Codex sent.

  fake_responses.py PORT LOG
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "Hello from the fake Responses API."


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json(200, {"object": "list", "data": []})

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size)
        with open(sys.argv[2], "a", encoding="utf-8") as log:
            log.write(json.dumps({"path": self.path, "body": json.loads(raw or b"{}")}) + "\n")
        if not self.path.endswith("/responses"):
            self._json(404, {"error": {"message": "not here"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def event(kind, **data):
            data["type"] = kind
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        item = {"id": "msg_fake", "type": "message", "role": "assistant", "status": "in_progress", "content": []}
        event("response.created", response={"id": "resp_fake", "object": "response", "status": "in_progress", "output": []})
        event("response.output_item.added", output_index=0, item=item)
        for piece in ("Hello from ", "the fake ", "Responses API."):
            event("response.output_text.delta", item_id="msg_fake", output_index=0, content_index=0, delta=piece)
        done = dict(item, status="completed", content=[{"type": "output_text", "text": ANSWER, "annotations": []}])
        event("response.output_item.done", output_index=0, item=done)
        event("response.completed", response={
            "id": "resp_fake", "object": "response", "status": "completed", "output": [],
            "usage": {"input_tokens": 11, "input_tokens_details": {"cached_tokens": 3}, "output_tokens": 4,
                      "output_tokens_details": {"reasoning_tokens": 1}, "total_tokens": 15}})
        self.close_connection = True


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
