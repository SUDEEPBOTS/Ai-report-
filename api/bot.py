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

# --- GLOBAL VARIABLES ---
active_timers = {} # To store running tasks

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
TG_ASK_LINK, TG_ASK_MODE, TG_ASK_COUNT = range(9, 12)
TG_TIMER_LINK = 13 # New State for Timer

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

async def safe_edit_text(query, text, markup=None):
    if len(text) > 4000: text = text[:4000] + "\n...(truncated)"
    try: await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    except: await query.edit_message_text(text, reply_markup=markup)

# --- START & ADMIN ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    e_c = senders_collection.count_documents({}) if senders_collection is not None else 0
    t_c = tg_sessions_collection.count_documents({}) if tg_sessions_collection is not None else 0
    await update.message.reply_text(f"👋 **Bot Ready!**\n📧 Emails: {e_c}\n🤖 Accounts: {t_c}\n\nPhoto bhejo report ke liye.")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    keyboard = [[InlineKeyboardButton("➕ Add Email", callback_data="add_email")], [InlineKeyboardButton("➕ Add TG Account", callback_data="add_tg_acc")]]
    msg = await update.message.reply_text(f"🔐 **Admin Panel**", reply_markup=InlineKeyboardMarkup(keyboard))
    update_db(update.message.from_user.id, {"last_bot_msg": msg.message_id})

# --- ADMIN WIZARDS ---
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
    # --- PHOTO HANDLER ---
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_file_id = update.message.photo[-1].file_id
    update_db(user_id, {"photo_id": photo_file_id})
    
    keyboard = [
        [InlineKeyboardButton("⚡ Short Report", callback_data="short"), InlineKeyboardButton("📊 Long Report", callback_data="long")],
        [InlineKeyboardButton("✉️ Email Report", callback_data="start_email")],
        [InlineKeyboardButton("🤖 TG Mass Report", callback_data="start_tg_report"), InlineKeyboardButton("⏳ Timer Report", callback_data="start_timer")]
    ]
    await update.message.reply_text("Select Action:", reply_markup=InlineKeyboardMarkup(keyboard))

# --- INDEPENDENT HANDLERS (Short/Long) ---
async def button_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    mode = query.data
    user_id = query.from_user.id
    
    if mode in ["short", "long"]:
        await query.answer()
        await query.edit_message_text(f"⏳ Analyzing...")
        try:
            data = get_from_db(user_id)
            img = await get_image_data(data['photo_id'], context.bot)
            prompt = "Short verdict" if mode == "short" else "Detailed analysis"
            response = model.generate_content([{'mime_type': 'image/jpeg', 'data': img}, prompt])
            await safe_edit_text(query, f"✅ Report:\n\n`{response.text}`")
        except: await query.edit_message_text("Error.")
        return ConversationHandler.END
    return ConversationHandler.END

# --- NEW: TIMER REPORT LOGIC ---
async def timer_start(u, c):
    await u.callback_query.answer()
    msg = await u.callback_query.edit_message_text("⏳ **Timer Mode:**\nTarget GC Link bhejo:")
    update_db(u.callback_query.from_user.id, {"last_bot_msg": msg.message_id})
    return TG_TIMER_LINK

