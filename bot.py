import os, json, re, datetime, asyncio
import httpx, gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

ASK_NAME, ASK_EMAIL = range(2)
EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

def _ws():
    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open(os.environ.get("GSHEET_NAME", "ForexBotUsers"))
    return sh.sheet1

def sheet_email_exists(ws, email): 
    emails = [e.strip().lower() for e in ws.col_values(4)[1:] if e]
    return email.strip().lower() in emails

def sheet_add(ws, chat_id, name, email, password, status="pending", notes=""):
    ts = datetime.datetime.utcnow().isoformat()
    ws.append_row([ts, str(chat_id), name, email, password, status, notes], value_input_option="RAW")

def sheet_update(ws, chat_id, email, status, notes=""):
    rows = ws.get_all_values()
    for i, r in enumerate(rows[1:], start=2):
        if len(r) >= 4 and r[1] == str(chat_id) and r[3].strip().lower() == email.strip().lower():
            r[5] = status
            if len(r) >= 7: r[6] = notes
            ws.update(f"A{i}:G{i}", [r]); break

PUP_URL = os.environ["PUPPETEER_API_URL"].rstrip("/")
PUP_SECRET = os.environ["PUPPETEER_SHARED_SECRET"]

async def call_puppeteer(name, email, password):
    headers = {"X-Auth": PUP_SECRET, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=20.0)) as client:
        r = await client.post(f"{PUP_URL}/create-demo", headers=headers, json={"name":name,"email":email,"password":password})
        ok = r.status_code == 200
        data = r.json() if ok else {"error": f"HTTP {r.status_code}", "body": r.text[:400]}
        return ok, data

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Zdravo! Kako se zoveš? (npr. Marko)")
    return ASK_NAME

async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2: 
        await update.message.reply_text("Prekratko ime, probaj opet:"); return ASK_NAME
    ctx.user_data["name"] = name
    await update.message.reply_text("Super! Unesi svoj email:")
    return ASK_EMAIL

async def got_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    if not EMAIL_REGEX.match(email):
        await update.message.reply_text("Email nije validan. Unesi ponovo:"); return ASK_EMAIL

    name = ctx.user_data["name"]; password = f"{name}123#"; chat_id = update.effective_chat.id
    ws = _ws()
    if sheet_email_exists(ws, email):
        await update.message.reply_text("Taj email je već registrovan. Unesi drugi:"); return ASK_EMAIL

    sheet_add(ws, chat_id, name, email, password, status="pending")
    await update.message.reply_text(f"✅ Hvala, {name}! Kreiram tvoj DEMO... Sačekaj 10–30 sekundi.")
    ok, data = await call_puppeteer(name, email, password)
    if ok:
        sheet_update(ws, chat_id, email, "created", data.get("note",""))
        await update.message.reply_text("🎉 Demo je kreiran! Uskoro ću ti poslati MetaTrader detalje.")
    else:
        sheet_update(ws, chat_id, email, "error", data.get("error",""))
        await update.message.reply_text("⚠️ Nije uspelo (verovatno zaštita). Pokušaćemo ponovo ili ručno.")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Prekinuto. Pošalji /start kad budeš spreman."); return ConversationHandler.END

async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.environ.get("OWNER_ID",""): return
    ws = _ws(); sent = 0
    for r in ws.get_all_values()[1:]:
        try: await ctx.bot.send_message(int(r[1]), "📣 Test broadcast – pozdrav ekipa!"); sent += 1; await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"Poslato ka {sent} korisnika.")

def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_email)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv); app.add_handler(CommandHandler("broadcast", broadcast))
    app.run_polling()

if __name__ == "__main__": main()