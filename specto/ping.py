"""`specto doctor --ping`: one tiny real model call, so the first real run fails early and plainly.

The call goes through the same `ClaudeCaller` or `GeminiCaller` that the
pipeline uses (built by `cli.make_caller`), with structured output into a
two-field model, one short line of prompt and a small answer budget. On
success it says who answered, what it cost and "ready". On failure it turns
the error into one plain sentence with the fix. It never prints a key, or any
part of one: every message is scrubbed of the key values in the environment
and of anything shaped like a key before it is shown.

Exit codes: 0 ready, 1 the model service said no, 2 no key (no network call
is made).
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Callable, Optional

from pydantic import BaseModel

from specto.doctor import api_key_set, gemini_key_set
from specto.estimate import CACHE_READ_FRACTION, PRICES

PING_SYSTEM = "You are a connectivity check. Answer with the JSON asked for and nothing else."
PING_PROMPT = 'Reply with ok set to true and word set to "ready".'
# Current Claude models think before they answer and Gemini 2.5 Pro cannot
# switch thinking off; the thinking is paid for out of this budget, so it has
# to be far more than the ten tokens of answer. Worst case this costs a few cents.
PING_MAX_TOKENS = 4000
PING_EFFORT = "low"

CLAUDE_KEY_HELP = "export ANTHROPIC_API_KEY=... (create one at https://console.anthropic.com/settings/keys)"
GEMINI_KEY_HELP = "export GEMINI_API_KEY=... (create one at https://aistudio.google.com/apikey)"
CACHE_WRITE_MULTIPLIER = 1.25

_KEY_SHAPES = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|AIza[A-Za-z0-9_\-]+|([?&]key=)[^&\s'\"]+)")
_KEY_VARS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


class Ping(BaseModel):
    ok: bool
    word: str


def scrub(text: str) -> str:
    """`text` with every key in the environment, and anything shaped like a key, replaced."""
    for name in _KEY_VARS:
        value = os.environ.get(name, "").strip()
        if len(value) >= 8:  # a shorter value would blank out ordinary words
            text = text.replace(value, "[key hidden]")
    return _KEY_SHAPES.sub(lambda m: (m.group(2) or "") + "[key hidden]", text)


def cost_cents(model: str, usage: dict) -> Optional[float]:
    """The estimated cost of one call in cents at the PRICES rates, or None when the model has no price."""
    if model not in PRICES:
        return None
    input_price, output_price = PRICES[model]
    dollars = (
        usage.get("input_tokens", 0) * input_price
        + usage.get("output_tokens", 0) * output_price
        + usage.get("cache_read_input_tokens", 0) * input_price * CACHE_READ_FRACTION
        + usage.get("cache_creation_input_tokens", 0) * input_price * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000
    return dollars * 100


def format_cents(cents: Optional[float], model: str) -> str:
    if cents is None:
        return f"not estimated (specto has no price for {model})"
    if cents < 0.01:
        return "under 0.01 cents (a hundredth of a cent)"
    return f"about {cents:.2f} cents"


# ------------------------------------------------------------------ errors


def _status_of(error: BaseException) -> Optional[int]:
    status = getattr(error, "status_code", None)  # anthropic.APIStatusError
    if status is None:
        status = getattr(error, "status", None)  # GeminiAPIError
    return status if isinstance(status, int) else None


def _is_network_error(error: BaseException) -> bool:
    try:
        import anthropic

        if isinstance(error, anthropic.APIConnectionError):  # includes timeouts
            return True
    except ImportError:  # pragma: no cover - anthropic is a dependency
        pass
    from specto.gemini import GeminiAPIError

    if isinstance(error, GeminiAPIError) and error.status is None:
        return True
    return isinstance(error, (OSError, TimeoutError))


def _server_message(error: BaseException) -> str:
    message = getattr(error, "message", None)
    return scrub(str(message if isinstance(message, str) and message else error)).strip()


def explain_error(provider: str, model: str, error: BaseException) -> str:
    """One plain sentence saying what went wrong and how to fix it. Contains no key."""
    gemini = provider == "gemini"
    key_name = "GEMINI_API_KEY" if gemini else "ANTHROPIC_API_KEY"
    key_help = GEMINI_KEY_HELP if gemini else CLAUDE_KEY_HELP
    text = _server_message(error)
    lowered = text.lower()

    if _is_network_error(error):
        return (
            f"specto could not reach the {'Gemini' if gemini else 'Claude'} service ({text}); "
            f"check the internet connection, VPN or proxy, then run it again."
        )
    status = _status_of(error)
    if status == 401 or (gemini and status == 400 and ("api key not valid" in lowered or "api_key_invalid" in lowered)):
        return f"The service rejected {key_name} as invalid (a 401 error); check it is the whole key with no spaces or quotes, or make a new one: {key_help}."
    if status == 403 and gemini and ("has not been used in project" in lowered or "disabled" in lowered):
        return (
            "The Generative Language API is not switched on in the Google project this key belongs to (a 403 error); "
            "open the Google Cloud console, go to APIs & Services > Library > Generative Language API, press Enable, "
            "then run it again."
        )
    if status == 403:
        return f"This key is not allowed to make this request (a 403 error); the service says: {text}. Check the key's project and permissions."
    if status == 404:
        return (
            f"The account behind {key_name} has no access to a model called {model}, or the name is misspelt (a 404 error); "
            f"pass --model with a model your account can use, for example "
            f"{'gemini-2.5-flash' if gemini else 'claude-sonnet-5'}."
        )
    if status == 400 and not gemini and "credit balance" in lowered:
        return "The account has no credit left (a 400 error from the billing check); add credit at https://console.anthropic.com/settings/billing and run it again."
    if status == 400:
        return (
            f"The service rejected the request (a 400 error); it says: {text}. "
            f"Try a different --model, and report this message if it keeps happening."
        )
    if status == 429:
        return (
            "The service is limiting this key (a 429 error), either too many requests or no credit or quota left; "
            "wait a minute and run it again, and if it repeats check the billing and usage limits for the account."
        )
    if status is not None and status >= 500:
        return f"The service had a problem on its own side (a {status} error); nothing is wrong with your setup, so try again in a few minutes."
    if status is not None:
        return f"The service answered with an unexpected {status} error: {text}."
    return f"The call failed with {type(error).__name__}: {text}"


# -------------------------------------------------------------------- run


def run_ping(
    provider: str,
    model: str,
    build_caller: Callable[[int], object],
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda text: print(text, file=sys.stderr),
    clock: Callable[[], float] = time.perf_counter,
) -> int:
    """Make one tiny call and report. `build_caller(max_tokens)` gives the pipeline's caller for `provider`
    (the CLI passes `make_caller`); it is only asked for after the key check, so no key means no call."""
    if provider == "gemini":
        present, name, help_text = gemini_key_set(), "GEMINI_API_KEY (or GOOGLE_API_KEY)", GEMINI_KEY_HELP
    else:
        present, name, help_text = api_key_set(), "ANTHROPIC_API_KEY", CLAUDE_KEY_HELP
    if not present:
        err(f"ping: {name} is not set, so no call was made; set it first: {help_text}.")
        return 2

    try:
        caller = build_caller(PING_MAX_TOKENS)
    except Exception as error:  # a caller that cannot even be built, before any request
        err(f"ping: {explain_error(provider, model, error)}")
        return 1
    if caller is None:
        err(f"ping: {name} is not set, so no call was made; set it first: {help_text}.")
        return 2
    model = getattr(caller, "model", model)

    started = clock()
    try:
        parsed, usage = caller(PING_SYSTEM, [{"type": "text", "text": PING_PROMPT}], Ping)
    except Exception as error:
        err(f"ping: {provider} / {model} failed after {clock() - started:.1f} s.")
        err(f"ping: {explain_error(provider, model, error)}")
        return 1
    seconds = clock() - started

    request_id = getattr(caller, "last_request_id", None)
    out(f"ping: {provider} / {model}")
    out(f"  request id   {scrub(str(request_id)) if request_id else 'not reported by the service'}")
    out(
        f"  tokens       {usage.get('input_tokens', 0)} in (what specto sent), "
        f"{usage.get('output_tokens', 0)} out (the answer, including any thinking)"
    )
    out(f"  cost         {format_cents(cost_cents(model, usage), model)}")
    out(f"  time         {seconds:.1f} s")
    out(f"  answer       ok={parsed.ok}, word={parsed.word!r}")
    out("ready: one real call went through the same code a run uses, so specto run can use this key.")
    return 0
