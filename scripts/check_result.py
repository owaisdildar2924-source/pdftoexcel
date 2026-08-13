"""Assert what the packaged app actually reported, by reading the result
file it writes.

Exit codes are unreliable here: the shipped executable is a windowed app
with no console, and PowerShell does not wait for windowed apps, so
$LASTEXITCODE says nothing useful about them. The app's own written
output is unambiguous on every platform, so the CI checks read that.

Usage:  python scripts/check_result.py <workdir> PROVEN|UNPROVEN|FAILED
"""
import os
import sys
import time

workdir, expected = sys.argv[1], sys.argv[2].upper()
path = os.path.join(workdir, "output", "_logs", "cli_result.txt")

# the app may still be finishing; wait a bounded time for its result
deadline = time.time() + 120
while time.time() < deadline:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        break
    time.sleep(1)
else:
    sys.exit(f"FAIL: the app produced no result file at {path} within "
             f"120 seconds. It may not have run at all.")

with open(path, encoding="utf-8") as fh:
    lines = [ln for ln in fh.read().splitlines() if ln.strip()]

if not lines:
    sys.exit("FAIL: the app's result file is empty.")

print("App reported:")
for ln in lines:
    print("   ", ln)

states = {ln.split("\t", 1)[0] for ln in lines}

if "NOTHING" in states:
    sys.exit("FAIL: the app found no PDF files to convert. The check "
             "proves nothing in this state.")

if expected == "PROVEN":
    if states != {"PROVEN"}:
        sys.exit(f"FAIL: expected every file to be PROVEN, got {states}.")
    print("OK: the clean statement was reported as proven.")
else:
    if expected not in states:
        sys.exit(
            f"FAIL: expected at least one {expected} result, got {states}. "
            f"A statement with a removed row was NOT caught — the "
            f"reconciliation safety net is not working.")
    print(f"OK: the broken statement was correctly reported as {expected}.")
