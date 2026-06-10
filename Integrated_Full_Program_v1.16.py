"""
Smart Packaging Line - HMI Inspection Station (v4 / v1.16)
Capstone Project - TE Connectivity

v4 changes:
- UI split into two windows: Operator window (clean, large status) +
  Configuration window (all technical parameters, log, stats)
- Operator window targets non-technical users: large color-coded result,
  Spanish labels, no z-scores or sigma counts visible
- Configuration window keeps all technical info: scale settings, recipe
  editor, advanced parameters, session statistics, log viewer
- F1 / F2 / F3 shortcuts open the configuration window on different tabs
- All classification / serial / logging logic preserved unchanged

Author: Ian (with Claude assistance)
"""

import json
import csv
import re
import socket
import threading
import queue
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

EMPTY_PLATFORM_G = 0.5

# ============================================================
# PATHS
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = LOG_DIR / f"inspection_log_{datetime.now().strftime('%Y%m%d')}.csv"


# ============================================================
# SPC ENGINE  (unchanged from v3)
# ============================================================
# Component IDs that are always present but never identified as "missing"
# (e.g. the plastic bag is part of every recipe but isn't a candidate for L2)
NON_CANDIDATE_COMPONENTS = {"PB"}


class Classifier:
    def __init__(self, components: dict, recipes: dict, k_sigma: float = 6.0):
        self.components = components
        self.recipes = recipes
        self.k_sigma = k_sigma

    def expected_weight(self, recipe_id: str) -> float:
        recipe = self.recipes[recipe_id]
        return sum(
            count * self.components[comp_id]["mean"]
            for comp_id, count in recipe["composition"].items()
        )

    def expected_std(self, recipe_id: str) -> float:
        recipe = self.recipes[recipe_id]
        variance = sum(
            count * (self.components[comp_id]["std"] ** 2)
            for comp_id, count in recipe["composition"].items()
        )
        return variance ** 0.5

    def bounds(self, recipe_id: str):
        mu = self.expected_weight(recipe_id)
        sigma = self.expected_std(recipe_id)
        return (mu - self.k_sigma * sigma, mu + self.k_sigma * sigma)

    def classify(self, recipe_id: str, measured_weight: float) -> dict:
        mu = self.expected_weight(recipe_id)
        sigma = self.expected_std(recipe_id)
        lower, upper = self.bounds(recipe_id)
        deviation = measured_weight - mu
        sigma_count = abs(deviation) / sigma if sigma > 0 else 0
        status = "OK" if lower <= measured_weight <= upper else "NG"

        missing_component = None
        confidence = None
        candidate_probabilities = {}

        if status == "NG":
            # L2 candidates: components in the recipe EXCEPT non-candidates (e.g. the bag).
            # The bag is part of every kit but cannot be the "missing piece".
            recipe_components = [
                cid for cid in self.recipes[recipe_id]["composition"].keys()
                if cid not in NON_CANDIDATE_COMPONENTS
            ]
            deficit = mu - measured_weight
            ambiguity_margin = 2 * sigma

            matches = {
                comp_id: abs(deficit - self.components[comp_id]["mean"])
                for comp_id in recipe_components
            }
            sorted_matches = sorted(matches.items(), key=lambda x: x[1])
            best_comp = sorted_matches[0][0]
            best_dist = sorted_matches[0][1]

            is_ambiguous = False
            if len(sorted_matches) > 1:
                second_comp = sorted_matches[1][0]
                boundary = (self.components[best_comp]["mean"] +
                            self.components[second_comp]["mean"]) / 2
                if abs(deficit - boundary) < ambiguity_margin:
                    is_ambiguous = True

            if is_ambiguous:
                missing_component = f"AMBIGUOUS: {best_comp} / {second_comp}"
                confidence = "LOW"
            else:
                missing_component = best_comp
                comp_std = self.components[best_comp]["std"]
                z = best_dist / comp_std if comp_std > 0 else float("inf")
                confidence = "HIGH" if z < 1 else ("MEDIUM" if z < 2 else "LOW")

            candidate_probabilities = {
                comp_id: round(
                    max(0.0, 100.0 * (1 - matches[comp_id] /
                        (ambiguity_margin + 1e-9))), 1)
                for comp_id in recipe_components
            }
        return {
            "recipe": recipe_id,
            "measured_weight": measured_weight,
            "expected_weight": mu,
            "expected_std": sigma,
            "lower_bound": lower,
            "upper_bound": upper,
            "deviation": deviation,
            "sigma_count": sigma_count,
            "status": status,
            "missing_component": missing_component,
            "confidence": confidence,
            "candidate_probabilities": candidate_probabilities,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }


# ============================================================
# SERIAL READER (unchanged from v3)
# ============================================================
class ScaleReader(threading.Thread):
    NUMBER_RE = re.compile(r'[-+]?(?:\d+\.\d*|\.\d+|\d+)')

    def __init__(self, port: str, baudrate: int, out_queue: queue.Queue, status_cb=None):
        super().__init__(daemon=True)
        self.port = port
        self.baudrate = baudrate
        self.out_queue = out_queue
        self.status_cb = status_cb
        self._stop_event = threading.Event()
        self._ser = None

    def stop(self):
        self._stop_event.set()
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass

    def _set_status(self, state, detail=""):
        if self.status_cb:
            try:
                self.status_cb(state, detail)
            except Exception:
                pass

    def run(self):
        if not SERIAL_AVAILABLE:
            self._set_status("error", "pyserial not installed")
            return
        self._set_status("connecting", f"{self.port} @ {self.baudrate}")
        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=1)
        except Exception as e:
            self._set_status("error", str(e))
            return
        self._set_status("connected", f"{self.port} @ {self.baudrate}")
        while not self._stop_event.is_set():
            try:
                line = self._ser.readline()
                if not line:
                    continue
                text = line.decode('utf-8', errors='ignore').strip()
                if not text:
                    continue
                match = self.NUMBER_RE.search(text)
                if match:
                    try:
                        value = float(match.group())
                        self.out_queue.put(("weight", value, text))
                    except ValueError:
                        pass
            except serial.SerialException as e:
                self._set_status("error", f"Serial error: {e}")
                break
            except Exception as e:
                self._set_status("error", f"Unexpected: {e}")
                break
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        self._set_status("disconnected", "")


# ============================================================
# STABILITY DETECTOR (unchanged from v3)
# ============================================================
class StabilityDetector:
    def __init__(self, window_size: int = 5, tolerance: float = 0.005):
        self.window_size = window_size
        self.tolerance = tolerance
        self.readings = []

    def push(self, value: float) -> tuple:
        self.readings.append(value)
        if len(self.readings) > self.window_size:
            self.readings.pop(0)
        if len(self.readings) < self.window_size:
            return (False, None)
        window_mean = sum(self.readings) / len(self.readings)
        max_dev = max(abs(r - window_mean) for r in self.readings)
        if max_dev <= self.tolerance:
            return (True, window_mean)
        return (False, None)

    def reset(self):
        self.readings.clear()


# ============================================================
# CELL AUTOMATION — Merlic + JAKA + Scale orchestration
# ============================================================
#
# Pipeline executed by CellOrchestrator on every iteration:
#
#   1) Ask Merlic which 3x3 cell is occupied (REST scan).
#   2) Activate JAKA digital output 1..9 mapped to that cell.
#   3) Load + play the JAKA pick-and-place program (e.g. "Test_ver1").
#      The program picks the part from the cell and places it on the
#      scale, then waits internally for digital input 10 (NG) or 11 (OK).
#   4) Wait for the scale to stabilize (max stability_timeout_s).
#   5) Classify the kit with the SPC classifier.
#   6) Set DO10 (NG) or DO11 (OK). JAKA resumes and routes the kit.
#   7) Reset DOs and loop.
#
# All JAKA DO indices below are 0-based to match the JAKA REST/TCP
# convention used in the original REST_SERVER.py (DO1 -> index 0).
# ============================================================

# Cell -> 0-based DO index for "this cell is occupied" trigger (DO1..DO9)
CELL_TO_DO_INDEX = {
    (0, 0): 0, (0, 1): 1, (0, 2): 2,
    (1, 0): 3, (1, 1): 4, (1, 2): 5,
    (2, 0): 6, (2, 1): 7, (2, 2): 8,
}

# Merlic ROI label -> cell (row, col)
DEFAULT_ROI_MAP = {
    "CheckPresence_ROI1": (0, 0),
    "CheckPresence_ROI2": (0, 1),
    "CheckPresence_ROI3": (0, 2),
    "CheckPresence_ROI4": (1, 0),
    "CheckPresence_ROI5": (1, 1),
    "CheckPresence_ROI6": (1, 2),
    "CheckPresence_ROI7": (2, 0),
    "CheckPresence_ROI8": (2, 1),
    "CheckPresence_ROI9": (2, 2),
}

# Result DOs read by the JAKA program after the kit is on the scale
DO_INDEX_NG = 9    # DO10 -> result = NG, robot drops to reject bin
DO_INDEX_OK = 10   # DO11 -> result = OK, robot drops to pack-out bin
ALL_DO_INDICES = list(range(11))  # 0..10 -> DO1..DO11 cleared between cycles


