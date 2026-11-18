import asyncio
import time
from typing import Optional

import aiohttp

from config import GEMINI_API_KEY, GEMINI_MODEL, AI_EXTRA_PROMPT, AI_TIMEOUT

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = (
    "You are an automated assistant inside a Telegram contact bot's DM. "
    "The human owner of the bot reads every user message and replies personally when needed. "
    "Keep answers short and clear (2-4 sentences unless more detail is genuinely needed). "
    "Reply in whichever language/style (English, Hindi, or Hinglish) the user wrote in. "
    "Never invent links, release dates, prices or promises. If you don't know something, "
    "or the user needs the owner (requests, promotions, complaints), say their message has "
    "been forwarded to the owner."
)
if AI_EXTRA_PROMPT:
    SYSTEM_PROMPT += " " + AI_EXTRA_PROMPT

# Thinking tokens count against maxOutputTokens, so keep thinking minimal and the
# limit comfortable; otherwise replies can come back empty or cut off.
MAX_OUTPUT_TOKENS = 1024
# Tried in order if GEMINI_MODEL doesn't exist for this key / API version.
FALLBACK_MODELS = ["gemini-2.5-flash"]

_session: Optional[aiohttp.ClientSession] = None

# What the last call looked like — shown by /aitest so problems are visible without server logs.
_state = {"model": GEMINI_MODEL, "thinking": True, "last_error": None, "last_ok": None, "last_seconds": None}


def is_enabled() -> bool:
    return bool(GEMINI_API_KEY)


def status() -> dict:
    return {"configured": is_enabled(), "model": _state["model"], "thinking": _state["thinking"],
            "last_error": _state["last_error"], "last_seconds": _state["last_seconds"]}


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session():
    if _session is not None and not _session.closed:
        await _session.close()


def _thinking_config(model: str) -> Optional[dict]:
    model = model.lower()
    if model.startswith("gemini-3"):
        # "minimal" isn't available on Pro models; "low" is their lowest level.
        return {"thinkingLevel": "low" if "pro" in model else "minimal"}
    if "2.5-flash" in model:
        return {"thinkingBudget": 0}
    return None  # unknown/older model: leave defaults alone


def _error_text(status_code: int, data) -> str:
    err = data.get("error", {}) if isinstance(data, dict) else {}
    return f"HTTP {status_code}: {err.get('status', '')} {err.get('message', data)}".strip()[:400]


def _extract_text(data: dict) -> tuple:
    """(text, why_not) — why_not explains an empty answer."""
    candidates = data.get("candidates") or []
    if not candidates:
        return None, f"no answer (blocked? {data.get('promptFeedback')})"
    parts = candidates[0].get("content", {}).get("parts", [])
    # Skip any "thought" parts, keep only the visible answer.
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        return None, f"empty answer (finishReason={candidates[0].get('finishReason')})"
    return text, None


async def ask_gemini(history: list, user_message: str) -> Optional[str]:
    """
    history: list of {"role": "user"/"model", "text": "..."} from database.get_chat_history,
    oldest turn first.

    Returns the AI's reply text, or None if AI is disabled (no API key) or the request fails for
    any reason (the reason is kept in status()["last_error"]) — callers fall back to normal bot
    behaviour (the plain "Message sent!" confirmation) when this is None.

    Built to survive the usual problems: a rejected thinking setting is dropped, an unknown model
    falls back to FALLBACK_MODELS, and busy/overloaded responses (429/5xx) are retried briefly —
    all inside the AI_TIMEOUT budget so a user is never kept waiting.
    """
    if not GEMINI_API_KEY:
        return None

    contents = [{"role": t["role"], "parts": [{"text": t["text"]}]} for t in history]
    contents.append({"role": "user", "parts": [{"text": user_message}]})
    headers = {"x-goog-api-key": GEMINI_API_KEY}  # header, not URL: can't leak through logged exceptions

    started = time.monotonic()
    deadline = started + AI_TIMEOUT
    call_error = None  # the most recent real error of THIS call (kept if the time budget runs out afterwards)
    models = [_state["model"]] + [m for m in FALLBACK_MODELS if m != _state["model"]]

    for model in models:
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                _state["last_error"] = call_error or f"timeout: no answer within {AI_TIMEOUT}s"
                print(f"[ai] gave up: {_state['last_error']}")
                return None

            generation_config = {"maxOutputTokens": MAX_OUTPUT_TOKENS}
            thinking = _thinking_config(model) if _state["thinking"] else None
            if thinking:
                generation_config["thinkingConfig"] = thinking
            payload = {
                "contents": contents,
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "generationConfig": generation_config,
            }

            try:
                async with _get_session().post(
                    GEMINI_URL.format(model=model), json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=remaining)
                ) as resp:
                    status_code = resp.status
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {"error": {"message": (await resp.text())[:200]}}
            except asyncio.TimeoutError:
                _state["last_error"] = f"timeout: no answer within {AI_TIMEOUT}s ({model})"
                print(f"[ai] {_state['last_error']}")
                return None
            except aiohttp.ClientError as e:
                _state["last_error"] = f"network error: {type(e).__name__}: {e}"
                print(f"[ai] {_state['last_error']}")
                return None

            if status_code == 200:
                text, why = _extract_text(data)
                if text:
                    _state.update(model=model, last_error=None, last_ok=time.time(), last_seconds=round(time.monotonic() - started, 1))
                    return text
                _state["last_error"] = why
                print(f"[ai] {why}")
                return None

            error = _error_text(status_code, data)
            call_error = _state["last_error"] = f"{model}: {error}"
            print(f"[ai] {_state['last_error']}")

            if status_code == 400 and thinking is not None:
                _state["thinking"] = False        # this model/API rejects the thinking setting: stop sending it
                continue
            if (status_code in (500, 502, 503, 504) and attempt < 2) or (status_code == 429 and attempt == 0):
                await asyncio.sleep(1 + attempt)  # busy / rate limited: short pause, try again
                continue
            if status_code in (400, 404):
                break                             # model not usable here -> next model
            return None                           # 401/403 (bad key), exhausted quota, ... retrying won't help
    return None
