"""The HTTP service end to end in-process, with the stub CLIs from test/stubs."""

import dataclasses
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import context
from cli_bridge import adapters, config
from cli_bridge.server import BridgeServer

TOKEN = "s" * 48


class Service(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        env = {
            "HOME": cls.tmp,
            "AGENTBOX_RUNTIME": os.path.join(cls.tmp, "rt"),
            "PATH": context.STUBS + os.pathsep + os.environ.get("PATH", ""),
            "AGENTBOX_CLI_BRIDGE": "1",
            "AGENTBOX_CLI_BRIDGE_TOKEN": TOKEN,
            "AGENTBOX_CLI_BRIDGE_CLAUDE": "1",
            "AGENTBOX_CLI_BRIDGE_CODEX": "1",
            "AGENTBOX_CLI_BRIDGE_BIND": "127.0.0.1",
            "AGENTBOX_CLI_BRIDGE_TIMEOUT": "5",
            "AGENTBOX_CLI_BRIDGE_MAX_OUTPUT_BYTES": "2048",
        }
        # An ephemeral port instead of 3002.
        cfg = dataclasses.replace(config.load(env, adapter_names=adapters.names()), port=0)
        cls.registry = adapters.Registry(cfg, env)
        os.makedirs(os.path.join(cfg.runtime_dir, "calls"), mode=0o700)
        for a in cls.registry.enabled():
            a.prepare()
        cls.server = BridgeServer(cfg, cls.registry)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()
        cls.cfg = cfg

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, token=TOKEN, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data if raw else (json.loads(data) if data else None)

    def chat(self, content, model="claude/sonnet", **kw):
        return self.call("POST", "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": content}], **kw})

    def test_health_hides_details_without_token(self):
        resp, body = self.call("GET", "/health", token=None)
        self.assertEqual(resp.status, 200)
        self.assertEqual(set(body), {"status", "service", "version"})
        resp, body = self.call("GET", "/health")
        self.assertTrue(body["adapters"]["claude"]["logged_in"])
        self.assertIn("codex/gpt-stub-1", body["adapters"]["codex"]["models"])

    def test_token(self):
        for token in (None, "wrong", TOKEN[:-1]):
            resp, body = self.call("GET", "/v1/models", token=token)
            self.assertEqual((resp.status, body["error"]["code"]), (401, "invalid_api_key"))
        resp, _ = self.call("POST", "/v1/chat/completions",
                            {"model": "claude", "messages": [{"role": "user", "content": "hi"}]}, token="x")
        self.assertEqual(resp.status, 401)

    def test_models(self):
        _, body = self.call("GET", "/v1/models")
        ids = [m["id"] for m in body["data"]]
        self.assertEqual(ids, ["claude", "claude/sonnet", "claude/opus", "claude/haiku", "codex", "codex/gpt-stub-1"])

    def test_completion(self):
        for model in ("claude/sonnet", "codex"):
            resp, body = self.chat("hello", model=model, temperature=0.1)
            with self.subTest(model=model):
                self.assertEqual(resp.status, 200)
                self.assertEqual(body["model"], model)
                self.assertEqual(body["choices"][0]["message"]["content"], "Hello from the stub.")
                self.assertEqual(body["choices"][0]["finish_reason"], "stop")
                self.assertGreater(body["usage"]["prompt_tokens"], 0)
                self.assertEqual(resp.getheader("x-cli-bridge-ignored"), "temperature")
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

    def test_streaming(self):
        for model in ("claude", "codex/gpt-stub-1"):
            resp, raw = self.call("POST", "/v1/chat/completions", {
                "model": model, "stream": True, "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "hello"}]}, raw=True)
            with self.subTest(model=model):
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.getheader("Content-Type").startswith("text/event-stream"))
                events = [e for e in raw.decode().split("\n\n") if e]
                self.assertEqual(events[-1], "data: [DONE]")
                chunks = [json.loads(e[6:]) for e in events[:-1]]
                self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
                text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
                self.assertEqual(text, "Hello from the stub.")
                self.assertEqual(chunks[-2]["choices"][0]["finish_reason"], "stop")
                self.assertEqual(chunks[-1]["choices"], [])
                self.assertIn("usage", chunks[-1])

    def test_output_cap_gives_length(self):
        resp, body = self.chat("STUB:big")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["choices"][0]["finish_reason"], "length")
        self.assertEqual(len(body["choices"][0]["message"]["content"].encode()), 2048)

    def test_errors(self):
        cases = [("STUB:limit", "claude", 429), ("STUB:limit", "codex", 429), ("STUB:tool", "claude", 502),
                 ("STUB:tool", "codex", 502), ("STUB:crash", "claude", 502)]
        for content, model, status in cases:
            resp, body = self.chat(content, model=model)
            with self.subTest(content=content, model=model):
                self.assertEqual(resp.status, status, body)
        resp, _ = self.chat("STUB:limit")
        self.assertIsNotNone(resp.getheader("Retry-After"))

    def test_timeout(self):
        t0 = time.monotonic()
        resp, body = self.chat("STUB:hang", model="codex")
        self.assertEqual((resp.status, body["error"]["code"]), (504, "cli_timeout"))
        self.assertLess(time.monotonic() - t0, 12)

    def test_validation(self):
        resp, body = self.chat("hi", tools=[{"type": "function", "function": {"name": "x"}}])
        self.assertEqual((resp.status, body["error"]["param"]), (400, "tools"))
        resp, body = self.chat("hi", model="gpt-4o")
        self.assertEqual(resp.status, 404)
        resp, body = self.call("GET", "/v1/chat/completions")
        self.assertEqual(resp.status, 405)

    def test_scratch_and_work_dirs_are_left_empty(self):
        self.chat("STUB:write")
        self.assertEqual(os.listdir(os.path.join(self.cfg.runtime_dir, "work")), [])
        self.assertEqual(os.listdir(os.path.join(self.cfg.runtime_dir, "calls")), [])


if __name__ == "__main__":
    unittest.main()
