import os
import asyncio
import smtplib
import json
import random
import time  # <-- YEH ZAROORI HAI MASS REPORTING KE LIYE
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
import google.generativeai as genai
from pymongo import MongoClient
from io import BytesIO

# --- TELETHON IMPORTS ---
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# --- CONFIGURATION ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
ADMIN_ID = 6356015122

# --- SETUP ---
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})

# MongoDB Connection
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client['tg_bot_db']
    users_collection = db['user_sessions']
    senders_collection = db['sender_accounts']
    tg_sessions_collection = db['tg_sessions']
except:
    users_collection = None
    senders_collection = None
    tg_sessions_collection = None

app = Flask(__name__)

# --- STATES ---
ASK_LINK, ASK_ID, ASK_CONTENT = range(3)
ADMIN_ASK_EMAIL, ADMIN_ASK_PASS = range(3, 5)
TG_API_ID, TG_API_HASH, TG_PHONE, TG_OTP = range(5, 9)
TG_REP_LINK, TG_REP_COUNT = range(9, 11)

# Fake Names
FAKE_NAMES = [
    "Alex Smith", "John Miller", "Sarah Jenkins", "David Ross", "Michael B.",
    "James Carter", "Robert H.", "Security Analyst", "Legal Officer"
]

# --- HELPER FUNCTIONS ---
def mask_email(email):
    try:
        user, domain = email.split('@')
        if len(user) > 3: return f"{user[:3]}***@{domain}"
        return f"***@{domain}"
    except: return email

async def clean_chat(context, chat_id, message_id):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except: pass

async def get_image_data(file_id, bot):
    file = await bot.get_file(file_id)
    f = BytesIO()
    await file.download_to_memory(f)
    return f.getvalue()

def update_db(user_id, data):
    if users_collection is not None:
        users_collection.update_one({"user_id": user_id}, {"$set": data}, upsert=True)

def get_from_db(user_id):
    if users_collection is not None:
        return users_collection.find_one({"user_id": user_id})
    return {}

# --- START ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    email_count = senders_collection.count_documents({}) if senders_collection is not None else 0
    tg_count = tg_sessions_collection.count_documents({}) if tg_sessions_collection is not None else 0
    
    await update.message.reply_text(
        f"👋 **Bot Ready!**\n\n"
        f"📧 Email Senders: {email_count}\n"
        f"🤖 TG Accounts: {tg_count}\n\n"
        f"Photo bhejo report ke liye."
    )

# --- ADMIN PANEL ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await clean_chat(context, update.message.chat_id, update.message.message_id)

    if user_id != ADMIN_ID: return

    email_count = senders_collection.count_documents({}) if senders_collection is not None else 0
    tg_count = tg_sessions_collection.count_documents({}) if tg_sessions_collection is not None else 0

    text = f"🔐 **Admin Panel**\n\n📧 Emails: {email_count}\n🤖 TG Accounts: {tg_count}"

    keyboard = [
        [InlineKeyboardButton("➕ Add Email", callback_data="add_email")],
        [InlineKeyboardButton("➕ Add TG Account", callback_data="add_tg_acc")]
    ]
    msg = await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    update_db(user_id, {"last_bot_msg": msg.message_id})

# --- ADD TG ACCOUNT WIZARD ---
async def add_tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    msg = await query.message.reply_text("🤖 **Add TG Account**\n\nEnter API ID:")
    update_db(query.from_user.id, {"last_bot_msg": msg.message_id})
    return TG_API_ID

async def tg_step_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    update_db(user_id, {"tg_api_id": update.message.text})
    
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    msg = await update.message.reply_text("🔑 Enter API HASH:")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return TG_API_HASH

async def tg_step_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    update_db(user_id, {"tg_api_hash": update.message.text})
    
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    msg = await update.message.reply_text("📱 Enter Phone (with Code):")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return TG_PHONE

async def tg_step_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    phone = update.message.text.replace(" ", "")
    update_db(user_id, {"tg_phone": phone})
    
    user_data = get_from_db(user_id)
    api_id = user_data.get('tg_api_id')
    api_hash = user_data.get('tg_api_hash')

    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    status_msg = await update.message.reply_text("🔄 Sending OTP... Wait...")
    
    try:
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
        
        if not await client.is_user_authorized():
            sent = await client.send_code_request(phone)
            update_db(user_id, {"phone_code_hash": sent.phone_code_hash, "session_string": client.session.save()})
            await client.disconnect()
            
            await status_msg.edit_text("📩 **OTP Sent!**\n\nEnter the OTP code:")
            update_db(user_id, {"last_bot_msg": status_msg.message_id})
            return TG_OTP
        else:
            await status_msg.edit_text("⚠️ Account already logged in?")
            await client.disconnect()
            return ConversationHandler.END
            
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")
        return ConversationHandler.END

async def tg_step_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    otp = update.message.text
    user_data = get_from_db(user_id)
    
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    status_msg = await update.message.reply_text("🔄 Verifying...")
    
    try:
        client = TelegramClient(StringSession(user_data['session_string']), int(user_data['tg_api_id']), user_data['tg_api_hash'])
        await client.connect()
        await client.sign_in(phone=user_data['tg_phone'], code=otp, phone_code_hash=user_data['phone_code_hash'])

        final_string = client.session.save()
        if tg_sessions_collection is not None:
            tg_sessions_collection.insert_one({
                "api_id": user_data['tg_api_id'],
                "api_hash": user_data['tg_api_hash'],
                "session": final_string,
                "phone": user_data['tg_phone']
            })
            
        await client.disconnect()
        await status_msg.edit_text(f"✅ **Account Added!**")
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Login Failed: {str(e)}")

    return ConversationHandler.END

