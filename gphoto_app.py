#!/usr/bin/env python3

import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

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

        # Cache konfiguracji fotograficznej. Dzięki temu samo wyzwolenie
        # migawki nie musi wykonywać --list-config ani ponownie odpytywać
        # aparatu o te same ustawienia przed każdą klatką.
        self._capture_setup_ready = False
        self._capture_setup_fast_jpeg = None
        self._config_paths_cache = []

        # Zapamiętujemy normalny rozmiar JPEG, żeby tryb szybki nie
        # wymuszał potem dużego JPEG-a ani nie zmieniał ustawienia na stałe.
        self._normal_jpeg_size = None
        self._capture_setup_quality = None
        self._capture_setup_jpeg_size = None

    def run(self, *args, binary=False, timeout=30):
        cmd = [GPHOTO2, *args]

        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["LANG"] = "C"

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=env,
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

    def reset_capture_setup(self):
        """Oznacza konfigurację fotografowania jako wymagającą odświeżenia."""
        self._capture_setup_ready = False
        self._capture_setup_fast_jpeg = None

    def set_config_paths_cache(self, config_paths):
        self._config_paths_cache = list(config_paths or [])

    def _find_config_path(self, config_paths, candidate_names):
        candidates = {
            name.lower()
            for name in candidate_names
        }

        for path in config_paths:
            name = path.rstrip("/").split("/")[-1].lower()

            if name in candidates:
                return path

        for path in config_paths:
            path_lower = path.lower()

            for candidate in candidates:
                if path_lower.endswith("/" + candidate):
                    return path

        return None

    def _set_choice_containing(
        self,
        config_paths,
        candidate_names,
        predicate,
        description,
        prefer=None,
    ):
        path = self._find_config_path(
            config_paths,
            candidate_names,
        )

        if not path:
            raise GPhotoError(
                f"Nie znaleziono ustawienia aparatu: {description}."
            )

        config = self.get_config(path)
        choices = config.get("choices", [])

        matches = []

        for _, value in choices:
            value_lower = value.lower()

            if predicate(value_lower):
                score = prefer(value_lower) if prefer else 0
                matches.append((score, value))

        if not matches:
            available = ", ".join(
                value
                for _, value in choices
            ) or "brak listy opcji"

            raise GPhotoError(
                f"Aparat nie udostępnia oczekiwanej opcji: {description}.\n"
                f"Dostępne wartości: {available}"
            )

        matches.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        selected = matches[0][1]

        if config.get("current") != selected:
            self.set_config(path, selected)

        return selected

    def prepare_capture_setup(self, fast_jpeg=False, config_paths=None):
        """
        Przygotowuje aparat do zdjęć bez niepotrzebnego skanowania przed
        każdą klatką.

        fast_jpeg=False:
            - RAW + JPEG Fine,
            - NIE zmieniamy rozmiaru JPEG; zostaje taki, jak ustawiony
              w aparacie przez użytkownika.

        fast_jpeg=True:
            - RAW + JPEG Basic,
            - jeśli aparat udostępnia osobne ustawienie rozmiaru JPEG,
              wybieramy najmniejszy dostępny rozmiar,
            - RAW/NEF nie jest zmniejszany.
        """
        fast_jpeg = bool(fast_jpeg)

        if (
            self._capture_setup_ready
            and self._capture_setup_fast_jpeg == fast_jpeg
        ):
            return

        if config_paths is None:
            config_paths = self._config_paths_cache

        if not config_paths:
            config_paths = self.list_config()

        self.set_config_paths_cache(config_paths)

        # Zapis na kartę pamięci.
        self._set_choice_containing(
            config_paths,
            [
                "capturetarget",
                "capture-target",
            ],
            lambda value: (
                "memory card" in value
                or value == "card"
                or "sd card" in value
                or "karta" in value
            ),
            "miejsce zapisu = karta pamięci",
        )

        def raw_jpeg_choice(value):
            raw_value = value.lower()
            normalized = (
                raw_value
                .replace(" ", "")
                .replace("_", "")
                .replace("-", "")
            )

            has_raw = (
                "raw" in normalized
                or "nef" in normalized
            )
            explicit_jpeg = (
                "jpeg" in normalized
                or "jpg" in normalized
            )
            has_quality_word = any(
                quality in normalized
                for quality in ("fine", "normal", "basic")
            )
            nikon_combined_mode = (
                (
                    "nef+" in raw_value.replace(" ", "")
                    or "raw+" in raw_value.replace(" ", "")
                )
                and has_quality_word
            )

            return has_raw and (explicit_jpeg or nikon_combined_mode)

        def quality_preference(value):
            # W szybkim trybie najpierw Basic; w normalnym Fine.
            if fast_jpeg:
                if "basic" in value:
                    return 30
                if "normal" in value:
                    return 20
                if "fine" in value:
                    return 10
            else:
                if "fine" in value:
                    return 30
                if "normal" in value:
                    return 20
                if "basic" in value:
                    return 10
            return 0

        quality_candidates = [
            "imagequality",
            "imagequality2",
            "image-quality",
            "imageformat",
            "image-format",
        ]
        self._capture_setup_quality = self._set_choice_containing(
            config_paths,
            quality_candidates,
            raw_jpeg_choice,
            "format RAW (NEF) + JPEG",
            prefer=quality_preference,
        )

        if fast_jpeg and "basic" not in self._capture_setup_quality.lower():
            raise GPhotoError(
                "Aparat nie przyjął trybu NEF + JPEG Basic. "
                f"Aktualnie wybrano: {self._capture_setup_quality}"
            )

        # Znajdź ustawienie rozmiaru JPEG. Nie bierzemy ścieżek RAW/NEF.
        size_candidates = [
            "imagesize",
            "imagesize2",
            "image-size",
            "image_size",
            "jpegsize",
            "jpeg-size",
            "jpeg_size",
            "jpegimagesize",
            "jpeg-image-size",
            "jpeg_image_size",
        ]

        size_path = self._find_config_path(config_paths, size_candidates)
        if not size_path:
            for path in config_paths:
                leaf = path.rstrip("/").split("/")[-1].lower()
                compact = leaf.replace("-", "").replace("_", "")
                if "raw" in compact or "nef" in compact:
                    continue
                if "imagesize" in compact or ("jpeg" in compact and "size" in compact):
                    size_path = path
                    break

        self._capture_setup_jpeg_size = None

        if fast_jpeg and not size_path:
            raise GPhotoError(
                "Nie znaleziono ustawienia rozmiaru JPEG w aparacie. "
                "Nie wykonuję zdjęcia w trybie szybkim, żeby nie przesyłać "
                "pełnowymiarowego JPEG-a przez USB."
            )

        if size_path:
            try:
                size_config = self.get_config(size_path)
                current_size = size_config.get("current")
                choices = [value for _, value in size_config.get("choices", [])]

                # W trybie normalnym zapamiętujemy rozmiar zastany przy starcie.
                # Po wyjściu z szybkiego JPEG przywracamy właśnie ten rozmiar,
                # a nie wymuszamy Large.
                if not fast_jpeg:
                    if self._normal_jpeg_size is None and current_size:
                        self._normal_jpeg_size = current_size

                    target_size = self._normal_jpeg_size or current_size
                    if target_size and choices and target_size in choices:
                        if current_size != target_size:
                            self.set_config(size_path, target_size)
                        self._capture_setup_jpeg_size = target_size
                    else:
                        self._capture_setup_jpeg_size = current_size

                elif choices:
                    # Jeśli nie mamy jeszcze zapamiętanego normalnego rozmiaru,
                    # zachowujemy go przed przełączeniem na najmniejszy JPEG.
                    if self._normal_jpeg_size is None and current_size:
                        self._normal_jpeg_size = current_size

                    def size_key(value):
                        s = value.strip().lower()
                        compact = re.sub(r"\s+", "", s)

                        # Najpierw jawne oznaczenia S/M/L.
                        semantic = 3
                        if (
                            "small" in s
                            or "mały" in s
                            or "maly" in s
                            or re.match(r"^s(?:$|[\s(\[/_-])", s)
                        ):
                            semantic = 0
                        elif (
                            "medium" in s
                            or "średni" in s
                            or "sredni" in s
                            or re.match(r"^m(?:$|[\s(\[/_-])", s)
                        ):
                            semantic = 1
                        elif (
                            "large" in s
                            or "duży" in s
                            or "duzy" in s
                            or re.match(r"^l(?:$|[\s(\[/_-])", s)
                        ):
                            semantic = 2

                        # Następnie faktyczna liczba pikseli, jeśli jest w nazwie.
                        area = float("inf")
                        dim_match = re.search(r"(\d{3,5})\s*[x×]\s*(\d{3,5})", s)
                        if dim_match:
                            area = int(dim_match.group(1)) * int(dim_match.group(2))
                        else:
                            mp_match = re.search(r"(\d+(?:[.,]\d+)?)\s*mp", compact)
                            if mp_match:
                                area = float(mp_match.group(1).replace(",", ".")) * 1_000_000

                        return (semantic, area, len(value))

                    selected_size = min(choices, key=size_key)
                    if current_size != selected_size:
                        self.set_config(size_path, selected_size)

                    # Odczyt zwrotny: nie ufamy samemu --set-config.
                    verified_size = self.get_config(size_path).get("current")
                    if verified_size != selected_size:
                        raise GPhotoError(
                            "Aparat nie przyjął najmniejszego rozmiaru JPEG. "
                            f"Żądano: {selected_size}; aparat zgłasza: {verified_size}."
                        )
                    self._capture_setup_jpeg_size = verified_size

            except GPhotoError:
                if fast_jpeg:
                    raise
                self._capture_setup_jpeg_size = None

        self._capture_setup_ready = True
        self._capture_setup_fast_jpeg = fast_jpeg

    def scan_and_prepare_capture_setup(self, fast_jpeg=False):
        """
        Pełne skanowanie wykonujemy poza sekwencją robienia klatek:
        po pojedynczym zdjęciu, po serii albo po zmianie trybu JPEG.
        """
        config_paths = self.list_config()
        self.set_config_paths_cache(config_paths)
        self.reset_capture_setup()
        self.prepare_capture_setup(
            fast_jpeg=fast_jpeg,
            config_paths=config_paths,
        )
        return config_paths

    def capture_photo(self, save_dir, need_raw_path=True, fast_jpeg=False):
        """
        Etap 1:
        - wykonuje zdjęcie w trybie NEF+JPEG,
        - wykonuje RAW+JPEG,
        - w trybie JPEG-only RAW zostaje na karcie SD,
        - na komputer pobiera wyłącznie JPEG.

        Dzięki --keep-raw JPEG jest dostępny do podglądu znacznie wcześniej.
        NEF jest pobierany osobno przez download_raw(), ale tylko gdy GUI
        rzeczywiście chce zapisać RAW także na laptopie.
        """
        save_dir = Path(save_dir).expanduser()
        save_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.prepare_capture_setup(fast_jpeg=fast_jpeg)

        stamp = datetime.now().strftime(
            "%Y-%m-%d_%H-%M-%S_%f"
        )
        basename = f"photo_{stamp}"
        filename_pattern = save_dir / f"{basename}.%C"

        capture_args = [
            "--force-overwrite",
            "--filename",
            str(filename_pattern),
            "--capture-image-and-download",
        ]

        # Kluczowe: w trybie JPEG-only NIE łączymy --keep z --keep-raw.
        # --keep-raw oznacza: RAW zostaje na aparacie, pobierany jest JPEG.
        # Dzięki temu NEF nie powinien przechodzić przez USB w tym etapie.
        if not need_raw_path:
            capture_args.append("--keep-raw")
        else:
            # W trybie pełnym nadal zachowujemy pliki na karcie.
            capture_args.extend(["--keep", "--keep-raw"])

        output = self.run(
            *capture_args,
            timeout=120,
        )

        # Lokalnie powinien być już JPEG.
        files = sorted(
            path
            for path in save_dir.glob(f"{basename}.*")
            if path.is_file()
        )

        jpeg_files = [
            path
            for path in files
            if path.suffix.lower() in {".jpg", ".jpeg"}
        ]

        # Bezpiecznik diagnostyczny: w trybie JPEG-only żaden lokalny NEF
        # nie powinien powstać. Jeśli libgphoto2 mimo wszystko go zapisze,
        # zgłaszamy to wyraźnie zamiast udawać, że tryb działa poprawnie.
        if not need_raw_path:
            unexpected_raw = [
                path for path in files
                if path.suffix.lower() in {".nef", ".raw"}
            ]
            if unexpected_raw:
                names = ", ".join(path.name for path in unexpected_raw)
                raise GPhotoError(
                    "Tryb JPEG-only nie został wykonany poprawnie: "
                    f"gphoto2 zapisał lokalnie RAW ({names}). "
                    "RAW powinien pozostać wyłącznie na karcie SD."
                )

        if not jpeg_files:
            found = ", ".join(
                path.name
                for path in files
            ) or "brak"

            raise GPhotoError(
                "Zdjęcie zostało wykonane, ale JPEG nie pojawił się "
                "na dysku.\n"
                f"Znalezione pliki: {found}"
            )

        # Jeśli na laptop ma trafić tylko JPEG, kończymy tutaj.
        # RAW/NEF pozostaje na karcie SD i w ogóle nie jest transmitowany USB.
        if not need_raw_path:
            return {
                "jpeg": jpeg_files[0],
                "raw": None,
                "raw_camera_folder": None,
                "raw_camera_name": None,
            }

        # gphoto2 z --keep-raw wypisuje lokalizację NEF na aparacie,
        # ale go jeszcze nie pobiera. Przy wymuszonym LC_ALL=C format
        # komunikatu jest przewidywalny.
        raw_matches = re.findall(
            r"(/[^\r\n]*?\.NEF)(?=\s|$)",
            output,
            flags=re.IGNORECASE,
        )

        if not raw_matches:
            raise GPhotoError(
                "JPEG został pobrany, ale program nie potrafił ustalić "
                "lokalizacji pliku NEF na karcie.\n\n"
                "RAW powinien nadal znajdować się na karcie SD."
            )

        raw_camera_path = raw_matches[-1]
        raw_camera_folder, raw_camera_name = raw_camera_path.rsplit(
            "/",
            1,
        )

        if not raw_camera_folder:
            raw_camera_folder = "/"

        raw_local_path = save_dir / f"{basename}.NEF"

        return {
            "jpeg": jpeg_files[0],
            "raw": raw_local_path,
            "raw_camera_folder": raw_camera_folder,
            "raw_camera_name": raw_camera_name,
        }

    def download_raw(self, capture_result):
        """
        Etap 2:
        pobiera NEF z karty do tego samego folderu na komputerze.
        Plik na karcie NIE jest usuwany.
        """
        raw_local_path = Path(
            capture_result["raw"]
        )

        self.run(
            "--folder",
            capture_result["raw_camera_folder"],
            "--get-file",
            capture_result["raw_camera_name"],
            "--filename",
            str(raw_local_path),
            "--force-overwrite",
            timeout=180,
        )

        if not raw_local_path.exists():
            raise GPhotoError(
                "Nie udało się pobrać pliku NEF na dysk. "
                "RAW powinien nadal znajdować się na karcie SD."
            )

        return capture_result


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

        # Jeden duży panel obrazu zamiast dwóch mniejszych.
        self.viewer_photoimage = None
        self.live_image_pil = None
        self.last_image_pil = None
        self.viewer_mode = tk.StringVar(value="last")
        self.viewer_resize_after = None

        self.controller = None
        self.config_paths = []

        self.status_var = tk.StringVar(
            value="Uruchamianie..."
        )

        self.save_dir_var = tk.StringVar(
            value=str(SAVE_DIR)
        )

        # Gdy włączone, na laptop trafia tylko JPEG.
        # RAW/NEF nadal zostaje na karcie SD aparatu.
        self.laptop_jpeg_only_var = tk.BooleanVar(value=True)
        self.current_capture_jpeg_only = False

        # Folder i tryb używane przez aktualnie trwającą serię.
        self.series_save_dir = SAVE_DIR
        self.series_jpeg_only = False

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

        save_dir_frame = ttk.Frame(
            sidebar,
        )

        save_dir_frame.pack(
            fill="x",
            pady=(3, 5),
        )

        save_dir_frame.columnconfigure(
            0,
            weight=1,
        )

        self.save_dir_entry = ttk.Entry(
            save_dir_frame,
            textvariable=self.save_dir_var,
            state="readonly",
            width=28,
        )

        self.save_dir_entry.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=(0, 6),
        )

        self.save_dir_button = ttk.Button(
            save_dir_frame,
            text="Wybierz…",
            command=self.choose_save_directory,
        )

        self.save_dir_button.grid(
            row=0,
            column=1,
        )

        self.laptop_jpeg_only_check = ttk.Checkbutton(
            sidebar,
            text="✓ Na laptop TYLKO szybki JPEG; RAW TYLKO na SD",
            variable=self.laptop_jpeg_only_var,
            command=self.on_laptop_jpeg_only_changed,
        )
        self.laptop_jpeg_only_check.pack(
            anchor="w",
            fill="x",
            pady=(2, 4),
        )

        ttk.Label(
            sidebar,
            text="Aparat: RAW (NEF) + JPEG na karcie SD",
            wraplength=300,
        ).pack(
            anchor="w",
            pady=(0, 10),
        )

        # ====================================================
        # PODGLĄD OBRAZU
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

        images.rowconfigure(
            1,
            weight=1,
        )

        # Przełącznik pomiędzy ostatnim zdjęciem a Live View.
        viewer_toolbar = ttk.Frame(
            images,
        )

        viewer_toolbar.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 6),
        )

        self.viewer_last_button = ttk.Button(
            viewer_toolbar,
            text="📷 Ostatnie zdjęcie",
            command=lambda: self.set_viewer_mode("last"),
        )

        self.viewer_last_button.pack(
            side="left",
            padx=(0, 6),
        )

        self.viewer_live_button = ttk.Button(
            viewer_toolbar,
            text="▶ Live View",
            command=lambda: self.set_viewer_mode("live"),
        )

        self.viewer_live_button.pack(
            side="left",
        )

        self.viewer_frame = ttk.LabelFrame(
            images,
            text="Ostatnie zdjęcie",
            padding=6,
        )

        self.viewer_frame.grid(
            row=1,
            column=0,
            sticky="nsew",
        )

        self.viewer_frame.rowconfigure(
            0,
            weight=1,
        )

        self.viewer_frame.columnconfigure(
            0,
            weight=1,
        )

        self.viewer_image_label = ttk.Label(
            self.viewer_frame,
            text="Nie wykonano jeszcze zdjęcia",
            anchor="center",
        )

        self.viewer_image_label.grid(
            row=0,
            column=0,
            sticky="nsew",
        )

        # Przy zmianie rozmiaru okna ponownie dopasowujemy obraz,
        # żeby wykorzystać cały dostępny panel.
        self.viewer_image_label.bind(
            "<Configure>",
            self.on_viewer_resize,
        )

        self.update_viewer_buttons()

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

    def choose_save_directory(self):
        current = Path(
            self.save_dir_var.get()
        ).expanduser()

        initial_dir = (
            current
            if current.exists()
            else Path.home()
        )

        selected = filedialog.askdirectory(
            title="Wybierz folder na zdjęcia",
            initialdir=str(initial_dir),
            mustexist=True,
        )

        if not selected:
            return

        self.save_dir_var.set(
            str(Path(selected))
        )

        self.status_var.set(
            f"Folder zapisu: {selected}"
        )

    def get_save_directory(self):
        value = self.save_dir_var.get().strip()

        if not value:
            raise ValueError(
                "Nie wybrano folderu zapisu."
            )

        save_dir = Path(value).expanduser()

        try:
            save_dir.mkdir(
                parents=True,
                exist_ok=True,
            )
        except OSError as exc:
            raise ValueError(
                f"Nie można użyć folderu zapisu:\n{exc}"
            ) from exc

        if not save_dir.is_dir():
            raise ValueError(
                "Wybrana ścieżka nie jest folderem."
            )

        if not os.access(
            save_dir,
            os.W_OK,
        ):
            raise ValueError(
                "Brak uprawnień do zapisu w wybranym folderze."
            )

        return save_dir

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
        controller.set_config_paths_cache(config_paths)

        # Jednorazowo przygotowujemy aparat przy starcie. Dzięki temu pierwsze
        # zdjęcie nie zaczyna się od skanowania konfiguracji.
        controller.prepare_capture_setup(
            fast_jpeg=True,
            config_paths=config_paths,
        )

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

        quality = self.controller._capture_setup_quality or "NEF + JPEG Basic"
        jpeg_size = self.controller._capture_setup_jpeg_size
        if jpeg_size:
            self.status_var.set(
                f"Aparat podłączony • JPEG-only AKTYWNY • {quality} • rozmiar {jpeg_size} • RAW tylko SD"
            )
        else:
            self.status_var.set(
                f"Aparat podłączony • JPEG-only AKTYWNY • {quality} • RAW tylko SD"
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
            self.controller.set_config_paths_cache(self.config_paths)

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

            self.status_var.set(
                "Live View zatrzymany"
            )

            if self.viewer_mode.get() == "live":
                self.refresh_viewer()

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

    def on_laptop_jpeg_only_changed(self):
        """Przygotowuje nowy tryb od razu po zmianie checkboxa, nie przy zdjęciu."""
        if not self.controller or self.series_running:
            return

        fast_jpeg = bool(self.laptop_jpeg_only_var.get())
        self.capture_button.config(state="disabled")
        self.series_start_button.config(state="disabled")
        self.refresh_button.config(state="disabled")
        self.laptop_jpeg_only_check.config(state="disabled")

        self.status_var.set(
            "Ustawiam szybki JPEG (Basic + najmniejszy dostępny rozmiar)..."
            if fast_jpeg
            else "Przywracam JPEG Fine i poprzedni rozmiar..."
        )

        future = self.executor.submit(
            self.controller.scan_and_prepare_capture_setup,
            fast_jpeg,
        )
        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self._capture_mode_prepare_done,
                f,
                fast_jpeg,
            )
        )

    def _capture_mode_prepare_done(self, future, fast_jpeg):
        try:
            self.config_paths = future.result()
        except Exception as exc:
            self.status_var.set(f"Błąd ustawiania trybu JPEG: {exc}")
            messagebox.showerror("Ustawienia JPEG", str(exc))
        else:
            if fast_jpeg:
                quality = self.controller._capture_setup_quality or "JPEG Basic"
                jpeg_size = self.controller._capture_setup_jpeg_size
                if jpeg_size:
                    self.status_var.set(
                        f"Tryb gotowy: {quality}; JPEG {jpeg_size} na laptop, RAW na SD"
                    )
                else:
                    self.status_var.set(
                        f"Tryb gotowy: {quality}; rozmiaru JPEG aparat nie udostępnił, RAW na SD"
                    )
            else:
                self.status_var.set(
                    "Tryb gotowy: JPEG Fine + RAW; przywrócono normalny rozmiar JPEG"
                )
        finally:
            if not self.closing and not self.series_running:
                self.capture_button.config(
                    state="normal" if self.controller else "disabled"
                )
                self.series_start_button.config(
                    state="normal" if self.controller else "disabled"
                )
                self.refresh_button.config(
                    state="normal" if self.controller else "disabled"
                )
                self.laptop_jpeg_only_check.config(state="normal")

    def _scan_after_single_capture(self, final_status):
        """Skanuje i przygotowuje aparat dopiero PO pojedynczym zdjęciu."""
        if self.closing or not self.controller:
            return


        fast_jpeg = self.current_capture_jpeg_only
        self.status_var.set(final_status + " • sprawdzam ustawienia na następne zdjęcie...")

        future = self.executor.submit(
            self.controller.scan_and_prepare_capture_setup,
            fast_jpeg,
        )
        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self._scan_after_single_done,
                f,
                final_status,
            )
        )

    def _scan_after_single_done(self, future, final_status):
        try:
            self.config_paths = future.result()
        except Exception as exc:
            self.status_var.set(
                final_status + f" • błąd skanowania ustawień: {exc}"
            )
        else:
            self.status_var.set(final_status)
        finally:
            if not self.closing:
                self.capture_button.config(
                    state="normal" if self.controller else "disabled"
                )
                self.series_start_button.config(
                    state="normal" if self.controller else "disabled"
                )
                self.refresh_button.config(
                    state="normal" if self.controller else "disabled"
                )
                self.laptop_jpeg_only_check.config(state="normal")

    def _scan_after_series(self, final_status, progress_status=None, play_sound=False):
        """Skanuje aparat raz po zakończeniu/zatrzymaniu całej serii."""
        if self.closing or not self.controller:
            self._restore_after_series()
            return

        self.series_capture_in_progress = False
        self.status_var.set(final_status + " • sprawdzam ustawienia...")
        if progress_status is not None:
            self.series_progress_var.set(progress_status)

        fast_jpeg = self.series_jpeg_only
        future = self.executor.submit(
            self.controller.scan_and_prepare_capture_setup,
            fast_jpeg,
        )
        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self._scan_after_series_done,
                f,
                final_status,
                progress_status,
                play_sound,
            )
        )

    def _scan_after_series_done(
        self,
        future,
        final_status,
        progress_status,
        play_sound,
    ):
        try:
            self.config_paths = future.result()
        except Exception as exc:
            self.status_var.set(
                final_status + f" • błąd skanowania ustawień: {exc}"
            )
        else:
            self.status_var.set(final_status)

        if progress_status is not None:
            self.series_progress_var.set(progress_status)

        self._restore_after_series()

        if play_sound:
            self.play_completion_sound()

    # --------------------------------------------------------
    # CAPTURE
    # --------------------------------------------------------

    def capture_photo(self):
        if not self.controller:
            return

        try:
            save_dir = self.get_save_directory()
        except ValueError as exc:
            messagebox.showerror(
                "Folder zapisu",
                str(exc),
            )
            return

        # Zapamiętujemy tryb na czas tego zdjęcia, żeby zmiana checkboxa
        # w trakcie ekspozycji nie zmieniła zachowania po wykonaniu klatki.
        self.current_capture_jpeg_only = bool(
            self.laptop_jpeg_only_var.get()
        )

        self.capture_button.config(
            state="disabled"
        )
        self.series_start_button.config(state="disabled")
        self.refresh_button.config(state="disabled")
        self.laptop_jpeg_only_check.config(state="disabled")

        self.status_var.set(
            "Robię zdjęcie RAW + JPEG..."
        )

        # Pierwszy etap pobiera tylko JPEG. Ścieżkę RAW ustalamy tylko wtedy,
        # gdy po JPEG ma nastąpić transfer NEF na laptop.
        future = self.executor.submit(
            self.controller.capture_photo,
            save_dir,
            not self.current_capture_jpeg_only,
            self.current_capture_jpeg_only,
        )

        future.add_done_callback(
            lambda f: self.root.after(
                0,
                self.capture_jpeg_done,
                f,
            )
        )

    def capture_jpeg_done(self, future):
        try:
            result = future.result()

        except Exception as exc:
            self.capture_button.config(
                state="normal"
            )
            self.series_start_button.config(
                state="normal" if self.controller else "disabled"
            )
            self.refresh_button.config(
                state="normal" if self.controller else "disabled"
            )
            self.laptop_jpeg_only_check.config(state="normal")

            self.status_var.set(
                f"Błąd wykonywania zdjęcia: {exc}"
            )

            messagebox.showerror(
                "Błąd aparatu",
                str(exc),
            )
            return

        # JPEG jest już na dysku — pokazujemy go NATYCHMIAST,
        # nie czekając na transfer dużego NEF-a.
        try:
            image = Image.open(
                result["jpeg"]
            )
            image.load()

            self.display_last_image(
                image.copy()
            )

        except Exception as exc:
            self.status_var.set(
                f"JPEG zapisany, ale podgląd się nie udał: {exc}"
            )

        if self.current_capture_jpeg_only:
            try:
                jpeg_mb = result["jpeg"].stat().st_size / (1024 * 1024)
                jpeg_info = f"{jpeg_mb:.1f} MB"
            except OSError:
                jpeg_info = "rozmiar nieznany"
            final_status = (
                f"JPEG zapisany na laptopie: {result['jpeg'].name} ({jpeg_info}) • "
                "RAW został wyłącznie na karcie SD"
            )
            self._scan_after_single_capture(final_status)
            return

        self.status_var.set(
            "JPEG gotowy • pobieram NEF w tle..."
        )

        # Drugi etap: RAW. Nadal używamy jednego wątku gphoto2,
        # więc aparat nie dostaje równoległych poleceń USB.
        raw_future = self.executor.submit(
            self.controller.download_raw,
            result,
        )

        raw_future.add_done_callback(
            lambda f: self.root.after(
                0,
                self.capture_raw_done,
                f,
            )
        )

    def capture_raw_done(self, future):
        try:
            result = future.result()

        except Exception as exc:
            final_status = f"JPEG zapisany; błąd pobierania RAW: {exc}"
            messagebox.showerror(
                "Błąd pobierania RAW",
                str(exc),
            )
            self._scan_after_single_capture(final_status)
            return

        final_status = (
            "Zapisano na dysku i karcie: "
            f"{result['jpeg'].name} + {result['raw'].name}"
        )
        self._scan_after_single_capture(final_status)

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

        try:
            self.series_save_dir = self.get_save_directory()
        except ValueError as exc:
            messagebox.showerror(
                "Folder zapisu",
                str(exc),
            )
            return

        # Konfiguracja została przygotowana wcześniej (przy starcie aplikacji
        # albo przy zmianie trybu JPEG). W samej serii nie skanujemy ustawień.

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
        self.series_jpeg_only = bool(self.laptop_jpeg_only_var.get())

        self.series_start_button.config(state="disabled")
        self.series_stop_button.config(state="normal")
        self.capture_button.config(state="disabled")
        self.refresh_button.config(state="disabled")
        self.save_dir_button.config(state="disabled")
        self.laptop_jpeg_only_check.config(state="disabled")

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
            final_status = (
                f"Seria zatrzymana: {self.series_done} / {self.series_total}"
            )
            progress_status = (
                f"Zatrzymano: {self.series_done} / {self.series_total}"
            )
            self._scan_after_series(
                final_status,
                progress_status,
                play_sound=False,
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
            self.controller.capture_photo,
            self.series_save_dir,
            not self.series_jpeg_only,
            self.series_jpeg_only,
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
        try:
            result = future.result()

        except Exception as exc:
            self.series_capture_in_progress = False
            was_running = self.series_running
            self.series_running = False

            final_status = f"Błąd serii przy zdjęciu {shot_number}: {exc}"
            progress_status = f"Błąd przy {shot_number}/{self.series_total}"

            if was_running:
                messagebox.showerror(
                    "Błąd serii",
                    str(exc),
                )

            self._scan_after_series(
                final_status,
                progress_status,
                play_sound=False,
            )
            return

        # JPEG jest już dostępny — aktualizujemy podgląd przed RAW-em.
        try:
            image = Image.open(
                result["jpeg"]
            )
            image.load()
            self.display_last_image(
                image.copy()
            )
        except Exception:
            pass

        if self.series_jpeg_only:
            self.series_capture_in_progress = False
            self._series_shot_finished(shot_number, jpeg_only=True)
            return

        self.status_var.set(
            f"Seria {shot_number}/{self.series_total}: "
            "JPEG gotowy • pobieram NEF..."
        )

        self.series_progress_var.set(
            f"{self.series_done} / {self.series_total}  •  RAW {shot_number}"
        )

        raw_future = self.executor.submit(
            self.controller.download_raw,
            result,
        )

        raw_future.add_done_callback(
            lambda f, n=shot_number: self.root.after(
                0,
                self._series_raw_done,
                f,
                n,
            )
        )

    def _series_raw_done(self, future, shot_number):
        self.series_capture_in_progress = False

        try:
            future.result()

        except Exception as exc:
            was_running = self.series_running
            self.series_running = False

            final_status = (
                f"JPEG zapisany, ale błąd RAW przy {shot_number}: {exc}"
            )
            progress_status = f"Błąd RAW przy {shot_number}/{self.series_total}"

            if was_running:
                messagebox.showerror(
                    "Błąd pobierania RAW",
                    str(exc),
                )

            self._scan_after_series(
                final_status,
                progress_status,
                play_sound=False,
            )
            return

        # Mamy JPG + NEF na dysku oraz oba pliki na karcie.
        self._series_shot_finished(shot_number, jpeg_only=False)

    def _series_shot_finished(self, shot_number, jpeg_only=False):
        """Kończy obsługę jednej klatki i planuje następną."""
        self.series_done = shot_number

        self.series_progress_var.set(
            f"{self.series_done} / {self.series_total}"
        )

        if not self.series_running:
            final_status = (
                f"Seria zatrzymana: {self.series_done} / {self.series_total}"
            )
            progress_status = (
                f"Zatrzymano: {self.series_done} / {self.series_total}"
            )
            self._scan_after_series(
                final_status,
                progress_status,
                play_sound=False,
            )
            return

        if self.series_done >= self.series_total:
            self._finish_series()
            return

        # Interwał liczony jest od rozpoczęcia poprzedniej ekspozycji.
        # W trybie JPEG-only nie czekamy na żaden transfer RAW, więc kolejna
        # klatka może ruszyć zgodnie z zadanym interwałem dużo wcześniej.
        elapsed = 0.0
        if self.series_last_start is not None:
            elapsed = time.monotonic() - self.series_last_start

        wait_seconds = max(
            0.0,
            self.series_interval - elapsed,
        )

        mode_text = "JPEG na laptopie; RAW na SD" if jpeg_only else "JPEG + RAW na laptopie"
        self.status_var.set(
            f"Seria: {self.series_done}/{self.series_total}; {mode_text}; "
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

        final_status = (
            f"Seria zakończona: {self.series_done}/{self.series_total}"
        )
        progress_status = (
            f"✓ Gotowe: {self.series_done} / {self.series_total}"
        )

        self.play_completion_sound()

        self._scan_after_series(
            final_status,
            progress_status,
            play_sound=False,
        )

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
        self.save_dir_button.config(state="normal")
        self.laptop_jpeg_only_check.config(state="normal")

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
    # IMAGES / JEDEN DUŻY PODGLĄD
    # --------------------------------------------------------

    def set_viewer_mode(self, mode):
        if mode not in {"last", "live"}:
            return

        self.viewer_mode.set(mode)
        self.update_viewer_buttons()
        self.refresh_viewer()

    def update_viewer_buttons(self):
        mode = self.viewer_mode.get()

        # Aktywny widok ma wyłączony odpowiadający mu przycisk,
        # dzięki czemu od razu widać, który panel jest wybrany.
        self.viewer_last_button.config(
            state="disabled" if mode == "last" else "normal"
        )

        self.viewer_live_button.config(
            state="disabled" if mode == "live" else "normal"
        )

        self.viewer_frame.config(
            text=(
                "Live View"
                if mode == "live"
                else "Ostatnie zdjęcie"
            )
        )

    def on_viewer_resize(self, _event=None):
        # Configure może wywoływać się wiele razy podczas przeciągania
        # rozmiaru okna, więc odświeżamy z małym opóźnieniem.
        if self.viewer_resize_after is not None:
            try:
                self.root.after_cancel(
                    self.viewer_resize_after
                )
            except tk.TclError:
                pass

        self.viewer_resize_after = self.root.after(
            100,
            self.refresh_viewer,
        )

    def get_viewer_max_size(self):
        width = self.viewer_image_label.winfo_width()
        height = self.viewer_image_label.winfo_height()

        # Przy pierwszym renderze Tk może jeszcze raportować 1x1.
        if width < 100:
            width = 900
        if height < 100:
            height = 700

        return (
            max(100, width - 12),
            max(100, height - 12),
        )

    def prepare_image(
        self,
        image,
        max_size=None,
    ):
        # Pracujemy na kopii, bo thumbnail modyfikuje obraz.
        image = image.copy()

        # Obrót na podstawie EXIF.
        image = ImageOps.exif_transpose(
            image
        )

        if max_size is None:
            max_size = self.get_viewer_max_size()

        image.thumbnail(
            max_size,
            Image.Resampling.LANCZOS,
        )

        return image

    def render_viewer_image(self, image):
        prepared = self.prepare_image(
            image,
            self.get_viewer_max_size(),
        )

        self.viewer_photoimage = ImageTk.PhotoImage(
            prepared
        )

        self.viewer_image_label.config(
            image=self.viewer_photoimage,
            text="",
        )

    def refresh_viewer(self):
        self.viewer_resize_after = None
        mode = self.viewer_mode.get()

        if mode == "live":
            if self.live_image_pil is not None:
                self.render_viewer_image(
                    self.live_image_pil
                )
            else:
                self.viewer_photoimage = None
                self.viewer_image_label.config(
                    image="",
                    text=(
                        "Live View uruchomiony — czekam na obraz..."
                        if self.live_enabled
                        else "Live View wyłączony"
                    ),
                )

        else:
            if self.last_image_pil is not None:
                self.render_viewer_image(
                    self.last_image_pil
                )
            else:
                self.viewer_photoimage = None
                self.viewer_image_label.config(
                    image="",
                    text="Nie wykonano jeszcze zdjęcia",
                )

    def display_live_image(self, image):
        # Zachowujemy pełniejszą wersję PIL, a skalowanie wykonujemy
        # dopiero dla aktualnego rozmiaru dużego panelu.
        self.live_image_pil = ImageOps.exif_transpose(
            image.copy()
        )

        if self.viewer_mode.get() == "live":
            self.render_viewer_image(
                self.live_image_pil
            )

    def display_last_image(self, image):
        self.last_image_pil = ImageOps.exif_transpose(
            image.copy()
        )

        if self.viewer_mode.get() == "last":
            self.render_viewer_image(
                self.last_image_pil
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

        if self.viewer_resize_after is not None:
            try:
                self.root.after_cancel(
                    self.viewer_resize_after
                )
            except tk.TclError:
                pass
            self.viewer_resize_after = None

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