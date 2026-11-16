import os


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _required_int(name: str) -> int:
    value = _required(name)
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be a number, got: {value!r}")


API_ID = _required_int("API_ID")
API_HASH = _required("API_HASH")
BOT_TOKEN = _required("BOT_TOKEN")
ADMIN = _required_int("ADMIN")

DATABASE_URI = _required("DATABASE_URI")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "MRNContactBot")

# AI assistant (Google Gemini). Leave GEMINI_API_KEY empty to disable AI
# replies — the bot then behaves exactly as before (just forwards to admin).
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
# Optional extra instructions for the AI (e.g. what your channel/bot is about).
AI_EXTRA_PROMPT = os.environ.get("AI_EXTRA_PROMPT", "")

# Minimum seconds between two bot-generated (AI / indexed-content) replies to
# the same user. Messages are still forwarded to the admin during cooldown.
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "5"))
