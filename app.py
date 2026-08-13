"""PDF to Excel — desktop app entry point.

No terminal window (packaged with PyInstaller --noconsole), no network,
nothing to install. `--cli <workdir>` runs headless (used by CI).
"""
from __future__ import annotations

import os
import sys


def run_cli(workdir: str) -> int:
    """Headless mode used by the CI end-to-end check: run the packaged
    executable, as a user would, on real PDFs, and let the caller verify
    the spreadsheet figure by figure."""
    from bankconv.pipeline import process_folder
    outcomes = process_folder(workdir)
    report = []
    worst = 0
    for o in outcomes:
        report.append(f"{o.state.upper()}\t{o.source}\t{o.detail}")
        worst = max(worst, {"proven": 0, "unproven": 1, "failed": 2}[o.state])
    if not outcomes:
        # finding nothing to do is not success; a caller checking only the
        # exit code must never read an empty run as "everything proven"
        report.append("NOTHING\t(no PDF files were found in the input folder)")
        worst = 3
    out = os.path.join(workdir, "output", "_logs", "cli_result.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")
    return worst


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--cli":
        raise SystemExit(run_cli(sys.argv[2]))
    # the GUI is imported lazily so headless environments can run --cli
    from gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
