"""Request validation and the message mapping."""

import unittest

import context  # noqa: F401
from cli_bridge import mapping
from cli_bridge.errors import BridgeError


def req(**kw):
    body = {"model": "claude", "messages": [{"role": "user", "content": "Hi"}]}
    body.update(kw)
    return body


class MessageMapping(unittest.TestCase):
    def test_single_user_message_is_the_prompt_verbatim(self):
        r = mapping.parse_request(req(messages=[{"role": "user", "content": "  Hello\nworld  "}]))
        self.assertEqual(r.prompt, "  Hello\nworld  ")
        self.assertIsNone(r.system)
        self.assertEqual(r.turns, 1)

    def test_system_and_developer_messages_join_in_order(self):
        r = mapping.parse_request(req(messages=[
            {"role": "system", "content": "A"},
            {"role": "user", "content": "Hi"},
            {"role": "developer", "content": [{"type": "text", "text": "B"}]},
            {"role": "system", "content": "   "},
            {"role": "user", "content": "Again"},
        ]))
        self.assertEqual(r.system, "A\n\nB")

    def test_history_becomes_a_transcript(self):
        msgs = [
            {"role": "user", "content": "Hello", "name": "ann  lee"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "And now?"},
        ]
        r = mapping.parse_request(req(messages=msgs))
        b = mapping.boundary_for([mapping.Turn("user", "Hello")])
        self.assertIn(f'"--- <role> {b} ---"', r.prompt)
        self.assertIn(f"--- user (ann lee) {b} ---\nHello\n", r.prompt)
        self.assertIn(f"--- assistant {b} ---\nHi there\n", r.prompt)
        self.assertTrue(r.prompt.endswith(f"--- user {b} ---\nAnd now?\n"))
        self.assertEqual(r.turns, 3)

    def test_boundary_is_stable_across_turns_of_one_conversation(self):
        first = mapping.parse_request(req(messages=[
            {"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"}]))
        later = mapping.parse_request(req(messages=[
            {"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"}, {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3"}]))
        self.assertTrue(later.prompt.startswith(first.prompt.rstrip("\n")))

    def test_text_parts_join_with_newlines(self):
        r = mapping.parse_request(req(messages=[{"role": "user", "content": [
            {"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}]))
        self.assertEqual(r.prompt, "one\ntwo")

    def test_assistant_without_content_is_empty(self):
        r = mapping.parse_request(req(messages=[
            {"role": "user", "content": "a"}, {"role": "assistant", "content": None},
            {"role": "user", "content": "b"}]))
        self.assertEqual(r.turns, 3)


class Refusals(unittest.TestCase):
    def assertRefused(self, body, param=None, code="unsupported_parameter"):
        with self.assertRaises(BridgeError) as cm:
            mapping.parse_request(body)
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, code)
        if param:
            self.assertEqual(cm.exception.param, param)
        return cm.exception

    def test_features_the_clis_cannot_do(self):
        cases = {
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "functions": [{"name": "f"}],
            "tool_choice": "required",
            "function_call": {"name": "f"},
            "response_format": {"type": "json_schema", "json_schema": {}},
            "n": 2,
            "logprobs": True,
            "top_logprobs": 2,
            "audio": {"voice": "x"},
            "modalities": ["text", "audio"],
            "prediction": {"type": "content", "content": "x"},
            "web_search_options": {},
            "stop": ["\n"],
        }
        for param, value in cases.items():
            with self.subTest(param=param):
                self.assertRefused(req(**{param: value}), param)
        self.assertRefused(req(response_format={"type": "json_object"}), "response_format")

    def test_harmless_values_pass(self):
        r = mapping.parse_request(req(tools=[], n=1, response_format={"type": "text"}, logprobs=False,
                                      stop=[], tool_choice="none", modalities=["text"]))
        self.assertEqual(r.ignored, [])

    def test_non_text_content(self):
        for part in ({"type": "image_url", "image_url": {"url": "x"}}, {"type": "input_audio"},
                     {"type": "file", "file": {}}, {"type": "video"}):
            with self.subTest(part=part["type"]):
                self.assertRefused(req(messages=[{"role": "user", "content": [part]}]), "messages[0].content")

    def test_tool_messages_and_calls(self):
        self.assertRefused(req(messages=[{"role": "tool", "content": "x", "tool_call_id": "1"}]))
        self.assertRefused(req(messages=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "user", "content": "b"}]))

    def test_shape_errors(self):
        self.assertRefused(req(messages=[]), code="invalid_request")
        self.assertRefused(req(messages=[{"role": "system", "content": "only"}]), code="invalid_request")
        self.assertRefused(req(messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]),
                           code="invalid_request")
        self.assertRefused(req(messages=[{"role": "user", "content": "   "}]), code="invalid_request")
        self.assertRefused(req(messages=[{"role": "wizard", "content": "x"}]), code="invalid_request")
        self.assertRefused({"messages": [{"role": "user", "content": "x"}]}, "model", code="invalid_request")
        self.assertRefused(req(stream="yes"), "stream", code="invalid_request")
        self.assertRefused([1, 2], code="invalid_request")


class Parameters(unittest.TestCase):
    def test_sampling_settings_are_reported_as_ignored(self):
        r = mapping.parse_request(req(temperature=0.2, max_tokens=50, top_p=None, new_api_field=1, user="u"))
        self.assertEqual(r.ignored, ["max_tokens", "new_api_field", "temperature", "user"])
        self.assertTrue(set(r.ignored) - {"new_api_field"} <= mapping.IGNORED)

    def test_stream_options(self):
        self.assertTrue(mapping.parse_request(req(stream=True, stream_options={"include_usage": True})).include_usage)
        self.assertFalse(mapping.parse_request(req(stream=False, stream_options={"include_usage": True})).include_usage)
        self.assertFalse(mapping.parse_request(req(stream=True)).include_usage)

    def test_reasoning_effort_passes_through(self):
        self.assertEqual(mapping.parse_request(req(reasoning_effort="high")).reasoning_effort, "high")


if __name__ == "__main__":
    unittest.main()
