"""Stand-ins for system tools used by integration tests (sudo, nginx, supervisorctl,
systemctl, certbot, crontab, apt-get, apt-cache, dpkg-query, uv).

The tool name comes from argv[1] (the wrapper passes it). Each call is logged to
$FAKE_TOOL_LOG. $FAKE_FAIL is a comma list of tool names (or "tool:arg") that fail.
"""

import json
import os
import subprocess
import sys

tool, args = sys.argv[1], sys.argv[2:]
with open(os.environ["FAKE_TOOL_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": tool, "argv": args}) + "\n")
fail = set(filter(None, os.environ.get("FAKE_FAIL", "").split(",")))
if tool in fail or any(f"{tool}:{a}" in fail for a in args):
    print(f"fake {tool}: failing as requested", file=sys.stderr)
    sys.exit(1)

if tool == "sudo":
    # Run the command unprivileged (tests point every /etc path into a sysroot).
    sys.exit(subprocess.run(args).returncode)
if tool == "crontab":
    path = os.environ["FAKE_CRONTAB"]
    if args == ["-l"]:
        if not os.path.exists(path):
            print("no crontab for tester", file=sys.stderr)
            sys.exit(1)
        sys.stdout.write(open(path).read())
    elif args == ["-"]:
        open(path, "w").write(sys.stdin.read())
    sys.exit(0)
if tool == "dpkg-query":
    installed = set(os.environ.get("FAKE_DPKG_INSTALLED", "").split(","))
    if args[-1] in installed:
        print("install ok installed", end="")
        sys.exit(0)
    sys.exit(1)
if tool == "apt-cache":
    print(f"mariadb-server:\n  Installed: (none)\n  Candidate: {os.environ.get('FAKE_APT_MARIADB', '1:11.8.2-1')}\n")
    sys.exit(0)
if tool == "uv" and args[:2] == ["python", "find"]:
    py = os.environ.get("FAKE_UV_PYTHON")
    if py:
        print(py)
        sys.exit(0)
    sys.exit(2)
if tool == "supervisorctl" and args[-1:] == ["status"]:
    print(os.environ.get("FAKE_SUPERVISOR_STATUS", ""))
    sys.exit(0)
sys.exit(0)
