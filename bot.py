import os
import re
import json
import asyncio
import logging
from groq import Groq
from telegram import Update, Bot
from telegram.ext import (
    Application, MessageHandler, filters, ContextTypes
)

# ============================================================
# CONFIG — Yahan apni values daalo
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "YOUR_GROQ_API_KEY_HERE")
DISCUSSION_GROUP_ID = int(os.getenv("DISCUSSION_GROUP_ID", "0"))  # -100xxxxxxxxxx format
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # APK channel ID

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# GROQ CLIENT
# ============================================================
groq_client = Groq(api_key=GROQ_API_KEY)

# ============================================================
# APP/APK KEYWORDS MAP
# Yahan apps ke naam aur unke keywords add karo
# ============================================================
APP_KEYWORDS = {
    "capcut": ["capcut", "cap cut", "video edit app", "tiktok editor"],
    "inshot": ["inshot", "in shot", "inshot pro"],
    "kinemaster": ["kinemaster", "kine master", "kinemaster pro"],
    "alight motion": ["alight motion", "alight", "motion graphics app"],
    "vn": ["vn editor", "vn video", "vn app"],
    "canva": ["canva", "canva pro", "design app"],
    "pixellab": ["pixellab", "pixel lab", "text on photo"],
    "picsart": ["picsart", "pics art", "photo editor"],
    "lightroom": ["lightroom", "lightroom mobile", "lr mobile", "adobe lightroom"],
    "premiere": ["premiere rush", "adobe premiere", "premiere pro mobile"],
}

# ============================================================
# CHANNEL MESSAGE CACHE
# Bot channel ke messages memory mein rakhega
# ============================================================
channel_cache = {}  # { "capcut": [{"text": "...", "link": "...", "message_id": 123}] }


async def fetch_channel_messages(bot: Bot):
    """Channel ke recent messages fetch karke cache mein store karo"""
    global channel_cache
    try:
        logger.info("Channel messages fetch kar raha hun...")
        # Telegram se directly channel messages fetch nahi hote without forwarding
        # Isliye hum bot ko channel mein add karke updates catch karenge
        # Cache file se load karo agar hai
        if os.path.exists("channel_cache.json"):
            with open("channel_cache.json", "r", encoding="utf-8") as f:
                channel_cache = json.load(f)
            logger.info(f"Cache load hua: {len(channel_cache)} apps")
    except Exception as e:
        logger.error(f"Cache load error: {e}")


