"""Persistent OpenRouter controller with a shared, fail-closed $10 spend ledger.

Only OPENROUTER_API_KEY supplies credentials. Authenticated requests use the
fixed OpenRouter HTTPS endpoint, refuse redirects, and never log response bodies
or credentials on errors. This module does not read dotenv files.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
import os
import sys
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .lmstudio import LMStudioAgent, LMStudioError


DEFAULT_MODEL = "z-ai/glm-5.3-flash"
BASE_URL = "https://openrouter.ai/api"
DEFAULT_BUDGET_PATH = Path(__file__).resolve().parents[1] / "results" / "openrouter-budget.json"
HARD_BUDGET_USD = 10
NANODOLLARS = 1_000_000_000
PRICE_CAPS = {"prompt": 1, "completion": 2, "request": 0}
MAX_RESPONSE_BYTES = 16_000_000


class OpenRouterError(LMStudioError):
    """Sanitized OpenRouter transport, model, or spending failure."""


class OpenRouterHTTPError(OpenRouterError):
    def __init__(self, status_code, retry_after_s=None, detail=None, provider_name=None):
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        self.provider_name = provider_name
        super().__init__(f"OpenRouter returned HTTP {status_code}" + (f": {detail}" if detail else "; response details withheld"))


class BudgetError(OpenRouterError):
    """The persistent spending guard cannot authorize another request."""


def _money(value) -> int:
    """Round costs upward to integer nanodollars; never accept NaN or negatives."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BudgetError("Invalid monetary value in OpenRouter accounting")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("1000000"):
            raise InvalidOperation
        return int((amount * NANODOLLARS).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, OverflowError):
        raise BudgetError("Invalid monetary value in OpenRouter accounting") from None


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


