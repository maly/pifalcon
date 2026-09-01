import ipaddress
import os
import re
import subprocess
import threading

from flask import Flask, jsonify, render_template, request


CONTROL_RE = re.compile(
    r"^\s*([a-zA-Z0-9_]+)\s+0x[0-9a-fA-F]+\s+\(([^)]+)\)\s*:\s*(.*)$"
)
MENU_RE = re.compile(r"^\s+(-?\d+):\s+(.+?)\s*$")
ATTRIBUTE_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9_-]*)=(-?\d+)")
SUPPORTED_TYPES = {"int", "int64", "bool", "menu", "intmenu"}
UNSAFE_FLAGS = {"disabled", "read-only", "write-only"}


class ControlError(Exception):
    pass


def _default_is_valid(control):
    default = control.get("default")
    if default is None:
        return False
    if control["type"] == "bool":
        return default in (0, 1)
    if control["type"] in ("menu", "intmenu"):
        return default in control["menu"]
    minimum = control.get("min")
    maximum = control.get("max")
    step = control.get("step", 1)
    if minimum is None or maximum is None or not minimum <= default <= maximum:
        return False
    return step > 0 and (default - minimum) % step == 0


def parse_controls(output):
    controls = []
    current = None
    group = "Other Controls"

    def finish_control():
        if current is None:
            return
        current["default_valid"] = _default_is_valid(current)
        if not (set(current["flags"]) & UNSAFE_FLAGS):
            controls.append(current)

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.endswith("Controls") and "0x" not in line:
            group = stripped
            continue

        match = CONTROL_RE.match(line)
        if match:
            finish_control()
            name, control_type, details = match.groups()
            control_type = control_type.strip().lower()
            current = None
            if control_type not in SUPPORTED_TYPES:
                continue
            attributes = {key: int(value) for key, value in ATTRIBUTE_RE.findall(details)}
            flags = []
            if "flags=" in details:
                flags_text = details.split("flags=", 1)[1]
                flags = [flag.strip() for flag in flags_text.split(",") if flag.strip()]
            current = {
                "name": name,
                "label": name.replace("_", " ").title(),
                "group": group,
                "type": control_type,
                "value": attributes.get("value"),
                "default": attributes.get("default"),
                "min": attributes.get("min"),
                "max": attributes.get("max"),
                "step": attributes.get("step", 1),
                "flags": flags,
                "inactive": "inactive" in flags,
                "menu": {},
            }
            continue

        menu_match = MENU_RE.match(line)
        if current is not None and current["type"] in ("menu", "intmenu") and menu_match:
            menu_value, menu_label = menu_match.groups()
            current["menu"][int(menu_value)] = menu_label

    finish_control()
    return controls


def validate_value(control, value):
    if control.get("inactive"):
        raise ControlError(f"Control {control['name']} is currently inactive")
    if isinstance(value, bool):
        numeric = int(value)
    elif isinstance(value, int):
        numeric = value
    else:
        raise ControlError("Control value must be an integer or boolean")

    if control["type"] == "bool":
        if numeric not in (0, 1):
            raise ControlError("Boolean control accepts only 0 or 1")
        return numeric
    if control["type"] in ("menu", "intmenu"):
        if numeric not in control["menu"]:
            raise ControlError(f"Unsupported menu value: {numeric}")
        return numeric

    minimum = control["min"]
    maximum = control["max"]
    step = control["step"]
    if numeric < minimum or numeric > maximum:
        raise ControlError(f"Value must be between {minimum} and {maximum}")
    if step <= 0 or (numeric - minimum) % step:
        raise ControlError(f"Value must use step {step}")
    return numeric


class V4L2Camera:
    def __init__(self, device):
        self.device = device
        self._lock = threading.RLock()

    def _run(self, *arguments):
        try:
            result = subprocess.run(
                ["/usr/bin/v4l2-ctl", f"--device={self.device}", *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=8,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
            stderr = getattr(error, "stderr", "") or ""
            detail = stderr.strip() or str(error)
            raise ControlError(f"v4l2-ctl failed: {detail}") from error
        return result.stdout

    def list_controls(self):
        with self._lock:
            controls = parse_controls(self._run("--list-ctrls-menus"))
            if not controls:
                raise ControlError("Camera reported no adjustable V4L2 controls")
            return controls

    def set_control(self, name, value):
        with self._lock:
            controls = {control["name"]: control for control in self.list_controls()}
            if name not in controls:
                raise ControlError(f"Unknown or unsupported control: {name}")
            numeric = validate_value(controls[name], value)
            self._run(f"--set-ctrl={name}={numeric}")

    def reset_defaults(self):
        with self._lock:
            initial = self.list_controls()
            automatic = [
                control for control in initial
                if "auto" in control["name"] or "automatic" in control["name"]
            ]
            remaining = [control for control in initial if control not in automatic]
            applied = []
            skipped = []
            errors = []

            for original in automatic + remaining:
                if not original["default_valid"]:
                    skipped.append(original["name"])
                    continue
                try:
                    current = {item["name"]: item for item in self.list_controls()}.get(original["name"])
                    if current is None or current["inactive"]:
                        skipped.append(original["name"])
                        continue
                    numeric = validate_value(current, original["default"])
                    self._run(f"--set-ctrl={original['name']}={numeric}")
                    applied.append(original["name"])
                except ControlError as error:
                    errors.append({"name": original["name"], "error": str(error)})
            return {"applied": applied, "skipped": skipped, "errors": errors}


def _parse_networks(networks):
    if networks is None:
        networks = os.environ.get(
            "ALLOWED_NETWORKS",
            "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        ).split(",")
    return [ipaddress.ip_network(network.strip()) for network in networks if network.strip()]


def create_app(camera=None, stream_url=None, allowed_networks=None):
    app = Flask(__name__)
    camera = camera or V4L2Camera(os.environ.get("CAMERA_DEVICE", "/dev/video0"))
    stream_url = stream_url or os.environ.get(
        "CAMERA_STREAM_URL", "http://gravipi.local:8080/stream"
    )
    networks = _parse_networks(allowed_networks)

    @app.before_request
    def restrict_to_lan():
        try:
            remote = ipaddress.ip_address(request.remote_addr)
        except ValueError:
            return jsonify(error="Invalid client address"), 403
        if not any(remote in network for network in networks):
            return jsonify(error="Camera controls are available only from the LAN"), 403
        return None

    def current_state():
        return {"device": camera.device, "controls": camera.list_controls()}

    @app.errorhandler(ControlError)
    def handle_control_error(error):
        return jsonify(error=str(error)), 400

    @app.get("/")
    def index():
        return render_template("index.html", stream_url=stream_url)

    @app.get("/api/controls")
    def controls():
        return jsonify(current_state())

    @app.put("/api/controls/<name>")
    def set_control(name):
        payload = request.get_json(silent=True) or {}
        if "value" not in payload:
            raise ControlError("JSON field 'value' is required")
        camera.set_control(name, payload["value"])
        return jsonify(current_state())

    @app.post("/api/reset")
    def reset():
        result = camera.reset_defaults()
        return jsonify({**current_state(), "reset": result})

    return app


app = create_app()
