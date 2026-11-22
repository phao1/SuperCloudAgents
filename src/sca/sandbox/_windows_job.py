"""Run a command in a Windows Job Object that also owns its descendants."""

import subprocess
import sys


def main() -> int:
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, "")
    kind = win32job.JobObjectExtendedLimitInformation
    limits = win32job.QueryInformationJobObject(job, kind)
    limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, kind, limits)
    win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    # Keep the handle open until the OS exits this process with the command's status.
    job.Detach()
    return subprocess.call(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
