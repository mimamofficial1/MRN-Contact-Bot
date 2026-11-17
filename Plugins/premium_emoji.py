# Premium (animated custom) emoji for the WHOLE bot - in messages, captions AND buttons.
# (Ported from the MRN file-store bot to the MRN Contact Bot: kurigram's native button classes, MongoDB settings.)
# This one file does everything; no other file is edited and no env variable / STRING_SESSION is needed.
#
# How it works
#   Every message / caption / edit the bot sends goes through Client.invoke(). This plugin hooks
#   there. In TEXT it finds the normal emoji (✅ 📁 ⚡ ...) that you mapped and adds a custom-emoji
#   entity, so users see your premium emoji. On BUTTONS it takes the first mapped emoji out of the
#   button label and shows it as the button's premium icon (Bot API 9.4 "icon_custom_emoji_id").
#   All existing texts / buttons in all files keep working unchanged.
#
# Telegram rule: premium emoji work in private, group and supergroup chats when the BOT'S OWNER
# (the account that created the bot in @BotFather) has Telegram Premium. Not supported by Telegram:
# inline mode (and channels may refuse - those chats are then skipped automatically; LOG_CHANNEL
# is always skipped).
#
# NOTHING TO SET UP. The bot learns YOUR style by itself:
#   * every post you make in a channel where the bot is a member/admin is read; the premium emoji in it
#     (and the whole emoji packs they come from) become the bot's emoji too,
#   * on every start it also reads the last ~200 posts of the channels it knows (auto-button channels,
#     force-sub channels) so it has your style from the first minute,
#   * any message / forwarded post with premium emoji that an ADMIN sends to the bot in private is learned too.
# Emoji nobody could be matched with fall back to Telegram's own animated pack (DEFAULT_PACKS below).
# Text emoji + button icons switch ON by themselves.
# Telegram limit: premium BUTTON icons are shown only in private/group/supergroup chats. Buttons on channel
# posts keep their normal emoji (a bot needs an extra username bought on Fragment for more - then set PREMIUM_CHANNEL_BUTTONS=1).
#
# Optional: /emojipack <pack link>, /setemoji + premium emoji (hand-mapping, highest priority),
#           /emoji (status), /emojioff, /emojion, /emojibtn on|off, /emojilist, /delemoji <emoji or all>
#
# Safety: if Telegram ever refuses a message because of the emoji / icons (e.g. the owner's Premium
# expired), the message is re-sent plain at once, and after 3 such failures in a row that feature
# switches itself off. Nothing breaks.

import asyncio
import logging
import os
import re
import time
from io import BytesIO

from pyrogram import Client, enums, filters, raw
from pyrogram.errors import FloodWait, RPCError
from pyrogram.raw.core import TLObject
from pyrogram.raw.core.primitives import Bytes, Int, Long, String, Vector
from pyrogram.types import Message
from pyrogram.utils import get_channel_id

from database import admin_filter, get_settings, update_setting, indexed_channels_col, get_force_channels

# Optional: a log channel that must never receive premium emoji (the contact bot has none by default).
LOG_CHANNEL = os.environ.get("LOG_CHANNEL", "")

logger = logging.getLogger(__name__)

# Public emoji packs (t.me/addemoji/<name>) used automatically. Earlier = higher priority. Packs that
# cannot be loaded are skipped; more can be added with /emojipack or the PREMIUM_EMOJI_PACKS env var.
# Any emoji the bot's code uses that none of the packs has is looked up with Telegram's own emoji search.
DEFAULT_PACKS = [
    "RestrictedEmoji",     # Telegram's animated "Telemoji": colorful animated versions of the standard emoji
    "BasicinterfaceEmoji", # colorful animated icons - fills the gaps
    "AnimatedIconsEmoji",
]

MAX_STYLE_PACKS = 12
BACKFILL_POSTS = 200
MAX_PER_MESSAGE = 80
MAX_MAPPINGS = 300
AUTO_OFF_AFTER = 3
ID_STRIKES = 3     # an emoji id that failed this many times and never once worked is dropped
_FE0F = "\ufe0f"

# errors that have nothing to do with emoji: never retried
_UNRELATED = {
    "MESSAGE_NOT_MODIFIED", "PEER_ID_INVALID", "INPUT_USER_DEACTIVATED", "USER_IS_BLOCKED",
    "CHAT_WRITE_FORBIDDEN", "MESSAGE_ID_INVALID", "CHAT_ID_INVALID", "CHANNEL_INVALID",
    "USER_DEACTIVATED", "MESSAGE_DELETE_FORBIDDEN", "REPLY_MARKUP_INVALID", "BUTTON_URL_INVALID",
    "BUTTON_DATA_INVALID", "MEDIA_EMPTY", "MESSAGE_EMPTY", "PEER_FLOOD", "BOT_RESPONSE_TIMEOUT",
    "USER_PRIVACY_RESTRICTED", "CHAT_ADMIN_REQUIRED", "CHANNEL_PRIVATE", "MESSAGE_TOO_LONG",
}

_state = {
    "loaded": False, "on": True, "btn_on": True, "map": {}, "rx": None,
    "txt_fails": 0, "btn_fails": 0,
    "manual": {},      # emoji you mapped by hand (/setemoji)  - highest priority
    "auto": {},        # emoji found in the packs
    "masked": set(),   # emoji you removed with /delemoji
    "learned": {},     # char -> {emoji id: times seen} read from YOUR posts
    "style_packs": [], # [id, access_hash] of the emoji packs your posts use
    "style": {},       # char -> id from those whole packs
    "sources": {},     # channel id -> newest post id read
    "broadcast": set(),# broadcast channels (peer ids): Telegram shows NO premium button icons there
    "packs": [],       # extra packs added with /emojipack
    "packs_ok": {},    # pack name -> how many emoji it gave
    "packs_started": False,
}
_BG = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)
    return t
_skip_channels = set()   # channels/supergroups that refused premium emoji once -> never tried again

try:
    _lc = int(LOG_CHANNEL)
    _log_channel_id = get_channel_id(_lc) if _lc < -1000000000000 else None
except Exception:
    _log_channel_id = None


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _s64(v: int) -> int:
    """Telegram ids are signed 64-bit on the wire."""
    v = int(v)
    return v - (1 << 64) if v >= (1 << 63) else v


def _rebuild():
    merged = dict(_state["auto"])
    merged.update(_state["style"])
    for ch, counts in _state["learned"].items():
        if counts:
            merged[ch] = max(counts.items(), key=lambda kv: kv[1])[0]
    merged = {k: v for k, v in merged.items() if k not in _state["masked"]}
    merged.update(_state["manual"])
    _state["map"] = merged
    keys = sorted(_state["map"], key=len, reverse=True)
    if not keys:
        _state["rx"] = None
        return
    # never cut a ZWJ sequence (👨‍💻) or a skin-tone emoji in half
    pat = "|".join("(?<!\u200d)" + re.escape(k) + _FE0F + "?(?![\u200d\U0001F3FB-\U0001F3FF])" for k in keys)
    _state["rx"] = re.compile(pat)


