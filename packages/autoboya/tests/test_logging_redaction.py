from autoboya.logging import redact


def test_redact_sensitive_values():
    text = "Authtoken abc123 password=secret CASTGC=TGT-1 user [22312345]"
    redacted = redact(text)

    assert "secret" not in redacted
    assert "TGT-1" not in redacted
    assert "22312345" not in redacted


def test_http_log_filter_redacts_query_credentials():
    import logging
    from autoboya.logging import RedactingFilter

    record = logging.LogRecord("httpx", logging.INFO, "", 0,
                               'HTTP Request: GET %s "HTTP/1.1 200 OK"',
                               ("https://example.test/cas?token=private-token&ticket=private-ticket",), None)
    assert RedactingFilter().filter(record)
    assert "private-token" not in record.getMessage()
    assert "private-ticket" not in record.getMessage()


def test_json_token_is_redacted():
    assert "private-token" not in redact('{"token": "private-token"}')
