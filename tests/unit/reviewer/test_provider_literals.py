from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from tests.conftest import CONSUMER_ALPHA
from tests.integration.fake_omp import CATALOG

from theseus_reviewer import providers
from theseus_reviewer._vendor import yaml

pytestmark = [pytest.mark.unit, pytest.mark.fast]

SOURCE = Path(__file__).resolve().parents[3] / "src" / "theseus_reviewer"
VENDORED = SOURCE / "_vendor"


KNOWN_OMP_PROVIDERS = frozenset(
    {"anthropic", "openai-codex", "google-antigravity", "muse-code", "xai", "github-copilot", "zeta-ai"}
)
ALLOWED = {
    ("candidate_checks.py", "anthropic/"),
    ("candidate_checks.py", "anthropic/{}"),
    ("candidate_checks.py", "openai-codex/gpt-6.1-sol"),
}


def _catalog_providers() -> set[str]:
    config = yaml.safe_load((CONSUMER_ALPHA / ".github" / "review-omp-config.yml").read_text(encoding="utf-8"))
    return {*KNOWN_OMP_PROVIDERS, *providers.routed(config), *(name.partition("/")[0] for name, _, _ in CATALOG)}


def _mentions(text: str, known: set[str]) -> list[str]:
    return [name for name in sorted(known) if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text)]


def _texts(tree: ast.AST) -> list[tuple[int, str]]:
    found = [(node.lineno, node.value) for node in _literals(tree)]
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            folded = _folded(node)
            if folded is not None:
                found.append((node.lineno, folded))
        if isinstance(node, ast.JoinedStr):
            found.append(
                (node.lineno, "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values))
            )
    return found


def _folded(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded(node.left), _folded(node.right)
        return None if left is None or right is None else left + right
    return None


def _sources() -> list[Path]:
    return [path for path in sorted(SOURCE.rglob("*.py")) if VENDORED not in path.parents]


def _literals(tree: ast.AST) -> list[ast.Constant]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_the_provider_ids_the_scan_looks_for_are_the_catalog_providers() -> None:
    assert {"anthropic", "openai-codex", "google-antigravity"} <= _catalog_providers()


def test_no_source_module_names_a_provider_in_a_literal_a_qualified_model_or_a_joined_string() -> None:
    known = _catalog_providers()
    found = [
        f"{path.name}:{line}: {text!r}"
        for path in _sources()
        for line, text in _texts(ast.parse(path.read_text(encoding="utf-8")))
        if _mentions(text, known) and (path.name, text) not in ALLOWED
    ]
    assert found == []


def test_the_scan_catches_a_split_provider_string_and_a_model_qualified_literal() -> None:
    known = _catalog_providers()
    tree = ast.parse('A = "anth" + "ropic"\nB = "xai/grok-5"\nC = f"muse-code/{name}"\nD = "legacy-anthropic-name"')
    caught = [text for _, text in _texts(tree) if _mentions(text, known)]
    assert "anthropic" in caught and "xai/grok-5" in caught and "muse-code/{}" in caught
    assert "legacy-anthropic-name" not in caught


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
