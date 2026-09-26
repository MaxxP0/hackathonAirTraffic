"""Local HTTP adapter for the simulation and bundled radar interface."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from copy import deepcopy
import json
import math
import mimetypes
from pathlib import Path
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

from .cli import SCENARIOS, advance_simulation, load_agent, validate_reset, validate_step
from .environment import AirTrafficEnv
from .landing_metrics import landing_metrics


WEB_ROOT = Path(__file__).parent / "web"
MAX_BODY_BYTES = 1_000_000
OPENROUTER_DEFAULT_MODEL = "z-ai/glm-5.3-flash"


class SimulationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, *, seed=7, scenario="mixed", duration=1800,
                 agent_spec="reference", base_url="http://127.0.0.1:1234", model=None,
                 llm_timeout=900, max_tokens=8192):
        self.env = AirTrafficEnv(seed=seed, scenario=scenario, duration_s=duration)
        self.state_lock = threading.Lock()
        # Model inference may take seconds. Serialize mutations but allow state
        # reads while it runs, and never advance the clock on a failed request.
        self.operation_lock = threading.Lock()
        self.agent_options = dict(base_url=base_url, model=model, timeout_s=llm_timeout,
                                  max_tokens=max_tokens)
        self.set_controller(agent_spec)
        super().__init__(address, SimulationHandler)

    def set_controller(self, kind, options=None):
        if kind not in {"reference", "lmstudio", "openrouter"}:
            raise ValueError("controller kind must be reference, lmstudio or openrouter")
        options = self.agent_options if options is None else options
        options = dict(options)
        if kind == "openrouter":
            options["model"] = options.get("model") or OPENROUTER_DEFAULT_MODEL
            # Hosted credentials and endpoint belong to the agent. The HTTP
            # interface cannot supply an API key or override its destination.
            agent_options = {key: options[key] for key in ("model", "timeout_s", "max_tokens")}
        else:
            agent_options = options
        replacement = load_agent(kind, **agent_options)
        with self.state_lock:
            self.agent, self.agent_options = replacement, dict(options)
            self.controller = {"kind": kind, "model": options.get("model") if kind != "reference" else None,
                               "status": "idle", "message": "Ready for the next simulation step",
                               "decision_count": 0, "last_decision": None, "error": None}
            if kind != "openrouter":
                self.controller["base_url"] = options["base_url"]

    def observation(self):
        with self.state_lock:
            result = self.env.observe()
            result["landing_metrics"] = landing_metrics(result)
            result["controller"] = deepcopy(self.controller)
            result["controller"]["model"] = getattr(self.agent, "model", self.controller["model"])
            if self.controller["kind"] == "openrouter":
                try:
                    metadata_fn = getattr(self.agent, "metadata", None)
                    metadata = metadata_fn() if callable(metadata_fn) else {}
                    budget = metadata.get("budget", {})
                    if not isinstance(budget, dict):
                        raise ValueError("invalid budget metadata")
                    public_budget = {}
                    for key in ("limit_usd", "spent_usd", "reserved_usd", "remaining_usd"):
                        if key not in budget:
                            continue
                        value = budget[key]
                        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                            raise ValueError("invalid budget metadata")
                        public_budget[key] = value
                    result["controller"]["budget"] = public_budget
                    result["controller"]["budget_blocked"] = budget.get("blocked") is True
                except Exception:
                    # Optional cost diagnostics must not break state polling or
                    # expose raw ledger/credential data from an exception. The
                    # agent's budget guard still gates every paid request.
                    result["controller"]["budget_error"] = "OpenRouter budget information is unavailable."
            return result


class SimulationHandler(BaseHTTPRequestHandler):
    server_version = "ATCBench/1.0"

    def send_error(self, code, message=None, explain=None):
        self._json({"error": message or self.responses.get(code, ("request error",))[0]}, code)

    def _json(self, value: dict, status: int = 200) -> None:
        body = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/api/state":
            self._json(self.server.observation())
        elif path == "/api/scenarios":
            self._json({"scenarios": list(SCENARIOS)})
        elif path.startswith("/api/"):
            self._json({"error": "unknown endpoint"}, 404)
        else:
            try:
                root = WEB_ROOT.resolve()
                target = (root / (path.lstrip("/") or "index.html")).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    self._json({"error": "file not found"}, 404)
                    return
                body = target.read_bytes()
                self.send_response(200)
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or content_type == "application/javascript":
                    content_type += "; charset=utf-8"
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ValueError):
                self._json({"error": "file not found"}, 404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/step", "/api/reset", "/api/controller"}:
            self._json({"error": "unknown endpoint"}, 404)
            return
        try:
            # JSON-only requests prevent ordinary cross-origin forms from
            # driving the local environment. No CORS headers are enabled.
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                self._json({"error": "Content-Type must be application/json"}, 415)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                self._json({"error": "JSON body must contain between 1 byte and 1 MB"}, 413)
                return

            def reject_constant(value):
                raise ValueError(f"non-finite JSON number: {value}")

            payload = json.loads(self.rfile.read(length), parse_constant=reject_constant)
            if not self.server.operation_lock.acquire(blocking=False):
                self._json({"error": "A controller decision is in progress; wait for it to finish.",
                            "controller": self.server.observation()["controller"]}, 409)
                return
            try:
                if path == "/api/controller":
                    self._configure_controller(payload)
                elif path == "/api/reset":
                    config = validate_reset(payload, self.server.env)
                    replacement = AirTrafficEnv(**config)
                    # Construct both before replacing the current episode.
                    self.server.set_controller(self.server.controller["kind"])
                    with self.server.state_lock:
                        self.server.env = replacement
                else:
                    self._step(payload)
                self._json(self.server.observation())
            finally:
                self.server.operation_lock.release()
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as error:
            self._json({"error": str(error)}, 400)
        except (OSError, RuntimeError) as error:
            self._json({"error": str(error), "controller": self.server.observation()["controller"]}, 502)

    def _configure_controller(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"kind", "base_url", "model"}:
            raise ValueError("controller expects kind, optional base_url and model")
        if payload.get("kind") == "openrouter" and "base_url" in payload:
            raise ValueError("OpenRouter uses a fixed hosted endpoint; base_url is only configurable for LM Studio")
        options = dict(self.server.agent_options)
        if payload.get("kind") != self.server.controller["kind"] and "model" not in payload:
            options["model"] = None
        for key in ("base_url", "model"):
            if key in payload:
                if payload[key] is not None and not isinstance(payload[key], str):
                    raise ValueError(f"{key} must be a string")
                options[key] = payload[key] or ("http://127.0.0.1:1234" if key == "base_url" else None)
        self.server.set_controller(payload.get("kind"), options)

    def _step(self, payload):
        commands, seconds, autopilot = validate_step(payload, allow_autopilot=True, max_seconds=600)
        if autopilot and not self.server.env.done:
            controller = self.server.controller
            with self.server.state_lock:
                provider = {"lmstudio": "LM Studio", "openrouter": "OpenRouter"}.get(controller["kind"])
                controller.update(status="thinking", message=f"Requesting a decision from {provider}" if provider else "Evaluating reference policy", error=None)
                before = self.server.env.observe()
                before["decision_interval_s"] = seconds
            started = time.monotonic()
            try:
                automatic = self.server.agent.act(before)
                if not isinstance(automatic, list) or any(not isinstance(c, (str, dict)) for c in automatic):
                    raise RuntimeError("Controller returned an invalid command list")
            except Exception as error:
                with self.server.state_lock:
                    controller.update(status="error", error=str(error), message="Controller failed. Simulation time has not advanced.",
                                      last_decision=deepcopy(getattr(self.server.agent, "last_decision", None)))
                raise RuntimeError(f"{controller['kind']} controller: {error}") from error
            decision = deepcopy(getattr(self.server.agent, "last_decision", None)) or {
                "summary": f"Issued {len(automatic)} command(s)" if automatic else "Waiting for aircraft to complete clearances or a runway to become available",
                "commands": automatic, "latency_s": round(time.monotonic() - started, 3)}
            with self.server.state_lock:
                controller.update(status="ready", message=decision.get("summary", "Decision received"), last_decision=decision,
                                  decision_count=controller["decision_count"] + 1,
                                  model=getattr(self.server.agent, "model", controller["model"]))
            # A manual command overrides automatic commands for that callsign.
            def callsign(command):
                if isinstance(command, dict):
                    value = command.get("callsign", "")
                    return value.upper() if isinstance(value, str) else ""
                bits = command.split()
                return bits[1].upper() if len(bits) > 1 else ""
            manual_callsigns = {callsign(command) for command in commands if isinstance(command, (str, dict))}
            automatic = [command for command in automatic if callsign(command) not in manual_callsigns]
            commands = commands + automatic[:100 - len(commands)]
        with self.server.state_lock:
            advance_simulation(self.server.env, commands, seconds)
            if self.server.env.done:
                self.server.controller.update(status="idle", message="Episode complete. Start a new episode to continue.")


def serve(*, port: int = 8000, seed: int = 7, scenario: str = "mixed", duration: int = 1800,
          agent_spec="reference", base_url="http://127.0.0.1:1234", model=None,
          llm_timeout=900, max_tokens=8192) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535")
    with SimulationServer(("127.0.0.1", port), seed=seed, scenario=scenario, duration=duration,
                          agent_spec=agent_spec, base_url=base_url, model=model,
                          llm_timeout=llm_timeout, max_tokens=max_tokens) as server:
        actual_port = server.server_address[1]
        print(f"Radar: http://127.0.0.1:{actual_port} (Ctrl-C to stop)", file=sys.stderr, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
