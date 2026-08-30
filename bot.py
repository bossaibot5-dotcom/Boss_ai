import os
import time
import sqlite3
import threading
import base64
import io
import traceback
import requests
import telebot

from google import genai

from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

FREE_LIMIT = 15
MONTHLY_PRICE = 100

DB_FILE = "bossai.db"

bot = telebot.TeleBot(TOKEN, parse_mode=None)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            free_used INTEGER DEFAULT 0,
            free_date TEXT,
            model TEXT DEFAULT 'DeepSeek',
            subscription_until INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT NULL,
            referrals INTEGER DEFAULT 0,
            paid_referrals INTEGER DEFAULT 0,
            created_at INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at INTEGER
        )
    """)

    conn.commit()
    conn.close()


def current_date():
    return time.strftime("%Y-%m-%d")


def get_user(user_id, first_name="", username=""):
    conn = get_db()

    user = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if user is None:
        conn.execute(
            """
            INSERT INTO users
            (
                user_id,
                first_name,
                username,
                free_used,
                free_date,
                created_at
            )
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (
                user_id,
                first_name or "",
                username or "",
                current_date(),
                int(time.time())
            )
        )

        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    elif user["free_date"] != current_date():

        conn.execute(
            """
            UPDATE users
            SET free_used=0, free_date=?
            WHERE user_id=?
            """,
            (
                current_date(),
                user_id
            )
        )

        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    conn.close()

    return user


def subscription_active(user):
    return (
        user["subscription_until"]
        and user["subscription_until"] > int(time.time())
    )


def get_subscription_price(user):

    if user["referrals"] >= 50 and user["paid_referrals"] >= 10:
        return 50

    if user["referrals"] >= 30:
        return 70

    return 100


# ============================================================
# MAIN KEYBOARD
# ============================================================

def main_keyboard():

    markup = ReplyKeyboardMarkup(resize_keyboard=True)

    markup.row(
        KeyboardButton("💳 Payment Methods"),
        KeyboardButton("👥 Referral")
    )

    markup.row(
        KeyboardButton("🤖 Models"),
        KeyboardButton("🔄 Restart")
    )

    markup.row(
        KeyboardButton("❓ Help"),
        KeyboardButton("📊 My Account")
    )

    markup.row(
        KeyboardButton("🎨 Create Image"),
        KeyboardButton("🎵 Create Music")
    )

    return markup


# ============================================================
# MEMORY
# ============================================================

