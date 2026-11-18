"""Run a tiny host beside read-only workspace dependencies without copying them."""
import fcntl
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

source, bun = Path(sys.argv[1]).resolve(), sys.argv[2]
with tempfile.TemporaryDirectory(prefix="sca-brain-") as temporary:
    root = Path(temporary)
    modules = root / "node_modules"
    modules.mkdir()
    for package in ("sdk", "core", "cli"):
        for entry in (source / "packages" / package / "node_modules").iterdir():
            if entry.name.startswith("@"):
                scope = modules / entry.name
                scope.mkdir(exist_ok=True)
                for dependency in entry.iterdir():
                    target = scope / dependency.name
                    if not target.exists():
                        target.symlink_to(dependency.resolve())
            elif not (modules / entry.name).exists():
                (modules / entry.name).symlink_to(entry.resolve())
    for entry in Path(__file__).parent.glob("*.ts"):
        shutil.copyfile(entry, root / entry.name)
    os.environ["SCA_OPENCODE_SOURCE"] = str(source)
    # Keep the parent alive so temporary dependency links survive the child.
    import subprocess

    arguments = (["test", str(root / "driver.test.ts")] if sys.argv[3:] == ["--test"]
                 else [str(root / "server.ts"), *sys.argv[4:]] if sys.argv[3:4] == ["--server"]
                 else [str(root / "host.ts"), *sys.argv[3:]])
    if sys.argv[3:4] == ["--server"]:
        directory = Path(sys.argv[4])
        with (directory / "server.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (directory / "brain.sock").unlink(missing_ok=True)
            child = subprocess.Popen([bun, *arguments], pass_fds=(lock.fileno(),))
            signal.signal(signal.SIGTERM, lambda *_: child.terminate())
            signal.signal(signal.SIGINT, lambda *_: child.terminate())
            sys.exit(child.wait())
    sys.exit(subprocess.call([bun, *arguments]))
