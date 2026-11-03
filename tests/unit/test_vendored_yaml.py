from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from theseus_reviewer import _vendor
from theseus_reviewer._vendor import yaml

pytestmark = [pytest.mark.unit, pytest.mark.fast]

VENDORED = Path(_vendor.__file__).parent / "yaml"
SDIST_SHA256 = "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
FILES_DIGEST = "860b044385d0be1402fc5d4288d8bd482c5ee11889c2d9c282e81dc5976a093f"


def test_the_vendored_files_are_the_recorded_pyyaml_6_0_3_sources():
    lines = "".join(
        f"{path.name}\t{hashlib.sha256(path.read_bytes()).hexdigest()}\n"
        for path in sorted(VENDORED.iterdir())
        if path.is_file()
    )
    assert hashlib.sha256(lines.encode()).hexdigest() == FILES_DIGEST
    assert yaml.__version__ == "6.0.3"
    assert len(SDIST_SHA256) == 64


def test_the_vendored_license_travels_with_the_code():
    text = (VENDORED / "LICENSE").read_text(encoding="utf-8")
    assert "Kirill Simonov" in text and "Permission is hereby granted" in text


def test_no_native_extension_or_libyaml_binding_is_vendored():
    assert yaml.__with_libyaml__ is False
    assert not (VENDORED / "cyaml.py").exists()
    assert not list(VENDORED.glob("*.so")) and not list(VENDORED.glob("_yaml*"))


def test_safe_load_and_safe_dump_round_trip():
    data = {"a": [1, 2, {"b": "c"}], "d": None, "e": True}
    assert yaml.safe_load(yaml.safe_dump(data)) == data
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load("a: [unclosed")
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load("!!python/object/apply:os.system ['true']")


def test_the_package_never_imports_an_ambient_yaml(tmp_path: Path):
    ambient = tmp_path / "ambient" / "yaml"
    ambient.mkdir(parents=True)
    marker = tmp_path / "ambient-yaml-imported"
    (ambient / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8")
    package_parent = Path(_vendor.__file__).resolve().parents[2]
    script = (
        "import sys\n"
        f"sys.path[:0] = [{str(tmp_path / 'ambient')!r}, {str(package_parent)!r}]\n"
        "from theseus_reviewer import policy, gate, omp_bump_gate, cli\n"
        "policy.load_policy\n"
        "print('yaml' in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
    assert not marker.exists()
