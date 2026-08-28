#!/usr/bin/env python3

import io
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageTk, ImageOps


# ============================================================
# KONFIGURACJA
# ============================================================

GPHOTO2 = "gphoto2"

SAVE_DIR = Path.home() / "gphoto_gui_photos"
SAVE_DIR.mkdir(parents=True, exist_ok=True)

LIVE_PREVIEW_DELAY_MS = 150

# Aplikacja szuka tych nazw w:
#     gphoto2 --list-config
#
# Różni producenci używają czasami różnych nazw.
SETTING_DEFINITIONS = [
    (
        "Czas",
        [
            "shutterspeed",
            "shutterspeed2",
        ],
    ),
    (
        "Przysłona",
        [
            "f-number",
            "aperture",
        ],
    ),
    (
        "ISO",
        [
            "iso",
        ],
    ),
    (
        "Komp. eksp.",
        [
            "exposurecompensation",
            "exposurecompensation2",
        ],
    ),
    (
        "Balans bieli",
        [
            "whitebalance",
        ],
    ),
]


# ============================================================
# GPHOTO2
# ============================================================

class GPhotoError(Exception):
    pass


class GPhotoController:
    def __init__(self):
        if shutil.which(GPHOTO2) is None:
            raise GPhotoError(
                "Nie znaleziono programu gphoto2.\n\n"
                "Zainstaluj go np.:\n"
                "sudo apt install gphoto2"
            )

    def run(self, *args, binary=False, timeout=30):
        cmd = [GPHOTO2, *args]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise GPhotoError(
                f"Timeout polecenia:\n{' '.join(cmd)}"
            )

        if result.returncode != 0:
            error = result.stderr.decode(
                "utf-8",
                errors="replace"
            ).strip()

            raise GPhotoError(
                error or f"gphoto2 zakończył się kodem {result.returncode}"
            )

        if binary:
            return result.stdout

        return result.stdout.decode(
            "utf-8",
            errors="replace"
        )

    def autodetect(self):
        return self.run("--auto-detect")

    def list_config(self):
        text = self.run("--list-config")

        return [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("/")
        ]

    def get_config(self, path):
        text = self.run("--get-config", path)

        result = {
            "path": path,
            "label": path,
            "type": None,
            "current": None,
            "choices": [],
            "bottom": None,
            "top": None,
            "step": None,
        }

        for line in text.splitlines():
            line = line.strip()

            if line.startswith("Label:"):
                result["label"] = line.split(":", 1)[1].strip()

            elif line.startswith("Type:"):
                result["type"] = line.split(":", 1)[1].strip().upper()

            elif line.startswith("Current:"):
                result["current"] = line.split(":", 1)[1].strip()

            elif line.startswith("Choice:"):
                # Niektóre wersje mogą mieć inny format,
                # ale standardowo:
                #
                # Choice: 0 1/100
                #
                parts = line.split(None, 2)

                if len(parts) >= 3:
                    index = int(parts[1])
                    value = parts[2]

                    result["choices"].append(
                        (index, value)
                    )

            elif line.startswith("Bottom:"):
                try:
                    result["bottom"] = float(
                        line.split(":", 1)[1].strip()
                    )
                except ValueError:
                    pass

            elif line.startswith("Top:"):
                try:
                    result["top"] = float(
                        line.split(":", 1)[1].strip()
                    )
                except ValueError:
                    pass

            elif line.startswith("Step:"):
                try:
                    result["step"] = float(
                        line.split(":", 1)[1].strip()
                    )
                except ValueError:
                    pass

        return result

    def set_config(self, path, value):
        self.run(
            "--set-config",
            f"{path}={value}",
            timeout=15,
        )

    def capture_preview(self):
        data = self.run(
            "--capture-preview",
            "--stdout",
            binary=True,
            timeout=15,
        )

        if not data:
            raise GPhotoError(
                "Aparat nie zwrócił obrazu Live View."
            )

        try:
            image = Image.open(io.BytesIO(data))
            image.load()
            return image.copy()

        except Exception as exc:
            raise GPhotoError(
                f"Nie można odczytać obrazu Live View: {exc}"
            )

    def capture_photo(self):
        filename = SAVE_DIR / datetime.now().strftime(
            "photo_%Y-%m-%d_%H-%M-%S.jpg"
        )

        self.run(
            "--force-overwrite",
            "--filename",
            str(filename),
            "--capture-image-and-download",
            timeout=60,
        )

        if not filename.exists():
            raise GPhotoError(
                "gphoto2 wykonał zdjęcie, ale plik nie został znaleziony."
            )

        return filename


