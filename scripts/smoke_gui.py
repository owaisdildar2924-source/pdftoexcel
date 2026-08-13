"""Build the real app window off-screen, exercise it, then close it.

This runs on the Windows CI runner, which has a display. It catches the
class of bug I cannot see from a headless machine: a window that fails to
construct, a button wired to a missing method, a theme asset missing from
the bundle.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gui  # noqa: E402

work = tempfile.mkdtemp()

app = gui.App()
app.withdraw()                     # build it, but keep it off-screen

app.workdir = work
app.folder_var.set(work)
app.prepare_folders()
for sub in ("input", "output", "processed", "failed"):
    assert os.path.isdir(os.path.join(work, sub)), sub
print("folder scaffolding OK")

# the two new buttons must be wired to something that actually works
opened = []
gui.open_in_file_manager = lambda p: opened.append(p)
app.btn_input.invoke()
app.btn_output.invoke()
assert opened == [os.path.join(work, "input"),
                  os.path.join(work, "output")], opened
print("open input / open output buttons OK")

# results rendering must not raise, and must colour the lines
class O:
    def __init__(s, state, source, detail):
        s.state, s.source, s.detail = state, source, detail

app._results([O("proven", "a.pdf", "Balances agree."),
              O("unproven", "b.pdf", "Off by 150.25."),
              O("failed", "c.pdf", "Not readable.")])
text = app.log._textbox.get("1.0", "end")
assert "PROVEN" in text and "NOT PROVEN" in text and "FAILED" in text
assert "19" not in text
print("results log OK")

app._set_mode("Dark")
app._set_mode("Light")
print("light/dark toggle OK")

app.update()
app.destroy()
print("SMOKE TEST PASSED - the window builds, renders and closes cleanly")
