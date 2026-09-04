from __future__ import annotations

import copy
import gzip
from pathlib import Path
import sys
import unittest
import zlib


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import compaction  # noqa: E402


class CompactionHelpersTests(unittest.TestCase):
    def test_ocx1_round_trip_and_invalid_values(self) -> None:
        encoded = compaction.encode_compaction_summary("关键 marker-123")
        self.assertTrue(encoded.startswith("ocx1:"))
        self.assertEqual(compaction.decode_compaction_summary(encoded), "关键 marker-123")
        self.assertIsNone(compaction.decode_compaction_summary("opaque-openai-blob"))
        self.assertIsNone(compaction.decode_compaction_summary("ocx1:not-base64"))
        with self.assertRaises(compaction.CompactionError):
            compaction.encode_compaction_summary("   ")

    def test_v2_detection_and_native_model_routing(self) -> None:
        payload = {"input": [{"type": "message"}, {"type": "compaction_trigger"}]}
        self.assertTrue(compaction.is_v2_compaction_request(payload))
        self.assertFalse(compaction.is_v2_compaction_request({"input": []}))
        self.assertTrue(compaction.is_native_compaction_model("gpt-5.6-sol"))
        self.assertTrue(compaction.is_native_compaction_model("codex-auto-review"))
        self.assertFalse(compaction.is_native_compaction_model("gemini-3.8-flash"))

    def test_summary_payload_removes_tools_images_and_private_shapes(self) -> None:
        prior = compaction.encode_compaction_summary("prior-marker")
        payload = {
            "model": "gemini-test",
            "tools": [{"type": "function", "name": "danger"}],
            "additional_tools": [{"name": "extra"}],
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "keep-me"},
                        {"type": "input_image", "image_url": "data:image/png;base64,secret"},
                    ],
                },
                {"type": "compaction", "encrypted_content": prior},
                {"type": "function_call", "name": "pwd", "arguments": "{}"},
                {"type": "compaction_trigger"},
            ],
        }
        rendered = compaction.build_summary_payload(payload)
        self.assertEqual(set(rendered), {"model", "input", "max_output_tokens", "store", "stream"})
        text = rendered["input"][0]["content"][0]["text"]
        self.assertIn("keep-me", text)
        self.assertIn("prior-marker", text)
        self.assertIn("tool call: pwd", text)
        self.assertNotIn("base64,secret", text)
        self.assertNotIn("additional_tools", text)

    def test_prepare_forward_decodes_ocx1_and_cleans_native_reasoning(self) -> None:
        payload = {
            "model": "gpt-5.6-sol",
            "input": [
                {
                    "type": "compaction",
                    "encrypted_content": compaction.encode_compaction_summary("resume-marker"),
                },
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": "invalid"}],
                    "summary": [{"type": "summary_text", "text": "valid"}],
                    "encrypted_content": "opaque",
                },
            ],
        }
        updated, counts = compaction.prepare_forward_payload(payload)
        self.assertEqual(counts, {"decoded_compactions": 1, "reasoning_items_cleaned": 1})
        self.assertEqual(updated["input"][0]["type"], "message")
        self.assertIn("resume-marker", updated["input"][0]["content"][0]["text"])
        self.assertNotIn("content", updated["input"][1])
        self.assertEqual(updated["input"][1]["summary"][0]["text"], "valid")
        self.assertIn("content", payload["input"][1])

    def test_prepare_forward_preserves_real_blob_and_external_reasoning(self) -> None:
        payload = {
            "model": "glm-5.3",
            "input": [
                {"type": "compaction", "encrypted_content": "real-openai-blob"},
                {"type": "reasoning", "content": [1, 2, 3]},
            ],
        }
        updated, counts = compaction.prepare_forward_payload(payload)
        self.assertEqual(updated, payload)
        self.assertEqual(counts, {"decoded_compactions": 0, "reasoning_items_cleaned": 0})

    def test_prepare_forward_keeps_ordinary_request_identical(self) -> None:
        payload = {
            "model": "gemini-3.8-flash",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        }
        original = copy.deepcopy(payload)
        updated, counts = compaction.prepare_forward_payload(payload)
        self.assertEqual(updated, original)
        self.assertEqual(counts, {"decoded_compactions": 0, "reasoning_items_cleaned": 0})

    def test_v1_output_retains_recent_user_messages_and_summary(self) -> None:
        output = compaction.build_v1_output(
            [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "older"}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "skip"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "newer"}]},
            ],
            "checkpoint",
        )
        texts = [item["content"][0]["text"] for item in output]
        self.assertEqual(texts[:2], ["older", "newer"])
        self.assertIn("checkpoint", texts[-1])
        self.assertTrue(texts[-1].startswith(compaction.SUMMARY_PREFIX))

    def test_request_body_encodings_round_trip(self) -> None:
        source = b'{"marker":"hello"}'
        encodings = ["identity", "gzip", "x-gzip", "deflate"]
        if compaction._zstd is not None:
            encodings.extend(["zstd", "zstandard"])
        for encoding in encodings:
            encoded = compaction.encode_request_body(source, encoding)
            self.assertEqual(compaction.decode_request_body(encoded, encoding), source)
        raw_deflate = zlib.compress(source)[2:-4]
        self.assertEqual(compaction.decode_request_body(raw_deflate, "deflate"), source)
        self.assertEqual(gzip.decompress(compaction.encode_request_body(source, "gzip")), source)
        self.assertTrue(compaction.encoding_supported("gzip"))
        self.assertEqual(compaction.encoding_supported("zstd"), compaction._zstd is not None)

    def test_request_body_rejects_unknown_encoding_and_size(self) -> None:
        with self.assertRaises(compaction.CompactionError):
            compaction.decode_request_body(b"x", "br")
        self.assertFalse(compaction.encoding_supported("br"))
        with self.assertRaises(compaction.CompactionError):
            compaction.decode_request_body(b"12345", "identity", max_bytes=4)

    def test_synthetic_v2_has_one_compaction_and_ordered_events(self) -> None:
        response, events = compaction.build_v2_response("glm-5.3", "marker")
        self.assertEqual(
            [event["type"] for event in events],
            [
                "response.created",
                "response.output_item.added",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual([item["type"] for item in response["output"]], ["compaction"])
        self.assertEqual(
            compaction.decode_compaction_summary(response["output"][0]["encrypted_content"]),
            "marker",
        )
        rendered = compaction.render_sse(events)
        self.assertIn(b"response.output_item.done", rendered)
        self.assertTrue(rendered.endswith(b"data: [DONE]\n\n"))

    def test_failure_events_do_not_contain_summary_or_credentials(self) -> None:
        events = compaction.build_v2_failure_events("deepseek-v4-pro")
        self.assertEqual([event["type"] for event in events], ["response.created", "response.failed"])
        self.assertEqual(events[-1]["response"]["status"], "failed")
        self.assertEqual(events[-1]["response"]["output"], [])

    def test_extract_output_text(self) -> None:
        payload = {
            "output": [
                {"type": "reasoning", "content": []},
                {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
            ]
        }
        self.assertEqual(compaction.extract_output_text(payload), "done")


if __name__ == "__main__":
    unittest.main()
