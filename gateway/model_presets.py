"""Public model provider presets used by the configuration console."""


_MODEL_PRESETS = {
    "grok": {
        "label": "Grok",
        "base_url": "https://api.x.ai/v1",
        "mode": "responses",
        "models": ["grok-4.5"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "mode": "chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "custom": {
        "label": "自定义",
        "base_url": "",
        "mode": "chat",
        "models": [],
    },
}


def public_model_presets() -> dict[str, dict]:
    """Return a copy of the non-secret provider metadata for the UI."""

    return {
        provider: {**preset, "models": list(preset["models"])}
        for provider, preset in _MODEL_PRESETS.items()
    }
