"""Opt-in real proxy checks; never touch the deployment's network or container."""
import os
import uuid

import pytest

from sca.sandbox.docker import DockerSandboxProvider
from sca.sandbox.egress import CA_TARGET

pytestmark = [pytest.mark.docker, pytest.mark.integration,
              pytest.mark.skipif(os.getenv("SCA_TEST_EGRESS") != "1", reason="opt-in Docker test")]

PROBE = r'''
import socket, ssl, sys, time
proxy = sys.argv[1]

def request(target, sni=None, host=None, verify=False):
    with socket.create_connection((proxy, 3128), timeout=20) as sock:
        sock.sendall(('CONNECT '+target+':443 HTTP/1.1\r\nHost: '+target+':443\r\n\r\n').encode())
        response = b''
        while b'\r\n\r\n' not in response:
            part = sock.recv(4096)
            if not part:
                return None
            response += part
        if b' 200 ' not in response.split(b'\r\n')[0]:
            return int(response.split()[1])
        context = ssl.create_default_context(cafile='/etc/sca-egress/ca-bundle.crt')
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if sni is None:
            context.check_hostname = False
        try:
            with context.wrap_socket(sock, server_hostname=sni) as tls:
                path = '/nix-cache-info' if (host or sni) == 'cache.nixos.org' else '/simple/'
                tls.sendall(('GET '+path+' HTTP/1.1\r\nHost: '+(host or sni)+'\r\nConnection: close\r\n\r\n').encode())
                response = tls.recv(4096)
                return int(response.split()[1]) if response else None
        except (OSError, ssl.SSLError):
            return None

for attempt in range(30):
    try:
        if request('pypi.org', 'pypi.org', verify=True) == 200:
            break
    except OSError:
        pass
    time.sleep(1)
else:
    raise AssertionError('allowed HTTPS failed')
assert request('cache.nixos.org', 'cache.nixos.org') in (None, 403, 409)
assert request('pypi.org', 'cache.nixos.org') in (None, 403, 409)
# 503: Squid refuses because the origin certificate does not cover the Host header.
assert request('pypi.org', 'pypi.org', 'cache.nixos.org') in (None, 403, 409, 503)
assert request('1.1.1.1', 'pypi.org') in (None, 403, 409)
assert request('pypi.org', None, 'pypi.org') in (None, 403, 409)
'''


async def test_real_tls_egress(tmp_path):
    prefix = 'sca-egress-test-' + uuid.uuid4().hex[:12]
    provider = DockerSandboxProvider(name_prefix=prefix, egress_ca_dir=tmp_path / 'ca',
                                    egress_allowlist=['pypi.org', 'files.pythonhosted.org'])
    try:
        proxy = await provider.ensure_egress()
        address = proxy.removeprefix('http://').split(':')[0]
        args = ['run', '--rm', '--network', provider.network_name,
                '--name', prefix + '-probe', '--memory', '128m', '--cpus', '0.5',
                '--volume', f'{provider.egress_ca_dir / "ca-bundle.crt"}:{CA_TARGET}:ro']
        await provider._cli(*args, 'sca-sandbox:latest', 'python3', '-c', PROBE, address)
        await provider._cli(*args, '--env', f'HTTPS_PROXY={proxy}', '--env', 'NO_PROXY=',
                            '--env', f'PIP_CERT={CA_TARGET}', 'sca-sandbox:latest',
                            'python3', '-m', 'pip', 'download', '--no-deps', '--no-cache-dir',
                            '--index-url', 'https://pypi.org/simple', '--dest', '/tmp/wheels',
                            'six==1.17.0')
    finally:
        await provider._cli('rm', '-f', prefix + '-probe', check=False)
        await provider._cli('rm', '-f', provider.proxy_name, check=False)
        await provider._cli('network', 'rm', provider.network_name, check=False)
