import io
import sys
import tarfile
from types import SimpleNamespace

import pytest

from sca.workspace.tarsafe import extract


@pytest.mark.parametrize(
    "name,kind,link",
    [
        ("/absolute", tarfile.REGTYPE, ""),
        ("../outside", tarfile.REGTYPE, ""),
        ("dir/../../outside", tarfile.REGTYPE, ""),
        ("link", tarfile.SYMTYPE, "../outside"),
        ("link", tarfile.SYMTYPE, "/outside"),
        ("hard", tarfile.LNKTYPE, "../outside"),
        ("device", tarfile.CHRTYPE, ""),
        ("block", tarfile.BLKTYPE, ""),
        ("fifo", tarfile.FIFOTYPE, ""),
    ],
)
def test_rejects_untrusted_members(tmp_path, name, kind, link):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = link
        tar.addfile(member)
    data.seek(0)
    with pytest.raises(tarfile.FilterError):
        extract(data, tmp_path / "target")
    assert not (tmp_path / "outside").exists()


def test_safe_links_and_executable(tmp_path):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        member = tarfile.TarInfo("program")
        member.size = 3
        member.mode = 0o755
        tar.addfile(member, io.BytesIO(b"\x00\xff\n"))
        link = tarfile.TarInfo("link")
        link.type, link.linkname = tarfile.SYMTYPE, "program"
        tar.addfile(link)
    data.seek(0)
    extract(data, tmp_path)
    assert (tmp_path / "link").read_bytes() == b"\x00\xff\n"
    if sys.platform != "win32":
        assert (tmp_path / "program").stat().st_mode & 0o111
    else:
        assert not (tmp_path / "link").is_symlink()


def test_windows_forward_link_copy_respects_expanded_budget(tmp_path, monkeypatch):
    from sca.workspace import tarsafe

    monkeypatch.setattr(tarsafe, "sys", SimpleNamespace(platform="win32"))
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        link = tarfile.TarInfo("alias")
        link.type = tarfile.SYMTYPE
        link.linkname = "data"
        tar.addfile(link)
        source = tarfile.TarInfo("data")
        source.size = 3
        tar.addfile(source, io.BytesIO(b"xyz"))
    data.seek(0)
    with pytest.raises(ValueError, match="expanded size"):
        extract(data, tmp_path / "small", max_bytes=5)
    data.seek(0)
    target = tmp_path / "complete"
    extract(data, target, max_bytes=6)
    assert (target / "alias").read_bytes() == b"xyz"
    assert not (target / "alias").is_symlink()
