"""
dispatcher/app/image_registry.py
───────────────────────────────────
In-memory cache of Docker images known locally on the dispatcher node.

Populated at startup by ``populate()`` — scans ``client.images.list()`` and
builds a dict keyed by the first tag on each image (or ``<none>`` if none).

Provides:
    ``list_images()``  – return all cached images as serialisable dicts.
    ``check_image(name)`` – look up a single image by tag.
"""
from __future__ import annotations

import typing as t
import logging
from docker.errors import APIError

logger = logging.getLogger("caas.dispatcher")

# Mutable global owned by this module — populated at app start.
_image_cache: t.Optional[t.List[t.Dict]] = None


def _tag_for_image(image) -> str:
    """Return the first human-readable tag for *image*, or '<none>' fallback."""
    tags = image.tags
    if tags:
        return tags[0]
    for rt in image.attrs.get("RepoTags", []):
        if rt and rt != "<none>":
            return rt
    return f"<none> (id {image.id[:12]})"


def _image_size_mb(image) -> float:
    """Return image size in MiB (rounded)."""
    raw = image.attrs.get("Size", 0)
    if not isinstance(raw, (int, float)):
        return 0
    return round(raw / (1024 * 1024), 1)


def _image_labels(image) -> dict:
    """Extract image Config labels or empty dict."""
    config_labels = image.attrs.get("Config", {}).get("Labels")
    if isinstance(config_labels, dict):
        return config_labels
    return {}


def _serialize_image(image) -> dict:
    """Convert a Docker Image object into a plain, serialisable dict."""
    tag = _tag_for_image(image)
    return {
        "name": tag,
        "id": image.id,
        "short_id": image.short_id,
        "size_mb": _image_size_mb(image),
        "labels": _image_labels(image),
        "created": image.attrs.get("Created", ""),
    }


def populate(docker_client) -> None:
    """Fetch all local images from Docker and populate the global cache."""
    global _image_cache
    _image_cache = []
    try:
        images = docker_client.images.list()
        _image_cache = [_serialize_image(img) for img in images]
        logger.info("Image registry populated: %d images cached.", len(_image_cache))
    except APIError as e:
        logger.warning("Failed to populate image registry: %s", e)


def list_images() -> t.List[dict]:
    """Return all cached images as serialisable dicts."""
    return list(_image_cache) if _image_cache else []


def check_image(name: str) -> t.Optional[dict]:
    """Check whether *name* (a full tag string) is in the cache.

    Returns the image dict if found, ``None`` otherwise.
    """
    for img in (_image_cache or []):
        if img["name"] == name:
            return img
    return None