async def _load_state():
    if time.monotonic() < _state.get("retry_at", 0):
        return
    try:
        st = await get_settings()
    except Exception:
        _state["retry_at"] = time.monotonic() + 30   # database not ready: don't hammer it, retry in 30s
        return
    # ON by default; only an explicit /emojioff, /emojibtn off or an automatic safety switch-off turns them off
    on, btn = st.get("premium_emoji"), st.get("premium_btn")
    _state["on"] = True if on is None else bool(on)
    _state["btn_on"] = True if btn is None else bool(btn)
    manual = {}
    for pair in st.get("premium_emoji_map") or []:
        try:
            manual[str(pair[0])] = _s64(pair[1])
        except Exception:
            continue
    _state["manual"] = manual
    _state["masked"] = set(st.get("premium_emoji_mask") or [])
    learned = {}
    for row in st.get("premium_emoji_learned") or []:
        try:
            learned.setdefault(str(row[0]), {})[_s64(row[1])] = int(row[2])
        except Exception:
            continue
    _state["learned"] = learned
    _state["style_packs"] = [[str(a), str(b)] for a, b in (st.get("premium_emoji_style_packs") or [])]
    _state["sources"] = {int(c): int(l) for c, l in (st.get("premium_emoji_sources") or [])}
    _state["packs"] = [str(x) for x in (st.get("premium_emoji_packs") or [])]
    _rebuild()
    _state["loaded"] = True


def _pack_map(res):
    out = {}
    for doc in getattr(res, "documents", None) or []:
        for attr in doc.attributes:
            if isinstance(attr, raw.types.DocumentAttributeCustomEmoji) and attr.alt:
                out.setdefault(attr.alt.replace(_FE0F, "").strip(), _s64(doc.id))
    return out


async def _fetch_pack(client, name):
    """{normal emoji: custom emoji id} for one public emoji pack (empty if it cannot be loaded)."""
    res = await client.invoke(
        raw.functions.messages.GetStickerSet(stickerset=raw.types.InputStickerSetShortName(short_name=name), hash=0),
        retries=2, timeout=20,
    )
    return _pack_map(res)


async def _fetch_pack_id(client, set_id, access_hash):
    res = await client.invoke(
        raw.functions.messages.GetStickerSet(
            stickerset=raw.types.InputStickerSetID(id=int(set_id), access_hash=int(access_hash)), hash=0),
        retries=2, timeout=20,
    )
    return _pack_map(res)


# ---------------------------------------------------------------- learning YOUR style from your posts
def _pairs_from(m):
    """[(normal emoji, custom emoji id)] for every premium emoji inside a message / caption."""
    text = getattr(m, "text", None) or getattr(m, "caption", None) or ""
    ents = getattr(m, "entities", None) or getattr(m, "caption_entities", None) or []
    u = str(text).encode("utf-16-le")
    out = []
    for e in ents:
        if e.type != enums.MessageEntityType.CUSTOM_EMOJI or not e.custom_emoji_id:
            continue
        ch = u[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le", "ignore").replace(_FE0F, "").strip()
        if ch:
            out.append((ch, _s64(e.custom_emoji_id)))
    return out


async def _save_style():
    await update_setting("premium_emoji_learned",
                         [[k, str(i), n] for k, d in _state["learned"].items() for i, n in d.items()])
    await update_setting("premium_emoji_style_packs", _state["style_packs"])
    await update_setting("premium_emoji_sources", [[c, l] for c, l in _state["sources"].items()])


async def _expand_style_packs(client, ids):
    """The emoji ids found in your posts belong to packs; load those WHOLE packs so every emoji of your
    style is available, not just the ones you happened to use."""
    if len(_state["style_packs"]) >= MAX_STYLE_PACKS or not ids:
        return
    ids, docs = sorted(ids), []
    try:
        for i in range(0, len(ids), 100):
            docs += list(await client.invoke(raw.functions.messages.GetCustomEmojiDocuments(document_id=ids[i:i + 100]),
                                             retries=1, timeout=20))
    except Exception as e:
        logger.info(f"[premium_emoji] could not look up your emoji packs ({type(e).__name__})")
        return
    have = {(int(a), int(b)) for a, b in _state["style_packs"]}
    for d in docs:
        if not isinstance(d, raw.types.Document):
            continue
        for a in d.attributes:
            if not (isinstance(a, raw.types.DocumentAttributeCustomEmoji)
                    and isinstance(a.stickerset, raw.types.InputStickerSetID)):
                continue
            key = (a.stickerset.id, a.stickerset.access_hash)
            if key in have or len(_state["style_packs"]) >= MAX_STYLE_PACKS:
                continue
            try:
                pack = await _fetch_pack_id(client, *key)
            except Exception as e:
                logger.info(f"[premium_emoji] one of your packs could not be loaded ({type(e).__name__})")
                continue
            have.add(key)
            _state["style_packs"].append([str(key[0]), str(key[1])])
            for k, v in pack.items():
                _state["style"].setdefault(k, v)
            logger.info(f"[premium_emoji] learned one of your emoji packs ({len(pack)} emoji)")


async def _learn_pairs(client, pairs, chat=None, last=None):
    """Remember these premium emoji as YOUR style. Returns how many emoji ids were new."""
    new_ids, changed = set(), False
    for ch, did in pairs:
        ch = ch.replace(_FE0F, "").strip()
        if not ch or _NOT_REAL_EMOJI.match(ch):
            continue
        d = _state["learned"].setdefault(ch, {})
        if did not in d:
            new_ids.add(did)
        d[did] = d.get(did, 0) + 1
        changed = True
    if chat is not None and last:
        _state["sources"][chat] = max(_state["sources"].get(chat, 0), last)
    if not changed:
        return 0
    _rebuild()
    await _expand_style_packs(client, new_ids)
    _rebuild()
    try:
        await _save_style()
    except Exception:
        pass
    return len(new_ids)


async def _exists(client, chat, hi, size=50):
    msgs = await client.get_messages(chat, list(range(max(1, hi - size + 1), hi + 1)))
    return [m.id for m in msgs if not m.empty]


async def _top_id(client, chat):
    """Roughly the newest post id of a channel (bots cannot list history, but can ask for ids)."""
    n = 50
    if not await _exists(client, chat, n):
        return 0
    while n < (1 << 24):          # double until a stretch of 50 ids is completely empty
        n *= 2
        if not await _exists(client, chat, n):
            break
    lo, hi = n // 2, n            # ids just below `lo` exist, the stretch ending at `hi` is empty
    while hi - lo > 50:
        mid = (lo + hi) // 2
        if await _exists(client, chat, mid):
            lo = mid
        else:
            hi = mid
    return max(await _exists(client, chat, lo, size=hi - lo + 50) or [0])


async def _known_channels():
    ids = set(_state["sources"])
    try:                                          # channels registered through the indexer
        ids |= {doc["chat_id"] async for doc in indexed_channels_col.find({})}
    except Exception:
        pass
    try:                                          # force-join channels that were saved as numeric IDs
        for e in await get_force_channels():
            if str(e).lstrip("-").isdigit():
                ids.add(int(e))
    except Exception:
        pass
    ids = {c for c in ids if isinstance(c, int) and c < 0 and c != _log_channel_id_full()}
    return ids


def _log_channel_id_full():
    try:
        return int(LOG_CHANNEL)
    except Exception:
        return None


async def _backfill(client):
    """Read the last posts of the channels the bot knows, so your style is learned right at the start."""
    total = 0
    for chat in sorted(await _known_channels()):
        try:
            top = await _top_id(client, chat)
            if not top:
                continue
            pairs = []
            for hi in range(top, max(top - BACKFILL_POSTS, 0), -100):
                msgs = await client.get_messages(chat, list(range(max(1, hi - 99), hi + 1)))
                for m in msgs:
                    if not m.empty:
                        pairs += _pairs_from(m)
            if pairs:
                total += await _learn_pairs(client, pairs, chat, top)
            else:
                _state["sources"][chat] = max(_state["sources"].get(chat, 0), top)
        except Exception as e:
            logger.info(f"[premium_emoji] could not read channel {chat}: {type(e).__name__}")
    if total:
        logger.info(f"[premium_emoji] learned {total} emoji from your channel posts "
                    f"({len(_state['style_packs'])} of your packs, {len(_state['style'])} emoji in them)")


_EMOJI_RX = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\u2190-\u21FF\u25A0-\u25FF\U0001F000-\U0001F2FF]")


