#!/usr/bin/env python3
"""ANZII Shift Helper — Jira shift monitor (single file)."""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox
from typing import Any, Literal

import customtkinter as ctk
import requests

APP_NAME = "ANZII Shift Helper"
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"
LOG_DIR = Path(__file__).resolve().parent / "logs"
DEFAULT_POLL = 300
DEFAULT_STATUS = "Investigate"
DEFAULT_COMMENT = "wip"

State = Literal["setup", "ready", "shift"]

# ── design tokens ───────────────────────────────────────────────────────────
C = {
    "bg": "#0c1218",
    "surface": "#151d27",
    "surface2": "#1c2733",
    "border": "#2a3d4d",
    "text": "#eef4f8",
    "muted": "#8aa0b2",
    "accent": "#0097a7",
    "accent_h": "#00b8cc",
    "ok": "#3ecf8e",
    "warn": "#e8b84a",
    "err": "#f07070",
    "danger": "#b83838",
    "danger_h": "#d64545",
}


# ── settings ────────────────────────────────────────────────────────────────
def load_settings() -> dict[str, str]:
    keys = (
        "jira_url", "email", "api_token", "project_key", "assignee", "poll_seconds",
        "assign_status", "assign_comment",
    )
    defaults = {k: "" for k in keys}
    defaults["poll_seconds"] = str(DEFAULT_POLL)
    defaults["assign_status"] = DEFAULT_STATUS
    defaults["assign_comment"] = DEFAULT_COMMENT
    if not SETTINGS_FILE.exists():
        return defaults
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return {**defaults, **{k: str(raw[k]) for k in keys if k in raw}}
    except (json.JSONDecodeError, OSError, KeyError):
        return defaults


def save_settings(data: dict[str, str]) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def settings_valid(s: dict[str, str] | None = None) -> bool:
    return not validate_settings(s or load_settings())


def validate_settings(s: dict[str, str]) -> list[str]:
    errs: list[str] = []
    for key, label in [
        ("jira_url", "Jira site URL"),
        ("email", "Email"),
        ("api_token", "API token"),
        ("project_key", "Project key"),
    ]:
        if not s.get(key, "").strip():
            errs.append(label)
    try:
        if int(s.get("poll_seconds") or DEFAULT_POLL) < 30:
            errs.append("Poll interval (min 30 sec)")
    except ValueError:
        errs.append("Poll interval")
    return errs


# ── file log ────────────────────────────────────────────────────────────────
class FileLogger:
    def __init__(self) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        self.path = LOG_DIR / f"shift_{day}.log"
        if not self.path.exists():
            self.path.write_text(f"# {APP_NAME} — {day}\n", encoding="utf-8")

    def write(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level.upper():5}] {message}\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)

    def write_ticket(self, key: str, action: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [TICKET] {key} | {action}\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)


# ── jira ────────────────────────────────────────────────────────────────────
def build_jql(project_key: str, *, one_unassigned: bool = False) -> str:
    base = f'project = "{project_key}" AND statusCategory != Done'
    if one_unassigned:
        return f"{base} AND assignee is EMPTY ORDER BY created ASC"
    return f"{base} ORDER BY updated DESC"


