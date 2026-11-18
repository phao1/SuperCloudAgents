from pathlib import Path

from sca.agents.opencode_build import perform_build, reserve_build
from sca.config import HOST_ARCH, Settings
from sca.workspace.layers import command


async def test_shared_build_objects_and_readonly_mount(tmp_path: Path):
    source = tmp_path / "source"
    await command("git", "init", str(source))
    (source / "file").write_text("source")
    await command("git", "-C", str(source), "add", ".")
    await command("git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=t@t",
                  "commit", "-m", "initial")
    bare = tmp_path / "source.git"
    await command("git", "clone", "--bare", str(source), str(bare))
    cfg = Settings(data_dir=tmp_path / "data", opencode_repo_url=str(bare))
    (cfg.opencode_src_cache / "node_modules").mkdir(parents=True)
    record = reserve_build(cfg, "build", "tenant", ref="HEAD")
    invoked = False

    async def runner(argv: list[str], log: Path) -> str:
        nonlocal invoked
        if argv[0] == "git":
            if argv[1] == "clone":
                assert "--shared" in argv
            return await command(*argv)
        invoked = True
        assert f"{bare}:{bare}:ro" in argv
        checkout = cfg.data_dir / "opencode/build-work/build/src"
        assert (checkout / ".git/objects/info/alternates").read_text().strip() == str(bare / "objects")
        assert not list((checkout / ".git/objects/pack").iterdir())
        binary = checkout / f"packages/cli/dist/cli-linux-{HOST_ARCH}/bin/opencode"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fake build output")
        return ""

    result = await perform_build(cfg, record, runner)
    assert invoked and result["status"] == "succeeded"
    assert not (cfg.data_dir / "opencode/build-work/build").exists()
    assert (source / "file").read_text() == "source"
