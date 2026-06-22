# -*- coding: utf-8 -*-
"""
Сервис платного отчёта AI-видимости.
Поток: форма на сайте -> /create-payment -> оплата ТBank -> вебхук /tbank/notify
       -> движок опрашивает нейросети -> build_report делает PDF -> письмо клиенту.

Запуск:   pip install flask ; python3 app.py
Секреты (переменные окружения, НЕ в коде):
  TBANK_TERMINAL   = 1782125233968DEMO        (демо-терминал)
  TBANK_PASSWORD   = <пароль терминала>        ← впишешь сама, я его не вижу
  PRICE_RUB        = 1290
  BASE_URL         = https://service.annakurbatova.ru   (адрес этого сервиса)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM  (для писем)
  PERPLEXITY_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
  DEEPSEEK_API_KEY, GIGACHAT_API_KEY, YANDEX_API_KEY, YANDEX_FOLDER_ID  (по мере появления)
  TEST_MODE=1  -> движок в мок-режиме (без ключей, для проверки)
"""
import os, json, time, hashlib, sqlite3, threading, smtplib, ssl, urllib.request, uuid, re
from email.message import EmailMessage
from flask import Flask, request, redirect, jsonify, send_file, abort

import engine, build_report

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH") or os.path.join(APP_DIR, "orders.db")
REPORTS = os.environ.get("REPORTS_DIR") or os.path.join(APP_DIR, "reports")
os.makedirs(REPORTS, exist_ok=True)

TERMINAL = os.environ.get("TBANK_TERMINAL", "1782125233968DEMO")
PRICE = int(os.environ.get("PRICE_RUB", "1290"))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
TBANK_INIT = "https://securepay.tinkoff.ru/v2/Init"
def tg_token(): return os.environ.get("TELEGRAM_BOT_TOKEN", "")            # читаем live при каждом вызове
def tg_bot():   return os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")

app = Flask(__name__)

# ───────────────────────── хранилище заказов ─────────────────────────
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id TEXT PRIMARY KEY, created REAL, status TEXT,
            brand TEXT, brand_short TEXT, site TEXT, niche TEXT, city TEXT, email TEXT,
            payment_id TEXT, pdf TEXT, tg_chat_id TEXT)""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(orders)").fetchall()]
        if "tg_chat_id" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN tg_chat_id TEXT")
init_db()

def cache_hit(site):
    """Кэш по домену на 14 дней: не гонять API повторно по тому же сайту."""
    with db() as c:
        r = c.execute("SELECT pdf,created FROM orders WHERE site=? AND status='done' AND pdf IS NOT NULL ORDER BY created DESC LIMIT 1",
                      (engine._host(site),)).fetchone()
    if r and r["pdf"] and os.path.exists(r["pdf"]) and time.time()-r["created"] < 14*86400:
        return r["pdf"]
    return None

# ───────────────────────── подпись ТBank ─────────────────────────────
def tbank_token(params, password):
    """Token = sha256 от конкатенации значений корневых полей (+Password), отсортированных по ключу."""
    flat = {k: v for k, v in params.items() if k != "Token" and not isinstance(v, (dict, list))}
    flat["Password"] = password
    concat = "".join(str(flat[k]).lower() if isinstance(flat[k], bool) else str(flat[k])
                      for k in sorted(flat))
    return hashlib.sha256(concat.encode("utf-8")).hexdigest()

def tbank_init(order_id, email):
    password = os.environ["TBANK_PASSWORD"]  # из секрета, не из кода
    bot = tg_bot()
    back = f"https://t.me/{bot}?start={order_id}" if bot else f"{BASE_URL}/thanks?order={order_id}"
    payload = {
        "TerminalKey": TERMINAL,
        "Amount": PRICE * 100,                 # в копейках
        "OrderId": order_id,
        "Description": "Отчёт о видимости бренда в нейросетях",
        "PayType": "O",                        # одностадийная оплата (списание сразу)
        "NotificationURL": f"{BASE_URL}/tbank/notify",
        "SuccessURL": back,                    # возврат в бот, там придёт отчёт
        "FailURL": back,
    }
    payload["Token"] = tbank_token(payload, password)
    req = urllib.request.Request(TBANK_INIT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

# ───────────────────────── письма ───────────────────────────────────
def send_report_email(to, pdf_path, brand):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("[mail] SMTP не настроен, пропуск письма на", to); return
    msg = EmailMessage()
    msg["Subject"] = "Ваш отчёт о видимости в нейросетях"
    msg["From"] = os.environ.get("MAIL_FROM", os.environ.get("SMTP_USER"))
    msg["To"] = to
    msg.set_content(f"Здравствуйте!\n\nГотов отчёт о видимости «{brand}» в нейросетях. Файл во вложении.\n\nАнна Курбатова · annakurbatova.ru")
    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf",
                           filename="Отчёт-AI-видимость.pdf")
    port = int(os.environ.get("SMTP_PORT", "465"))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(msg)
    print("[mail] отправлено на", to)

