"""Outbound-call safety: no redirects with API keys, and failure classification."""

from __future__ import annotations

import ssl

import httpx
import pytest
from pydantic import SecretStr

from tactidose.integrations import netsafe as ns


def test_gemini_http_options_turn_redirects_off():
    from google.genai import types

    opts = ns.gemini_http_options(types, 1500)
    assert opts.client_args == {"follow_redirects": False} and opts.timeout == 1500
    assert ns.gemini_http_options(types).timeout is None


def test_sdk_client_built_with_the_options_does_not_follow_redirects():
    from google import genai
    from google.genai import types

    client = genai.Client(api_key="test-key-not-real", http_options=ns.gemini_http_options(types, 1000))
    try:
        assert client._api_client._httpx_client.follow_redirects is False
    finally:
        client.close()


@pytest.mark.parametrize("secret,expected", [
    (None, "not set"), ("", "not set"), ("short", "set (5 chars)"),
    ("AIzaSyExampleExampleExample123", "set (30 chars, ends ...123)"),
    (SecretStr("sk_0123456789abcdefXYZ"), "set (22 chars, ends ...XYZ)"),
])
def test_redact_never_shows_more_than_the_last_three_characters(secret, expected):
    assert ns.redact(secret) == expected


def test_redirect_host_drops_path_and_query():
    assert ns.redirect_host("https://sso.example.com/home/bookmark/1?user=alex") == "sso.example.com"
    assert ns.redirect_host(None) == "another site"


@pytest.mark.parametrize("status,body,code", [
    (307, "", ns.BLOCKED),
    (400, '{"error": {"message": "API key not valid. Please pass a valid API key."}}', ns.INVALID_KEY),
    (400, "User location is not supported for the API use.", ns.PERMISSION),
    (400, "Invalid JSON payload", ns.BAD_REQUEST),
    (401, '{"detail": {"status": "invalid_api_key"}}', ns.INVALID_KEY),
    (401, '{"detail": {"status": "detected_unusual_activity", "message": "Unusual activity detected."}}', ns.PERMISSION),
    (402, "", ns.QUOTA),
    (403, "Method doesn't allow unregistered callers", ns.PERMISSION),
    (404, "models/gemini-9 is not found", ns.NOT_FOUND),
    (422, "", ns.BAD_REQUEST),
    (429, "RESOURCE_EXHAUSTED", ns.QUOTA),
    (503, "", ns.SERVER_ERROR),
    (418, "", ns.ERROR),
])
def test_classify_status(status, body, code):
    assert ns.classify_status(status, body).code == code


def test_blocked_message_names_only_the_host():
    f = ns.classify_status(307, location="https://sso.example.com/home/bookmark/1?user=alex")
    assert f.code == ns.BLOCKED and "sso.example.com" in f.message and "alex" not in f.message


def test_classify_response_reads_the_location_header():
    resp = httpx.Response(307, headers={"location": "https://block.example.net/deny?u=1"})
    f = ns.classify_response(resp)
    assert f.code == ns.BLOCKED and "block.example.net" in f.message


def test_classify_exception_kinds():
    req = httpx.Request("POST", "https://api.example.com")
    assert ns.classify_exception(httpx.ReadTimeout("slow", request=req)).code == ns.TIMEOUT
    assert ns.classify_exception(httpx.ConnectError("refused", request=req)).code == ns.NETWORK_ERROR
    tls = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=req)
    assert ns.classify_exception(tls).code == ns.TLS_ERROR
    try:
        raise RuntimeError("wrapped") from ssl.SSLCertVerificationError("unable to get local issuer certificate")
    except RuntimeError as exc:
        assert ns.classify_exception(exc).code == ns.TLS_ERROR
    assert ns.classify_exception(ValueError("odd")).code == ns.ERROR


def test_classify_exception_reads_sdk_api_errors():
    from google.genai import errors

    assert ns.classify_exception(errors.APIError(307, {"message": "", "status": "Temporary Redirect"})).code == ns.BLOCKED
    assert ns.classify_exception(errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})).code == ns.QUOTA
    assert ns.classify_exception(errors.ClientError(404, {"error": {"message": "not found", "status": "NOT_FOUND"}})).code == ns.NOT_FOUND


@pytest.mark.parametrize("body,code", [
    ('{"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}', ns.QUOTA),
    ('{"detail": {"status": "missing_permissions", "message": "The API key is missing text_to_speech."}}', ns.PERMISSION),
])
def test_elevenlabs_401_variants(body, code):
    assert ns.classify_status(401, body).code == code


def test_sdk_redirect_error_names_the_redirect_host():
    from google.genai import errors

    resp = httpx.Response(307, headers={"location": "https://sso.example.com/bookmark?user=alex"})
    f = ns.classify_exception(errors.APIError(307, {"message": "", "status": "Temporary Redirect"}, resp))
    assert f.code == ns.BLOCKED and "sso.example.com" in f.message and "alex" not in f.message
