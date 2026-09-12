from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
import tkinter as tk
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .branding_cache import BrandingCache
from .client import (
    AuthenticationError,
    BlackboardClient,
    BlackboardError,
    ContentNode,
    Course,
    CourseSelection,
    SchoolBranding,
    UserProfile,
    filter_content_nodes,
    normalize_base_url,
)
from .dependencies import audit_dependencies, install_command
from .secure_store import SessionStore, SessionStoreError
from .updater import (
    Release,
    automatic_install_supported,
    check_release,
    download_update,
    install_update,
)


COLORS = {
    "bg": "#ffffff",
    "surface": "#f4f7f5",
    "surface_hover": "#e8eeea",
    "ink": "#17201a",
    "muted": "#56645b",
    "border": "#cdd7d0",
    "primary": "#2f6545",
    "primary_hover": "#255338",
    "primary_soft": "#e2eee6",
    "success": "#267047",
    "success_soft": "#e4f2e9",
    "warning": "#8a5a13",
    "warning_soft": "#fbf0d8",
    "danger": "#b42318",
    "danger_soft": "#fce8e6",
    "focus": "#1f6fa8",
}

if sys.platform == "win32":
    FONT, FONT_MONO = "Segoe UI", "Cascadia Mono"
elif sys.platform == "darwin":
    FONT, FONT_MONO = "SF Pro Text", "SF Mono"
else:
    FONT, FONT_MONO = "DejaVu Sans", "DejaVu Sans Mono"


def resource_path(*parts: str) -> Path:
    """Resolve bundled assets in both source and PyInstaller builds."""
    bundle_root = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
    )
    return bundle_root.joinpath(*parts)


