from typing import Optional

import aiohttp

from config import GEMINI_API_KEY, GEMINI_MODEL

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

SYSTEM_PROMPT = (
    "You are a helpful assistant replying to users in a Telegram contact bot's DM. "
    "Keep answers short and clear (2-4 sentences unless more detail is genuinely needed). "
    "Reply in whichever language/style (English, Hindi, or Hinglish) the user wrote in."
)

MAX_OUTPUT_TOKENS = 400
REQUEST_TIMEOUT = 25  # seconds


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

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS, "temperature": 0.7},
    }
    url = GEMINI_URL.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    print(f"Gemini API error {resp.status}: {data}")
                    return None
                candidates = data.get("candidates") or []
                if not candidates:
                    return None
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts).strip()
                return text or None
    except Exception as e:
        print(f"Gemini request failed: {e}")
        return None
