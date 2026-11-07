import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALLERS = {
    "review-verdict.yml": "review.yml",
    "review-gate.yml": "gate.yml",
}


def test_theseus_pin_is_one_commit():
    lock = json.loads((ROOT / ".github/theseus-lock.json").read_text(encoding="utf-8"))
    assert lock["workflow_sha"] == lock["source_commit"]
    for name, callee in CALLERS.items():
        text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        uses = re.findall(rf"joeharris76/theseus/\.github/workflows/{re.escape(callee)}@([0-9a-f]{{40}})\b", text)
        refs = re.findall(r"^\s*theseus_ref:\s*([0-9a-f]{40})\s*$", text, re.MULTILINE)
        assert uses == refs == [lock["workflow_sha"]], name