def save_message(user_id, role, content):

    conn = get_db()

    conn.execute(
        """
        INSERT INTO messages
        (
            user_id,
            role,
            content,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            role,
            content,
            int(time.time())
        )
    )

    conn.commit()
    conn.close()


def get_history(user_id):

    conn = get_db()

    rows = conn.execute(
        """
        SELECT role, content
        FROM messages
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 10
        """,
        (user_id,)
    ).fetchall()

    conn.close()

    rows = list(reversed(rows))

    return [
        {
            "role": row["role"],
            "content": row["content"]
        }
        for row in rows
    ]


# ============================================================
# AI SYSTEM PROMPT
# ============================================================

def system_prompt():

    return (
        "You are BOSSAI, a natural all-in-one AI assistant. "

        "Speak naturally and intelligently. "
        "Default language is English. "
        "If the user speaks Amharic, respond naturally in Amharic. "
        "If the user speaks another language, respond naturally in that language. "

        "Remember relevant conversation context. "

        "Do not unnecessarily say that you are a bot. "

        "IMPORTANT FORMATTING RULES: "
        "Do not use hashtag symbols. "
        "Do not use Markdown headings. "
        "Do not use double asterisks for bold text. "
        "Do not use excessive decorative formatting. "
        "Do not start every answer with unnecessary headings. "
        "Write clean, natural and attractive responses. "
        "You may use emojis when they genuinely improve readability. "
        "Use short paragraphs and simple lists when useful. "

        "When answering a normal question, answer the actual question directly. "

        "If the user asks you to generate an image, tell them to use "
        "the Create Image button if they are not already using that feature. "

        "If the user asks you to generate music, tell them to use "
        "the Create Music button if they are not already using that feature. "

        "Never claim that a file was generated or sent unless the system "
        "actually generated and sent the file."
    )


# ============================================================
# CHAT MODELS
# ============================================================

CHAT_MODELS = {
    "DeepSeek": "deepseek/deepseek-chat",
    "GPT-4o": "openai/gpt-4o",
    "Claude": "anthropic/claude-3.5-sonnet",
    "Grok": "x-ai/grok-2-1212",
}


# ============================================================
# OPENROUTER CHAT
# ============================================================

def ask_openrouter(user_id, text):

    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    user = get_user(user_id)

    model = user["model"]

    history = get_history(user_id)

    messages = [
        {
            "role": "system",
            "content": system_prompt()
        }
    ]

    messages.extend(history)

    messages.append(
        {
            "role": "user",
            "content": text
        }
    )

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",

        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },

        json={
            "model": CHAT_MODELS.get(
                model,
                CHAT_MODELS["DeepSeek"]
            ),
            "messages": messages
        },

        timeout=90
    )

    if not response.ok:
        raise RuntimeError(
            f"OpenRouter {response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    return data["choices"][0]["message"]["content"]


# ============================================================
# GEMINI CHAT
# ============================================================

def ask_gemini(user_id, text):

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")

    history = get_history(user_id)

    conversation = ""

    for item in history:
        conversation += (
            item["role"]
            + ": "
            + item["content"]
            + "\n"
        )

    prompt = (
        system_prompt()
        + "\n\nPrevious conversation:\n"
        + conversation
        + "\n\nCurrent user message:\n"
        + text
    )

    client = genai.Client(
        api_key=GEMINI_API_KEY
    )

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt
    )

    if not response.text:
        raise RuntimeError(
            "Gemini returned an empty response."
        )

    return response.text


# ============================================================
# AI ROUTER
# ============================================================

def ask_ai(user_id, text):

    user = get_user(user_id)

    if user["model"] == "Gemini":
        return ask_gemini(user_id, text)

    try:

        return ask_openrouter(
            user_id,
            text
        )

    except Exception as openrouter_error:

        print(
            "OpenRouter failed:",
            openrouter_error
        )

        if GEMINI_API_KEY:

            try:

                return ask_gemini(
                    user_id,
                    text
                )

            except Exception as gemini_error:

                print(
                    "Gemini fallback failed:",
                    gemini_error
                )

                raise RuntimeError(
                    "The AI services are temporarily unavailable."
                )

        raise


# ============================================================
# TYPING
# ============================================================

def typing_loop(chat_id, stop_event):

    while not stop_event.is_set():

        try:
            bot.send_chat_action(
                chat_id,
                "typing"
            )
        except Exception:
            pass

        stop_event.wait(4)


# ============================================================
# CLEAN AI FORMATTING
# ============================================================

def clean_formatting(text):

    if not text:
        return ""

    replacements = [
        ("**", ""),
        ("###", ""),
        ("##", ""),
        ("___", ""),
        ("__", ""),
    ]

    for old, new in replacements:
        text = text.replace(old, new)

    text = text.replace("\r\n", "\n")

    while "\n\n\n" in text:
        text = text.replace(
            "\n\n\n",
            "\n\n"
        )

    return text.strip()


# ============================================================
# REPLY MESSAGE
# ============================================================

def send_reply_long_message(
    message,
    text
):

    if not text:
        text = "Sorry, I could not generate a response."

    text = clean_formatting(text)

    chunks = []

    for i in range(
        0,
        len(text),
        4000
    ):
        chunks.append(
            text[i:i + 4000]
        )

    if not chunks:
        return

    # First part replies directly to the user's message
    bot.reply_to(
        message,
        chunks[0]
    )

    # Remaining parts are normal messages
    for chunk in chunks[1:]:

        bot.send_message(
            message.chat.id,
            chunk
        )


# ============================================================
# START
# ============================================================

@bot.message_handler(commands=["start"])
def start(message):

    user_id = message.from_user.id

    first_name = (
        message.from_user.first_name
        or "there"
    )

    username = (
        message.from_user.username
        or ""
    )

    user = get_user(
        user_id,
        first_name,
        username
    )

    # --------------------------------------------------------
    # REFERRAL PROCESSING
    # --------------------------------------------------------

    referral_text = ""

    if message.text:

        parts = message.text.split(
            maxsplit=1
        )

        if len(parts) == 2:

            start_parameter = parts[1].strip()

            if start_parameter.startswith(
                "ref_"
            ):

                try:

                    referrer_id = int(
                        start_parameter[4:]
                    )

                    if (
                        referrer_id != user_id
                        and user["referred_by"] is None
                    ):

                        conn = get_db()

                        referrer = conn.execute(
                            """
                            SELECT user_id
                            FROM users
                            WHERE user_id=?
                            """,
                            (referrer_id,)
                        ).fetchone()

                        if referrer:

                            conn.execute(
                                """
                                UPDATE users
                                SET referred_by=?
                                WHERE user_id=?
                                AND referred_by IS NULL
                                """,
                                (
                                    referrer_id,
                                    user_id
                                )
                            )

                            conn.execute(
                                """
                                UPDATE users
                                SET referrals=referrals+1
                                WHERE user_id=?
                                """,
                                (referrer_id,)
                            )

                            conn.commit()

                            referral_text = (
                                "\n\n🎉 Welcome through a referral!\n"
                                "The referral has been recorded successfully."
                            )

                        conn.close()

                except Exception as error:

                    print(
                        "Referral error:",
                        error
                    )

    price = get_subscription_price(user)

    text = (
        f"👋 Hello {first_name}!\n\n"

        "Welcome to BOSSAI.\n"
        "Your all-in-one AI assistant.\n\n"

        "🤖 AI Chat\n"
        "Ask questions, learn, write, translate, "
        "solve problems and work with code.\n\n"

        "🎨 Image Generation\n"
        "Create images from your own description.\n\n"

        "🎵 Music Generation\n"
        "Create short AI music clips from a prompt.\n\n"

        "🧠 Conversation Memory\n"
        "BOSSAI can keep the recent context of your chat.\n\n"

        f"🆓 Free access: {FREE_LIMIT} messages per day\n"
        f"⭐ Unlimited: {price} ETB/month\n\n"

        "Choose an option from the menu below "
        "and let's get started."
    )

    bot.send_message(
        message.chat.id,
        text + referral_text,
        reply_markup=main_keyboard()
    )


# ============================================================
# HELP
# ============================================================

@bot.message_handler(commands=["help"])
def help_command(message):

    text = (
        "❓ BOSSAI Help\n\n"

        "💬 Chat\n"
        "Send your question directly.\n\n"

        f"🆓 Free plan\n"
        f"{FREE_LIMIT} messages per day.\n\n"

        f"⭐ Unlimited\n"
        f"{MONTHLY_PRICE} ETB/month.\n\n"

        "💳 Payment Methods\n"
        "Choose the available payment method.\n\n"

        "👥 Referral\n"
        "Invite people using your personal referral link "
        "and track your referrals.\n\n"

        "🤖 Models\n"
        "Choose the AI model you want to use.\n\n"

        "🎨 Create Image\n"
        "Describe the image you want.\n\n"

        "🎵 Create Music\n"
        "Describe the music you want.\n\n"

        "🔄 Restart\n"
        "Clear your current AI conversation memory.\n\n"

        "Support: @Silent_Survivorr"
    )

    bot.reply_to(
        message,
        text
    )


@bot.message_handler(
    func=lambda m: m.text == "❓ Help"
)
def help_button(message):

    help_command(message)


# ============================================================
# PAYMENT MENU
# ============================================================

def show_payment_menu(message):

    user = get_user(
        message.from_user.id
    )

    price = get_subscription_price(
        user
    )

    markup = InlineKeyboardMarkup()

    markup.add(
        InlineKeyboardButton(
            f"💳 Telebirr — {price} ETB/month",
            callback_data="telebirr"
        )
    )

    markup.add(
        InlineKeyboardButton(
            "🌍 Payoneer",
            callback_data="payoneer"
        )
    )

    markup.add(
        InlineKeyboardButton(
            "🅿️ PayPal",
            callback_data="paypal"
        )
    )

    bot.send_message(
        message.chat.id,
        "💳 Choose your payment method:",
        reply_markup=markup
    )


@bot.message_handler(
    commands=["menu"]
)
def menu_command(message):

    show_payment_menu(
        message
    )


@bot.message_handler(
    func=lambda m: m.text == "💳 Payment Methods"
)
def payment_button(message):

    show_payment_menu(
        message
    )


# ============================================================
# PAYMENT CALLBACK
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
    call.data in [
        "telebirr",
        "payoneer",
        "paypal"
    ]
)
def payment_callback(call):

    bot.answer_callback_query(
        call.id
    )

    if call.data == "telebirr":

        user = get_user(
            call.from_user.id
        )

        price = get_subscription_price(
            user
        )

        bot.send_message(
            call.message.chat.id,

            f"💳 Telebirr Payment\n\n"
            f"Amount: {price} ETB/month\n\n"
            "Receiver: Hussein\n"
            "Telebirr: 0964990206\n\n"
            "After payment, send your payment receipt "
            "screenshot here.\n\n"
            "Your subscription will be activated "
            "after manual verification."
        )

    elif call.data == "payoneer":

        bot.send_message(
            call.message.chat.id,
            "🌍 Payoneer is currently unavailable."
        )

    elif call.data == "paypal":

        bot.send_message(
            call.message.chat.id,
            "🅿️ PayPal is currently unavailable."
        )


# ============================================================
# PAYMENT RECEIPT
# ============================================================

@bot.message_handler(
    content_types=["photo"]
)
def payment_receipt(message):

    if ADMIN_ID == 0:

        bot.reply_to(
            message,
            "Receipt received. "
            "Admin verification is not configured yet."
        )

        return

    user = get_user(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.username
    )

    price = get_subscription_price(
        user
    )

    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO payments
        (
            user_id,
            amount,
            status,
            created_at
        )
        VALUES (?, ?, 'pending', ?)
        """,
        (
            message.from_user.id,
            price,
            int(time.time())
        )
    )

    payment_id = cursor.lastrowid

    conn.commit()
    conn.close()

    markup = InlineKeyboardMarkup()

    markup.add(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=
            f"approve:{payment_id}:{message.from_user.id}"
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=
            f"reject:{payment_id}:{message.from_user.id}"
        )
    )

    caption = (
        "💳 Payment Receipt\n\n"

        f"Payment ID: {payment_id}\n"
        f"User: {message.from_user.first_name}\n"
        f"Username: @{message.from_user.username or 'none'}\n"
        f"User ID: {message.from_user.id}\n"
        f"Amount: {price} ETB\n"
        "Status: Pending"
    )

    bot.send_photo(
        ADMIN_ID,
        message.photo[-1].file_id,
        caption=caption,
        reply_markup=markup
    )

    bot.reply_to(
        message,
        "✅ Your receipt has been sent for verification.\n\n"
        "Please wait for approval."
    )


