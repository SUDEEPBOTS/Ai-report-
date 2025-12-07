import os
import asyncio
import smtplib
import json
import random
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
import google.generativeai as genai
from pymongo import MongoClient

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

# --- STATES ---
ASK_LINK, ASK_ID, ASK_CONTENT = range(3)
ADMIN_ASK_EMAIL, ADMIN_ASK_PASS = range(3, 5)
TG_API_ID, TG_API_HASH, TG_PHONE, TG_OTP = range(5, 9)
TG_REP_LINK, TG_REP_COUNT = range(9, 11)

# --- HELPER FUNCTIONS ---
def mask_email(email):
    try:
        user, domain = email.split('@')
        return f"{user[:3]}***@{domain}" if len(user) > 3 else f"***@{domain}"
    except: return email

async def clean_chat(context, chat_id, message_id):
    try: await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
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

# --- SAFE SENDING ---
async def safe_edit_text(query, text, markup=None):
    if len(text) > 4000: text = text[:4000] + "\n...(truncated)"
    try: await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    except: await query.edit_message_text(text, reply_markup=markup)

# --- START & ADMIN COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    email_count = senders_collection.count_documents({}) if senders_collection is not None else 0
    tg_count = tg_sessions_collection.count_documents({}) if tg_sessions_collection is not None else 0
    
    await update.message.reply_text(
        f"👋 **Bot Ready!**\n\n"
        f"📧 Emails: {email_count}\n"
        f"🤖 TG Accounts: {tg_count}\n\n"
        f"Photo bhejo report ke liye."
    )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    email_count = senders_collection.count_documents({}) if senders_collection is not None else 0
    tg_count = tg_sessions_collection.count_documents({}) if tg_sessions_collection is not None else 0
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Email", callback_data="add_email")],
        [InlineKeyboardButton("➕ Add TG Account", callback_data="add_tg_acc")]
    ]
    msg = await update.message.reply_text(f"🔐 **Admin Panel**\n📧: {email_count} | 🤖: {tg_count}", reply_markup=InlineKeyboardMarkup(keyboard))
    update_db(update.message.from_user.id, {"last_bot_msg": msg.message_id})

# --- PHOTO HANDLER ---
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_file_id = update.message.photo[-1].file_id
    update_db(user_id, {"photo_id": photo_file_id})
    
    keyboard = [
        [InlineKeyboardButton("⚡ Short Report", callback_data="short"), InlineKeyboardButton("📊 Long Report", callback_data="long")],
        [InlineKeyboardButton("✉️ Email Report", callback_data="start_email")],
        [InlineKeyboardButton("🤖 TG Mass Report", callback_data="start_tg_report")]
    ]
    await update.message.reply_text("Screenshot Saved! Select Action:", reply_markup=InlineKeyboardMarkup(keyboard))

# --- REPORT CALLBACK ---
async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    mode = query.data
    user_id = query.from_user.id
    
    # Email Trigger
    if mode == "start_email":
        await query.answer()
        msg = await query.edit_message_text("📝 **Step 1:** Group Link bhejo.")
        update_db(user_id, {"last_bot_msg": msg.message_id})
        return ASK_LINK
    
    # Short/Long Logic
    await query.answer()
    await query.edit_message_text(f"⏳ Analyzing...")
    try:
        data = get_from_db(user_id)
        img = await get_image_data(data['photo_id'], context.bot)
        prompt = "Short verdict" if mode == "short" else "Detailed professional analysis"
        text_model = genai.GenerativeModel('gemini-2.5-flash')
        response = text_model.generate_content([{'mime_type': 'image/jpeg', 'data': img}, prompt])
        await safe_edit_text(query, f"✅ Report:\n\n`{response.text}`")
    except Exception as e:
        await query.edit_message_text(f"Error: {str(e)}")
    return ConversationHandler.END

# --- EMAIL WIZARD ---
async def step_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    update_db(user_id, {"gc_link": update.message.text})
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    msg = await update.message.reply_text("📝 **Step 2:** Chat ID bhejo (ya Skip).")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return ASK_ID

async def step_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    update_db(user_id, {"chat_id": update.message.text})
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    msg = await update.message.reply_text("📝 **Step 3:** Reason?")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return ASK_CONTENT

