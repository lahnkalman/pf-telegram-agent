import os, re, sqlite3, datetime as dt
from typing import List, Tuple
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, AIORateLimiter

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SECRET_PATH = os.environ.get("SECRET_PATH", "hook")  # הגן על ה-webhook בנתיב נסתר, לדוגמה: x9ab123

DB_PATH = "pf_agent.db"

# ---------- DB ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db(); cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS accounts(
      id INTEGER PRIMARY KEY, user_id TEXT, name TEXT, type TEXT, currency TEXT, balance REAL, last_updated TEXT
    );
    CREATE TABLE IF NOT EXISTS goals(
      id INTEGER PRIMARY KEY, user_id TEXT, title TEXT, target_type TEXT, target_value REAL, target_date TEXT, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS guardrails(
      id INTEGER PRIMARY KEY, user_id TEXT, max_pos_pct REAL, cash_buffer_pct REAL, stop_loss_pct REAL, max_mdd_month_pct REAL, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS scenarios(
      id INTEGER PRIMARY KEY, user_id TEXT, name TEXT, profile TEXT, rationale TEXT
    );
    CREATE TABLE IF NOT EXISTS scenario_steps(
      id INTEGER PRIMARY KEY, scenario_id INTEGER, due_date TEXT, action TEXT, amount REAL, notes TEXT, status TEXT
    );
    """)
    conn.commit(); conn.close()

DEFAULT_GUARDS = dict(max_pos_pct=15.0, cash_buffer_pct=20.0, stop_loss_pct=8.0, max_mdd_month_pct=5.0)

def ensure_guardrails(user_id: str):
    conn=db(); cur=conn.cursor()
    cur.execute("SELECT 1 FROM guardrails WHERE user_id=?", (user_id,))
    if not cur.fetchone():
        cur.execute("""INSERT INTO guardrails(user_id,max_pos_pct,cash_buffer_pct,stop_loss_pct,max_mdd_month_pct,notes)
                       VALUES(?,?,?,?,?,?)""",
                    (user_id, DEFAULT_GUARDS["max_pos_pct"], DEFAULT_GUARDS["cash_buffer_pct"],
                     DEFAULT_GUARDS["stop_loss_pct"], DEFAULT_GUARDS["max_mdd_month_pct"], "defaults"))
        conn.commit()
    conn.close()

def upsert_account(user_id: str, name: str, acc_type: str, currency: str, balance: float):
    conn=db(); cur=conn.cursor()
    cur.execute("SELECT id FROM accounts WHERE user_id=? AND name=?", (user_id, name))
    row=cur.fetchone()
    now = dt.datetime.utcnow().isoformat()
    if row:
        cur.execute("UPDATE accounts SET type=?, currency=?, balance=?, last_updated=? WHERE id=?",
                    (acc_type, currency, balance, now, row["id"]))
    else:
        cur.execute("INSERT INTO accounts(user_id,name,type,currency,balance,last_updated) VALUES(?,?,?,?,?,?)",
                    (user_id, name, acc_type, currency, balance, now))
    conn.commit(); conn.close()

def list_accounts(user_id: str) -> List[sqlite3.Row]:
    conn=db(); cur=conn.cursor()
    cur.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,))
    rows=cur.fetchall(); conn.close()
    return rows

def create_goal(user_id: str, title: str, target_value: float, target_date: str):
    conn=db(); cur=conn.cursor()
    cur.execute("""INSERT INTO goals(user_id,title,target_type,target_value,target_date,notes)
                   VALUES(?,?,?,?,?,?)""",(user_id,title,"amount",target_value,target_date,""))
    conn.commit(); conn.close()

def get_last_goal(user_id: str):
    conn=db(); cur=conn.cursor()
    cur.execute("SELECT * FROM goals WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
    g=cur.fetchone(); conn.close()
    return g

def build_scenarios(user_id: str, goal_title: str, target_value: float, target_date: str):
    profiles = [
        ("שמרני","שמור כרית מזומן גבוהה (≥30%), קניות קטנות, בלי מינוף"),
        ("בסיס","פיזור בסיסי (ETF רחב), הפקדה חודשית קבועה, קנייה מדורגת"),
        ("נועז","כרית מזומן 15–20%, הוספות על ירידות עם Stop-Loss 8%")
    ]
    conn=db(); cur=conn.cursor()
    created=[]
    for name, rationale in profiles:
        cur.execute("INSERT INTO scenarios(user_id,name,profile,rationale) VALUES(?,?,?,?)",
                    (user_id, name, name, rationale))
        sid = cur.lastrowid
        start = dt.date.today()
        steps = [
            (start + dt.timedelta(days=0),  "עדכון יתרות + אימות עמלות", 0, "בדיקת ברוקר/עמלות"),
            (start + dt.timedelta(days=7),  "הפקדה/קנייה מדורגת", target_value/3, f"עבור היעד: {goal_title}"),
            (start + dt.timedelta(days=14), "ביקורת guardrails", 0, "כרית מזומן / גודל פוזיציות ≤15%"),
            (start + dt.timedelta(days=21), "סיכום ביניים + החלטה להאיץ/להאט", 0, "התאם לתחושה ולכללים"),
        ]
        for due, action, amount, notes in steps:
            cur.execute("""INSERT INTO scenario_steps(scenario_id,due_date,action,amount,notes,status)
                           VALUES(?,?,?,?,?,?)""",(sid, due.isoformat(), action, amount, notes, "todo"))
        created.append(sid)
    conn.commit(); conn.close()
    return created

def scenario_status(user_id: str) -> str:
    conn=db(); cur=conn.cursor()
    cur.execute("""SELECT s.name, st.due_date, st.action, st.status
                   FROM scenarios s JOIN scenario_steps st ON s.id=st.scenario_id
                   WHERE s.user_id=? ORDER BY st.due_date ASC""",(user_id,))
    rows = cur.fetchall(); conn.close()
    if not rows: return "אין צעדים כרגע. כתוב 'תרחישים' כדי ליצור."
    out=[]
    for r in rows:
        out.append(f"[{r['name']}] {r['due_date']}: {r['action']} — {r['status']}")
    return "\n".join(out)

def parse_goal(text: str):
    m_val = re.search(r'(\d[\d,\.]*)', text)
    m_date = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    target_value = float(m_val.group(1).replace(",","")) if m_val else 3000.0
    target_date  = m_date.group(1) if m_date else (dt.date.today()+dt.timedelta(days=100)).isoformat()
    return target_value, target_date

# ---------- Telegram app (webhook) ----------
app = FastAPI()
application = Application.builder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()

from telegram import ReplyKeyboardMarkup, KeyboardButton
KB = ReplyKeyboardMarkup(
    [[KeyboardButton("יעד לדוגמה"), KeyboardButton("תרחישים")],
     [KeyboardButton("סטטוס"), KeyboardButton("חוקי סיכון")]],
    resize_keyboard=True
)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    uid = str(update.effective_user.id)
    ensure_guardrails(uid)
    # מאזן התחלתי לדוגמה – תוכל לעדכן מול הבוט:
    if not list_accounts(uid):
        upsert_account(uid, "MONDAY", "equity", "ILS", 5000.0)
        upsert_account(uid, "S&P 500", "fund", "ILS", 5000.0)
        upsert_account(uid, "BankIndex", "fund", "ILS", 5000.0)
    await update.message.reply_text(
        "היי! אני הסוכן הפיננסי ל-3–4 חודשים 🧭\n"
        "דוגמאות: 'יעד 6000 עד 2026-01-31' • 'תרחישים' • 'סטטוס' • 'חוקי סיכון' • 'מאזן'\n"
        "לעדכון יתרה: 'חשבון MONDAY 7000'",
        reply_markup=KB
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("פקודות: /start /help + הודעות חופשיות (יעד/תרחישים/סטטוס/חוקי סיכון/מאזן/חשבון ...)")

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    uid = str(update.effective_user.id)
    ensure_guardrails(uid)
    text = (update.message.text or "").strip()

    if text == "יעד לדוגמה":
        text = "יעד 6000 עד 2026-01-31"

    if any(k in text for k in ["חוקי", "guard", "סיכון"]):
        await update.message.reply_text(
            "חוקי סיכון:\n• Max position ≤ 15%\n• Cash buffer ≥ 20%\n• Stop-loss −8%\n• Max monthly drawdown −5%"
        ); return

    if text in ("מאזן", "יתרות"):
        rows = list_accounts(uid)
        if not rows:
            await update.message.reply_text("אין חשבונות. עדכן: 'חשבון MONDAY 7000'")
            return
        await update.message.reply_text("מאזן:\n" + "\n".join([f"• {r['name']}: {r['balance']:.0f} {r['currency']}" for r in rows]))
        return

    if text.startswith("חשבון "):
        m = re.match(r"חשבון\s+(.+?)\s+(\d+(?:\.\d+)?)", text)
        if m:
            name = m.group(1).strip(); balance = float(m.group(2))
            upsert_account(uid, name, "manual", "ILS", balance)
            await update.message.reply_text(f"עודכן: {name} = {balance:.0f} ₪."); return

    if any(k in text for k in ["סטטוס", "מצב", "מה נשאר"]):
        await update.message.reply_text(scenario_status(uid)); return

    if any(k in text for k in ["תרחיש", "תרחישים", "scenar"]):
        g = get_last_goal(uid)
        if not g:
            await update.message.reply_text("קודם נגדיר יעד: 'יעד 6000 עד 2026-01-31'."); return
        build_scenarios(uid, g['title'], g['target_value'], g['target_date'])
        await update.message.reply_text("נוצרו 3 תרחישים.\n" + scenario_status(uid)); return

    if any(k in text for k in ["יעד", "מטרה", "goal"]):
        val, date = parse_goal(text)
        title = f"יעד קצר טווח {int(val)} עד {date}"
        create_goal(uid, title, val, date)
        await update.message.reply_text(f"נרשם היעד: {title}\nכתוב 'תרחישים' כדי ליצור תוכנית."); return

    await update.message.reply_text("היי 🙌 כתוב: יעד / תרחישים / סטטוס / חוקי סיכון / מאזן / חשבון ...")

# חיבור ההנדלרים
application.add_handler(CommandHandler("start", start_handler))
application.add_handler(CommandHandler("help", help_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))

@app.get("/health")
async def health():
    return {"ok": True}

@app.post(f"/{SECRET_PATH}")
async def telegram_webhook(request: Request):
    if not BOT_TOKEN:
        return {"error": "BOT_TOKEN missing"}
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}