# ============================================================
# PAYMENT DECISION
# ============================================================

@bot.callback_query_handler(
    func=lambda call:
    call.data.startswith("approve:")
    or call.data.startswith("reject:")
)
def payment_decision(call):

    if call.from_user.id != ADMIN_ID:

        bot.answer_callback_query(
            call.id,
            "Not authorized.",
            show_alert=True
        )

        return

    bot.answer_callback_query(
        call.id
    )

    parts = call.data.split(":")

    action = parts[0]
    payment_id = int(parts[1])
    user_id = int(parts[2])

    conn = get_db()

    payment = conn.execute(
        """
        SELECT *
        FROM payments
        WHERE id=?
        """,
        (payment_id,)
    ).fetchone()

    if not payment:

        conn.close()

        bot.send_message(
            call.message.chat.id,
            "Payment record was not found."
        )

        return

    # Prevent double approval/rejection
    if payment["status"] != "pending":

        conn.close()

        bot.answer_callback_query(
            call.id,
            "This payment was already processed.",
            show_alert=True
        )

        return

    if action == "approve":

        now = int(time.time())

        # If subscription is still active,
        # add 30 days to the existing expiry.
        current_subscription = conn.execute(
            """
            SELECT subscription_until
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        current_until = (
            current_subscription["subscription_until"]
            if current_subscription
            else 0
        )

        base_time = max(
            now,
            current_until or 0
        )

        until = base_time + (
            30 * 24 * 60 * 60
        )

        conn.execute(
            """
            UPDATE payments
            SET status='approved'
            WHERE id=?
            """,
            (payment_id,)
        )

        conn.execute(
            """
            UPDATE users
            SET subscription_until=?
            WHERE user_id=?
            """,
            (
                until,
                user_id
            )
        )

        referral = conn.execute(
            """
            SELECT referred_by
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if referral and referral["referred_by"]:

            conn.execute(
                """
                UPDATE users
                SET paid_referrals=paid_referrals+1
                WHERE user_id=?
                """,
                (
                    referral["referred_by"],
                )
            )

        conn.commit()
        conn.close()

        try:

            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None
            )

        except Exception:
            pass

        bot.send_message(
            user_id,

            "✅ Payment approved!\n\n"
            "Your unlimited subscription is active.\n"
            "30 days have been added to your account.\n\n"
            "Thank you for using BOSSAI."
        )

    else:

        conn.execute(
            """
            UPDATE payments
            SET status='rejected'
            WHERE id=?
            """,
            (payment_id,)
        )

        conn.commit()
        conn.close()

        try:

            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None
            )

        except Exception:
            pass

        bot.send_message(
            user_id,

            "❌ Your payment receipt was rejected.\n\n"
            "Please send a valid receipt again.\n\n"
            "Support: @Silent_Survivorr"
        )


