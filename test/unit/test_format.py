"""OpenAI response bodies and the SSE / chunked framing."""

import json
import unittest

import context  # noqa: F401
from cli_bridge import openai_format as fmt


class Framing(unittest.TestCase):
    def test_sse_event_is_one_data_line(self):
        text = 'line one\n\ndata: [DONE]\r\n"quoted"   ü'
        event = fmt.sse(fmt.chunk("id", "m", 1, {"content": text}))
        self.assertTrue(event.startswith(b"data: "))
        self.assertTrue(event.endswith(b"\n\n"))
        body = event[len(b"data: "):-2]
        self.assertNotIn(b"\n", body)
        self.assertNotIn(b"\r", body)
        self.assertEqual(json.loads(body)["choices"][0]["delta"]["content"], text)

    def test_done_and_comment(self):
        self.assertEqual(fmt.DONE, b"data: [DONE]\n\n")
        self.assertEqual(fmt.sse_comment("a\nb"), b": a b\n\n")

    def test_http_chunk(self):
        self.assertEqual(fmt.http_chunk(b"hello"), b"5\r\nhello\r\n")
        self.assertEqual(fmt.http_chunk(b"x" * 26)[:4], b"1a\r\n")
        self.assertEqual(fmt.LAST_HTTP_CHUNK, b"0\r\n\r\n")

    def test_truncate_utf8_keeps_characters_whole(self):
        self.assertEqual(fmt.truncate_utf8("aüb", 2), "a")
        self.assertEqual(fmt.truncate_utf8("aüb", 3), "aü")
        self.assertEqual(fmt.truncate_utf8("abc", 10), "abc")


class Bodies(unittest.TestCase):
    def test_completion(self):
        u = fmt.Usage(prompt_tokens=10, completion_tokens=3, cached_tokens=4, reasoning_tokens=1, reported=True)
        body = fmt.completion("chatcmpl-1", "claude/sonnet", 5, "hi", "stop", u)
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"], {"role": "assistant", "content": "hi", "refusal": None})
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"]["total_tokens"], 13)
        self.assertEqual(body["usage"]["prompt_tokens_details"], {"cached_tokens": 4})
        self.assertEqual(body["usage"]["completion_tokens_details"], {"reasoning_tokens": 1})

    def test_chunks(self):
        c = fmt.chunk("id", "m", 1, {}, "length")
        self.assertEqual(c["object"], "chat.completion.chunk")
        self.assertEqual(c["choices"][0]["finish_reason"], "length")
        uc = fmt.usage_chunk("id", "m", 1, fmt.Usage())
        self.assertEqual(uc["choices"], [])
        self.assertEqual(uc["usage"]["total_tokens"], 0)

    def test_models_list(self):
        body = fmt.models_list([("claude/sonnet", "anthropic")])
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["data"][0]["id"], "claude/sonnet")
        self.assertEqual(body["data"][0]["owned_by"], "anthropic")


if __name__ == "__main__":
    unittest.main()
