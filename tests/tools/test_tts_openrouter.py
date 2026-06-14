"""Tests for the native OpenRouter TTS provider."""

import json
from unittest.mock import MagicMock, patch

import pytest

from gateway.session_context import _UNSET, _VAR_MAP
from tools import tts_tool


def _reset_session_context() -> None:
    for var in _VAR_MAP.values():
        var.set(_UNSET)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    _reset_session_context()
    yield
    _reset_session_context()


def test_generate_openrouter_tts_defaults_to_mp3(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    response = MagicMock()
    response.status_code = 200
    response.content = b"mp3-bytes"

    with patch("requests.post", return_value=response) as post:
        output = tmp_path / "speech.mp3"
        result = tts_tool._generate_openrouter_tts("Hello", str(output), {})

    assert result == str(output)
    assert output.read_bytes() == b"mp3-bytes"
    url = post.call_args[0][0]
    payload = post.call_args.kwargs["json"]
    assert url == "https://openrouter.ai/api/v1/audio/speech"
    assert payload["model"] == "microsoft/mai-voice-2"
    assert payload["voice"] == "en-US-Harper:MAI-Voice-2"
    assert payload["response_format"] == "mp3"


def test_generate_openrouter_tts_uses_config(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    response = MagicMock(status_code=200, content=b"audio")
    config = {
        "speed": 1.25,
        "openrouter": {
            "model": "microsoft/mai-voice-2",
            "voice": "en-US-Harper:MAI-Voice-2",
            "response_format": "mp3",
            "provider": {"order": ["azure"]},
        },
    }

    with patch("requests.post", return_value=response) as post:
        tts_tool._generate_openrouter_tts("Hello", str(tmp_path / "out.mp3"), config)

    payload = post.call_args.kwargs["json"]
    assert payload["model"] == "microsoft/mai-voice-2"
    assert payload["voice"] == "en-US-Harper:MAI-Voice-2"
    assert payload["response_format"] == "mp3"
    assert payload["speed"] == 1.25
    assert payload["provider"] == {"order": ["azure"]}


def test_text_to_speech_openrouter_keeps_generation_mp3_for_telegram(monkeypatch, tmp_path):
    requested = tmp_path / "speech.ogg"
    generated = tmp_path / "speech.mp3"
    opus = tmp_path / "speech.ogg"
    generated_paths = []

    def fake_generate(_text, output_path, _config):
        generated_paths.append(output_path)
        assert output_path == str(generated)
        generated.write_bytes(b"mp3")
        return output_path

    def fake_convert(path):
        assert path == str(generated)
        opus.write_bytes(b"ogg")
        return str(opus)

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setattr(
        tts_tool,
        "_load_tts_config",
        lambda: {"provider": "openrouter", "openrouter": {"model": "microsoft/mai-voice-2"}},
    )
    monkeypatch.setattr(tts_tool, "_generate_openrouter_tts", fake_generate)
    monkeypatch.setattr(tts_tool, "_convert_to_opus", fake_convert)

    result = json.loads(tts_tool.text_to_speech_tool("hello", output_path=str(requested)))

    assert generated_paths == [str(generated)]
    assert result["success"] is True
    assert result["provider"] == "openrouter"
    assert result["file_path"] == str(opus)
    assert result["voice_compatible"] is True
    assert result["media_tag"] == f"[[audio_as_voice]]\nMEDIA:{opus}"


def test_generate_openrouter_tts_missing_key(tmp_path):
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        tts_tool._generate_openrouter_tts("Hello", str(tmp_path / "out.mp3"), {})


def test_generate_openrouter_tts_api_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    response = MagicMock()
    response.status_code = 400
    response.text = '{"error":{"message":"bad voice"}}'
    response.json.return_value = {"error": {"message": "bad voice"}}

    with patch("requests.post", return_value=response), pytest.raises(
        RuntimeError,
        match="bad voice",
    ):
        tts_tool._generate_openrouter_tts("Hello", str(tmp_path / "out.mp3"), {})
