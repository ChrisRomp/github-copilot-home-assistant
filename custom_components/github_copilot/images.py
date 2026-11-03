"""Bounded image preparation; no automatic local resizing or source downloads."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from PIL import Image, UnidentifiedImageError

from .const import VERIFIED_IMAGE_RUNTIME
from .models import supports_vision

if TYPE_CHECKING:
    from copilot.session import Attachment as SDKAttachment
    from homeassistant.components.conversation import Attachment

# Admission estimate, not total process RSS; see the bounded Linux resource probe.
IMAGE_WORKING_SET_BYTES = 64 * 1024 * 1024
# Account for SDK JSON-RPC, event, and history copies observed on Linux ARM64.
IMAGE_TRANSPORT_COPIES = 32
_SDK_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


def verified_image_runtime() -> bool:
    """A new SDK/runtime needs its image contracts and resource profile rechecked."""
    from copilot._cli_version import CLI_VERSION

    return CLI_VERSION == VERIFIED_IMAGE_RUNTIME


class ImageMetadataError(HomeAssistantError):
    """Discovery did not provide enough information to safely send images."""


@dataclass(frozen=True)
class ImageLimits:
    count: int
    size: int
    mime_types: frozenset[str]


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    model: str | None


class ImageResultCollector:
    """Resolve only bytes returned by one native RPC or its referenced assets."""

    def __init__(self) -> None:
        self.assets: dict[str, dict[str, Any]] = {}
        self.encoded_bytes = 0
        self.error: str | None = None

    def add_asset(self, asset: dict[str, Any]) -> None:
        identifier, data = asset.get("assetId"), asset.get("data")
        if self.error is not None:
            return
        if (
            not isinstance(identifier, str)
            or len(identifier) > 128
            or not isinstance(data, str)
        ):
            self.error = "Invalid image asset"
            return
        asset = {
            key: asset.get(key)
            for key in ("assetId", "data", "mimeType", "byteLength", "type")
        }
        if identifier in self.assets:
            if self.assets[identifier] != asset:
                self.error = "Conflicting image asset"
            return
        self.encoded_bytes += len(data)
        if self.encoded_bytes * 8 > IMAGE_WORKING_SET_BYTES:
            self.error = "Image output exceeds the host processing budget"
            self.assets.clear()
            return
        self.assets[identifier] = asset

    def decode(self, result: dict[str, Any]) -> GeneratedImage:
        """Validate one canonical PNG; never follow model-written paths/URLs."""
        try:
            return self._decode(result)
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            SyntaxError,
            Image.DecompressionBombError,
        ) as err:
            raise HomeAssistantError(
                "GitHub Copilot did not return one valid image within the host "
                "processing budget. The generation was not retried and may have "
                "incurred usage."
            ) from err
        finally:
            self.assets.clear()

    def _decode(self, result: dict[str, Any]) -> GeneratedImage:
        if self.error is not None or not isinstance(result, dict):
            raise ValueError(self.error or "Invalid image result")
        if result.get("resultType") != "success":
            raise ValueError("Native image generation failed")
        images: dict[str, bytes] = {}
        encoded_seen: set[str] = set()
        total_encoded = 0
        for field in ("contents", "binaryResultsForLlm"):
            blocks = result.get(field) or []
            if not isinstance(blocks, list):
                raise ValueError("Invalid content blocks")
            for block in blocks:
                if not isinstance(block, dict):
                    raise ValueError("Invalid content block")
                if "assetId" in block:
                    block = self.assets.get(block["assetId"], {})
                if block.get("type") != "image" or "data" not in block:
                    continue
                data = block["data"]
                if not isinstance(data, str) or block.get("mimeType") != "image/png":
                    raise ValueError("Invalid native PNG")
                if len(data) * 8 > IMAGE_WORKING_SET_BYTES:
                    raise ValueError("Image transport budget exceeded")
                digest = hashlib.sha256(data.encode("ascii")).hexdigest()
                if digest in encoded_seen:
                    continue
                encoded_seen.add(digest)
                total_encoded += len(data)
                if (total_encoded + self.encoded_bytes) * 8 > IMAGE_WORKING_SET_BYTES:
                    raise ValueError("Image transport budget exceeded")
                decoded = base64.b64decode(data, validate=True)
                if "byteLength" in block and block["byteLength"] != len(decoded):
                    raise ValueError("Image asset size mismatch")
                images[hashlib.sha256(decoded).hexdigest()] = decoded
                if len(images) > 1:
                    raise ValueError("Ambiguous image outputs")
        if len(images) != 1:
            raise ValueError("Missing canonical image bytes")
        digest, raw = next(iter(images.items()))
        with closing(Image.open(BytesIO(raw))) as image:
            if image.format != "PNG" or getattr(image, "is_animated", False):
                raise ValueError("Expected one static PNG")
            width, height = image.size
            if (
                width * height * 16 + (total_encoded + self.encoded_bytes) * 4
                > IMAGE_WORKING_SET_BYTES
            ):
                raise ValueError("Image dimensions exceed host budget")
            image.verify()
        with closing(Image.open(BytesIO(raw))) as image:
            image.load()
        structured = result.get("structuredContent") or {}
        if not isinstance(structured, dict):
            raise ValueError("Invalid image metadata")
        metadata = structured.get("imageGeneration", {})
        if not isinstance(metadata, dict):
            raise ValueError("Invalid image metadata")
        descriptions = metadata.get("images") or []
        if descriptions:
            if not isinstance(descriptions, list) or len(descriptions) != 1:
                raise ValueError("Ambiguous image metadata")
            description = descriptions[0]
            if not isinstance(description, dict):
                raise ValueError("Invalid image description")
            if description.get("imageId", f"sha256:{digest}") != f"sha256:{digest}":
                raise ValueError("Image identity mismatch")
            if (
                description.get("width", width) != width
                or description.get("height", height) != height
            ):
                raise ValueError("Image dimensions mismatch")
        requested_model = metadata.get("requestedModel") or {}
        if not isinstance(requested_model, dict):
            raise ValueError("Invalid model metadata")
        model = requested_model.get("id")
        if model is not None and not isinstance(model, str):
            raise ValueError("Invalid model metadata")
        return GeneratedImage(raw, "image/png", width, height, model)


def vision_limits(model: Any) -> ImageLimits:
    """Do not invent limits for an unverified model/runtime combination."""
    limits = getattr(
        getattr(getattr(model, "capabilities", None), "limits", None), "vision", None
    )
    count = getattr(limits, "max_prompt_images", None)
    size = getattr(limits, "max_prompt_image_size", None)
    types = getattr(limits, "supported_media_types", None)
    if (
        not supports_vision(model)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
        or not isinstance(types, list)
        or not types
        or not all(isinstance(item, str) for item in types)
    ):
        raise ImageMetadataError(
            "The AI Task model's image limits are unavailable. Choose a model "
            "with published vision limits; text tasks remain available."
        )
    return ImageLimits(count, size, frozenset(types) & _SDK_MIME_TYPES)


def prepare_images(attachments: list[Attachment], model: Any) -> list[SDKAttachment]:
    """Validate in one executor job, retaining bounded encoded data only."""
    limits = vision_limits(model)
    if len(attachments) > limits.count:
        raise HomeAssistantError(
            f"The AI Task model accepts at most {limits.count} images per request."
        )
    result: list[SDKAttachment] = []
    transport_bytes = 0
    for attachment in attachments:
        remaining = IMAGE_WORKING_SET_BYTES - transport_bytes
        read_limit = min(limits.size, remaining * 3 // (4 * IMAGE_TRANSPORT_COPIES + 6))
        if read_limit < 1:
            raise HomeAssistantError("Image inputs exceed the host processing budget.")
        try:
            descriptor = os.open(attachment.path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise HomeAssistantError(
                        "Image attachments must be local regular files."
                    )
                data = source.read(read_limit + 1)
            if len(data) > read_limit:
                raise HomeAssistantError(
                    "Image inputs exceed the model or host processing budget. "
                    "Supply a lower-resolution camera/image source."
                )
            with closing(Image.open(BytesIO(data))) as image:
                mime = Image.MIME.get(image.format, "")
                if mime not in limits.mime_types or (
                    attachment.mime_type
                    and attachment.mime_type.split(";", 1)[0].lower() != mime
                ):
                    raise HomeAssistantError(
                        "The image format does not match its MIME type or is not "
                        "supported by the AI Task model."
                    )
                encoded_size = 4 * ((len(data) + 2) // 3)
                # Account for decoder/native rasters and duplicated JSON/base64 buffers.
                estimate = (
                    transport_bytes
                    + encoded_size * IMAGE_TRANSPORT_COPIES
                    + len(data) * 2
                    + image.width * image.height * 16
                )
                if estimate > IMAGE_WORKING_SET_BYTES:
                    raise HomeAssistantError(
                        "The image dimensions exceed the host processing budget. "
                        "Supply a lower-resolution camera/image source."
                    )
                if getattr(image, "is_animated", False):
                    raise HomeAssistantError(
                        "Animated or multi-frame images are not supported."
                    )
                image.load()
        except (
            OSError,
            ValueError,
            SyntaxError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ) as err:
            raise HomeAssistantError(
                "Could not read a valid image attachment. No AI request was sent."
            ) from err
        result.append(
            {
                "type": "blob",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": mime,
            }
        )
        transport_bytes += encoded_size * IMAGE_TRANSPORT_COPIES
        del data
    return result
