# Telegram Contact Bot

<b>Chat With Owner Directly Through This Bot.</b>

![Typing SVG](https://readme-typing-svg.herokuapp.com/?lines=Welcome+To+Telegram+Contact+Bot+!)

## Features

- [x] Forward User Messages To Owner/Admins
- [x] Reply To User Directly From Owner/Admin Chat
- [x] Cooldown On Bot Replies (No Spam) — `COOLDOWN_SECONDS`
- [x] Reply Mapping Stored In MongoDB (replies keep working after restarts)
- [x] AI Assistant Replies (Google Gemini, optional)
- [x] Channel Content Index — user text is matched against scanned channel posts and the post is sent back
- [x] Restart Bot Anytime (Admin Only)
- [x] Full Button-Based Settings Panel (`/settings`)
- [x] Automatic Keyword Replies
- [x] Force Join One Or More Channels (username, ID or private channel)
- [x] Customizable Start Message (Media / Text / Buttons)
- [x] Broadcast Message To All Users (Media / Text / Buttons / Pin)
- [x] Multi-Admin Support (Add/Remove Admins)
- [x] Ban / Unban Users
- [x] Bot Statistics
- [x] 24x7 Uptime With Built-In Web Server

## Commands

```
start - check I'm alive
settings - open the bot settings panel (admin only)
restart - restart bot (admin only)
clearindex - delete all indexed channel content (admin only)
```

### Channel content index (admin only)

Forward the **last post** of a channel (or send just its `t.me/...` post link) to the bot and
confirm. The bot scans every post up to that ID (text, links and captions) and keeps indexing new
posts, edits and deletions automatically. When a user's message matches indexed posts, that post is
sent back instead of an AI answer. The bot must be a member/admin of the channel.

All other management (automatic replies, force join, start message, broadcast,
admins, banned users, statistics) is done through the `/settings` button menu.

## Variables

* `API_ID` API Id from my.telegram.org
* `API_HASH` API Hash from my.telegram.org
* `BOT_TOKEN` Bot token from @BotFather
* `ADMIN` Telegram Account Id of Owner
* `DATABASE_URI` MongoDB connection string (required — used for users, admins, bans, settings)
* `DATABASE_NAME` MongoDB database name (optional, defaults to `MRNContactBot`)
* `GEMINI_API_KEY` Google Gemini API key (optional — leave empty to disable AI replies)
* `GEMINI_MODEL` Gemini model name (optional, defaults to `gemini-3.6-flash`)
* `AI_EXTRA_PROMPT` Extra instructions for the AI, e.g. what your channel is about (optional)
* `COOLDOWN_SECONDS` Minimum seconds between two bot-generated replies to the same user (optional, defaults to `5`)

The bot stops at startup with a clear message if a required variable is missing.

Force-join channels are managed from `/settings → Force join` (no env var needed anymore).


## How To Deploy

* Get `API_ID` & `API_HASH` from [my.telegram.org](https://my.telegram.org)
* Get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather)
* Get your `ADMIN` id from [@userinfobot](https://t.me/userinfobot)
* Deploy using any one method below, fill the variables, and you're done.

> ⚠️ The deploy buttons below point to the upstream repo. Replace the repository URL in each link with your own fork, otherwise you'll deploy the original code.

### Deploy To Heroku

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/Steve-Botz/TG-Contact-Bot)

### Deploy To Koyeb

[![Deploy](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&repository=github.com/Steve-Botz/TG-Contact-Bot&branch=main&run_command=python3+bot.py)

### Deploy To Render

[![Deploy](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Steve-Botz/TG-Contact-Bot)

### Run Cmd
```
python3 bot.py
```
### Deploy Locally

```
git clone https://github.com/Steve-Botz/TG-Contact-Bot
cd TG-Contact-Bot
pip install -r requirements.txt
python3 bot.py
```

## Contact Developer  👨‍💻

[![Contact Developer](https://img.shields.io/badge/Contact-Developer-blue?logo=telegram)](https://t.me/AmaniContactBot)    
[![Telegram Channel](https://img.shields.io/badge/Telegram-Updates%20Channel-blue?logo=telegram)](https://t.me/SteveBotz)  
[![Support Group](https://img.shields.io/badge/Telegram-Support%20Group-blue?logo=telegram)](https://t.me/SteveBotzSupport)

Join My <a href='https://t.me/SteveBotz'>Update Channel</a> For Latest Updates & Features.

---

<div align="center">
<b>⭐ Star this repository if you found it helpful! ⭐</b>
</div>
