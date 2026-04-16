"""
Guided desktop UI for rushport.

This wraps the existing auth and transfer pipeline in a small Tkinter wizard
so non-technical users can configure the app, authenticate, choose a share,
and run a transfer without working in a terminal.
"""

from __future__ import annotations

import asyncio
import io
import os
import queue
import threading
import webbrowser
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
import tkinter as tk
from tkinter import BooleanVar, StringVar, Tk, messagebox
from tkinter import scrolledtext
from tkinter import ttk

import yaml
from rich.console import Console

from auth.microsoft import MicrosoftAuth
from auth.rushfiles import RushfilesAuth, RushfilesAuthError
from clients.rushfiles import RushfilesClient
from config import ConfigError, load_config
from destinations.onedrive import create_onedrive_client
from models.rushfiles import Share
from sync.engine import SyncEngine
from sync.state import StateDB
from sync.transfer import sanitize_name


CONFIG_PATH = Path("config.yaml")
DEFAULT_WINDOW_SIZE = "1180x760"


class QueueTextStream(io.TextIOBase):
    def __init__(self, event_queue: queue.Queue):
        self._event_queue = event_queue
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buffer += text.replace("\r", "\n")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                self._event_queue.put(("log", line))
        return len(text)

    def flush(self) -> None:
        line = self._buffer.strip()
        self._buffer = ""
        if line:
            self._event_queue.put(("log", line))


@contextmanager
def queued_console(event_queue: queue.Queue):
    import sync.engine as engine_module

    old_console = engine_module.console
    stream = QueueTextStream(event_queue)
    engine_module.console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=100,
    )
    try:
        with redirect_stdout(stream), redirect_stderr(stream):
            yield
    finally:
        stream.flush()
        engine_module.console = old_console


