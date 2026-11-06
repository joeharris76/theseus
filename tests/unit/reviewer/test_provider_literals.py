from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.conftest import CONSUMER_ALPHA
from tests.integration.fake_omp import CATALOG

from theseus_reviewer import providers
from theseus_reviewer._vendor import yaml

pytestmark = [pytest.mark.unit, pytest.mark.fast]

SOURCE = Path(__file__).resolve().parents[3] / "src" / "theseus_reviewer"
VENDORED = SOURCE / "_vendor"


def _catalog_providers() -> set[str]:
    config = yaml.safe_load((CONSUMER_ALPHA / ".github" / "review-omp-config.yml").read_text(encoding="utf-8"))
    return {*providers.routed(config), *(selector.partition("/")[0] for selector, _, _ in CATALOG)}


def _sources() -> list[Path]:
    return [path for path in sorted(SOURCE.rglob("*.py")) if VENDORED not in path.parents]


def _literals(tree: ast.AST) -> list[ast.Constant]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_the_provider_ids_the_scan_looks_for_are_the_catalog_providers() -> None:
    assert {"anthropic", "openai-codex", "google-antigravity"} <= _catalog_providers()


def test_no_source_module_holds_a_provider_id_as_a_string_literal() -> None:
    known = _catalog_providers()
    found = [
        f"{path.name}:{node.lineno}: {node.value!r}"
        for path in _sources()
        for node in _literals(ast.parse(path.read_text(encoding="utf-8")))
        if node.value in known
    ]
    assert found == []


def test_no_source_module_holds_a_module_level_providers_constant_with_literal_members() -> None:
    found = []
    for path in _sources():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if any(name.endswith("PROVIDERS") for name in names) and node.value is not None and _literals(node.value):
                found.append(f"{path.name}:{node.lineno}: {names}")
    assert found == []
