from typing import Optional

import aiohttp

from config import GEMINI_API_KEY, GEMINI_MODEL, AI_EXTRA_PROMPT

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
REQUEST_TIMEOUT = 25  # seconds

_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
    return _session


async def close_session():
    if _session is not None and not _session.closed:
        await _session.close()


def _thinking_config() -> Optional[dict]:
    model = GEMINI_MODEL.lower()
    if model.startswith("gemini-3"):
        # "minimal" isn't available on Pro models; "low" is their lowest level.
        return {"thinkingLevel": "low" if "pro" in model else "minimal"}
    if "2.5-flash" in model:
        return {"thinkingBudget": 0}
    return None  # unknown/older model: leave defaults alone


async def ask_gemini(history: list, user_message: str) -> Optional[str]:
    """
    history: list of {"role": "user"/"model", "text": "..."} from database.get_chat_history,
    oldest turn first.

    Returns the AI's reply text, or None if AI is disabled (no API key) or the
    request fails for any reason — callers should fall back to normal bot
    behaviour (e.g. the plain "Message sent!" confirmation) when this is None.
    """
    if not GEMINI_API_KEY:
        return None

    contents = [{"role": turn["role"], "parts": [{"text": turn["text"]}]} for turn in history]
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    generation_config = {"maxOutputTokens": MAX_OUTPUT_TOKENS}
    thinking = _thinking_config()
    if thinking:
        generation_config["thinkingConfig"] = thinking

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": generation_config,
    }
    url = GEMINI_URL.format(model=GEMINI_MODEL)
    # Key goes in a header (not the URL) so it can't leak through logged exceptions.
    headers = {"x-goog-api-key": GEMINI_API_KEY}

    try:
        async with _get_session().post(url, json=payload, headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200:
                print(f"Gemini API error {resp.status}: {data}")
                return None
            candidates = data.get("candidates") or []
            if not candidates:
                print(f"Gemini returned no candidates: {data.get('promptFeedback')}")
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            # Skip any "thought" parts, keep only the visible answer.
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            return text or None
    except Exception as e:
        print(f"Gemini request failed: {type(e).__name__}: {e}")
        return None