class Jira:
    def __init__(self, base_url: str, email: str, token: str) -> None:
        self._root = f"{base_url.rstrip('/')}/rest/api/3"
        self._s = requests.Session()
        self._s.auth = (email, token)
        self._s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = self._s.request(method, f"{self._root}{path}", timeout=30, **kw)
        if not r.ok:
            raise RuntimeError(f"Jira {r.status_code}: {r.text[:280]}")
        return None if r.status_code == 204 else r.json()

    def myself(self) -> tuple[str, str]:
        d = self._req("GET", "/myself")
        return d["accountId"], d.get("displayName", "You")

    def resolve_assignee(self, query: str) -> tuple[str, str]:
        if not query.strip() or query.lower() in ("me", "self"):
            return self.myself()
        users = self._req("GET", "/user/search", params={"query": query, "maxResults": 10})
        if not users:
            raise RuntimeError(f"No user found: {query}")
        u = next((x for x in users if x.get("emailAddress", "").lower() == query.lower()), users[0])
        return u["accountId"], u.get("displayName", query)

    def fetch_issues(self, jql: str, limit: int = 100) -> list[dict[str, Any]]:
        data = self._req(
            "POST",
            "/search/jql",
            json={"jql": jql, "maxResults": limit, "fields": ["summary", "status", "assignee"]},
        )
        return data.get("issues", [])

    def assign(self, key: str, account_id: str) -> None:
        self._req("PUT", f"/issue/{key}", json={"fields": {"assignee": {"accountId": account_id}}})

    def transition_to(self, key: str, status_name: str) -> None:
        data = self._req("GET", f"/issue/{key}/transitions")
        target = status_name.strip().lower()
        for t in data.get("transitions", []):
            names = (
                t.get("name", "").lower(),
                t.get("to", {}).get("name", "").lower(),
            )
            if target in names:
                self._req("POST", f"/issue/{key}/transitions", json={"transition": {"id": t["id"]}})
                return
        available = ", ".join(t.get("name", "?") for t in data.get("transitions", []))
        raise RuntimeError(f"No '{status_name}' transition (available: {available})")

    def add_comment(self, key: str, text: str) -> None:
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": text}],
                    }
                ],
            }
        }
        self._req("POST", f"/issue/{key}/comment", json=body)

    def claim_ticket(self, key: str, account_id: str, status_name: str, comment: str) -> list[tuple[str, str]]:
        """Assign, transition, comment. Returns (step, detail) for UI and file log."""
        steps: list[tuple[str, str]] = []
        self.assign(key, account_id)
        steps.append(("assign", "assigned to you"))
        try:
            self.transition_to(key, status_name)
            steps.append(("status", f"changed to {status_name}"))
        except Exception as exc:
            steps.append(("status", f"FAILED — {exc}"))
        try:
            self.add_comment(key, comment)
            steps.append(("comment", f'added "{comment}"'))
        except Exception as exc:
            steps.append(("comment", f"FAILED — {exc}"))
        return steps


