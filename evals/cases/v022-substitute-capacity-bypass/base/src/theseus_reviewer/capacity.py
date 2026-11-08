from __future__ import annotations


def skip_text(load_tokens: int, limit: int) -> str:
    return f"skipped: the change needs {load_tokens} tokens and this reviewer accepts {limit}"


def unknown_text(error: str) -> str:
    return f"skipped: the reviewer's capacity is unknown ({error or 'not recorded'})"
