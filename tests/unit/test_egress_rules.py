import pytest

from sca.sandbox.egress import render_squid_conf, squid_domains


def test_allowlist_translation():
    assert squid_domains(["PyPI.org.", "*.chatgpt.com", "chatgpt.com", "a.b.c"]) == [
        ".chatgpt.com", "a.b.c", "pypi.org",
    ]


@pytest.mark.parametrize("bad", ["a b", "x.com\nhttp_access allow all", "*", "http://x.com", ""])
def test_entries_that_could_inject_configuration_are_rejected(bad):
    with pytest.raises(ValueError):
        squid_domains([bad])


def test_config_denies_by_default_and_blocks_private_ranges_first():
    conf = render_squid_conf(["pypi.org"])
    lines = conf.splitlines()
    assert lines.index("http_access deny private") < lines.index("http_access allow allowed")
    assert lines.index("http_access allow allowed") < lines.index("http_access deny all")
    assert "169.254.0.0/16" in conf
    assert "http_access allow" not in render_squid_conf([])