# ============================================================
# REFERRAL
# ============================================================

@bot.message_handler(
    commands=["referrals"]
)
def referral(message):

    show_referral(
        message
    )


@bot.message_handler(
    func=lambda m: m.text == "👥 Referral"
)
def referral_button(message):

    show_referral(
        message
    )


def show_referral(message):

    user = get_user(
        message.from_user.id
    )

    bot_username = bot.get_me().username

    referral_link = (
        f"https://t.me/"
        f"{bot_username}"
        f"?start=ref_{message.from_user.id}"
    )

    price = get_subscription_price(
        user
    )

    conn = get_db()

    invited_users = conn.execute(
        """
        SELECT first_name, username, created_at
        FROM users
        WHERE referred_by=?
        ORDER BY created_at DESC
        """,
        (message.from_user.id,)
    ).fetchall()

    conn.close()

    text = (
        "👥 Referral Program\n\n"

        "Your personal referral link:\n"
        f"{referral_link}\n\n"

        "Share this link with people you invite.\n\n"

        "🎯 Discount levels:\n"
        "30 referrals → 70 ETB/month\n"
        "50 referrals + 10 paid referrals → 50 ETB/month\n\n"

        f"👤 Total referrals: {user['referrals']}\n"
        f"💳 Paid referrals: {user['paid_referrals']}\n"
        f"💰 Your current price: {price} ETB/month\n"
    )

    if invited_users:

        text += "\n📋 Recent referrals:\n"

        for invited in invited_users[:10]:

            invited_name = (
                invited["first_name"]
                or "User"
            )

            invited_username = (
                f"@{invited['username']}"
                if invited["username"]
                else "no username"
            )

            text += (
                f"• {invited_name} "
                f"({invited_username})\n"
            )

    else:

        text += (
            "\nYou have not invited anyone yet."
        )

    bot.reply_to(
        message,
        text
    )


# ============================================================
# MODELS
# ============================================================

@bot.message_handler(
    func=lambda m: m.text == "🤖 Models"
)
def models(message):

    markup = InlineKeyboardMarkup()

    for model in CHAT_MODELS:

        markup.add(
            InlineKeyboardButton(
                model,
                callback_data=f"model:{model}"
            )
        )

    markup.add(
        InlineKeyboardButton(
            "Gemini",
            callback_data="model:Gemini"
        )
    )

    user = get_user(
        message.from_user.id
    )

    bot.send_message(
        message.chat.id,

        f"🤖 Current model: {user['model']}\n\n"
        "Choose your AI model:",

        reply_markup=markup
    )


