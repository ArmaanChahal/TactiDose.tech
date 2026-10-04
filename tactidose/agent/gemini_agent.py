"""Gemini function-calling loop for the patient agent (google-genai 2.x).

One :meth:`GeminiAgent.respond` call = one patient turn:

* ``contents`` = the conversation window (user/assistant text only; earlier tool calls are not
  replayed) + the patient's new message;
* config: the system prompt, one ``types.Tool`` with the declarations from
  :func:`~tactidose.agent.tools.tool_declarations`, automatic function calling **disabled**
  (every call goes through :class:`~tactidose.agent.tools.PatientTools`), default temperature;
* loop up to ``agent_max_steps`` model calls: function calls are executed by the patient-bound
  executor and answered with ``Part.from_function_response``-style parts; the model's own
  ``Content`` is appended unchanged so Gemini 3 thought signatures are preserved. The last step
  (when ``agent_max_steps >= 2``) forbids further calls (``FunctionCallingConfig(mode="NONE")``)
  so the model must answer in text;
* the whole turn shares one ``agent_timeout_s`` budget (per-request ``HttpOptions.timeout``);
* ``settings.agent_thinking_level`` "low" / "medium" / "high" sends
  ``ThinkingConfig(thinking_level=...)``; "" leaves the model's default (no ``thinking_config``);
* the configured model (``settings.effective_agent_model``) falls back once to
  ``settings.gemini_fallback_model`` on HTTP 404 / NOT_FOUND and keeps using it afterwards.

Redirects: the client is built with ``netsafe.gemini_http_options`` (``follow_redirects=False``;
the key is a custom header). google-genai 2.28 (read in ``_api_client.py``) never builds an httpx
client per request: a per-request ``HttpOptions`` only patches URL/headers/timeout/extra body
(``_build_request``) and retries (``_request``), and ``_request_once`` always sends through the
client's own ``_httpx_client``. The per-request options carry the same ``client_args`` anyway, so
an SDK version that did build one would inherit "no redirects".

Any model/network/SDK problem raises :class:`AgentModelError` *from* the original exception (or
from a ``TimeoutError`` when the turn budget runs out), so ``netsafe.classify_exception`` can
classify it; ``AgentService`` then answers with the rules agent. ``client`` may be any object with
``client.models.generate_content(model=, contents=, config=)`` (tests pass a scripted fake);
otherwise a ``genai.Client`` is created lazily, so construction never touches the network. The
API key is never logged.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from tactidose.agent.tools import MODEL_TOOLS, PatientTools, tool_declarations
from tactidose.config import Settings
from tactidose.integrations import netsafe

log = logging.getLogger(__name__)

#: Function calls handled per model step (a model asking for more gets an error result).
MAX_CALLS_PER_STEP = 6
#: ``agent_thinking_level`` -> ``types.ThinkingLevel`` member name ("minimal" is never sent).
THINKING_LEVELS = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH"}


class AgentModelError(Exception):
    """The model could not produce a reply (network, API error, timeout, empty/blocked answer)."""


@dataclass(frozen=True)
class GeminiTurn:
    text: str
    model: str
    steps: int


def _sdk() -> tuple[Any, Any]:
    from google import genai
    from google.genai import types

    return genai, types


def _api_error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return str(code)
    return str(getattr(exc, "status", "") or "unknown")


def is_model_not_found(exc: BaseException) -> bool:
    try:
        from google.genai import errors
    except ImportError:  # pragma: no cover - SDK installed in the dev venv
        return False
    if not isinstance(exc, errors.APIError):
        return False
    return getattr(exc, "code", None) == 404 or str(getattr(exc, "status", "") or "").upper() == "NOT_FOUND"


def describe_error(exc: BaseException) -> str:
    """Short, key-free description for logs and the fallback reason."""
    try:
        from google.genai import errors
    except ImportError:  # pragma: no cover
        errors = None  # type: ignore[assignment]
    if errors is not None and isinstance(exc, errors.APIError):
        return f"api_error:{_api_error_code(exc)}"
    name = type(exc).__name__
    if "timeout" in name.lower() or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)) or "connect" in name.lower():
        return "network"
    return name


def response_text(response: Any) -> str:
    """Concatenated non-thought text parts of the first candidate ("" when none)."""
    try:
        parts = response.candidates[0].content.parts or []
    except (AttributeError, IndexError, TypeError):
        return ""
    texts = [p.text for p in parts
             if isinstance(getattr(p, "text", None), str) and not getattr(p, "thought", False)]
    return " ".join(t.strip() for t in texts if t and t.strip())


def _finish_reason(response: Any) -> str:
    try:
        reason = response.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        feedback = getattr(response, "prompt_feedback", None)
        reason = getattr(feedback, "block_reason", None)
    name = getattr(reason, "name", None)
    return str(name or reason or "no candidate")


class GeminiAgent:
    """Gemini function-calling agent. Thread-safe; one instance per AgentService."""

    #: Never give a single request less than this, even when the turn budget is nearly spent.
    min_request_timeout_s: float = 2.0

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client
        self._lock = threading.Lock()
        self._model = settings.effective_agent_model
        key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
        self._secrets = tuple(s for s in (key,) if s)
        self._now: Callable[[], float] = time.monotonic

    @property
    def model(self) -> str:
        """Model id used for the next request (switches to the fallback after a 404)."""
        return self._model

    # ------------------------------------------------------------------ turn
    def respond(self, *, system_instruction: str, history: Sequence[Mapping[str, Any]],
                user_text: str, tools: PatientTools) -> GeminiTurn:
        """Run the function-calling loop for one patient message. Raises AgentModelError."""
        try:
            _, types = _sdk()
        except ImportError as exc:
            raise AgentModelError("google-genai is not installed") from exc
        client = self._get_client(types)
        deadline = self._now() + float(self.settings.agent_timeout_s)
        contents = self._history_contents(types, history)
        user_part = types.Part.from_text(text=user_text)
        if contents and contents[-1].role == "user":
            contents[-1].parts.append(user_part)  # a previous message got no reply: one user turn
        else:
            contents.append(types.Content(role="user", parts=[user_part]))
        tool = types.Tool(function_declarations=[
            self._declaration(types, d) for d in tool_declarations(int(self.settings.num_slots))])
        thinking = self._thinking_config(types)
        max_steps = int(self.settings.agent_max_steps)
        for step in range(1, max_steps + 1):
            text_only = max_steps > 1 and step == max_steps
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[tool],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))
                if text_only else None,
                thinking_config=thinking,
                http_options=netsafe.gemini_http_options(types, self._request_timeout_ms(deadline)),
            )
            response = self._generate(client, contents, config)
            calls = list(getattr(response, "function_calls", None) or [])
            if not calls:
                text = response_text(response)
                if not text:
                    raise AgentModelError(f"empty or blocked response ({_finish_reason(response)})")
                return GeminiTurn(text=text, model=self._model, steps=step)
            if step == max_steps:
                raise AgentModelError("the model still wanted tools after agent_max_steps")
            try:
                model_content = response.candidates[0].content
            except (AttributeError, IndexError, TypeError) as exc:
                raise AgentModelError("function calls without a candidate") from exc
            contents.append(model_content)
            parts = []
            for index, call in enumerate(calls):
                name = str(getattr(call, "name", "") or "")
                args = getattr(call, "args", None)
                if index >= MAX_CALLS_PER_STEP:
                    result: dict[str, Any] = {"error": "too_many_calls",
                                              "message": "Too many tool calls at once."}
                elif name not in MODEL_TOOLS:
                    result = {"error": "unknown_tool", "message": f"There is no tool named {name}."}
                else:
                    result = tools.execute(name, args if isinstance(args, Mapping) else {})
                parts.append(types.Part(function_response=types.FunctionResponse(
                    id=getattr(call, "id", None), name=name, response=result)))
            contents.append(types.Content(role="user", parts=parts))
        raise AgentModelError("no answer within agent_max_steps")  # pragma: no cover - loop returns/raises

    # ------------------------------------------------------------------ internals
    def _get_client(self, types: Any) -> Any:
        with self._lock:
            if self._client is None:
                if not self._secrets:
                    raise AgentModelError("no Gemini API key configured")
                genai, _ = _sdk()
                try:
                    self._client = genai.Client(
                        api_key=self._secrets[0],
                        vertexai=False,
                        http_options=netsafe.gemini_http_options(   # never follow redirects
                            types, max(1000, int(round(float(self.settings.agent_timeout_s) * 1000)))),
                    )
                except Exception as exc:  # noqa: BLE001
                    raise AgentModelError(f"Gemini client could not be created ({describe_error(exc)})") from exc
            return self._client

    def _thinking_config(self, types: Any) -> Any | None:
        level = THINKING_LEVELS.get(str(self.settings.agent_thinking_level or "").strip().lower())
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel[level]) if level else None

    def _request_timeout_ms(self, deadline: float) -> int:
        remaining = deadline - self._now()
        if remaining <= 0:
            raise AgentModelError("agent_timeout_s exceeded") from TimeoutError("agent_timeout_s exceeded")
        return int(max(self.min_request_timeout_s, remaining) * 1000)

    def _generate(self, client: Any, contents: list[Any], config: Any) -> Any:
        model = self._model
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:  # noqa: BLE001 - SDK, transport and timeout errors alike
            fallback = (self.settings.gemini_fallback_model or "").strip()
            if not (is_model_not_found(exc) and fallback and fallback != model):
                raise AgentModelError(self._redact(describe_error(exc))) from exc
            log.warning("Gemini model %r not found; using fallback %r from now on", model, fallback)
            with self._lock:
                self._model = fallback
            try:
                return client.models.generate_content(model=fallback, contents=contents, config=config)
            except Exception as exc2:  # noqa: BLE001
                raise AgentModelError(self._redact(describe_error(exc2))) from exc2

    @staticmethod
    def _declaration(types: Any, decl: Mapping[str, Any]) -> Any:
        kwargs: dict[str, Any] = {"name": decl["name"], "description": decl["description"]}
        if decl.get("parameters"):
            kwargs["parameters_json_schema"] = decl["parameters"]
        return types.FunctionDeclaration(**kwargs)

    @staticmethod
    def _history_contents(types: Any, history: Sequence[Mapping[str, Any]]) -> list[Any]:
        contents: list[Any] = []
        for msg in history:
            role = {"user": "user", "assistant": "model"}.get(str(msg.get("role")))
            text = str(msg.get("content") or "").strip()
            if role is None or not text:
                continue
            part = types.Part.from_text(text=text)
            if contents and contents[-1].role == role:
                contents[-1].parts.append(part)
            else:
                contents.append(types.Content(role=role, parts=[part]))
        while contents and contents[0].role != "user":
            contents.pop(0)
        return contents

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text
