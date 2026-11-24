from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from theseus_reviewer import github, policy

from .conftest import POLICY_PATH

pytestmark = [pytest.mark.unit, pytest.mark.fast]

REPO = "o/r"
LOGIN = "acme-review"
NOTE = "Classification: TypeSafe answered 402; classified by OpenAI Decisions gpt-6-luna: size small."
WHERE = "#7 at aaaaaaaaaaaa"


class FakeIssues:
    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.writes: list[tuple[str, str, dict[str, str]]] = []

    def run(self, argv: list[str], *, token: str, timeout: float) -> subprocess.CompletedProcess[str]:
        assert token == "issues-token"
        if "--paginate" in argv:
            listed = "".join(json.dumps(issue) + "\n" for issue in self.issues)
            return subprocess.CompletedProcess(argv, 0, listed, "")
        method, target = argv[argv.index("--method") + 1], argv[argv.index("--method") + 2]
        fields = dict(argv[index + 1].split("=", 1) for index, item in enumerate(argv) if item == "-f")
        self.writes.append((method, target, fields))
        return subprocess.CompletedProcess(argv, 0, "{}", "")


def _issue(
    number: int, *, title: str = github.CLASSIFIER_ISSUE_TITLE, login: str = f"{LOGIN}[bot]", pull: bool = False
) -> dict[str, Any]:
    return {"number": number, "title": title, "login": login, "pull": pull}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeIssues:
    fake = FakeIssues()
    monkeypatch.setattr(github, "run_gh", fake.run)
    return fake


def test_a_degraded_review_opens_one_issue_naming_the_review_and_the_note(api: FakeIssues) -> None:
    github.sync_classifier_issue(REPO, LOGIN, NOTE, WHERE, "issues-token")

    assert [(method, target) for method, target, _ in api.writes] == [("POST", f"repos/{REPO}/issues")]
    fields = api.writes[0][2]
    assert fields["title"] == github.CLASSIFIER_ISSUE_TITLE
    assert WHERE in fields["body"] and NOTE in fields["body"]


def test_a_degraded_review_with_the_issue_open_edits_it_instead_of_opening_another(api: FakeIssues) -> None:
    api.issues = [_issue(3, title="Something else"), _issue(4, login="someone"), _issue(5, pull=True), _issue(9)]

    github.sync_classifier_issue(REPO, LOGIN, NOTE, WHERE, "issues-token")

    assert [(method, target, set(fields)) for method, target, fields in api.writes] == [
        ("PATCH", f"repos/{REPO}/issues/9", {"body"})
    ]


def test_a_normal_review_comments_on_and_closes_the_open_issue(api: FakeIssues) -> None:
    api.issues = [_issue(9)]

    github.sync_classifier_issue(REPO, LOGIN, "", WHERE, "issues-token")

    assert [(method, target) for method, target, _ in api.writes] == [
        ("POST", f"repos/{REPO}/issues/9/comments"),
        ("PATCH", f"repos/{REPO}/issues/9"),
    ]
    assert WHERE in api.writes[0][2]["body"] and api.writes[1][2] == {"state": "closed"}


def test_a_normal_review_with_no_open_issue_writes_nothing(api: FakeIssues) -> None:
    api.issues = [_issue(4, login="someone")]

    github.sync_classifier_issue(REPO, LOGIN, "", WHERE, "issues-token")

    assert api.writes == []


def test_classifier_issue_defaults_to_off_and_accepts_only_a_boolean(tmp_path: Any) -> None:
    text = POLICY_PATH.read_text(encoding="utf-8")
    assert policy.load_policy(POLICY_PATH).classifier_issue is False
    target = tmp_path / "policy.yml"
    target.write_text(text + "\nclassifier_issue: true\n", encoding="utf-8")
    assert policy.load_policy(target).classifier_issue is True
    target.write_text(text + "\nclassifier_issue: 'yes'\n", encoding="utf-8")
    with pytest.raises(policy.PolicyError, match="classifier_issue must be a boolean"):
        policy.load_policy(target)
