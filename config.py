import os

API_ID = int(os.environ.get("API_ID", ""))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN = int(os.environ.get("ADMIN", ""))

DATABASE_URI = os.environ.get("DATABASE_URI", "mongodb+srv://PMTV4:2265714110@cluster0.xgtojp7.mongodb.net/?appName=Cluster0")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "MRNContactBot")