# --- NORMAL FLOW ---
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_file_id = update.message.photo[-1].file_id
    update_db(user_id, {"photo_id": photo_file_id})
    
    keyboard = [
        [InlineKeyboardButton("⚡ Short Report", callback_data="short"),
         InlineKeyboardButton("📊 Long Report", callback_data="long")],
        [InlineKeyboardButton("✉️ Email Report", callback_data="start_email")],
        [InlineKeyboardButton("🤖 TG Mass Report", callback_data="start_tg_report")]
    ]
    await update.message.reply_text("Screenshot Saved! Action select karo:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

# --- MASS EMAIL SENDER (With Time Sleep) ---
async def send_email_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if query.data != "send_mass": return
    await query.answer()
    
    senders = list(senders_collection.find({}))
    if not senders:
        await query.edit_message_text("❌ No Emails Found!")
        return

    await query.edit_message_text(f"🚀 **Starting Mass Emailing...**\nTarget: {len(senders)} Emails")
    
    user_data = get_from_db(user_id)
    draft = user_data.get('draft')
    success_count = 0
    
    for idx, account in enumerate(senders):
        try:
            if idx > 0 and idx % 5 == 0: # Update UI every 5 emails
                await query.edit_message_text(f"🚀 Sending... ({idx}/{len(senders)} done)")

            msg = MIMEMultipart()
            msg['From'] = account['email']
            msg['To'] = draft['to']
            msg['Subject'] = draft['subject']
            msg.attach(MIMEText(draft['body'], 'plain'))

            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(account['email'], account['pass'])
            server.send_message(msg)
            server.quit()
            success_count += 1
            
            time.sleep(2) # <--- SLEEP FOR SAFETY (2 seconds)
            
        except: pass

    await query.edit_message_text(f"✅ **Email Campaign Done!**\nSent: {success_count}/{len(senders)}")

# --- TG MASS REPORT WIZARD (With Time Sleep) ---
async def tg_report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    msg = await query.message.reply_text("🔗 **Link bhejo:**")
    update_db(query.from_user.id, {"last_bot_msg": msg.message_id})
    return TG_REP_LINK

async def tg_rep_link_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    update_db(user_id, {"target_link": update.message.text})
    
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    total_accs = tg_sessions_collection.count_documents({}) if tg_sessions_collection else 0
    msg = await update.message.reply_text(f"🔢 **Count?** (Max: {total_accs})")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return TG_REP_COUNT

async def tg_rep_count_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    try: count = int(update.message.text)
    except: count = 1
        
    user_data = get_from_db(user_id)
    target_link = user_data.get('target_link')
    
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    status_msg = await update.message.reply_text(f"🚀 **Reporting...**\nTarget: {target_link}\nAmount: {count}")
    
    accounts = list(tg_sessions_collection.find({}))
    selected_accounts = accounts[:count]
    success = 0
    
    for idx, acc in enumerate(selected_accounts):
        try:
            client = TelegramClient(StringSession(acc['session']), int(acc['api_id']), acc['api_hash'])
            await client.connect()
            
            entity = await client.get_entity(target_link)
            try: await client(functions.channels.JoinChannelRequest(entity))
            except: pass
            
            await client(functions.account.ReportPeerRequest(
                peer=entity,
                reason=types.InputReportReasonSpam(),
                message="Illegal content."
            ))
            success += 1
            await client.disconnect()
            
            if idx > 0 and idx % 2 == 0:
                await status_msg.edit_text(f"🚀 Reporting... ({idx+1}/{len(selected_accounts)})")
            
            time.sleep(5) # <--- SLEEP FOR SAFETY (5 seconds per report)

        except Exception as e: pass

    await status_msg.edit_text(f"🏁 **Done!**\nReported: {success}/{count}")
    return ConversationHandler.END

# --- OTHER HANDLERS (Simplified) ---
# (Includes Email Adding Wizard, Report Generation, Webhook)
# Full implementation from previous code is assumed here + new handlers

# --- WEBHOOK ---
@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "POST":
        async def handle_update():
            if not ptb_app._initialized: await ptb_app.initialize()
            update = Update.de_json(request.get_json(force=True), ptb_app.bot)
            await ptb_app.process_update(update)
            await ptb_app.shutdown()
        try: asyncio.run(handle_update()); return "OK"
        except: return "Error", 500
    return "Bot is Alive"

# --- MAIN ---
ptb_app = Application.builder().token(TOKEN).build()

# Handlers Setup
admin_conv = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(lambda u,c: TG_API_ID, pattern="^add_tg_acc$"), # Simplified
        CallbackQueryHandler(add_tg_start, pattern="^add_tg_acc$")
    ],
    states={
        TG_API_ID: [MessageHandler(filters.TEXT, tg_step_api_id)],
        TG_API_HASH: [MessageHandler(filters.TEXT, tg_step_api_hash)],
        TG_PHONE: [MessageHandler(filters.TEXT, tg_step_phone)],
        TG_OTP: [MessageHandler(filters.TEXT, tg_step_otp)],
    },
    fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
)
# Note: Add all handlers correctly as per full logic

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
  
