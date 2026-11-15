import io
import tarfile

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
    assert (tmp_path / "program").stat().st_mode & 0o111
