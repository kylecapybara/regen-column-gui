from __future__ import annotations

import csv
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from serial.tools import list_ports
from werkzeug.utils import secure_filename

from Valco import Valco
from reglo_ICC import reglo_ICC, to_scientific


BASE_DIR = Path(__file__).resolve().parent
METHODS_DIR = BASE_DIR / "methods"
METHODS_DIR.mkdir(exist_ok=True)
RUN_LOGS_DIR = BASE_DIR / "run_logs"
RUN_LOGS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)


def _first_available_port(start: int = 5001, host: str = "127.0.0.1", attempts: int = 100) -> int:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No available port found from {start} to {start + attempts - 1}.")


def _safe_float(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number.") from exc
    if not (number == number):
        raise ValueError(f"{field_name} must be a valid number.")
    return number


def _port_list() -> list[dict[str, str]]:
    ports = []
    for port in list_ports.comports():
        ports.append(
            {
                "device": port.device,
                "description": port.description,
                "hwid": port.hwid,
            }
        )
    return ports


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _method_file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "filename": path.name,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "modified_label": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def _method_files() -> list[dict[str, Any]]:
    return sorted(
        (_method_file_info(path) for path in METHODS_DIR.glob("*.txt") if path.is_file()),
        key=lambda item: item["modified"],
        reverse=True,
    )


def _run_log_files() -> list[Path]:
    return sorted(
        (path for path in RUN_LOGS_DIR.glob("*.csv") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _write_run_log(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "timestamp",
        "event",
        "step",
        "message",
        "solution",
        "channels",
        "valco_output",
        "duration_s",
        "volume_ml",
        "flow_ml_min",
        "rpm",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _format_solution_name(solution: dict[str, Any]) -> str:
    return (solution.get("name") or "").strip()


def _solution_key(name: Any) -> str:
    return " ".join(str(name or "").strip().lower().split())


def _normalize_solution_category(value: str | None) -> str:
    category = (value or "Other").strip().lower()
    if category == "acid":
        return "Acid"
    if category == "base":
        return "Base"
    return "Other"


def _normalize_step_direction(value: Any, field_name: str) -> str:
    direction = str(value or "CW").strip().upper()
    if direction not in {"CW", "CCW"}:
        raise ValueError(f"{field_name} must be CW or CCW.")
    return direction


def _normalize_step_channels(value: Any, field_name: str) -> list[int]:
    if value in (None, ""):
        return [1, 2, 3, 4, 5, 6, 7, 8]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of channels.")

    channels: list[int] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{field_name} must contain integers from 1 to 8.")
        try:
            channel = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must contain integers from 1 to 8.") from exc
        if channel not in {1, 2, 3, 4, 5, 6, 7, 8}:
            raise ValueError(f"{field_name} must contain integers from 1 to 8.")
        if channel not in channels:
            channels.append(channel)

    if not channels:
        return [1, 2, 3, 4, 5, 6, 7, 8]
    return channels


def _normalize_steps(steps: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    normalized = []
    for index, step in enumerate(steps, start=1):
        step_type = step.get("step_type", "flow")
        normalized_step: dict[str, Any] = {
            "id": step.get("id") or f"step-{index}",
            "step_type": step_type,
        }

        if step_type == "pause":
            normalized_step.update(
                {
                    "duration": _safe_float(step.get("duration", 0), f"Step {index} duration"),
                    "volume": 0.0,
                    "volume_unit": "mL",
                    "solution_position": None,
                    "solution_name": "",
                    "flow_rate": 0.0,
                    "flow_unit": "mL/min",
                    "direction": "CW",
                    "channels": [1, 2, 3, 4, 5, 6, 7, 8],
                    "primary_channel": None,
                    "valco_output_position": None,
                }
            )
            normalized.append(normalized_step)
            continue

        normalized_step.update(
            {
                "volume": _safe_float(step.get("volume", ""), f"Step {index} volume"),
                "volume_unit": step.get("volume_unit", "mL"),
                "flow_rate": _safe_float(step.get("flow_rate", ""), f"Step {index} flow rate"),
                "flow_unit": step.get("flow_unit", "mL/min"),
            }
        )

        if mode == "channel_select":
            primary_channel = _safe_float(step.get("primary_channel"), f"Step {index} primary channel")
            if not (1 <= primary_channel <= 8):
                raise ValueError(f"Step {index} primary channel must be between 1 and 8.")

            normalized_step.update(
                {
                    "primary_channel": int(primary_channel),
                    "solution_position": None,
                    "solution_name": "",
                    "direction": "CW",
                    "channels": [],
                    "valco_output_position": None,
                }
            )

            valco_output_position = step.get("valco_output_position")
            if valco_output_position in (None, ""):
                normalized_step["valco_output_position"] = None
            else:
                valco_output_position = _safe_float(valco_output_position, f"Step {index} Valco output position")
                if not (1 <= valco_output_position <= 6):
                    raise ValueError(f"Step {index} Valco output position must be between 1 and 6.")
                normalized_step["valco_output_position"] = int(valco_output_position)
        else:
            normalized_step.update(
                {
                    "solution_position": step.get("solution_position"),
                    "solution_name": (step.get("solution_name") or "").strip(),
                    "direction": _normalize_step_direction(step.get("direction"), f"Step {index} direction"),
                    "channels": _normalize_step_channels(step.get("channels"), f"Step {index} channels"),
                    "primary_channel": None,
                    "valco_output_position": step.get("valco_output_position"),
                }
            )

        normalized.append(normalized_step)
    return normalized


def _flow_from_rpm(rpm: float, calibration_m: float, calibration_b: float) -> float:
    return calibration_m * rpm + calibration_b


def _step_to_flow_mlpmin(
    step: dict[str, Any],
    bed_volume_ml: float,
    calibration_m: float,
    calibration_b: float,
) -> tuple[float, str]:
    flow_rate = float(step["flow_rate"])
    unit = step["flow_unit"]
    if unit == "mL/min":
        return flow_rate, "mL/min"
    if unit == "BV/hr":
        return flow_rate * bed_volume_ml / 60.0, "mL/min"
    if unit == "RPM":
        return _flow_from_rpm(min(flow_rate, 100.0), calibration_m, calibration_b), "mL/min"
    raise ValueError(f"Unsupported flow unit: {unit}")


def _rpm_for_step_flow(step: dict[str, Any], flow_mlpmin: float, calibration_m: float, calibration_b: float) -> float:
    if step["flow_unit"] == "RPM":
        return min(float(step["flow_rate"]), 100.0)
    return min(_rpm_from_flow(flow_mlpmin, calibration_m, calibration_b), 100.0)


def _step_to_volume_ml(step: dict[str, Any], bed_volume_ml: float) -> float:
    volume = float(step["volume"])
    unit = step["volume_unit"]
    if unit == "mL":
        return volume
    if unit == "BV":
        return volume * bed_volume_ml
    raise ValueError(f"Unsupported volume unit: {unit}")


def _serialize_step(step: dict[str, Any], channel_config: dict[int, dict[str, Any]] | None = None) -> str:
    if step.get("step_type") == "pause":
        return f"Pause {step['duration']:g} s"
    volume = step["volume"]
    volume_unit = step["volume_unit"]
    flow_rate = step["flow_rate"]
    flow_unit = step["flow_unit"]
    direction = _normalize_step_direction(step.get("direction"), "Step direction")
    primary_channel = step.get("primary_channel")
    if primary_channel is not None:
        channels = [int(primary_channel)]
    else:
        channels = _normalize_step_channels(step.get("channels"), "Step channels")
    channels_label = ",".join(str(channel) for channel in channels)
    solution_name = (step.get("solution_name") or "").strip()
    if not solution_name and primary_channel is not None and channel_config is not None:
        solution_name = _format_solution_name(channel_config.get(int(primary_channel), {}))
    solution_label = f" of ch{primary_channel}: {solution_name or f'Channel {primary_channel}'}" if primary_channel is not None else ""
    return f"Flow {volume:g} {volume_unit}{solution_label} at {flow_rate:g} {flow_unit} {direction} ch:{channels_label}"


STEP_PATTERN = re.compile(
    r"^(?:Flow\s+(?P<volume>-?\d+(?:\.\d+)?)\s+(?P<volume_unit>mL|BV)(?:\s+of\s+(?P<solution>.+?)\s+at|\s+at)\s+(?P<flow_rate>-?\d+(?:\.\d+)?)\s+(?P<flow_unit>mL/min|BV/hr|RPM)(?:\s+(?P<direction>CW|CCW))?(?:\s+ch:(?P<channels>[1-8](?:,[1-8])*))?|Pause\s+(?P<duration>-?\d+(?:\.\d+)?)\s+s)$"
)

SOLUTION_IDENTITY_PATTERN = re.compile(r"^ch\s*(?P<channel>[1-8])\s*:\s*(?P<name>.*)$", re.IGNORECASE)


def _parse_method_text(content: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = STEP_PATTERN.match(line)
        if not match:
            raise ValueError(f"Could not parse method line: {line}")

        if match.group("duration") is not None:
            steps.append({
                "id": f"step-{len(steps) + 1}",
                "step_type": "pause",
                "duration": float(match.group("duration")),
                "volume": 0.0,
                "volume_unit": "mL",
                "solution_name": "",
                "solution_position": None,
                "flow_rate": 0.0,
                "flow_unit": "mL/min",
                "direction": "CW",
                "channels": [1, 2, 3, 4, 5, 6, 7, 8],
            })
            continue

        solution = (match.group("solution") or "").strip()
        primary_channel = None
        identity_match = SOLUTION_IDENTITY_PATTERN.match(solution)
        if identity_match:
            primary_channel = int(identity_match.group("channel"))
            solution = identity_match.group("name").strip()
        direction = _normalize_step_direction(match.group("direction"), f"Step {len(steps) + 1} direction")
        channels_match = match.group("channels")
        channels = [int(value) for value in channels_match.split(",")] if channels_match else [1, 2, 3, 4, 5, 6, 7, 8]
        channels = _normalize_step_channels(channels, f"Step {len(steps) + 1} channels")
        if primary_channel is None and len(channels) == 1:
            primary_channel = channels[0]
        steps.append(
            {
                "id": f"step-{len(steps) + 1}",
                "volume": float(match.group("volume")),
                "volume_unit": match.group("volume_unit"),
                "solution_name": solution,
                "solution_position": None,
                "flow_rate": float(match.group("flow_rate")),
                "flow_unit": match.group("flow_unit"),
                "direction": direction,
                "channels": channels,
                "primary_channel": primary_channel,
            }
        )
    return steps


def _resolve_loaded_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with state.lock:
        channel_config = dict(state.channel_config)
    channel_by_solution = {
        _solution_key(value.get("name")): channel
        for channel, value in channel_config.items()
        if _solution_key(value.get("name"))
    }
    for step in steps:
        if step.get("step_type") == "pause":
            continue
        solution_name = step.get("solution_name")
        saved_channel = step.get("primary_channel")
        matched_channel = None
        if saved_channel is not None:
            saved_channel = int(saved_channel)
            saved_channel_name = channel_config.get(saved_channel, {}).get("name")
            if _solution_key(saved_channel_name) == _solution_key(solution_name):
                matched_channel = saved_channel
        if matched_channel is None:
            matched_channel = channel_by_solution.get(_solution_key(solution_name))
        if matched_channel is not None:
            step["primary_channel"] = matched_channel
            step["channels"] = [matched_channel]
        elif saved_channel is not None:
            step["primary_channel"] = saved_channel
            step["channels"] = [saved_channel]
    return steps


def _load_method_content(content: str) -> tuple[list[dict[str, Any]], str | None, str]:
    steps = _resolve_loaded_steps(_parse_method_text(content))
    mode = _current_mode()
    has_solution_refs = any(step.get("solution_name") for step in steps)
    warning = None
    if mode == "pump_only" and has_solution_refs:
        warning = "Loaded method contains solution references, but the app is in Pump-Only Mode. Those references will be ignored until a Valco is connected."
    return steps, warning, mode


@dataclass
class AppState:
    pump_a_port: str | None = None
    pump_b_port: str | None = None
    valco_port: str | None = None
    pump_a: reglo_ICC | None = None
    pump_b: reglo_ICC | None = None
    valco: Valco | None = None
    pump_a_connected: bool = False
    pump_b_connected: bool = False
    valco_connected: bool = False
    valco_position: int | None = None
    valco_changing: bool = False
    pump_a_active: bool = False
    pump_b_active: bool = False
    mode: str = "pump_only"
    preferred_valco_mode: str = "channel_select"
    running: bool = False
    is_paused: bool = False
    current_step_index: int | None = None
    current_step_label: str = ""
    time_remaining: float | None = None
    current_step_duration: float | None = None
    last_error: str = ""
    last_message: str = ""
    latest_run_log: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    run_thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    bed_volume_ml: float = 100.0
    calibration_m: float = 0.1694
    calibration_b: float = -0.727
    solution_config: dict[int, dict[str, Any]] = field(
        default_factory=lambda: {
            position: {"name": "", "category": "Other"} for position in range(1, 7)
        }
    )
    channel_config: dict[int, dict[str, Any]] = field(
        default_factory=lambda: {
            channel: {"name": "", "category": "Other"} for channel in range(1, 9)
        }
    )
    valco_output_config: dict[int, dict[str, Any]] = field(
        default_factory=lambda: {
            1: {"label": "Waste"},
            **{position: {"label": ""} for position in range(2, 7)},
        }
    )

    def close_devices(self) -> None:
        for device in (self.pump_a, self.pump_b, self.valco):
            if device is None:
                continue
            try:
                device.port.close()
            except Exception:
                pass
        self.pump_a = None
        self.pump_b = None
        self.valco = None
        self.pump_a_connected = False
        self.pump_b_connected = False
        self.valco_connected = False
        self.valco_position = None
        self.valco_changing = False
        self.pump_a_active = False
        self.pump_b_active = False
        self.mode = "pump_only"
        self.pump_a_port = None
        self.pump_b_port = None
        self.valco_port = None


state = AppState()


def _mark_pump_failed(name: str, error: Exception, results: dict[str, Any]) -> None:
    pump_attr = "pump_a" if name == "pump_a" else "pump_b"
    connected_attr = "pump_a_connected" if name == "pump_a" else "pump_b_connected"
    port_attr = "pump_a_port" if name == "pump_a" else "pump_b_port"
    device = getattr(state, pump_attr)
    if device is not None:
        try:
            device.port.close()
        except Exception:
            pass
    setattr(state, pump_attr, None)
    setattr(state, connected_attr, False)
    setattr(state, port_attr, None)
    results[name] = {"ok": False, "error": str(error)}


def _perform_heartbeat() -> dict[str, Any]:
    results: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "pump_a": {"ok": state.pump_a_connected},
        "pump_b": {"ok": state.pump_b_connected},
        "valco": {"ok": state.valco_connected},
    }
    with state.lock:
        if state.running:
            results["skipped"] = True
            results["reason"] = "Run in progress"
            return results

        had_pump = state.pump_a_connected or state.pump_b_connected
        had_valco = state.valco_connected
        if state.pump_a is not None:
            try:
                results["pump_a"] = {"ok": True, "handshake_response": state.pump_a.handshake()}
            except Exception as exc:
                _mark_pump_failed("pump_a", exc, results)
        if state.pump_b is not None:
            try:
                results["pump_b"] = {"ok": True, "handshake_response": state.pump_b.handshake()}
            except Exception as exc:
                _mark_pump_failed("pump_b", exc, results)
        if state.valco is not None:
            try:
                response = state.valco.raw_command("CP")
                state.valco_position = state.valco._parse_position(response)
                results["valco"] = {"ok": True, "position": state.valco_position}
            except Exception as exc:
                try:
                    state.valco.port.close()
                except Exception:
                    pass
                state.valco = None
                state.valco_connected = False
                state.valco_port = None
                state.valco_position = None
                results["valco"] = {"ok": False, "error": str(exc)}

        state.mode = _current_mode()
        if had_pump and not state.pump_a_connected and not state.pump_b_connected:
            results["ok"] = False
            state.last_error = "Hardware heartbeat lost all pump connections."
        elif had_valco and not state.valco_connected:
            results["ok"] = False
        return results


def _current_mode() -> str:
    if (state.pump_a_connected or state.pump_b_connected) and state.valco_connected:
        return state.preferred_valco_mode
    return "pump_only"


def _build_solution_lookup(solution_config: dict[int, dict[str, Any]]) -> dict[str, int]:
    lookup: dict[str, int] = {}
    for position, solution in solution_config.items():
        name = _format_solution_name(solution)
        if name:
            lookup[name.lower()] = position
    return lookup


def _validate_run_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]], float, float, float, str]:
    if not state.pump_a_connected and not state.pump_b_connected:
        raise ValueError("No Reglo ICC pump is connected.")

    mode = (payload.get("mode") or _current_mode()).strip().lower()
    if mode not in {"channel_select", "pump_only"}:
        raise ValueError("Invalid mode.")
    if state.valco_connected and mode != "channel_select":
        raise ValueError("Valco is connected; use channel_select mode.")
    if not state.valco_connected and mode != "pump_only":
        raise ValueError("Valco is not connected; use pump_only mode.")

    steps = _normalize_steps(payload.get("steps", []), mode)
    if not steps:
        raise ValueError("Add at least one step before running the method.")

    def _ensure_channel_available(channel: int, context: str) -> None:
        if 1 <= channel <= 4 and not state.pump_a_connected:
            raise ValueError(f"{context} uses channel {channel}, but Pump A is not connected.")
        if 5 <= channel <= 8 and not state.pump_b_connected:
            raise ValueError(f"{context} uses channel {channel}, but Pump B is not connected.")

    for index, step in enumerate(steps, start=1):
        if step.get("step_type") == "pause":
            continue
        for channel in step.get("channels", []):
            _ensure_channel_available(channel, f"Step {index}")
        if mode == "channel_select":
            _ensure_channel_available(step["primary_channel"], f"Step {index} primary channel")

    bed_volume_ml = _safe_float(payload.get("bed_volume_ml", state.bed_volume_ml), "Bed volume")
    if bed_volume_ml <= 0:
        raise ValueError("Bed volume must be greater than zero.")

    calibration = payload.get("calibration", {}) or {}
    calibration_m = _safe_float(calibration.get("m", state.calibration_m), "Calibration slope")
    calibration_b = _safe_float(calibration.get("b", state.calibration_b), "Calibration intercept")
    if calibration_m == 0:
        raise ValueError("Calibration slope must not be zero.")

    solution_config_payload = payload.get("solutions", {}) or {}
    solution_config: dict[int, dict[str, Any]] = {}
    for position in range(1, 7):
        entry = solution_config_payload.get(str(position), solution_config_payload.get(position, {})) or {}
        solution_config[position] = {
            "name": (entry.get("name") or "").strip(),
            "category": _normalize_solution_category(entry.get("category")),
        }

    channel_config_payload = payload.get("channel_config", {}) or {}
    channel_config: dict[int, dict[str, Any]] = {}
    for channel in range(1, 9):
        entry = channel_config_payload.get(str(channel), channel_config_payload.get(channel, {})) or {}
        channel_config[channel] = {
            "name": (entry.get("name") or "").strip(),
            "category": _normalize_solution_category(entry.get("category")),
        }

    valco_output_config_payload = payload.get("valco_output_config", {}) or {}
    valco_output_config: dict[int, dict[str, Any]] = {}
    for position in range(1, 7):
        entry = valco_output_config_payload.get(str(position), valco_output_config_payload.get(position, {})) or {}
        valco_output_config[position] = {
            "label": (entry.get("label") or "").strip(),
        }
    if not any(entry["label"] for entry in valco_output_config.values()):
        valco_output_config[1]["label"] = "Waste"

    return steps, solution_config, channel_config, valco_output_config, bed_volume_ml, calibration_m, calibration_b, mode


def _rpm_from_flow(flow_rate_ml_per_min: float, calibration_m: float, calibration_b: float) -> float:
    return (flow_rate_ml_per_min - calibration_b) / calibration_m


def _stop_pump() -> None:
    for pump in (state.pump_a, state.pump_b):
        if pump is None:
            continue
        try:
            pump.command_all("I")
        except Exception:
            pass
    with state.lock:
        state.pump_a_active = False
        state.pump_b_active = False


def _run_method(payload: dict[str, Any]) -> None:
    run_log_path = RUN_LOGS_DIR / f"run-{_timestamp_slug()}.csv"
    run_log_rows: list[dict[str, Any]] = []

    def log_event(event: str, message: str = "", step: dict[str, Any] | None = None, **extra: Any) -> None:
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "step": step.get("index") if step else "",
            "message": message,
            "solution": step.get("solution_name", "") if step else "",
            "channels": ",".join(str(channel) for channel in step.get("channels", [])) if step else "",
            "valco_output": step.get("output_position", "") if step else "",
            "duration_s": extra.get("duration_s", ""),
            "volume_ml": step.get("volume_ml", "") if step else "",
            "flow_ml_min": step.get("flow_mlpmin", "") if step else "",
            "rpm": ",".join(f"{channel}:{rpm:.3g}" for channel, rpm in step.get("channel_rpms", {}).items()) if step else "",
        }
        run_log_rows.append(row)

    try:
        steps, solution_config, channel_config, valco_output_config, bed_volume_ml, calibration_m, calibration_b, mode = _validate_run_payload(payload)
        channel_select_mode = mode == "channel_select"
        pump_only_mode = mode == "pump_only"
        log_event("run_start", f"Mode: {mode}")

        def _pump_for_channel(channel: int) -> reglo_ICC:
            if 1 <= channel <= 4:
                if state.pump_a is None:
                    raise ValueError("Pump A is not connected for channels 1-4.")
                return state.pump_a
            if 5 <= channel <= 8:
                if state.pump_b is None:
                    raise ValueError("Pump B is not connected for channels 5-8.")
                return state.pump_b
            raise ValueError(f"Channel {channel} is outside the supported range of 1-8.")

        def _command_pumps(command: str, channels: list[int]) -> None:
            pump_a_channels = [channel for channel in channels if 1 <= channel <= 4]
            pump_b_channels = [channel for channel in channels if 5 <= channel <= 8]
            if pump_a_channels and state.pump_a is not None:
                state.pump_a.command_all(command, which=pump_a_channels)
            if pump_b_channels and state.pump_b is not None:
                state.pump_b.command_all(command, which=pump_b_channels)

        def resolve_output_position(step: dict[str, Any], index: int) -> int:
            position = step.get("valco_output_position")
            if position in (None, "", 0):
                labeled_positions = [
                    candidate
                    for candidate, entry in valco_output_config.items()
                    if (entry.get("label") or "").strip()
                ]
                return labeled_positions[0] if labeled_positions else 1
            position = _safe_float(position, f"Step {index} Valco output position")
            if not (1 <= position <= 6):
                raise ValueError(f"Step {index} Valco output position must be between 1 and 6.")
            return int(position)

        def output_label(position: int) -> str:
            label = (valco_output_config.get(position, {}).get("label") or "").strip()
            return label or f"Position {position}"

        def validate_rpm(index: int, rpm: float) -> None:
            if rpm < 0:
                raise ValueError(f"Step {index} requires {rpm:.2f} RPM, which is below zero. Check calibration and flow rate.")

        previous_step_category = None
        expanded_steps: list[dict[str, Any]] = []
        for index, step in enumerate(steps, start=1):
            if step.get("step_type") == "pause":
                expanded_steps.append({"index": index, "step_type": "pause", "duration": step.get("duration", 0)})
                previous_step_category = None
                continue

            flow_mlpmin = _step_to_flow_mlpmin(step, bed_volume_ml, calibration_m, calibration_b)[0]
            volume_ml = _step_to_volume_ml(step, bed_volume_ml)
            if volume_ml <= 0:
                raise ValueError(f"Step {index} volume must be greater than zero.")
            if flow_mlpmin <= 0:
                raise ValueError(f"Step {index} flow rate must be greater than zero.")

            channel_rpms: dict[int, float] = {}
            channels: list[int] = []
            solution_position = step.get("solution_position")
            solution_name = (step.get("solution_name") or "").strip()
            category = "Other"
            output_position = 1 if pump_only_mode else resolve_output_position(step, index)

            if channel_select_mode:
                primary_channel = step["primary_channel"]
                rpm = _rpm_for_step_flow(step, flow_mlpmin, calibration_m, calibration_b)
                flow_mlpmin = _flow_from_rpm(rpm, calibration_m, calibration_b)
                channel_rpms[primary_channel] = rpm
                channels.append(primary_channel)

                categories = [
                    _normalize_solution_category(channel_config.get(channel, {}).get("category"))
                    for channel in channels
                ]
                current_step_category = categories[0] if categories else "Other"
                if previous_step_category in {"Acid", "Base"} and current_step_category in {"Acid", "Base"}:
                    if previous_step_category == "Acid" and current_step_category == "Base":
                        raise ValueError(f"Step {index} is Base after an Acid step. Acid/base adjacency is not allowed.")
                    if previous_step_category == "Base" and current_step_category == "Acid":
                        raise ValueError(f"Step {index} is Acid after a Base step. Acid/base adjacency is not allowed.")

                primary_name = _format_solution_name(channel_config.get(primary_channel, {}))
                solution_name = primary_name or f"Channel {primary_channel}"
                category = current_step_category
            else:
                channels = step["channels"]
                rpm = _rpm_for_step_flow(step, flow_mlpmin, calibration_m, calibration_b)
                flow_mlpmin = _flow_from_rpm(rpm, calibration_m, calibration_b)
                channel_rpms = {channel: rpm for channel in channels}

            previous_step_category = category if category in {"Acid", "Base"} else None

            for channel, rpm in channel_rpms.items():
                validate_rpm(index, rpm)

            expanded_steps.append(
                {
                    "index": index,
                    "volume_ml": volume_ml,
                    "flow_mlpmin": flow_mlpmin,
                    "channel_rpms": channel_rpms,
                    "direction": step["direction"],
                    "channels": channels,
                    "solution_position": solution_position,
                    "solution_name": solution_name,
                    "category": category,
                    "output_position": output_position,
                }
            )

        with state.lock:
            state.running = True
            state.current_step_index = 0
            state.current_step_label = "Preparing run"
            state.time_remaining = None
            state.last_error = ""
            state.last_message = "Run started."
            state.latest_run_log = str(run_log_path)
            state.stop_event.clear()

        for step in expanded_steps:
            if state.stop_event.is_set():
                break

            if step.get("step_type") == "pause":
                duration = step.get("duration", 0)
                log_event("pause_start", "Pause step started.", step, duration_s=duration)
                with state.lock:
                    state.current_step_index = step["index"]
                    state.current_step_label = f"Step {step['index']}: Paused - Waiting for Resume"
                    state.time_remaining = duration
                    state.current_step_duration = duration
                    state.is_paused = True

                _stop_pump()
                state.pause_event.wait(timeout=duration)
                state.pause_event.clear()
                log_event("pause_complete", "Pause step completed.", step, duration_s=duration)

                with state.lock:
                    state.is_paused = False
                    state.current_step_duration = None
                continue

            duration_seconds = (step["volume_ml"] / step["flow_mlpmin"]) * 60.0
            log_event("step_start", "Flow step started.", step, duration_s=duration_seconds)
            with state.lock:
                state.current_step_index = step["index"]
                if channel_select_mode:
                    state.current_step_label = (
                        f"Step {step['index']}: Flow {step['volume_ml']:.3g} mL of {step['solution_name']} "
                        f"at {step['flow_mlpmin']:.3g} mL/min to {output_label(step['output_position'])}"
                    )
                else:
                    state.current_step_label = (
                        f"Step {step['index']}: Flow {step['volume_ml']:.3g} mL at {step['flow_mlpmin']:.3g} mL/min"
                    )
                state.time_remaining = duration_seconds

            _stop_pump()
            if state.stop_event.is_set():
                break

            if state.valco is not None and step.get("output_position") is not None:
                output_position = int(step["output_position"])
                log_event("valve_move", f"Moving valve to {output_label(output_position)}.", step)
                with state.lock:
                    state.valco_changing = state.valco_position != output_position
                state.valco.set_position(output_position)
                with state.lock:
                    state.valco_position = output_position
                    state.valco_changing = False

            if state.pump_a is None and state.pump_b is None:
                raise ValueError("No pump connected during run.")

            direction_command = "J" if step["direction"] == "CW" else "K"
            _command_pumps("L", step["channels"])
            for channel, rpm in step["channel_rpms"].items():
                rpm_hundredths = int(round(rpm * 100))
                _pump_for_channel(channel).command_all("S" + f"{rpm_hundredths:06d}", which=[channel])
            _command_pumps(direction_command, step["channels"])
            _command_pumps("H", step["channels"])
            with state.lock:
                state.pump_a_active = any(1 <= channel <= 4 for channel in step["channels"])
                state.pump_b_active = any(5 <= channel <= 8 for channel in step["channels"])

            deadline = time.monotonic() + duration_seconds
            while True:
                remaining = deadline - time.monotonic()
                with state.lock:
                    state.time_remaining = max(0.0, remaining)
                if remaining <= 0:
                    break
                if state.stop_event.wait(timeout=min(0.25, remaining)):
                    break

            _stop_pump()
            if state.stop_event.is_set():
                log_event("step_stopped", "Flow step stopped by user.", step, duration_s=duration_seconds)
                break
            log_event("step_complete", "Flow step completed.", step, duration_s=duration_seconds)

        with state.lock:
            state.running = False
            state.current_step_index = None
            state.current_step_label = ""
            state.time_remaining = None
            state.pump_a_active = False
            state.pump_b_active = False
            state.valco_changing = False
            if state.stop_event.is_set():
                log_event("run_stopped", "Run stopped by user.")
                state.last_message = "Run stopped by user."
            else:
                log_event("run_complete", "Run completed.")
                state.last_message = "Run completed."
            state.stop_event.clear()
        _write_run_log(run_log_path, run_log_rows)
    except Exception as exc:
        log_event("run_error", str(exc))
        _stop_pump()
        with state.lock:
            state.running = False
            state.current_step_index = None
            state.current_step_label = ""
            state.time_remaining = None
            state.pump_a_active = False
            state.pump_b_active = False
            state.valco_changing = False
            state.last_error = str(exc)
            state.last_message = ""
            state.latest_run_log = str(run_log_path)
            state.stop_event.clear()
        _write_run_log(run_log_path, run_log_rows)


@app.route("/")
def index() -> str:
    return render_template("index.html", v=int(time.time()))


@app.get("/ports")
def ports() -> Any:
    return jsonify({"ports": _port_list()})


@app.post("/connect")
def connect() -> Any:
    payload = request.get_json(force=True, silent=True) or {}
    pump_a_port = (payload.get("pump_a_port") or "").strip()
    pump_b_port = (payload.get("pump_b_port") or "").strip()
    valco_port = (payload.get("valco_port") or "").strip()

    if not pump_a_port and not pump_b_port:
        return jsonify({"ok": False, "error": "Select at least one Reglo ICC pump COM port before connecting."}), 400

    with state.lock:
        state.close_devices()

    warning = ""
    pump_a = None
    pump_b = None
    try:
        if pump_a_port:
            pump_a = reglo_ICC(pump_a_port, reply=False)
            pump_a.reply = False
        if pump_b_port:
            pump_b = reglo_ICC(pump_b_port, reply=False)
            pump_b.reply = False
    except Exception as exc:
        for device in (pump_a, pump_b):
            if device is not None and getattr(device, "port", None):
                device.port.close()
        with state.lock:
            state.close_devices()
            state.last_error = f"Pump connection failed: {exc}"
        return jsonify({"ok": False, "error": f"Pump connection failed: {exc}"}), 400

    valco = None
    valco_connected = False
    if valco_port and valco_port.lower() not in {"none", "not connected"}:
        try:
            valco = Valco(valco_port)
            valco_connected = True
        except Exception as exc:
            warning = f"Valco connection failed, continuing in Pump-Only Mode: {exc}"
            valco = None
            valco_connected = False

    with state.lock:
        state.pump_a = pump_a
        state.pump_b = pump_b
        state.valco = valco
        state.pump_a_port = pump_a_port if pump_a is not None else None
        state.pump_b_port = pump_b_port if pump_b is not None else None
        state.valco_port = valco_port if valco_connected else None
        state.pump_a_connected = pump_a is not None
        state.pump_b_connected = pump_b is not None
        state.valco_connected = valco_connected
        state.valco_position = valco.current_position if valco_connected and valco is not None else None
        state.valco_changing = False
        state.pump_a_active = False
        state.pump_b_active = False
        state.mode = _current_mode()
        state.last_error = ""
        state.last_message = "Connected."

    response = {
        "ok": True,
        "pump_a_connected": state.pump_a_connected,
        "pump_b_connected": state.pump_b_connected,
        "valco_connected": valco_connected,
        "mode": state.mode,
    }
    if warning:
        response["warning"] = warning
    return jsonify(response)


@app.get("/status")
def status() -> Any:
    with state.lock:
        remaining = state.time_remaining
        return jsonify(
            {
                "pump_a_connected": state.pump_a_connected,
                "pump_b_connected": state.pump_b_connected,
                "pump_a_port": state.pump_a_port,
                "pump_b_port": state.pump_b_port,
                "valco_connected": state.valco_connected,
                "valco_port": state.valco_port,
                "valco_position": state.valco_position,
                "valco_changing": state.valco_changing,
                "pump_a_active": state.pump_a_active,
                "pump_b_active": state.pump_b_active,
                "mode": state.mode,
                "running": state.running,
                "current_step_index": state.current_step_index,
                "current_step_label": state.current_step_label,
                "time_remaining": remaining,
                "last_error": state.last_error,
                "last_message": state.last_message,
                "bed_volume_ml": state.bed_volume_ml,
                "calibration": {"m": state.calibration_m, "b": state.calibration_b},
                "solutions": {
                    str(position): value for position, value in state.solution_config.items()
                },
                "channels": {
                    str(channel): value for channel, value in state.channel_config.items()
                },
                "valco_outputs": {
                    str(position): value for position, value in state.valco_output_config.items()
                },
                "is_paused": state.is_paused,
                "current_step_duration": state.current_step_duration,
            }
        )


@app.post("/heartbeat")
def heartbeat() -> Any:
    return jsonify(_perform_heartbeat())


@app.post("/pause_ack")
def pause_ack() -> Any:
    with state.lock:
        state.pause_event.set()
    return jsonify({"ok": True, "message": "Resuming run."})


@app.post("/set_mode")
def set_mode() -> Any:
    payload = request.get_json(force=True, silent=True) or {}
    mode = (payload.get("mode") or "").strip().lower()
    if mode not in {"channel_select", "pump_only"}:
        return jsonify({"ok": False, "error": "Invalid mode."}), 400
    if state.valco_connected and mode != "channel_select":
        return jsonify({"ok": False, "error": "Valco is connected; use channel_select mode."}), 400
    if not state.valco_connected and mode != "pump_only":
        return jsonify({"ok": False, "error": "Valco is not connected; use pump_only mode."}), 400
    if mode != "pump_only" and not (state.pump_a_connected or state.pump_b_connected):
        return jsonify({"ok": False, "error": "Connect at least one Reglo ICC pump before using Valco modes."}), 400
    with state.lock:
        state.mode = mode
        state.preferred_valco_mode = mode
    return jsonify({"ok": True, "mode": state.mode})


@app.post("/run")
def run_method() -> Any:
    payload = request.get_json(force=True, silent=True) or {}
    with state.lock:
        if state.running:
            return jsonify({"ok": False, "error": "A run is already in progress."}), 400
        if "bed_volume_ml" in payload:
            try:
                state.bed_volume_ml = _safe_float(payload.get("bed_volume_ml"), "Bed volume")
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        if "calibration" in payload:
            calibration = payload.get("calibration") or {}
            try:
                state.calibration_m = _safe_float(calibration.get("m", state.calibration_m), "Calibration slope")
                state.calibration_b = _safe_float(calibration.get("b", state.calibration_b), "Calibration intercept")
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        if "solutions" in payload:
            solution_config: dict[int, dict[str, Any]] = {}
            for position in range(1, 7):
                entry = (payload.get("solutions") or {}).get(str(position), {}) or {}
                solution_config[position] = {
                    "name": (entry.get("name") or "").strip(),
                    "category": _normalize_solution_category(entry.get("category")),
                }
            state.solution_config = solution_config
        if "channel_config" in payload:
            channel_config: dict[int, dict[str, Any]] = {}
            for channel in range(1, 9):
                entry = (payload.get("channel_config") or {}).get(str(channel), {}) or {}
                channel_config[channel] = {
                    "name": (entry.get("name") or "").strip(),
                    "category": _normalize_solution_category(entry.get("category")),
                }
            state.channel_config = channel_config
        if "valco_output_config" in payload:
            valco_output_config: dict[int, dict[str, Any]] = {}
            for position in range(1, 7):
                entry = (payload.get("valco_output_config") or {}).get(str(position), {}) or {}
                valco_output_config[position] = {
                    "label": (entry.get("label") or "").strip(),
                }
            if not any(entry["label"] for entry in valco_output_config.values()):
                valco_output_config[1]["label"] = "Waste"
            state.valco_output_config = valco_output_config

    thread = threading.Thread(target=_run_method, args=(payload,), daemon=True)
    with state.lock:
        state.run_thread = thread
    thread.start()
    return jsonify({"ok": True, "message": "Run started."})


@app.post("/stop")
def stop_method() -> Any:
    with state.lock:
        state.stop_event.set()
    _stop_pump()
    return jsonify({"ok": True, "message": "Stop requested."})


@app.post("/save_method")
def save_method() -> Any:
    payload = request.get_json(force=True, silent=True) or {}
    filename = secure_filename((payload.get("filename") or "").strip())
    if not filename:
        return jsonify({"ok": False, "error": "Provide a filename."}), 400
    if not filename.lower().endswith(".txt"):
        filename += ".txt"

    mode = (payload.get("mode") or _current_mode()).strip().lower()
    if mode not in {"channel_select", "pump_only"}:
        return jsonify({"ok": False, "error": "Invalid mode."}), 400
    if state.valco_connected and mode != "channel_select":
        return jsonify({"ok": False, "error": "Valco is connected; use channel_select mode."}), 400
    if not state.valco_connected and mode != "pump_only":
        return jsonify({"ok": False, "error": "Valco is not connected; use pump_only mode."}), 400
    if mode != "pump_only" and not (state.pump_a_connected or state.pump_b_connected):
        return jsonify({"ok": False, "error": "Connect at least one Reglo ICC pump before using Valco modes."}), 400

    try:
        steps = _normalize_steps(payload.get("steps", []), mode)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    channel_config_payload = payload.get("channel_config", {}) or {}
    channel_config: dict[int, dict[str, Any]] = {}
    for channel in range(1, 9):
        entry = channel_config_payload.get(str(channel), channel_config_payload.get(channel, {})) or {}
        channel_config[channel] = {
            "name": _format_solution_name(entry),
            "category": _normalize_solution_category(entry.get("category")),
        }

    lines = [_serialize_step(step, channel_config) for step in steps]
    target = METHODS_DIR / filename
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return jsonify({"ok": True, "path": str(target), "filename": filename})


@app.get("/methods")
def list_methods() -> Any:
    return jsonify({"ok": True, "methods": _method_files()})


@app.get("/methods/<path:filename>")
def load_saved_method(filename: str) -> Any:
    safe_filename = secure_filename(filename)
    if not safe_filename:
        return jsonify({"ok": False, "error": "Invalid filename."}), 400
    target = METHODS_DIR / safe_filename
    if not target.exists() or not target.is_file():
        return jsonify({"ok": False, "error": "Method not found."}), 404
    try:
        steps, warning, mode = _load_method_content(target.read_text(encoding="utf-8", errors="replace"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    with state.lock:
        state.last_message = f"Loaded {safe_filename}."
        state.last_error = ""
    return jsonify({"ok": True, "steps": steps, "warning": warning, "mode": mode, "filename": safe_filename})


@app.delete("/methods/<path:filename>")
def delete_saved_method(filename: str) -> Any:
    safe_filename = secure_filename(filename)
    if not safe_filename:
        return jsonify({"ok": False, "error": "Invalid filename."}), 400
    target = METHODS_DIR / safe_filename
    if not target.exists() or not target.is_file():
        return jsonify({"ok": False, "error": "Method not found."}), 404
    target.unlink()
    return jsonify({"ok": True, "filename": safe_filename})


@app.post("/load_method")
def load_method() -> Any:
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Upload a .txt file."}), 400

    file = request.files["file"]
    content = file.read().decode("utf-8", errors="replace")
    try:
        steps, warning, mode = _load_method_content(content)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    with state.lock:
        state.last_message = "Method loaded."
        state.last_error = ""

    return jsonify({"ok": True, "steps": steps, "warning": warning, "mode": mode})


@app.get("/run_logs/latest.csv")
def latest_run_log() -> Any:
    with state.lock:
        latest_path = Path(state.latest_run_log) if state.latest_run_log else None
    if latest_path is None or not latest_path.exists():
        logs = _run_log_files()
        latest_path = logs[0] if logs else None
    if latest_path is None or not latest_path.exists():
        return jsonify({"ok": False, "error": "No run logs are available yet."}), 404
    content = latest_path.read_text(encoding="utf-8", errors="replace")
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{latest_path.name}"'},
    )


if __name__ == "__main__":
    port = _first_available_port()
    print(f"Starting local GUI on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
