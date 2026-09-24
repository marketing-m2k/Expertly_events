"""Desktop GUI for the event scraper.

Runs the scrape in a background thread inside THIS process — once you launch
it, it keeps running independent of any terminal/chat session, until you
close the window or click Stop. Same scraping logic as main.py (dedup,
future-only filtering, link verification, resume-where-it-left-off).

Run with: python gui.py
"""

import json
import os
import threading
import tkinter as tk
from tkinter import ttk

import main as scraper_main

PROGRESS_PATH = scraper_main.PROGRESS_PATH
STOP_FLAG_PATH = scraper_main.STOP_FLAG_PATH

SOURCE = "Sources/Event_scrapper_-_Website_completed.xlsx"
SOURCE_SHEET = "India"
OUTPUT = "output/raw/Events.xlsx"
SHEET = "Events"
FAILURES_LOG = "output/failures/failures.csv"
ENGINE = "free"

BG = "#0f1117"
PANEL = "#1a1d29"
FG = "#e6e6e6"


class ScraperGUI:
    def __init__(self, root):
        self.root = root
        root.title("Expertly Event Scraper")
        root.geometry("780x580")
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.worker_thread = None
        self._build_ui()
        self._poll()

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text="Expertly Event Scraper", fg=FG, bg=BG,
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        self.status_label = tk.Label(header, text="idle", fg="#999", bg=BG, font=("Segoe UI", 11))
        self.status_label.pack(side="right")

        controls = tk.Frame(self.root, bg=BG)
        controls.pack(fill="x", padx=16, pady=6)
        self.start_btn = tk.Button(controls, text="▶ Start / Resume", command=self.start,
                                    bg="#22c55e", fg="#06240f", font=("Segoe UI", 10, "bold"),
                                    relief="flat", padx=16, pady=6, activebackground="#16a34a")
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = tk.Button(controls, text="■ Stop", command=self.stop,
                                   bg="#ef4444", fg="#2a0a0a", font=("Segoe UI", 10, "bold"),
                                   relief="flat", padx=16, pady=6, state="disabled", activebackground="#dc2626")
        self.stop_btn.pack(side="left")
        self.msg_label = tk.Label(controls, text="", fg="#888", bg=BG, font=("Segoe UI", 9))
        self.msg_label.pack(side="left", padx=12)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TProgressbar", troughcolor=PANEL, background="#4f8cff", thickness=10)
        self.progress = ttk.Progressbar(self.root, style="TProgressbar", mode="determinate")
        self.progress.pack(fill="x", padx=16, pady=(4, 10))

        cards = tk.Frame(self.root, bg=BG)
        cards.pack(fill="x", padx=16, pady=(0, 10))
        self.card_vars = {}
        for key, label in [("progress", "Progress"), ("events_found", "Found"),
                            ("events_added", "Added"), ("links_fixed", "Links Fixed"),
                            ("failures", "Failures")]:
            f = tk.Frame(cards, bg=PANEL, padx=10, pady=8)
            f.pack(side="left", expand=True, fill="x", padx=4)
            tk.Label(f, text=label.upper(), fg="#888", bg=PANEL, font=("Segoe UI", 8)).pack(anchor="w")
            var = tk.StringVar(value="0")
            tk.Label(f, textvariable=var, fg=FG, bg=PANEL, font=("Segoe UI", 14, "bold")).pack(anchor="w")
            self.card_vars[key] = var

        self.current_label = tk.Label(self.root, text="Currently scraping: —", fg="#ccc", bg=BG,
                                       font=("Segoe UI", 10), anchor="w", wraplength=740, justify="left")
        self.current_label.pack(fill="x", padx=16, pady=(0, 8))

        log_frame = tk.Frame(self.root, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        tk.Label(log_frame, text="LOG", fg="#888", bg=BG, font=("Segoe UI", 8)).pack(anchor="w")
        self.log_text = tk.Text(log_frame, bg=PANEL, fg="#bbb", font=("Consolas", 9), relief="flat", wrap="none")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("fail", foreground="#f87171")
        self.log_text.configure(state="disabled")

    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.msg_label.config(text="starting...")

        def work():
            scraper_main.run(SOURCE, OUTPUT, SHEET, 0, 0, FAILURES_LOG, ENGINE, resume=True,
                              source_sheet=SOURCE_SHEET)

        self.worker_thread = threading.Thread(target=work, daemon=True)
        self.worker_thread.start()

    def stop(self):
        os.makedirs(os.path.dirname(STOP_FLAG_PATH), exist_ok=True)
        with open(STOP_FLAG_PATH, "w") as f:
            f.write("stop")
        self.msg_label.config(text="stopping after current site...")

    def _on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop()
        self.root.destroy()

    def _poll(self):
        running = self.worker_thread is not None and self.worker_thread.is_alive()
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        if not running and self.msg_label.cget("text").startswith("stopping"):
            self.msg_label.config(text="")

        try:
            with open(PROGRESS_PATH, encoding="utf-8") as f:
                s = json.load(f)
            self.status_label.config(text=s.get("status", "idle"))
            total = s.get("total_sites", 0) or 1
            processed = s.get("processed", 0)
            self.progress["maximum"] = total
            self.progress["value"] = processed
            self.card_vars["progress"].set(f"{processed} / {total}")
            self.card_vars["events_found"].set(str(s.get("events_found", 0)))
            self.card_vars["events_added"].set(str(s.get("events_added", 0)))
            self.card_vars["links_fixed"].set(str(s.get("links_fixed", 0)))
            self.card_vars["failures"].set(str(s.get("failures", 0)))
            self.current_label.config(
                text=f"Currently scraping: {s.get('current_organizer') or '—'}  "
                     f"{s.get('current_url') or ''}"
            )

            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            for line in (s.get("recent_log") or [])[:40]:
                tag = ("fail",) if "FAILED" in line else ()
                self.log_text.insert("end", line + "\n", tag)
            self.log_text.configure(state="disabled")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        self.root.after(1000, self._poll)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs("output", exist_ok=True)
    root = tk.Tk()
    ScraperGUI(root)
    root.mainloop()
