import os, json, re, datetime, asyncio
import httpx, gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ==== STATES ====
ASK_NAME, ASK_EMAIL, ASK_PHONE = range(3)
EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# ==== SHEET HELPERS ====
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

# ==== PUPPETEER SERVICE ====
PUP_URL = os.environ["PUPPETEER_API_URL"].rstrip("/")
PUP_SECRET = os.environ["PUPPETEER_SHARED_SECRET"]

def _norm_phone(raw: str, default_cc="+381") -> str:
    s = "".join(ch for ch in raw.strip() if ch.isdigit() or ch=="+")
    if not s: return ""
    if s.startswith("+"): return s
    if s.startswith("00"): return "+" + s[2:]
    if s.startswith("0"):  return default_cc + s[1:]
    return "+" + s

async def call_puppeteer_create_demo(name, email, password, phone, country="Serbia"):
    headers = {"X-Auth": PUP_SECRET, "Content-Type": "application/json"}
    payload = {"name": name, "email": email, "password": password, "phone": phone, "country": country}
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=30.0)) as client:
        r = await client.post(f"{PUP_URL}/create-demo", headers=headers, json=payload)
        ok = r.status_code == 200
        data = r.json() if ok else {"error": f"HTTP {r.status_code}", "body": r.text[:400]}
        return ok, data

async def call_puppeteer_create_mt4(email, password):
    headers = {"X-Auth": PUP_SECRET, "Content-Type": "application/json"}
    payload = {"email": email, "password": password}
    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=30.0)) as client:
        r = await client.post(f"{PUP_URL}/create-mt4", headers=headers, json=payload)
        ok = r.status_code == 200
        data = r.json() if ok else {"error": f"HTTP {r.status_code}", "body": r.text[:400]}
        return ok, data

# ==== HANDLERS ====
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

    # 1) DEMO (optimistički: čak i kad web ne vrati jasno 'success', nastavljamo dalje)
    ok, data = await call_puppeteer_create_demo(name, email, password, phone, country="Serbia")
    shots = (data or {}).get("screenshots", [])
    if shots:
        try: await update.message.reply_photo(shots[-1], caption="📸 Outcome screenshot")
        except: pass

    # ako je uspeh – super; ako nije, vodimo ga kao 'verovatno kreiran' i idemo dalje na MT4
    if ok and (data.get("ok") is True):
        sheet_update(ws, chat_id, email, "created", data.get("note",""))
    else:
        sheet_update(ws, chat_id, email, "maybe_created", data.get("note","") or "prošli Cloudflare / probaćemo MT4")

    await update.message.reply_text("🎉 Demo (verovatno) kreiran. Pokušavam da kreiram MT4 nalog...")

    # 2) MT4 (CFD-MT4 / EUR)
    mt4_ok, mt4 = await call_puppeteer_create_mt4(email, password)
    if mt4_ok and mt4.get("ok") and mt4.get("mt4_login"):
        mt4_login = mt4["mt4_login"]
        sheet_update(ws, chat_id, email, "mt4_ok", f"mt4_login:{mt4_login}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Kontaktiraj SUPPORT", url="https://t.me/aleksa_asf01")]])
        await update.message.reply_text(
            f"MetaTrader 4 Login: {mt4_login}\nŠifra: {password}\n\nAko imaš poteškoća sa povezivanjem, javi se SUPPORT-u ⬇️",
            reply_markup=kb
        )
        # pošalji i poslednji MT4 screenshot, ako ima
        s2 = (mt4 or {}).get("screenshots", [])
        if s2:
            try: await update.message.reply_photo(s2[-1], caption="📸 MT4 rezultat")
            except: pass
    else:
        msg = mt4.get("error") or mt4.get("phase") or "nepoznato"
        sheet_update(ws, chat_id, email, "mt4_error", msg)
        await update.message.reply_text("ℹ️ Nalog je verovatno kreiran, ali nisam uspeo odmah da povučem MT4 podatke. Uradiću ručno.")

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