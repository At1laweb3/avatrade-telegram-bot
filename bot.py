import os, json, re, datetime, asyncio
import httpx, gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

ASK_NAME, ASK_EMAIL, ASK_PHONE = range(3)
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
            if len(r) >= 7:
                r[6] = ((r[6] + " | ") if r[6] else "") + (notes or "")
            ws.update(f"A{i}:G{i}", [r])
            break

PUP_URL = os.environ["PUPPETEER_API_URL"].rstrip("/")
PUP_SECRET = os.environ["PUPPETEER_SHARED_SECRET"]

def _norm_phone(raw: str, default_cc="+381") -> str:
    s = "".join(ch for ch in raw.strip() if ch.isdigit() or ch=="+")
    if not s: return ""
    if s.startswith("+"): return s
    if s.startswith("00"): return "+" + s[2:]
    if s.startswith("0"):  return default_cc + s[1:]
    return "+" + s

async def call_signup(name, email, password, phone, country="Serbia"):
    headers = {"X-Auth": PUP_SECRET, "Content-Type": "application/json"}
    payload = {"name": name, "email": email, "password": password, "phone": phone, "country": country}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=25.0)) as client:
        r = await client.post(f"{PUP_URL}/create-demo", headers=headers, json=payload)
        ok = r.status_code == 200
        data = r.json() if ok else {"error": f"HTTP {r.status_code}", "body": r.text[:400]}
        return ok, data

async def call_create_mt4(email, password):
    headers = {"X-Auth": PUP_SECRET, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=25.0)) as client:
        r = await client.post(f"{PUP_URL}/create-mt4", headers=headers, json={"email": email, "password": password})
        ok = r.status_code == 200
        data = r.json() if ok else {"error": f"HTTP {r.status_code}", "body": r.text[:400]}
        return ok, data

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Zdravo! Kako se zoveš? (npr. Marko)")
    return ASK_NAME

async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Prekratko ime, probaj opet:")
        return ASK_NAME
    ctx.user_data["name"] = name
    await update.message.reply_text("Super! Unesi svoj email:")
    return ASK_EMAIL

async def got_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    if not EMAIL_REGEX.match(email):
        await update.message.reply_text("Email nije validan. Unesi ponovo:")
        return ASK_EMAIL
    ctx.user_data["email"] = email
    await update.message.reply_text("Unesi broj telefona (sa pozivnim, npr. +381641234567 ili 064...):")
    return ASK_PHONE

async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw_phone = update.message.text.strip()
    phone = _norm_phone(raw_phone, "+381")
    if len(phone) < 8:
        await update.message.reply_text("Telefon nije validan. Probaj ponovo (npr. +38164xxxxxxx):")
        return ASK_PHONE

    name     = ctx.user_data["name"]
    email    = ctx.user_data["email"]
    password = f"{name}123#"
    chat_id  = update.effective_chat.id

    ws = _ws()
    if sheet_email_exists(ws, email):
        await update.message.reply_text("Taj email je već registrovan. Unesi drugi:")
        return ASK_EMAIL

    sheet_add(ws, chat_id, name, email, password, status="pending", notes=f"phone:{phone}")

    await update.message.reply_text(f"✅ Hvala, {name}! Kreiram tvoj DEMO... Sačekaj 10–30 sekundi.")

    ok1, data1 = await call_signup(name, email, password, phone, country="Serbia")
    if ok1 and data1.get("ok"):
        sheet_update(ws, chat_id, email, "created", data1.get("note",""))
        await update.message.reply_text("🎉 Demo je kreiran! Pripremam MT4 podatke...")
    else:
        msg = (data1.get("error") or data1.get("note") or "nepoznato")
        sheet_update(ws, chat_id, email, "error", msg)
        await update.message.reply_text("⚠️ Nije uspelo (verovatno zaštita). Pokušaćemo ponovo ili ručno.")
        # pokaži poslednji screenshot ako ima
        shots = (data1 or {}).get("screenshots", [])
        if shots:
            try: await update.message.reply_photo(shots[-1], caption="📸 Outcome screenshot")
            except: pass
        return ConversationHandler.END

    # 2) MT4 kreiranje
    ok2, data2 = await call_create_mt4(email, password)
    if ok2 and data2.get("ok"):
        mt_login  = data2.get("mt4_login")
        mt_server = data2.get("mt4_server")
        sheet_update(ws, chat_id, email, "mt4", f"mt4:{mt_login}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Kontakt SUPPORT", url="https://t.me/aleksa_asf01")]])
        text = (
            "✅ Tvoj MT4 DEMO je spreman!\n\n"
            f"Login ID: <code>{mt_login}</code>\n"
            f"Server: <code>{mt_server}</code>\n"
            f"Lozinka: <code>{password}</code>\n\n"
            "Ako imaš poteškoća sa povezivanjem, javi se SUPPORT-u."
        )
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        msg = data2.get("error") or data2.get("note") or "nepoznato"
        sheet_update(ws, chat_id, email, "mt4-error", msg)
        await update.message.reply_text("⚠️ Kreiranje MT4 naloga nije uspelo (verovatno zaštita).")

    # pošalji poslednji screenshot iz MT4 koraka ako postoji
    shots2 = (data2 or {}).get("screenshots", [])
    if shots2:
        try: await update.message.reply_photo(shots2[-1], caption="📸 MT4 screenshot")
        except: pass

    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Prekinuto. Pošalji /start kad budeš spreman.")
    return ConversationHandler.END

async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != os.environ.get("OWNER_ID",""):
        return
    ws = _ws(); sent = 0
    for r in ws.get_all_values()[1:]:
        try:
            await ctx.bot.send_message(int(r[1]), "📣 Test broadcast – pozdrav ekipa!")
            sent += 1; await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"Poslato ka {sent} korisnika.")

def main():
    app = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_email)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.run_polling()

if __name__ == "__main__":
    main()