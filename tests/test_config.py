from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import ConfigError, deep_merge, load_config
from harness.models import FakeModelClient, client_for_role


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_project_overrides_user_config(tmp_path: Path) -> None:
    user = write(
        tmp_path / "user.yaml",
        """
providers: {a: {type: fake}, b: {type: fake}}
models:
  default: {provider: a, model: big}
  roles: {implementer: {provider: a, model: small}}
""",
    )
    project = tmp_path / "proj"
    write(
        project / ".harness" / "config.yaml",
        "models: {roles: {implementer: {provider: b, model: other, max_tokens: 100}}}",
    )
    cfg = load_config(project, user)
    assert cfg.model_for("implementer").provider == "b"
    assert cfg.model_for("implementer").max_tokens == 100
    assert cfg.model_for("reviewer").model == "big"  # falls back to default


def test_unknown_provider_rejected(tmp_path: Path) -> None:
    user = write(tmp_path / "u.yaml", "models: {default: {provider: nope, model: x}}")
    with pytest.raises(ConfigError, match="unknown provider"):
        load_config(None, user)


def test_no_model_for_role(tmp_path: Path) -> None:
    cfg = load_config(None, tmp_path / "missing.yaml")
    with pytest.raises(ConfigError, match="no model configured"):
        cfg.model_for("implementer")


def test_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(None, write(tmp_path / "u.yaml", "providers: [unclosed"))


def test_client_for_role(tmp_path: Path) -> None:
    user = write(
        tmp_path / "u.yaml",
        """
providers: {f: {type: fake}, real: {type: anthropic}}
models: {default: {provider: f, model: m}, roles: {reviewer: {provider: real, model: x}}}
""",
    )
    cfg = load_config(None, user)
    assert isinstance(client_for_role(cfg, "implementer"), FakeModelClient)
    with pytest.raises(ConfigError, match="not available"):
        client_for_role(cfg, "reviewer")


def test_deep_merge() -> None:
    assert deep_merge({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"c": 3}, "l": [2]}) == {
        "a": {"b": 1, "c": 3},
        "l": [2],
    }