class RushportGUI:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("rushport")
        self.root.geometry(DEFAULT_WINDOW_SIZE)
        self.root.minsize(1040, 680)

        self.event_queue: queue.Queue = queue.Queue()
        self.busy = False
        self.current_step = 0
        self.shares: list[Share] = []
        self.rf_authenticated = False
        self.ms_authenticated = False
        self._last_auto_destination = ""

        self.rf_email_var = StringVar()
        self.rf_password_var = StringVar()
        self.clientgateway_var = StringVar()
        self.filecache_var = StringVar()
        self.ms_tenant_var = StringVar(value="common")
        self.ms_client_mode_var = StringVar(value="shared")
        self.ms_client_id_var = StringVar()
        self.transfer_concurrency_var = StringVar(value="4")
        self.chunk_size_var = StringVar(value="10")
        self.retry_attempts_var = StringVar(value="3")
        self.retry_delay_var = StringVar(value="5")
        self.state_db_var = StringVar(value="~/.rushport/state.db")

        self.destination_folder_var = StringVar()
        self.selected_share_var = StringVar()
        self.dry_run_var = BooleanVar(value=False)
        self.reset_var = BooleanVar(value=False)

        self.step_titles = [
            "1. Setup",
            "2. Connect",
            "3. Choose Share",
            "4. Transfer",
        ]
        self.step_descriptions = [
            "Enter tenant URLs and pick Microsoft auth mode.",
            "Authenticate both services with guided actions.",
            "Load shares and choose what to migrate.",
            "Review the plan and run the transfer.",
        ]
        self.step_cards: list[ttk.Frame] = []
        self.step_title_labels: list[ttk.Label] = []
        self.step_desc_labels: list[ttk.Label] = []
        self.page_frames: list[ttk.Frame] = []

        self.share_tree: ttk.Treeview | None = None
        self.log_text: scrolledtext.ScrolledText | None = None
        self.summary_label: ttk.Label | None = None
        self.rf_status_value: ttk.Label | None = None
        self.ms_status_value: ttk.Label | None = None
        self.page_canvas: tk.Canvas | None = None
        self.page_stack: ttk.Frame | None = None
        self.onedrive_prompt_window: tk.Toplevel | None = None

        self._configure_style()
        self._build_layout()
        self._update_client_id_state()
        self._set_step(0)
        self._refresh_step_state()
        self._start_background("refresh_auth", self._refresh_auth_status_task)
        self.root.after(125, self._drain_events)

    def _configure_style(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.configure(bg="#f5f7fb")
        style.configure("Root.TFrame", background="#f5f7fb")
        style.configure("Sidebar.TFrame", background="#10233f")
        style.configure("Card.TFrame", background="#ffffff", relief="flat")
        style.configure("SidebarCard.TFrame", background="#153053", relief="flat")
        style.configure(
            "Title.TLabel",
            background="#f5f7fb",
            foreground="#0f172a",
            font=("Segoe UI", 20, "bold"),
        )
        style.configure(
            "Body.TLabel",
            background="#ffffff",
            foreground="#334155",
            font=("Segoe UI", 10),
        )
        style.configure(
            "SidebarTitle.TLabel",
            background="#10233f",
            foreground="#ffffff",
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "SidebarHint.TLabel",
            background="#10233f",
            foreground="#cbd5e1",
            font=("Segoe UI", 10),
        )
        style.configure(
            "StepTitle.TLabel",
            background="#153053",
            foreground="#cbd5e1",
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "StepDesc.TLabel",
            background="#153053",
            foreground="#94a3b8",
            font=("Segoe UI", 9),
        )
        style.configure(
            "StepTitleActive.TLabel",
            background="#0d9488",
            foreground="#ffffff",
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "StepDescActive.TLabel",
            background="#0d9488",
            foreground="#e6fffa",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Section.TLabel",
            background="#ffffff",
            foreground="#0f172a",
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "ValueOk.TLabel",
            background="#ffffff",
            foreground="#15803d",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "ValueWarn.TLabel",
            background="#ffffff",
            foreground="#b45309",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "Primary.TButton",
            padding=(16, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#0f766e"), ("!disabled", "#0d9488")],
            foreground=[("!disabled", "#ffffff")],
        )
        style.configure("Secondary.TButton", padding=(14, 10), font=("Segoe UI", 10))
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("TEntry", padding=8)
        style.configure("TRadiobutton", background="#ffffff", foreground="#334155")
        style.configure("TCheckbutton", background="#ffffff", foreground="#334155")
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build_layout(self) -> None:
        root_frame = ttk.Frame(self.root, style="Root.TFrame", padding=0)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(1, weight=1)
        root_frame.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(root_frame, style="Sidebar.TFrame", padding=(24, 28))
        sidebar.grid(row=0, column=0, sticky="ns")

        content = ttk.Frame(root_frame, style="Root.TFrame", padding=(28, 24))
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        ttk.Label(sidebar, text="rushport", style="SidebarTitle.TLabel").pack(anchor="w")
        ttk.Label(
            sidebar,
            text="A guided migration flow for moving authorized RushFiles data into OneDrive.",
            style="SidebarHint.TLabel",
            wraplength=220,
            justify="left",
        ).pack(anchor="w", pady=(8, 24))

        for title, desc in zip(self.step_titles, self.step_descriptions):
            card = ttk.Frame(sidebar, style="SidebarCard.TFrame", padding=14)
            card.pack(fill="x", pady=(0, 12))
            title_label = ttk.Label(card, text=title, style="StepTitle.TLabel")
            title_label.pack(anchor="w")
            desc_label = ttk.Label(
                card,
                text=desc,
                style="StepDesc.TLabel",
                wraplength=200,
                justify="left",
            )
            desc_label.pack(anchor="w", pady=(6, 0))
            self.step_cards.append(card)
            self.step_title_labels.append(title_label)
            self.step_desc_labels.append(desc_label)

        header = ttk.Frame(content, style="Root.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Migration wizard", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="A steady path from configuration to transfer. Save your settings once, then work through the steps on the left.",
            style="Body.TLabel",
            wraplength=780,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        self.page_container = ttk.Frame(content, style="Card.TFrame", padding=0)
        self.page_container.grid(row=1, column=0, sticky="nsew", pady=(20, 16))
        self.page_container.columnconfigure(0, weight=1)
        self.page_container.rowconfigure(0, weight=1)

        page_canvas = tk.Canvas(
            self.page_container,
            highlightthickness=0,
            bg="#ffffff",
        )
        page_canvas.grid(row=0, column=0, sticky="nsew")
        page_scrollbar = ttk.Scrollbar(
            self.page_container,
            orient="vertical",
            command=page_canvas.yview,
        )
        page_scrollbar.grid(row=0, column=1, sticky="ns")
        page_canvas.configure(yscrollcommand=page_scrollbar.set)

        self.page_stack = ttk.Frame(page_canvas, style="Card.TFrame", padding=24)
        self.page_stack.columnconfigure(0, weight=1)
        self.page_stack.rowconfigure(0, weight=1)
        page_window = page_canvas.create_window((0, 0), window=self.page_stack, anchor="nw")

        def _sync_page_scroll(_event=None):
            page_canvas.configure(scrollregion=page_canvas.bbox("all"))

        def _sync_page_width(event):
            page_canvas.itemconfigure(page_window, width=event.width)

        self.page_stack.bind("<Configure>", _sync_page_scroll)
        page_canvas.bind("<Configure>", _sync_page_width)
        self.page_canvas = page_canvas

        nav = ttk.Frame(content, style="Root.TFrame")
        nav.grid(row=2, column=0, sticky="ew")
        nav.columnconfigure(1, weight=1)

        self.back_button = ttk.Button(nav, text="Back", command=self._go_back, style="Secondary.TButton")
        self.back_button.grid(row=0, column=0, sticky="w")
        self.nav_hint = ttk.Label(nav, text="", style="Body.TLabel")
        self.nav_hint.grid(row=0, column=1, sticky="w", padx=12)
        self.next_button = ttk.Button(nav, text="Next", command=self._go_next, style="Primary.TButton")
        self.next_button.grid(row=0, column=2, sticky="e")

        self._build_setup_page()
        self._build_connect_page()
        self._build_share_page()
        self._build_transfer_page()

    def _new_page(self) -> ttk.Frame:
        frame = ttk.Frame(self.page_stack, style="Card.TFrame")
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        self.page_frames.append(frame)
        return frame

    def _build_setup_page(self) -> None:
        frame = self._new_page()
        ttk.Label(frame, text="Setup the connection details", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            frame,
            text="Enter the tenant URLs for RushFiles and choose whether Microsoft sign-in should use the shared rushport app or your own app registration.",
            style="Body.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(8, 20))

        form = ttk.Frame(frame, style="Card.TFrame")
        form.grid(row=2, column=0, sticky="nsew")
        for idx in range(2):
            form.columnconfigure(idx, weight=1)

        self._labeled_entry(form, 0, 0, "RushFiles client gateway URL", self.clientgateway_var)
        self._labeled_entry(form, 0, 1, "RushFiles file cache URL (optional)", self.filecache_var)
        self._labeled_entry(form, 1, 0, "Microsoft tenant ID", self.ms_tenant_var)

        mode_frame = ttk.Frame(form, style="Card.TFrame", padding=(0, 8))
        mode_frame.grid(row=2, column=0, sticky="ew", padx=(0, 12), pady=(6, 0))
        ttk.Label(mode_frame, text="Microsoft app mode", style="Section.TLabel").pack(anchor="w")
        ttk.Radiobutton(
            mode_frame,
            text="Use shared rushport app (simplest)",
            value="shared",
            variable=self.ms_client_mode_var,
            command=self._update_client_id_state,
        ).pack(anchor="w", pady=(10, 4))
        ttk.Radiobutton(
            mode_frame,
            text="Use my own Microsoft Entra app",
            value="custom",
            variable=self.ms_client_mode_var,
            command=self._update_client_id_state,
        ).pack(anchor="w")

        self._labeled_entry(form, 2, 1, "Custom Microsoft client ID", self.ms_client_id_var)

        advanced = ttk.LabelFrame(frame, text="Transfer defaults", padding=16)
        advanced.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        for idx in range(4):
            advanced.columnconfigure(idx, weight=1)
        self._labeled_entry(advanced, 0, 0, "Concurrency", self.transfer_concurrency_var, width=18)
        self._labeled_entry(advanced, 0, 1, "Chunk size (MB)", self.chunk_size_var, width=18)
        self._labeled_entry(advanced, 0, 2, "Retry attempts", self.retry_attempts_var, width=18)
        self._labeled_entry(advanced, 0, 3, "Retry delay (s)", self.retry_delay_var, width=18)
        self._labeled_entry(advanced, 1, 0, "State DB path", self.state_db_var, width=36, columnspan=2)

        action_row = ttk.Frame(frame, style="Card.TFrame")
        action_row.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        ttk.Button(action_row, text="Save configuration", command=self._save_configuration, style="Primary.TButton").pack(side="left")
        ttk.Button(action_row, text="Load saved config", command=self._load_existing_config, style="Secondary.TButton").pack(side="left", padx=(10, 0))

    def _build_connect_page(self) -> None:
        frame = self._new_page()
        ttk.Label(frame, text="Connect your accounts", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            frame,
            text="Authenticate RushFiles first, then connect OneDrive. The RushFiles login opens Chromium so the sign-in flow is easier to follow.",
            style="Body.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(8, 20))

        status_card = ttk.LabelFrame(frame, text="Connection status", padding=16)
        status_card.grid(row=2, column=0, sticky="ew")
        status_card.columnconfigure(1, weight=1)
        ttk.Label(status_card, text="RushFiles").grid(row=0, column=0, sticky="w")
        self.rf_status_value = ttk.Label(status_card, text="Checking...", style="ValueWarn.TLabel")
        self.rf_status_value.grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Label(status_card, text="OneDrive").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.ms_status_value = ttk.Label(status_card, text="Checking...", style="ValueWarn.TLabel")
        self.ms_status_value.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        ttk.Button(status_card, text="Refresh status", command=lambda: self._start_background("refresh_auth", self._refresh_auth_status_task), style="Secondary.TButton").grid(
            row=0, column=2, rowspan=2, sticky="e"
        )

        creds = ttk.LabelFrame(frame, text="RushFiles sign-in", padding=16)
        creds.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        creds.columnconfigure(0, weight=1)
        creds.columnconfigure(1, weight=1)
        self._labeled_entry(creds, 0, 0, "Email", self.rf_email_var)
        self._labeled_entry(creds, 0, 1, "Password", self.rf_password_var, show="*")

        button_row = ttk.Frame(frame, style="Card.TFrame")
        button_row.grid(row=4, column=0, sticky="w", pady=(18, 0))
        ttk.Button(
            button_row,
            text="Authenticate RushFiles",
            command=self._authenticate_rushfiles,
            style="Primary.TButton",
        ).pack(side="left")
        ttk.Button(
            button_row,
            text="Authenticate OneDrive",
            command=lambda: self._start_background("auth_onedrive", self._auth_onedrive_task),
            style="Secondary.TButton",
        ).pack(side="left", padx=(10, 0))

    def _build_share_page(self) -> None:
        frame = self._new_page()
        frame.rowconfigure(3, weight=1)
        ttk.Label(frame, text="Choose what to migrate", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            frame,
            text="Load your accessible RushFiles shares, choose one, and set the destination folder name that should be used in OneDrive.",
            style="Body.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(8, 18))

        controls = ttk.Frame(frame, style="Card.TFrame")
        controls.grid(row=2, column=0, sticky="ew")
        ttk.Button(controls, text="Load shares", command=lambda: self._start_background("load_shares", self._load_shares_task), style="Primary.TButton").pack(side="left")
        ttk.Label(controls, text="Select a share below to continue.", style="Body.TLabel").pack(side="left", padx=(12, 0))

        tree_frame = ttk.Frame(frame, style="Card.TFrame")
        tree_frame.grid(row=3, column=0, sticky="nsew", pady=(16, 0))
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        columns = ("share_name", "company", "share_id")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        tree.heading("share_name", text="Share")
        tree.heading("company", text="Company")
        tree.heading("share_id", text="Share ID")
        tree.column("share_name", width=260, anchor="w")
        tree.column("company", width=220, anchor="w")
        tree.column("share_id", width=320, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", self._on_share_selected)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        self.share_tree = tree

        options = ttk.LabelFrame(frame, text="Transfer options", padding=16)
        options.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        options.columnconfigure(0, weight=1)
        self._labeled_entry(options, 0, 0, "Destination folder in OneDrive", self.destination_folder_var, width=42)
        ttk.Checkbutton(options, text="Test only (scan, do not upload)", variable=self.dry_run_var).grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Checkbutton(options, text="Restart this share from scratch", variable=self.reset_var).grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _build_transfer_page(self) -> None:
        frame = self._new_page()
        frame.rowconfigure(3, weight=1)
        ttk.Label(frame, text="Run the migration", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            frame,
            text="Review the plan, start the transfer, and watch activity in the log below. You can leave the app open while the migration runs.",
            style="Body.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(8, 18))

        summary = ttk.LabelFrame(frame, text="Transfer summary", padding=16)
        summary.grid(row=2, column=0, sticky="ew")
        self.summary_label = ttk.Label(
            summary,
            text="No transfer prepared yet.",
            style="Body.TLabel",
            wraplength=760,
            justify="left",
        )
        self.summary_label.pack(anchor="w")

        log_card = ttk.LabelFrame(frame, text="Activity log", padding=10)
        log_card.grid(row=3, column=0, sticky="nsew", pady=(18, 0))
        log_card.rowconfigure(0, weight=1)
        log_card.columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            log_card,
            wrap="word",
            height=18,
            font=("Cascadia Mono", 10),
            bg="#0f172a",
            fg="#e2e8f0",
            insertbackground="#e2e8f0",
            relief="flat",
            padx=12,
            pady=12,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.insert("end", "Activity will appear here.\n")
        self.log_text.configure(state="disabled")

        action_row = ttk.Frame(frame, style="Card.TFrame")
        action_row.grid(row=4, column=0, sticky="w", pady=(18, 0))
        ttk.Button(action_row, text="Start transfer", command=self._start_transfer, style="Primary.TButton").pack(side="left")
        ttk.Button(action_row, text="Clear log", command=self._clear_log, style="Secondary.TButton").pack(side="left", padx=(10, 0))

    def _labeled_entry(
        self,
        parent: ttk.Widget,
        row: int,
        column: int,
        label: str,
        variable: StringVar,
        width: int = 30,
        show: str | None = None,
        columnspan: int = 1,
    ) -> ttk.Entry:
        wrapper = ttk.Frame(parent, style="Card.TFrame")
        wrapper.grid(row=row, column=column, columnspan=columnspan, sticky="ew", padx=(0, 12), pady=(0, 12))
        ttk.Label(wrapper, text=label).pack(anchor="w", pady=(0, 6))
        entry = ttk.Entry(wrapper, textvariable=variable, width=width, show=show or "")
        entry.pack(fill="x")
        return entry

    def _load_existing_config(self) -> None:
        if not CONFIG_PATH.exists():
            messagebox.showinfo("No saved config", "No local config.yaml was found yet.")
            return
        try:
            data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            messagebox.showerror("Could not load config", "config.yaml could not be read.")
            return

        rushfiles = data.get("rushfiles", {})
        microsoft = data.get("microsoft", {})
        transfer = data.get("transfer", {})
        state = data.get("state", {})

        self.rf_email_var.set(rushfiles.get("email", ""))
        self.clientgateway_var.set(rushfiles.get("clientgateway_base", ""))
        self.filecache_var.set(rushfiles.get("filecache_base", ""))
        self.ms_tenant_var.set(microsoft.get("tenant_id", "common") or "common")
        client_id = microsoft.get("client_id", "")
        if client_id:
            self.ms_client_mode_var.set("custom")
            self.ms_client_id_var.set(client_id)
        else:
            self.ms_client_mode_var.set("shared")
        self.transfer_concurrency_var.set(str(transfer.get("concurrency", 4)))
        self.chunk_size_var.set(str(transfer.get("chunk_size_mb", 10)))
        self.retry_attempts_var.set(str(transfer.get("retry_attempts", 3)))
        self.retry_delay_var.set(str(transfer.get("retry_delay_s", 5)))
        self.state_db_var.set(state.get("db_path", "~/.rushport/state.db"))
        self._append_log("Loaded saved values from config.yaml.")
        self._update_transfer_summary()

    def _save_configuration(self, notify: bool = True) -> bool:
        if not self.clientgateway_var.get().strip():
            messagebox.showerror("Missing value", "RushFiles client gateway URL is required.")
            return False
        if self.ms_client_mode_var.get() == "custom" and not self.ms_client_id_var.get().strip():
            messagebox.showerror("Missing value", "Enter a Microsoft client ID or switch back to the shared app option.")
            return False

        data = {}
        if CONFIG_PATH.exists():
            try:
                data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}

        data["rushfiles"] = {
            "email": self.rf_email_var.get().strip(),
            "password": "",
            "clientgateway_base": self.clientgateway_var.get().strip(),
            "filecache_base": self.filecache_var.get().strip(),
        }
        data["microsoft"] = {
            "client_id": self.ms_client_id_var.get().strip() if self.ms_client_mode_var.get() == "custom" else "",
            "tenant_id": self.ms_tenant_var.get().strip() or "common",
        }
        try:
            concurrency = int(self.transfer_concurrency_var.get().strip() or "4")
            chunk_size = int(self.chunk_size_var.get().strip() or "10")
            retry_attempts = int(self.retry_attempts_var.get().strip() or "3")
            retry_delay = float(self.retry_delay_var.get().strip() or "5")
        except ValueError:
            messagebox.showerror("Invalid settings", "Transfer defaults must use numbers only.")
            return False

        data["transfer"] = {
            "concurrency": concurrency,
            "chunk_size_mb": chunk_size,
            "retry_attempts": retry_attempts,
            "retry_delay_s": retry_delay,
        }
        data["state"] = {
            "db_path": self.state_db_var.get().strip() or "~/.rushport/state.db",
        }

        CONFIG_PATH.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
        self._append_log("Configuration saved.")
        self._refresh_step_state()
        if notify:
            messagebox.showinfo("Saved", "Configuration saved. You can continue to account connection.")
        return True

    def _update_client_id_state(self) -> None:
        if self.ms_client_mode_var.get() == "shared":
            self.ms_client_id_var.set("")

    def _set_step(self, step_index: int) -> None:
        self.current_step = step_index
        for idx, frame in enumerate(self.page_frames):
            if idx == step_index:
                frame.tkraise()
        if self.page_canvas:
            self.page_canvas.yview_moveto(0)

        for idx, _card in enumerate(self.step_cards):
            active = idx == step_index
            self.step_title_labels[idx].configure(style="StepTitleActive.TLabel" if active else "StepTitle.TLabel")
            self.step_desc_labels[idx].configure(style="StepDescActive.TLabel" if active else "StepDesc.TLabel")

        self.back_button.configure(state="normal" if step_index > 0 else "disabled")
        self.next_button.configure(text="Next" if step_index < len(self.page_frames) - 1 else "Stay here")
        self.nav_hint.configure(text=self.step_descriptions[step_index])
        self._update_transfer_summary()

    def _go_next(self) -> None:
        if self.current_step >= len(self.page_frames) - 1:
            return
        if self.current_step == 0:
            if not self._save_configuration():
                return
        elif self.current_step == 1 and not (self.rf_authenticated and self.ms_authenticated):
            messagebox.showinfo("Continue after sign-in", "Connect both RushFiles and OneDrive before moving to share selection.")
            return
        elif self.current_step == 2 and not self._has_share_selection():
            messagebox.showinfo("Choose a share", "Select a share and confirm the destination folder before continuing.")
            return
        self._set_step(self.current_step + 1)

    def _go_back(self) -> None:
        if self.current_step > 0:
            self._set_step(self.current_step - 1)

    def _refresh_step_state(self) -> None:
        self.rf_status_value.configure(
            text="Connected" if self.rf_authenticated else "Needs sign-in",
            style="ValueOk.TLabel" if self.rf_authenticated else "ValueWarn.TLabel",
        )
        self.ms_status_value.configure(
            text="Connected" if self.ms_authenticated else "Needs sign-in",
            style="ValueOk.TLabel" if self.ms_authenticated else "ValueWarn.TLabel",
        )
        self._update_transfer_summary()

    def _update_transfer_summary(self) -> None:
        if not self.summary_label:
            return
        share = self._current_share()
        if not share:
            text = "No transfer prepared yet."
        else:
            mode = "Dry run" if self.dry_run_var.get() else "Full migration"
            reset = " Restart from scratch is enabled." if self.reset_var.get() else ""
            text = (
                f"{mode} for '{share.name}' into OneDrive folder '{self.destination_folder_var.get().strip() or sanitize_name(share.name)}'."
                f"{reset}"
            )
        self.summary_label.configure(text=text)

    def _append_log(self, text: str) -> None:
        if not self.log_text:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        if not self.log_text:
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _start_background(self, kind: str, target) -> None:
        if self.busy:
            messagebox.showinfo("Please wait", "An action is already running.")
            return

        self.busy = True
        self._append_log(f"Starting: {kind}")

        def runner() -> None:
            try:
                result = target()
            except Exception as exc:
                self.event_queue.put(("task_error", {"kind": kind, "error": str(exc)}))
            else:
                self.event_queue.put(("task_done", {"kind": kind, "result": result}))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_events(self) -> None:
        while True:
            try:
                event_type, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "log":
                self._append_log(payload)
            elif event_type == "onedrive_prompt":
                self._show_onedrive_prompt(payload)
            elif event_type == "task_done":
                self.busy = False
                self._handle_task_done(payload["kind"], payload.get("result"))
            elif event_type == "task_error":
                self.busy = False
                self._append_log(f"Error during {payload['kind']}: {payload['error']}")
                messagebox.showerror("Action failed", payload["error"])
        self.root.after(125, self._drain_events)

    def _handle_task_done(self, kind: str, result) -> None:
        if kind == "refresh_auth":
            self.rf_authenticated, self.ms_authenticated = result
            self._refresh_step_state()
        elif kind == "auth_rushfiles":
            self.rf_authenticated = True
            self._refresh_step_state()
            messagebox.showinfo("RushFiles connected", "RushFiles authentication completed successfully.")
        elif kind == "auth_onedrive":
            self.ms_authenticated = True
            self._refresh_step_state()
            if self.onedrive_prompt_window and self.onedrive_prompt_window.winfo_exists():
                self.onedrive_prompt_window.destroy()
                self.onedrive_prompt_window = None
            messagebox.showinfo("OneDrive connected", "OneDrive authentication completed successfully.")
        elif kind == "load_shares":
            self.shares = result
            self._populate_shares()
            self._set_step(2)
        elif kind == "transfer":
            self._append_log("Transfer finished.")
            stats = result
            summary = f"Transferred {stats.transferred} file(s). Failed: {stats.failed}."
            if self.dry_run_var.get():
                summary = f"Dry run scanned {stats.scanned} file(s) totaling {stats.total_bytes} bytes."
            messagebox.showinfo("Transfer complete", summary)

    def _show_onedrive_prompt(self, flow: dict) -> None:
        verification_uri = flow.get("verification_uri") or "https://aka.ms/devicelogin"
        user_code = flow.get("user_code", "")
        expires_in = flow.get("expires_in")
        self._copy_to_clipboard(user_code)

        try:
            webbrowser.open(verification_uri)
        except Exception:
            self._append_log(f"Open this URL manually: {verification_uri}")

        if self.onedrive_prompt_window and self.onedrive_prompt_window.winfo_exists():
            self.onedrive_prompt_window.destroy()

        window = tk.Toplevel(self.root)
        window.title("OneDrive sign-in")
        window.transient(self.root)
        window.grab_set()
        window.resizable(False, False)
        window.configure(bg="#ffffff")
        window.geometry("520x320")
        self.onedrive_prompt_window = window

        body = ttk.Frame(window, padding=20, style="Card.TFrame")
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Finish Microsoft sign-in", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="A browser window has been opened. Enter the code below on Microsoft's device login page.",
            style="Body.TLabel",
            wraplength=460,
            justify="left",
        ).pack(anchor="w", pady=(8, 16))

        ttk.Label(body, text="Device login page").pack(anchor="w")
        url_entry = ttk.Entry(body, width=56)
        url_entry.pack(fill="x", pady=(6, 14))
        url_entry.insert(0, verification_uri)
        url_entry.configure(state="readonly")

        ttk.Label(body, text="Code to enter").pack(anchor="w")
        code_entry = ttk.Entry(body, width=24, font=("Cascadia Mono", 16))
        code_entry.pack(fill="x", pady=(6, 8))
        code_entry.insert(0, user_code or "")
        code_entry.configure(state="readonly")
        code_entry.focus_set()
        code_entry.selection_range(0, "end")

        expiry_text = "This code stays valid until the Microsoft sign-in step expires."
        if expires_in:
            minutes = max(1, int(expires_in) // 60)
            expiry_text = f"This code expires in about {minutes} minute(s)."
        ttk.Label(body, text=expiry_text, style="Body.TLabel").pack(anchor="w", pady=(2, 18))

        button_row = ttk.Frame(body, style="Card.TFrame")
        button_row.pack(fill="x")
        ttk.Button(
            button_row,
            text="Copy code",
            command=lambda: self._copy_to_clipboard(user_code, notify=True),
            style="Primary.TButton",
        ).pack(side="left")
        ttk.Button(
            button_row,
            text="Open sign-in page",
            command=lambda: webbrowser.open(verification_uri),
            style="Secondary.TButton",
        ).pack(side="left", padx=(10, 0))
        ttk.Button(
            button_row,
            text="Close",
            command=window.destroy,
            style="Secondary.TButton",
        ).pack(side="right")

    def _copy_to_clipboard(self, text: str, notify: bool = False) -> None:
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            self._append_log("Copied code to clipboard.")
            if notify:
                messagebox.showinfo("Copied", "The code has been copied to your clipboard.")
        except Exception as exc:
            self._append_log(f"Could not copy to clipboard automatically: {exc}")
            if notify:
                messagebox.showerror("Clipboard error", f"Could not copy the code automatically: {exc}")

    def _refresh_auth_status_task(self):
        return asyncio.run(self._refresh_auth_status_async())

    async def _refresh_auth_status_async(self):
        rf_ok = False
        ms_ok = False
        try:
            cfg = load_config()
        except ConfigError:
            return rf_ok, ms_ok

        rf_auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        try:
            rf_ok = await rf_auth.load_cached()
        except RushfilesAuthError:
            rf_ok = False

        ms_auth = MicrosoftAuth(client_id=cfg.ms_client_id, tenant_id=cfg.ms_tenant_id)
        ms_ok = ms_auth.is_authenticated()
        return rf_ok, ms_ok

    def _authenticate_rushfiles(self) -> None:
        email = self.rf_email_var.get().strip()
        password = self.rf_password_var.get().strip()
        if not email or not password:
            messagebox.showerror("Missing sign-in details", "Enter your RushFiles email and password first.")
            return
        if not self._save_configuration(notify=False):
            return
        self._start_background(
            "auth_rushfiles",
            lambda: self._auth_rushfiles_task(email, password),
        )

    def _auth_rushfiles_task(self, email: str, password: str):
        return asyncio.run(self._auth_rushfiles_async(email, password))

    async def _auth_rushfiles_async(self, email: str, password: str):
        cfg = load_config()
        auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        old_headed = os.environ.get("RF_AUTH_HEADED")
        os.environ["RF_AUTH_HEADED"] = "1"
        self.event_queue.put(("log", "Opening Chromium for RushFiles sign-in..."))
        try:
            with queued_console(self.event_queue):
                await auth.login(email, password)
        finally:
            if old_headed is None:
                os.environ.pop("RF_AUTH_HEADED", None)
            else:
                os.environ["RF_AUTH_HEADED"] = old_headed
        self.event_queue.put(("log", "RushFiles authentication completed."))
        return True

    def _auth_onedrive_task(self):
        return asyncio.run(self._auth_onedrive_async())

    async def _auth_onedrive_async(self):
        cfg = load_config()
        ms_auth = MicrosoftAuth(client_id=cfg.ms_client_id, tenant_id=cfg.ms_tenant_id)
        self.event_queue.put(("log", "Starting OneDrive device-code sign-in..."))
        flow = ms_auth.start_device_flow()
        self.event_queue.put(("onedrive_prompt", flow))
        self.event_queue.put(("log", flow.get("message", "Microsoft sign-in started.")))
        with queued_console(self.event_queue):
            ms_auth.complete_device_flow(flow)
        self.event_queue.put(("log", "OneDrive authentication completed."))
        return True

    def _load_shares_task(self):
        return asyncio.run(self._load_shares_async())

    async def _load_shares_async(self):
        cfg = load_config()
        rf_auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        if not await rf_auth.load_cached():
            raise RushfilesAuthError("RushFiles is not authenticated yet.")

        async with RushfilesClient(
            rf_auth,
            clientgateway_base=cfg.rf_clientgateway_base,
            filecache_base=cfg.rf_filecache_base,
        ) as client:
            shares = await client.list_shares()

        if not shares:
            self.event_queue.put(("log", "No shares were returned for this account."))
        else:
            self.event_queue.put(("log", f"Loaded {len(shares)} share(s)."))
        return shares

    def _populate_shares(self) -> None:
        if not self.share_tree:
            return
        for item_id in self.share_tree.get_children():
            self.share_tree.delete(item_id)
        for share in self.shares:
            company = share.company_name or share.company_id
            self.share_tree.insert("", "end", iid=share.id, values=(share.name, company, share.id))
        if self.shares:
            first = self.shares[0]
            self.share_tree.selection_set(first.id)
            self._on_share_selected(None)
        self._refresh_step_state()

    def _on_share_selected(self, _event) -> None:
        share = self._current_share()
        if not share:
            return
        self.selected_share_var.set(share.id)
        next_auto = sanitize_name(share.name)
        current_destination = self.destination_folder_var.get().strip()
        if not current_destination or current_destination == self._last_auto_destination:
            self.destination_folder_var.set(next_auto)
        self._last_auto_destination = next_auto
        self._update_transfer_summary()

    def _current_share(self) -> Share | None:
        if not self.share_tree:
            return None
        selected = self.share_tree.selection()
        if not selected:
            return None
        share_id = selected[0]
        return next((share for share in self.shares if share.id == share_id), None)

    def _has_share_selection(self) -> bool:
        return self._current_share() is not None and bool(self.destination_folder_var.get().strip())

    def _start_transfer(self) -> None:
        share = self._current_share()
        destination_folder = self.destination_folder_var.get().strip()
        dry_run = self.dry_run_var.get()
        reset = self.reset_var.get()
        if not share or not destination_folder:
            messagebox.showerror("Incomplete transfer setup", "Select a share and destination folder first.")
            return
        self._set_step(3)
        self._start_background(
            "transfer",
            lambda: self._transfer_task(share, destination_folder, dry_run, reset),
        )

    def _transfer_task(self, share: Share, destination_folder: str, dry_run: bool, reset: bool):
        return asyncio.run(self._transfer_async(share, destination_folder, dry_run, reset))

    async def _transfer_async(self, share: Share, destination_folder: str, dry_run: bool, reset: bool):
        cfg = load_config()
        destination_folder = destination_folder or sanitize_name(share.name)

        rf_auth = RushfilesAuth(clientgateway_base=cfg.rf_clientgateway_base)
        if not await rf_auth.load_cached():
            raise RushfilesAuthError("RushFiles is not authenticated yet.")

        ms_auth, graph_client = create_onedrive_client(
            client_id=cfg.ms_client_id,
            tenant_id=cfg.ms_tenant_id,
            chunk_size_mb=cfg.chunk_size_mb,
        )
        if not ms_auth.is_authenticated():
            raise RuntimeError("OneDrive is not authenticated yet.")

        self.event_queue.put(("log", f"Preparing transfer for share '{share.name}'..."))

        async with RushfilesClient(
            rf_auth,
            clientgateway_base=cfg.rf_clientgateway_base,
            filecache_base=cfg.rf_filecache_base,
        ) as rf_client:
            async with StateDB(cfg.db_path) as state_db:
                if dry_run:
                    from sync.engine import SyncStats, _human_bytes

                    stats = SyncStats()
                    async for _path, vf in rf_client.walk(share.id):
                        if vf.is_file:
                            stats.scanned += 1
                            stats.total_bytes += vf.size_bytes
                    self.event_queue.put(("log", f"Dry run complete: {stats.scanned} file(s), {_human_bytes(stats.total_bytes)}."))
                    return stats

                async with graph_client:
                    engine = SyncEngine(
                        rf_client=rf_client,
                        graph_client=graph_client,
                        state_db=state_db,
                        concurrency=cfg.concurrency,
                        retry_attempts=cfg.retry_attempts,
                        retry_delay_s=cfg.retry_delay_s,
                    )
                    if reset:
                        reset_count = await state_db.reset_share(share.id)
                        self.event_queue.put(("log", f"Reset {reset_count} file(s) back to pending before transfer."))
                    with queued_console(self.event_queue):
                        stats = await engine.run(
                            share.id,
                            destination_folder,
                            resume=False,
                        )
                    return stats


def launch_gui() -> None:
    root = Tk()
    RushportGUI(root)
    root.mainloop()


def main() -> None:
    launch_gui()


if __name__ == "__main__":
    main()