async def timer_logic_start(u, c):
    user_id = u.message.from_user.id
    target = u.message.text
    
    # Check if timer already runs
    if user_id in active_timers:
        await u.message.reply_text("⚠️ Ek Timer pehle se chal raha hai! Pehle use roko.")
        return ConversationHandler.END

    # Start Background Task
    msg = await u.message.reply_text(f"✅ **Timer Started!**\nTarget: {target}\nReporting every 30s...", 
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop Timer", callback_data="stop_timer")]]))
    
    task = asyncio.create_task(run_timer_background(c.bot, user_id, target))
    active_timers[user_id] = task
    return ConversationHandler.END

async def run_timer_background(bot, user_id, target):
    try:
        round_count = 1
        while True:
            # Report Logic
            accs = list(tg_sessions_collection.find({}))
            if not accs: 
                await bot.send_message(user_id, "❌ No Accounts Found! Timer Stopped.")
                break
            
            success = 0
            for acc in accs:
                try:
                    cl = TelegramClient(StringSession(acc['session']), int(acc['api_id']), acc['api_hash'])
                    await cl.connect()
                    ent = await cl.get_entity(target)
                    try: await cl(functions.channels.JoinChannelRequest(ent)); except: pass
                    await cl(functions.account.ReportPeerRequest(peer=ent, reason=types.InputReportReasonSpam(), message="Illegal content"))
                    success += 1
                    await cl.disconnect()
                except: pass
            
            # Send DM Update
            try: await bot.send_message(user_id, f"⏰ **Round {round_count} Done!**\n✅ Reports Sent: {success}\nWaiting 30s...")
            except: pass # User blocked bot?
            
            round_count += 1
            await asyncio.sleep(30) # 30 Second Gap
            
    except asyncio.CancelledError:
        pass # Task stopped normally
    finally:
        if user_id in active_timers: del active_timers[user_id]

async def stop_timer_callback(u, c):
    query = u.callback_query
    user_id = query.from_user.id
    if user_id in active_timers:
        active_timers[user_id].cancel()
        del active_timers[user_id]
        await query.answer("Stopped!")
        await query.edit_message_text("🛑 **Timer Stopped Successfully.**")
    else:
        await query.answer("No active timer found.")

# --- TG MASS REPORT (Regular vs Multiple) ---
async def tg_report_start(u, c):
    await u.callback_query.answer()
    msg = await u.callback_query.edit_message_text("🔗 **Target Group Link:**")
    update_db(u.callback_query.from_user.id, {"last_bot_msg": msg.message_id})
    return TG_ASK_LINK

async def tg_ask_link(u, c):
    update_db(u.message.from_user.id, {"target_link": u.message.text})
    keyboard = [[InlineKeyboardButton("🔹 Regular (1x)", callback_data="mode_reg"), InlineKeyboardButton("🔁 Multiple (Loop)", callback_data="mode_mul")]]
    msg = await u.message.reply_text("⚙️ **Select Mode:**", reply_markup=InlineKeyboardMarkup(keyboard))
    update_db(u.message.from_user.id, {"last_bot_msg": msg.message_id})
    return TG_ASK_MODE

async def tg_mode_select(u, c):
    query = u.callback_query; await query.answer()
    if query.data == "mode_reg":
        update_db(query.from_user.id, {"loop_count": 1}); return await execute_tg_logic(query, c, query.from_user.id)
    msg = await query.edit_message_text("🔢 **Per Account Reports?**"); update_db(query.from_user.id, {"last_bot_msg": msg.message_id}); return TG_ASK_COUNT

async def tg_set_count(u, c):
    try: count = int(u.message.text)
    except: count = 1
    update_db(u.message.from_user.id, {"loop_count": count}); return await execute_tg_logic(u.message, c, u.message.from_user.id)

async def execute_tg_logic(obj, context, user_id):
    data = get_from_db(user_id); target = data.get('target_link'); loop_count = data.get('loop_count', 1)
    status_msg = await obj.reply_text(f"🚀 **Starting...**\nTarget: {target}\nMode: {loop_count}x per acc")
    
    accs = list(tg_sessions_collection.find({}))
    success = 0; failed = 0
    
    for acc in accs:
        try:
            cl = TelegramClient(StringSession(acc['session']), int(acc['api_id']), acc['api_hash']); await cl.connect()
            ent = await cl.get_entity(target)
            try: await cl(functions.channels.JoinChannelRequest(ent)); except: pass
            
            for _ in range(loop_count):
                try:
                    await cl(functions.account.ReportPeerRequest(peer=ent, reason=types.InputReportReasonSpam(), message="Illegal")); success += 1
                except: failed += 1
                if (success + failed) % 2 == 0:
                    try: await status_msg.edit_text(f"📡 **Live:**\n✅: {success} | ❌: {failed}")
                    except: pass
                await asyncio.sleep(2)
            await cl.disconnect(); await asyncio.sleep(2)
        except: failed += loop_count
    
    await status_msg.edit_text(f"🏁 **Finished!**\n✅ Success: {success}\n❌ Failed: {failed}")
    return ConversationHandler.END

# --- EMAIL FLOW (Re-added) ---
async def start_email_flow(u, c): await u.callback_query.answer(); msg = await u.callback_query.edit_message_text("📝 Link:"); update_db(u.callback_query.from_user.id, {"last_bot_msg": msg.message_id}); return ASK_LINK
async def step_link(u, c): update_db(u.message.from_user.id, {"gc_link": u.message.text}); msg = await u.message.reply_text("📝 Chat ID:"); update_db(u.message.from_user.id, {"last_bot_msg": msg.message_id}); return ASK_ID
async def step_id(u, c): update_db(u.message.from_user.id, {"chat_id": u.message.text}); msg = await u.message.reply_text("📝 Reason?"); update_db(u.message.from_user.id, {"last_bot_msg": msg.message_id}); return ASK_CONTENT
async def step_generate(u, c):
    msg = await u.message.reply_text("🤖 Generating..."); user_id = u.message.from_user.id
    try:
        data = get_from_db(user_id); img = await get_image_data(data['photo_id'], c.bot); raw = data.get('gc_link', '').replace("https://","")
        prompt = f"Write takedown email. Link: {raw}, ID: {data.get('chat_id')}, Reason: {u.message.text}. Output JSON: {{'to': 'email', 'subject': 'sub', 'body': 'text'}}"
        response = model.generate_content([{'mime_type': 'image/jpeg', 'data': img}, prompt])
        email_data = json.loads(response.text); update_db(user_id, {"draft": email_data})
        count = senders_collection.count_documents({}); kb = [[InlineKeyboardButton(f"🚀 Send ({count})", callback_data="send_mass")]]
        await msg.edit_text(f"📧 **Draft:**\nTo: `{email_data['to']}`", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    except: await msg.edit_text("Error.")
    return ConversationHandler.END

async def send_email_action(u, c):
    if u.callback_query.data != "send_mass": return
    await u.callback_query.answer(); senders = list(senders_collection.find({}))
    await u.callback_query.edit_message_text(f"🚀 Sending...")
    draft = get_from_db(u.callback_query.from_user.id).get('draft'); success = 0
    for acc in senders:
        try:
            s = smtplib.SMTP('smtp.gmail.com', 587); s.starttls(); s.login(acc['email'], acc['pass'])
            m = MIMEMultipart(); m['From'] = acc['email']; m['To'] = draft['to']; m['Subject'] = draft['subject']; m.attach(MIMEText(draft['body'], 'plain'))
            s.send_message(m); s.quit(); success += 1; await asyncio.sleep(2)
        except: pass
    await u.callback_query.edit_message_text(f"✅ Email Done: {success}/{len(senders)}")

async def cancel(u, c): await u.message.reply_text("❌ Cancelled."); return ConversationHandler.END

# --- MAIN ---
if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    
    admin_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_email_click, pattern="^add_email$"), CallbackQueryHandler(add_tg_start, pattern="^add_tg_acc$")], states={ADMIN_ASK_EMAIL:[MessageHandler(filters.TEXT, admin_step_email)], ADMIN_ASK_PASS:[MessageHandler(filters.TEXT, admin_step_pass)], TG_API_ID:[MessageHandler(filters.TEXT, tg_step_api_id)], TG_API_HASH:[MessageHandler(filters.TEXT, tg_step_api_hash)], TG_PHONE:[MessageHandler(filters.TEXT, tg_step_phone)], TG_OTP:[MessageHandler(filters.TEXT, tg_step_otp)]}, fallbacks=[CommandHandler('cancel', cancel)])
    email_conv = ConversationHandler(entry_points=[CallbackQueryHandler(start_email_flow, pattern="^start_email$")], states={ASK_LINK:[MessageHandler(filters.TEXT, step_link)], ASK_ID:[MessageHandler(filters.TEXT, step_id)], ASK_CONTENT:[MessageHandler(filters.TEXT, step_generate)]}, fallbacks=[CommandHandler('cancel', cancel)])
    tg_conv = ConversationHandler(entry_points=[CallbackQueryHandler(tg_report_start, pattern="^start_tg_report$")], states={TG_ASK_LINK:[MessageHandler(filters.TEXT, tg_ask_link)], TG_ASK_MODE:[CallbackQueryHandler(tg_mode_select, pattern="^(mode_reg|mode_mul)$")], TG_ASK_COUNT:[MessageHandler(filters.TEXT, tg_set_count)]}, fallbacks=[CommandHandler('cancel', cancel)])
    timer_conv = ConversationHandler(entry_points=[CallbackQueryHandler(timer_start, pattern="^start_timer$")], states={TG_TIMER_LINK:[MessageHandler(filters.TEXT, timer_logic_start)]}, fallbacks=[CommandHandler('cancel', cancel)])

    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(admin_conv); app.add_handler(email_conv); app.add_handler(tg_conv); app.add_handler(timer_conv)
    app.add_handler(CallbackQueryHandler(button_callback_router, pattern="^(short|long)$"))
    app.add_handler(CallbackQueryHandler(send_email_action, pattern="^send_mass$"))
    app.add_handler(CallbackQueryHandler(stop_timer_callback, pattern="^stop_timer$"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    
    print("Bot Polling..."); app.run_polling()
        