@bot.callback_query_handler(
    func=lambda call:
    call.data.startswith("model:")
)
def model_callback(call):

    bot.answer_callback_query(
        call.id
    )

    model = call.data.split(
        ":",
        1
    )[1]

    if (
        model not in CHAT_MODELS
        and model != "Gemini"
    ):
        return

    conn = get_db()

    conn.execute(
        """
        UPDATE users
        SET model=?
        WHERE user_id=?
        """,
        (
            model,
            call.from_user.id
        )
    )

    conn.commit()
    conn.close()

    bot.send_message(
        call.message.chat.id,
        f"✅ Model changed to {model}."
    )


# ============================================================
# MY ACCOUNT
# ============================================================

@bot.message_handler(
    func=lambda m: m.text == "📊 My Account"
)
def account(message):

    user = get_user(
        message.from_user.id
    )

    remaining = max(
        0,
        FREE_LIMIT - user["free_used"]
    )

    if subscription_active(user):

        days = max(
            1,
            int(
                (
                    user["subscription_until"]
                    - int(time.time())
                ) / 86400
            )
        )

        plan = (
            "⭐ Unlimited active\n"
            f"Approximately {days} days remaining"
        )

    else:

        plan = "🆓 Free plan"

    conn = get_db()

    total_users = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        """
    ).fetchone()["count"]

    paid_users = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE subscription_until > ?
        """,
        (int(time.time()),)
    ).fetchone()["count"]

    conn.close()

    bot.reply_to(
        message,

        "📊 My Account\n\n"

        f"Plan: {plan}\n"
        f"Free messages remaining today: {remaining}\n"
        f"Current model: {user['model']}\n\n"

        f"👥 Referrals: {user['referrals']}\n"
        f"💳 Paid referrals: {user['paid_referrals']}\n\n"

        f"Registered users: {total_users}\n"
        f"Active paid users: {paid_users}"
    )


# ============================================================
# RESTART
# ============================================================

@bot.message_handler(
    commands=["restart"]
)
def restart_command(message):

    restart(
        message
    )


@bot.message_handler(
    func=lambda m: m.text == "🔄 Restart"
)
def restart(message):

    conn = get_db()

    conn.execute(
        """
        DELETE FROM messages
        WHERE user_id=?
        """,
        (
            message.from_user.id,
        )
    )

    conn.commit()
    conn.close()

    bot.reply_to(
        message,

        "🔄 Done!\n\n"
        "Your previous conversation memory has been cleared.\n"
        "You can start a new conversation now.",

        reply_markup=main_keyboard()
    )


# ============================================================
# FILES
# ============================================================

@bot.message_handler(
    content_types=[
        "document",
        "voice",
        "audio"
    ]
)
def file_handler(message):

    bot.reply_to(
        message,

        "📎 I received your file.\n\n"
        "File and voice analysis can be connected "
        "to the appropriate processing service."
    )


# ============================================================
# GEMINI IMAGE GENERATION
# ============================================================

IMAGE_MODEL = "gemini-2.5-flash-image"


def generate_image(prompt):

    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing."
        )

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/interactions"
    )

    response = requests.post(
        url,

        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY
        },

        json={
            "model": IMAGE_MODEL,
            "input": prompt
        },

        timeout=180
    )

    if not response.ok:

        raise RuntimeError(
            f"Gemini image error "
            f"{response.status_code}"
        )

    data = response.json()

    output_image = data.get(
        "output_image"
    )

    if output_image:

        image_data = output_image.get(
            "data"
        )

        if image_data:

            return base64.b64decode(
                image_data
            )

    # Fallback: inspect steps
    for step in data.get("steps", []):

        for block in step.get(
            "content",
            []
        ):

            if block.get("type") == "image":

                image_data = block.get(
                    "data"
                )

                if image_data:

                    return base64.b64decode(
                        image_data
                    )

    raise RuntimeError(
        "No image was returned."
    )


image_waiting = set()


# ============================================================
# IMAGE BUTTON
# ============================================================

@bot.message_handler(
    func=lambda m:
    m.text == "🎨 Create Image"
)
def image_button(message):

    image_waiting.add(
        message.from_user.id
    )

    bot.reply_to(
        message,

        "🎨 Create Image\n\n"

        "Tell me what image you want.\n\n"

        "Example:\n"
        "A futuristic city at night, "
        "cinematic lighting, realistic, "
        "highly detailed."
    )


