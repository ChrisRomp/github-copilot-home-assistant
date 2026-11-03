"""Image guards use small fixtures rather than stressing the validation host."""

import base64
import struct
import zlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from custom_components.github_copilot.images import (
    ImageMetadataError,
    ImageResultCollector,
    prepare_images,
    vision_limits,
)


@pytest.fixture
def image_setup(tmp_path):
    path = tmp_path / "image.png"
    with Image.new("RGB", (32, 32)) as image:
        image.save(path)
    model = SimpleNamespace(
        capabilities=SimpleNamespace(
            supports=SimpleNamespace(vision=True),
            limits=SimpleNamespace(
                vision=SimpleNamespace(
                    max_prompt_images=2,
                    max_prompt_image_size=1024 * 1024,
                    supported_media_types=["image/png", "image/jpeg"],
                )
            ),
        )
    )
    return SimpleNamespace(
        path=path,
        model=model,
        attachment=SimpleNamespace(path=path, mime_type="image/png"),
    )


def test_valid_image_becomes_blob_without_path_or_mutating_source(image_setup):
    setup = image_setup
    before = setup.path.read_bytes()
    result = prepare_images([setup.attachment], setup.model)
    assert result == [
        {
            "type": "blob",
            "mimeType": "image/png",
            "data": base64.b64encode(before).decode(),
        }
    ]
    assert setup.path.read_bytes() == before


def test_corrupt_image_fails_instead_of_being_omitted(image_setup):
    from homeassistant.exceptions import HomeAssistantError

    image_setup.path.write_bytes(b"not an image")
    with pytest.raises(HomeAssistantError, match="valid image"):
        prepare_images([image_setup.attachment], image_setup.model)


def png_chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def small_png(pixel_data, extra_chunk=b""):
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", pixel_data)
        + extra_chunk
        + png_chunk(b"IEND", b"")
    )


def test_invalid_png_chunk_syntax_is_reported_as_invalid_attachment(image_setup):
    from homeassistant.exceptions import HomeAssistantError

    raw = small_png(zlib.compress(b""), png_chunk(b"BAD!", b""))
    with Image.open(BytesIO(raw)) as image, pytest.raises(SyntaxError):
        image.load()
    image_setup.path.write_bytes(raw)
    with pytest.raises(HomeAssistantError, match="valid image attachment"):
        prepare_images([image_setup.attachment], image_setup.model)


def test_fifo_is_rejected_without_blocking_an_executor_thread(image_setup):
    import os

    from homeassistant.exceptions import HomeAssistantError

    fifo = image_setup.path.with_name("image.fifo")
    os.mkfifo(fifo)
    image_setup.attachment.path = fifo
    with pytest.raises(HomeAssistantError, match="regular files"):
        prepare_images([image_setup.attachment], image_setup.model)


def test_sdk_payload_amplification_is_reserved_before_decoding(
    image_setup, monkeypatch
):
    from homeassistant.exceptions import HomeAssistantError

    image_setup.model.capabilities.limits.vision.max_prompt_image_size = (
        20 * 1024 * 1024
    )
    image_setup.path.write_bytes(image_setup.path.read_bytes() + bytes(2 * 1024 * 1024))
    decoder = Mock()
    monkeypatch.setattr(Image, "open", decoder)
    with pytest.raises(HomeAssistantError, match="processing budget"):
        prepare_images([image_setup.attachment], image_setup.model)
    decoder.assert_not_called()


def test_metadata_is_required_and_no_default_is_invented(image_setup):
    image_setup.model.capabilities.limits.vision.max_prompt_images = None
    with pytest.raises(ImageMetadataError):
        vision_limits(image_setup.model)


def test_count_and_actual_mime_are_checked(image_setup):
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match="at most 2"):
        prepare_images([image_setup.attachment] * 3, image_setup.model)
    image_setup.attachment.mime_type = "image/jpeg"
    with pytest.raises(HomeAssistantError, match="MIME"):
        prepare_images([image_setup.attachment], image_setup.model)