def save_cache():
    """Cache ko file mein save karo"""
    try:
        with open("channel_cache.json", "w", encoding="utf-8") as f:
            json.dump(channel_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Cache save error: {e}")


def detect_app_request(text: str):
    """
    Message mein kaunsi app maangi ja rahi hai detect karo
    Returns: app_name ya None
    """
    text_lower = text.lower()
    for app_name, keywords in APP_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text_lower:
                return app_name
    return None


def find_app_in_cache(app_name: str):
    """Cache mein app ka link dhundo"""
    app_lower = app_name.lower()
    # Direct match
    if app_lower in channel_cache:
        entries = channel_cache[app_lower]
        if entries:
            return entries[-1]  # Latest entry do
    # Partial match
    for cached_app, entries in channel_cache.items():
        if app_lower in cached_app or cached_app in app_lower:
            if entries:
                return entries[-1]
    return None


def extract_links(text: str):
    """Text se URLs nikalo"""
    url_pattern = r'https?://[^\s]+'
    return re.findall(url_pattern, text)


async def get_ai_response(user_message: str, username: str = "bhai") -> str:
    """Groq AI se response lo"""
    try:
        system_prompt = """Tu ek helpful YouTube channel assistant hai.
Tu Hinglish mein baat karta hai (Hindi + English mix).
Tu friendly, casual aur helpful hai — bilkul ek dost ki tarah.

Channel ke baare mein:
- Ye channel video editing tutorials, new apps, websites aur tech tips share karta hai
- Hindi-speaking audience ke liye content hai
- CapCut, InShot, Kinemaster, Alight Motion jaisi video editing apps ke baare mein jaanta hai

Rules:
- Short aur clear answers do (3-5 lines max)
- Hinglish use karo (Hindi + English mix)
- Agar koi app ya software ke baare mein pooche to basic info do
- Agar kuch nahi pata to honestly bol do
- Emojis thodi use karo
- "Bhai" ya user ke naam se address karo"""

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            max_tokens=300,
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        return "Bhai, abhi kuch technical issue aa gaya. Thodi der baad try karo! 🙏"


def should_bot_reply(message_text: str, bot_username: str, is_reply_to_bot: bool) -> bool:
    """
    Bot ko reply karna chahiye ya nahi decide karo
    - Question mark hai
    - Bot ka mention hai
    - App/APK request hai
    - Bot ke message ka reply hai
    """
    if not message_text:
        return False
    text_lower = message_text.lower()

    # Bot mention
    if f"@{bot_username.lower()}" in text_lower:
        return True

    # Bot ke message ka reply
    if is_reply_to_bot:
        return True

    # Question
    if "?" in message_text:
        return True

    # App request keywords
    app_request_words = ["link do", "link bhejo", "kahan se", "kaise milega",
                         "download karo", "download link", "apk do", "apk bhejo",
                         "mod apk", "free mein", "kahan milega", "share karo link"]
    for word in app_request_words:
        if word in text_lower:
            return True

    # App name detect ho
    if detect_app_request(message_text):
        return True

    return False


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main channel ke posts ko cache mein save karo
    (Bot channel mein admin hona chahiye)
    """
    message = update.channel_post or update.edited_channel_post
    if not message:
        return

    # Sirf apne channel ke posts
    if message.chat.id != CHANNEL_ID:
        return

    text = message.text or message.caption or ""
    links = extract_links(text)

    # App detect karo
    app_name = detect_app_request(text)

    if app_name and links:
        if app_name not in channel_cache:
            channel_cache[app_name] = []

        channel_cache[app_name].append({
            "text": text[:200],
            "links": links,
            "message_id": message.message_id
        })
        save_cache()
        logger.info(f"Cache updated: {app_name} -> {links}")


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Discussion group ke messages handle karo"""
    message = update.message
    if not message:
        return

    # Sirf discussion group
    if message.chat.id != DISCUSSION_GROUP_ID:
        return

    # Bot ke apne messages ignore karo
    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or message.caption or ""
    if not text:
        return

    # Username
    user = message.from_user
    username = user.first_name if user else "Bhai"

    # Bot ke message ka reply hai?
    is_reply_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.is_bot
    )

    bot_username = context.bot.username or "bot"

    # Reply karna chahiye?
    if not should_bot_reply(text, bot_username, is_reply_to_bot):
        return

    logger.info(f"Replying to: {username} -> {text[:50]}")

    # Typing indicator
    await context.bot.send_chat_action(
        chat_id=message.chat.id,
        action="typing"
    )

    # Pehle app request check karo
    app_name = detect_app_request(text)
    reply_text = ""

    if app_name:
        cached = find_app_in_cache(app_name)
        if cached:
            links_str = "\n".join(cached["links"])
            reply_text = (
                f"Haan {username}! 😊 **{app_name.title()}** ka link ye raha:\n\n"
                f"{links_str}\n\n"
                f"Channel pe aur bhi materials available hain! 🔥"
            )
        else:
            # AI se response lo aur batao channel check karo
            ai_resp = await get_ai_response(text, username)
            reply_text = (
                f"{ai_resp}\n\n"
                f"📌 *Tip: Channel mein check karo, wahan link share kiya hoga!*"
            )
    else:
        # Normal question — AI se answer lo
        reply_text = await get_ai_response(text, username)

    # Reply bhejo
    try:
        await message.reply_text(
            reply_text,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Reply error: {e}")
        # Markdown fail ho to plain text try karo
        try:
            await message.reply_text(reply_text)
        except Exception as e2:
            logger.error(f"Plain reply error: {e2}")


async def post_init(application: Application):
    """Bot start hone par cache load karo"""
    await fetch_channel_messages(application.bot)
    logger.info("✅ Bot ready hai!")


def main():
    """Bot start karo"""
    print("🤖 Bot start ho raha hai...")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handlers add karo
    # Channel posts (APK links cache karne ke liye)
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL,
        handle_channel_post
    ))

    # Group messages (discussion)
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT,
        handle_group_message
    ))

    print("✅ Bot chal raha hai! Rokne ke liye Ctrl+C dabaao.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
