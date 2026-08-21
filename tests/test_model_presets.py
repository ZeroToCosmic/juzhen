import json

from gateway.model_presets import public_model_presets


def test_public_model_presets_exposes_only_public_grok_deepseek_and_custom_details():
    presets = public_model_presets()

    assert set(presets) == {"grok", "deepseek", "custom"}
    assert presets["grok"] == {
        "label": "Grok",
        "base_url": "https://api.x.ai/v1",
        "mode": "responses",
        "models": ["grok-4.5"],
    }
    assert presets["deepseek"] == {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "mode": "chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    }
    assert presets["custom"] == {
        "label": "自定义",
        "base_url": "",
        "mode": "chat",
        "models": [],
    }
    assert "api_key" not in json.dumps(presets)
