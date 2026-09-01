# Telegram Contact Bot

<b>Chat With Owner Directly Through This Bot.</b>

![Typing SVG](https://readme-typing-svg.herokuapp.com/?lines=Welcome+To+Telegram+Contact+Bot+!)

## Features

- [x] Forward User Messages To Owner/Admins
- [x] Reply To User Directly From Owner/Admin Chat
- [x] Auto Cooldown Message (No Spam)
- [x] Auto Memory Cleanup
- [x] Restart Bot Anytime (Admin Only)
- [x] Full Button-Based Settings Panel (`/settings`)
- [x] Automatic Keyword Replies
- [x] Force Join One Or More Channels
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
```

All other management (automatic replies, force join, start message, broadcast,
admins, banned users, statistics) is done through the `/settings` button menu.

## Variables

* `API_ID` API Id from my.telegram.org
* `API_HASH` API Hash from my.telegram.org
* `BOT_TOKEN` Bot token from @BotFather
* `ADMIN` Telegram Account Id of Owner
* `DATABASE_URI` MongoDB connection string (required — used for users, admins, bans, settings)
* `DATABASE_NAME` MongoDB database name (optional, defaults to `MRNContactBot`)

Force-join channels are managed from `/settings → Force join` (no env var needed anymore).


## How To Deploy

* Get `API_ID` & `API_HASH` from [my.telegram.org](https://my.telegram.org)
* Get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather)
* Get your `ADMIN` id from [@userinfobot](https://t.me/userinfobot)
* Deploy using any one method below, fill the variables, and you're done.

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
