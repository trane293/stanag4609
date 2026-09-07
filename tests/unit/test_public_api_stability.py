"""Compatibility guard for the documented public import surfaces."""

from __future__ import annotations

import hashlib
import importlib

import pytest

PUBLIC_API_BASELINE = {
    "stanag4609": (526, "f2e59102cb949faf1b757e7d1a020431c3c943da6823e61ec55b70bcdb0e68ca"),
    "stanag4609.audio": (
        3,
        "ced310ee79e54be8083985e96966cc082836ddf133725b164a9ded05f69212fd",
    ),
    "stanag4609.klv": (
        17,
        "9cf4d2e69181018578d0083a692d54811b06cf9fcdbe7655e483c7cbf01e531d",
    ),
    "stanag4609.player": (
        19,
        "34f92d22abb8b524181d413e1650d3b90720f9d3446d493720b4207402bc9fa4",
    ),
    "stanag4609.sidecar": (
        31,
        "3de4ed0fabf9d4b0fa5521bc9d1a705cdf4aeba5b52b8734d16d55b1138079d9",
    ),
    "stanag4609.transport": (
        165,
        "2a25f297e9195679b5a83dea2cc5d8cb54dc1d0d26146c3960200ab295420787",
    ),
}


@pytest.mark.parametrize("module_name", PUBLIC_API_BASELINE)
def test_public_api_matches_reviewed_baseline(module_name: str) -> None:
    """Require an explicit review whenever the supported import surface changes."""

    module = importlib.import_module(module_name)
    names = list(module.__all__)
    assert len(names) == len(set(names)), f"{module_name}.__all__ contains duplicates"
    assert not [name for name in names if not hasattr(module, name)]

    payload = ("\n".join(sorted(names)) + "\n").encode()
    actual = (len(names), hashlib.sha256(payload).hexdigest())
    assert actual == PUBLIC_API_BASELINE[module_name]
