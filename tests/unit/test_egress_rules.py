import pytest

from sca.sandbox.egress import render_squid_conf, squid_domains


def test_allowlist_translation():
    assert squid_domains(["PyPI.org.", "*.chatgpt.com", "chatgpt.com", "a.b.c"]) == [
        ".chatgpt.com",
        "a.b.c",
        "pypi.org",
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


@pytest.mark.parametrize("bad", ["1.1.1.1", "127.0.0.1", "::1", "*.1.1.1.1"])
def test_ip_allowlist_rejected(bad):
    with pytest.raises(ValueError):
        squid_domains([bad])


def test_tls_checks_sni_then_bumps_and_rechecks_http():
    conf = render_squid_conf(["pypi.org"])
    assert "ssl-bump cert=/ca/ca.crt key=/ca/ca.key" in conf
    assert "%ssl::>sni" in conf
    assert conf.index("ssl_bump peek step1") < conf.index("ssl_bump terminate !has_sni")
    assert conf.index("ssl_bump terminate !has_sni") < conf.index("ssl_bump bump allowed_sni")
    assert "ssl_bump terminate all" in conf
    assert "splice" not in conf
    assert "acl allowed dstdomain -n pypi.org" in conf
    assert "http_access deny ip_literal" in conf
    assert "sslproxy_cert_error deny all" in conf
    assert "access_log none" in conf


async def test_ca_is_persistent_and_only_bundle_is_public(tmp_path):
    from sca.sandbox.egress import prepare_ca

    directory = tmp_path / "ca"
    await prepare_ca(directory)
    key = (directory / "ca.key").read_bytes()
    cert = (directory / "ca.crt").read_bytes()
    await prepare_ca(directory)
    assert (directory / "ca.key").read_bytes() == key
    assert (directory / "ca.crt").read_bytes() == cert
    assert cert in (directory / "ca-bundle.crt").read_bytes()
    assert b"PRIVATE KEY" not in (directory / "ca-bundle.crt").read_bytes()
    assert (directory / "ca.key").stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700


async def test_docker_only_mounts_public_bundle_into_sandbox(tmp_path):
    from sca.core.models import SandboxSpec
    from sca.sandbox.docker import DockerSandboxProvider
    from sca.sandbox.egress import CA_ENV, CA_TARGET

    class FakeDocker(DockerSandboxProvider):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.calls: list[tuple[str, ...]] = []

        async def _cli(self, *args, check=True):
            self.calls.append(args)
            if args[0] == "inspect":
                if "IPAddress" in args[2]:
                    return 0, "172.22.0.2", ""
                return 1, "", "No such container"
            return 0, "", ""

    provider = FakeDocker(
        egress_ca_dir=tmp_path / "ca", egress_allowlist=["pypi.org"], name_prefix="egress-unit"
    )
    await provider.create("egress-unit", SandboxSpec(image="fake"))
    runs = [call for call in provider.calls if call[0] == "run"]
    assert len(runs) == 2
    proxy, sandbox = runs
    assert f"{provider.egress_ca_dir}:/ca:ro" in proxy
    assert f"{provider.egress_ca_dir / 'ca-bundle.crt'}:{CA_TARGET}:ro" in sandbox
    assert all("ca.key" not in arg and ":/ca:" not in arg for arg in sandbox)
    for key in CA_ENV:
        assert f"{key}={CA_TARGET}" in sandbox


def test_k8s_restricted_egress_uses_tls_proxy():
    from sca.sandbox.k8s import egress_manifests

    deployment = next(m for m in egress_manifests(["pypi.org"]) if m["kind"] == "Deployment")
    command = deployment["spec"]["template"]["spec"]["containers"][0]["command"]
    assert "exec squid -N" in command[-1]
    assert "exit 1" not in command[-1]


def test_raw_sni_helper_rejects_missing_ip_and_malformed_sni():
    import subprocess
    import sys
    from pathlib import Path

    helper = Path(__file__).resolve().parents[2] / "deploy/egress/check-sni"
    result = subprocess.run(
        [sys.executable, str(helper)],
        input="pypi.org -\n- -\n1.1.1.1 -\n::1 -\n%20 -\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert result.stdout.splitlines() == ["OK", "ERR", "ERR", "ERR", "ERR"]
