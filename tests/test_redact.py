import json

from dynatrace_mcp.redact import Redactor, fit_output, truncate_strings

from conftest import TOKEN


def test_masks_common_secrets():
    r = Redactor()
    cases = {
        "login failed password=hunter2 for user bob": "hunter2",
        'cfg {"apiKey": "abc123xyz"}': "abc123xyz",
        "Authorization: Bearer eyJabc.def.ghi1234": "eyJabc",
        "token dt0c01.ABCDEFGHIJKL.MNOPQRSTUVWXYZ123 leaked": "dt0c01.ABCDEFGHIJKL",
        "jdbc url postgres://app:s3cr3t@db:5432/x": "s3cr3t",
        "aws AKIAABCDEFGHIJKLMNOP": "AKIAABCDEFGHIJKLMNOP",
        "client_secret: 'zzz-top'": "zzz-top",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----": "MIIabc",
    }
    for text, secret in cases.items():
        out = r.text(text)
        assert secret not in out, out
        assert "***" in out


def test_leaves_normal_text_alone():
    r = Redactor()
    text = "GET /checkout returned 500 after 1200 ms (tokenizer ok)"
    assert r.text(text) == text


def test_configured_token_masked_even_when_disabled():
    r = Redactor([TOKEN], enabled=False)
    assert TOKEN not in r.text(f"echo {TOKEN}")
    assert r.text("password=x") == "password=x"


def test_data_walks_nested_structures():
    r = Redactor()
    out = r.data({"a": ["password=1", {"b": "secret: two"}], "n": 5})
    assert out == {"a": ["password=***", {"b": "secret: ***"}], "n": 5}


def test_truncate_strings():
    assert truncate_strings({"x": "a" * 50}, 10)["x"].startswith("a" * 10 + "... [truncated 40")


def test_fit_output_halves_biggest_list():
    data = {"results": [{"content": "x" * 100} for _ in range(500)], "count": 500}
    out = fit_output(data, 6000)
    assert len(json.dumps(out)) <= 6500
    assert len(out["results"]) < 500
    assert out["_truncated"]
    small = {"a": 1}
    assert fit_output(small, 6000) is small
