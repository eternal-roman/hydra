"""Brain pins: Opus 5.5 at max effort, Grok 4.7 at xhigh."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hydra_brain


class _Block:
    type = "text"
    text = '{"decision":"HOLD"}'


class _Usage:
    input_tokens = 4
    output_tokens = 5
    prompt_tokens = 4
    completion_tokens = 5


class _AnthMsg:
    content = [_Block()]
    usage = _Usage()
    stop_reason = "end_turn"


class _Choice:
    class message:
        content = "ok"
    finish_reason = "stop"


class _XResp:
    choices = [_Choice()]
    usage = _Usage()


class _Messages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _AnthMsg()


class _Completions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _XResp()


class _Chat:
    def __init__(self):
        self.completions = _Completions()


class _Client:
    def __init__(self):
        self.messages = _Messages()
        self.chat = _Chat()


def test_constants():
    assert hydra_brain.ANTHROPIC_MODEL == "claude-opus-5-5"
    assert hydra_brain.ANTHROPIC_EFFORT == "max"
    assert hydra_brain.GROK_TRADING_MODEL == "grok-4.7"
    assert hydra_brain.GROK_REASONING_EFFORT == "xhigh"
    assert hydra_brain.COST_ANTHROPIC == (4.0, 20.0)
    assert hydra_brain.COST_XAI == (2.0, 6.0)


def test_anthropic_call_uses_max_effort_and_token_floor():
    brain = hydra_brain.HydraBrain.__new__(hydra_brain.HydraBrain)
    client = _Client()
    text, tin, tout = brain._call_llm(
        "system", "user", 300,
        client=client, provider="anthropic", model="claude-opus-5-5",
    )
    assert text == '{"decision":"HOLD"}'
    assert tin == 4 and tout == 5
    kw = client.messages.kwargs
    assert kw["output_config"] == {"effort": "max"}
    assert kw["max_tokens"] == 8192
    assert "temperature" not in kw


def test_grok_trading_call_uses_xhigh():
    brain = hydra_brain.HydraBrain.__new__(hydra_brain.HydraBrain)
    client = _Client()
    brain._call_llm(
        "system", "user", 350,
        client=client, provider="xai", model="grok-4.7",
    )
    kw = client.chat.completions.kwargs
    assert kw["extra_body"] == {"reasoning_effort": "xhigh"}
    assert kw["max_tokens"] == 8192
    assert kw["timeout"] == 120.0
