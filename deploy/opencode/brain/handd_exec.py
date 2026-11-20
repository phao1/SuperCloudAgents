"""Connect an OpenCode ChildProcess handle to one job on the worker's handd relay."""
import base64
import json
import os
import socket
import sys
import threading
from pathlib import Path

CHUNK = 32 * 1024
MAX_FRAME = 16 * 1024 * 1024


def send(connection: socket.socket, frame: dict) -> None:
    packet = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
    if len(packet) > MAX_FRAME:
        raise ValueError("handd request exceeds frame limit")
    connection.sendall(packet)


def forward_stdin(connection: socket.socket) -> None:
    try:
        while data := os.read(0, CHUNK):
            send(connection, {"data": base64.b64encode(data).decode()})
        # A PTY has one bidirectional descriptor. Send the default terminal EOF
        # character without closing the socket that carries replies.
        send(connection, {"data": "BA=="})
    except OSError:
        pass


def run(connection: socket.socket, operation: str, args: list[str]) -> int:
    with connection.makefile("rb") as replies:
        if operation == "probe":
            send(connection, {"op": "probe"})
            line = replies.readline(MAX_FRAME)
            return 0 if line and json.loads(line).get("ready") is True else 1

        options = json.loads(args[0])
        stdin = b""
        if options["buffer_stdin"]:
            limit = MAX_FRAME * 3 // 4
            stdin = sys.stdin.buffer.read(limit + 1)
            if len(stdin) > limit:
                raise ValueError("handd stdin exceeds frame limit")
        send(connection, {"op": "exec", "argv": args[1:], "cwd": options["cwd"],
                          "env": options["env"], "pty": options["pty"],
                          "stdin": base64.b64encode(stdin).decode()})
        if options["pty"]:
            threading.Thread(target=forward_stdin, args=(connection,), daemon=True).start()
        while line := replies.readline(MAX_FRAME):
            reply = json.loads(line)
            for name, stream in (("stdout", sys.stdout.buffer), ("stderr", sys.stderr.buffer)):
                if name in reply:
                    stream.write(base64.b64decode(reply[name], validate=True))
                    stream.flush()
            if "exit" in reply:
                if reply.get("truncated") is True:
                    sys.stderr.buffer.write(b"\n[handd terminal output truncated]\n")
                    sys.stderr.buffer.flush()
                code = reply["exit"]
                return 128 - code if code < 0 else code
    sys.stderr.buffer.write(b"[handd connection closed; command outcome unknown]\n")
    return 125


def main(path: Path, operation: str, args: list[str]) -> int:
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                connection.connect(f"/proc/self/fd/{fd}/{path.name}")
            finally:
                os.close(fd)
            return run(connection, operation, args)
    except (KeyError, OSError, TypeError, ValueError):
        return 125


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3:]))
