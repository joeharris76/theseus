from __future__ import annotations

import pytest

from theseus_reviewer.infra.transport import (
    HttpResponse,
    Redactor,
    TransportError,
    UrllibTransport,
    bearer,
    run_command,
)

pytestmark = [pytest.mark.unit, pytest.mark.fast]


def test_the_real_transport_refuses_a_plain_http_url() -> None:
    with pytest.raises(TransportError, match="non-https"):
        UrllibTransport().request("GET", "http://example.invalid/x?token=SENTINEL-query-0001")


def test_a_refused_url_does_not_echo_its_query() -> None:
    with pytest.raises(TransportError) as caught:
        UrllibTransport().request("GET", "http://example.invalid/x?token=SENTINEL-query-0001")
    assert "SENTINEL" not in str(caught.value)


def test_a_command_gets_its_stdin_and_reports_its_status() -> None:
    result = run_command(["cat"], stdin="from-stdin")
    assert (result.returncode, result.stdout) == (0, "from-stdin")
    assert run_command(["false"]).returncode == 1


def test_a_missing_command_is_a_transport_error() -> None:
    with pytest.raises(TransportError, match="cannot run"):
        run_command(["definitely-not-a-command-xyz"])


def test_a_response_parses_json_or_says_it_is_not() -> None:
    assert HttpResponse(200, {}, b'{"a": 1}').json() == {"a": 1}
    assert HttpResponse(204).ok and not HttpResponse(404).ok
    with pytest.raises(TransportError, match="HTTP 502"):
        HttpResponse(502, {}, b"<html>").json()


def test_the_redactor_hides_every_value_it_knows_and_ignores_short_ones() -> None:
    redactor = Redactor(["SENTINEL-one-1111", "short"])
    redactor.add("SENTINEL-two-2222")
    shown = redactor.scrub("a SENTINEL-one-1111 b SENTINEL-two-2222 short")
    assert shown == "a [redacted] b [redacted] short"
    assert redactor.values == ("SENTINEL-one-1111", "SENTINEL-two-2222")


def test_a_bearer_header_carries_extras() -> None:
    assert bearer("tok", Accept="application/json") == {"Authorization": "Bearer tok", "Accept": "application/json"}
