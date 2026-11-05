from __future__ import annotations

import ast

import pytest

from theseus_reviewer.sqlglot_relocation import RelocationError, relocate

pytestmark = [pytest.mark.unit, pytest.mark.fast]

NAMESPACE = "benchbox._vendor.sqlglot"


def test_from_imports_move_into_the_namespace_without_touching_relative_or_foreign_imports() -> None:
    source = (
        "from sqlglot import exp\nfrom sqlglot.dialects import Dialect\nfrom . import helper\nfrom other import x\n"
    )
    expected = (
        f"from {NAMESPACE} import exp\nfrom {NAMESPACE}.dialects import Dialect\n"
        "from . import helper\nfrom other import x\n"
    )
    assert relocate(source, "mod.py", NAMESPACE) == expected


def test_plain_imports_keep_the_upstream_name_bound() -> None:
    assert relocate("import sqlglot\n", "mod.py", NAMESPACE) == f"import {NAMESPACE} as sqlglot\n"
    assert relocate("import sqlglot.expressions\n", "mod.py", NAMESPACE) == (
        f"import {NAMESPACE}.expressions; import {NAMESPACE} as sqlglot\n"
    )
    assert relocate("import sqlglot.expressions as e\n", "mod.py", NAMESPACE) == (
        f"import {NAMESPACE}.expressions as e\n"
    )


def test_doctest_imports_in_docstrings_are_relocated() -> None:
    source = 'def f():\n    """\n    >>> import sqlglot\n    >>> from sqlglot import exp\n    """\n'
    expected = (
        f"def f():\n    '\\n    >>> import {NAMESPACE} as sqlglot\\n    >>> from {NAMESPACE} import exp\\n    '\n"
    )
    assert relocate(source, "mod.py", NAMESPACE) == expected


def test_reviewed_dynamic_import_strings_are_relocated_only_at_their_reviewed_paths() -> None:
    source = 'NAME = "sqlglot.dialects"\n'
    assert relocate(source, "dialects/dialect.py", NAMESPACE) == f"NAME = '{NAMESPACE}.dialects'\n"
    with pytest.raises(RelocationError, match="Unreviewed namespace string"):
        relocate(source, "other.py", NAMESPACE)


def test_logger_names_and_retained_strings_are_left_alone() -> None:
    assert relocate('import logging\nlogger = logging.getLogger("sqlglot")\n', "mod.py", NAMESPACE) == (
        'import logging\nlogger = logging.getLogger("sqlglot")\n'
    )
    assert relocate('KEY = "sqlglot.meta"\n', "expressions/core.py", NAMESPACE) == 'KEY = "sqlglot.meta"\n'


def test_an_unreviewed_dynamic_fstring_import_is_refused() -> None:
    with pytest.raises(RelocationError, match="Unreviewed dynamic import string"):
        relocate('def f(name):\n    return __import__(f"sqlglot.{name}")\n', "mod.py", NAMESPACE)


@pytest.mark.parametrize("source", ["def broken(:\n", "x = '\n"])
def test_source_that_does_not_parse_is_a_relocation_error(source: str) -> None:
    with pytest.raises(RelocationError, match="cannot be relocated"):
        relocate(source, "mod.py", NAMESPACE)


def test_relocation_parses_but_never_executes_the_source() -> None:
    marker = "raise SystemExit('executed')\n"
    assert relocate(marker, "mod.py", NAMESPACE) == marker


def test_an_encoding_cookie_cannot_hide_executable_code_in_a_reviewed_comment() -> None:
    source = "# coding: utf-7\n# +AAo-INJECTED = True\n"
    assert ast.parse(source).body == []
    runtime: dict[str, object] = {}
    exec(compile(source.encode(), "encoded_fixture.py", "exec"), runtime)
    assert runtime["INJECTED"] is True
    with pytest.raises(RelocationError, match="must use UTF-8"):
        relocate(source, "mod.py", NAMESPACE)


@pytest.mark.parametrize("source", ["# coding: latin-1\nVALUE = 1\n", "\ufeffimport sqlglot\n"])
def test_non_utf8_encodings_and_bom_are_refused_before_relocation(source: str) -> None:
    with pytest.raises(RelocationError, match="must use UTF-8"):
        relocate(source, "mod.py", NAMESPACE)
