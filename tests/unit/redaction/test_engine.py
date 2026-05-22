"""RedactionEngine pattern + key behaviour."""
from __future__ import annotations

import re

import pytest

from gg_relay.redaction import REDACTED, RedactionEngine


@pytest.fixture
def engine() -> RedactionEngine:
    return RedactionEngine()


class TestPatternBasedMasking:
    def test_anthropic_api_key_in_freeform_text(self, engine: RedactionEngine):
        s = engine.redact_string("set sk-ant-12345-AbCdEfGhIjK in env")
        assert "sk-ant-" not in s
        assert REDACTED in s

    def test_bearer_token_header(self, engine: RedactionEngine):
        s = engine.redact_string("Authorization: Bearer eyJabc.def.ghi")
        assert "Bearer eyJabc" not in s
        assert REDACTED in s

    def test_aws_access_key_id(self, engine: RedactionEngine):
        s = engine.redact_string("aws id AKIAIOSFODNN7EXAMPLE more text")
        assert "AKIAIOSFODNN7EXAMPLE" not in s
        assert REDACTED in s

    def test_api_key_assignment(self, engine: RedactionEngine):
        s = engine.redact_string('api_key="abc-123" extra')
        assert "abc-123" not in s
        assert REDACTED in s

    def test_token_colon_separator(self, engine: RedactionEngine):
        s = engine.redact_string("token: abcdef123 trailing")
        assert "abcdef123" not in s
        assert REDACTED in s

    def test_github_pat(self, engine: RedactionEngine):
        pat = "ghp_" + "x" * 40
        s = engine.redact_string(f"clone with {pat} now")
        assert pat not in s

    def test_non_secret_text_unchanged(self, engine: RedactionEngine):
        s = "this is a normal log line about cwd=/data/work and seq=42"
        assert engine.redact_string(s) == s


class TestKeyBasedMasking:
    def test_top_level_key(self, engine: RedactionEngine):
        d = engine.redact_dict({"api_key": "abc", "model": "claude"})
        assert d["api_key"] == REDACTED
        assert d["model"] == "claude"

    def test_key_lookup_is_case_insensitive(self, engine: RedactionEngine):
        d = engine.redact_dict({"API_KEY": "abc", "ANTHROPIC_API_KEY": "x"})
        assert d["API_KEY"] == REDACTED
        assert d["ANTHROPIC_API_KEY"] == REDACTED

    def test_nested_dict(self, engine: RedactionEngine):
        d = engine.redact_dict(
            {"outer": {"token": "v", "ok": "shown"}, "ok2": "kept"}
        )
        assert d["outer"]["token"] == REDACTED
        assert d["outer"]["ok"] == "shown"
        assert d["ok2"] == "kept"

    def test_list_of_dicts(self, engine: RedactionEngine):
        d = engine.redact_dict(
            {"items": [{"password": "p1"}, {"password": "p2"}, {"name": "nm"}]}
        )
        passwords = [item.get("password") for item in d["items"][:2]]
        assert all(p == REDACTED for p in passwords)
        assert d["items"][2]["name"] == "nm"

    def test_input_not_mutated(self, engine: RedactionEngine):
        src = {"token": "stay-original", "child": {"password": "p"}}
        copy = engine.redact_dict(src)
        assert src["token"] == "stay-original"
        assert src["child"]["password"] == "p"
        assert copy["token"] == REDACTED


class TestIdempotency:
    def test_redacting_twice_is_no_op(self, engine: RedactionEngine):
        once = engine.redact_string("token: abcXYZ-12345")
        twice = engine.redact_string(once)
        assert once == twice

    def test_dict_redact_idempotent(self, engine: RedactionEngine):
        a = engine.redact_dict({"api_key": "secret", "msg": "Bearer abc.def"})
        b = engine.redact_dict(a)
        assert a == b


class TestCustomPatterns:
    def test_caller_can_add_pattern(self):
        custom = (re.compile(r"INTERNAL-[0-9]{6}"),)
        eng = RedactionEngine(patterns=custom)
        assert (
            eng.redact_string("ticket INTERNAL-123456 active")
            == f"ticket {REDACTED} active"
        )
        # Default patterns aren't loaded — sk-ant- still leaks here.
        assert "sk-ant-x" in eng.redact_string("token sk-ant-xyz seen")

    def test_caller_can_extend_sensitive_keys(self):
        eng = RedactionEngine(sensitive_keys=["custom_secret"])
        d = eng.redact_dict({"custom_secret": "x", "api_key": "y"})
        assert d["custom_secret"] == REDACTED
        # api_key is not in the custom set, so the pattern path covers
        # ``api_key=...`` shaped strings but a dict KEY of "api_key" is
        # not blanked when the caller overrides the key set entirely.
        assert d["api_key"] == "y"


class TestFrameRedaction:
    def test_frame_helper_redacts_payload(self, engine: RedactionEngine):
        frame = {
            "v": 1,
            "type": "msg.chunk",
            "seq": 3,
            "ts": "2026-05-22T10:00:00Z",
            "data": {"prompt": "use api_key=abcdef nowhere"},
        }
        out = engine.redact_frame(frame)
        assert out["seq"] == 3
        assert out["type"] == "msg.chunk"
        assert REDACTED in out["data"]["prompt"]


class TestSensitiveKeysProperty:
    def test_default_keys_include_common_names(self, engine: RedactionEngine):
        keys = engine.sensitive_keys
        assert "api_key" in keys
        assert "anthropic_api_key" in keys
        assert "credentials" in keys