class SpendLedger:
    """Process-safe reservations; neither a new agent nor a reset clears costs.

    A stable sibling lock protects atomic ledger replacements across processes.
    Missing/corrupt ledgers after initialization fail closed. Unknown charges
    retain their entire reservation indefinitely; there is no reset/refund API.
    """

    def __init__(self, path=None, budget_usd=HARD_BUDGET_USD):
        self.path = Path(path if path is not None else DEFAULT_BUDGET_PATH).expanduser().absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.limit = _money(budget_usd)
        if not 0 < self.limit <= HARD_BUDGET_USD * NANODOLLARS:
            raise BudgetError("OpenRouter budget must be greater than zero and at most $10")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked() as (state, lock):
            # A lower configured limit stays lower across later constructors.
            state["limit_nusd"] = min(state["limit_nusd"], self.limit)
            self._save(state)
            if not lock.read(1):
                lock.seek(0)
                lock.write(b"openrouter-budget-v1\n")
                lock.flush()
                os.fsync(lock.fileno())

    def _validate(self, state):
        if not isinstance(state, dict) or set(state) != {"version", "limit_nusd", "entries", "blocked"}:
            raise BudgetError("OpenRouter budget ledger is malformed; requests are blocked")
        if state["version"] != 1 or not _integer(state["limit_nusd"], 1) or state["limit_nusd"] > HARD_BUDGET_USD * NANODOLLARS:
            raise BudgetError("OpenRouter budget ledger has an invalid limit or version")
        if not isinstance(state["entries"], dict) or not isinstance(state["blocked"], bool):
            raise BudgetError("OpenRouter budget ledger is malformed; requests are blocked")
        for identity, entry in state["entries"].items():
            if not isinstance(identity, str) or not isinstance(entry, dict) or set(entry) != {"reserved_nusd", "cost_nusd", "status", "model", "created_s"}:
                raise BudgetError("OpenRouter budget ledger contains a malformed reservation")
            if not _integer(entry["reserved_nusd"], 1) or not _integer(entry["created_s"]) or not isinstance(entry["model"], str):
                raise BudgetError("OpenRouter budget ledger contains invalid reservation values")
            if entry["status"] not in {"reserved", "uncertain", "settled", "overrun"}:
                raise BudgetError("OpenRouter budget ledger contains an invalid reservation status")
            if entry["status"] in {"settled", "overrun"}:
                if not _integer(entry["cost_nusd"]):
                    raise BudgetError("OpenRouter budget ledger has an invalid recorded charge")
                if entry["status"] == "settled" and entry["cost_nusd"] > entry["reserved_nusd"]:
                    raise BudgetError("OpenRouter budget ledger contains an unmarked cost overrun")
                if entry["status"] == "overrun" and not state["blocked"]:
                    raise BudgetError("OpenRouter budget ledger must block after a cost overrun")
            elif entry["cost_nusd"] is not None:
                raise BudgetError("OpenRouter budget ledger has a charge without settlement")
        return state

    @contextmanager
    def _locked(self):
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise BudgetError("OpenRouter budget files must not be symbolic links")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            with os.fdopen(descriptor, "r+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                lock.seek(0)
                initialized = bool(lock.read(1))
                if self.path.exists():
                    try:
                        state = self._validate(json.loads(self.path.read_text(encoding="utf-8")))
                    except (ValueError, UnicodeError):
                        raise BudgetError("OpenRouter budget ledger is unreadable; requests are blocked") from None
                elif initialized:
                    raise BudgetError("OpenRouter budget ledger is missing after initialization; requests are blocked")
                else:
                    state = {"version": 1, "limit_nusd": self.limit, "entries": {}, "blocked": False}
                lock.seek(0)
                yield state, lock
        except OSError:
            raise BudgetError("OpenRouter budget ledger could not be locked or persisted; requests are blocked") from None

    def _save(self, state):
        self._validate(state)
        name = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump(state, temporary, sort_keys=True, allow_nan=False)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(name, self.path)
            name = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if name is not None:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _totals(state):
        spent = sum(entry["cost_nusd"] for entry in state["entries"].values()
                    if entry["status"] in {"settled", "overrun"})
        reserved = sum(entry["reserved_nusd"] for entry in state["entries"].values()
                       if entry["status"] in {"reserved", "uncertain"})
        return spent, reserved

    def snapshot(self):
        with self._locked() as (state, _):
            spent, reserved = self._totals(state)
            return {"limit_usd": state["limit_nusd"] / NANODOLLARS,
                    "spent_usd": spent / NANODOLLARS, "reserved_usd": reserved / NANODOLLARS,
                    "remaining_usd": max(0, state["limit_nusd"] - spent - reserved) / NANODOLLARS,
                    "blocked": state["blocked"], "request_count": len(state["entries"])}

    def reserve(self, amount_nusd: int, model: str):
        if not _integer(amount_nusd, 1):
            raise BudgetError("OpenRouter reservation must be a positive integer amount")
        with self._locked() as (state, _):
            spent, reserved = self._totals(state)
            if state["blocked"] or spent + reserved + amount_nusd > state["limit_nusd"]:
                raise BudgetError("OpenRouter spending limit reached; no request was sent")
            identity = uuid.uuid4().hex
            state["entries"][identity] = {"reserved_nusd": amount_nusd, "cost_nusd": None,
                                          "status": "reserved", "model": model, "created_s": int(time.time())}
            self._save(state)
            return identity

    def finish(self, identity, cost=None):
        """Settle a verified charge; missing/invalid cost keeps the full reserve."""
        try:
            actual = _money(cost) if cost is not None else None
        except BudgetError:
            actual = None
        overrun = False
        with self._locked() as (state, _):
            if identity not in state["entries"]:
                raise BudgetError("OpenRouter reservation is missing; requests are blocked")
            entry = state["entries"][identity]
            if entry["status"] not in {"reserved", "uncertain"}:
                raise BudgetError("OpenRouter reservation was already settled")
            if actual is None:
                entry["status"] = "uncertain"
            else:
                entry["cost_nusd"] = actual
                overrun = actual > entry["reserved_nusd"]
                entry["status"] = "overrun" if overrun else "settled"
                state["blocked"] = state["blocked"] or overrun
            self._save(state)
        if overrun:
            raise BudgetError("OpenRouter reported a charge above its reserved bound; further requests are blocked")
        return actual / NANODOLLARS if actual is not None else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OpenRouterError("OpenRouter redirects are refused; credentials were not forwarded")


def _redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {_redact(key, secret): _redact(item, secret) for key, item in value.items()}
    return value


class OpenRouterAgent(LMStudioAgent):
    provider_label = "OpenRouter"

    def __init__(self, model=DEFAULT_MODEL, timeout_s=180, max_tokens=8192,
                 temperature=0, max_memory_turns=4, budget_usd=HARD_BUDGET_USD, budget_path=None):
        model = model or DEFAULT_MODEL
        if (not isinstance(model, str) or not model.strip() or len(model) > 200
                or ":" in model or "@" in model or model.startswith("openrouter/")):
            raise OpenRouterError("OpenRouter requires one explicit catalog model ID without routing variants")
        super().__init__(base_url=BASE_URL, model=model, timeout_s=timeout_s,
                         max_tokens=max_tokens, temperature=temperature, max_memory_turns=max_memory_turns)
        self.ledger = SpendLedger(budget_path, budget_usd)
        self.reasoning_mode = "low"
        self.reasoning_effort = "low"
        self._last_charge = None
        self._context_length = None
        self._request_attempts = []

    def _http_json(self, path, payload=None):
        if (path, payload is None) not in {("/v1/models", True), ("/v1/chat/completions", False)}:
            raise OpenRouterError("Unsupported OpenRouter endpoint")
        headers = {"Content-Type": "application/json"}
        secret = None
        if payload is not None:
            secret = os.environ.get("OPENROUTER_API_KEY", "").strip()
            if not secret or any(character.isspace() for character in secret):
                raise OpenRouterError("Set OPENROUTER_API_KEY in the server or CLI environment")
            headers["Authorization"] = "Bearer " + secret
        request = Request(BASE_URL + path, data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
                          headers=headers)
        deadline = time.monotonic() + self.timeout_s
        try:
            # Never use self.base_url here: even mutation cannot redirect auth.
            with build_opener(_NoRedirect()).open(request, timeout=self.timeout_s) as response:
                raw = self._read_body(response, deadline)
                data = json.loads(raw)
        except HTTPError as error:
            detail = provider_name = None
            try:
                failure = _redact(json.loads(error.read(8192)), secret).get("error", {})
                if isinstance(failure, dict):
                    if isinstance(failure.get("message"), str):
                        detail = failure["message"][:300]
                    metadata = failure.get("metadata", {})
                    if isinstance(metadata, dict) and isinstance(metadata.get("provider_name"), str):
                        provider_name = metadata["provider_name"][:100]
                        detail = f"{detail or 'Provider error'} ({provider_name})"
            except (ValueError, OSError, AttributeError):
                pass
            try:
                retry_after = float(error.headers.get("Retry-After", "nan"))
                if not math.isfinite(retry_after) or retry_after < 0:
                    retry_after = None
            except (ValueError, TypeError, AttributeError):
                try:
                    retry_after = max(0, parsedate_to_datetime(error.headers.get("Retry-After", "")).timestamp() - time.time())
                except (ValueError, TypeError, AttributeError, OverflowError):
                    retry_after = None
            error.close()
            raise OpenRouterHTTPError(error.code, retry_after, detail, provider_name) from None
        except (URLError, OSError, ValueError):
            raise OpenRouterError("OpenRouter connection, timeout, or response-decoding failure") from None
        data = _redact(data, secret)
        if not isinstance(data, dict) or "error" in data:
            raise OpenRouterError("OpenRouter returned an API error or invalid response object")
        return data

    @staticmethod
    def _read_body(response, deadline):
        chunks, size = [], 0
        read = getattr(response, "read1", response.read)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenRouterError("OpenRouter request exceeded its total wall-clock deadline")
            # urllib's timeout otherwise applies independently to every socket
            # operation, so keepalive bytes can stretch a request indefinitely.
            socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if socket is not None:
                socket.settimeout(remaining)
            block = read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not block:
                return b"".join(chunks)
            chunks.append(block)
            size += len(block)
            if size > MAX_RESPONSE_BYTES:
                raise OpenRouterError("OpenRouter response exceeded the bounded response size")

    def _discover_model(self):
        inventory = self._http_json("/v1/models")
        entries = inventory.get("data")
        if not isinstance(entries, list):
            raise OpenRouterError("OpenRouter model inventory has no data list")
        info = next((item for item in entries if isinstance(item, dict) and item.get("id") == self.requested_model), None)
        if info is None:
            raise OpenRouterError("Requested OpenRouter model is not present in the model catalog")
        supported = info.get("supported_parameters", [])
        if (not isinstance(supported, list) or not all(isinstance(item, str) for item in supported)
                or not {"response_format", "structured_outputs", "max_tokens", "reasoning_effort"} <= set(supported)):
            raise OpenRouterError("Requested OpenRouter model lacks required structured-output, token-limit or reasoning controls")
        context = info.get("context_length")
        if not _integer(context, 1) or context > 10_000_000 or self.max_tokens >= context:
            raise OpenRouterError("OpenRouter model has an invalid or insufficient context limit")
        self.model = self.requested_model
        self._context_length = context
        self.model_info = {key: deepcopy(info[key]) for key in ("id", "name", "context_length", "pricing", "supported_parameters", "architecture", "top_provider") if key in info}
        self._resolved_model = True

    def _reservation_bound(self, payload):
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages or any(
            not isinstance(message, dict) or set(message) != {"role", "content"}
            or message["role"] not in {"system", "user", "assistant"} or not isinstance(message["content"], str)
            for message in messages
        ):
            raise OpenRouterError("OpenRouter budget guard accepts only text conversation messages")
        if payload.get("model") != self.model or payload.get("max_tokens") != self.max_tokens or payload.get("stream") is not False:
            raise OpenRouterError("OpenRouter request does not match its budgeted model and token limit")
        input_bytes = len(json.dumps({"messages": messages, "response_format": payload.get("response_format")},
                                    ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        # One token per byte plus generous role/schema framing is a conservative
        # admission check. Reserve the FULL advertised context to cover any
        # provider-added wrappers as well; actual confirmed cost releases margin.
        byte_bound = input_bytes + 16384 + 1024 * len(messages)
        if self._context_length is None or byte_bound + self.max_tokens > self._context_length:
            raise OpenRouterError("OpenRouter input exceeds the conservative context allowance; no request sent")
        amount = self._context_length * PRICE_CAPS["prompt"] * 1000 + self.max_tokens * PRICE_CAPS["completion"] * 1000
        return amount, byte_bound

    def _json_request(self, path, payload=None):
        if path != "/v1/chat/completions" or payload is None:
            raise OpenRouterError("Unsupported OpenRouter completion endpoint")
        amount, byte_bound = self._reservation_bound(payload)
        # Verify configuration before reserving, without retaining the credential.
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise OpenRouterError("Set OPENROUTER_API_KEY in the server or CLI environment")
        protected = deepcopy(payload)
        protected.update(provider={"max_price": dict(PRICE_CAPS), "require_parameters": True,
                                    "allow_fallbacks": False, "sort": "throughput"},
                         plugins=[], reasoning={"effort": "low"})
        # Some reasoning models do not expose sampling temperature. Omitting
        # unsupported parameters avoids silently changing providers or models.
        if "temperature" not in self.model_info.get("supported_parameters", []):
            protected.pop("temperature", None)
        if set(protected) - {"model", "messages", "temperature", "max_tokens", "response_format", "stream", "provider", "plugins", "reasoning"}:
            raise OpenRouterError("Unexpected OpenRouter request options are blocked by the budget guard")
        for attempt in range(3):
            reservation = self.ledger.reserve(amount, self.model)
            self._last_charge = {"reservation_id": reservation, "reserved_cost_usd": amount / NANODOLLARS,
                                 "input_byte_token_bound": byte_bound, "reserved_input_tokens": self._context_length,
                                 "cost_usd": None, "provider_name": None, "accounting_status": "reserved"}
            try:
                response = self._http_json(path, protected)
                break
            except OpenRouterHTTPError as error:
                if error.status_code != 429:
                    raise
                # A failed provider request can still incur a charge. Retain
                # this reservation and independently budget a bounded retry.
                self.ledger.finish(reservation)
                self._last_charge["accounting_status"] = "uncertain"
                self._last_charge["provider_name"] = error.provider_name
                delay = max(30 * (attempt + 1), error.retry_after_s or 0)
                self._request_attempts.append({**self._last_charge, "http_status": 429,
                                               "retry_after_s": error.retry_after_s,
                                               "wait_s": delay if attempt < 2 and delay <= 60 else 0})
                if attempt == 2 or delay > 60:
                    raise
                print(f"OpenRouter rate limited; retry {attempt + 1}/2 in {delay:g}s while simulation stays paused",
                      file=sys.stderr, flush=True)
                time.sleep(delay)
        usage = response.get("usage", {})
        if isinstance(usage, dict):
            try:
                self._last_charge["cost_usd"] = _money(usage.get("cost")) / NANODOLLARS
            except BudgetError:
                pass
        if isinstance(response.get("provider"), str):
            self._last_charge["provider_name"] = response["provider"][:200]
        if response.get("model") not in {None, self.model}:
            raise OpenRouterError("OpenRouter returned a different model; commands were not accepted")
        return response

    def _public_budget(self):
        try:
            return self.ledger.snapshot()
        except BudgetError:
            return {"limit_usd": min(HARD_BUDGET_USD, self.ledger.limit / NANODOLLARS),
                    "spent_usd": None, "reserved_usd": None, "remaining_usd": 0,
                    "blocked": True, "error": "Persistent budget ledger is unavailable; requests are blocked"}

    def describe(self):
        result = super().describe()
        result.update(agent="openrouter", provider="openrouter", base_url=BASE_URL,
                      reasoning_mode="low", reasoning_effort="low", price_caps_usd_per_million=dict(PRICE_CAPS),
                      budget=self._public_budget(), reservation_policy="full advertised context plus max output tokens at provider price caps")
        result.pop("advertised_reasoning_default", None)
        result["temperature_sent"] = "temperature" in self.model_info.get("supported_parameters", [])
        result["provider_sort"] = "throughput"
        result["rate_limit_retry_policy"] = "at most two retries; 30/60 second backoff; honor Retry-After up to 60 seconds"
        return result

    def act(self, observation):
        self._last_charge = None
        self._request_attempts = []
        succeeded = False
        identity = tuple(observation.get(key) for key in ("seed", "scenario", "duration_s"))
        observed_time = observation.get("time_s")
        resetting = self._episode_identity is not None and (
            identity != self._episode_identity or
            (observed_time is not None and self._last_observation_time_s is not None
             and observed_time < self._last_observation_time_s)
        )
        previous = ([], "", 0) if resetting else (deepcopy(self._conversation), self.latest_plan, self._successful_decisions)
        try:
            commands = super().act(observation)
            if self._last_charge:
                actual = self.ledger.finish(self._last_charge["reservation_id"], self._last_charge["cost_usd"])
                self._last_charge["accounting_status"] = "settled" if actual is not None else "uncertain"
            succeeded = True
            return commands
        except BudgetError as error:
            # A billing-guard failure cannot publish commands or erase the plan.
            if self.last_decision and self.last_decision.get("status") == "ok":
                self._conversation, self.latest_plan, self._successful_decisions = previous
                self._errors += 1
                self._last_decision_error = str(error)
                self.last_decision.update(status="error", commands=[], plan=self.latest_plan, error=str(error),
                                          memory_turns=len(self._conversation) // 2)
            raise
        finally:
            if self._last_charge and not succeeded and self._last_charge["accounting_status"] == "reserved":
                try:
                    actual = self.ledger.finish(self._last_charge["reservation_id"], self._last_charge["cost_usd"])
                    self._last_charge["accounting_status"] = "settled" if actual is not None else "uncertain"
                except BudgetError:
                    self._last_charge["accounting_status"] = "blocked"
            if self.last_decision is not None:
                self.last_decision.update(provider="openrouter", cost_usd=(self._last_charge or {}).get("cost_usd"),
                                          budget=self._public_budget(), provider_sort="throughput")
                self.last_decision.pop("advertised_reasoning_default", None)
                if self._last_charge:
                    self.last_decision.update(deepcopy(self._last_charge))
                if self._request_attempts:
                    self.last_decision["rate_limit_attempts"] = deepcopy(self._request_attempts)