def format_elapsed(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ── models ──────────────────────────────────────────────────────────────────
@dataclass
class TicketRow:
    key: str
    summary: str
    status: str
    assignee: str
    assigned_by_app: bool = False


@dataclass
class ShiftSession:
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    polls: int = 0
    tickets: list[TicketRow] = field(default_factory=list)
    assigned_keys: list[str] = field(default_factory=list)

    def summary(self) -> str:
        keys = ", ".join(self.assigned_keys) if self.assigned_keys else "None"
        return (
            f"Duration: {self._dur()}\n"
            f"Refreshes: {self.polls}\n"
            f"Tickets listed: {len(self.tickets)}\n"
            f"Assigned by you: {len(self.assigned_keys)}\n"
            f"Keys: {keys}"
        )

    def _dur(self) -> str:
        s = int((datetime.now(timezone.utc) - self.started).total_seconds())
        return f"{s // 60}m {s % 60}s"


# ── settings dialog ─────────────────────────────────────────────────────────
class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent: App, on_save: Any) -> None:
        super().__init__(parent)
        self._on_save = on_save
        self.title("Connection settings")
        self.geometry("500x580")
        self.minsize(460, 520)
        self.configure(fg_color=C["bg"])
        self.transient(parent)
        self.grab_set()

        # header (fixed top)
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=24, pady=(20, 8))
        ctk.CTkLabel(
            hdr, text="Jira connection",
            font=ctk.CTkFont(size=18, weight="bold"), text_color=C["text"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            hdr, text="Required before you can start a shift.",
            font=ctk.CTkFont(size=12), text_color=C["muted"],
        ).pack(anchor="w", pady=(4, 0))

        # footer with Save / Cancel (fixed bottom — always visible)
        footer = ctk.CTkFrame(self, fg_color=C["surface"], corner_radius=0)
        footer.pack(side="bottom", fill="x")

        self._err = ctk.CTkLabel(footer, text="", text_color=C["err"], wraplength=440, font=ctk.CTkFont(size=11))
        self._err.pack(anchor="w", padx=24, pady=(12, 0))

        btn_row = ctk.CTkFrame(footer, fg_color="transparent")
        btn_row.pack(fill="x", padx=24, pady=(8, 16))
        ctk.CTkButton(
            btn_row, text="Save settings", width=140, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=C["accent"], hover_color=C["accent_h"],
            command=self._save,
        ).pack(side="right")
        ctk.CTkButton(
            btn_row, text="Cancel", width=100, height=40,
            fg_color=C["surface2"], hover_color=C["border"],
            command=self.destroy,
        ).pack(side="right", padx=(0, 10))

        # scrollable form (middle)
        scroll = ctk.CTkScrollableFrame(
            self, fg_color=C["surface"], corner_radius=12,
            scrollbar_button_color=C["border"], scrollbar_button_hover_color=C["muted"],
        )
        scroll.pack(fill="both", expand=True, padx=24, pady=(0, 8))

        s = load_settings()
        self._entries: dict[str, ctk.CTkEntry] = {}
        fields = [
            ("jira_url", "Jira site URL", "https://company.atlassian.net", None),
            ("email", "Your email", "", None),
            ("api_token", "API token", "From id.atlassian.com → Security", None),
            ("project_key", "Project key", "e.g. ACS", None),
            ("assignee", "Assign to (optional)", "Leave blank = you", None),
            ("poll_seconds", "Refresh every (seconds)", "300", None),
            ("assign_status", "Status after assign", None, DEFAULT_STATUS),
            ("assign_comment", "Comment after assign", None, DEFAULT_COMMENT),
        ]
        for key, label, hint, placeholder in fields:
            block = ctk.CTkFrame(scroll, fg_color="transparent")
            block.pack(fill="x", padx=12, pady=10)
            ctk.CTkLabel(block, text=label, font=ctk.CTkFont(size=12, weight="bold"), text_color=C["text"]).pack(
                anchor="w"
            )
            if hint:
                ctk.CTkLabel(block, text=hint, font=ctk.CTkFont(size=11), text_color=C["muted"]).pack(anchor="w")
            e = ctk.CTkEntry(block, height=36, fg_color=C["surface2"], border_color=C["border"])
            e.pack(fill="x", pady=(4, 0))
            if placeholder:
                e.configure(placeholder_text=placeholder)
                saved = s.get(key, "").strip()
                if saved:
                    e.insert(0, saved)
            else:
                e.insert(0, s.get(key, ""))
            if key == "api_token":
                e.configure(show="•")
            self._entries[key] = e

    def _save(self) -> None:
        data = {k: e.get().strip() for k, e in self._entries.items()}
        if not data.get("assign_status"):
            data["assign_status"] = DEFAULT_STATUS
        if not data.get("assign_comment"):
            data["assign_comment"] = DEFAULT_COMMENT
        missing = validate_settings(data)
        if missing:
            self._err.configure(text="Missing: " + ", ".join(missing))
            return
        save_settings(data)
        self._on_save()
        self.destroy()


# ── main app ────────────────────────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self.geometry("880x620")
        self.minsize(820, 580)
        self.configure(fg_color=C["bg"])

        self._q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._file_log = FileLogger()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._state: State = "setup"
        self._running = False
        self._jira: Jira | None = None
        self._assignee_id = ""
        self._settings = load_settings()
        self._jql = ""
        self._poll_sec = DEFAULT_POLL
        self._session = ShiftSession()
        self._countdown = 0
        self._shift_start: datetime | None = None
        self._shift_clock_id: str | None = None
        self._empty_ticket_lbl: ctk.CTkLabel | None = None

        self._build()
        self._apply_state()
        self._process_q()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(400, self._first_run)

    def _first_run(self) -> None:
        if not settings_valid():
            self._open_settings()
        else:
            self._reload_settings()
            self._log("info", "Ready. Start a shift when you are on the floor.")

    def _build(self) -> None:
        # ── top bar
        top = ctk.CTkFrame(self, fg_color=C["surface"], corner_radius=0, height=64)
        top.pack(fill="x")
        top.pack_propagate(False)

        ctk.CTkLabel(
            top, text=APP_NAME,
            font=ctk.CTkFont(family="Bahnschrift SemiBold", size=20, weight="bold"),
            text_color=C["text"],
        ).pack(side="left", padx=20, pady=14)

        self._pill = ctk.CTkLabel(
            top, text="", width=120, height=30, corner_radius=15,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._pill.pack(side="right", padx=16, pady=14)

        ctk.CTkButton(
            top, text="Settings", width=88, height=32,
            fg_color=C["surface2"], hover_color=C["border"],
            command=self._open_settings,
        ).pack(side="right", padx=(0, 4), pady=14)

        # chips
        chip_row = ctk.CTkFrame(self, fg_color="transparent")
        chip_row.pack(fill="x", padx=20, pady=(14, 0))
        self._chip_row = chip_row
        self._chip_project = self._chip(chip_row, "Project", "—")
        self._chip_project.pack(side="left", padx=(0, 8))
        self._chip_poll = self._chip(chip_row, "Refresh", "—")
        self._chip_poll.pack(side="left", padx=(0, 8))
        self._chip_shift = self._chip(chip_row, "Shift time", "—")
        self._chip_shift.pack(side="left")

        # setup banner
        self._banner = ctk.CTkFrame(self, fg_color="#1a2a35", corner_radius=10, border_width=1, border_color=C["accent"])
        inner_b = ctk.CTkFrame(self._banner, fg_color="transparent")
        inner_b.pack(fill="x", padx=16, pady=12)
        ctk.CTkLabel(
            inner_b,
            text="Connect Jira to get started",
            font=ctk.CTkFont(size=14, weight="bold"), text_color=C["text"],
        ).pack(side="left")
        ctk.CTkButton(
            inner_b, text="Open settings", width=120, height=32,
            fg_color=C["accent"], hover_color=C["accent_h"], command=self._open_settings,
        ).pack(side="right")

        # options + actions
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.pack(fill="x", padx=20, pady=(14, 0))

        self._auto_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            ctrl,
            text="Auto-assign → Investigate + comment \"wip\" (see Settings)",
            variable=self._auto_var,
            font=ctk.CTkFont(size=13),
            fg_color=C["accent"], hover_color=C["accent_h"],
            border_color=C["border"], checkmark_color=C["bg"],
        ).pack(side="left")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(12, 0))

        self._btn_start = ctk.CTkButton(
            actions, text="Start shift", width=140, height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=C["accent"], hover_color=C["accent_h"],
            command=self._start_shift,
        )
        self._btn_start.pack(side="left", padx=(0, 8))

        self._btn_end = ctk.CTkButton(
            actions, text="End shift", width=110, height=42,
            fg_color=C["surface2"], hover_color=C["border"], text_color=C["muted"],
            state="disabled", command=self._end_shift,
        )
        self._btn_end.pack(side="left", padx=(0, 8))

        self._btn_refresh = ctk.CTkButton(
            actions, text="Refresh now", width=120, height=42,
            fg_color="transparent", border_width=1, border_color=C["border"],
            text_color=C["text"], hover_color=C["surface2"],
            state="disabled", command=self._refresh_now,
        )
        self._btn_refresh.pack(side="left")

        self._timer_lbl = ctk.CTkLabel(
            actions, text="", font=ctk.CTkFont(size=12), text_color=C["muted"],
        )
        self._timer_lbl.pack(side="right", padx=8)

        # tertiary test link
        test_row = ctk.CTkFrame(self, fg_color="transparent")
        test_row.pack(fill="x", padx=20, pady=(6, 0))
        self._btn_test = ctk.CTkButton(
            test_row, text="Run test: 1 ticket (assign + Investigate + wip)", height=28, width=280,
            font=ctk.CTkFont(size=12), fg_color="transparent",
            text_color=C["muted"], hover_color=C["surface2"],
            command=self._test_once,
        )
        self._btn_test.pack(side="left")

        # main panels — fixed height ratio, no window scroll
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=20, pady=(12, 8))
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        # activity
        log_wrap = ctk.CTkFrame(main, fg_color=C["surface"], corner_radius=12)
        log_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ctk.CTkLabel(
            log_wrap, text="Activity", font=ctk.CTkFont(size=13, weight="bold"), text_color=C["text"],
        ).pack(anchor="w", padx=14, pady=(12, 6))
        log_inner = ctk.CTkFrame(log_wrap, fg_color=C["surface2"], corner_radius=8)
        log_inner.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._log_text = tk.Text(
            log_inner, wrap="word", height=12, relief="flat", borderwidth=0,
            bg=C["surface2"], fg=C["text"], insertbackground=C["text"],
            font=("Segoe UI", 11), padx=10, pady=8, state="disabled",
        )
        self._log_text.pack(fill="both", expand=True)
        for tag, color in (("info", C["muted"]), ("ok", C["ok"]), ("warn", C["warn"]), ("err", C["err"])):
            self._log_text.tag_configure(tag, foreground=color)

        # tickets
        tick_wrap = ctk.CTkFrame(main, fg_color=C["surface"], corner_radius=12)
        tick_wrap.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ctk.CTkLabel(
            tick_wrap, text="Tickets", font=ctk.CTkFont(size=13, weight="bold"), text_color=C["text"],
        ).pack(anchor="w", padx=14, pady=(12, 6))
        self._ticket_frame = ctk.CTkFrame(tick_wrap, fg_color=C["surface2"], corner_radius=8)
        self._ticket_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._ticket_scroll = ctk.CTkScrollableFrame(
            self._ticket_frame, fg_color="transparent", scrollbar_button_color=C["border"],
        )
        self._ticket_scroll.pack(fill="both", expand=True)
        self._show_ticket_empty("Start a shift to load tickets from Jira.")

        # footer
        foot = ctk.CTkFrame(self, fg_color=C["surface"], corner_radius=0, height=32)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        self._foot_lbl = ctk.CTkLabel(
            foot,
            text=f"Log file: {self._file_log.path.name}",
            font=ctk.CTkFont(size=11), text_color=C["muted"],
        )
        self._foot_lbl.pack(side="left", padx=16, pady=6)

    def _chip(self, parent: ctk.CTkFrame, title: str, value: str) -> ctk.CTkFrame:
        f = ctk.CTkFrame(parent, fg_color=C["surface"], corner_radius=8, border_width=1, border_color=C["border"])
        ctk.CTkLabel(f, text=title.upper(), font=ctk.CTkFont(size=9, weight="bold"), text_color=C["muted"]).pack(
            anchor="w", padx=10, pady=(6, 0)
        )
        val = ctk.CTkLabel(f, text=value, font=ctk.CTkFont(size=13, weight="bold"), text_color=C["text"])
        val.pack(anchor="w", padx=10, pady=(0, 8))
        f._val = val  # type: ignore[attr-defined]
        return f

    def _set_chip(self, chip: ctk.CTkFrame, value: str, *, warn: bool = False) -> None:
        chip._val.configure(text=value, text_color=C["warn"] if warn else C["text"])  # type: ignore[attr-defined]

    # ── state ──
    def _apply_state(self) -> None:
        ok = settings_valid()
        self._state = "setup" if not ok else ("shift" if self._running else "ready")

        if self._state == "setup":
            self._banner.pack(fill="x", padx=20, pady=(12, 0), after=self._chip_row)
            self._pill.configure(text="Setup needed", fg_color="#2a3540", text_color=C["warn"])
            self._btn_start.configure(state="disabled")
            self._style_end_button(active=False)
            self._btn_refresh.configure(state="disabled")
            self._btn_test.configure(state="disabled")
        else:
            self._banner.pack_forget()
            if self._state == "shift":
                self._pill.configure(text="On shift", fg_color="#0d3d32", text_color=C["ok"])
                self._btn_start.configure(state="disabled")
                self._style_end_button(active=True)
                self._btn_refresh.configure(state="normal")
            else:
                self._pill.configure(text="Ready", fg_color="#1a3040", text_color=C["accent"])
                self._btn_start.configure(state="normal")
                self._style_end_button(active=False)
                self._btn_refresh.configure(state="disabled")
            self._btn_test.configure(state="normal")

    def _style_end_button(self, *, active: bool) -> None:
        if active:
            self._btn_end.configure(
                state="normal",
                fg_color=C["danger"],
                hover_color=C["danger_h"],
                text_color=C["text"],
                border_width=0,
            )
        else:
            self._btn_end.configure(
                state="disabled",
                fg_color=C["surface2"],
                hover_color=C["border"],
                text_color=C["muted"],
                border_width=0,
            )

    def _reload_settings(self) -> None:
        self._settings = load_settings()
        self._poll_sec = int(self._settings.get("poll_seconds") or DEFAULT_POLL)
        pk = self._settings.get("project_key", "") or "—"
        self._set_chip(self._chip_project, pk, warn=pk == "—")
        mins = max(1, self._poll_sec // 60)
        self._set_chip(self._chip_poll, f"every {mins} min")
        if pk != "—":
            self._jql = build_jql(pk)
        self._apply_state()

    def _open_settings(self) -> None:
        SettingsDialog(self, on_save=self._on_settings_saved)

    def _on_settings_saved(self) -> None:
        self._reload_settings()
        self._log("ok", "Settings saved.")
        self._show_ticket_empty("Start a shift to load tickets.")

    # ── logging ──
    def _log(self, level: str, msg: str) -> None:
        self._file_log.write(level, msg)
        self._log_text.configure(state="normal")
        tag = level if level in ("info", "ok", "warn", "err") else "info"
        self._log_text.insert("end", msg + "\n", tag)
        self._log_text.configure(state="disabled")
        self._log_text.see("end")

    def _qlog(self, level: str, msg: str) -> None:
        self._q.put(("log", (level, msg)))

    # ── jira connect ──
    def _connect(self) -> bool:
        if not settings_valid():
            self._log("warn", "Complete settings before connecting.")
            return False
        s = self._settings
        try:
            self._jira = Jira(s["jira_url"], s["email"], s["api_token"])
            self._assignee_id, name = self._jira.resolve_assignee(s.get("assignee", ""))
            self._jql = build_jql(s["project_key"])
            self._log("ok", f"Connected — assignee: {name}")
            return True
        except Exception as exc:
            self._log("err", f"Connection failed: {exc}")
            return False

    # ── shift ──
    def _start_shift(self) -> None:
        if self._running or not settings_valid():
            return
        if not self._connect():
            return
        self._session = ShiftSession()
        self._shift_start = datetime.now()
        self._stop.clear()
        self._running = True
        self._clear_tickets()
        self._show_ticket_empty("Fetching tickets…")
        self._apply_state()
        self._start_shift_clock()
        auto = "on" if self._auto_var.get() else "off"
        self._log("ok", f"Shift started (auto-assign {auto}).")
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._q.put(("fetch", False))
            self._countdown = self._poll_sec
            for remaining in range(self._poll_sec, -1, -1):
                if self._stop.is_set():
                    break
                self._q.put(("tick", remaining))
                self._stop.wait(1)
        self._q.put(("ended", None))

    def _refresh_now(self) -> None:
        if self._running:
            self._q.put(("fetch", False))

    def _test_once(self) -> None:
        if self._running:
            self._log("warn", "End your shift before running a test.")
            return
        if not self._connect():
            return
        self._session = ShiftSession()
        self._clear_tickets()
        self._log("info", "Test: fetching one unassigned ticket…")
        self._q.put(("fetch", True))

    def _do_fetch(self, test_one: bool) -> None:
        if not self._jira:
            return
        s = self._settings
        jql = build_jql(s["project_key"], one_unassigned=test_one)
        limit = 1 if test_one else 100
        self._session.polls += 1

        try:
            issues = self._jira.fetch_issues(jql, limit=limit)
        except Exception as exc:
            self._qlog("err", f"Could not load tickets: {exc}")
            return

        self._qlog("info", f"Loaded {len(issues)} ticket(s).")
        if issues:
            self._clear_tickets()
        auto = self._auto_var.get() or test_one

        for issue in issues:
            key = issue["key"]
            f = issue.get("fields", {})
            summary = (f.get("summary") or "—")[:80]
            status = f.get("status", {}).get("name", "?")
            assignee = f.get("assignee")
            name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
            row = TicketRow(key, summary, status, name)

            if assignee and not test_one:
                self._q.put(("ticket", row))
                continue

            if auto and not assignee:
                try:
                    status_name = self._settings.get("assign_status") or DEFAULT_STATUS
                    comment = self._settings.get("assign_comment") or DEFAULT_COMMENT
                    steps = self._jira.claim_ticket(
                        key, self._assignee_id, status_name, comment
                    )
                    row.assigned_by_app = True
                    row.assignee = "You"
                    row.status = status_name
                    self._session.assigned_keys.append(key)
                    summary = ", ".join(f"{s}: {d}" for s, d in steps)
                    self._qlog("ok", f"{key} — {summary}")
                    if self._auto_var.get():
                        self._file_log.write_ticket(key, "claim started")
                        for step, detail in steps:
                            self._file_log.write_ticket(key, f"{step} — {detail}")
                except Exception as exc:
                    self._qlog("err", f"{key}: {exc}")
                    if self._auto_var.get():
                        self._file_log.write_ticket(key, f"claim FAILED — {exc}")

            self._session.tickets.append(row)
            self._q.put(("ticket", row))

        if test_one:
            if self._session.assigned_keys:
                self._q.put(("test_ok", self._session.assigned_keys[-1]))
            else:
                self._q.put(("test_fail", None))

    def _end_shift(self) -> None:
        if not self._running:
            return
        self._log("info", "Ending shift…")
        self._stop.set()

    def _on_ended(self) -> None:
        self._running = False
        self._stop_shift_clock()
        final = "—"
        if self._shift_start:
            secs = int((datetime.now() - self._shift_start).total_seconds())
            final = format_elapsed(secs)
            self._set_chip(self._chip_shift, final)
        self._shift_start = None
        self._timer_lbl.configure(text="")
        self._apply_state()
        s = self._session.summary()
        if final != "—":
            s = f"Shift length: {final}\n\n{s}"
        self._log("info", "Shift ended.\n" + s)
        messagebox.showinfo("Shift ended", s)

    def _start_shift_clock(self) -> None:
        self._stop_shift_clock()
        self._tick_shift_clock()

    def _stop_shift_clock(self) -> None:
        if self._shift_clock_id:
            self.after_cancel(self._shift_clock_id)
            self._shift_clock_id = None

    def _tick_shift_clock(self) -> None:
        if self._running and self._shift_start:
            secs = int((datetime.now() - self._shift_start).total_seconds())
            self._set_chip(self._chip_shift, format_elapsed(secs))
            self._shift_clock_id = self.after(1000, self._tick_shift_clock)

    # ── tickets ui ──
    def _clear_tickets(self) -> None:
        for w in self._ticket_scroll.winfo_children():
            w.destroy()
        if self._empty_ticket_lbl:
            self._empty_ticket_lbl.destroy()
            self._empty_ticket_lbl = None

    def _show_ticket_empty(self, msg: str) -> None:
        self._clear_tickets()
        self._empty_ticket_lbl = ctk.CTkLabel(
            self._ticket_scroll, text=msg,
            font=ctk.CTkFont(size=13), text_color=C["muted"], wraplength=280,
        )
        self._empty_ticket_lbl.pack(expand=True, pady=40)

    def _add_ticket(self, t: TicketRow) -> None:
        if self._empty_ticket_lbl:
            self._empty_ticket_lbl.destroy()
            self._empty_ticket_lbl = None
        card = ctk.CTkFrame(self._ticket_scroll, fg_color=C["bg"], corner_radius=8)
        card.pack(fill="x", pady=4, padx=4)
        color = C["ok"] if t.assigned_by_app else C["accent"]
        ctk.CTkLabel(
            card, text=t.key, width=76, anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=color,
        ).pack(side="left", padx=10, pady=10)
        ctk.CTkLabel(
            card, text=f"{t.status} · {t.assignee}\n{t.summary}",
            anchor="w", justify="left", font=ctk.CTkFont(size=11), text_color=C["text"],
        ).pack(side="left", fill="x", expand=True, padx=(0, 10), pady=8)

    def _process_q(self) -> None:
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "log":
                    lvl, msg = val
                    self._log(lvl, msg)
                elif kind == "fetch":
                    self._do_fetch(val)
                elif kind == "ticket":
                    self._add_ticket(val)
                elif kind == "tick":
                    m, sec = divmod(int(val), 60)
                    self._timer_lbl.configure(text=f"Next refresh {m:02d}:{sec:02d}")
                elif kind == "ended":
                    self._on_ended()
                elif kind == "test_ok":
                    messagebox.showinfo("Test passed", f"Assigned ticket {val}")
                elif kind == "test_fail":
                    messagebox.showwarning("Test", "No unassigned ticket found in this project.")
        except queue.Empty:
            pass
        self.after(150, self._process_q)

    def _on_close(self) -> None:
        if self._running:
            if messagebox.askyesno("Quit", "A shift is running. End shift and quit?"):
                self._stop.set()
                self.after(400, self.destroy)
        else:
            self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
