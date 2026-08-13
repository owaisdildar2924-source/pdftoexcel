"""GUI for PDF to Excel (CustomTkinter).

Layout: single column. Folder bar, two folder shortcuts, one primary
action, then the run log. No summary tiles — the outcome sentence and the
colour-coded log carry the detail.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk

import customtkinter as ctk

APP_NAME = "PDF to Excel"
SETTINGS = os.path.join(os.path.expanduser("~"), ".pdf2excel_settings.json")

BLUE = "#185FA5"
BLUE_HOVER = "#0C447C"
GREEN = "#1D7A4A"
AMBER = "#A8690B"
RED = "#B02318"
MUTED = "#6E6E6A"


def load_settings() -> dict:
    try:
        with open(SETTINGS, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_settings(d: dict) -> None:
    try:
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    except Exception:
        pass


def open_in_file_manager(path: str) -> None:
    """Open a folder in the OS file manager."""
    os.makedirs(path, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)            # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("720x620")
        self.minsize(640, 560)

        icon = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
            "packaging", "app.ico")
        if os.path.exists(icon):
            try:
                self.iconbitmap(icon)
            except Exception:
                pass

        self.workdir: str = load_settings().get("workdir", "")
        self.q: queue.Queue = queue.Queue()
        self.running = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        pad = {"padx": 22}

        # ---- header ----------------------------------------------------
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(18, 4), **pad)
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=APP_NAME,
                     font=ctk.CTkFont(size=19, weight="bold")
                     ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header,
                     text="Bank statements and settlement reports to Excel",
                     text_color=MUTED, font=ctk.CTkFont(size=12)
                     ).grid(row=1, column=0, sticky="w", pady=(1, 0))
        self.mode = ctk.CTkSegmentedButton(
            header, values=["Light", "Dark"], width=130, height=26,
            font=ctk.CTkFont(size=11), command=self._set_mode)
        self.mode.set("Light")
        self.mode.grid(row=0, column=1, rowspan=2, sticky="e")

        # ---- folder bar -------------------------------------------------
        bar = ctk.CTkFrame(self, corner_radius=10)
        bar.grid(row=1, column=0, sticky="ew", pady=(14, 10), **pad)
        bar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bar, text="\U0001F4C1", font=ctk.CTkFont(size=17)
                     ).grid(row=0, column=0, rowspan=2, padx=(14, 10), pady=12)
        ctk.CTkLabel(bar, text="Working folder", text_color=MUTED,
                     font=ctk.CTkFont(size=11)
                     ).grid(row=0, column=1, sticky="sw", pady=(11, 0))
        self.folder_var = tk.StringVar(
            value=self.workdir or "No folder chosen yet")
        ctk.CTkLabel(bar, textvariable=self.folder_var,
                     font=ctk.CTkFont(size=13), anchor="w"
                     ).grid(row=1, column=1, sticky="nw", pady=(0, 11))
        ctk.CTkButton(bar, text="Change", width=88, height=30,
                      corner_radius=8, fg_color="transparent", border_width=1,
                      text_color=("#1F2937", "#E5E7EB"),
                      command=self.choose_folder
                      ).grid(row=0, column=2, rowspan=2, padx=14)

        # ---- folder shortcuts -------------------------------------------
        shortcuts = ctk.CTkFrame(self, fg_color="transparent")
        shortcuts.grid(row=2, column=0, sticky="ew", **pad)
        shortcuts.grid_columnconfigure((0, 1), weight=1, uniform="s")
        self.btn_input = ctk.CTkButton(
            shortcuts, text="Open input folder", height=36, corner_radius=8,
            fg_color="transparent", border_width=1,
            text_color=("#1F2937", "#E5E7EB"),
            command=lambda: self._open_sub("input"))
        self.btn_input.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.btn_output = ctk.CTkButton(
            shortcuts, text="Open output folder", height=36, corner_radius=8,
            fg_color="transparent", border_width=1,
            text_color=("#1F2937", "#E5E7EB"),
            command=lambda: self._open_sub("output"))
        self.btn_output.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        # ---- primary action ---------------------------------------------
        self.btn = ctk.CTkButton(
            self, text="Convert PDFs now", height=44, corner_radius=10,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=BLUE, hover_color=BLUE_HOVER, command=self.convert)
        self.btn.grid(row=3, column=0, sticky="ew", pady=(12, 6), **pad)

        self.progress = ctk.CTkProgressBar(self, height=4, corner_radius=4,
                                           progress_color=BLUE)
        self.progress.set(0)

        self.status = tk.StringVar(
            value="Drop PDF statements into the input folder, then convert.")
        self.status_lbl = ctk.CTkLabel(
            self, textvariable=self.status, anchor="w",
            font=ctk.CTkFont(size=12), text_color=MUTED)
        self.status_lbl.grid(row=5, column=0, sticky="ew", pady=(8, 4), **pad)

        # ---- log ---------------------------------------------------------
        self.log = ctk.CTkTextbox(self, corner_radius=10, wrap="word",
                                  font=ctk.CTkFont(family="Consolas", size=12))
        self.log.grid(row=4, column=0, sticky="nsew", pady=(6, 0), **pad)
        self.log.configure(state="disabled")
        self._tags()

        ctk.CTkLabel(self, text="Green means the figures reconcile. Amber "
                                "means they do not, and the reason is in the "
                                "spreadsheet.",
                     text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w"
                     ).grid(row=6, column=0, sticky="ew", pady=(0, 16), **pad)

        if self.workdir and os.path.isdir(self.workdir):
            self.prepare_folders()
        self.after(150, self.poll)

    # ------------------------------------------------------------------
    def _tags(self) -> None:
        t = self.log._textbox
        t.tag_config("proven", foreground=GREEN)
        t.tag_config("unproven", foreground=AMBER)
        t.tag_config("failed", foreground=RED)
        t.tag_config("detail", foreground=MUTED)

    def _set_mode(self, value: str) -> None:
        ctk.set_appearance_mode(value.lower())
        self._tags()

    def _open_sub(self, sub: str) -> None:
        if not self._have_folder():
            return
        open_in_file_manager(os.path.join(self.workdir, sub))

    def _have_folder(self) -> bool:
        if self.workdir and os.path.isdir(self.workdir):
            return True
        self.status.set("Choose a working folder first.")
        return False

    def choose_folder(self) -> None:
        d = ctk.filedialog.askdirectory(
            title="Choose the folder this tool should work in")
        if d:
            self.workdir = d
            self.folder_var.set(d)
            save_settings({"workdir": d})
            self.prepare_folders()
            self.status.set("Folder ready. Put PDFs in the input folder.")

    def prepare_folders(self) -> None:
        for sub in ("input", "output", "processed", "failed"):
            os.makedirs(os.path.join(self.workdir, sub), exist_ok=True)

    # ------------------------------------------------------------------
    def convert(self) -> None:
        if self.running or not self._have_folder():
            return
        self.running = True
        self.btn.configure(state="disabled", text="Converting…")
        self.progress.grid(row=3, column=0, sticky="ew", padx=22,
                           pady=(58, 0))
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self._log_clear()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            from bankconv.pipeline import process_folder
            outcomes = process_folder(
                self.workdir, progress=lambda s: self.q.put(("status", s)))
            self.q.put(("done", outcomes))
        except Exception as e:
            self.q.put(("error", str(e)))

    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                elif kind == "error":
                    self.status.set("Something went wrong.")
                    self._line(f"{payload}\n", "failed")
                    self._finish()
                elif kind == "done":
                    self._results(payload)
                    self._finish()
        except queue.Empty:
            pass
        self.after(150, self.poll)

    def _finish(self) -> None:
        self.running = False
        self.progress.stop()
        self.progress.grid_forget()
        self.btn.configure(state="normal", text="Convert PDFs now")

    def _results(self, outcomes) -> None:
        if not outcomes:
            self.status.set("No PDF files found in the input folder.")
            return
        p = sum(1 for o in outcomes if o.state == "proven")
        u = sum(1 for o in outcomes if o.state == "unproven")
        f = sum(1 for o in outcomes if o.state == "failed")
        self.status.set(
            f"Done. {p} proven, {u} converted but not proven, {f} failed. "
            f"Details below and in output/_logs.")
        for o in outcomes:
            head = {"proven": "PROVEN", "unproven": "NOT PROVEN",
                    "failed": "FAILED"}[o.state]
            self._line(f"{head}  {o.source}\n", o.state)
            self._line(f"    {o.detail}\n\n", "detail")

    def _log_clear(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _line(self, text: str, tag: str) -> None:
        self.log.configure(state="normal")
        self.log._textbox.insert("end", text, tag)
        self.log.configure(state="disabled")
        self.log.see("end")


def main() -> None:
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