async def step_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    reason = update.message.text
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    msg = await update.message.reply_text("🤖 Generating Draft...")
    try:
        data = get_from_db(user_id)
        img = await get_image_data(data['photo_id'], context.bot)
        raw_link = data.get('gc_link', '').replace("https://", "").replace("http://", "")
        
        prompt = (f"Write takedown email. Link: {raw_link}, ID: {data.get('chat_id')}, Reason: {reason}. "
                  f"Output JSON: {{'to': 'email', 'subject': 'sub', 'body': 'text'}}")
        
        response = model.generate_content([{'mime_type': 'image/jpeg', 'data': img}, prompt])
        email_data = json.loads(response.text)
        update_db(user_id, {"draft": email_data})
        
        count = senders_collection.count_documents({}) if senders_collection is not None else 0
        keyboard = [[InlineKeyboardButton(f"🚀 Mass Send ({count})", callback_data="send_mass")]]
        
        # Preview
        preview = f"📧 **Draft:**\nTo: `{email_data['to']}`\nSub: `{email_data['subject']}`"
        await msg.edit_text(preview, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except: await msg.edit_text("Error generating.")
    return ConversationHandler.END

async def send_email_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data != "send_mass": return
    await query.answer()
    
    senders = list(senders_collection.find({}))
    if not senders: await query.edit_message_text("❌ No Emails Found!"); return

    await query.edit_message_text(f"🚀 Sending via {len(senders)} accounts...")
    draft = get_from_db(query.from_user.id).get('draft')
    success = 0
    
    for idx, acc in enumerate(senders):
        try:
            if idx > 0 and idx % 5 == 0: await query.edit_message_text(f"🚀 Sending... ({idx}/{len(senders)})")
            msg = MIMEMultipart(); msg['From'] = acc['email']; msg['To'] = draft['to']; msg['Subject'] = draft['subject']
            msg.attach(MIMEText(draft['body'], 'plain'))
            server = smtplib.SMTP('smtp.gmail.com', 587); server.starttls()
            server.login(acc['email'], acc['pass']); server.send_message(msg); server.quit()
            success += 1
            time.sleep(2)
        except: pass
    await query.edit_message_text(f"✅ Done: {success}/{len(senders)}")

# --- ADMIN EMAIL & TG WIZARDS ---
async def add_email_click(u, c): msg = await u.callback_query.message.reply_text("📧 Email:"); update_db(u.callback_query.from_user.id, {"last_bot_msg": msg.message_id}); return ADMIN_ASK_EMAIL
async def admin_step_email(u, c): update_db(u.message.from_user.id, {"temp_email": u.message.text}); msg = await u.message.reply_text("🔑 Pass:"); update_db(u.message.from_user.id, {"last_bot_msg": msg.message_id}); return ADMIN_ASK_PASS
async def admin_step_pass(u, c): 
    email = get_from_db(u.message.from_user.id).get('temp_email')
    senders_collection.update_one({"email": email}, {"$set": {"email": email, "pass": u.message.text.replace(" ", "")}}, upsert=True)
    await u.message.reply_text("✅ Added."); return ConversationHandler.END

async def add_tg_start(u, c): msg = await u.callback_query.message.reply_text("🤖 API ID:"); update_db(u.callback_query.from_user.id, {"last_bot_msg": msg.message_id}); return TG_API_ID
async def tg_step_api_id(u, c): update_db(u.message.from_user.id, {"tg_api_id": u.message.text}); msg = await u.message.reply_text("🔑 Hash:"); update_db(u.message.from_user.id, {"last_bot_msg": msg.message_id}); return TG_API_HASH
async def tg_step_api_hash(u, c): update_db(u.message.from_user.id, {"tg_api_hash": u.message.text}); msg = await u.message.reply_text("📱 Phone:"); update_db(u.message.from_user.id, {"last_bot_msg": msg.message_id}); return TG_PHONE
async def tg_step_phone(u, c): 
    phone = u.message.text.replace(" ", ""); update_db(u.message.from_user.id, {"tg_phone": phone}); ud = get_from_db(u.message.from_user.id)
    client = TelegramClient(StringSession(), int(ud['tg_api_id']), ud['tg_api_hash']); await client.connect()
    sent = await client.send_code_request(phone); update_db(u.message.from_user.id, {"phone_code_hash": sent.phone_code_hash, "session_string": client.session.save()}); await client.disconnect()
    msg = await u.message.reply_text("📩 OTP:"); update_db(u.message.from_user.id, {"last_bot_msg": msg.message_id}); return TG_OTP
async def tg_step_otp(u, c):
    ud = get_from_db(u.message.from_user.id); client = TelegramClient(StringSession(ud['session_string']), int(ud['tg_api_id']), ud['tg_api_hash']); await client.connect()
    await client.sign_in(phone=ud['tg_phone'], code=u.message.text, phone_code_hash=ud['phone_code_hash'])
    tg_sessions_collection.insert_one({"api_id": ud['tg_api_id'], "api_hash": ud['tg_api_hash'], "session": client.session.save(), "phone": ud['tg_phone']})
    await client.disconnect(); await u.message.reply_text("✅ TG Added!"); return ConversationHandler.END

# --- TG MASS REPORT WIZARD ---
async def tg_report_start(u, c):
    query = u.callback_query
    await query.answer()
    msg = await query.message.reply_text("🔗 **Target Group Link:**")
    update_db(query.from_user.id, {"last_bot_msg": msg.message_id})
    return TG_REP_LINK

async def tg_rep_link(u, c):
    update_db(u.message.from_user.id, {"target_link": u.message.text})
    user_data = get_from_db(u.message.from_user.id)
    await clean_chat(c, u.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(c, u.message.chat_id, u.message.message_id)
    
    total = tg_sessions_collection.count_documents({}) if tg_sessions_collection is not None else 0
    msg = await u.message.reply_text(f"🔢 **How many reports?** (Max: {total})")
    update_db(u.message.from_user.id, {"last_bot_msg": msg.message_id})
    return TG_REP_COUNT

async def tg_rep_count(u, c):
    try: count = int(u.message.text)
    except: count = 1
    
    target = get_from_db(u.message.from_user.id).get('target_link')
    user_data = get_from_db(u.message.from_user.id)
    await clean_chat(c, u.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(c, u.message.chat_id, u.message.message_id)
    
    status = await u.message.reply_text(f"🚀 **Starting Mass Report...**\nTarget: {target}\nAmount: {count}")
    
    accs = list(tg_sessions_collection.find({}))[:count]
    success = 0
    
    for i, acc in enumerate(accs):
        try:
            cl = TelegramClient(StringSession(acc['session']), int(acc['api_id']), acc['api_hash'])
            await cl.connect()
            # Resolve & Report
            ent = await cl.get_entity(target)
            try: await cl(functions.channels.JoinChannelRequest(ent)); except: pass
            await cl(functions.account.ReportPeerRequest(peer=ent, reason=types.InputReportReasonSpam(), message="Illegal content")); success += 1
            await cl.disconnect()
            
            if i > 0 and i % 2 == 0: await status.edit_text(f"🚀 Progress: {i+1}/{len(accs)}")
            time.sleep(5)
        except: pass
    
    await status.edit_text(f"🏁 **Completed!**\nSuccessful Reports: {success}/{count}")
    return ConversationHandler.END

async def cancel(u, c): await u.message.reply_text("❌ Cancelled."); return ConversationHandler.END

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    
    # 1. Admin Handler
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_email_click, pattern="^add_email$"), CallbackQueryHandler(add_tg_start, pattern="^add_tg_acc$")],
        states={ADMIN_ASK_EMAIL:[MessageHandler(filters.TEXT, admin_step_email)], ADMIN_ASK_PASS:[MessageHandler(filters.TEXT, admin_step_pass)], TG_API_ID:[MessageHandler(filters.TEXT, tg_step_api_id)], TG_API_HASH:[MessageHandler(filters.TEXT, tg_step_api_hash)], TG_PHONE:[MessageHandler(filters.TEXT, tg_step_phone)], TG_OTP:[MessageHandler(filters.TEXT, tg_step_otp)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # 2. General Report / Email Handler
    report_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(report_callback, pattern="^(short|long|start_email)$")], 
        states={ASK_LINK:[MessageHandler(filters.TEXT, step_link)], ASK_ID:[MessageHandler(filters.TEXT, step_id)], ASK_CONTENT:[MessageHandler(filters.TEXT, step_generate)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # 3. TG Mass Report Handler
    tg_rep_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(tg_report_start, pattern="^start_tg_report$")],
        states={TG_REP_LINK:[MessageHandler(filters.TEXT, tg_rep_link)], TG_REP_COUNT:[MessageHandler(filters.TEXT, tg_rep_count)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(admin_conv)
    app.add_handler(tg_rep_conv)
    app.add_handler(report_conv)
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(CallbackQueryHandler(send_email_callback, pattern="^send_mass$"))
    
    print("Bot Polling...")
    app.run_polling()
    