# ============================================================
# WIDGET USTAWIENIA APARATU
# ============================================================

class CameraSettingWidget(ttk.Frame):
    def __init__(
        self,
        parent,
        app,
        display_name,
        config,
    ):
        super().__init__(parent)

        self.app = app
        self.display_name = display_name
        self.config = config

        self.pending_after = None
        self.internal_change = False

        self.columnconfigure(1, weight=1)

        ttk.Label(
            self,
            text=display_name,
            width=15,
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 10),
        )

        self.value_label = ttk.Label(
            self,
            text="",
            width=14,
            anchor="center",
        )

        self.value_label.grid(
            row=0,
            column=2,
            padx=(10, 0),
        )

        config_type = config["type"]

        if config["choices"]:
            self.mode = "choice"
            self.choices = config["choices"]

            max_index = len(self.choices) - 1

            self.variable = tk.DoubleVar()

            self.scale = ttk.Scale(
                self,
                from_=0,
                to=max_index,
                variable=self.variable,
                command=self.on_slider,
            )

            self.scale.grid(
                row=0,
                column=1,
                sticky="ew",
            )

            current_index = 0

            for i, (_, value) in enumerate(self.choices):
                if value == config["current"]:
                    current_index = i
                    break

            self.internal_change = True
            self.variable.set(current_index)
            self.internal_change = False

            self.update_value_label(current_index)

        elif (
            config["bottom"] is not None
            and config["top"] is not None
        ):
            self.mode = "range"

            self.minimum = config["bottom"]
            self.maximum = config["top"]
            self.step = config["step"] or 1.0

            try:
                current = float(config["current"])
            except (TypeError, ValueError):
                current = self.minimum

            self.variable = tk.DoubleVar(value=current)

            self.scale = ttk.Scale(
                self,
                from_=self.minimum,
                to=self.maximum,
                variable=self.variable,
                command=self.on_slider,
            )

            self.scale.grid(
                row=0,
                column=1,
                sticky="ew",
            )

            self.update_value_label(current)

        else:
            self.mode = "unsupported"

            ttk.Label(
                self,
                text=f"Nieobsługiwany typ: {config_type}",
            ).grid(
                row=0,
                column=1,
                sticky="w",
            )

    def update_value_label(self, raw_value):
        if self.mode == "choice":
            index = int(round(float(raw_value)))
            index = max(0, min(index, len(self.choices) - 1))

            value = self.choices[index][1]

        else:
            value = float(raw_value)

            if self.step:
                value = round(
                    value / self.step
                ) * self.step

            if value.is_integer():
                value = int(value)

        self.value_label.config(text=str(value))

    def on_slider(self, raw_value):
        if self.internal_change:
            return

        self.update_value_label(raw_value)

        # Nie wysyłamy polecenia przy KAŻDYM pikselu ruchu.
        # Czekamy chwilę, aż użytkownik przestanie ruszać suwakiem.

        if self.pending_after is not None:
            self.after_cancel(self.pending_after)

        self.pending_after = self.after(
            300,
            self.send_value,
        )

    def send_value(self):
        self.pending_after = None

        if self.mode == "choice":
            index = int(round(self.variable.get()))
            index = max(0, min(index, len(self.choices) - 1))

            # Przyciągnij suwak dokładnie do indeksu.
            self.internal_change = True
            self.variable.set(index)
            self.internal_change = False

            value = self.choices[index][1]

        elif self.mode == "range":
            value = self.variable.get()

            if self.step:
                value = round(
                    value / self.step
                ) * self.step

            value = max(
                self.minimum,
                min(self.maximum, value),
            )

        else:
            return

        self.app.set_camera_config(
            self.config["path"],
            value,
            self.display_name,
        )


# ============================================================
# GUI
# ============================================================