# ───────────────────────── Telegram-доставка ─────────────────────────
def _tg(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{tg_token()}/{method}"
    if files:
        b = "----tg" + uuid.uuid4().hex; body = b""
        for k, v in (data or {}).items():
            body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (fn, content, ct) in files.items():
            body += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
                     f'Content-Type: {ct}\r\n\r\n').encode() + content + b"\r\n"
        body += f"--{b}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def tg_send_message(chat_id, text):
    if tg_token(): _tg("sendMessage", {"chat_id": chat_id, "text": text})

def tg_send_document(chat_id, pdf_path, caption=""):
    with open(pdf_path, "rb") as f: content = f.read()
    return _tg("sendDocument", {"chat_id": str(chat_id), "caption": caption},
               {"document": ("Отчёт-AI-видимость.pdf", content, "application/pdf")})

def tg_send_payment_button(chat_id, pay_url, brand):
    _tg("sendMessage", {"chat_id": chat_id,
        "text": f"Заказ на отчёт о видимости «{brand}» в нейросетях принят.\n\nНажмите кнопку, чтобы оплатить 1290 ₽. Сразу после оплаты я пришлю готовый отчёт сюда, в этот чат.",
        "reply_markup": {"inline_keyboard": [[{"text": "Оплатить 1290 ₽", "url": pay_url}]]}})

def deliver(o, pdf):
    """Доставка отчёта: основной канал — Telegram, почта — опциональный резерв."""
    sent = False
    if o["tg_chat_id"] and tg_token():
        try:
            tg_send_document(o["tg_chat_id"], pdf, f"Готов отчёт о видимости «{o['brand']}» в нейросетях.")
            sent = True
        except Exception as e:
            print("[tg] ошибка отправки:", e)
    if o["email"] and os.environ.get("SMTP_HOST"):
        try: send_report_email(o["email"], pdf, o["brand"])
        except Exception as e: print("[mail] ошибка:", e)
    return sent

# ───────────────────────── генерация отчёта ──────────────────────────
def generate(order_id):
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o: return
    print(f"[generate] старт order={order_id} site={o['site']}", flush=True)
    try:
        cached = cache_hit(o["site"])
        if cached:
            pdf = cached
        else:
            data = engine.run(o["brand"], o["site"], o["niche"], o["city"], brand_short=o["brand_short"])
            pdf = os.path.join(REPORTS, f"{order_id}.pdf")
            build_report.build(data, pdf)
        with db() as c:
            c.execute("UPDATE orders SET status='done', pdf=? WHERE id=?", (pdf, order_id))
            o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        sent = deliver(o, pdf)   # Telegram (если клиент уже нажал Старт) + опционально почта
        print(f"[generate] готово order={order_id} pdf_ok={os.path.exists(pdf)} отправлен_в_tg={sent}", flush=True)
    except Exception as e:
        print("[generate] ошибка:", e, flush=True)
        with db() as c:
            c.execute("UPDATE orders SET status='error' WHERE id=?", (order_id,))

