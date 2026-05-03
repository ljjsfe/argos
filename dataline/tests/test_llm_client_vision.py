"""Tests for LLMClient vision support.

Most tests are unit-level (no real API call) and exercise the multipart
content builder + fallback paths. One integration test against the real
DashScope endpoint is included but skipped unless DASHSCOPE_API_KEY is set
in the environment, since live calls cost money.
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dataline.core.llm_client import LLMClient, LLMConfig


def _make_dummy_png(path: Path) -> None:
    """Tiny 1x1 white PNG so tests don't depend on Pillow at runtime."""
    # 67 bytes — minimal valid PNG (white 1x1)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452"
        "0000000100000001080600000040b0e9"
        "910000000d49444154789c63f8ffff3f"
        "0000050001017a4d3a410000000049454e44ae426082"
    )
    path.write_bytes(png_bytes)


def _client(provider: str = "dashscope") -> LLMClient:
    return LLMClient(LLMConfig(
        provider=provider,
        model="qwen3.5-35b-a3b",
        api_key="test-key",
        base_url="https://example.test/v1",
    ))


# --- _build_vision_content ---


def test_build_vision_content_emits_text_and_image_parts():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "img.png"
        _make_dummy_png(path)

        client = _client()
        parts = client._build_vision_content("describe", [str(path)])

        assert len(parts) == 2
        assert parts[0] == {"type": "text", "text": "describe"}
        assert parts[1]["type"] == "image_url"
        url = parts[1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")


def test_build_vision_content_handles_multiple_images():
    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "a.png"; _make_dummy_png(a)
        b = Path(tmp) / "b.png"; _make_dummy_png(b)

        client = _client()
        parts = client._build_vision_content("compare", [str(a), str(b)])

        assert len(parts) == 3  # 1 text + 2 images
        assert parts[0]["type"] == "text"
        assert parts[1]["type"] == "image_url"
        assert parts[2]["type"] == "image_url"


def test_build_vision_content_raises_on_empty_list():
    client = _client()
    with pytest.raises(ValueError, match="image_paths is empty"):
        client._build_vision_content("text", [])


def test_build_vision_content_raises_on_missing_file():
    client = _client()
    with pytest.raises(OSError, match="image not found"):
        client._build_vision_content("text", ["/no/such/file.png"])


def test_build_vision_content_defaults_to_png_for_unknown_extension():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "image.bin"  # extension mimetypes can't classify
        _make_dummy_png(path)

        client = _client()
        parts = client._build_vision_content("x", [str(path)])

        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


# --- chat_with_image: success path (mocked) ---


def test_chat_with_image_calls_openai_with_multipart_content():
    """Mock the underlying client; verify the message structure sent."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "img.png"
        _make_dummy_png(path)

        client = _client()

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="ANSWER"))]
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=10)

        with patch.object(client._client.chat.completions, "create",
                          return_value=mock_response) as mock_create:
            text = client.chat_with_image(
                system="sys", user="What is in this image?",
                image_paths=[str(path)],
            )

        assert text == "ANSWER"
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        messages = call_kwargs["messages"]
        # system is a plain string
        assert messages[0] == {"role": "system", "content": "sys"}
        # user is a multipart list
        assert messages[1]["role"] == "user"
        content = messages[1]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "What is in this image?"}
        assert content[1]["type"] == "image_url"


# --- fallback_to_text behavior ---


def test_chat_with_image_fallback_on_failure():
    """When vision call raises, fallback_to_text=True should retry as text."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "img.png"
        _make_dummy_png(path)

        client = _client()

        # First call (vision) raises; second call (text fallback) succeeds.
        mock_text_response = MagicMock()
        mock_text_response.choices = [MagicMock(message=MagicMock(content="TEXT_ANSWER"))]
        mock_text_response.usage = MagicMock(prompt_tokens=50, completion_tokens=5)

        call_count = {"n": 0}
        def side_effect(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("model does not support vision input")
            return mock_text_response

        with patch.object(client._client.chat.completions, "create",
                          side_effect=side_effect):
            text = client.chat_with_image(
                system="sys", user="describe",
                image_paths=[str(path)],
                fallback_to_text=True,
            )

        assert text == "TEXT_ANSWER"
        assert call_count["n"] == 2


def test_chat_with_image_no_fallback_raises():
    """fallback_to_text=False surfaces the underlying error."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "img.png"
        _make_dummy_png(path)

        client = _client()

        with patch.object(client._client.chat.completions, "create",
                          side_effect=RuntimeError("vision rejected")):
            with pytest.raises(RuntimeError, match="vision rejected"):
                client.chat_with_image(
                    system="sys", user="describe",
                    image_paths=[str(path)],
                    fallback_to_text=False,
                )


# --- Anthropic fallback ---


def test_chat_with_image_anthropic_falls_back_to_text():
    """Anthropic provider has no vision schema in this client — should
    transparently fall through to the text chat() path."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "img.png"
        _make_dummy_png(path)

        # Bypass anthropic SDK import (not installed in dev) by stubbing
        # _build_client before construction.
        with patch.object(LLMClient, "_build_client", return_value=MagicMock()):
            client = LLMClient(LLMConfig(
                provider="anthropic",
                model="claude-test",
                api_key="test-key",
            ))

        mock_resp = MagicMock()
        mock_resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_block = MagicMock()
        mock_block.text = "FROM_TEXT_PATH"
        mock_resp.content = [mock_block]

        with patch.object(client._client.messages, "create", return_value=mock_resp):
            text = client.chat_with_image(
                system="sys", user="describe",
                image_paths=[str(path)],
            )

        assert text == "FROM_TEXT_PATH"


# --- Live integration (skipped unless API key + opt-in env flag set) ---


@pytest.mark.skipif(
    not (os.environ.get("DASHSCOPE_API_KEY") and os.environ.get("VISION_LIVE_TEST")),
    reason="live API test — set DASHSCOPE_API_KEY and VISION_LIVE_TEST=1 to run",
)
def test_live_dashscope_vision():
    """Probe the real DashScope endpoint with a synthetic image."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (300, 100), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
    except Exception:
        font = ImageFont.load_default()
    d.text((20, 30), "Q3 sales: 42", fill="black", font=font)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "live.png"
        img.save(path)

        client = LLMClient(LLMConfig(
            provider="dashscope",
            model="qwen3.5-35b-a3b",
            api_key=os.environ["DASHSCOPE_API_KEY"],
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ))

        text = client.chat_with_image(
            system="You read text from images. Reply with only the number.",
            user="What number appears in this image?",
            image_paths=[str(path)],
            fallback_to_text=False,
        )

        assert "42" in text
