import os
import time
import sqlite3
import threading
import base64
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


def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, free_used INTEGER DEFAULT 0, free_date TEXT, model TEXT DEFAULT 'DeepSeek', subscription_until INTEGER DEFAULT 0, referred_by INTEGER DEFAULT NULL, referrals INTEGER DEFAULT 0, paid_referrals INTEGER DEFAULT 0, created_at INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT, created_at INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT DEFAULT 'pending', created_at INTEGER)")
    conn.commit()
    conn.close()


def current_date():
    return time.strftime("%Y-%m-%d")


def get_user(user_id, first_name="", username=""):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    if user is None:
        conn.execute(
            "INSERT INTO users (user_id, first_name, username, free_used, free_date, created_at) VALUES (?, ?, ?, 0, ?, ?)",
            (user_id, first_name or "", username or "", current_date(), int(time.time()))
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    elif user["free_date"] != current_date():
        conn.execute("UPDATE users SET free_used=0, free_date=? WHERE user_id=?", (current_date(), user_id))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    conn.close()
    return user


def subscription_active(user):
    return user["subscription_until"] and user["subscription_until"] > int(time.time())


def get_subscription_price(user):
    if user["referrals"] >= 50 and user["paid_referrals"] >= 10:
        return 50
    if user["referrals"] >= 30:
        return 70
    return 100


def main_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("💳 Payment Methods"), KeyboardButton("👥 Referral"))
    markup.row(KeyboardButton("🤖 Models"), KeyboardButton("🔄 Restart"))
    markup.row(KeyboardButton("❓ Help"), KeyboardButton("📊 My Account"))
    return markup


def save_message(user_id, role, content):
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (user_id, role, content, int(time.time()))
    )
    conn.commit()
    conn.close()


def get_history(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (user_id,)
    ).fetchall()
    conn.close()
    rows = list(reversed(rows))
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def system_prompt():
    return (
        "You are BOSSAI, a natural all-in-one AI assistant. "
        "Speak naturally. Default language is English. "
        "If the user speaks Amharic, respond naturally in Amharic. "
        "If the user speaks another language, respond naturally in that language. "
        "Do not unnecessarily say that you are a bot. Do not use hashtag symbols. "
        "Be helpful, clear and natural. Remember relevant conversation context."
    )


CHAT_MODELS = {
    "DeepSeek": "deepseek/deepseek-chat",
    "GPT-4o": "openai/gpt-4o",
    "Claude": "anthropic/claude-3.5-sonnet",
    "Grok": "x-ai/grok-2-1212",
}


def ask_openrouter(user_id, text):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    user = get_user(user_id)
    model = user["model"]
    history = get_history(user_id)

    messages = [{"role": "system", "content": system_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": CHAT_MODELS.get(model, CHAT_MODELS["DeepSeek"]), "messages": messages},
        timeout=90
    )

    if not response.ok:
        raise RuntimeError(f"OpenRouter {response.status_code}: {response.text[:300]}")

    data = response.json()
    return data["choices"][0]["message"]["content"]


def ask_gemini(user_id, text):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")

    history = get_history(user_id)
    conversation = ""
    for item in history:
        conversation += item["role"] + ": " + item["content"] + "\n"

    prompt = system_prompt() + "\n\nPrevious conversation:\n" + conversation + "\n\nCurrent user message:\n" + text

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt)

    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")

    return response.text


def ask_ai(user_id, text):
    user = get_user(user_id)

    if user["model"] == "Gemini":
        return ask_gemini(user_id, text)

    try:
        return ask_openrouter(user_id, text)
    except Exception as openrouter_error:
        print("OpenRouter failed:", openrouter_error)
        if GEMINI_API_KEY:
            try:
                return ask_gemini(user_id, text)
            except Exception as gemini_error:
                print("Gemini fallback also failed:", gemini_error)
                raise RuntimeError(f"OpenRouter error: {openrouter_error} | Gemini error: {gemini_error}")
        raise


def typing_loop(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        stop_event.wait(4)


def send_long_message(chat_id, text):
    if not text:
        text = "Sorry, I could not generate a response."
    for i in range(0, len(text), 4000):
        bot.send_message(chat_id, text[i:i + 4000])


@bot.message_handler(commands=["start"])
def start(message):
    get_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    name = message.from_user.first_name or "there"

    text = (
        f"Hello {name}! Welcome to BOSSAI — your all-in-one AI assistant.\n\n"
        "Access GPT-4o, Claude, DeepSeek, Grok, and Gemini in one bot.\n\n"
        "I can:\n"
        "• Answer questions\n"
        "• Write and translate text\n"
        "• Write and debug code\n"
        "• Solve math problems\n"
        "• Remember conversations\n\n"
        f"Free: {FREE_LIMIT} messages per day\n"
        f"Unlimited: {MONTHLY_PRICE} ETB/month\n\n"
        "Use the buttons below."
    )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard())


