from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

STATE_FILE = "state.json"


class GitHubError(Exception):
    pass


def _gh(*arguments: str) -> str:
    result = subprocess.run(["gh", "api", *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GitHubError(result.stderr.strip()[:300])
    return result.stdout


def get_json(path: str) -> Any:
    return json.loads(_gh(path))


def latest_state_run(repo: str, pull: int) -> str | None:
    listing = get_json(f"repos/{repo}/actions/artifacts?name=review-state-{pull}")
    runs = [item["workflow_run"]["id"] for item in listing["artifacts"] if not item["expired"]]
    return str(max(runs)) if runs else None


def download_state(repo: str, run_id: str, directory: Path) -> dict[str, Any] | None:
    result = subprocess.run(
        ["gh", "run", "download", run_id, "--repo", repo, "-n", "review-state", "-D", str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return json.loads((directory / STATE_FILE).read_text(encoding="utf-8"))
