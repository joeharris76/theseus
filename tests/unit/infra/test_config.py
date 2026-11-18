from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from theseus_reviewer.infra.config import ConfigError, load_infra, parse_infra

from .example import EXAMPLE, example, write_policy

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def _bad(tmp_path: Path, mutate: Any) -> str:
    data = deepcopy(EXAMPLE)
    mutate(data)
    with pytest.raises(ConfigError) as caught:
        parse_infra(data, tmp_path / "review.yml")
    return str(caught.value)


def test_the_documented_example_parses(tmp_path: Path) -> None:
    config = example(tmp_path)
    assert config.github.repository == "Example-Org/example"
    assert config.github.branch_policies == ("develop", "release/*")
    assert config.tailnet is not None and config.tailnet.tag == "tag:review-ci" and config.tailnet.name == "-"
    assert [spec.name for spec in config.secrets][:2] == ["REVIEW_APP_ID", "REVIEW_APP_PRIVATE_KEY"]
    assert config.secret("REVIEW_APP_PRIVATE_KEY").app_id_secret == "REVIEW_APP_ID"
    assert config.secret("TYPESAFE_API_KEY").mirror_keychain == "TYPESAFE_API_KEY"
    assert config.environment_key == "Example-Org/example:review"


def test_a_policy_file_loads_and_resolves_the_repository_root(tmp_path: Path) -> None:
    path = write_policy(tmp_path / "repo" / ".github" / "review.yml", EXAMPLE)
    config = load_infra(path)
    assert config.repo_root == (tmp_path / "repo").resolve()


def test_the_default_keychain_and_state_locations_expand_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    data = deepcopy(EXAMPLE)
    data["store"] = {"backend": "file"}
    del data["state"]
    config = parse_infra(data, tmp_path / "review.yml")
    assert config.store.directory == tmp_path / ".config" / "theseus" / "infra"
    assert config.state_path == tmp_path / ".config" / "theseus" / "infra-state" / "Example-Org__example__review.json"
    data["store"] = {"backend": "keychain"}
    assert parse_infra(data, tmp_path / "review.yml").store.directory is None


def test_a_policy_without_an_infra_section_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "review.yml"
    path.write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no infra"):
        load_infra(path)
    with pytest.raises(ConfigError, match="cannot read"):
        load_infra(tmp_path / "missing.yml")


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda d: d.update(extra=1), "unknown key 'extra'"),
        (lambda d: d["store"].update(backend="vault"), "keychain or file"),
        (lambda d: d["github"].update(repository="not a repo"), "repository"),
        (lambda d: d["github"].update(branch_policies=[]), "non-empty list"),
        (lambda d: d["github"].update(branch_policies=["a", "a"]), "duplicate"),
        (lambda d: d["tailnet"].update(tag="review-ci"), "infra.tailnet.tag"),
        (lambda d: d["tailnet"].update(deny_port=4000), "differ"),
        (lambda d: d["tailnet"].update(gateway_port=70000), "port number"),
        (lambda d: d["tailnet"].pop("gateway_dst"), "needs gateway_dst"),
        (lambda d: d["tailnet"]["oauth_client"].update(scopes=[]), "non-empty list"),
        (lambda d: d["gateway"].update(client="Review CI"), "infra.gateway.client"),
        (lambda d: d["secrets"].update(lower_case={"source": "literal", "value": "x"}), "valid secret name"),
        (lambda d: d["secrets"]["REVIEW_APP_ID"].update(source="vault"), "source must be one of"),
        (lambda d: d["secrets"]["REVIEW_APP_ID"].update(extra=1), "unknown key 'extra'"),
        (lambda d: d["secrets"]["REVIEW_APP_ID"].update(value=""), "non-empty"),
        (lambda d: d["secrets"]["REVIEW_APP_ID"].update(value="abc"), "numeric literal"),
        (lambda d: d["secrets"]["REVIEW_APP_PRIVATE_KEY"].update(app_id_secret="NOPE"), "numeric literal"),
        (lambda d: d["secrets"]["REVIEW_APP_PRIVATE_KEY"].pop("pem_dir"), "needs pem_dir"),
        (lambda d: d["secrets"]["REVIEW_APP_PRIVATE_KEY"].update(max_age_days=0), "positive"),
        (lambda d: d["secrets"]["TS_OAUTH_CI_SECRET"].update(field="key"), "id or secret"),
        (lambda d: d["secrets"]["TS_OAUTH_CI_SECRET"].update(field="id"), "more than once"),
        (lambda d: d["secrets"]["TYPESAFE_API_KEY"].update(rotator="auto"), "must be owner or typesafe"),
        (lambda d: d["secrets"]["TYPESAFE_API_KEY"].update(rotator="manual"), "no longer names a rotator"),
        (
            lambda d: d["secrets"].update(PLAIN={"source": "store", "mirror_keychain": "X"}),
            "applies only to rotator: typesafe",
        ),
        (lambda d: d["secrets"]["OPENAI_API_KEY"].pop("project"), "needs project"),
        (lambda d: d["secrets"].update(SECOND_URL={"source": "gateway_url"}), "more than once"),
        (lambda d: d.pop("tailnet"), "infra.tailnet is required"),
        (lambda d: d.pop("gateway"), "infra.gateway is required"),
        (lambda d: d.update(secrets={}), "at least one"),
    ],
)
def test_an_invalid_section_is_rejected_with_the_reason(tmp_path: Path, mutate: Any, expect: str) -> None:
    assert expect in _bad(tmp_path, mutate)


def test_a_literal_may_be_a_yaml_number(tmp_path: Path) -> None:
    data = deepcopy(EXAMPLE)
    data["secrets"]["REVIEW_APP_ID"]["value"] = 123456
    assert parse_infra(data, tmp_path / "review.yml").secret("REVIEW_APP_ID").value == "123456"


def test_the_policy_parser_ignores_the_infra_section_entirely(tmp_path: Path) -> None:
    from conftest import ALPHA_POLICY
    from theseus_reviewer._vendor import yaml
    from theseus_reviewer.policy import parse_policy

    raw = yaml.safe_load(ALPHA_POLICY.read_text(encoding="utf-8"))
    without = parse_policy(raw)
    with_infra = parse_policy({**raw, "infra": EXAMPLE})
    garbage = parse_policy({**raw, "infra": {"not": ["even", "valid"]}})
    assert with_infra == without == garbage