@bot.message_handler(commands=["help"])
def help_command(message):
    bot.send_message(
        message.chat.id,
        "BOSSAI Help\n\n"
        "Chat: Send your question directly.\n"
        f"Free: {FREE_LIMIT} messages per day.\n"
        f"Unlimited: {MONTHLY_PRICE} ETB/month.\n"
        "Payment Methods: Choose Telebirr, Payoneer or PayPal.\n"
        "Referral: Invite users and receive discounts.\n"
        "Models: Choose your AI model.\n"
        "Restart: Clear your current conversation.\n\n"
        "Support: @Huss_moham"
    )


@bot.message_handler(func=lambda m: m.text == "❓ Help")
def help_button(message):
    help_command(message)


def show_payment_menu(message):
    user = get_user(message.from_user.id)
    price = get_subscription_price(user)

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(f"💳 Telebirr — {price} ETB/month", callback_data="telebirr"))
    markup.add(InlineKeyboardButton("🌍 Payoneer", callback_data="payoneer"))
    markup.add(InlineKeyboardButton("🅿️ PayPal", callback_data="paypal"))

    bot.send_message(message.chat.id, "Choose your payment method:", reply_markup=markup)


@bot.message_handler(commands=["menu"])
def menu_command(message):
    show_payment_menu(message)


@bot.message_handler(func=lambda m: m.text == "💳 Payment Methods")
def payment_button(message):
    show_payment_menu(message)


@bot.callback_query_handler(func=lambda call: call.data in ["telebirr", "payoneer", "paypal"])
def payment_callback(call):
    bot.answer_callback_query(call.id)

    if call.data == "telebirr":
        user = get_user(call.from_user.id)
        price = get_subscription_price(user)
        bot.send_message(
            call.message.chat.id,
            f"Telebirr Payment\n\n"
            f"Amount: {price} ETB/month\n\n"
            "Receiver: Hussein\n"
            "Telebirr: 0964990206\n\n"
            "After payment, send your payment receipt screenshot here.\n\n"
            "Your subscription will be activated after manual verification."
        )
    elif call.data == "payoneer":
        bot.send_message(call.message.chat.id, "Payoneer: Soon Available.")
    elif call.data == "paypal":
        bot.send_message(call.message.chat.id, "PayPal: Soon Available.")