class GPhotoGUI:
    def __init__(self, root):
        self.root = root

        self.root.title("gphoto2 Camera Control")
        self.root.geometry("1200x760")
        self.root.minsize(850, 600)

        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="gphoto",
        )

        self.live_enabled = False
        self.closing = False

        self.live_photoimage = None
        self.last_photoimage = None

        self.controller = None
        self.config_paths = []

        self.status_var = tk.StringVar(
            value="Uruchamianie..."
        )

        # Seria / timelapse
        self.series_count_var = tk.IntVar(value=50)
        self.series_interval_var = tk.DoubleVar(value=15.0)
        self.series_delay_var = tk.DoubleVar(value=0.0)
        self.series_progress_var = tk.StringVar(value="Gotowy")

        self.series_running = False
        self.series_capture_in_progress = False
        self.series_total = 0
        self.series_done = 0
        self.series_interval = 0.0
        self.series_after_id = None
        self.series_last_start = None

        self.build_ui()

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.close,
        )

        # Uruchamiamy inicjalizację po pokazaniu okna.
        self.root.after(
            100,
            self.initialize_camera,
        )

    # --------------------------------------------------------
    # GUI
    # --------------------------------------------------------

    def build_ui(self):
        main = ttk.Frame(
            self.root,
            padding=12,
        )

        main.pack(
            fill="both",
            expand=True,
        )

        main.columnconfigure(
            1,
            weight=1,
        )

        main.rowconfigure(
            0,
            weight=1,
        )

        # ====================================================
        # LEWY PANEL
        # ====================================================

        sidebar = ttk.Frame(
            main,
            padding=(0, 0, 15, 0),
        )

        sidebar.grid(
            row=0,
            column=0,
            sticky="ns",
        )

        ttk.Label(
            sidebar,
            text="Sterowanie aparatem",
            font=("", 16, "bold"),
        ).pack(
            anchor="w",
            pady=(0, 15),
        )

        self.camera_label = ttk.Label(
            sidebar,
            text="Wykrywanie aparatu...",
            wraplength=320,
        )

        self.camera_label.pack(
            anchor="w",
            fill="x",
            pady=(0, 15),
        )

        self.settings_frame = ttk.LabelFrame(
            sidebar,
            text="Parametry",
            padding=10,
        )

        self.settings_frame.pack(
            fill="x",
            pady=(0, 15),
        )

        self.loading_label = ttk.Label(
            self.settings_frame,
            text="Ładowanie ustawień...",
        )

        self.loading_label.pack(
            anchor="w",
        )

        # ====================================================
        # PRZYCISKI
        # ====================================================

        button_frame = ttk.Frame(sidebar)

        button_frame.pack(
            fill="x",
            pady=(0, 10),
        )

        self.live_button = ttk.Button(
            button_frame,
            text="▶ Live View",
            command=self.toggle_live,
            state="disabled",
        )

        self.live_button.pack(
            fill="x",
            pady=3,
        )

        self.capture_button = ttk.Button(
            button_frame,
            text="📷  Zrób zdjęcie",
            command=self.capture_photo,
            state="disabled",
        )

        self.capture_button.pack(
            fill="x",
            pady=3,
            ipady=7,
        )

        self.refresh_button = ttk.Button(
            button_frame,
            text="Odśwież ustawienia",
            command=self.refresh_settings,
            state="disabled",
        )

        self.refresh_button.pack(
            fill="x",
            pady=3,
        )

        # ====================================================
        # SERIA / TIMELAPSE
        # ====================================================

        series_frame = ttk.LabelFrame(
            sidebar,
            text="Seria / timelapse",
            padding=10,
        )

        series_frame.pack(
            fill="x",
            pady=(8, 10),
        )

        series_frame.columnconfigure(1, weight=1)

        ttk.Label(
            series_frame,
            text="Liczba zdjęć:",
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=3,
        )

        ttk.Spinbox(
            series_frame,
            from_=1,
            to=9999,
            textvariable=self.series_count_var,
            width=10,
        ).grid(
            row=0,
            column=1,
            sticky="ew",
            pady=3,
        )

        ttk.Label(
            series_frame,
            text="Interwał [s]:",
        ).grid(
            row=1,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=3,
        )

        ttk.Spinbox(
            series_frame,
            from_=0,
            to=86400,
            increment=0.5,
            textvariable=self.series_interval_var,
            width=10,
        ).grid(
            row=1,
            column=1,
            sticky="ew",
            pady=3,
        )

        ttk.Label(
            series_frame,
            text="Opóźnienie startu [s]:",
        ).grid(
            row=2,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=3,
        )

        ttk.Spinbox(
            series_frame,
            from_=0,
            to=3600,
            increment=1,
            textvariable=self.series_delay_var,
            width=10,
        ).grid(
            row=2,
            column=1,
            sticky="ew",
            pady=3,
        )

        ttk.Label(
            series_frame,
            text="Interwał liczony od startu jednej klatki do startu następnej.",
            wraplength=290,
        ).grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(4, 6),
        )

        self.series_start_button = ttk.Button(
            series_frame,
            text="▶ Start serii",
            command=self.start_series,
            state="disabled",
        )

        self.series_start_button.grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=3,
            ipady=4,
        )

        self.series_stop_button = ttk.Button(
            series_frame,
            text="■ Zatrzymaj serię",
            command=self.stop_series,
            state="disabled",
        )

        self.series_stop_button.grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=3,
        )

        self.sound_test_button = ttk.Button(
            series_frame,
            text="🔔 Test dźwięku",
            command=self.test_completion_sound,
            width=16,
        )

        self.sound_test_button.grid(
            row=6,
            column=0,
            columnspan=2,
            pady=(5, 2),
        )

        ttk.Label(
            series_frame,
            textvariable=self.series_progress_var,
            anchor="center",
        ).grid(
            row=7,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(6, 0),
        )

        ttk.Separator(
            sidebar,
        ).pack(
            fill="x",
            pady=10,
        )

        ttk.Label(
            sidebar,
            text="Zdjęcia zapisywane w:",
        ).pack(
            anchor="w",
        )

        ttk.Label(
            sidebar,
            text=str(SAVE_DIR),
            wraplength=300,
        ).pack(
            anchor="w",
            pady=(3, 10),
        )

        # ====================================================
        # OBRAZY
        # ====================================================

        images = ttk.Frame(main)

        images.grid(
            row=0,
            column=1,
            sticky="nsew",
        )

        images.columnconfigure(
            0,
            weight=1,
        )

        images.columnconfigure(
            1,
            weight=1,
        )

        images.rowconfigure(
            0,
            weight=1,
        )

        # LIVE

        live_frame = ttk.LabelFrame(
            images,
            text="Live View",
            padding=6,
        )

        live_frame.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, 5),
        )

        live_frame.rowconfigure(
            0,
            weight=1,
        )

        live_frame.columnconfigure(
            0,
            weight=1,
        )

        self.live_image_label = ttk.Label(
            live_frame,
            text="Live View wyłączony",
            anchor="center",
        )

        self.live_image_label.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        # LAST PHOTO

        last_frame = ttk.LabelFrame(
            images,
            text="Ostatnie zdjęcie",
            padding=6,
        )

        last_frame.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=(5, 0),
        )

        last_frame.rowconfigure(
            0,
            weight=1,
        )

        last_frame.columnconfigure(
            0,
            weight=1,
        )

        self.last_image_label = ttk.Label(
            last_frame,
            text="Nie wykonano jeszcze zdjęcia",
            anchor="center",
        )

        self.last_image_label.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        # ====================================================
        # STATUS
        # ====================================================

        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            relief="sunken",
            anchor="w",
            padding=(8, 4),
        )

        status.pack(
            side="bottom",
            fill="x",
        )

    # --------------------------------------------------------
    # INIT
    # --------------------------------------------------------

    def initialize_camera(self):
        self.status_var.set(
            "Łączenie z aparatem..."
        )

        future = self.executor.submit(
            self._initialize_camera_worker
        )

        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self._initialize_camera_done,
                f,
            )
        )

    def _initialize_camera_worker(self):
        controller = GPhotoController()

        detect_output = controller.autodetect()
        config_paths = controller.list_config()

        return (
            controller,
            detect_output,
            config_paths,
        )

    def _initialize_camera_done(self, future):
        try:
            (
                self.controller,
                detect_output,
                self.config_paths,
            ) = future.result()

        except Exception as exc:
            self.status_var.set(
                "Błąd połączenia z aparatem"
            )

            self.camera_label.config(
                text=f"Błąd:\n{exc}"
            )

            messagebox.showerror(
                "gphoto2",
                str(exc),
            )

            return

        lines = [
            line.strip()
            for line in detect_output.splitlines()
            if line.strip()
        ]

        if len(lines) >= 3:
            camera_name = lines[-1]
        else:
            camera_name = detect_output.strip()

        self.camera_label.config(
            text=camera_name
        )

        self.status_var.set(
            "Aparat podłączony"
        )

        self.live_button.config(
            state="normal"
        )

        self.capture_button.config(
            state="normal"
        )

        self.refresh_button.config(
            state="normal"
        )

        self.series_start_button.config(
            state="normal"
        )

        self.load_settings()

    # --------------------------------------------------------
    # SETTINGS
    # --------------------------------------------------------

    def find_config_path(self, candidate_names):
        candidates = {
            name.lower()
            for name in candidate_names
        }

        # Najpierw dokładna nazwa ostatniej części ścieżki.
        for path in self.config_paths:
            name = path.rstrip("/").split("/")[-1].lower()

            if name in candidates:
                return path

        # Potem luźniejsze wyszukiwanie.
        for path in self.config_paths:
            path_lower = path.lower()

            for candidate in candidates:
                if path_lower.endswith(
                    "/" + candidate
                ):
                    return path

        return None

    def load_settings(self):
        for child in self.settings_frame.winfo_children():
            child.destroy()

        ttk.Label(
            self.settings_frame,
            text="Odczytywanie z aparatu...",
        ).pack(anchor="w")

        future = self.executor.submit(
            self._load_settings_worker
        )

        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self._load_settings_done,
                f,
            )
        )

    def _load_settings_worker(self):
        settings = []

        for display_name, candidates in SETTING_DEFINITIONS:
            path = self.find_config_path(
                candidates
            )

            if not path:
                continue

            try:
                config = self.controller.get_config(
                    path
                )

                settings.append(
                    (
                        display_name,
                        config,
                    )
                )

            except GPhotoError:
                pass

        return settings

    def _load_settings_done(self, future):
        for child in self.settings_frame.winfo_children():
            child.destroy()

        try:
            settings = future.result()

        except Exception as exc:
            ttk.Label(
                self.settings_frame,
                text=f"Błąd:\n{exc}",
            ).pack(anchor="w")

            return

        if not settings:
            ttk.Label(
                self.settings_frame,
                text=(
                    "Nie znaleziono standardowych\n"
                    "ustawień aparatu."
                ),
            ).pack(anchor="w")

            return

        for display_name, config in settings:
            widget = CameraSettingWidget(
                self.settings_frame,
                self,
                display_name,
                config,
            )

            widget.pack(
                fill="x",
                pady=7,
            )

    def refresh_settings(self):
        if not self.controller:
            return

        self.status_var.set(
            "Odświeżanie listy ustawień..."
        )

        future = self.executor.submit(
            self.controller.list_config
        )

        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self._refresh_done,
                f,
            )
        )

    def _refresh_done(self, future):
        try:
            self.config_paths = future.result()

        except Exception as exc:
            self.status_var.set(
                f"Błąd: {exc}"
            )
            return

        self.status_var.set(
            "Ustawienia odświeżone"
        )

        self.load_settings()

    def set_camera_config(
        self,
        path,
        value,
        display_name,
    ):
        if not self.controller:
            return

        self.status_var.set(
            f"{display_name}: {value}"
        )

        future = self.executor.submit(
            self.controller.set_config,
            path,
            value,
        )

        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self._set_config_done,
                f,
                display_name,
                value,
            )
        )

    def _set_config_done(
        self,
        future,
        display_name,
        value,
    ):
        try:
            future.result()

        except Exception as exc:
            self.status_var.set(
                f"Błąd {display_name}: {exc}"
            )
            return

        self.status_var.set(
            f"{display_name} = {value}"
        )

    # --------------------------------------------------------
    # LIVE VIEW
    # --------------------------------------------------------

    def toggle_live(self):
        self.live_enabled = not self.live_enabled

        if self.live_enabled:
            self.live_button.config(
                text="■ Zatrzymaj Live View"
            )

            self.status_var.set(
                "Live View uruchomiony"
            )

            self.request_live_frame()

        else:
            self.live_button.config(
                text="▶ Live View"
            )

            self.live_image_label.config(
                image="",
                text="Live View zatrzymany",
            )

            self.live_photoimage = None

            self.status_var.set(
                "Live View zatrzymany"
            )

    def request_live_frame(self):
        if (
            not self.live_enabled
            or self.closing
            or not self.controller
        ):
            return

        future = self.executor.submit(
            self.controller.capture_preview
        )

        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self.live_frame_done,
                f,
            )
        )

    def live_frame_done(self, future):
        if (
            not self.live_enabled
            or self.closing
        ):
            return

        try:
            image = future.result()

        except Exception as exc:
            self.status_var.set(
                f"Live View: {exc}"
            )

            self.root.after(
                1000,
                self.request_live_frame,
            )

            return

        self.display_live_image(image)

        self.root.after(
            LIVE_PREVIEW_DELAY_MS,
            self.request_live_frame,
        )

    # --------------------------------------------------------
    # CAPTURE
    # --------------------------------------------------------

    def capture_photo(self):
        if not self.controller:
            return

        self.capture_button.config(
            state="disabled"
        )

        self.status_var.set(
            "Robię zdjęcie..."
        )

        future = self.executor.submit(
            self.controller.capture_photo
        )

        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self.capture_done,
                f,
            )
        )

    def capture_done(self, future):
        self.capture_button.config(
            state="normal"
        )

        try:
            filename = future.result()

        except Exception as exc:
            self.status_var.set(
                f"Błąd wykonywania zdjęcia: {exc}"
            )

            messagebox.showerror(
                "Błąd aparatu",
                str(exc),
            )

            return

        try:
            image = Image.open(filename)
            image.load()

            self.display_last_image(
                image.copy()
            )

        except Exception as exc:
            self.status_var.set(
                f"Zdjęcie zapisane, ale podgląd się nie udał: {exc}"
            )

            return

        self.status_var.set(
            f"Zapisano: {filename.name}"
        )

    # --------------------------------------------------------
    # SERIA / TIMELAPSE
    # --------------------------------------------------------

    def start_series(self):
        if not self.controller or self.series_running:
            return

        try:
            count = int(self.series_count_var.get())
            interval = float(self.series_interval_var.get())
            delay = float(self.series_delay_var.get())
        except (tk.TclError, TypeError, ValueError):
            messagebox.showerror(
                "Seria / timelapse",
                "Podaj poprawną liczbę zdjęć, interwał i opóźnienie.",
            )
            return

        if count < 1:
            messagebox.showerror(
                "Seria / timelapse",
                "Liczba zdjęć musi być większa od zera.",
            )
            return

        if interval < 0 or delay < 0:
            messagebox.showerror(
                "Seria / timelapse",
                "Interwał i opóźnienie nie mogą być ujemne.",
            )
            return

        # Live View stale odpytuje aparat. Podczas serii wyłączamy go,
        # aby polecenia nie konkurowały ze sobą o połączenie USB.
        if self.live_enabled:
            self.toggle_live()

        self.series_running = True
        self.series_capture_in_progress = False
        self.series_total = count
        self.series_done = 0
        self.series_interval = interval
        self.series_last_start = None

        self.series_start_button.config(state="disabled")
        self.series_stop_button.config(state="normal")
        self.capture_button.config(state="disabled")
        self.refresh_button.config(state="disabled")

        self.series_progress_var.set(
            f"0 / {self.series_total}"
        )

        if delay > 0:
            self.status_var.set(
                f"Seria wystartuje za {delay:g} s..."
            )
        else:
            self.status_var.set(
                "Start serii..."
            )

        delay_ms = max(0, int(delay * 1000))
        self.series_after_id = self.root.after(
            delay_ms,
            self._series_capture_next,
        )

    def stop_series(self):
        if not self.series_running:
            return

        self.series_running = False

        if self.series_after_id is not None:
            try:
                self.root.after_cancel(self.series_after_id)
            except tk.TclError:
                pass
            self.series_after_id = None

        if self.series_capture_in_progress:
            self.status_var.set(
                "Zatrzymywanie serii po bieżącym zdjęciu..."
            )
            self.series_progress_var.set(
                f"Zatrzymywanie… {self.series_done} / {self.series_total}"
            )
        else:
            self._restore_after_series()
            self.status_var.set(
                f"Seria zatrzymana: {self.series_done} / {self.series_total}"
            )
            self.series_progress_var.set(
                f"Zatrzymano: {self.series_done} / {self.series_total}"
            )

    def _series_capture_next(self):
        self.series_after_id = None

        if (
            not self.series_running
            or self.closing
            or not self.controller
        ):
            return

        if self.series_done >= self.series_total:
            self._finish_series()
            return

        shot_number = self.series_done + 1

        self.series_capture_in_progress = True
        self.series_last_start = time.monotonic()

        self.status_var.set(
            f"Seria: robię zdjęcie {shot_number}/{self.series_total}..."
        )
        self.series_progress_var.set(
            f"{self.series_done} / {self.series_total}  •  wykonywanie {shot_number}"
        )

        future = self.executor.submit(
            self.controller.capture_photo
        )

        future.add_done_callback(
            lambda f, n=shot_number: self.root.after(
                0,
                self._series_capture_done,
                f,
                n,
            )
        )

    def _series_capture_done(self, future, shot_number):
        self.series_capture_in_progress = False

        try:
            filename = future.result()

        except Exception as exc:
            was_running = self.series_running
            self.series_running = False
            self._restore_after_series()

            self.status_var.set(
                f"Błąd serii przy zdjęciu {shot_number}: {exc}"
            )
            self.series_progress_var.set(
                f"Błąd przy {shot_number}/{self.series_total}"
            )

            if was_running:
                messagebox.showerror(
                    "Błąd serii",
                    str(exc),
                )
            return

        # Zdjęcie zostało wykonane nawet wtedy, gdy użytkownik kliknął
        # „Zatrzymaj” w trakcie ekspozycji.
        self.series_done = shot_number

        try:
            image = Image.open(filename)
            image.load()
            self.display_last_image(
                image.copy()
            )
        except Exception:
            # Brak podglądu (np. nieobsługiwany format RAW) nie powinien
            # zatrzymywać całej serii.
            pass

        self.series_progress_var.set(
            f"{self.series_done} / {self.series_total}"
        )

        if not self.series_running:
            self._restore_after_series()
            self.status_var.set(
                f"Seria zatrzymana: {self.series_done} / {self.series_total}"
            )
            self.series_progress_var.set(
                f"Zatrzymano: {self.series_done} / {self.series_total}"
            )
            return

        if self.series_done >= self.series_total:
            self._finish_series()
            return

        # Interwał jest liczony od STARTU poprzedniej ekspozycji do
        # STARTU następnej. Jeśli aparat potrzebował dłużej niż zadany
        # interwał (np. długa ekspozycja + zapis), następna klatka
        # rozpocznie się tak szybko, jak to możliwe.
        elapsed = 0.0
        if self.series_last_start is not None:
            elapsed = time.monotonic() - self.series_last_start

        wait_seconds = max(
            0.0,
            self.series_interval - elapsed,
        )

        self.status_var.set(
            f"Seria: {self.series_done}/{self.series_total}; "
            f"następne za {wait_seconds:.1f} s"
        )

        self.series_after_id = self.root.after(
            int(wait_seconds * 1000),
            self._series_capture_next,
        )

    def _finish_series(self):
        self.series_running = False
        self.series_capture_in_progress = False

        if self.series_after_id is not None:
            try:
                self.root.after_cancel(self.series_after_id)
            except tk.TclError:
                pass
            self.series_after_id = None

        self._restore_after_series()

        self.status_var.set(
            f"Seria zakończona: {self.series_done}/{self.series_total}"
        )
        self.series_progress_var.set(
            f"✓ Gotowe: {self.series_done} / {self.series_total}"
        )

        self.play_completion_sound()

    def _restore_after_series(self):
        if self.closing:
            return

        self.series_start_button.config(
            state="normal" if self.controller else "disabled"
        )
        self.series_stop_button.config(state="disabled")
        self.capture_button.config(
            state="normal" if self.controller else "disabled"
        )
        self.refresh_button.config(
            state="normal" if self.controller else "disabled"
        )

    def test_completion_sound(self):
        """Ręczny test dźwięku z poziomu GUI."""
        self.status_var.set("Test dźwięku...")
        self.play_completion_sound()

    def play_completion_sound(self):
        """Krótki dźwięk po wykonaniu całej serii lub po kliknięciu testu."""

        def sound_worker():
            played = False

            # ------------------------------------------------
            # macOS
            # ------------------------------------------------
            if sys.platform == "darwin":
                afplay = shutil.which("afplay")

                mac_sounds = [
                    "/System/Library/Sounds/Glass.aiff",
                    "/System/Library/Sounds/Ping.aiff",
                    "/System/Library/Sounds/Pop.aiff",
                ]

                if afplay:
                    for sound_file in mac_sounds:
                        if os.path.exists(sound_file):
                            try:
                                subprocess.run(
                                    [afplay, sound_file],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=4,
                                )
                                played = True
                                break
                            except Exception:
                                pass

            # ------------------------------------------------
            # Linux / Linux Mint
            # ------------------------------------------------
            elif sys.platform.startswith("linux"):
                if shutil.which("canberra-gtk-play"):
                    try:
                        subprocess.run(
                            ["canberra-gtk-play", "-i", "complete"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=3,
                        )
                        played = True
                    except Exception:
                        pass

                sound_files = [
                    "/usr/share/sounds/freedesktop/stereo/complete.oga",
                    "/usr/share/sounds/freedesktop/stereo/message.oga",
                    "/usr/share/sounds/freedesktop/stereo/bell.oga",
                ]

                if not played and shutil.which("paplay"):
                    for sound_file in sound_files:
                        if os.path.exists(sound_file):
                            try:
                                subprocess.run(
                                    ["paplay", sound_file],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=3,
                                )
                                played = True
                                break
                            except Exception:
                                pass

                if not played and shutil.which("pw-play"):
                    for sound_file in sound_files:
                        if os.path.exists(sound_file):
                            try:
                                subprocess.run(
                                    ["pw-play", sound_file],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=3,
                                )
                                played = True
                                break
                            except Exception:
                                pass

            # ------------------------------------------------
            # Fallback wspólny dla każdego systemu
            # ------------------------------------------------
            if not played and not self.closing:
                try:
                    self.root.after(0, self.root.bell)
                    played = True
                except Exception:
                    pass

            if not self.closing:
                def update_status():
                    if played:
                        self.status_var.set("Dźwięk działa")
                    else:
                        self.status_var.set(
                            "Nie udało się odtworzyć dźwięku"
                        )

                try:
                    self.root.after(0, update_status)
                except Exception:
                    pass

        threading.Thread(
            target=sound_worker,
            daemon=True,
            name="series-completion-sound",
        ).start()

    # --------------------------------------------------------
    # IMAGES
    # --------------------------------------------------------

    def prepare_image(
        self,
        image,
        max_size=(650, 650),
    ):
        # Obrót na podstawie EXIF
        image = ImageOps.exif_transpose(
            image
        )

        image.thumbnail(
            max_size,
            Image.Resampling.LANCZOS,
        )

        return image

    def display_live_image(self, image):
        image = self.prepare_image(
            image,
            (650, 650),
        )

        self.live_photoimage = ImageTk.PhotoImage(
            image
        )

        self.live_image_label.config(
            image=self.live_photoimage,
            text="",
        )

    def display_last_image(self, image):
        image = self.prepare_image(
            image,
            (650, 650),
        )

        self.last_photoimage = ImageTk.PhotoImage(
            image
        )

        self.last_image_label.config(
            image=self.last_photoimage,
            text="",
        )

    # --------------------------------------------------------
    # CLOSE
    # --------------------------------------------------------

    def close(self):
        self.closing = True
        self.live_enabled = False
        self.series_running = False

        if self.series_after_id is not None:
            try:
                self.root.after_cancel(self.series_after_id)
            except tk.TclError:
                pass
            self.series_after_id = None

        self.executor.shutdown(
            wait=False,
            cancel_futures=True,
        )

        self.root.destroy()


# ============================================================
# START
# ============================================================

def main():
    root = tk.Tk()

    try:
        style = ttk.Style()

        if "clam" in style.theme_names():
            style.theme_use("clam")

    except Exception:
        pass

    GPhotoGUI(root)

    root.mainloop()


if __name__ == "__main__":
    main()