def test_pixel_budget_is_checked_before_decode(image_setup, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    image = Mock(format="PNG", width=100_000, height=100_000)
    image.__enter__ = Mock(return_value=image)
    image.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(Image, "open", Mock(return_value=image))
    with pytest.raises(HomeAssistantError, match="dimensions"):
        prepare_images([image_setup.attachment], image_setup.model)
    image.load.assert_not_called()


def test_encoded_budget_is_shared_across_images(image_setup, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.github_copilot import images

    with Image.open(image_setup.path) as source:
        buffer = BytesIO()
        source.save(buffer, format="PNG")
    size = len(buffer.getvalue())
    # Enough for one fixture's raster and encoded buffers, but not a second.
    budget = (
        32 * 32 * 16
        + size * 2
        + images.IMAGE_TRANSPORT_COPIES * (4 * ((size + 2) // 3))
    )
    monkeypatch.setattr(images, "IMAGE_WORKING_SET_BYTES", budget)
    prepare_images([image_setup.attachment], image_setup.model)
    with pytest.raises(HomeAssistantError, match="budget"):
        prepare_images([image_setup.attachment] * 2, image_setup.model)


def image_result(raw):
    return {
        "resultType": "success",
        "contents": [
            {
                "type": "image",
                "data": base64.b64encode(raw).decode(),
                "mimeType": "image/png",
            }
        ],
    }


def test_native_output_duplicate_representations_are_one_image(image_setup):
    result = image_result(image_setup.path.read_bytes())
    result["binaryResultsForLlm"] = result["contents"]
    image = ImageResultCollector().decode(result)
    assert image.width == image.height == 32
    assert image.data == image_setup.path.read_bytes()
    assert image.mime_type == "image/png"


def test_output_png_with_valid_crc_but_invalid_pixels_is_rejected():
    from homeassistant.exceptions import HomeAssistantError

    raw = small_png(b"not a zlib stream")
    with Image.open(BytesIO(raw)) as image:
        image.verify()
    with pytest.raises(HomeAssistantError, match="one valid image"):
        ImageResultCollector().decode(image_result(raw))


def test_output_png_chunk_syntax_is_reported_as_invalid_image():
    from homeassistant.exceptions import HomeAssistantError

    raw = small_png(zlib.compress(b""), png_chunk(b"BAD!", b""))
    with pytest.raises(HomeAssistantError, match="one valid image"):
        ImageResultCollector().decode(image_result(raw))


def test_output_pixel_budget_is_checked_before_decode(monkeypatch, image_setup):
    from homeassistant.exceptions import HomeAssistantError

    image = Mock(format="PNG", is_animated=False, size=(100_000, 100_000))
    opened = Mock(return_value=image)
    monkeypatch.setattr(Image, "open", opened)
    with pytest.raises(HomeAssistantError, match="processing budget"):
        ImageResultCollector().decode(image_result(image_setup.path.read_bytes()))
    opened.assert_called_once()
    image.verify.assert_not_called()
    image.load.assert_not_called()
    image.close.assert_called_once()


def test_native_asset_reference_is_resolved_and_cleared(image_setup):
    raw = image_setup.path.read_bytes()
    collector = ImageResultCollector()
    collector.add_asset(
        {
            "assetId": "asset",
            "type": "image",
            "data": base64.b64encode(raw).decode(),
            "byteLength": len(raw),
            "mimeType": "image/png",
        }
    )
    image = collector.decode(
        {
            "resultType": "success",
            "binaryResultsForLlm": [{"type": "binary_asset", "assetId": "asset"}],
        }
    )
    assert image.data == raw
    assert not collector.assets


def test_persistence_omission_does_not_discard_usable_output(image_setup):
    result = image_result(image_setup.path.read_bytes())
    result["binaryResultsForLlm"] = [
        {"type": "image", "omittedReason": "too_large", "mimeType": "image/png"}
    ]
    assert ImageResultCollector().decode(result).data == image_setup.path.read_bytes()


@pytest.mark.parametrize(
    "result",
    [
        {"resultType": "failure"},
        {
            "resultType": "success",
            "contents": [{"type": "resource_link", "uri": "/tmp/image.png"}],
        },
        {
            "resultType": "success",
            "contents": [{"type": "binary_asset", "assetId": "unknown"}],
        },
        {
            "resultType": "success",
            "contents": [{"type": "image", "data": "invalid", "mimeType": "image/png"}],
        },
    ],
)
def test_invalid_native_image_outputs_fail_explicitly(result):
    from homeassistant.exceptions import HomeAssistantError

    with pytest.raises(HomeAssistantError, match="one valid image"):
        ImageResultCollector().decode(result)


def test_conflicting_image_metadata_is_not_trusted(image_setup):
    from homeassistant.exceptions import HomeAssistantError

    result = image_result(image_setup.path.read_bytes())
    result["structuredContent"] = {
        "imageGeneration": {"images": [{"width": 1000, "height": 1000}]}
    }
    with pytest.raises(HomeAssistantError, match="one valid image"):
        ImageResultCollector().decode(result)


def test_oversized_output_is_rejected_before_hashing_or_decoding(monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.github_copilot import images

    monkeypatch.setattr(images, "IMAGE_WORKING_SET_BYTES", 8)
    digest = Mock()
    monkeypatch.setattr(images.hashlib, "sha256", digest)
    with pytest.raises(HomeAssistantError, match="processing budget"):
        ImageResultCollector().decode(
            {
                "resultType": "success",
                "contents": [
                    {"type": "image", "mimeType": "image/png", "data": "AAAA"}
                ],
            }
        )
    digest.assert_not_called()