@bot.message_handler(content_types=["photo"])
def payment_receipt(message):
    if ADMIN_ID == 0:
        bot.reply_to(message, "Receipt received. Admin verification is not configured yet.")
        return

    user = get_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    price = get_subscription_price(user)

    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO payments (user_id, amount, status, created_at) VALUES (?, ?, 'pending', ?)",
        (message.from_user.id, price, int(time.time()))
    )
    payment_id = cursor.lastrowid
    conn.commit()
    conn.close()

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{payment_id}:{message.from_user.id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{payment_id}:{message.from_user.id}")
    )

    caption = (
        f"Payment Receipt\n\nPayment ID: {payment_id}\n"
        f"User: {message.from_user.first_name}\n"
        f"Username: @{message.from_user.username or 'none'}\n"
        f"User ID: {message.from_user.id}\nAmount: {price} ETB\nStatus: Pending"
    )

    bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=caption, reply_markup=markup)
    bot.reply_to(message, "Your receipt has been sent for verification. Please wait for approval.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("approve:") or call.data.startswith("reject:"))
def payment_decision(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    parts = call.data.split(":")
    action = parts[0]
    payment_id = int(parts[1])
    user_id = int(parts[2])

    if action == "approve":
        until = int(time.time()) + 30 * 24 * 60 * 60
        conn = get_db()
        conn.execute("UPDATE payments SET status='approved' WHERE id=?", (payment_id,))
        conn.execute("UPDATE users SET subscription_until=? WHERE user_id=?", (until, user_id))

        referral = conn.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if referral and referral["referred_by"]:
            conn.execute(
                "UPDATE users SET paid_referrals = paid_referrals + 1 WHERE user_id=?",
                (referral["referred_by"],)
            )

        conn.commit()
        conn.close()

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(user_id, "Payment approved.\n\nYour unlimited subscription is active for 30 days.\n\nThank you for using BOSSAI.")
    else:
        conn = get_db()
        conn.execute("UPDATE payments SET status='rejected' WHERE id=?", (payment_id,))
        conn.commit()
        conn.close()

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(user_id, "Your payment receipt was rejected.\n\nPlease send a valid receipt again.\n\nSupport: @Huss_moham")


@bot.message_handler(func=lambda m: m.text == "👥 Referral")
def referral(message):
    user = get_user(message.from_user.id)
    bot_username = bot.get_me().username
    referral_link = f"https://t.me/{bot_username}?start=ref_{message.from_user.id}"
    price = get_subscription_price(user)

    bot.send_message(
        message.chat.id,
        f"Referral Program\n\nYour referral link:\n{referral_link}\n\n"
        f"30 referrals → 70 ETB/month\n"
        f"50 referrals + 10 paid referrals → 50 ETB/month\n\n"
        f"Your referrals: {user['referrals']}\n"
        f"Paid referrals: {user['paid_referrals']}\n\n"
        f"Current price: {price} ETB/month"
    )


@bot.message_handler(func=lambda m: m.text == "🤖 Models")
def models(message):
    markup = InlineKeyboardMarkup()
    for model in CHAT_MODELS:
        markup.add(InlineKeyboardButton(model, callback_data=f"model:{model}"))
    markup.add(InlineKeyboardButton("Gemini", callback_data="model:Gemini"))

    user = get_user(message.from_user.id)
    bot.send_message(message.chat.id, f"Current model: {user['model']}\n\nChoose a model:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("model:"))
def model_callback(call):
    bot.answer_callback_query(call.id)
    model = call.data.split(":", 1)[1]

    if model not in CHAT_MODELS and model != "Gemini":
        return

    conn = get_db()
    conn.execute("UPDATE users SET model=? WHERE user_id=?", (model, call.from_user.id))
    conn.commit()
    conn.close()

    bot.send_message(call.message.chat.id, f"Model changed to {model}.")


@bot.message_handler(func=lambda m: m.text == "📊 My Account")
def account(message):
    user = get_user(message.from_user.id)
    remaining = max(0, FREE_LIMIT - user["free_used"])

    if subscription_active(user):
        days = max(1, int((user["subscription_until"] - int(time.time())) / 86400))
        plan = f"Unlimited active\nApproximately {days} days remaining"
    else:
        plan = "Free plan"

    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    paid_users = conn.execute(
        "SELECT COUNT(*) AS count FROM users WHERE subscription_until > ?", (int(time.time()),)
    ).fetchone()["count"]
    conn.close()

    bot.send_message(
        message.chat.id,
        f"My Account\n\nPlan: {plan}\n"
        f"Free messages remaining today: {remaining}\n"
        f"Current model: {user['model']}\n"
        f"Referrals: {user['referrals']}\n"
        f"Paid referrals: {user['paid_referrals']}\n\n"
        f"Registered users: {total_users}\n"
        f"Active paid users: {paid_users}"
    )


@bot.message_handler(func=lambda m: m.text == "🔄 Restart")
def restart(message):
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE user_id=?", (message.from_user.id,))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, "Conversation restarted. You can start a new chat.", reply_markup=main_keyboard())


@bot.message_handler(content_types=["document", "voice", "audio"])
def file_handler(message):
    bot.reply_to(message, "I received your file.\n\nFile and voice analysis can be connected to the appropriate processing service.")


busy_users = set()
busy_lock = threading.Lock()
last_request = {}


@bot.message_handler(content_types=["text"])
def chat(message):
    text = message.text.strip()
    if not text or text.startswith("/"):
        return

    user_id = message.from_user.id
    now = time.time()
    previous = last_request.get(user_id, 0)
    if now - previous < 2:
        bot.reply_to(message, "⏳ Wait a moment before your next message.")
        return
    last_request[user_id] = now

    user = get_user(user_id, message.from_user.first_name, message.from_user.username)

    if not subscription_active(user):
        if user["free_used"] >= FREE_LIMIT:
            bot.reply_to(
                message,
                f"You have used all {FREE_LIMIT} free messages for today.\n\n"
                f"Unlimited access is {MONTHLY_PRICE} ETB/month.\n\n"
                "Open Payment Methods to continue."
            )
            return

        conn = get_db()
        conn.execute("UPDATE users SET free_used=free_used+1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

    with busy_lock:
        if user_id in busy_users:
            bot.reply_to(message, "⏳ Wait a moment, your previous message is still processing.")
            return
        busy_users.add(user_id)

    stop_event = threading.Event()
    typing_thread = threading.Thread(target=typing_loop, args=(message.chat.id, stop_event), daemon=True)
    typing_thread.start()

    try:
        save_message(user_id, "user", text)
        answer = ask_ai(user_id, text)
        save_message(user_id, "assistant", answer)
        send_long_message(message.chat.id, answer)
    except Exception as error:
        print("CHAT ERROR:", error)
        traceback.print_exc()
        bot.reply_to(message, f"⚠️ Debug info (temporary): {str(error)[:500]}")
    finally:
        stop_event.set()
        with busy_lock:
            busy_users.discard(user_id)


def startup_diagnostic():
    if ADMIN_ID == 0:
        return

    def status_line(name, value):
        if not value:
            return f"❌ {name}: NOT SET"
        tail = value[-4:] if len(value) >= 4 else value
        return f"✅ {name}: set (…{tail}, length {len(value)})"

    report = "🔧 BOSSAI Startup Diagnostic\n\n"
    report += status_line("TELEGRAM_BOT_TOKEN", TOKEN) + "\n"
    report += status_line("GEMINI_API_KEY", GEMINI_API_KEY) + "\n"
    report += status_line("OPENROUTER_API_KEY", OPENROUTER_API_KEY) + "\n"
    report += f"✅ ADMIN_ID: {ADMIN_ID}\n"

    try:
        bot.send_message(ADMIN_ID, report)
    except Exception as e:
        print("Could not send startup diagnostic:", e)


def main():
    init_database()
    print("BOSSAI is running...")
    startup_diagnostic()
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as error:
            print("Polling error:", error)
            time.sleep(5)


if __name__ == "__main__":
    main()