def process_image_prompt(message):

    user_id = message.from_user.id

    image_waiting.discard(
        user_id
    )

    prompt = (
        message.text or ""
    ).strip()

    if not prompt:

        bot.reply_to(
            message,
            "Please describe the image you want."
        )

        return

    user = get_user(
        user_id,
        message.from_user.first_name,
        message.from_user.username
    )

    if not subscription_active(user):

        if user["free_used"] >= FREE_LIMIT:

            bot.reply_to(
                message,

                f"You have used all "
                f"{FREE_LIMIT} free messages for today.\n\n"
                f"Unlimited access is "
                f"{MONTHLY_PRICE} ETB/month.\n\n"
                "Open Payment Methods to continue."
            )

            return

        conn = get_db()

        conn.execute(
            """
            UPDATE users
            SET free_used=free_used+1
            WHERE user_id=?
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()

    stop_event = threading.Event()

    typing_thread = threading.Thread(
        target=typing_loop,
        args=(
            message.chat.id,
            stop_event
        ),
        daemon=True
    )

    typing_thread.start()

    try:

        bot.reply_to(
            message,
            "🎨 Creating your image...\n"
            "Please wait a moment."
        )

        image_bytes = generate_image(
            prompt
        )

        image_file = io.BytesIO(
            image_bytes
        )

        image_file.name = (
            "bossai_image.png"
        )

        bot.send_photo(
            message.chat.id,
            image_file,
            caption="🎨 Generated by BOSSAI"
        )

    except Exception as error:

        print(
            "IMAGE ERROR:",
            error
        )

        traceback.print_exc()

        bot.send_message(
            message.chat.id,

            "❌ I couldn't generate the image "
            "right now.\n\n"
            "Please check the Gemini API quota "
            "and try again later."
        )

    finally:

        stop_event.set()


# ============================================================
# GEMINI LYRIA MUSIC
# ============================================================

MUSIC_MODEL = "lyria-3-clip-preview"


def generate_music(prompt):

    if not GEMINI_API_KEY:

        raise RuntimeError(
            "GEMINI_API_KEY is missing."
        )

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/interactions"
    )

    response = requests.post(
        url,

        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY
        },

        json={
            "model": MUSIC_MODEL,
            "input": prompt,
            "response_format": {
                "type": "audio"
            }
        },

        timeout=240
    )

    if not response.ok:

        raise RuntimeError(
            f"Gemini music error "
            f"{response.status_code}"
        )

    data = response.json()

    output_audio = data.get(
        "output_audio"
    )

    if output_audio:

        audio_data = output_audio.get(
            "data"
        )

        if audio_data:

            return base64.b64decode(
                audio_data
            )

    for step in data.get(
        "steps",
        []
    ):

        for block in step.get(
            "content",
            []
        ):

            if block.get(
                "type"
            ) == "audio":

                audio_data = block.get(
                    "data"
                )

                if audio_data:

                    return base64.b64decode(
                        audio_data
                    )

    raise RuntimeError(
        "No audio was returned."
    )


music_waiting = set()


# ============================================================
# MUSIC BUTTON
# ============================================================

@bot.message_handler(
    func=lambda m:
    m.text == "🎵 Create Music"
)
def music_button(message):

    music_waiting.add(
        message.from_user.id
    )

    bot.reply_to(
        message,

        "🎵 Create Music\n\n"

        "Describe the music you want.\n"
        "You can include the genre, mood, "
        "instruments and language.\n\n"

        "Example:\n"
        "Upbeat Ethiopian-inspired pop music "
        "about friendship, happy mood."
    )


def process_music_prompt(message):

    user_id = message.from_user.id

    music_waiting.discard(
        user_id
    )

    prompt = (
        message.text or ""
    ).strip()

    if not prompt:

        bot.reply_to(
            message,
            "Please describe the music you want."
        )

        return

    user = get_user(
        user_id,
        message.from_user.first_name,
        message.from_user.username
    )

    if not subscription_active(user):

        if user["free_used"] >= FREE_LIMIT:

            bot.reply_to(
                message,

                f"You have used all "
                f"{FREE_LIMIT} free messages for today.\n\n"
                f"Unlimited access is "
                f"{MONTHLY_PRICE} ETB/month.\n\n"
                "Open Payment Methods to continue."
            )

            return

        conn = get_db()

        conn.execute(
            """
            UPDATE users
            SET free_used=free_used+1
            WHERE user_id=?
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()

    stop_event = threading.Event()

    typing_thread = threading.Thread(
        target=typing_loop,
        args=(
            message.chat.id,
            stop_event
        ),
        daemon=True
    )

    typing_thread.start()

    try:

        bot.reply_to(
            message,
            "🎵 Composing your music...\n"
            "This may take a little while."
        )

        audio_bytes = generate_music(
            prompt
        )

        audio_file = io.BytesIO(
            audio_bytes
        )

        audio_file.name = (
            "bossai_music.mp3"
        )

        bot.send_audio(
            message.chat.id,
            audio_file,
            caption="🎵 Generated by BOSSAI"
        )

    except Exception as error:

        print(
            "MUSIC ERROR:",
            error
        )

        traceback.print_exc()

        bot.send_message(
            message.chat.id,

            "❌ I couldn't generate the music "
            "right now.\n\n"
            "Please check the Gemini API quota "
            "and try again later."
        )

    finally:

        stop_event.set()


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@bot.message_handler(
    commands=["admin"]
)
def admin_command(message):

    if message.from_user.id != ADMIN_ID:
        return

    now = int(time.time())

    soon = now + (
        3 * 86400
    )

    today_start = int(
        time.mktime(
            time.strptime(
                current_date(),
                "%Y-%m-%d"
            )
        )
    )

    conn = get_db()

    total_users = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        """
    ).fetchone()["c"]

    active_subs = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE subscription_until > ?
        """,
        (now,)
    ).fetchone()["c"]

    expiring_soon = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE subscription_until > ?
        AND subscription_until <= ?
        """,
        (
            now,
            soon
        )
    ).fetchone()["c"]

    pending = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM payments
        WHERE status='pending'
        """
    ).fetchone()["c"]

    approved = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM payments
        WHERE status='approved'
        """
    ).fetchone()["c"]

    rejected = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM payments
        WHERE status='rejected'
        """
    ).fetchone()["c"]

    today_registered = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE created_at >= ?
        """,
        (today_start,)
    ).fetchone()["c"]

    today_messages = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM messages
        WHERE created_at >= ?
        """,
        (today_start,)
    ).fetchone()["c"]

    active_today = conn.execute(
        """
        SELECT COUNT(DISTINCT user_id) AS c
        FROM messages
        WHERE created_at >= ?
        """,
        (today_start,)
    ).fetchone()["c"]

    total_referrals = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE referred_by IS NOT NULL
        """
    ).fetchone()["c"]

    total_paid_referrals = conn.execute(
        """
        SELECT COALESCE(
            SUM(paid_referrals),
            0
        ) AS c
        FROM users
        """
    ).fetchone()["c"]

    total_revenue = conn.execute(
        """
        SELECT COALESCE(
            SUM(amount),
            0
        ) AS c
        FROM payments
        WHERE status='approved'
        """
    ).fetchone()["c"]

    free_users = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE subscription_until <= ?
        """,
        (now,)
    ).fetchone()["c"]

    conn.close()

    report = (
        "👑 BOSSAI ADMIN DASHBOARD\n\n"

        "👥 USERS\n"
        f"Total registered: {total_users}\n"
        f"Free users: {free_users}\n"
        f"Active subscribers: {active_subs}\n"
        f"Expiring within 3 days: {expiring_soon}\n"
        f"Registered today: {today_registered}\n\n"

        "📈 ACTIVITY\n"
        f"Users active today: {active_today}\n"
        f"Messages today: {today_messages}\n\n"

        "👥 REFERRALS\n"
        f"Total referral registrations: {total_referrals}\n"
        f"Paid referral conversions: {total_paid_referrals}\n\n"

        "💳 PAYMENTS\n"
        f"Pending: {pending}\n"
        f"Approved: {approved}\n"
        f"Rejected: {rejected}\n"
        f"Approved revenue: {total_revenue} ETB\n\n"

        "🟢 BOT STATUS\n"
        "24/7 polling: ACTIVE"
    )

    bot.send_message(
        message.chat.id,
        report
    )


