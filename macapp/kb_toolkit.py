#!/usr/bin/env python3
"""
kb_toolkit.py - A small macOS/Tkinter GUI front-end for the two
*receive-only* KillerBee helper tools in this repo:

  - Device discovery (zbid / killerbee.devlist) - list attached
    CC1354P10 / CC1352P7 boards and their serial ports.
  - RSSI scan (tools/rssi_scan.py) - passive ambient RF-energy
    monitoring across pages 0 (2.4GHz), 28 and 31 (sub-1GHz).

Both are receive-only: the GUI never transmits. It deliberately does NOT
wrap the jamming tools (subg_jam_hop.py / jam24_rotate.py) - those remain
command-line-only, for supervised, authorized use. See this directory's
README.md for why.

It does not reimplement anything - it shells out to the existing tools with
the same Python interpreter that launched it, so behavior matches the CLI
exactly. Tkinter ships with Python, so this runs with no extra GUI
dependency; see README.md for turning it into a double-clickable .app.
"""

import os
import queue
import subprocess
import sys
import threading

import tkinter as tk
from tkinter import ttk, scrolledtext

# Repo root = parent of this macapp/ directory, so the bundled tools and the
# killerbee package are importable/runnable without an install.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO_ROOT, "tools")


def _tool_env():
    env = dict(os.environ)
    # Same trick the tools' own sys.path manipulation assumes - lets the
    # bundled killerbee package import without a pip install.
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env


class KBToolkit(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KillerBee Toolkit (receive-only)")
        self.geometry("760x560")
        self.minsize(640, 460)

        self.proc = None            # running rssi_scan subprocess, if any
        self.out_queue = queue.Queue()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_devices_tab(nb)
        self._build_rssi_tab(nb)

        self.after(100, self._drain_output)

    # ---------------- Devices tab (zbid) ----------------
    def _build_devices_tab(self, nb):
        frame = ttk.Frame(nb)
        nb.add(frame, text="Devices")

        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(8, 4), padx=8)
        ttk.Button(bar, text="Refresh device list", command=self.refresh_devices).pack(side="left")
        ttk.Label(bar, text="(attached CC1354P10 / CC1352P7 boards)").pack(side="left", padx=8)

        self.dev_text = scrolledtext.ScrolledText(frame, height=18, wrap="none")
        self.dev_text.pack(fill="both", expand=True, padx=8, pady=8)

    def refresh_devices(self):
        self.dev_text.delete("1.0", "end")
        self.dev_text.insert("end", "Scanning for devices...\n")
        self.update_idletasks()
        try:
            out = subprocess.run(
                [sys.executable, os.path.join(TOOLS, "zbid")],
                cwd=REPO_ROOT, env=_tool_env(),
                capture_output=True, text=True, timeout=30,
            )
            text = out.stdout + (("\n" + out.stderr) if out.stderr else "")
        except Exception as e:
            text = "Error running zbid: %s" % e
        self.dev_text.delete("1.0", "end")
        self.dev_text.insert("end", text or "(no output)\n")
        self._refresh_device_dropdown()

    # ---------------- RSSI scan tab ----------------
    def _build_rssi_tab(self, nb):
        frame = ttk.Frame(nb)
        nb.add(frame, text="RSSI Scan")

        form = ttk.Frame(frame)
        form.pack(fill="x", padx=8, pady=8)

        ttk.Label(form, text="Device:").grid(row=0, column=0, sticky="w")
        self.iface_var = tk.StringVar()
        self.iface_combo = ttk.Combobox(form, textvariable=self.iface_var, width=34)
        self.iface_combo.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        ttk.Button(form, text="Detect", command=self._refresh_device_dropdown).grid(row=0, column=2, padx=4)

        ttk.Label(form, text="Board:").grid(row=1, column=0, sticky="w")
        self.devtype_var = tk.StringVar(value="cc1354p10")
        ttk.Combobox(form, textvariable=self.devtype_var,
                     values=["cc1354p10", "cc1352p7"], width=12, state="readonly"
                     ).grid(row=1, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(form, text="Pages:").grid(row=2, column=0, sticky="w")
        self.pages_var = tk.StringVar(value="28,31,0")
        ttk.Entry(form, textvariable=self.pages_var, width=16).grid(row=2, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(form, text="Channels (blank = all):").grid(row=3, column=0, sticky="w")
        self.chan_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.chan_var, width=16).grid(row=3, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(form, text="Dwell (s):").grid(row=4, column=0, sticky="w")
        self.dwell_var = tk.StringVar(value="0.5")
        ttk.Entry(form, textvariable=self.dwell_var, width=8).grid(row=4, column=1, sticky="w", padx=4, pady=2)

        btns = ttk.Frame(frame)
        btns.pack(fill="x", padx=8)
        self.start_btn = ttk.Button(btns, text="Start scan", command=self.start_scan)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        self.scan_text = scrolledtext.ScrolledText(frame, height=16, wrap="none")
        self.scan_text.pack(fill="both", expand=True, padx=8, pady=8)

    def _refresh_device_dropdown(self):
        """Populate the device dropdown from killerbee.devlist()."""
        devs = []
        try:
            sys.path.insert(0, REPO_ROOT)
            from killerbee import kbutils  # type: ignore
            for row in kbutils.devlist():
                devs.append(str(row[0]))
        except Exception:
            devs = []
        self.iface_combo["values"] = devs
        if devs and not self.iface_var.get():
            self.iface_var.set(devs[0])

    def start_scan(self):
        if self.proc is not None:
            return
        iface = self.iface_var.get().strip()
        if not iface:
            self._append(self.scan_text, "Pick a device first (Detect / Devices tab).\n")
            return
        cmd = [sys.executable, os.path.join(TOOLS, "rssi_scan.py"),
               "-i", iface, "-d", self.devtype_var.get(),
               "-p", self.pages_var.get().strip() or "28,31,0",
               "-t", self.dwell_var.get().strip() or "0.5"]
        chans = self.chan_var.get().strip()
        if chans:
            cmd += ["-c", chans]

        self.scan_text.delete("1.0", "end")
        self._append(self.scan_text, "$ " + " ".join(cmd) + "\n\n")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=REPO_ROOT, env=_tool_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except Exception as e:
            self._append(self.scan_text, "Failed to start: %s\n" % e)
            self.proc = None
            return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def stop_scan(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                self.out_queue.put(line)
        except Exception:
            pass
        rc = proc.wait()
        self.out_queue.put("\n[scan ended, exit %s]\n" % rc)
        self.out_queue.put(None)  # sentinel: process done

    def _drain_output(self):
        try:
            while True:
                item = self.out_queue.get_nowait()
                if item is None:
                    self.proc = None
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    continue
                self._append(self.scan_text, item)
        except queue.Empty:
            pass
        self.after(100, self._drain_output)

    @staticmethod
    def _append(widget, text):
        widget.insert("end", text)
        widget.see("end")


if __name__ == "__main__":
    KBToolkit().mainloop()