# typography the bot uses that merely lives in the same Unicode blocks: arrows, tick, skin-tone modifiers ...
_NOT_REAL_EMOJI = re.compile("[\u2190-\u21FF\u2713\u276E\u2642\u2640\u263A\U0001F3FB-\U0001F3FF\u200d]")


def _bot_emoji():
    """Every emoji character that appears in the bot's own source files."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    found = set()
    for base, _dirs, files in os.walk(root):
        if any(x in base for x in (".git", "__pycache__", "node_modules")):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            try:
                with open(os.path.join(base, fn), encoding="utf-8", errors="ignore") as fh:
                    found.update(_EMOJI_RX.findall(fh.read()))
            except OSError:
                pass
    return found


async def _search_missing(client, auto):
    """For emoji the bot uses but no pack has: ask Telegram's emoji search for a matching premium
    emoji. Returns (found, missing, refused) - refused=True if Telegram does not let this account search."""
    missing = sorted(e for e in _bot_emoji() if e not in auto and not _NOT_REAL_EMOJI.match(e))
    found = 0
    for ch in missing:
        try:
            res = await client.invoke(raw.functions.messages.SearchCustomEmoji(emoticon=ch, hash=0), retries=1, timeout=15)
        except Exception as e:
            logger.info(f"[premium_emoji] emoji search not available for this account ({type(e).__name__})")
            return found, missing, True
        ids = getattr(res, "document_id", None) or []
        if ids:
            auto.setdefault(ch, _s64(ids[0]))
            found += 1
    return found, missing, False


async def _valid_ids(client, ids):
    """The subset of `ids` that Telegram confirms are real custom emoji (None if the check is not possible)."""
    ids = sorted(set(ids))
    valid = set()
    try:
        for i in range(0, len(ids), 100):
            res = await client.invoke(raw.functions.messages.GetCustomEmojiDocuments(document_id=ids[i:i + 100]),
                                      retries=1, timeout=20)
            for d in res:
                if isinstance(d, raw.types.Document) and any(
                        isinstance(a, raw.types.DocumentAttributeCustomEmoji) for a in d.attributes):
                    valid.add(d.id)
    except Exception as e:
        logger.info(f"[premium_emoji] could not validate emoji ids ({type(e).__name__}); using them as they are")
        return None
    return valid


_user = {"client": None, "tried": False}


async def _user_client():
    """OPTIONAL: a user session (env STRING_SESSION) used ONLY to look up emoji ids at startup
    (read-only, never sends anything). Helps when Telegram does not let the bot search/load them."""
    sess = os.environ.get("STRING_SESSION", "").strip()
    if not sess or _user["tried"]:
        return _user["client"]
    _user["tried"] = True
    try:
        from config import API_HASH, API_ID
        c = Client("emoji_lookup", api_id=API_ID, api_hash=API_HASH, session_string=sess,
                   in_memory=True, no_updates=True)
        await c.start()
        _user["client"] = c
        logger.info("[premium_emoji] user session started for emoji lookup")
    except Exception as e:
        logger.warning(f"[premium_emoji] STRING_SESSION could not be used: {type(e).__name__}: {e}")
    return _user["client"]


async def _autoload_packs(client):
    """Load the packs from Telegram and merge them (earlier pack wins). Runs once per start."""
    if _state["packs_started"]:
        return
    _state["packs_started"] = True
    names = list(DEFAULT_PACKS) + [n for n in os.environ.get("PREMIUM_EMOJI_PACKS", "").split() if n]
    await _load_state() if not _state["loaded"] else None
    names += [n for n in _state["packs"] if n not in names]
    auto, ok, failed = {}, {}, []
    for name in names:
        try:
            got = await _fetch_pack(client, name)
        except Exception as e:
            failed.append(name)
            logger.warning(f"[premium_emoji] pack '{name}' could not be loaded: {type(e).__name__}: {e}")
            continue
        ok[name] = len(got)
        for k, v in got.items():
            auto.setdefault(k, v)
    style = {}
    for sid, ah in _state["style_packs"]:                # your own packs, remembered from earlier posts
        try:
            for k, v in (await _fetch_pack_id(client, sid, ah)).items():
                style.setdefault(k, v)
        except Exception as e:
            logger.info(f"[premium_emoji] saved style pack could not be loaded ({type(e).__name__})")
    _state["style"] = style
    user = None
    search = "ok"
    try:
        if failed:                                   # packs the bot could not load -> optional user session
            user = await _user_client()
            for name in list(failed) if user else []:
                try:
                    got = await _fetch_pack(user, name)
                except Exception:
                    continue
                failed.remove(name)
                ok[name] = len(got)
                for k, v in got.items():
                    auto.setdefault(k, v)
        try:
            _found, missing, refused = await _search_missing(client, auto)
            if refused:
                search = "refused"
                user = user or await _user_client()
                if user:
                    _found, missing, refused = await _search_missing(user, auto)
                    search = "ok (user session)" if not refused else "refused"
        except Exception as e:
            missing, search = [], f"error {type(e).__name__}"
        valid = await _valid_ids(client, auto.values())
        if valid is None and user:
            valid = await _valid_ids(user, auto.values())
        invalid = 0
        # only trust the check if it confirms at least half of the ids; an empty / near-empty answer
        # means the check itself is unreliable, and then nothing is thrown away
        if valid is not None and len(valid) >= max(1, len(set(auto.values())) // 2):
            for k in [k for k, v in auto.items() if v not in valid]:
                del auto[k]
                invalid += 1
        elif valid is not None:
            logger.info(f"[premium_emoji] id check confirmed only {len(valid)} of {len(set(auto.values()))} ids; "
                        "ignoring the check")
    finally:
        if user is not None:
            try:
                await user.stop()
            except Exception:
                pass
    _state["auto"], _state["packs_ok"] = auto, ok
    _rebuild()
    real = {e for e in _bot_emoji() if not _NOT_REAL_EMOJI.match(e)}
    still = sorted(e for e in real if e not in _state["map"])
    _state["report"] = {"packs": ok, "failed": failed, "total": len(real), "covered": len(real) - len(still),
                        "missing": still, "invalid": invalid, "search": search}
    logger.info(f"[premium_emoji] {len(real) - len(still)}/{len(real)} of the bot's emoji have a premium version "
                f"(packs: {', '.join(f'{n}:{c}' for n, c in ok.items()) or 'none'}; failed packs: {failed or 'none'}; "
                f"invalid ids dropped: {invalid}; emoji search: {search}); "
                f"text={'ON' if _state['on'] else 'OFF'} buttons={'ON' if _state['btn_on'] else 'OFF'}")
    if still:
        logger.info(f"[premium_emoji] no premium version found for: {' '.join(still)}")
    _spawn(_backfill_then_report(client))


async def _backfill_then_report(client):
    try:
        await _backfill(client)
    except Exception as e:
        logger.warning(f"[premium_emoji] reading your channels failed: {type(e).__name__}: {e}")
    real = {e for e in _bot_emoji() if not _NOT_REAL_EMOJI.match(e)}
    still = sorted(e for e in real if e not in _state["map"])
    if _state.get("report"):
        _state["report"].update(covered=len(real) - len(still), missing=still, total=len(real))
    logger.info(f"[premium_emoji] after learning your style: {len(real) - len(still)}/{len(real)} emoji covered")


def _report_text():
    r = _state.get("report")
    if not r:
        return "Premium emoji: abhi load ho raha hai..."
    lines = [
        f"<b>📊 Coverage:</b> {r['covered']}/{r['total']} emoji premium ho sakte hain",
        "<b>📦 Packs:</b> " + (", ".join(f"{n} ({c})" for n, c in r["packs"].items()) or "koi nahi"),
    ]
    if r["failed"]:
        lines.append("<b>❌ Load nahi hue:</b> " + ", ".join(r["failed"]))
    lines.append(f"<b>🎨 Tumhara style:</b> {len(_state['learned'])} emoji posts se seekhe, "
                 f"{len(_state['style_packs'])} packs ({len(_state['style'])} emoji)")
    lines.append(f"<b>🔎 Emoji search:</b> {r['search']}")
    if r["invalid"]:
        lines.append(f"<b>🗑 Galat IDs hataye:</b> {r['invalid']}")
    if r["missing"]:
        lines.append("<b>Normal rahenge:</b> " + " ".join(r["missing"]))
    return "\n".join(lines)


async def _notify_admin(client):
    """If something is missing, tell the first admin ONCE (at most once an hour) - so nobody has to dig
    through logs. Silent when everything is covered."""
    r = _state.get("report")
    if not r or not (r["missing"] or r["failed"] or r["invalid"]):
        return
    try:
        from config import ADMIN as admin
        st = await get_settings()
        if time.time() - float(st.get("premium_emoji_report_ts") or 0) < 3600:
            return
        await update_setting("premium_emoji_report_ts", time.time())
        await client.send_message(
            admin,
            "<b>✅ Premium emoji status</b>\n\n" + _report_text() +
            f"\n\n<b>Text:</b> {'ON' if _state['on'] else 'OFF'} · <b>Buttons:</b> {'ON' if _state['btn_on'] else 'OFF'}",
        )
    except Exception as e:
        logger.info(f"[premium_emoji] status message not sent: {type(e).__name__}")


def start_premium(client):
    """Called by bot.py once the bot is connected: loads packs + learns your style in the background.
    (No message is sent to anybody - see /emoji for the status.)"""
    _spawn(_startup(client))


async def _startup(client):
    try:
        await _load_state()
        await _autoload_packs(client)
        # Same admin status message as the file-store bot, but OFF by default so a restart stays quiet
        # (the contact bot should say only "online"). Set env PREMIUM_EMOJI_NOTIFY=1 to turn it on.
        if os.environ.get("PREMIUM_EMOJI_NOTIFY", "").strip() in ("1", "true", "yes"):
            await asyncio.sleep(25)              # let _backfill_then_report finish first
            await _notify_admin(client)
    except Exception:
        logger.error("[premium_emoji] startup failed", exc_info=True)


async def _save_map():
    await update_setting("premium_emoji_map", [[k, str(v)] for k, v in _state["manual"].items()])
    await update_setting("premium_emoji_mask", sorted(_state["masked"]))


# --------------------------------------------------------------------------- text
def _inject(text, entities):
    """Returns a new entity list with premium emoji added, or None if nothing to add."""
    rx = _state["rx"]
    if not text or rx is None:
        return None
    entities = list(entities or [])
    blocked = [
        (e.offset, e.offset + e.length)
        for e in entities
        if isinstance(e, (raw.types.MessageEntityCustomEmoji, raw.types.MessageEntityCode, raw.types.MessageEntityPre))
    ]
    mapping = _state["map"]
    added, pos16, last = [], 0, 0
    for m in rx.finditer(text):
        pos16 += _u16(text[last:m.start()])
        last = m.start()
        doc_id = mapping.get(m.group().replace(_FE0F, ""))
        length = _u16(m.group())
        if doc_id is None or any(a <= pos16 < b for a, b in blocked):
            continue
        added.append(raw.types.MessageEntityCustomEmoji(offset=pos16, length=length, document_id=doc_id))
        if len(added) >= MAX_PER_MESSAGE:
            break
    if not added:
        return None
    return sorted(entities + added, key=lambda e: e.offset)


# --------------------------------------------------------------------------- buttons
# kurigram (layer 233) already writes inline keyboards in the new format (KeyboardInlineButton with an
# optional KeyboardButtonStyle that carries the premium icon), so the native classes are used directly.
# pyrofork 2.3.69 (layer 220) predates that format: there the ORIGINAL hand-written classes below are used.
# The right path is picked automatically (_NATIVE_BUTTONS), so the same file works on both libraries.
_NATIVE_BUTTONS = hasattr(raw.types, "KeyboardInlineButton")

# Telegram's newest TL layer describes inline buttons in a NEW format that carries a "style"
# with the premium icon. pyrofork 2.3.69 (layer 220) predates it, so the few classes needed to
# WRITE such a keyboard are defined here. Layouts and ids are copied from the layer-229 schema
# and verified byte-for-byte against kurigram's generated classes. They are only ever written,
# never read: Telegram still answers in the old format because the session declares layer 220.
class _BtnStyle(TLObject):
    __slots__ = ["icon"]
    ID = 0x4FDD3430
    QUALNAME = "types.KeyboardButtonStyle"

    def __init__(self, *, icon=None):
        self.icon = icon

    def write(self, *args) -> bytes:
        b = BytesIO()
        b.write(Int(self.ID, False))
        b.write(Int((1 << 3) if self.icon is not None else 0))      # flags: icon
        if self.icon is not None:
            b.write(Long(self.icon))
        return b.getvalue()


class _BtnTypeUrl(TLObject):
    __slots__ = ["url"]
    ID = 0xECA4F8D4
    QUALNAME = "types.InlineButtonTypeUrl"

    def __init__(self, *, url):
        self.url = url

    def write(self, *args) -> bytes:
        b = BytesIO()
        b.write(Int(self.ID, False))
        b.write(String(self.url))
        return b.getvalue()


class _BtnTypeCallback(TLObject):
    __slots__ = ["data", "requires_password"]
    ID = 0x2955BC38
    QUALNAME = "types.InlineButtonTypeCallback"

    def __init__(self, *, data, requires_password=None):
        self.data = data
        self.requires_password = requires_password

    def write(self, *args) -> bytes:
        b = BytesIO()
        b.write(Int(self.ID, False))
        b.write(Int((1 << 0) if self.requires_password else 0))     # flags: requires_password
        b.write(Bytes(self.data))
        return b.getvalue()


class _Btn(TLObject):
    __slots__ = ["text", "type", "style"]
    ID = 0x11C1A322
    QUALNAME = "types.KeyboardInlineButton"

    def __init__(self, *, text, type, style=None):
        self.text = text
        self.type = type
        self.style = style

    def write(self, *args) -> bytes:
        b = BytesIO()
        b.write(Int(self.ID, False))
        b.write(Int((1 << 10) if self.style is not None else 0))    # flags: style
        if self.style is not None:
            b.write(self.style.write())
        b.write(String(self.text))
        b.write(self.type.write())
        return b.getvalue()


class _BtnRow(TLObject):
    __slots__ = ["buttons"]
    ID = 0x19420AF6
    QUALNAME = "types.KeyboardInlineButtonRow"

    def __init__(self, *, buttons):
        self.buttons = buttons

    def write(self, *args) -> bytes:
        b = BytesIO()
        b.write(Int(self.ID, False))
        b.write(Vector(self.buttons))
        return b.getvalue()


class _BtnMarkup(TLObject):
    __slots__ = ["rows", "force_reply"]
    ID = 0xB2B15770
    QUALNAME = "types.ReplyInlineMarkup"

    def __init__(self, *, rows, force_reply=None):
        self.rows = rows
        self.force_reply = force_reply

    def write(self, *args) -> bytes:
        b = BytesIO()
        b.write(Int(self.ID, False))
        b.write(Int((1 << 5) if self.force_reply else 0))           # flags: force_reply
        b.write(Vector(self.rows))
        return b.getvalue()


def _split_icon(label: str):
    """'🚀 FAST DOWNLOAD 🚀' -> ('FAST DOWNLOAD', icon_id) using the first mapped emoji."""
    rx = _state["rx"]
    if rx is None or not label:
        return label, None
    for m in rx.finditer(label):
        key = m.group().replace(_FE0F, "")
        doc_id = _state["map"].get(key)
        if doc_id is None:
            continue
        cleaned = re.sub(re.escape(key) + _FE0F + "?", "", label)   # the icon replaces every copy
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return (cleaned, doc_id) if cleaned else (label, None)
    return label, None


def _native_style_markup(markup):
    """Inline keyboard -> the same keyboard with premium icons, or None if there is nothing to change
    (or the keyboard holds something we do not convert - then it is sent untouched)."""
    if type(markup) is not raw.types.ReplyInlineMarkup:
        return None
    rows, any_icon = [], False
    for row in markup.rows:
        buttons = []
        for btn in row.buttons:
            if type(btn) is not raw.types.KeyboardInlineButton:
                return None
            text, style = btn.text, btn.style
            cleaned, icon = _split_icon(btn.text)
            if icon is not None and not (style is not None and style.icon is not None):
                any_icon = True
                text = cleaned
                style = raw.types.KeyboardButtonStyle(
                    bg_primary=getattr(style, "bg_primary", None), bg_danger=getattr(style, "bg_danger", None),
                    bg_success=getattr(style, "bg_success", None), icon=icon)
            buttons.append(raw.types.KeyboardInlineButton(text=text, type=btn.type, style=style))
        rows.append(raw.types.KeyboardInlineButtonRow(buttons=buttons))
    if not any_icon:
        return None
    return raw.types.ReplyInlineMarkup(rows=rows, force_reply=getattr(markup, "force_reply", None))


def _legacy_style_markup(markup):
    """Old-format inline keyboard -> new format with premium icons, or None if there is nothing to
    change / the keyboard holds a button kind we do not convert (then it is sent untouched)."""
    if type(markup) is not raw.types.ReplyInlineMarkup:
        return None
    rows, any_icon = [], False
    for row in markup.rows:
        buttons = []
        for btn in row.buttons:
            kind = type(btn)
            if kind is raw.types.KeyboardButtonUrl:
                typ = _BtnTypeUrl(url=btn.url)
            elif kind is raw.types.KeyboardButtonCallback:
                typ = _BtnTypeCallback(data=btn.data, requires_password=getattr(btn, "requires_password", None))
            else:
                return None
            text, icon = _split_icon(btn.text)
            any_icon = any_icon or icon is not None
            buttons.append(_Btn(text=text, type=typ, style=_BtnStyle(icon=icon) if icon is not None else None))
        rows.append(_BtnRow(buttons=buttons))
    if not any_icon:
        return None
    return _BtnMarkup(rows=rows, force_reply=getattr(markup, "force_reply", None))


def _style_markup(markup):
    return _native_style_markup(markup) if _NATIVE_BUTTONS else _legacy_style_markup(markup)


def _test_markup(doc_id):
    """One test button with a premium icon, in whichever format this library uses."""
    if _NATIVE_BUTTONS:
        return raw.types.ReplyInlineMarkup(rows=[raw.types.KeyboardInlineButtonRow(buttons=[raw.types.KeyboardInlineButton(
            text="Button test", type=raw.types.InlineButtonTypeCallback(data=b"premium_btn_test"),
            style=raw.types.KeyboardButtonStyle(icon=doc_id))])])
    return _BtnMarkup(rows=[_BtnRow(buttons=[_Btn(
        text="Button test", type=_BtnTypeCallback(data=b"premium_btn_test"), style=_BtnStyle(icon=doc_id),
    )])])


# --------------------------------------------------------------------------- the hook
def _channel_post(peer, query) -> bool:
    """True for posts in a broadcast channel. Telegram only shows premium BUTTON icons for bots in private,
    group and supergroup chats (or for bots that bought an extra username on Fragment); in a channel it
    silently ignores the icon - and the emoji we took out of the label would simply be gone. So buttons of
    channel posts keep their normal emoji. (Set env PREMIUM_CHANNEL_BUTTONS=1 if the bot has a Fragment username.)"""
    if os.environ.get("PREMIUM_CHANNEL_BUTTONS", "").strip() in ("1", "true", "yes"):
        return False
    if not isinstance(peer, raw.types.InputPeerChannel):
        return False
    if peer.channel_id in _state["broadcast"]:
        return True
    # a markup-only edit of a channel message is what the auto-button feature does to channel posts
    return (type(query) is raw.functions.messages.EditMessage
            and getattr(query, "message", None) is None and getattr(query, "media", None) is None)


def _mark_broadcast(chat_id):
    try:
        if int(chat_id) < -1000000000000:
            _state["broadcast"].add(get_channel_id(int(chat_id)))
    except Exception:
        pass


def _skip_peer(peer) -> bool:
    return isinstance(peer, raw.types.InputPeerChannel) and (
        peer.channel_id == _log_channel_id or peer.channel_id in _skip_channels
    )


def _targets(query):
    f = raw.functions.messages
    t = type(query)
    if t in (f.SendMessage, f.SendMedia, f.EditMessage):
        return [] if _skip_peer(query.peer) else [query]
    if t is f.SendMultiMedia:
        return [] if _skip_peer(query.peer) else list(query.multi_media)
    return []


_orig_invoke = Client.invoke


def _revert(undo, kind):
    for u in undo:
        if u[0] == kind:
            setattr(u[1], u[2], u[3])


def _retryable(e) -> bool:
    return not isinstance(e, FloodWait) and getattr(e, "ID", "") not in _UNRELATED


# Per emoji-id experience: a message that carried id X went through (ok) or had to be re-sent
# plain (bad). An id that keeps failing and never once worked is thrown away, so ONE bad emoji
# can no longer turn a whole message plain.
_id_ok, _id_bad = {}, {}
_ERR_LOGGED = {"n": 0}


def _forget_bad():
    dead = {i for i, n in _id_bad.items() if n >= ID_STRIKES and _id_ok.get(i, 0) == 0}
    if not dead:
        return
    for store in ("auto", "manual", "style"):
        for k in [k for k, v in _state[store].items() if v in dead]:
            del _state[store][k]
    for ch in list(_state["learned"]):
        for i in [i for i in _state["learned"][ch] if i in dead]:
            del _state["learned"][ch][i]
    for i in dead:
        _id_bad.pop(i, None)
    _rebuild()
    logger.warning(f"[premium_emoji] dropped {len(dead)} emoji id(s) that Telegram keeps refusing: {sorted(dead)}")


async def _blame(kind, peer):
    """The plain re-send worked, so `kind` ('txt' or 'btn') is what Telegram refused."""
    if isinstance(peer, raw.types.InputPeerChannel):
        _skip_channels.add(peer.channel_id)      # that channel does not take premium emoji: skip it from now on
        logger.warning(f"[premium_emoji] channel {peer.channel_id} refused premium {kind}; skipping it from now on")
        return
    key = f"{kind}_fails"
    _state[key] += 1
    logger.warning(f"[premium_emoji] Telegram refused premium {'emoji' if kind == 'txt' else 'button icons'}; "
                   f"sent plain instead ({_state[key]}/{AUTO_OFF_AFTER})")
    if _state[key] >= AUTO_OFF_AFTER:
        # switched off for THIS run only (not saved): the next start tries again, so a temporary
        # problem can never leave the feature stuck off forever
        _state["on" if kind == "txt" else "btn_on"] = False
        logger.error(f"[premium_emoji] switched {'emoji' if kind == 'txt' else 'button icons'} OFF until the next restart - "
                     "does the bot owner still have Telegram Premium?")


async def _invoke(self, query, *args, **kwargs):
    targets = _targets(query)
    if not targets:                      # login, downloads, reactions, ... : not our business
        return await _orig_invoke(self, query, *args, **kwargs)
    if not _state["loaded"]:
        await _load_state()
    if not _state["packs_started"] and _state["loaded"]:
        _spawn(_autoload_packs(self))    # normally already done at startup; this is the safety net
    undo = []                            # (kind, object, attribute, original value, emoji ids added)
    if _state["on"] and _state["rx"] is not None:
        for tgt in targets:
            old = getattr(tgt, "entities", None)
            new = _inject(getattr(tgt, "message", None), old)
            if new is not None:
                old_ids = {id(e) for e in (old or [])}
                ids = [e.document_id for e in new
                       if type(e) is raw.types.MessageEntityCustomEmoji and id(e) not in old_ids]
                undo.append(("txt", tgt, "entities", old, ids))
                tgt.entities = new
    if (_state["btn_on"] and _state["rx"] is not None and getattr(query, "reply_markup", None) is not None
            and not _channel_post(query.peer, query)):
        new = _style_markup(query.reply_markup)
        if new is not None:
            undo.append(("btn", query, "reply_markup", query.reply_markup, []))
            query.reply_markup = new
    if not undo:
        return await _orig_invoke(self, query, *args, **kwargs)
    kinds = {u[0] for u in undo}
    call_kwargs = kwargs
    if "btn" in kinds:   # the new button format is only ever tried with a SHORT timeout, then plain
        call_kwargs = dict(kwargs)
        call_kwargs.setdefault("retries", 2)
        call_kwargs.setdefault("timeout", 12)
    try:
        result = await _orig_invoke(self, query, *args, **call_kwargs)
        for k in kinds:
            _state[f"{k}_fails"] = 0
        for u in undo:
            for i in u[4]:
                _id_ok[i] = _id_ok.get(i, 0) + 1
        return result
    except (RPCError, asyncio.TimeoutError, TimeoutError, OSError) as e:
        if isinstance(e, RPCError):
            if not _retryable(e):
                raise
        elif "btn" not in kinds:
            raise                        # a plain network problem, nothing to do with emoji
        if _ERR_LOGGED["n"] < 30:
            _ERR_LOGGED["n"] += 1
            logger.warning(f"[premium_emoji] {type(query).__name__} with {'+'.join(sorted(kinds))} failed: "
                           f"{type(e).__name__} {getattr(e, 'ID', '')} - retrying without it")
        txt = [u for u in undo if u[0] == "txt"]
        blamed = None
        if "btn" in kinds:               # step 1: take only the button icons away
            _revert(undo, "btn")
            try:
                result = await _orig_invoke(self, query, *args, **kwargs)
                blamed = "btn"
            except RPCError as e2:
                if not _retryable(e2):
                    raise
        if blamed is None and txt:       # step 2: keep the emoji that worked before, drop only the unproven ones
            dropped, changed = set(), False
            for _k, tgt, _a, old, ids in txt:
                unproven = {i for i in ids if _id_ok.get(i, 0) == 0}
                if unproven and len(unproven) < len(set(ids)):
                    old_ids = {id(x) for x in (old or [])}
                    tgt.entities = [x for x in tgt.entities if not (
                        type(x) is raw.types.MessageEntityCustomEmoji and x.document_id in unproven and id(x) not in old_ids)]
                    dropped |= unproven
                    changed = True
            if changed:
                try:
                    result = await _orig_invoke(self, query, *args, **kwargs)
                    for i in dropped:
                        _id_bad[i] = _id_bad.get(i, 0) + 1
                    for _k, _t, _a, _o, ids in txt:
                        for i in ids:
                            if i not in dropped:
                                _id_ok[i] = _id_ok.get(i, 0) + 1
                    _forget_bad()
                    blamed = "partial"
                except RPCError as e3:
                    if not _retryable(e3):
                        raise
        if blamed is None:               # step 3: everything plain
            _revert(undo, "txt")
            _revert(undo, "btn")
            result = await _orig_invoke(self, query, *args, **kwargs)   # raises again if it was something else
            blamed = "txt" if txt else "btn"
            for _k, _t, _a, _o, ids in txt:
                for i in ids:
                    _id_bad[i] = _id_bad.get(i, 0) + 1
            _forget_bad()
        if blamed != "partial":
            await _blame(blamed, query.peer)
        return result


if not getattr(Client.invoke, "_premium_patched", False):
    _invoke._premium_patched = True
    Client.invoke = _invoke


# --------------------------------------------------------------------------- admin commands
_ADMIN = admin_filter


def _learn_from(m: Message) -> dict:
    text = m.text or m.caption or ""
    ents = m.entities or m.caption_entities or []
    u = str(text).encode("utf-16-le")
    out = {}
    for e in ents:
        if e.type != enums.MessageEntityType.CUSTOM_EMOJI:
            continue
        ch = u[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le", "ignore").replace(_FE0F, "").strip()
        if ch and e.custom_emoji_id:
            out[ch] = _s64(e.custom_emoji_id)
    return out


async def _ensure_loaded():
    if not _state["loaded"]:
        await _load_state()


@Client.on_message(filters.command("setemoji") & filters.private & _ADMIN)
async def set_emoji_cmd(client, message: Message):
    await _ensure_loaded()
    learned = _learn_from(message)
    if message.reply_to_message:
        learned.update(_learn_from(message.reply_to_message))
    if not learned:
        return await message.reply_text(
            "<b>Premium emoji kaise set karein:</b>\n\n"
            "<code>/setemoji</code> ke saath apne <b>premium emoji</b> bhejo (same message mein), "
            "ya kisi premium emoji wale message ko reply karke <code>/setemoji</code> likho.\n\n"
            "Har premium emoji apne normal emoji ki jagah le leta hai (jaise premium ✅ → normal ✅).\n"
            "Phir <code>/emojion</code> (text) aur <code>/emojibtn on</code> (buttons) se chalu karo."
        )
    if len(_state["map"]) + len(learned) > MAX_MAPPINGS:
        return await message.reply_text(f"<b>❌ Max {MAX_MAPPINGS} emoji mappings allowed.</b>")
    _state["manual"].update(learned)
    _state["masked"] -= set(learned)
    _rebuild()
    await _save_map()
    await message.reply_text(
        f"<b>✅ {len(learned)} emoji saved</b> (total {len(_state['map'])}).\n"
        + ("Ab <code>/emojion</code> bhejo." if not _state["on"] else "Ye turant lagu ho gaye.")
    )


@Client.on_message(filters.command("emojion") & filters.private & _ADMIN)
async def emoji_on_cmd(client, message: Message):
    await _ensure_loaded()
    if not _state["map"]:
        return await message.reply_text("<b>Pehle <code>/setemoji</code> se kuch premium emoji set karo.</b>")
    ch, doc_id = next(iter(_state["map"].items()))
    try:
        # explicit entity, so this test works whatever the current switch state is
        await client.send_message(
            message.chat.id,
            f'<emoji id="{doc_id}">{ch}</emoji> Premium emoji test',
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        _state["on"] = False
        return await message.reply_text(
            "<b>❌ Telegram ne premium emoji accept nahi kiya.</b>\n\n"
            "Bot ke <b>owner account</b> (jisse BotFather mein bot banaya) ke paas Telegram Premium hona chahiye.\n\n"
            f"<code>{type(e).__name__}: {e}</code>"
        )
    _state["on"] = True
    _state["txt_fails"] = 0
    await update_setting("premium_emoji", True)
    await message.reply_text(
        "<b>✅ Premium emoji ON.</b> Test message mein animated emoji dikh raha hai to sab theek hai.\n"
        "Buttons ke liye: <code>/emojibtn on</code>"
    )


@Client.on_message(filters.command("emojioff") & filters.private & _ADMIN)
async def emoji_off_cmd(client, message: Message):
    _state["on"] = False
    await update_setting("premium_emoji", False)
    await message.reply_text("<b>Premium emoji (text) OFF.</b> Mappings saved hain, <code>/emojion</code> se wapas chalu karo.")


@Client.on_message(filters.command("emojibtn") & filters.private & _ADMIN)
async def emoji_btn_cmd(client, message: Message):
    await _ensure_loaded()
    arg = message.command[1].lower() if len(message.command) > 1 else ""
    if arg == "off":
        _state["btn_on"] = False
        await update_setting("premium_btn", False)
        return await message.reply_text("<b>Button icons OFF.</b> Buttons ab normal emoji ke saath aayenge.")
    if arg != "on":
        return await message.reply_text(
            f"<b>Button icons:</b> {'ON ✅' if _state['btn_on'] else 'OFF ❌'}\n\n"
            "<code>/emojibtn on</code> - test karke chalu karta hai\n<code>/emojibtn off</code> - band"
        )
    if not _state["map"]:
        return await message.reply_text("<b>Pehle <code>/setemoji</code> se kuch premium emoji set karo.</b>")
    ch, doc_id = next(iter(_state["map"].items()))
    markup = _test_markup(doc_id)
    try:
        await client.invoke(
            raw.functions.messages.SendMessage(
                peer=await client.resolve_peer(message.chat.id),
                message=f"Button premium emoji test ({ch})",
                random_id=int.from_bytes(os.urandom(8), "big", signed=True),
                reply_markup=markup,
            ),
            retries=1, timeout=20,
        )
    except Exception as e:
        _state["btn_on"] = False
        return await message.reply_text(
            "<b>❌ Telegram ne button icons accept nahi kiye.</b>\n\n"
            "Ya to bot owner ke paas Premium nahi hai, ya Telegram is bot ke liye button icons nahi deta. Text wale premium emoji pe koi asar nahi hai, wo chalte rahenge.\n\n"
            f"<code>{type(e).__name__}: {e}</code>"
        )
    _state["btn_on"] = True
    _state["btn_fails"] = 0
    await update_setting("premium_btn", True)
    await message.reply_text(
        "<b>✅ Button icons ON.</b>\n\nUpar wale test message ke button pe <b>animated emoji</b> dikh raha hai? "
        "Agar sirf plain text dikhe (icon na dikhe), to <code>/emojibtn off</code> kar do."
    )


@Client.on_message(filters.command("emojipack") & filters.private & _ADMIN)
async def emoji_pack_cmd(client, message: Message):
    await _ensure_loaded()
    arg = message.command[1].strip() if len(message.command) > 1 else ""
    name = re.sub(r"^.*addemoji/", "", arg).strip("/ ")
    if not name:
        packs = ", ".join(f"{n} ({c})" for n, c in _state["packs_ok"].items()) or "—"
        return await message.reply_text(
            f"<b>Loaded packs:</b> {packs}\n\nNaya pack jodne ke liye: <code>/emojipack t.me/addemoji/PackName</code>"
        )
    try:
        got = await _fetch_pack(client, name)
    except Exception as e:
        return await message.reply_text(f"<b>❌ Pack load nahi hua:</b> <code>{type(e).__name__}</code>")
    if not got:
        return await message.reply_text("<b>❌ Is pack mein koi emoji nahi mila.</b>")
    if name not in _state["packs"]:
        _state["packs"].append(name)
        await update_setting("premium_emoji_packs", _state["packs"])
    for k, v in got.items():
        _state["auto"].setdefault(k, v)
    _state["packs_ok"][name] = len(got)
    _rebuild()
    await message.reply_text(f"<b>✅ {name}: {len(got)} emoji jude.</b> Total {len(_state['map'])}.")


@Client.on_message(filters.command("delemoji") & filters.private & _ADMIN)
async def del_emoji_cmd(client, message: Message):
    await _ensure_loaded()
    parts = (message.text or "").split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        return await message.reply_text("<b>Usage:</b> <code>/delemoji ✅ 📁</code> ya <code>/delemoji all</code>")
    if arg.lower() == "all":
        n = len(_state["map"])
        _state["manual"].clear()
        _state["masked"] = set(_state["auto"]) | set(_state["style"]) | set(_state["learned"])
    else:
        n = 0
        for ch in list(_state["map"]):
            if ch in arg.replace(_FE0F, ""):
                _state["manual"].pop(ch, None)
                if ch in _state["auto"] or ch in _state["style"] or ch in _state["learned"]:
                    _state["masked"].add(ch)
                n += 1
    _rebuild()
    await _save_map()
    await message.reply_text(f"<b>🗑 {n} emoji removed</b> ({len(_state['map'])} left).")


@Client.on_message(filters.command(["emoji", "emojilist"]) & filters.private & _ADMIN)
async def emoji_status_cmd(client, message: Message):
    await _ensure_loaded()
    m = _state["map"]
    listing = " ".join(m) if m else "—"
    await message.reply_text(
        f"<b>Premium emoji (text):</b> {'ON ✅' if _state['on'] else 'OFF ❌'}\n"
        f"<b>Button icons:</b> {'ON ✅' if _state['btn_on'] else 'OFF ❌'}\n\n"
        f"{_report_text()}\n"
        f"<b>Haath se set:</b> {len(_state['manual'])}\n\n"
        "<code>/emojipack</code> · <code>/setemoji</code> · <code>/delemoji</code> · <code>/emojion</code> · "
        "<code>/emojioff</code> · <code>/emojibtn on|off</code>"
    )


# --------------------------------------------------------------------------- learn from your posts, automatically
@Client.on_message(filters.channel, group=-6)
async def learn_from_channel_post(client, message: Message):
    """Every channel post the bot can see: if it holds premium emoji, that is YOUR style - remember it."""
    try:
        _mark_broadcast(message.chat.id)
        pairs = _pairs_from(message)
        if not pairs or message.chat.id == _log_channel_id_full():
            return
        if not _state["loaded"]:
            await _load_state()
        n = await _learn_pairs(client, pairs, message.chat.id, message.id)
        if n:
            logger.info(f"[premium_emoji] learned {n} new emoji from a post in {message.chat.id}")
    except Exception:
        logger.error("[premium_emoji] learning from a channel post failed", exc_info=True)


_HAS_PREMIUM_EMOJI = filters.create(lambda _, __, m: bool(_pairs_from(m)))


@Client.on_message(filters.private & _ADMIN & _HAS_PREMIUM_EMOJI, group=-6)
async def learn_from_admin_message(client, message: Message):
    """An admin sends (or forwards) something with premium emoji to the bot: learn them, silently."""
    try:
        if (message.text or "").startswith("/"):
            return                       # /setemoji has its own handler
        if not _state["loaded"]:
            await _load_state()
        n = await _learn_pairs(client, _pairs_from(message))
        if n:
            await message.reply_text(f"<b>✅ {n} naye premium emoji seekh liye.</b> Ab bot mein yahi style lagega.")
    except Exception:
        logger.error("[premium_emoji] learning from an admin message failed", exc_info=True)