# ============================================================
# ADMIN USER LOOKUP
# ============================================================

@bot.message_handler(
    commands=["user"]
)
def admin_user_lookup(message):

    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2:

        bot.reply_to(
            message,
            "Usage:\n/user USER_ID"
        )

        return

    try:

        user_id = int(
            parts[1]
        )

    except ValueError:

        bot.reply_to(
            message,
            "Invalid user ID."
        )

        return

    conn = get_db()

    user = conn.execute(
        """
        SELECT *
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    if not user:

        conn.close()

        bot.reply_to(
            message,
            "User not found."
        )

        return

    referral_users = conn.execute(
        """
        SELECT user_id, first_name, username
        FROM users
        WHERE referred_by=?
        ORDER BY created_at DESC
        """,
        (user_id,)
    ).fetchall()

    user_message_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM messages
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()["c"]

    conn.close()

    subscription = (
        "ACTIVE"
        if subscription_active(user)
        else "NOT ACTIVE"
    )

    report = (
        "👤 USER DETAILS\n\n"

        f"ID: {user['user_id']}\n"
        f"Name: {user['first_name']}\n"
        f"Username: @{user['username'] or 'none'}\n"
        f"Model: {user['model']}\n"
        f"Messages stored: {user_message_count}\n"
        f"Free used today: {user['free_used']}\n\n"

        f"Subscription: {subscription}\n"
        f"Subscription until: "
        f"{user['subscription_until']}\n\n"

        f"Referrals: {user['referrals']}\n"
        f"Paid referrals: {user['paid_referrals']}\n"
        f"Referred by: {user['referred_by'] or 'None'}\n\n"

        f"Verified referral records: "
        f"{len(referral_users)}"
    )

    bot.send_message(
        message.chat.id,
        report
    )


# ============================================================
# ADMIN REFERRAL CHECK
# ============================================================

@bot.message_handler(
    commands=["ref"]
)
def admin_ref_check(message):

    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2:

        bot.reply_to(
            message,
            "Usage:\n/ref USER_ID"
        )

        return

    try:

        user_id = int(
            parts[1]
        )

    except ValueError:

        bot.reply_to(
            message,
            "Invalid user ID."
        )

        return

    conn = get_db()

    user = conn.execute(
        """
        SELECT user_id, first_name,
               username, referrals,
               paid_referrals
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    if not user:

        conn.close()

        bot.reply_to(
            message,
            "User not found."
        )

        return

    referrals = conn.execute(
        """
        SELECT
            user_id,
            first_name,
            username,
            created_at,
            subscription_until
        FROM users
        WHERE referred_by=?
        ORDER BY created_at DESC
        """,
        (user_id,)
    ).fetchall()

    conn.close()

    text = (
        "👥 REFERRAL VERIFICATION\n\n"

        f"User ID: {user['user_id']}\n"
        f"Name: {user['first_name']}\n"
        f"Username: @{user['username'] or 'none'}\n\n"

        f"System referral count: "
        f"{user['referrals']}\n"

        f"Paid referrals: "
        f"{user['paid_referrals']}\n\n"

        "Verified invited users:\n"
    )

    if not referrals:

        text += "No verified referrals."

    else:

        for index, invited in enumerate(
            referrals,
            start=1
        ):

            name = (
                invited["first_name"]
                or "User"
            )

            username = (
                f"@{invited['username']}"
                if invited["username"]
                else "no username"
            )

            paid = (
                "Paid subscriber"
                if invited["subscription_until"]
                and invited["subscription_until"] > int(time.time())
                else "Not active"
            )

            text += (
                f"{index}. {name} "
                f"({username}) — "
                f"{paid}\n"
                f"   ID: {invited['user_id']}\n"
            )

    bot.send_message(
        message.chat.id,
        text
    )


# ============================================================
# BUSY USERS
# ============================================================

busy_users = set()

busy_lock = threading.Lock()

last_request = {}


# ============================================================
# MAIN CHAT
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def chat(message):

    text = (
        message.text or ""
    ).strip()

    if not text:
        return

    if text.startswith("/"):
        return

    user_id = message.from_user.id

    # Image waiting mode
    if user_id in image_waiting:

        process_image_prompt(
            message
        )

        return

    # Music waiting mode
    if user_id in music_waiting:

        process_music_prompt(
            message
        )

        return

    # Rate limit
    now = time.time()

    previous = last_request.get(
        user_id,
        0
    )

    if now - previous < 2:

        bot.reply_to(
            message,
            "⏳ Wait a moment before sending another message."
        )

        return

    last_request[user_id] = now

    user = get_user(
        user_id,
        message.from_user.first_name,
        message.from_user.username
    )

    # Free limit
    if not subscription_active(user):

        if user["free_used"] >= FREE_LIMIT:

            bot.reply_to(
                message,

                f"🆓 You have used all "
                f"{FREE_LIMIT} free messages for today.\n\n"

                f"⭐ Unlimited access is "
                f"{MONTHLY_PRICE} ETB/month.\n\n"

                "Open Payment Methods to continue."
            )

            return

        conn = get_db()

        conn.execute(
            """
            UPDATE users
            SET free_used=free_used+1
            WHERE user_id=?
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()

    # Busy check
    with busy_lock:

        if user_id in busy_users:

            bot.reply_to(
                message,
                "⏳ Your previous message is still processing. "
                "Please wait a moment."
            )

            return

        busy_users.add(
            user_id
        )

    stop_event = threading.Event()

    typing_thread = threading.Thread(
        target=typing_loop,
        args=(
            message.chat.id,
            stop_event
        ),
        daemon=True
    )

    typing_thread.start()

    try:

        save_message(
            user_id,
            "user",
            text
        )

        answer = ask_ai(
            user_id,
            text
        )

        save_message(
            user_id,
            "assistant",
            answer
        )

        send_reply_long_message(
            message,
            answer
        )

    except Exception as error:

        print(
            "CHAT ERROR:",
            error
        )

        traceback.print_exc()

        bot.reply_to(
            message,

            "❌ I'm sorry, but I couldn't "
            "complete that request right now.\n\n"
            "Please try again in a moment."
        )

    finally:

        stop_event.set()

        with busy_lock:

            busy_users.discard(
                user_id
            )


# ============================================================
# STARTUP DIAGNOSTIC
# ============================================================

def startup_diagnostic():

    if ADMIN_ID == 0:
        return

    def status_line(
        name,
        value
    ):

        if not value:

            return (
                f"NOT SET: {name}"
            )

        tail = (
            value[-4:]
            if len(value) >= 4
            else value
        )

        return (
            f"OK: {name} "
            f"(ends in {tail}, "
            f"length {len(value)})"
        )

    report = (
        "BOSSAI STARTUP DIAGNOSTIC\n\n"
    )

    report += (
        status_line(
            "TELEGRAM_BOT_TOKEN",
            TOKEN
        )
        + "\n"
    )

    report += (
        status_line(
            "GEMINI_API_KEY",
            GEMINI_API_KEY
        )
        + "\n"
    )

    report += (
        status_line(
            "OPENROUTER_API_KEY",
            OPENROUTER_API_KEY
        )
        + "\n"
    )

    report += (
        f"ADMIN_ID: {ADMIN_ID}\n"
    )

    report += (
        "\n24/7 polling: READY"
    )

    try:

        bot.send_message(
            ADMIN_ID,
            report
        )

    except Exception as e:

        print(
            "Could not send startup diagnostic:",
            e
        )


# ============================================================
# MAIN 24/7 LOOP
# ============================================================

def main():

    init_database()

    print(
        "BOSSAI is running..."
    )

    startup_diagnostic()

    while True:

        try:

            bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                long_polling_timeout=30
            )

        except Exception as error:

            print(
                "Polling error:",
                error
            )

            time.sleep(5)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