class JakaClient:
    """Thin TCP wrapper around the JAKA REST/TCP command port."""

    def __init__(self, ip: str, port: int, timeout: float = 10.0):
        self.ip = ip
        self.port = port
        self.timeout = timeout

    def _send(self, cmd: dict) -> str:
        """Open a fresh socket, send one JSON command, read one response."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect((self.ip, self.port))
            s.sendall((json.dumps(cmd) + "\r\n").encode())
            time.sleep(0.3)
            try:
                resp = s.recv(2048).decode(errors="ignore")
            except socket.timeout:
                resp = ""
            return resp
        finally:
            try:
                s.close()
            except Exception:
                pass

    def set_digital_output(self, index: int, value: int) -> str:
        return self._send({
            "cmdName": "set_digital_output",
            "type": 0,
            "index": index,
            "value": value,
        })

   # def clear_all_outputs(self):
   #     for i in ALL_DO_INDICES:
   #         try:
   #             self.set_digital_output(i, 0)
   #         except Exception:
   #             pass

    def power_on(self) -> str:
        return self._send({"cmdName": "power_on"})

    def enable_robot(self) -> str:
        return self._send({"cmdName": "enable_robot"})

    def load_program(self, name: str) -> str:
        return self._send({
            "cmdName": "load_program",
            "perams": {"programName": name},
        })

    def play_program(self) -> str:
        return self._send({"cmdName": "play_program", "perams": {}})

    def signal_result(self, status: str):
        """status = 'OK' or 'NG'. Activates DO11 or DO10 respectively."""
        if status == "OK":
            self.set_digital_output(DO_INDEX_OK, 1)
        else:
            self.set_digital_output(DO_INDEX_NG, 1)

class EStopMonitor(threading.Thread):
    """
    Polls the JAKA status stream (port 10000) every 200 ms.

    Confirmed field mapping (tested on hardware):
      emergency_stop == 1  →  E-Stop activo
      emergency_stop == 0  →  Normal
      din[0]         == 1  →  Reset button (DI1) presionado
    """

    POLL_INTERVAL = 0.2

    def __init__(self, jaka_ip: str, event_queue: queue.Queue):
        super().__init__(daemon=True)
        self.jaka_ip     = jaka_ip
        self.event_queue = event_queue
        self._stop_event  = threading.Event()
        self._estop_active = False

    def stop(self):
        self._stop_event.set()

    def _read_status(self) -> dict | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((self.jaka_ip, 10000))
            buf = b""
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    buf += s.recv(8192)
                except socket.timeout:
                    break
                for line in buf.decode(errors="ignore").split("\n"):
                    line = line.strip()
                    if line.startswith("{") and "emergency_stop" in line:
                        try:
                            s.close()
                            return json.loads(line)
                        except Exception:
                            pass
            s.close()
        except Exception:
            pass
        return None

    def run(self):
            waiting_for_reset = False

            while not self._stop_event.is_set():
                status = self._read_status()
                if status is not None:
                    estop_now = status.get("emergency_stop", 0) == 1
                    di1_now   = status.get("din", [0])[0] == 1

                    if estop_now and not self._estop_active:
                        # E-Stop se presionó
                        self._estop_active  = True
                        waiting_for_reset   = False
                        self.event_queue.put(("estop", "pressed"))

                    elif not estop_now and self._estop_active and not waiting_for_reset:
                        # Hongo desbloqueado — ahora espera el reset
                        waiting_for_reset = True

                    elif waiting_for_reset and di1_now:
                        # Reset presionado — limpiar
                        self._estop_active = False
                        waiting_for_reset  = False
                        self.event_queue.put(("estop", "reset"))

                self._stop_event.wait(self.POLL_INTERVAL)

class MerlicClient:
    """REST client for the Merlic vision system."""

    def __init__(self, base_url: str, roi_map: dict = None, timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.roi_map = roi_map or DEFAULT_ROI_MAP
        self.timeout = timeout

    def scan(self):
        """Trigger a Merlic recipe and return the (row, col) of the
        first occupied ROI, or None if no kit is detected / on error."""
        if not REQUESTS_AVAILABLE:
            return None
        try:
            requests.post(
                f"{self.base_url}/recipes/actions",
                json={"action": "StartSingleJob", "recipe_id": 0},
                timeout=self.timeout,
            )
            time.sleep(2)
            resp = requests.get(
                f"{self.base_url}/results",
                params={"limit": 1},
                timeout=self.timeout,
            )
            results = resp.json()
            if not results:
                return None
            content = results[0].get("content", {})
            for roi, cell in self.roi_map.items():
                if content.get(roi, False):
                    return cell
            return None
        except Exception:
            return None


class CellOrchestrator(threading.Thread):
    """Background thread that drives the Merlic -> JAKA -> Scale -> JAKA loop.

    The orchestrator does not classify by itself: it tells the GUI via
    callbacks what to do, and waits on an Event that the GUI sets when
    a stable weight arrives and the classifier produces a verdict.
    """

    def __init__(self,
                 merlic: MerlicClient,
                 jaka: JakaClient,
                 program_name: str,
                 stability_timeout_s: float,
                 event_queue: queue.Queue,
                 inspect_request_cb,
                 wait_for_stable_cb,
                 counter_cb,
                 reset_ui_cb,
                 get_live_weight_cb=None):
        
        super().__init__(daemon=True)
        self.merlic = merlic
        self.jaka = jaka
        self.program_name = program_name
        self.stability_timeout_s = stability_timeout_s
        self.event_queue = event_queue
        self.inspect_request_cb = inspect_request_cb
        self.wait_for_stable_cb = wait_for_stable_cb
        self.counter_cb = counter_cb
        self.reset_ui_cb = reset_ui_cb
        self.get_live_weight_cb = get_live_weight_cb
        self._last_sent_status = None   
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _emit(self, state: str, detail: str = ""):
        self.event_queue.put(("orch_state", state, detail))

    def run(self):
        self._emit("running", "Esperando kit en la matriz")
        while not self._stop_event.is_set():

            # --- 0) ¿Hay kit residual en la báscula? ---
            # Si la báscula aún tiene peso (kit del ciclo anterior no retirado),
            # reenviar el último resultado al robot SIN incrementar contadores.
            if self.get_live_weight_cb is not None:
                current_w = self.get_live_weight_cb()
                if current_w is not None and current_w >= EMPTY_PLATFORM_G:
                    self._emit(
                        "signaling",
                        f"Báscula ocupada ({current_w:.2f} g) — dando RUN al robot"
                    )
                    try:
                        self.jaka.signal_result(self._last_sent_status or "NG")
                        self.jaka.play_program()
                    except Exception as e:
                        self._emit("error", f"JAKA play error: {e}")
                    time.sleep(20.0)
                    continue   # NO escanea Merlic hasta que la báscula esté vacía

            # --- 1) Merlic scan ---
            self._emit("scanning", "Consultando Merlic")
            cell = self.merlic.scan()
            if self._stop_event.is_set():
                break
            if cell is None:
                self._emit("idle", "Sin kit detectado")
                time.sleep(1.0)
                continue

            row, col = cell
            do_idx = CELL_TO_DO_INDEX[cell]
            self._emit("cell_detected", f"Celda ({row},{col}) — DO{do_idx + 1}")

            # --- 2) Activate the cell DO ---
            try:
                self.jaka.set_digital_output(do_idx, 1)
            except Exception as e:
                self._emit("error", f"JAKA DO error: {e}")
                time.sleep(2.0)
                continue

            # --- 3) Small settle before launching program ---
            time.sleep(2.0)
            if self._stop_event.is_set():
                break

            # --- 4) Load + play JAKA program ---
            try:
                self._emit("jaka_loading", f"Cargando {self.program_name}")
                self.jaka.load_program(self.program_name)
                time.sleep(1.0)
                self._emit("jaka_running", "Robot tomando pieza")
                self.jaka.play_program()
            except Exception as e:
                self._emit("error", f"JAKA program error: {e}")
                time.sleep(2.0)
                continue
            time.sleep(15.0) # TIMESLEEP DESPUES DE PLAY JAKA PROGRAM

            # --- 5) Wait for the scale to stabilize ---
            self._emit("weighing", "Esperando peso estable")
            weight = self.wait_for_stable_cb(self.stability_timeout_s)
            if self._stop_event.is_set():
                break
            
            if weight is None:
                self._emit("timeout", "Báscula no se estabilizó (10 s)")
                # Default to NG so the JAKA still routes the kit (safe side)
                try:
                    self.jaka.signal_result("NG")
                except Exception:
                    pass
                time.sleep(2.0)
                continue

            if weight < EMPTY_PLATFORM_G:
                self._emit("weighing", "Báscula vacía — esperando kit")
                time.sleep(1.0)
                continue
    
            # --- 6) Ask GUI to classify; it returns OK or NG ---
            self._emit("classifying", f"Peso: {weight:.2f} g")
            status = self.inspect_request_cb(weight)
            if status not in ("OK", "NG"):
                status = "NG"

            # --- 7) Signal result to JAKA ---

            try:

                self._emit("signaling", f"Enviando {status} al JAKA")
                self.jaka.signal_result(status)
                self._last_sent_status = status
                time.sleep(2.0)  # JAKA lee el DO y empieza a rutear
                self.counter_cb(status)

                # Relanzar el programa para el siguiente ciclo
                self._emit("jaka_loading", f"Recargando {self.program_name}")
                self.jaka.load_program(self.program_name)
                time.sleep(1.0)
                self._emit("jaka_running", "Robot listo para siguiente pieza")
                self.jaka.play_program()
                
            except Exception as e:

                self._emit("error", f"JAKA signal error: {e}")
            
            #####SOLUCION 2 // DAR PLAY AL PROGRAMA NUEVAMENTE PARA UTILIZAR BIEN LA SALIDA DIGITAL. 

            time.sleep(17.0)
            self.reset_ui_cb()
            


# ============================================================
# CONFIG  (unchanged from v3)
# ============================================================
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        default = {
            "components": {
                "S1": {"description": "Tornillo tipo 1", "mean": 2.7533, "std": 0.0066},
                "S2": {"description": "Tornillo tipo 2", "mean": 4.0360, "std": 0.0138},
                "W1": {"description": "Rondana tipo 1", "mean": 0.9933, "std": 0.0140},
                "W2": {"description": "Rondana tipo 2", "mean": 4.5627, "std": 0.0252},
                "N1": {"description": "Tuerca tipo 1", "mean": 1.1557, "std": 0.0276},
                "N2": {"description": "Tuerca tipo 2", "mean": 1.3743, "std": 0.0251},
                "PB": {"description": "Bolsa de plástico", "mean": 0.9700, "std": 0.0400},
            },
            "recipes": {
                "R3": {"description": "Ensamblaje de Conectores (PB+S2+W2+N2)",
                       "composition": {"PB": 1, "S2": 1, "W2": 1, "N2": 1}},
                "R4": {"description": "Ensamblaje Estándar (PB+S1+W1+N1+S2)",
                       "composition": {"PB": 1, "S1": 1, "W1": 1, "N1": 1, "S2": 1}},
                "R6": {"description": "Ensamblaje Completo (PB+S1+W1×2+N2×2+N1)",
                       "composition": {"PB": 1, "S1": 1, "W1": 2, "N2": 2, "N1": 1}},
            },
            "settings": {
                "k_sigma": 6.0,
                "serial_port": "COM6",
                "baudrate": 9600,
                "auto_inspect_on_stable": True,
                "stability_window": 5,
                "stability_tolerance_g": 0.005,
                "automation_enabled": False,
                "merlic_base_url": "http://127.0.0.1:8040/api/v1",
                "jaka_ip": "10.5.5.100",
                "jaka_port": 10001,
                "jaka_program_name": "Test_ver1",
                "stability_timeout_s": 10.0,
            },
        }
        save_config(default)
        return default

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    s = cfg.setdefault("settings", {})
    s.setdefault("k_sigma", 6.0)
    s.setdefault("serial_port", "COM6")
    s.setdefault("baudrate", 9600)
    s.setdefault("auto_inspect_on_stable", True)
    s.setdefault("stability_window", 5)
    s.setdefault("stability_tolerance_g", 0.005)
    s.setdefault("automation_enabled", False)
    s.setdefault("merlic_base_url", "http://127.0.0.1:8040/api/v1")
    s.setdefault("jaka_ip", "10.5.5.100")
    s.setdefault("jaka_port", 10001)
    s.setdefault("jaka_program_name", "Test_ver1")
    s.setdefault("stability_timeout_s", 10.0)

    # Migration: add bag (PB) to components and recipes if missing in older configs
    comps = cfg.setdefault("components", {})
    if "PB" not in comps:
        comps["PB"] = {"description": "Bolsa de plástico",
                       "mean": 0.9700, "std": 0.0400}
    recipes = cfg.setdefault("recipes", {})
    for rid, recipe in recipes.items():
        comp = recipe.setdefault("composition", {})
        if "PB" not in comp:
            comp["PB"] = 1

    return cfg


def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def log_inspection(result: dict):
    file_exists = LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        fieldnames = [
            "timestamp", "recipe", "measured_weight", "expected_weight",
            "expected_std", "lower_bound", "upper_bound", "deviation",
            "sigma_count", "status", "missing_component", "confidence",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: result.get(k, "") for k in fieldnames})


# ============================================================
# COLOR PALETTE  (shared between both windows)
# ============================================================
class Theme:
    # Operator window — clean, light, friendly
    OP_BG          = "#F5F6F8"   # window background (light gray)
    OP_CARD        = "#FFFFFF"   # white card
    OP_TITLEBAR    = "#1E2D3F"   # dark navy
    OP_TITLEBAR_FG = "#FFFFFF"
    OP_RECIPE_BG   = "#F0F2F5"
    OP_RECIPE_PILL = "#2E7CD6"
    OP_TEXT        = "#1B2733"
    OP_TEXT_DIM    = "#6B7785"
    OP_BORDER      = "#D9DEE5"

    # Status zones
    OK_BG          = "#C8E6C9"   # light green panel
    OK_FG          = "#1B5E20"   # dark green text
    OK_ACCENT      = "#2E7D32"

    NG_BG          = "#FFB3B0"   # light red panel
    NG_FG          = "#8B0000"   # dark red text
    NG_ACCENT      = "#C62828"

    AMB_BG         = "#FFE0B2"
    AMB_FG         = "#8B4513"
    AMB_ACCENT     = "#E65100"

    WAIT_BG        = "#E8EBEE"
    WAIT_FG        = "#6B7785"

    # Action button
    BTN_BLUE       = "#2E7CD6"
    BTN_BLUE_HOVER = "#1E5FA8"

    # Config window — dark technical theme
    CFG_BG         = "#1E2A38"
    CFG_PANEL      = "#2C3E50"
    CFG_INPUT      = "#34495E"
    CFG_ACCENT     = "#3498DB"
    CFG_OK         = "#27AE60"
    CFG_NG         = "#E74C3C"
    CFG_WARN       = "#F39C12"
    CFG_TEXT       = "#ECF0F1"
    CFG_DIM        = "#95A5A6"
    CFG_BORDER     = "#4A6278"


# ============================================================
# OPERATOR WINDOW — main visualization (non-technical user)
# ============================================================
class OperatorWindow(tk.Tk):
    """
    Clean window designed for a non-technical operator. Shows only:
    - Active recipe + description (Spanish)
    - Big visual result: KIT COMPLETO / KIT INCOMPLETO / REVISAR
    - Current weight vs. target
    - What component is missing (if any)
    - Session counters (Total / Completos / Con falla)
    - Big inspect button
    No z-scores, no sigma, no technical jargon visible here.
    """

    def __init__(self):
        super().__init__()
        self.title("TE Connectivity — Inspección de Kit")
        self.geometry("1100x720")
        self.configure(bg=Theme.OP_BG)
        self.minsize(960, 660)

        # ---- State ----
        self.config_data = load_config()
        self.classifier = Classifier(
            components=self.config_data["components"],
            recipes=self.config_data["recipes"],
            k_sigma=self.config_data["settings"]["k_sigma"],
        )

        self.current_recipe = tk.StringVar(
            value=list(self.config_data["recipes"].keys())[0])
        self.weight_var = tk.StringVar()
        self.live_weight = None
        self.auto_mode_var = tk.BooleanVar(
            value=self.config_data["settings"]["auto_inspect_on_stable"])

        self.inspection_count = 0
        self.ok_count = 0
        self.ng_count = 0

        self.serial_queue = queue.Queue()
        self.scale_reader = None
        self.stability = StabilityDetector(
            window_size=self.config_data["settings"]["stability_window"],
            tolerance=self.config_data["settings"]["stability_tolerance_g"],
        )
        self.last_stable_value = None
        self.last_inspected_value = None
        self.port_status = ("disconnected", "")

        # Config window reference (created lazily)
        self.config_window = None

        # ---- Automation state (Merlic + JAKA orchestrator) ----
        self.orchestrator = None
        self.orch_event_queue = queue.Queue()
        self.estop_monitor = None #E-Stop and Reset buttons logic.
        # Event + storage used by the orchestrator thread to wait for a
        # stable weight from the main GUI thread.
        self._stable_event = threading.Event()
        self._stable_weight_value = None

        self._kit_being_processed = False
        self._inspection_enabled = False
        # Set by on_inspect when running under the orchestrator so it can
        # report the verdict back without re-running classification.
        self._auto_last_status = None
        self.auto_status_var = tk.StringVar(value="Modo manual")

        # ---- Build UI ----
        self._build_ui()
        self._update_clock()
        self._update_recipe_info()
        self._poll_serial_queue()
        self._poll_orch_queue()
        self.after(500, self.connect_scale)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Shortcuts: F1 = recipes tab, F2 = scale tab, F3 = full config,
        #           F4 = toggle automation
        self.bind("<F1>", lambda e: self.open_config_window(tab=1))
        self.bind("<F2>", lambda e: self.open_config_window(tab=0))
        self.bind("<F3>", lambda e: self.open_config_window(tab=0))
        self.bind("<F4>", lambda e: self.toggle_automation())

    # ============================================================
    # UI BUILD
    # ============================================================
    def _build_ui(self):
        self._build_titlebar()
        self._build_recipe_bar()
        self._build_result_zone()
        self._build_info_row()
        self._build_counters()
        self._build_action_row()
        self._build_statusbar()

    def _build_titlebar(self):
        bar = tk.Frame(self, bg=Theme.OP_TITLEBAR, height=44)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        tk.Label(
            bar, text="TE Connectivity — Inspección de Kit",
            bg=Theme.OP_TITLEBAR, fg=Theme.OP_TITLEBAR_FG,
            font=("Consolas", 20, "bold"),
        ).pack(side="left", padx=18)

        self.clock_label = tk.Label(
            bar, text="",
            bg=Theme.OP_TITLEBAR, fg=Theme.OP_TITLEBAR_FG,
            font=("Consolas", 18),
        )
        self.clock_label.pack(side="right", padx=18)

    def _build_recipe_bar(self):
        bar = tk.Frame(self, bg=Theme.OP_RECIPE_BG, height=48)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        inner = tk.Frame(bar, bg=Theme.OP_RECIPE_BG)
        inner.pack(side="left", padx=18, pady=8)

        # Recipe pill
        self.recipe_pill = tk.Label(
            inner, text=self.current_recipe.get(),
            bg=Theme.OP_RECIPE_PILL, fg="white",
            font=("Helvetica", 19, "bold"),
            padx=12, pady=2,
        )
        self.recipe_pill.pack(side="left", padx=(0, 12))

        self.recipe_desc_label = tk.Label(
            inner, text="Receta activa: —",
            bg=Theme.OP_RECIPE_BG, fg=Theme.OP_TEXT,
            font=("Helvetica", 19),
        )
        self.recipe_desc_label.pack(side="left")

        # Recipe selector (right side, subtle)
        right = tk.Frame(bar, bg=Theme.OP_RECIPE_BG)
        right.pack(side="right", padx=18, pady=8)
        tk.Label(
            right, text="Cambiar receta:",
            bg=Theme.OP_RECIPE_BG, fg=Theme.OP_TEXT_DIM,
            font=("Helvetica", 15),
        ).pack(side="left", padx=(0, 6))
        recipe_menu = ttk.Combobox(
            right, textvariable=self.current_recipe,
            values=list(self.config_data["recipes"].keys()),
            state="readonly", font=("Helvetica", 16, "bold"),
            width=6,
        )
        recipe_menu.pack(side="left")
        recipe_menu.bind("<<ComboboxSelected>>",
                         lambda e: self._update_recipe_info())

    def _build_result_zone(self):
        """Big colored result panel — the centerpiece of the operator UI."""
        wrap = tk.Frame(self, bg=Theme.OP_BG)
        wrap.pack(fill="x", padx=18, pady=(14, 0))

        # The colored panel itself
        self.result_panel = tk.Frame(
            wrap, bg=Theme.WAIT_BG, height=280,
            highlightbackground=Theme.OP_BORDER, highlightthickness=1,
        )
        self.result_panel.pack(fill="x")
        self.result_panel.pack_propagate(False)

        # Big icon + title vertically centered
        self.result_icon = tk.Label(
            self.result_panel, text="◯",
            bg=Theme.WAIT_BG, fg=Theme.WAIT_FG,
            font=("Helvetica", 80, "bold"),
        )
        self.result_icon.pack(pady=(20, 0))

        self.result_title = tk.Label(
            self.result_panel, text="ESPERANDO KIT",
            bg=Theme.WAIT_BG, fg=Theme.WAIT_FG,
            font=("Helvetica", 56, "bold"),
        )
        self.result_title.pack()

        self.result_sub = tk.Label(
            self.result_panel,
            text="Coloca un kit en la báscula para iniciar",
            bg=Theme.WAIT_BG, fg=Theme.WAIT_FG,
            font=("Helvetica", 16),
        )
        self.result_sub.pack(pady=(4, 12))

    def _build_info_row(self):
        """Two-card row: current weight + what's missing."""
        row = tk.Frame(self, bg=Theme.OP_BG)
        row.pack(fill="x", padx=18, pady=(14, 0))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)

        # ----- LEFT: peso actual -----
        left = tk.Frame(
            row, bg=Theme.OP_CARD,
            highlightbackground=Theme.OP_BORDER, highlightthickness=1,
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8), ipady=8)

        tk.Label(
            left, text="PESO ACTUAL",
            bg=Theme.OP_CARD, fg=Theme.OP_TEXT_DIM,
            font=("Helvetica", 18, "bold"),
        ).pack(pady=(8, 0))

        self.weight_display = tk.Label(
            left, text="—.— g",
            bg=Theme.OP_CARD, fg=Theme.OP_TEXT,
            font=("Helvetica", 64, "bold"),
        )
        self.weight_display.pack()

        self.weight_target_label = tk.Label(
            left, text="Meta: — g",
            bg=Theme.OP_CARD, fg=Theme.OP_TEXT_DIM,
            font=("Helvetica", 18),
        )
        self.weight_target_label.pack(pady=(0, 8))

        # ----- RIGHT: ¿qué falta? -----
        right = tk.Frame(
            row, bg=Theme.OP_CARD,
            highlightbackground=Theme.OP_BORDER, highlightthickness=1,
        )
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0), ipady=8)

        tk.Label(
            right, text="¿QUÉ FALTA?",
            bg=Theme.OP_CARD, fg=Theme.OP_TEXT_DIM,
            font=("Helvetica", 18, "bold"),
        ).pack(pady=(8, 0))

        self.missing_display = tk.Label(
            right, text="—",
            bg=Theme.OP_CARD, fg=Theme.OP_TEXT,
            font=("Helvetica", 54, "bold"),
            justify="center",
        )
        self.missing_display.pack(pady=(2, 0))

        self.missing_sub = tk.Label(
            right, text="",
            bg=Theme.OP_CARD, fg=Theme.OP_TEXT_DIM,
            font=("Helvetica", 18),
        )
        self.missing_sub.pack(pady=(2, 8))

    def _build_counters(self):
        """Three counter cards: Total / Completos / Con falla."""
        row = tk.Frame(self, bg=Theme.OP_BG)
        row.pack(fill="x", padx=18, pady=(14, 0))
        for i in range(3):
            row.columnconfigure(i, weight=1)

        self.counter_cards = {}
        items = [
            ("total",     "Total",         Theme.OP_TEXT),
            ("ok",        "Completo (OK)", Theme.OK_ACCENT),
            ("ng",        "Con Falla (NG)", Theme.NG_ACCENT),
        ]
        for col, (key, label, color) in enumerate(items):
            padx = (0, 4) if col == 0 else (4, 4) if col == 1 else (4, 0)
            card = tk.Frame(
                row, bg=Theme.OP_CARD,
                highlightbackground=Theme.OP_BORDER, highlightthickness=1,
            )
            card.grid(row=0, column=col, sticky="nsew", padx=padx, ipady=8)
            value_label = tk.Label(
                card, text="0",
                bg=Theme.OP_CARD, fg=color,
                font=("Helvetica", 54, "bold"),
            )
            value_label.pack(pady=(6, 0))
            tk.Label(
                card, text=label,
                bg=Theme.OP_CARD, fg=Theme.OP_TEXT_DIM,
                font=("Helvetica", 18),
            ).pack(pady=(0, 6))
            self.counter_cards[key] = (card, value_label)

    def _build_action_row(self):
        row = tk.Frame(self, bg=Theme.OP_BG)
        row.pack(fill="x", padx=18, pady=(14, 0))

        self.inspect_btn = tk.Button(
            row, text="▶  INSPECCIONAR",
            bg=Theme.BTN_BLUE, fg="white",
            activebackground=Theme.BTN_BLUE_HOVER, activeforeground="white",
            font=("Helvetica", 24, "bold"),
            relief="flat", cursor="hand2",
            command=self.on_inspect,
        )
        self.inspect_btn.pack(fill="x", ipady=12)

        self.auto_label = tk.Label(
            row,
            text=("✓ Inspección automática activada — no necesitas presionar"
                  if self.auto_mode_var.get() else
                  "Inspección automática desactivada — usa el botón"),
            bg=Theme.OP_BG, fg=Theme.OP_TEXT_DIM,
            font=("Helvetica", 16),
        )
        self.auto_label.pack(pady=(8, 0))

        # -- Cell automation (Merlic + JAKA) controls --
        auto_row = tk.Frame(row, bg=Theme.OP_BG)
        auto_row.pack(fill="x", pady=(10, 0))

        self.auto_toggle_btn = tk.Button(
            auto_row, text="▶ Iniciar modo automático",
            bg=Theme.OP_CARD, fg=Theme.OP_TEXT,
            activebackground=Theme.OP_BORDER,
            font=("Helvetica", 16, "bold"),
            relief="flat", cursor="hand2",
            command=self.toggle_automation,
        )
        self.auto_toggle_btn.pack(side="left", ipady=4, ipadx=10)

        self.auto_status_label = tk.Label(
            auto_row, textvariable=self.auto_status_var,
            bg=Theme.OP_BG, fg=Theme.OP_TEXT_DIM,
            font=("Helvetica", 16, "bold"),
        )
        self.auto_status_label.pack(side="left", padx=14)

    def _build_statusbar(self):
        bar = tk.Frame(self, bg="#E8EBEE", height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        left = tk.Label(
            bar,
            text=(f"  Log: {LOG_FILE.name}   │   "
                  "F1: Recetas   │   F2: Báscula   │   "
                  "F3: Configuración   │   F4: Modo automático"),
            bg="#E8EBEE", fg=Theme.OP_TEXT_DIM,
            font=("Consolas", 11),
        )
        left.pack(side="left")

        self.port_status_label = tk.Label(
            bar, text="● Desconectada",
            bg="#E8EBEE", fg=Theme.NG_ACCENT,
            font=("Consolas", 11, "bold"),
        )
        self.port_status_label.pack(side="right", padx=10)

    # ============================================================
    # RECIPE / CLOCK
    # ============================================================
    def _update_clock(self):
        self.clock_label.config(
            text=datetime.now().strftime("%Y-%m-%d  │  %H:%M:%S"))
        self.after(1000, self._update_clock)

    def _update_recipe_info(self):
        rid = self.current_recipe.get()
        recipe = self.config_data["recipes"][rid]
        mu = self.classifier.expected_weight(rid)
        desc = recipe.get("description", "—")
        self.recipe_pill.config(text=rid)
        self.recipe_desc_label.config(text=f"Receta activa: {desc}")
        self.weight_target_label.config(text=f"Meta: {mu:.2f} g")
        # Also refresh config window if open
        if self.config_window and self.config_window.winfo_exists():
            self.config_window.refresh_recipe_panel()

    # ============================================================
    # SERIAL SCALE
    # ============================================================
    def connect_scale(self):
        settings = self.config_data["settings"]
        if self.scale_reader and self.scale_reader.is_alive():
            self.scale_reader.stop()
            self.scale_reader.join(timeout=2)
        if not SERIAL_AVAILABLE:
            self._set_port_status("error", "pyserial not installed")
            return
        self.scale_reader = ScaleReader(
            port=settings["serial_port"],
            baudrate=settings["baudrate"],
            out_queue=self.serial_queue,
            status_cb=self._set_port_status,
        )
        self.scale_reader.start()

    def _set_port_status(self, state, detail):
        self.after(0, self._set_port_status_main, state, detail)

    def _set_port_status_main(self, state, detail):
        self.port_status = (state, detail)
        mapping = {
            "connected":    ("● Conectada", Theme.OK_ACCENT),
            "connecting":   ("● Conectando...", Theme.AMB_ACCENT),
            "disconnected": ("● Desconectada", Theme.NG_ACCENT),
            "error":        (f"● Error: {(detail or '')[:30]}", Theme.NG_ACCENT),
        }
        txt, color = mapping.get(state, ("● Desconocido", Theme.OP_TEXT_DIM))
        self.port_status_label.config(text=txt, fg=color)
        if self.config_window and self.config_window.winfo_exists():
            self.config_window.refresh_port_status(txt, color)

    def _poll_serial_queue(self):
        try:
            while True:
                item = self.serial_queue.get_nowait()
                if item[0] == "weight":
                    _, value, raw = item
                    self._handle_live_weight(value)
        except queue.Empty:
            pass
        self.after(50, self._poll_serial_queue)

    def _handle_live_weight(self, value: float):
        self.live_weight = value
        self.weight_display.config(text=f"{value:.2f} g")
        is_stable, stable_val = self.stability.push(value)
        if is_stable:
            self.last_stable_value = stable_val
            self.weight_var.set(f"{stable_val:.4f}")
            # Wake any thread waiting for a stable reading (e.g. orchestrator)
            self._stable_weight_value = stable_val
            self._stable_event.set()

    # ============================================================
    # INSPECT ACTION
    # ============================================================
    def on_inspect(self, from_orchestrator: bool = False):
        # from_orchestrator=True cuando la llamada viene del CellOrchestrator.
        # En ese caso NO actualizamos los contadores aquí — se actualizan
        # después de que el JAKA confirma la señal y relanza el programa.
        # from_orchestrator=False (default) = modo manual, comportamiento normal.  
        if self.last_stable_value is not None:
            weight = self.last_stable_value
        elif self.live_weight is not None:
            weight = self.live_weight
        else:
            messagebox.showwarning(
                "Sin peso",
                "Aún no hay lectura de la báscula. Coloca un kit y espera.")
            return
        
        if weight < EMPTY_PLATFORM_G:
            self._auto_last_status = None  # no es un veredicto real
            if not from_orchestrator:
                self._render_waiting()
            return
        
        rid = self.current_recipe.get()
        result = self.classifier.classify(rid, weight)
        log_inspection(result)
        self._render_result(result)

        # Solo actualizamos contadores aquí si es modo manual.
        # En modo automático los contadores los actualiza counter_cb
        # una vez que el JAKA ya recibió la señal y relanzó el programa.
        if not from_orchestrator:
            self._update_counters(result["status"])

        # Guardamos el resultado para que _auto_classify_callback lo lea
        # desde el thread del orquestador.
        self._auto_last_status = result["status"]

        if self.config_window and self.config_window.winfo_exists():
            self.config_window.append_log_row(result)

        self.after(10000, self._render_waiting)

    def on_clear(self):
        self.weight_var.set("")
        self.last_inspected_value = None
        self.stability.reset()
        self._render_waiting()

    def _render_waiting(self):
        self._set_result_panel(Theme.WAIT_BG, Theme.WAIT_FG,
                                "◯", "ESPERANDO KIT",
                                "Coloca un kit en la báscula para iniciar")
        self.weight_display.config(text="—.— g", fg=Theme.OP_TEXT)
        self.missing_display.config(text="—", fg=Theme.OP_TEXT)
        self.missing_sub.config(text="")

    def _render_result(self, result):
        is_ok = result["status"] == "OK"
        is_ambiguous = (not is_ok and
                        str(result.get("missing_component", "")).startswith("AMBIGUOUS"))

        # ---- Result panel ----
        if is_ok:
            self._set_result_panel(
                Theme.OK_BG, Theme.OK_FG,
                "✓", "KIT COMPLETO",
                "Coloca el siguiente kit en la báscula")
            self.weight_display.config(fg=Theme.OP_TEXT)
            self.missing_display.config(text="✓ Completo", fg=Theme.OK_ACCENT)
            self.missing_sub.config(text="Faltan: Ninguna")
        elif is_ambiguous:
            self._set_result_panel(
                Theme.AMB_BG, Theme.AMB_FG,
                "⚠", "REVISAR KIT",
                "Inspección manual requerida — varias piezas posibles")
            self.weight_display.config(fg=Theme.AMB_ACCENT)
            candidates = result["missing_component"].replace("AMBIGUOUS: ", "")
            comp_a, comp_b = [c.strip() for c in candidates.split("/")[:2]]
            comps = self.config_data["components"]
            name_a = comps.get(comp_a, {}).get("description", comp_a)
            name_b = comps.get(comp_b, {}).get("description", comp_b)
            self.missing_display.config(
                text=f"{comp_a}  ó  {comp_b}", fg=Theme.AMB_ACCENT)
            self.missing_sub.config(text=f"Posible: {name_a} / {name_b}")
        else:
            self._set_result_panel(
                Theme.NG_BG, Theme.NG_FG,
                "✗", "KIT INCOMPLETO",
                "Por favor, agregue los componentes faltantes")
            self.weight_display.config(fg=Theme.NG_ACCENT)

            missing_id = result["missing_component"]
            comp_info = self.config_data["components"].get(missing_id, {})
            comp_desc = comp_info.get("description", missing_id)
            self.missing_display.config(
                text=f"Falta: 1 Pieza ({missing_id})", fg=Theme.NG_ACCENT)
            self.missing_sub.config(text=comp_desc)

    def _set_result_panel(self, bg, fg, icon, title, sub):
        self.result_panel.config(bg=bg)
        self.result_icon.config(bg=bg, fg=fg, text=icon)
        self.result_title.config(bg=bg, fg=fg, text=title)
        self.result_sub.config(bg=bg, fg=fg, text=sub)

    def _update_counters(self, status):
        self.inspection_count += 1
        if status == "OK":
            self.ok_count += 1
        else:
            self.ng_count += 1  # AMBIGUOUS counts as NG
        self.counter_cards["total"][1].config(text=str(self.inspection_count))
        self.counter_cards["ok"][1].config(text=str(self.ok_count))
        self.counter_cards["ng"][1].config(text=str(self.ng_count))

        # Highlight the relevant counter card
        for key, (card, _) in self.counter_cards.items():
            card.config(highlightbackground=Theme.OP_BORDER, highlightthickness=1)
        active_key = "ok" if status == "OK" else "ng"
        active_color = Theme.OK_ACCENT if status == "OK" else Theme.NG_ACCENT
        self.counter_cards[active_key][0].config(
            highlightbackground=active_color, highlightthickness=2)

        if self.config_window and self.config_window.winfo_exists():
            self.config_window.refresh_session_stats()

    # ============================================================
    # CONFIG WINDOW
    # ============================================================
    def open_config_window(self, tab=0):
        if self.config_window and self.config_window.winfo_exists():
            self.config_window.lift()
            self.config_window.focus_force()
            self.config_window.select_tab(tab)
            return
        self.config_window = ConfigWindow(self, initial_tab=tab)

    def reset_session_counters(self):
        self.inspection_count = 0
        self.ok_count = 0
        self.ng_count = 0
        self._inspection_enabled = False
        for key, (card, val) in self.counter_cards.items():
            val.config(text="0")
            card.config(highlightbackground=Theme.OP_BORDER, highlightthickness=1)

    def apply_new_config(self, new_config: dict):
        """Called by the config window when settings change."""
        save_config(new_config)
        self.config_data = new_config
        self.classifier = Classifier(
            components=new_config["components"],
            recipes=new_config["recipes"],
            k_sigma=new_config["settings"]["k_sigma"],
        )
        self.stability = StabilityDetector(
            window_size=new_config["settings"]["stability_window"],
            tolerance=new_config["settings"]["stability_tolerance_g"],
        )
        self.auto_mode_var.set(new_config["settings"]["auto_inspect_on_stable"])
        self.auto_label.config(
            text=("✓ Inspección automática activada — no necesitas presionar"
                  if self.auto_mode_var.get() else
                  "Inspección automática desactivada — usa el botón"))
        # Refresh recipe selector
        rid = self.current_recipe.get()
        valid_recipes = list(new_config["recipes"].keys())
        if rid not in valid_recipes and valid_recipes:
            self.current_recipe.set(valid_recipes[0])
        self._update_recipe_info()
        self.connect_scale()

    # ============================================================
    # CELL AUTOMATION (Merlic + JAKA + Scale)
    # ============================================================
    def toggle_automation(self):
        """Start or stop the Merlic -> JAKA -> Scale cell loop."""
        if self.orchestrator and self.orchestrator.is_alive():
            self.stop_automation()
        else:
            self.start_automation()

    def start_automation(self):
        if self.orchestrator and self.orchestrator.is_alive():
            return
        self._inspection_enabled = True    
        if not REQUESTS_AVAILABLE:
            messagebox.showerror(
                "Librería faltante",
                "La librería 'requests' no está instalada.\n\n"
                "Ejecuta en la terminal:\n"
                "pip install requests"
            )
            return
        s = self.config_data["settings"]
        merlic = MerlicClient(s["merlic_base_url"])
        jaka   = JakaClient(s["jaka_ip"], int(s["jaka_port"]))
        self.orchestrator = CellOrchestrator(
            merlic=merlic,
            jaka=jaka,
            program_name=s["jaka_program_name"],
            stability_timeout_s=float(s["stability_timeout_s"]),
            event_queue=self.orch_event_queue,
            inspect_request_cb=self._auto_classify_callback,
            wait_for_stable_cb=self._auto_wait_for_stable,
            counter_cb=self._auto_update_counters,
            reset_ui_cb=lambda: self.after(0, self._render_waiting),
            get_live_weight_cb=lambda: self.live_weight)

        self._set_auto_status("Iniciando modo automático…",
                              Theme.AMB_ACCENT)
        if hasattr(self, "auto_toggle_btn"):
            self.auto_toggle_btn.config(text="⏹ Detener modo automático")

        self.estop_monitor = EStopMonitor(s["jaka_ip"], self.orch_event_queue)
        self.estop_monitor.start()
        self.orchestrator.start()

    def stop_automation(self):
        if self.orchestrator:
            self.orchestrator.stop()
        if self.estop_monitor:
            self.estop_monitor.stop()
        # Unblock any pending wait in the orchestrator thread
        self._stable_event.set()
        self._set_auto_status("Modo manual", Theme.OP_TEXT_DIM)
        if hasattr(self, "auto_toggle_btn"):
            self.auto_toggle_btn.config(text="▶ Iniciar modo automático")

    def _auto_wait_for_stable(self, timeout_s: float):
        """Called from the orchestrator thread. Blocks until the GUI
        observes a stable weight or until timeout. Returns the weight
        (float) on success, None on timeout."""
        # Reset latch so we ignore the previous stable reading
        self._stable_event.clear()
        self._stable_weight_value = None
        # Also reset the stability detector buffer so we don't pick up
        # readings taken before the new kit landed on the scale
        try:
            self.stability.reset()
        except Exception:
            pass
        triggered = self._stable_event.wait(timeout_s)
        if not triggered:
            return None
        return self._stable_weight_value

    def _auto_classify_callback(self, weight: float) -> str:
        # Este método es llamado desde el thread del orquestador.
        # Usamos after(0,...) para ejecutar on_inspect en el thread
        # de Tkinter (nunca tocar la GUI desde un thread secundario).
        done   = threading.Event()
        holder = {"status": "NG"}

        def _run():
            self._kit_being_processed = True
            self._auto_last_status = None
            self.last_stable_value = weight
            # Le avisamos a on_inspect que viene del orquestador
            # para que NO actualice los contadores todavía.
            self.on_inspect(from_orchestrator=True)
            holder["status"] = self._auto_last_status or "NG"
            self._kit_being_processed = False  
            done.set()

        self.after(0, _run)
        # Esperamos máximo 5s a que Tkinter ejecute _run
        done.wait(timeout=5.0)
        return holder["status"]
    
    def _auto_update_counters(self, status: str):
        # Puente entre el thread del orquestador y Tkinter.
        # El orquestador llama este método desde su thread,
        # pero _update_counters solo puede tocarse desde el thread
        # principal de Tkinter — por eso usamos after(0,...).
        self.after(0, self._update_counters, status)

    def _set_auto_status(self, text: str, color: str):
        self.auto_status_var.set(text)
        if hasattr(self, "auto_status_label"):
            try:
                self.auto_status_label.config(fg=color)
            except Exception:
                pass

    def _poll_orch_queue(self):
        try:
            while True:
                item = self.orch_event_queue.get_nowait()
                if item[0] == "orch_state":
                    _, state, detail = item
                    self._render_orch_state(state, detail)
                elif item[0] == "estop":
                    _, action = item
                    if action == "pressed":
                        self._trigger_estop_fault()
                    elif action == "reset":
                        self._clear_estop_fault()
        except queue.Empty:
            pass
        self.after(100, self._poll_orch_queue)

    def _render_orch_state(self, state: str, detail: str):
        mapping = {
            "running":       ("● Esperando kit",                Theme.OK_ACCENT),
            "scanning":      ("● Escaneando Merlic",            Theme.AMB_ACCENT),
            "idle":          ("● Sin kit detectado",            Theme.OP_TEXT_DIM),
            "cell_detected": (f"● Kit detectado: {detail}",     Theme.OK_ACCENT),
            "jaka_loading":  ("● Cargando programa JAKA",       Theme.AMB_ACCENT),
            "jaka_running":  ("● Robot tomando pieza",          Theme.AMB_ACCENT),
            "weighing":      ("● Esperando peso estable",       Theme.AMB_ACCENT),
            "classifying":   (f"● Clasificando ({detail})",     Theme.AMB_ACCENT),
            "signaling":     (f"● {detail}",                    Theme.OK_ACCENT),
            "timeout":       ("● Timeout de báscula",           Theme.NG_ACCENT),
            "error":         (f"● {detail}",                    Theme.NG_ACCENT),
            "stopped":       ("Modo manual",                    Theme.OP_TEXT_DIM),
        }
        text, color = mapping.get(state, (f"● {state}", Theme.OP_TEXT_DIM))
        self._set_auto_status(text, color)
    
    def _trigger_estop_fault(self):
        # Stop orchestrator only — EStopMonitor must keep running to detect reset
        if self.orchestrator:
            self.orchestrator.stop()
        self._stable_event.set()
        if hasattr(self, "auto_toggle_btn"):
            self.auto_toggle_btn.config(text="▶ Iniciar modo automático")
        self._set_result_panel(
            "#3D0000", "#FF4444",
            "⛔", "PARO DE EMERGENCIA",
            "Desbloquea el hongo y presiona RESET para continuar",
        )
        self._set_auto_status("⛔  PARO DE EMERGENCIA — presiona RESET", "#FF4444")
        if hasattr(self, "inspect_btn"):
            self.inspect_btn.config(state="disabled")

    def _clear_estop_fault(self):
        # Re-enable robot hardware: power on → wait → enable
        s = self.config_data["settings"]
        jaka = JakaClient(s["jaka_ip"], int(s["jaka_port"]))
        try:
            jaka.power_on()
            time.sleep(2.0)
            jaka.enable_robot()
        except Exception:
            pass  # Si el JAKA no responde, igual limpiar la HMI

        self._set_result_panel(
            Theme.OP_BG, Theme.OP_TEXT,
            "—", "Sistema restablecido",
            "Modo automático listo — presiona Iniciar",
        )
        self._set_auto_status("Sistema restablecido — listo", Theme.OK_ACCENT)
        if hasattr(self, "inspect_btn"):
            self.inspect_btn.config(state="normal")

    def _on_close(self):
        if self.orchestrator and self.orchestrator.is_alive():
            self.orchestrator.stop()
            # Unblock any pending wait so the thread can exit promptly
            self._stable_event.set()
        if self.estop_monitor:
            self.estop_monitor.stop()
        if self.scale_reader:
            self.scale_reader.stop()
        if self.config_window and self.config_window.winfo_exists():
            self.config_window.destroy()
        self.destroy()


# ============================================================
# CONFIG WINDOW — technical settings & analysis
# ============================================================
class ConfigWindow(tk.Toplevel):
    """
    Secondary window with everything technical:
    Tab 1: Báscula  — serial port, baudrate, stability params, k_sigma
    Tab 2: Recetas  — component & recipe editor (with raw JSON fallback)
    Tab 3: Análisis — session statistics + log viewer
    """

    def __init__(self, master: "OperatorWindow", initial_tab=0):
        super().__init__(master)
        self.master_app = master
        self.title("TE Connectivity — Configuración & Análisis")
        self.geometry("820x720")
        self.configure(bg=Theme.CFG_BG)
        self.minsize(720, 600)

        self._build_titlebar()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        self._build_scale_tab()
        self._build_recipe_tab()
        self._build_analysis_tab()

        self._build_statusbar()
        self.select_tab(initial_tab)

        # Populate log viewer with anything already logged today
        self._load_existing_log()

    def _build_titlebar(self):
        bar = tk.Frame(self, bg=Theme.CFG_PANEL, height=46)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        tk.Label(
            bar, text="Configuración & Análisis",
            bg=Theme.CFG_PANEL, fg=Theme.CFG_ACCENT,
            font=("Consolas", 14, "bold"),
        ).pack(side="left", padx=18, pady=8)
        tk.Label(
            bar, text="Uso ocasional",
            bg=Theme.CFG_INPUT, fg=Theme.CFG_DIM,
            font=("Consolas", 11), padx=10, pady=2,
        ).pack(side="right", padx=18, pady=12)

    # ------------------------------------------------------------
    # Tab 1: Báscula
    # ------------------------------------------------------------
    def _build_scale_tab(self):
        tab = tk.Frame(self.notebook, bg=Theme.CFG_BG)
        self.notebook.add(tab, text="  Báscula  ")

        self._tab_section_title(tab, "CONFIGURACIÓN DE ESCALA")
        body = tk.Frame(tab, bg=Theme.CFG_BG)
        body.pack(padx=25, pady=10, fill="x")

        settings = self.master_app.config_data["settings"]

        # Serial port
        self.port_var = tk.StringVar(value=settings["serial_port"])
        self.baud_var = tk.IntVar(value=settings["baudrate"])
        self.win_var = tk.IntVar(value=settings["stability_window"])
        self.tol_var = tk.StringVar(value=str(settings["stability_tolerance_g"]))
        self.k_var = tk.StringVar(value=str(settings["k_sigma"]))
        self.auto_var = tk.BooleanVar(value=settings["auto_inspect_on_stable"])

        self._labeled_combobox(
            body, 0, "Puerto serial:", self.port_var,
            self._available_ports())
        self._labeled_combobox(
            body, 1, "Baudrate:", self.baud_var,
            [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200])
        self._labeled_spinbox(
            body, 2, "Ventana de estabilidad (lecturas):", self.win_var, 3, 20)
        self._labeled_entry(
            body, 3, "Tolerancia de estabilidad (g):", self.tol_var)
        self._labeled_entry(
            body, 4, "Tolerancia de clasificación (k × σ):", self.k_var)

        # Auto-inspect checkbox
        chk = tk.Checkbutton(
            body, text="Inspección automática al detectar lectura estable",
            variable=self.auto_var,
            bg=Theme.CFG_BG, fg=Theme.CFG_TEXT,
            selectcolor=Theme.CFG_INPUT,
            activebackground=Theme.CFG_BG, activeforeground=Theme.CFG_TEXT,
            font=("Consolas", 12),
        )
        chk.grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))

        body.columnconfigure(1, weight=1)

        # Help text
        tk.Label(
            tab,
            text=("• El cambio de puerto/baud reconectará la báscula automáticamente.\n"
                  "• Ventana de estabilidad: cuántas lecturas consecutivas deben\n"
                  "  estar dentro de la tolerancia para considerarse estables.\n"
                  "• k × σ controla qué tan estricta es la clasificación OK/NG."),
            bg=Theme.CFG_BG, fg=Theme.CFG_DIM,
            font=("Consolas", 11), justify="left",
        ).pack(padx=25, pady=(15, 5), anchor="w")

        # Action buttons
        btn_row = tk.Frame(tab, bg=Theme.CFG_BG)
        btn_row.pack(pady=15)
        tk.Button(
            btn_row, text="GUARDAR & RECONECTAR",
            bg=Theme.CFG_OK, fg="white",
            font=("Consolas", 11, "bold"),
            relief="flat", padx=20, pady=6,
            command=self._save_scale_settings,
        ).pack(side="left", padx=5)
        tk.Button(
            btn_row, text="DESCARTAR",
            bg=Theme.CFG_NG, fg="white",
            font=("Consolas", 11, "bold"),
            relief="flat", padx=20, pady=6,
            command=self._reset_scale_form,
        ).pack(side="left", padx=5)

    def _available_ports(self):
        if not SERIAL_AVAILABLE:
            return ["COM1", "COM3", "COM6", "/dev/ttyUSB0"]
        ports = [p.device for p in serial.tools.list_ports.comports()]
        return ports or ["COM1", "COM3", "COM6", "/dev/ttyUSB0"]

    def _save_scale_settings(self):
        try:
            new_settings = dict(self.master_app.config_data["settings"])
            new_settings["serial_port"] = self.port_var.get().strip()
            new_settings["baudrate"] = int(self.baud_var.get())
            new_settings["stability_window"] = int(self.win_var.get())
            new_settings["stability_tolerance_g"] = float(self.tol_var.get())
            new_settings["k_sigma"] = float(self.k_var.get())
            new_settings["auto_inspect_on_stable"] = bool(self.auto_var.get())
            new_config = dict(self.master_app.config_data)
            new_config["settings"] = new_settings
            self.master_app.apply_new_config(new_config)
            messagebox.showinfo("Guardado", "Configuración aplicada correctamente.")
        except (ValueError, TypeError) as e:
            messagebox.showerror("Valor inválido", str(e))

    def _reset_scale_form(self):
        settings = self.master_app.config_data["settings"]
        self.port_var.set(settings["serial_port"])
        self.baud_var.set(settings["baudrate"])
        self.win_var.set(settings["stability_window"])
        self.tol_var.set(str(settings["stability_tolerance_g"]))
        self.k_var.set(str(settings["k_sigma"]))
        self.auto_var.set(settings["auto_inspect_on_stable"])

    # ------------------------------------------------------------
    # Tab 2: Recetas
    # ------------------------------------------------------------
    def _build_recipe_tab(self):
        tab = tk.Frame(self.notebook, bg=Theme.CFG_BG)
        self.notebook.add(tab, text="  Recetas  ")

        self._tab_section_title(tab, "RECETAS DEFINIDAS")

        # Summary table
        cols = ("recipe", "components", "mu", "sigma", "lower", "upper")
        self.recipe_tree = ttk.Treeview(
            tab, columns=cols, show="headings", height=6)
        headings = {
            "recipe": "Receta", "components": "Composición",
            "mu": "µ (g)", "sigma": "σ_kit (g)",
            "lower": "Mín (g)", "upper": "Máx (g)",
        }
        widths = {"recipe": 70, "components": 220, "mu": 80,
                  "sigma": 80, "lower": 80, "upper": 80}
        for c in cols:
            self.recipe_tree.heading(c, text=headings[c])
            self.recipe_tree.column(c, width=widths[c], anchor="center")
        self.recipe_tree.pack(fill="x", padx=25, pady=(5, 5))

        self._tab_section_title(tab, "EDITOR DE COMPONENTES Y RECETAS (JSON)")

        editor_frame = tk.Frame(tab, bg=Theme.CFG_BG)
        editor_frame.pack(fill="both", expand=True, padx=25, pady=(5, 5))

        self.recipe_text = tk.Text(
            editor_frame,
            bg=Theme.CFG_INPUT, fg=Theme.CFG_TEXT,
            insertbackground=Theme.CFG_TEXT,
            font=("Consolas", 12), relief="flat",
            wrap="none",
        )
        scrollbar = tk.Scrollbar(editor_frame, command=self.recipe_text.yview)
        self.recipe_text.config(yscrollcommand=scrollbar.set)
        self.recipe_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        btn_row = tk.Frame(tab, bg=Theme.CFG_BG)
        btn_row.pack(pady=(5, 15))
        tk.Button(
            btn_row, text="GUARDAR CAMBIOS",
            bg=Theme.CFG_OK, fg="white",
            font=("Consolas", 11, "bold"),
            relief="flat", padx=20, pady=6,
            command=self._save_recipes,
        ).pack(side="left", padx=5)
        tk.Button(
            btn_row, text="RECARGAR",
            bg=Theme.CFG_ACCENT, fg="white",
            font=("Consolas", 11, "bold"),
            relief="flat", padx=20, pady=6,
            command=self.refresh_recipe_panel,
        ).pack(side="left", padx=5)

        self.refresh_recipe_panel()

    def refresh_recipe_panel(self):
        """Reload tree summary + JSON editor from master config."""
        cfg = self.master_app.config_data
        # Table
        for i in self.recipe_tree.get_children():
            self.recipe_tree.delete(i)
        classifier = self.master_app.classifier
        for rid, recipe in cfg["recipes"].items():
            comps = ", ".join(
                f"{c}×{n}" if n > 1 else c
                for c, n in recipe["composition"].items())
            mu = classifier.expected_weight(rid)
            sigma = classifier.expected_std(rid)
            lower, upper = classifier.bounds(rid)
            self.recipe_tree.insert(
                "", "end",
                values=(rid, comps,
                        f"{mu:.4f}", f"{sigma:.4f}",
                        f"{lower:.4f}", f"{upper:.4f}"))
        # JSON editor
        editable = {
            "components": cfg["components"],
            "recipes": cfg["recipes"],
        }
        self.recipe_text.delete("1.0", "end")
        self.recipe_text.insert("1.0", json.dumps(
            editable, indent=2, ensure_ascii=False))

    def _save_recipes(self):
        try:
            edited = json.loads(self.recipe_text.get("1.0", "end"))
            if "components" not in edited or "recipes" not in edited:
                raise ValueError("El JSON debe incluir 'components' y 'recipes'.")
            new_config = dict(self.master_app.config_data)
            new_config["components"] = edited["components"]
            new_config["recipes"] = edited["recipes"]
            self.master_app.apply_new_config(new_config)
            self.refresh_recipe_panel()
            messagebox.showinfo("Guardado", "Recetas y componentes actualizados.")
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON inválido", str(e))
        except ValueError as e:
            messagebox.showerror("Error", str(e))

    # ------------------------------------------------------------
    # Tab 3: Análisis
    # ------------------------------------------------------------
    def _build_analysis_tab(self):
        tab = tk.Frame(self.notebook, bg=Theme.CFG_BG)
        self.notebook.add(tab, text="  Análisis  ")

        self._tab_section_title(tab, "ESTADÍSTICAS DE SESIÓN")
        stats_row = tk.Frame(tab, bg=Theme.CFG_BG)
        stats_row.pack(fill="x", padx=25, pady=(5, 10))
        for c in range(3):
            stats_row.columnconfigure(c, weight=1)

        self.stat_cards = {}
        for col, (key, label, color) in enumerate([
            ("total", "Total inspeccionados", Theme.CFG_ACCENT),
            ("ok_pct", "% Completos", Theme.CFG_OK),
            ("ng_pct", "% Con falla", Theme.CFG_NG),
        ]):
            cell = tk.Frame(
                stats_row, bg=Theme.CFG_PANEL,
                highlightbackground=color, highlightthickness=2)
            cell.grid(row=0, column=col, sticky="nsew",
                      padx=(0 if col == 0 else 6, 0))
            tk.Label(
                cell, text=label,
                bg=Theme.CFG_PANEL, fg=Theme.CFG_DIM,
                font=("Consolas", 11)).pack(pady=(8, 0))
            val = tk.Label(
                cell, text="—",
                bg=Theme.CFG_PANEL, fg=color,
                font=("Consolas", 22, "bold"))
            val.pack(pady=(0, 8))
            self.stat_cards[key] = val

        # Reset button
        tk.Button(
            tab, text="↺  RESETEAR CONTADORES DE SESIÓN",
            bg=Theme.CFG_INPUT, fg=Theme.CFG_TEXT,
            font=("Consolas", 12),
            relief="flat", padx=14, pady=4,
            command=self._reset_counters,
        ).pack(padx=25, anchor="w")

        self._tab_section_title(tab, "REGISTRO DE INSPECCIONES")

        # Log viewer
        log_frame = tk.Frame(tab, bg=Theme.CFG_BG)
        log_frame.pack(fill="both", expand=True, padx=25, pady=(5, 5))

        cols = ("ts", "recipe", "weight", "status", "missing")
        self.log_tree = ttk.Treeview(
            log_frame, columns=cols, show="headings", height=10)
        headings = {
            "ts": "Hora", "recipe": "Receta",
            "weight": "Peso (g)", "status": "Resultado",
            "missing": "Faltante",
        }
        widths = {"ts": 130, "recipe": 70, "weight": 90,
                  "status": 100, "missing": 200}
        for c in cols:
            self.log_tree.heading(c, text=headings[c])
            self.log_tree.column(c, width=widths[c], anchor="center")
        scroll = tk.Scrollbar(log_frame, command=self.log_tree.yview)
        self.log_tree.config(yscrollcommand=scroll.set)
        self.log_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        btn_row = tk.Frame(tab, bg=Theme.CFG_BG)
        btn_row.pack(pady=(5, 12))
        tk.Button(
            btn_row, text="📁  ABRIR CARPETA DE LOGS",
            bg=Theme.CFG_INPUT, fg=Theme.CFG_TEXT,
            font=("Consolas", 12), relief="flat", padx=14, pady=4,
            command=self._open_log_folder,
        ).pack(side="left", padx=5)
        tk.Button(
            btn_row, text="↻  RECARGAR DEL ARCHIVO",
            bg=Theme.CFG_INPUT, fg=Theme.CFG_TEXT,
            font=("Consolas", 12), relief="flat", padx=14, pady=4,
            command=self._load_existing_log,
        ).pack(side="left", padx=5)

        self.refresh_session_stats()

    def refresh_session_stats(self):
        if not hasattr(self, "stat_cards"):
            return
        total = self.master_app.inspection_count
        ok = self.master_app.ok_count
        ng = self.master_app.ng_count
        self.stat_cards["total"].config(text=str(total))
        if total > 0:
            self.stat_cards["ok_pct"].config(text=f"{100 * ok / total:.1f}%")
            self.stat_cards["ng_pct"].config(text=f"{100 * ng / total:.1f}%")
        else:
            self.stat_cards["ok_pct"].config(text="—")
            self.stat_cards["ng_pct"].config(text="—")

    def append_log_row(self, result: dict):
        ts = result["timestamp"].split("T")[-1]
        status = result["status"]
        if str(result.get("missing_component", "")).startswith("AMBIGUOUS"):
            label = "AMBIGUO"
        else:
            label = "COMPLETO" if status == "OK" else "FALLA"
        missing = result.get("missing_component") or "—"
        self.log_tree.insert(
            "", 0,
            values=(ts, result["recipe"],
                    f"{result['measured_weight']:.4f}",
                    label, missing))

    def _load_existing_log(self):
        for i in self.log_tree.get_children():
            self.log_tree.delete(i)
        if not LOG_FILE.exists():
            return
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            # Most recent first
            for row in reversed(rows[-200:]):
                ts = row["timestamp"].split("T")[-1] if "T" in row["timestamp"] else row["timestamp"]
                status = row["status"]
                missing = row.get("missing_component", "") or "—"
                if missing.startswith("AMBIGUOUS"):
                    label = "AMBIGUO"
                else:
                    label = "COMPLETO" if status == "OK" else "FALLA"
                self.log_tree.insert(
                    "", "end",
                    values=(ts, row["recipe"],
                            f"{float(row['measured_weight']):.4f}",
                            label, missing))
        except Exception as e:
            messagebox.showerror("Error leyendo log", str(e))

    def _open_log_folder(self):
        import sys, subprocess, os
        path = str(LOG_DIR.resolve())
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showinfo("Carpeta de logs", f"Ruta: {path}\n\n{e}")

    def _reset_counters(self):
        if messagebox.askyesno(
                "Resetear",
                "¿Resetear los contadores de la sesión actual?\n"
                "Esto NO borra el archivo de log."):
            self.master_app.reset_session_counters()
            self.refresh_session_stats()

    # ------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------
    def _build_statusbar(self):
        bar = tk.Frame(self, bg=Theme.CFG_PANEL, height=24)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        tk.Label(
            bar,
            text=f"Log activo: {LOG_FILE.name}",
            bg=Theme.CFG_PANEL, fg=Theme.CFG_DIM,
            font=("Consolas", 11),
        ).pack(side="left", padx=14)
        state, detail = self.master_app.port_status
        mapping = {
            "connected":    ("● Báscula conectada", Theme.CFG_OK),
            "connecting":   ("● Conectando...", Theme.CFG_WARN),
            "disconnected": ("● Báscula desconectada", Theme.CFG_NG),
            "error":        (f"● Error: {(detail or '')[:30]}", Theme.CFG_NG),
        }
        txt, color = mapping.get(state, ("● —", Theme.CFG_DIM))
        self.port_status_label = tk.Label(
            bar, text=txt, bg=Theme.CFG_PANEL, fg=color,
            font=("Consolas", 11, "bold"),
        )
        self.port_status_label.pack(side="right", padx=14)

    def refresh_port_status(self, text, color):
        if hasattr(self, "port_status_label"):
            self.port_status_label.config(text=text, fg=color)

    def _tab_section_title(self, parent, text):
        frame = tk.Frame(parent, bg=Theme.CFG_BG)
        frame.pack(fill="x", padx=20, pady=(15, 4))
        tk.Label(
            frame, text=text,
            bg=Theme.CFG_BG, fg=Theme.CFG_ACCENT,
            font=("Consolas", 12, "bold"),
        ).pack(anchor="w")
        sep = tk.Frame(parent, bg=Theme.CFG_BORDER, height=1)
        sep.pack(fill="x", padx=20)

    def _labeled_entry(self, parent, row, label, var):
        tk.Label(
            parent, text=label, bg=Theme.CFG_BG, fg=Theme.CFG_TEXT,
            font=("Consolas", 12),
        ).grid(row=row, column=0, sticky="w", pady=5)
        tk.Entry(
            parent, textvariable=var,
            font=("Consolas", 11), width=24,
            bg=Theme.CFG_INPUT, fg=Theme.CFG_TEXT,
            insertbackground=Theme.CFG_TEXT, relief="flat",
        ).grid(row=row, column=1, sticky="ew", pady=5, padx=(10, 0))

    def _labeled_spinbox(self, parent, row, label, var, mn, mx):
        tk.Label(
            parent, text=label, bg=Theme.CFG_BG, fg=Theme.CFG_TEXT,
            font=("Consolas", 12),
        ).grid(row=row, column=0, sticky="w", pady=5)
        tk.Spinbox(
            parent, textvariable=var, from_=mn, to=mx,
            font=("Consolas", 11), width=22,
            bg=Theme.CFG_INPUT, fg=Theme.CFG_TEXT, relief="flat",
        ).grid(row=row, column=1, sticky="ew", pady=5, padx=(10, 0))

    def _labeled_combobox(self, parent, row, label, var, values):
        tk.Label(
            parent, text=label, bg=Theme.CFG_BG, fg=Theme.CFG_TEXT,
            font=("Consolas", 12),
        ).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Combobox(
            parent, textvariable=var, values=values,
            font=("Consolas", 11), width=22,
        ).grid(row=row, column=1, sticky="ew", pady=5, padx=(10, 0))

    def select_tab(self, index: int):
        try:
            self.notebook.select(index)
        except Exception:
            pass


# ============================================================
# ENTRY
# ============================================================
if __name__ == "__main__":
    app = OperatorWindow()
    app.mainloop()