def maybe_start_generation(oid):
    """Запускает движок только если заказ оплачен (status='paid'). Защита от двойного старта."""
    with db() as c:
        o = c.execute("SELECT status, tg_chat_id FROM orders WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "paid":
            return False
        c.execute("UPDATE orders SET status='processing' WHERE id=?", (oid,))
    if o["tg_chat_id"]:
        tg_send_message(o["tg_chat_id"],
            "Спасибо за оплату! Ваш отчёт о видимости в нейросетях уже формируется. "
            "Я пришлю его сюда, в чат, в течение 10 минут. Можно закрыть страницу оплаты, "
            "я сообщу, как только всё будет готово.")
    threading.Thread(target=generate, args=(oid,), daemon=True).start()
    return True

# ───────────────────────── маршруты ──────────────────────────────────
ORDER_FORM = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Отчёт о видимости в нейросетях</title><style>
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#141210;color:#fff;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
.card{width:100%;max-width:440px}
.ey{display:inline-block;font-size:11px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase;color:#DE4A2C;border:1px solid rgba(222,74,44,.4);border-radius:30px;padding:6px 14px;margin-bottom:18px}
h1{font-size:25px;line-height:1.15;margin:0 0 10px}
p.sub{color:rgba(255,255,255,.6);font-size:15px;line-height:1.5;margin:0 0 20px}
label{display:block;font-size:13px;color:rgba(255,255,255,.7);margin:14px 0 6px}
input{width:100%;padding:13px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.05);color:#fff;font-size:15px}
input:focus{outline:none;border-color:#DE4A2C}
button{width:100%;margin-top:22px;padding:15px;border:0;border-radius:30px;background:#DE4A2C;color:#fff;font-size:16px;font-weight:700;cursor:pointer}
.note{font-size:12px;color:rgba(255,255,255,.45);margin-top:14px;line-height:1.5}
</style></head><body><div class=card>
<div class=ey>AI-видимость · отчёт</div>
<h1>Проверим ваш бизнес в 7 нейросетях</h1>
<p class=sub>Введите сайт. Проверим по 10 коммерческим запросам в ChatGPT, Яндекс Нейро и ещё 5 AI-сервисах. Готовый отчёт придёт в Telegram.</p>
<form method=post action=/create-payment>
  <label>Адрес сайта *</label><input name=site placeholder="example.ru" required>
  <label>Ниша, чем занимаетесь</label><input name=niche placeholder="мебель на заказ">
  <label>Город</label><input name=city placeholder="Москва">
  <label>E-mail (необязательно)</label><input name=email type=email placeholder="вы@почта.ру">
  <button type=submit>Получить отчёт за 1290 ₽ &rarr;</button>
</form>
<p class=note>После оплаты вы перейдёте в Telegram-бот, нажмёте «Старт», и отчёт придёт в чат за несколько минут.</p>
</div></body></html>"""

def err_page(msg, code=502):
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<div style='font-family:system-ui,Arial;max-width:460px;margin:14vh auto;text-align:center;color:#1c1813;padding:0 20px'>"
            f"<h2>Не получилось</h2><p style='color:#5e564a;line-height:1.5'>{msg}</p>"
            "<a href='/' style='color:#DE4A2C;font-weight:600'>Назад к форме</a></div>", code)

@app.get("/")
def home():
    return ORDER_FORM

@app.post("/create-payment")
def create_payment():
    f = request.get_json(force=True, silent=True) or request.form
    site = (f.get("site") or "").strip()
    email = (f.get("email") or "").strip()     # опционально, как резерв
    niche = (f.get("niche") or "").strip()
    if not site or "." not in site:
        return err_page("Укажите корректный адрес сайта.", 400)
    brand = (f.get("brand") or "").strip() or engine._host(site)
    short = (f.get("brand_short") or "").strip() or re.sub(r"[«»\"']", "", brand).split(",")[0]
    order_id = uuid.uuid4().hex[:16]
    with db() as c:
        c.execute("INSERT INTO orders(id,created,status,brand,brand_short,site,niche,city,email) VALUES(?,?,?,?,?,?,?,?,?)",
                  (order_id, time.time(), "new", brand, short, site, niche, (f.get("city") or "").strip(), email))
    bot = tg_bot()
    if not bot:
        return err_page("Сервис временно недоступен, напишите нам в Telegram.", 503)
    # Ведём клиента в бот: там он жмёт Старт, получает кнопку оплаты, а после оплаты отчёт приходит в чат сам.
    link = f"https://t.me/{bot}?start={order_id}"
    if request.is_json:
        return jsonify(redirect=link, orderId=order_id)
    return redirect(link, code=302)

@app.post("/tbank/notify")
def tbank_notify():
    data = request.get_json(force=True, silent=True) or {}
    password = os.environ.get("TBANK_PASSWORD", "")
    got = data.get("Token", "")
    ok = tbank_token(data, password) == got
    print(f"[notify] order={data.get('OrderId')} status={data.get('Status')} success={data.get('Success')} token_ok={ok}", flush=True)
    if not ok:
        abort(403)                              # подпись не сошлась
    if data.get("Status") in ("CONFIRMED", "AUTHORIZED") and data.get("Success"):
        oid = data.get("OrderId")
        with db() as c:
            o = c.execute("SELECT status, tg_chat_id FROM orders WHERE id=?", (oid,)).fetchone()
        print(f"[notify] найден={bool(o)} статус={o['status'] if o else None} чат={o['tg_chat_id'] if o else None}", flush=True)
        if o and o["status"] in ("new", "pending"):
            with db() as c:
                c.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))   # фиксируем оплату, движок пока НЕ запускаем
            if o["tg_chat_id"]:           # клиент уже нажал Старт раньше оплаты -> запускаем сразу
                maybe_start_generation(oid)
    return "OK"

@app.get("/thanks")
def thanks():
    oid = request.args.get("order", "")
    link = f"https://t.me/{tg_bot()}?start={oid}" if tg_bot() else "#"
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
    <div style="font-family:system-ui,Arial;max-width:520px;margin:12vh auto;padding:0 20px;text-align:center;color:#1c1813">
      <h2 style="font-size:24px">Оплата прошла, спасибо!</h2>
      <p style="font-size:16px;color:#5e564a;line-height:1.55">Отчёт формируется (5-10 минут) и придёт вам в Telegram.
      Нажмите кнопку, затем «Старт» в боте, туда придёт готовый PDF.</p>
      <a href="{link}" style="display:inline-block;margin-top:14px;background:#DE4A2C;color:#fff;font-weight:700;
      text-decoration:none;padding:14px 26px;border-radius:30px;font-size:16px">Получить отчёт в Telegram</a>
    </div>"""

@app.post("/telegram/webhook")
def tg_webhook():
    upd = request.get_json(force=True, silent=True) or {}
    msg = upd.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat and text.startswith("/start"):
        parts = text.split(maxsplit=1)
        oid = parts[1].strip() if len(parts) > 1 else ""
        if oid:
            with db() as c:
                o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if o:
                with db() as c:
                    c.execute("UPDATE orders SET tg_chat_id=? WHERE id=?", (str(chat), oid))
                st = o["status"]
                if st == "done" and o["pdf"] and os.path.exists(o["pdf"]):
                    try: tg_send_document(chat, o["pdf"], f"Готов отчёт о видимости «{o['brand']}» в нейросетях.")
                    except Exception as e: print("[tg] ошибка:", e)
                elif st == "paid":      # оплачено -> запускаем движок именно сейчас
                    tg_send_message(chat, "Оплата подтверждена. Формирую отчёт по 7 нейросетям, пришлю сюда через несколько минут.")
                    maybe_start_generation(oid)
                elif st == "processing":
                    tg_send_message(chat, "Отчёт уже формируется, пришлю его сюда через пару минут.")
                elif st == "new":       # создаём платёж и шлём кнопку оплаты прямо в чат
                    try:
                        res = tbank_init(oid, o["email"])
                        if res.get("Success"):
                            with db() as c:
                                c.execute("UPDATE orders SET status='pending', payment_id=? WHERE id=?", (res.get("PaymentId"), oid))
                            tg_send_payment_button(chat, res["PaymentURL"], o["brand"])
                        else:
                            tg_send_message(chat, "Не удалось создать оплату. Напишите нам, поможем оформить.")
                    except Exception as e:
                        print("[pay] ошибка:", e)
                        tg_send_message(chat, "Оплата временно недоступна, попробуйте чуть позже.")
                else:                   # pending: платёж уже создан
                    tg_send_message(chat, "Кнопка оплаты выше. После оплаты отчёт придёт сюда автоматически.")
                return "OK"
        tg_send_message(chat, "Здравствуйте! Чтобы получить отчёт, перейдите по кнопке после оплаты на сайте.")
    return "OK"

@app.get("/fail")
def fail():
    return "<h2>Оплата не завершена.</h2><p>Попробуйте ещё раз или напишите нам.</p>", 200

@app.get("/order/<oid>")
def order_status(oid):
    with db() as c:
        o = c.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: abort(404)
    return jsonify(status=o["status"])

@app.get("/report/<oid>")
def report(oid):
    with db() as c:
        o = c.execute("SELECT pdf,status FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "done" or not o["pdf"] or not os.path.exists(o["pdf"]):
        abort(404)
    return send_file(o["pdf"], mimetype="application/pdf")

@app.get("/health")
def health():
    keys = {k: bool(os.environ.get(v)) for k, v in engine.KEY_ENV.items()}
    return jsonify(ok=True, terminal=TERMINAL, price=PRICE, test_mode=os.environ.get("TEST_MODE") == "1",
                   telegram=bool(tg_token()), bot=tg_bot(), keys=keys)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
