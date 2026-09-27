"""Provider shim: Anthropic 1.x no longer accepts temperature=."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions.providers import ProviderClient


class _Block:
    type = "text"
    text = "ok"


class _Usage:
    input_tokens = 3
    output_tokens = 5


class _Message:
    content = [_Block()]
    usage = _Usage()
    stop_reason = "end_turn"


class _Messages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _Message()


class _SDK:
    def __init__(self):
        self.messages = _Messages()
        self.timeout_s = None

    def with_options(self, timeout):
        self.timeout_s = timeout
        return self


def test_anthropic_temperature_goes_through_extra_body():
    client = ProviderClient()
    sdk = _SDK()
    client._anthropic = sdk
    resp = client._call_anthropic(
        "claude-opus-5-5",
        "system",
        [{"role": "user", "content": "hi"}],
        64,
        0.2,
    )
    assert resp.text == "ok"
    assert resp.provider == "anthropic"
    assert resp.tokens_in == 3
    assert resp.tokens_out == 5
    assert sdk.timeout_s == 110.0
    kwargs = sdk.messages.kwargs
    assert "temperature" not in kwargs
    assert "extra_body" not in kwargs
    assert kwargs["output_config"] == {"effort": "max"}
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["max_tokens"] == 8192


class _Choice:
    class message:
        content = "xai-ok"
    finish_reason = "stop"


class _XAIResponse:
    choices = [_Choice()]
    usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 2})()


class _XAICompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _XAIResponse()


class _XAIChat:
    def __init__(self):
        self.completions = _XAICompletions()


class _XAI:
    def __init__(self):
        self.chat = _XAIChat()

    def with_options(self, timeout):
        self.timeout_s = timeout
        return self


def test_xai_still_passes_temperature_as_a_keyword():
    client = ProviderClient()
    sdk = _XAI()
    client._xai = sdk
    resp = client._call_xai(
        "grok-4.3",
        "system",
        [{"role": "user", "content": "hi"}],
        32,
        0.4,
    )
    assert resp.text == "xai-ok"
    assert resp.provider == "xai"
    assert sdk.chat.completions.kwargs["temperature"] == 0.4
    assert "extra_body" not in sdk.chat.completions.kwargs
    assert sdk.chat.completions.kwargs["max_tokens"] == 32


def test_grok_4_7_asks_for_xhigh_and_keeps_temperature():
    client = ProviderClient()
    sdk = _XAI()
    client._xai = sdk
    client._call_xai(
        "grok-4.7",
        "system",
        [{"role": "user", "content": "hi"}],
        32,
        0.15,
    )
    kwargs = sdk.chat.completions.kwargs
    assert kwargs["temperature"] == 0.15
    assert kwargs["extra_body"] == {"reasoning_effort": "xhigh"}
    assert kwargs["max_tokens"] == 8192
    assert sdk.timeout_s == 110.0