def configure_process_identity() -> None:
    """Give Windows a stable taskbar identity before Tk creates a window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
            "KNN-07.BlackboardDownloader"
        )
    except (AttributeError, OSError):
        pass


class BlackboardApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Blackboard Downloader")
        self.geometry("900x760")
        self.minsize(790, 660)
        self.configure(bg=COLORS["bg"])
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.window_icon_image = None
        self.header_logo_image = None
        self._configure_window_icon()

        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.client: BlackboardClient | None = None
        self.session_store = SessionStore()
        self.branding_cache = BrandingCache()
        self.profile: UserProfile | None = None
        self.branding: SchoolBranding | None = None
        self.school_logo_image = None
        self.courses: list[Course] = []
        self.course_contents: dict[str, tuple[ContentNode, ...]] = {}
        self.content_errors: dict[str, str] = {}
        self.tree_meta: dict[str, tuple[str, str, str | None]] = {}
        self.tree_states: dict[str, int] = {}
        self.tree_labels: dict[str, str] = {}
        self.course_tree_items: dict[str, str] = {}
        self.content_tree_items: dict[tuple[str, str], str] = {}
        self.course_load_states: dict[str, str] = {}
        self.pending_course_selection: dict[str, int | None] = {}
        # Bound concurrency so bulk selection is fast without flooding Blackboard.
        self.content_load_slots = threading.Semaphore(4)
        self.content_request_slots = threading.Semaphore(8)
        self.content_cancel_events: dict[str, threading.Event] = {}
        self.busy = False
        self.downloading = False
        self.log_has_content = False

        settings = self._load_settings()
        self.check_updates_var = tk.BooleanVar(value=settings.get("check_updates", True))
        self.auto_update_var = tk.BooleanVar(value=settings.get("auto_update", False))
        self.update_cancel = threading.Event()
        self.update_running = False
        self.installing_update = False
        self.available_update: Release | None = None
        self.pending_update: Path | None = None
        self.update_status = tk.StringVar(value=f"Version {__version__}")
        default_destination = Path.home() / "Downloads" / "Blackboard"
        self.url_var = tk.StringVar(value=settings.get("url", ""))
        self.destination_var = tk.StringVar(
            value=settings.get("destination", str(default_destination))
        )
        self.remember_var = tk.BooleanVar(
            value=settings.get("remember_session", True)
        )
        self.has_saved_extension_preferences = "extensions" in settings
        self.saved_extensions = set(settings.get("extensions", []))
        self.extension_preferences = {
            extension: True for extension in self.saved_extensions
        }
        self.extension_vars: dict[str, tk.BooleanVar] = {}
        self.extension_checks: list[ttk.Checkbutton] = []
        self.status_var = tk.StringVar(value="Ready to connect")
        self.school_name_var = tk.StringVar(value="No school connected")
        self.user_name_var = tk.StringVar(value="Not signed in")
        self.progress_var = tk.DoubleVar(value=0)

        self._configure_styles()
        self._build_ui()
        self._build_update_menu()
        self.bind("<Escape>", lambda _event: self._cancel_download() if self.downloading else None)
        self.after(100, self._drain_events)
        self.after(300, self._audit_environment)
        self.after(1500, self._scheduled_update_check)

    def _configure_window_icon(self) -> None:
        try:
            self.window_icon_image = tk.PhotoImage(
                file=str(resource_path("assets", "app_icon.png"))
            )
            self.iconphoto(True, self.window_icon_image)
        except tk.TclError:
            self.window_icon_image = None

        if sys.platform == "win32":
            try:
                self.iconbitmap(
                    default=str(resource_path("assets", "app_icon.ico"))
                )
            except tk.TclError:
                pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=(FONT, 10), background=COLORS["bg"], foreground=COLORS["ink"])
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["ink"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure(
            "SurfaceIdentity.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["primary"],
            font=(FONT, 11, "bold"),
        )
        style.configure("Surface.TLabel", background=COLORS["surface"], foreground=COLORS["ink"])
        style.configure("SurfaceMuted.TLabel", background=COLORS["surface"], foreground=COLORS["muted"])
        style.configure("Title.TLabel", font=(FONT, 21, "bold"), foreground=COLORS["ink"])
        style.configure("Section.TLabel", font=(FONT, 12, "bold"), foreground=COLORS["ink"])
        style.configure("Field.TLabel", font=(FONT, 9, "bold"), foreground=COLORS["ink"])
        style.configure(
            "SurfaceSection.TLabel",
            font=(FONT, 12, "bold"),
            background=COLORS["surface"],
            foreground=COLORS["ink"],
        )
        style.configure(
            "Primary.TButton",
            background=COLORS["primary"],
            foreground="white",
            padding=(17, 10),
            borderwidth=0,
            font=(FONT, 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("pressed", COLORS["primary_hover"]), ("active", COLORS["primary_hover"]), ("disabled", "#a8b3ac")],
            foreground=[("disabled", "#edf1ee")],
        )
        style.configure(
            "TButton",
            padding=(13, 9),
            borderwidth=1,
            background=COLORS["bg"],
            foreground=COLORS["ink"],
            bordercolor=COLORS["border"],
        )
        style.map(
            "TButton",
            background=[("pressed", COLORS["surface_hover"]), ("active", COLORS["surface"]), ("disabled", COLORS["surface"])],
            foreground=[("disabled", "#8d9891")],
            bordercolor=[("focus", COLORS["focus"])],
        )
        style.configure(
            "Quiet.TButton",
            padding=(10, 6),
            background=COLORS["bg"],
            borderwidth=0,
            foreground=COLORS["primary"],
            font=(FONT, 9, "bold"),
        )
        style.map("Quiet.TButton", background=[("active", COLORS["primary_soft"]), ("pressed", COLORS["primary_soft"])])
        style.configure(
            "TEntry",
            padding=9,
            fieldbackground=COLORS["bg"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["focus"],
            darkcolor=COLORS["border"],
        )
        style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["ink"])
        style.configure("Surface.TCheckbutton", background=COLORS["surface"], foreground=COLORS["ink"])
        style.map(
            "Surface.TCheckbutton",
            background=[("active", COLORS["surface_hover"]), ("selected", COLORS["surface"])],
            foreground=[("disabled", "#8d9891")],
        )
        style.configure(
            "Course.Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["ink"],
            borderwidth=0,
            relief="flat",
            rowheight=28,
        )
        style.configure(
            "Course.Treeview.Heading",
            background=COLORS["surface_hover"],
            foreground=COLORS["muted"],
            font=(FONT, 9, "bold"),
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "Course.Treeview",
            background=[("selected", COLORS["primary_soft"])],
            foreground=[("selected", COLORS["ink"])],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=COLORS["primary"],
            troughcolor=COLORS["surface_hover"],
            borderwidth=0,
            thickness=7,
        )
        for name, background, foreground in (
            ("Ready", COLORS["surface"], COLORS["muted"]),
            ("Working", COLORS["warning_soft"], COLORS["warning"]),
            ("Success", COLORS["success_soft"], COLORS["success"]),
            ("Error", COLORS["danger_soft"], COLORS["danger"]),
        ):
            style.configure(
                f"{name}.Status.TLabel",
                background=background,
                foreground=foreground,
                padding=(10, 5),
                font=(FONT, 9, "bold"),
            )
        style.configure("TSeparator", background=COLORS["border"])

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(32, 22, 32, 18))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.columnconfigure(1, weight=1)
        try:
            from PIL import Image, ImageTk

            with Image.open(resource_path("assets", "app_icon.png")) as source:
                header_logo = source.convert("RGBA")
                header_logo.thumbnail((44, 44), Image.Resampling.LANCZOS)
            self.header_logo_image = ImageTk.PhotoImage(header_logo)
        except (ImportError, OSError, tk.TclError):
            self.header_logo_image = None

        if self.header_logo_image:
            self.logo_label = tk.Label(
                header,
                image=self.header_logo_image,
                background=COLORS["bg"],
                width=44,
                height=44,
                borderwidth=0,
                highlightthickness=0,
            )
        else:
            self.logo_label = tk.Label(
                header,
                text="↓",
                font=(FONT, 18, "bold"),
                background=COLORS["primary"],
                foreground="white",
                width=2,
                height=1,
            )
        self.logo_label.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
        ttk.Label(header, text="Blackboard Downloader", style="Title.TLabel").grid(row=0, column=1, sticky="w")
        self.forget_button = ttk.Button(
            header,
            text="Forget sign-in",
            style="Quiet.TButton",
            command=self._forget_sign_in,
            state="disabled",
        )
        self.forget_button.grid(row=0, column=2, sticky="e", padx=(0, 8))
        self.forget_button.grid_remove()
        self.status_label = ttk.Label(
            header,
            textvariable=self.status_var,
            style="Ready.Status.TLabel",
        )
        self.status_label.grid(row=0, column=3, sticky="e")
        ttk.Label(
            header,
            text="A tidy offline copy of your course files, in three steps.",
            style="Muted.TLabel",
        ).grid(row=1, column=1, columnspan=3, sticky="w", pady=(3, 0))

        self.connect_panel = ttk.Frame(outer, style="Surface.TFrame", padding=16)
        self.connect_panel.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self.connect_panel.columnconfigure(0, weight=1)
        self._step_header(
            self.connect_panel,
            1,
            "Connect",
            "Your password stays with your school; this session is saved securely on this device.",
            surface=True,
        ).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(
            self.connect_panel,
            text="BLACKBOARD ADDRESS",
            style="SurfaceMuted.TLabel",
            font=(FONT, 8, "bold"),
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 5))
        self.url_entry = ttk.Entry(self.connect_panel, textvariable=self.url_var)
        self.url_entry.grid(row=2, column=0, sticky="ew", padx=(0, 8))
        self.url_entry.bind("<Return>", lambda _event: self._open_login())
        self.open_login_button = ttk.Button(
            self.connect_panel,
            text="Open sign-in",
            style="Primary.TButton",
            command=self._open_login,
        )
        self.open_login_button.grid(row=2, column=1, padx=(0, 8))
        self.signed_in_button = ttk.Button(
            self.connect_panel, text="I’m signed in", command=self._capture_login, state="disabled"
        )
        self.signed_in_button.grid(row=2, column=2)
        self.remember_check = ttk.Checkbutton(
            self.connect_panel,
            text="Remember sign-in on this device",
            variable=self.remember_var,
            command=self._remember_preference_changed,
            style="Surface.TCheckbutton",
        )
        self.remember_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.school_identity_shell = ttk.Frame(
            self.connect_panel, style="Surface.TFrame"
        )
        self.school_identity_shell.grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(11, 0)
        )
        self.school_identity_shell.columnconfigure(1, weight=1)
        ttk.Separator(self.school_identity_shell).grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10)
        )
        self.school_logo_label = tk.Label(
            self.school_identity_shell,
            text="S",
            font=(FONT, 10, "bold"),
            background=COLORS["primary"],
            foreground="white",
            width=3,
            height=2,
        )
        self.school_logo_label.grid(
            row=1, column=0, rowspan=2, sticky="w", padx=(0, 11)
        )
        ttk.Label(
            self.school_identity_shell,
            textvariable=self.school_name_var,
            style="SurfaceIdentity.TLabel",
        ).grid(row=1, column=1, sticky="w")
        ttk.Label(
            self.school_identity_shell,
            textvariable=self.user_name_var,
            style="SurfaceMuted.TLabel",
        ).grid(row=2, column=1, sticky="w", pady=(2, 0))
        self.school_identity_shell.grid_remove()

        controls = self._step_header(
            outer,
            2,
            "Choose courses",
            "Expand a course to choose exactly which content to save.",
        )
        controls.grid(row=2, column=0, sticky="ew", pady=(0, 9))
        controls.columnconfigure(2, weight=1)
        self.selection_label = ttk.Label(controls, text="Connect to load courses", style="Muted.TLabel")
        self.selection_label.grid(row=0, column=3, rowspan=2, padx=(12, 8))
        self.select_all_button = ttk.Button(
            controls,
            text="Select all",
            style="Quiet.TButton",
            command=self._select_all,
            state="disabled",
        )
        self.select_all_button.grid(row=0, column=4, rowspan=2, padx=(6, 0))

        course_shell = ttk.Frame(outer, style="Surface.TFrame")
        course_shell.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        course_shell.columnconfigure(0, weight=1)
        course_shell.rowconfigure(0, weight=1)
        self.course_tree = ttk.Treeview(
            course_shell,
            columns=("details",),
            show="tree headings",
            selectmode="browse",
            style="Course.Treeview",
            height=9,
        )
        self.course_tree.heading("#0", text="Course / content", anchor="w")
        self.course_tree.heading("details", text="Files", anchor="e")
        self.course_tree.column("#0", minwidth=360, width=610, stretch=True)
        self.course_tree.column("details", minwidth=120, width=210, stretch=False, anchor="e")
        self.course_tree.tag_configure(
            "term", foreground=COLORS["primary"], font=(FONT, 10, "bold")
        )
        self.course_tree.tag_configure("muted", foreground=COLORS["muted"])
        scrollbar = ttk.Scrollbar(course_shell, orient="vertical", command=self.course_tree.yview)
        self.course_tree.configure(yscrollcommand=scrollbar.set)
        self.course_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.course_tree.bind("<ButtonRelease-1>", self._tree_clicked)
        self.course_tree.bind("<space>", self._tree_space_pressed)
        self.course_tree.bind("<<TreeviewOpen>>", self._tree_opened)
        self.course_tree.insert(
            "", "end", iid="message", text="Your courses will appear here after you connect."
        )

        download = ttk.Frame(outer)
        download.grid(row=4, column=0, sticky="ew")
        download.columnconfigure(0, weight=1)
        self._step_header(
            download,
            3,
            "Download",
            "File types are discovered from your selected course content.",
        ).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        fields = ttk.Frame(download)
        fields.grid(row=1, column=0, columnspan=3, sticky="ew")
        fields.columnconfigure(0, weight=1)
        save_field = ttk.Frame(fields)
        save_field.grid(row=0, column=0, sticky="ew", padx=(0, 24))
        save_field.columnconfigure(0, weight=1)
        ttk.Label(save_field, text="SAVE TO", style="Field.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        self.destination_entry = ttk.Entry(save_field, textvariable=self.destination_var)
        self.destination_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.browse_button = ttk.Button(save_field, text="Browse…", command=self._choose_destination)
        self.browse_button.grid(row=1, column=1)

        types_field = ttk.Frame(fields)
        types_field.grid(row=0, column=1, sticky="nw")
        ttk.Label(types_field, text="FILE TYPES", style="Field.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.extension_container = ttk.Frame(types_field)
        self.extension_container.grid(row=1, column=0, columnspan=3, sticky="w")
        self.file_types_empty = ttk.Label(
            self.extension_container,
            text="Select course content to discover types",
            style="Muted.TLabel",
        )
        self.file_types_empty.grid(row=0, column=0, sticky="w")

        self.progress = ttk.Progressbar(download, variable=self.progress_var, maximum=100)
        self.progress.grid(row=2, column=0, sticky="ew", padx=(0, 10), pady=(12, 0))
        self.start_button = ttk.Button(download, text="Download selected", style="Primary.TButton", command=self._start_download, state="disabled")
        self.start_button.grid(row=2, column=1, padx=(0, 8), pady=(12, 0))
        self.cancel_button = ttk.Button(download, text="Cancel", command=self._cancel_download, state="disabled")
        self.cancel_button.grid(row=2, column=2, pady=(12, 0))

        activity = ttk.Frame(download, style="Surface.TFrame", padding=(12, 8))
        activity.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        activity.columnconfigure(0, weight=1)
        ttk.Label(
            activity,
            text="ACTIVITY",
            style="SurfaceMuted.TLabel",
            font=(FONT, 8, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.log = tk.Text(
            activity,
            height=2,
            wrap="word",
            font=(FONT_MONO, 9),
            background=COLORS["surface"],
            foreground=COLORS["muted"],
            relief="flat",
            padx=0,
            pady=0,
            borderwidth=0,
            highlightthickness=0,
            state="disabled",
        )
        self.log.grid(row=1, column=0, sticky="ew")
        self._set_log_placeholder("Waiting to connect.")
        ttk.Label(
            download, textvariable=self.update_status, style="Muted.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _step_header(
        self,
        parent,
        number: int,
        title: str,
        description: str,
        surface: bool = False,
    ) -> ttk.Frame:
        style = "Surface.TFrame" if surface else "TFrame"
        label_style = "SurfaceSection.TLabel" if surface else "Section.TLabel"
        muted_style = "SurfaceMuted.TLabel" if surface else "Muted.TLabel"
        frame = ttk.Frame(parent, style=style)
        tk.Label(
            frame,
            text=str(number),
            font=(FONT, 9, "bold"),
            background=COLORS["primary_soft"],
            foreground=COLORS["primary"],
            width=2,
            height=1,
        ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 10))
        ttk.Label(frame, text=title, style=label_style).grid(row=0, column=1, sticky="w")
        ttk.Label(frame, text=description, style=muted_style).grid(row=1, column=1, sticky="w", pady=(1, 0))
        return frame

    def _open_login(self) -> None:
        try:
            base_url = normalize_base_url(self.url_var.get())
        except ValueError as exc:
            messagebox.showerror("Check the address", str(exc), parent=self)
            self.url_entry.focus_set()
            return
        if self.busy:
            return
        self._clear_log()
        self._append_log("Opening your browser for Blackboard sign-in…")
        self._show_course_message("Waiting for sign-in…")
        self._set_busy(True, "Opening browser…", indeterminate=True)
        self.signed_in_button.configure(state="disabled")
        if self.client:
            self.client.close()
        try:
            self.client = BlackboardClient(base_url)
        except BlackboardError as exc:
            self._set_busy(False, "Setup required")
            self.open_login_button.configure(state="disabled")
            self._set_status("Setup required", "error")
            self._append_log(f"Error: {exc}")
            messagebox.showerror("Setup required", str(exc), parent=self)
            return
        threading.Thread(target=self._open_login_worker, daemon=True).start()

    def _audit_environment(self) -> None:
        missing = audit_dependencies()
        if not missing:
            self._try_restore_saved_session()
            return
        names = "\n".join(f"• {dependency.display_name}" for dependency in missing)
        command = install_command()
        self.open_login_button.configure(state="disabled")
        self._set_status("Setup required", "error")
        self._set_log_placeholder(
            "Missing packages: "
            + ", ".join(dependency.package_name for dependency in missing)
        )
        messagebox.showwarning(
            "Install required packages",
            "This Python environment is missing:\n\n"
            f"{names}\n\n"
            "Close the app, run this command, then restart:\n\n"
            f"{command}",
            parent=self,
        )

    def _try_restore_saved_session(self) -> None:
        if (
            self.busy
            or not self.remember_var.get()
            or not self.url_var.get().strip()
        ):
            return
        try:
            base_url = normalize_base_url(self.url_var.get())
            cookies = self.session_store.load(base_url)
        except (ValueError, SessionStoreError) as exc:
            self._append_log(f"Saved sign-in unavailable: {exc}")
            return
        if not cookies:
            return
        try:
            self.client = BlackboardClient(base_url)
        except BlackboardError as exc:
            self._append_log(f"Saved sign-in unavailable: {exc}")
            return
        self.cancel_event.clear()
        self._show_course_message("Restoring your saved sign-in…")
        self._append_log("Restoring the encrypted session from your system credential vault…")
        self._set_busy(True, "Restoring sign-in…", indeterminate=True)
        threading.Thread(
            target=self._restore_session_worker,
            args=(base_url, cookies),
            daemon=True,
        ).start()

    def _restore_session_worker(self, base_url: str, cookies: list[dict]) -> None:
        try:
            assert self.client is not None
            profile = self.client.restore_session(cookies)
            branding = self.branding_cache.load(base_url)
            if branding is None:
                branding = self.client.get_school_branding()
                try:
                    self.branding_cache.save(base_url, branding)
                except OSError:
                    pass
            courses = self.client.get_courses(profile.id, self._queue_progress)
            self.events.put(
                (
                    "courses",
                    courses,
                    profile,
                    branding,
                    True,
                    True,
                    None,
                )
            )
        except AuthenticationError as exc:
            try:
                self.session_store.delete(base_url)
            except SessionStoreError:
                pass
            self.events.put(("restore_failed", exc, True))
        except Exception as exc:
            self.events.put(("restore_failed", exc, False))

    def _open_login_worker(self) -> None:
        try:
            assert self.client is not None
            self.client.open_login()
            self.events.put(("login_open",))
        except Exception as exc:
            self.events.put(("error", exc))

    def _capture_login(self) -> None:
        if not self.client or self.busy:
            return
        self.cancel_event.clear()
        self._show_course_message("Loading your courses…")
        self._append_log("Checking the Blackboard session and loading courses…")
        self._set_busy(True, "Checking sign-in…", indeterminate=True)
        self.signed_in_button.configure(state="disabled")
        threading.Thread(
            target=self._capture_login_worker,
            args=(self.remember_var.get(),),
            daemon=True,
        ).start()

    def _capture_login_worker(self, remember_session: bool) -> None:
        try:
            assert self.client is not None
            profile = self.client.capture_login()
            storage_warning = None
            if remember_session:
                try:
                    self.session_store.save(
                        self.client.base_url, self.client.export_session()
                    )
                except SessionStoreError as exc:
                    storage_warning = str(exc)
            else:
                try:
                    self.session_store.delete(self.client.base_url)
                except SessionStoreError as exc:
                    storage_warning = str(exc)
            branding = self.client.get_school_branding()
            try:
                self.branding_cache.save(self.client.base_url, branding)
            except OSError:
                pass
            courses = self.client.get_courses(profile.id, self._queue_progress)
            self.events.put(
                (
                    "courses",
                    courses,
                    profile,
                    branding,
                    False,
                    remember_session and storage_warning is None,
                    storage_warning,
                )
            )
        except Exception as exc:
            self.events.put(("error", exc))

    def _render_courses(self) -> None:
        self.course_tree.delete(*self.course_tree.get_children())
        self.tree_meta.clear()
        self.tree_states.clear()
        self.tree_labels.clear()
        self.course_tree_items.clear()
        self.content_tree_items.clear()
        self.course_load_states.clear()
        self.pending_course_selection.clear()
        term_items: dict[str, str] = {}
        for course in self.courses:
            term_item = term_items.get(course.term)
            if term_item is None:
                term_item = self.course_tree.insert(
                    "", "end", text=course.term, open=True, tags=("term",)
                )
                term_items[course.term] = term_item
            course_item = self.course_tree.insert(
                term_item,
                "end",
                text=course.name,
                values=(course.code,),
                open=False,
            )
            self.tree_meta[course_item] = ("course", course.id, None)
            self.tree_labels[course_item] = course.name
            self.course_tree_items[course.id] = course_item

            self.course_tree.insert(
                course_item,
                "end",
                text="Expand this course to load its content",
                values=("Not loaded",),
                tags=("muted",),
            )
            self.course_load_states[course.id] = "unloaded"
            self.pending_course_selection[course.id] = None
            self.tree_states[course_item] = 0
            self._refresh_tree_label(course_item)
        if not self.courses:
            self.course_tree.insert(
                "", "end", iid="message", text="No accessible courses were found for this account."
            )
        self._update_selection()

    def _insert_content_node(
        self,
        parent: str,
        course_id: str,
        node: ContentNode,
        selected: bool = False,
    ) -> None:
        counts = self._node_extension_counts(node)
        details = ", ".join(
            f"{self._extension_label(extension)} · {count}"
            for extension, count in sorted(counts.items())
        )
        item = self.course_tree.insert(
            parent,
            "end",
            text=node.title,
            values=(details,),
            open=False,
        )
        self.tree_meta[item] = ("content", course_id, node.id)
        self.tree_states[item] = 1 if selected else 0
        self.tree_labels[item] = node.title
        self.content_tree_items[(course_id, node.id)] = item
        self._refresh_tree_label(item)
        for child in node.children:
            self._insert_content_node(item, course_id, child, selected)

    @classmethod
    def _node_extension_counts(cls, node: ContentNode) -> dict[str, int]:
        counts: dict[str, int] = {}
        for remote in node.files:
            counts[remote.extension] = counts.get(remote.extension, 0) + 1
        for child in node.children:
            for extension, count in cls._node_extension_counts(child).items():
                counts[extension] = counts.get(extension, 0) + count
        return counts

    @staticmethod
    def _extension_label(extension: str) -> str:
        return extension.lstrip(".").upper() if extension else "NO EXT"

    def _show_course_message(self, message: str) -> None:
        self.course_tree.delete(*self.course_tree.get_children())
        self.course_tree.insert("", "end", iid="message", text=message)

    def _select_all(self) -> None:
        should_select = not self.courses or not all(
            self._course_is_effectively_selected(course.id) for course in self.courses
        )
        target = 1 if should_select else 0
        for course in self.courses:
            item = self.course_tree_items.get(course.id)
            if not item:
                continue
            if self.course_load_states.get(course.id) == "loaded":
                self._set_tree_branch(item, target)
            elif should_select:
                self._ensure_course_loaded(course.id, select_after=1)
            else:
                self.pending_course_selection[course.id] = 0
                self.tree_states[item] = 0
                self._refresh_tree_label(item)
        self._update_selection()

    def _course_is_effectively_selected(self, course_id: str) -> bool:
        if self.course_load_states.get(course_id) == "loaded":
            return self.tree_states.get(self.course_tree_items.get(course_id, "")) == 1
        return self.pending_course_selection.get(course_id) == 1

    def _tree_opened(self, _event) -> None:
        item = self.course_tree.focus()
        metadata = self.tree_meta.get(item)
        if metadata and metadata[0] == "course":
            self._ensure_course_loaded(metadata[1])

    def _tree_clicked(self, event) -> None:
        if self.busy or self.course_tree.identify_column(event.x) != "#0":
            return
        item = self.course_tree.identify_row(event.y)
        element = self.course_tree.identify_element(event.x, event.y)
        if not item or item not in self.tree_meta:
            return
        kind, course_id, _ = self.tree_meta[item]
        if "indicator" in element:
            if kind == "course":
                self._ensure_course_loaded(course_id)
            return
        if kind == "course" and self.course_load_states.get(course_id) != "loaded":
            self._ensure_course_loaded(course_id, select_after=1)
            return
        self._toggle_tree_item(item)

    def _tree_space_pressed(self, _event) -> str | None:
        if self.busy:
            return None
        selected = self.course_tree.selection()
        if selected and selected[0] in self.tree_meta:
            kind, course_id, _ = self.tree_meta[selected[0]]
            if kind == "course" and self.course_load_states.get(course_id) != "loaded":
                self._ensure_course_loaded(course_id, select_after=1)
                return "break"
            self._toggle_tree_item(selected[0])
            return "break"
        return None

    def _ensure_course_loaded(
        self, course_id: str, select_after: int | None = None
    ) -> None:
        if self.installing_update:
            return
        state = self.course_load_states.get(course_id)
        if state == "loaded":
            if select_after is not None:
                item = self.course_tree_items.get(course_id)
                if item:
                    self._set_tree_branch(item, select_after)
                    self._update_selection()
            return
        if state == "loading":
            if select_after is not None:
                self.pending_course_selection[course_id] = select_after
            return
        course = next((item for item in self.courses if item.id == course_id), None)
        tree_item = self.course_tree_items.get(course_id)
        if course is None or tree_item is None or self.client is None:
            return

        self.course_load_states[course_id] = "loading"
        self.pending_course_selection[course_id] = select_after
        self.tree_states[tree_item] = 0
        self._refresh_tree_label(tree_item)
        loading_detail = f"{course.code} · Loading…" if course.code else "Loading…"
        self.course_tree.item(tree_item, open=True, values=(loading_detail,))
        for child in self.course_tree.get_children(tree_item):
            self.course_tree.delete(child)
        self.course_tree.insert(
            tree_item,
            "end",
            text="Loading document files…",
            values=("Please wait",),
            tags=("muted",),
        )
        self._append_log(f"Loading content for {course.name}…")
        cancel = threading.Event()
        self.content_cancel_events[course_id] = cancel
        threading.Thread(
            target=self._load_course_content_worker,
            args=(course, self.client, cancel),
            daemon=True,
        ).start()

    def _load_course_content_worker(
        self,
        course: Course,
        client: BlackboardClient,
        cancel: threading.Event,
    ) -> None:
        worker: BlackboardClient | None = None
        try:
            with self.content_load_slots:
                if cancel.is_set():
                    return
                worker = client.create_worker_client(self.content_request_slots)
                contents = worker.get_course_content(course, cancel)
            self.events.put(("course_content", course.id, contents, None))
        except InterruptedError:
            return
        except Exception as exc:
            self.events.put(("course_content", course.id, (), exc))
        finally:
            if worker is not None:
                worker.close()

    def _apply_loaded_course(
        self,
        course_id: str,
        contents: tuple[ContentNode, ...],
        error: Exception | None,
    ) -> None:
        tree_item = self.course_tree_items.get(course_id)
        course = next((item for item in self.courses if item.id == course_id), None)
        if tree_item is None or course is None:
            return
        for child in self.course_tree.get_children(tree_item):
            self._forget_tree_branch(child)
            self.course_tree.delete(child)

        selected = self.pending_course_selection.get(course_id) == 1
        self.content_cancel_events.pop(course_id, None)
        if error is not None:
            self.course_load_states[course_id] = "error"
            self.content_errors[course_id] = str(error)
            error_detail = f"{course.code} · Unavailable" if course.code else "Unavailable"
            self.course_tree.item(tree_item, values=(error_detail,))
            self.course_tree.insert(
                tree_item,
                "end",
                text="Content could not be loaded — click the course to retry",
                values=("Unavailable",),
                tags=("muted",),
            )
            self.tree_states[tree_item] = 0
            self._append_log(f"Could not load {course.name}: {error}")
        else:
            self.course_load_states[course_id] = "loaded"
            self.content_errors.pop(course_id, None)
            self.course_contents[course_id] = contents
            self.course_tree.item(tree_item, values=(course.code,))
            if contents:
                for node in contents:
                    self._insert_content_node(
                        tree_item, course_id, node, selected=selected
                    )
                self.tree_states[tree_item] = 1 if selected else 0
                self._append_log(f"Loaded document files from {course.name}.")
            else:
                self.course_tree.insert(
                    tree_item,
                    "end",
                    text="No document files found",
                    values=("Empty",),
                    tags=("muted",),
                )
                self.tree_states[tree_item] = 0
                self._append_log(f"No document files were found in {course.name}.")
        self._refresh_tree_label(tree_item)
        self.course_tree.item(tree_item, open=True)
        self._update_selection()

    def _forget_tree_branch(self, item: str) -> None:
        for child in self.course_tree.get_children(item):
            self._forget_tree_branch(child)
        metadata = self.tree_meta.pop(item, None)
        self.tree_states.pop(item, None)
        self.tree_labels.pop(item, None)
        if metadata and metadata[0] == "content" and metadata[2] is not None:
            self.content_tree_items.pop((metadata[1], metadata[2]), None)

    def _toggle_tree_item(self, item: str) -> None:
        target = 0 if self.tree_states.get(item) == 1 else 1
        self._set_tree_branch(item, target)
        parent = self.course_tree.parent(item)
        while parent and parent in self.tree_meta:
            child_states = [
                self.tree_states[child]
                for child in self.course_tree.get_children(parent)
                if child in self.tree_states
            ]
            if child_states and all(state == 1 for state in child_states):
                state = 1
            elif child_states and all(state == 0 for state in child_states):
                state = 0
            else:
                state = 2
            self.tree_states[parent] = state
            self._refresh_tree_label(parent)
            parent = self.course_tree.parent(parent)
        self._update_selection()

    def _set_tree_branch(self, item: str, state: int) -> None:
        if item in self.tree_states:
            self.tree_states[item] = state
            self._refresh_tree_label(item)
        for child in self.course_tree.get_children(item):
            self._set_tree_branch(child, state)

    def _refresh_tree_label(self, item: str) -> None:
        symbols = {0: "☐", 1: "☑", 2: "◩"}
        self.course_tree.item(
            item,
            text=f"{symbols[self.tree_states[item]]}  {self.tree_labels[item]}",
        )

    def _update_selection(self) -> None:
        selected_courses = sum(
            self._course_is_effectively_selected(course.id)
            or self.tree_states.get(self.course_tree_items.get(course.id, ""), 0) == 2
            for course in self.courses
        )
        selected_parts = sum(
            self.tree_states.get(item, 0) == 1
            for item in self.content_tree_items.values()
        )
        total = len(self.course_tree_items)
        if total:
            self.selection_label.configure(
                text=f"{selected_courses} course{'s' if selected_courses != 1 else ''} · "
                f"{selected_parts} part{'s' if selected_parts != 1 else ''}"
            )
        else:
            self.selection_label.configure(text="No courses")
        self.select_all_button.configure(state="normal" if total and not self.busy else "disabled")
        all_selected = bool(total) and all(
            self._course_is_effectively_selected(course.id) for course in self.courses
        )
        self.select_all_button.configure(text="Clear all" if all_selected else "Select all")
        self._render_extension_filters()

    def _render_extension_filters(self) -> None:
        for extension, variable in self.extension_vars.items():
            self.extension_preferences[extension] = variable.get()
        for child in self.extension_container.winfo_children():
            child.destroy()
        self.extension_vars.clear()
        self.extension_checks.clear()

        counts: dict[str, int] = {}

        def collect(course_id: str, node: ContentNode) -> None:
            item = self.content_tree_items.get((course_id, node.id))
            if item and self.tree_states.get(item) == 1:
                for remote in node.files:
                    counts[remote.extension] = counts.get(remote.extension, 0) + 1
            for child in node.children:
                collect(course_id, child)

        for course in self.courses:
            for root in self.course_contents.get(course.id, ()):
                collect(course.id, root)

        if not counts:
            self.file_types_empty = ttk.Label(
                self.extension_container,
                text="No files in the selected content",
                style="Muted.TLabel",
            )
            self.file_types_empty.grid(row=0, column=0, sticky="w")
            self.start_button.configure(state="disabled")
            return

        for index, extension in enumerate(sorted(counts, key=lambda value: (not value, value))):
            if extension in self.extension_preferences:
                selected = self.extension_preferences[extension]
            elif self.has_saved_extension_preferences:
                selected = extension in self.saved_extensions
            else:
                selected = True
            variable = tk.BooleanVar(value=selected)
            self.extension_vars[extension] = variable
            check = ttk.Checkbutton(
                self.extension_container,
                text=f"{self._extension_label(extension)} ({counts[extension]})",
                variable=variable,
                command=lambda ext=extension, var=variable: self._extension_changed(ext, var),
            )
            check.grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 12))
            check.configure(state="disabled" if self.busy else "normal")
            self.extension_checks.append(check)
        self._update_download_button()

    def _extension_changed(self, extension: str, variable: tk.BooleanVar) -> None:
        self.extension_preferences[extension] = variable.get()
        self._update_download_button()

    def _update_download_button(self) -> None:
        has_content = any(
            self.tree_states.get(item) == 1
            for item in self.content_tree_items.values()
        )
        has_types = any(variable.get() for variable in self.extension_vars.values())
        self.start_button.configure(
            state="normal" if has_content and has_types and not self.busy else "disabled"
        )

    def _choose_destination(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Choose download folder",
            initialdir=self.destination_var.get() or str(Path.home()),
        )
        if chosen:
            self.destination_var.set(chosen)

    def _start_download(self) -> None:
        selected: list[CourseSelection] = []
        for course in self.courses:
            selected_ids = {
                node_id
                for (course_id, node_id), item in self.content_tree_items.items()
                if course_id == course.id and self.tree_states.get(item) == 1
            }
            contents = filter_content_nodes(
                self.course_contents.get(course.id, ()), selected_ids
            )
            if contents:
                selected.append(CourseSelection(course=course, contents=contents))
        extensions = {extension for extension, variable in self.extension_vars.items() if variable.get()}
        destination = Path(self.destination_var.get()).expanduser()
        if not selected:
            messagebox.showinfo("Choose courses", "Select at least one course.", parent=self)
            return
        if not extensions:
            messagebox.showinfo("Choose file types", "Select at least one file type.", parent=self)
            return
        if not self.destination_var.get().strip():
            messagebox.showinfo("Choose a folder", "Choose where downloads should be saved.", parent=self)
            return
        self._save_settings()
        self.cancel_event.clear()
        self.progress_var.set(0)
        self._clear_log()
        self._append_log(
            f"Preparing content from {len(selected)} selected course{'s' if len(selected) != 1 else ''}…"
        )
        self.downloading = True
        self._set_busy(
            True,
            f"Starting {len(selected)} course{'s' if len(selected) != 1 else ''}…",
        )
        self.cancel_button.configure(state="normal")
        threading.Thread(
            target=self._download_worker,
            args=(selected, destination, extensions),
            daemon=True,
        ).start()

    def _download_worker(
        self,
        selections: list[CourseSelection],
        destination: Path,
        extensions: set[str],
    ) -> None:
        try:
            assert self.client is not None
            result = self.client.download_courses(
                selections,
                destination,
                extensions,
                self._queue_progress,
                self.cancel_event,
                self.content_request_slots,
            )
            self.events.put(("done", *result, destination))
        except InterruptedError:
            self.events.put(("cancelled",))
        except Exception as exc:
            self.events.put(("error", exc))

    def _cancel_download(self) -> None:
        if not self.downloading:
            return
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self._set_status("Cancelling safely…", "working")

    def _build_update_menu(self) -> None:
        menu = tk.Menu(self)
        updates = tk.Menu(menu, tearoff=False)
        updates.add_command(label=f"Version {__version__}", state="disabled")
        updates.add_command(label="Check for updates", command=self._check_for_updates)
        updates.add_command(label="Install available update", command=self._offer_update)
        updates.add_separator()
        updates.add_checkbutton(
            label="Check automatically", variable=self.check_updates_var,
            command=self._save_settings,
        )
        updates.add_checkbutton(
            label="Install automatically when idle", variable=self.auto_update_var,
            command=self._auto_update_changed,
            state="normal" if automatic_install_supported() else "disabled",
        )
        updates.add_command(
            label="View releases", command=lambda: webbrowser.open(
                "https://github.com/KNN-07/Blackboard-Downloader/releases"
            ),
        )
        menu.add_cascade(label="Updates", menu=updates)
        self.configure(menu=menu)

    def _auto_update_changed(self) -> None:
        if self.auto_update_var.get():
            if not messagebox.askyesno(
                "Enable automatic updates?",
                "New stable releases will be downloaded, verified, and installed when "
                "Blackboard work is idle. The app will close for installation and restart. "
                "Your operating system may ask for permission.",
                parent=self,
            ):
                self.auto_update_var.set(False)
            else:
                self.check_updates_var.set(True)
        self._save_settings()

    def _scheduled_update_check(self) -> None:
        if self.check_updates_var.get():
            self._check_for_updates(manual=False)
        self.after(24 * 60 * 60 * 1000, self._scheduled_update_check)

    def _check_for_updates(self, manual: bool = True) -> None:
        if self.update_running or self.pending_update is not None:
            if manual:
                messagebox.showinfo("Updates", self.update_status.get(), parent=self)
            return
        self.update_running = True
        self.update_status.set("Checking for updates…")

        def worker() -> None:
            try:
                self.events.put(("update_checked", check_release(__version__), manual))
            except Exception as exc:
                self.events.put(("update_error", str(exc), manual))

        threading.Thread(target=worker, daemon=True, name="bb-update-check").start()

    def _update_idle(self) -> bool:
        return (
            not self.busy
            and not self.downloading
            and str(self.signed_in_button["state"]) == "disabled"
            and not any(state == "loading" for state in self.course_load_states.values())
        )

    def _offer_update(self) -> None:
        if self.update_running or self.pending_update is not None:
            messagebox.showinfo("Updates", self.update_status.get(), parent=self)
            return
        release = self.available_update
        if release is None:
            self._check_for_updates()
            return
        if not automatic_install_supported():
            if messagebox.askyesno(
                "Update available",
                f"Version {release.version} is available (installed: {__version__}).\n\n"
                "This source checkout or installation cannot be replaced automatically. "
                "Open the release page for installation instructions?",
                parent=self,
            ):
                webbrowser.open(release.url)
            return
        if messagebox.askyesno(
            "Install update?",
            f"Download and install version {release.version}?\n\n"
            "The app will close and restart once Blackboard work is idle. "
            "Operating-system permission may be required.",
            parent=self,
        ):
            self._download_update(release)

    def _download_update(self, release: Release) -> None:
        if self.update_running:
            return
        self.update_running = True
        self.update_cancel.clear()
        self.update_status.set(f"Downloading version {release.version}…")
        self._append_log(self.update_status.get())

        def worker() -> None:
            try:
                path = download_update(
                    release, self.update_cancel,
                    lambda current, total: self.events.put(
                        ("update_progress", current, total)
                    ),
                )
                self.events.put(("update_ready", path))
            except Exception as exc:
                self.events.put(("update_error", str(exc), True))

        threading.Thread(target=worker, daemon=True, name="bb-update-download").start()

    def _install_pending_update(self) -> None:
        if self.pending_update is None or self.installing_update:
            return
        if not self._update_idle():
            self.after(1000, self._install_pending_update)
            return
        path = self.pending_update
        self.installing_update = True
        self.update_running = True
        self.update_status.set("Preparing update installation…")
        self._set_busy(True, "Preparing update installation…", indeterminate=True)

        def worker() -> None:
            try:
                install_update(path)
                self.events.put(("update_install_started",))
            except Exception as exc:
                self.events.put(("update_install_error", str(exc)))

        threading.Thread(target=worker, daemon=True, name="bb-update-install").start()

    def _handle_update_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "update_checked":
            _, release, manual = event
            self.update_running = False
            self.available_update = release
            if release is None:
                self.update_status.set(f"Version {__version__} is up to date")
                if manual:
                    messagebox.showinfo("Updates", self.update_status.get(), parent=self)
                return
            self.update_status.set(f"Version {release.version} is available")
            self._append_log(self.update_status.get() + " — use the Updates menu to install.")
            if self.auto_update_var.get() and automatic_install_supported():
                self._download_update(release)
            elif manual:
                self._offer_update()
        elif kind == "update_progress":
            _, current, total = event
            self.update_status.set(
                f"Downloading update: {current * 100 // total}%"
                if total else f"Downloading update: {current // 1024} KB"
            )
        elif kind == "update_ready":
            self.update_running = False
            self.pending_update = event[1]
            self.update_status.set("Update verified; waiting for Blackboard work to finish")
            self._append_log(self.update_status.get())
            self.after(100, self._install_pending_update)
        elif kind == "update_install_started":
            self.installing_update = False
            self.pending_update = None
            self.after_idle(self._on_close)
        elif kind == "update_install_error":
            self.installing_update = False
            self.update_running = False
            self.pending_update = None
            self._set_busy(False, "Update installation failed")
            self.update_status.set("Update installation failed")
            self._append_log(f"Update installation failed: {event[1]}")
            messagebox.showerror("Could not install update", event[1], parent=self)
        elif kind == "update_error":
            _, error, manual = event
            self.update_running = False
            self.update_status.set("Update failed")
            self._append_log(f"Update failed: {error}")
            if manual:
                messagebox.showerror("Update failed", error, parent=self)

    def _queue_progress(self, kind: str, message: str, current: int, total: int) -> None:
        self.events.put(("progress", kind, message, current, total))

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind.startswith("update_"):
                    self._handle_update_event(event)
                    continue
                if kind == "login_open":
                    self._set_busy(False, "Waiting for sign-in")
                    self._set_status("Waiting for sign-in", "working")
                    self.signed_in_button.configure(state="normal")
                    self._append_log("Your browser is open. Finish signing in, then click “I’m signed in”.")
                elif kind == "courses":
                    (
                        _,
                        courses,
                        profile,
                        branding,
                        restored,
                        remembered,
                        storage_warning,
                    ) = event
                    self.courses = courses
                    self.course_contents = {}
                    self.content_errors = {}
                    self.profile = profile
                    self.branding = branding
                    self._render_courses()
                    self._set_busy(False, f"Connected · {len(self.courses)} courses found")
                    self._set_status(f"Connected · {len(self.courses)} courses", "success")
                    self._apply_identity(profile, branding)
                    self.forget_button.grid()
                    self.forget_button.configure(state="normal")
                    if restored:
                        connection_message = (
                            f"Restored sign-in for {profile.display_name}."
                        )
                    elif remembered:
                        connection_message = (
                            f"Saved sign-in for {profile.display_name}."
                        )
                    else:
                        connection_message = (
                            f"Signed in as {profile.display_name} without saving the session."
                        )
                    self._append_log(
                        f"{connection_message} Found {len(self.courses)} available course(s). "
                        "Open a course to load its document files."
                    )
                    if storage_warning:
                        self.remember_var.set(False)
                        self._append_log(f"Session was not saved: {storage_warning}")
                        messagebox.showwarning(
                            "Sign-in will not be remembered",
                            storage_warning,
                            parent=self,
                        )
                    self._save_settings()
                elif kind == "restore_failed":
                    _, error, expired = event
                    if self.client:
                        self.client.close()
                    self.client = None
                    self._set_busy(False, "Sign-in required")
                    self._set_status("Sign-in required", "ready")
                    if expired:
                        self._show_course_message("Your saved session expired. Sign in again to continue.")
                        self._append_log(
                            "The saved Blackboard session expired and was removed."
                        )
                    else:
                        self._show_course_message("The saved sign-in could not be checked. You can retry or sign in again.")
                        self._append_log(f"Could not restore the saved sign-in: {error}")
                    self.open_login_button.configure(state="normal")
                elif kind == "course_content":
                    _, course_id, contents, error = event
                    self._apply_loaded_course(course_id, contents, error)
                elif kind == "progress":
                    _, progress_kind, message, current, total = event
                    self._set_status(message, "working")
                    if progress_kind == "log":
                        self._append_log(message)
                    if total:
                        self.progress_var.set(current / total * 100)
                elif kind == "done":
                    _, downloaded, skipped, unavailable, destination = event
                    self.progress_var.set(100)
                    self.downloading = False
                    summary = f"Done · {downloaded} downloaded, {skipped} already present"
                    if unavailable:
                        summary += f", {unavailable} unavailable"
                    self._set_busy(False, summary)
                    self._set_status(
                        f"Done · {downloaded} downloaded"
                        + (f", {unavailable} unavailable" if unavailable else ""),
                        "working" if unavailable else "success",
                    )
                    self.cancel_button.configure(state="disabled")
                    if unavailable:
                        self._append_log(
                            f"Skipped {unavailable} file(s) that Blackboard no longer provides."
                        )
                    self._append_log(f"Saved to {destination}")
                    if messagebox.askyesno(
                        "Download complete",
                        f"Downloaded {downloaded} file(s).\n"
                        f"Skipped {skipped} existing file(s).\n"
                        f"Unavailable on Blackboard: {unavailable}.\n\n"
                        "Open the folder?",
                        parent=self,
                    ):
                        self._open_folder(destination)
                elif kind == "cancelled":
                    self.downloading = False
                    self._set_busy(False, "Download cancelled")
                    self._set_status("Download cancelled", "ready")
                    self.cancel_button.configure(state="disabled")
                    self._append_log("Cancelled. No partial file was kept.")
                elif kind == "error":
                    error = event[1]
                    self.downloading = False
                    self._set_busy(False, "Something went wrong")
                    self._set_status("Needs attention", "error")
                    self.cancel_button.configure(state="disabled")
                    self.signed_in_button.configure(state="normal" if self.client and self.client.driver else "disabled")
                    friendly = str(error) if isinstance(error, (BlackboardError, ValueError)) else "An unexpected error occurred. Please try again."
                    self._append_log(f"Error: {friendly}")
                    messagebox.showerror("Blackboard Downloader", friendly, parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _set_busy(self, busy: bool, status: str, indeterminate: bool = False) -> None:
        self.busy = busy
        self._set_status(status, "working" if busy else "ready")
        if busy and indeterminate:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
        self.open_login_button.configure(state="disabled" if busy else "normal")
        self.forget_button.configure(
            state="disabled" if busy or not self.profile else "normal"
        )
        self.url_entry.configure(state="disabled" if busy else "normal")
        self.destination_entry.configure(state="disabled" if busy else "normal")
        self.browse_button.configure(state="disabled" if busy else "normal")
        self.remember_check.configure(state="disabled" if busy else "normal")
        for check in self.extension_checks:
            check.configure(state="disabled" if busy else "normal")
        self._update_selection()

    def _set_status(self, message: str, tone: str) -> None:
        styles = {
            "ready": "Ready.Status.TLabel",
            "working": "Working.Status.TLabel",
            "success": "Success.Status.TLabel",
            "error": "Error.Status.TLabel",
        }
        self.status_var.set(message)
        self.status_label.configure(style=styles.get(tone, styles["ready"]))

    def _apply_identity(
        self, profile: UserProfile, branding: SchoolBranding
    ) -> None:
        self.school_name_var.set(branding.name)
        self.user_name_var.set(f"Signed in as {profile.display_name}")
        self.title(f"{branding.name} — Blackboard Downloader")
        self.school_identity_shell.grid()
        if branding.logo_bytes:
            try:
                from PIL import Image, ImageTk

                image = Image.open(BytesIO(branding.logo_bytes)).convert("RGBA")
                # Keep wide institution marks readable: constrain their height
                # without forcing them into a square or enlarging small images.
                image.thumbnail((image.width, 42), Image.Resampling.LANCZOS)
                self.school_logo_image = ImageTk.PhotoImage(image)
                self.school_logo_label.configure(
                    image=self.school_logo_image,
                    text="",
                    width=image.width,
                    height=image.height,
                    background=COLORS["surface"],
                )
                return
            except Exception:
                pass
        initials = "".join(
            word[0] for word in branding.name.split() if word
        )[:2].upper() or "B"
        self.school_logo_image = None
        self.school_logo_label.configure(
            image="",
            text=initials,
            font=(FONT, 10, "bold"),
            background=COLORS["primary"],
            foreground="white",
            width=3,
            height=2,
        )

    def _reset_identity(self) -> None:
        self.profile = None
        self.branding = None
        self.school_logo_image = None
        self.school_name_var.set("No school connected")
        self.user_name_var.set("Not signed in")
        self.title("Blackboard Downloader")
        self.school_logo_label.configure(
            image="",
            text="S",
            font=(FONT, 10, "bold"),
            background=COLORS["primary"],
            foreground="white",
            width=3,
            height=2,
        )
        self.school_identity_shell.grid_remove()
        self.forget_button.configure(state="disabled")
        self.forget_button.grid_remove()

    def _forget_sign_in(self) -> None:
        if not self.profile or self.busy:
            return
        if not messagebox.askyesno(
            "Forget saved sign-in?",
            "This removes the saved Blackboard session from your system credential vault. "
            "Your school password was never stored.",
            parent=self,
        ):
            return
        try:
            base_url = normalize_base_url(self.url_var.get())
            self.session_store.delete(base_url)
            self.branding_cache.delete(base_url)
        except (ValueError, SessionStoreError) as exc:
            messagebox.showerror("Could not forget sign-in", str(exc), parent=self)
            return
        if self.client:
            self.client.close()
        for cancel in self.content_cancel_events.values():
            cancel.set()
        self.content_cancel_events.clear()
        self.client = None
        self.courses = []
        self.course_contents.clear()
        self.content_errors.clear()
        self.remember_var.set(False)
        self._reset_identity()
        self._render_courses()
        self.signed_in_button.configure(state="disabled")
        self.progress_var.set(0)
        self._set_status("Sign-in forgotten", "ready")
        self._set_log_placeholder("Saved sign-in removed. Connect again when ready.")
        self._save_settings()

    def _remember_preference_changed(self) -> None:
        self._save_settings()
        if not self.profile or not self.client:
            return
        try:
            if self.remember_var.get():
                self.session_store.save(
                    self.client.base_url, self.client.export_session()
                )
                self._append_log("This sign-in will be remembered on this device.")
            else:
                self.session_store.delete(self.client.base_url)
                self._append_log(
                    "Saved sign-in removed. The current session remains active until you close the app."
                )
        except SessionStoreError as exc:
            self.remember_var.set(not self.remember_var.get())
            self._save_settings()
            messagebox.showwarning(
                "Could not change sign-in preference", str(exc), parent=self
            )

    @staticmethod
    def _open_folder(path: Path) -> None:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        if not self.log_has_content:
            self.log.delete("1.0", "end")
            self.log_has_content = True
        self.log.insert("end", f"{message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.log_has_content = False

    def _set_log_placeholder(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", message)
        self.log.configure(state="disabled")
        self.log_has_content = False

    @staticmethod
    def _settings_path() -> Path:
        if sys.platform == "win32":
            root = Path(os.environ.get("APPDATA", Path.home())) / "Blackboard Downloader"
        elif sys.platform == "darwin":
            root = Path.home() / "Library" / "Application Support" / "Blackboard Downloader"
        else:
            config_root = Path(
                os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
            )
            root = config_root / "blackboard-downloader"
        return root / "settings.json"

    def _load_settings(self) -> dict:
        try:
            return json.loads(self._settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        path = self._settings_path()
        try:
            for extension, variable in self.extension_vars.items():
                self.extension_preferences[extension] = variable.get()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "url": self.url_var.get().strip(),
                        "destination": self.destination_var.get().strip(),
                        "remember_session": self.remember_var.get(),
                        "check_updates": self.check_updates_var.get(),
                        "auto_update": self.auto_update_var.get(),
                        "extensions": sorted(
                            extension
                            for extension, selected in self.extension_preferences.items()
                            if selected
                        ),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _on_close(self) -> None:
        if self.installing_update:
            messagebox.showinfo(
                "Preparing update", "Please wait for update preparation to finish.",
                parent=self,
            )
            return
        self.update_cancel.set()
        self.cancel_event.set()
        for cancel in self.content_cancel_events.values():
            cancel.set()
        self._save_settings()
        if self.client:
            self.client.close()
        self.destroy()


def main() -> None:
    configure_process_identity()
    BlackboardApp().mainloop()
