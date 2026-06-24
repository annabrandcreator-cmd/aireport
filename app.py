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
import os, json, time, hashlib, sqlite3, threading, smtplib, ssl, urllib.request, uuid, re, traceback
from email.message import EmailMessage
from flask import Flask, request, redirect, jsonify, send_file, abort

import engine, build_report

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH") or os.path.join(APP_DIR, "orders.db")
REPORTS = os.environ.get("REPORTS_DIR") or os.path.join(APP_DIR, "reports")
os.makedirs(REPORTS, exist_ok=True)

TERMINAL = os.environ.get("TBANK_TERMINAL", "1782125233968DEMO")
PRICE = int(os.environ.get("PRICE_RUB", "1290"))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").strip().rstrip("/")
if BASE_URL and not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL      # подстраховка: добавим схему, если её забыли
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
        if "prep" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN prep TEXT")   # JSON: запросы + контекст для подтверждения
        for col, ddl in [("rating","INTEGER"),("feedback","TEXT"),("awaiting","TEXT"),
                         ("kind","TEXT"),("parent","TEXT"),("qn","INTEGER"),("amount","INTEGER"),("err","TEXT")]:
            if col not in cols:
                c.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
init_db()

ADDON_PRICES = {1: 190, 3: 490, 5: 790, 10: 1290}   # дозакупка: запросов -> цена, ₽

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

def tbank_init(order_id, email, amount=None, description=None):
    password = os.environ["TBANK_PASSWORD"]  # из секрета, не из кода
    rub = amount if amount is not None else PRICE
    payload = {
        "TerminalKey": TERMINAL,
        "Amount": rub * 100,                   # в копейках
        "OrderId": order_id,
        "Description": description or "Отчёт о видимости бренда в нейросетях",
        "PayType": "O",                        # одностадийная оплата (списание сразу)
        "NotificationURL": f"{BASE_URL}/tbank/notify",
        "SuccessURL": f"{BASE_URL}/thanks?order={order_id}",   # обычная веб-страница, не t.me
        "FailURL": f"{BASE_URL}/fail?order={order_id}",
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

def tg_send_buttons(chat_id, text, keyboard):
    if tg_token(): _tg("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": keyboard}})

def tg_send_force_reply(chat_id, text, placeholder=""):
    """Сообщение с режимом ответа и понятной подсказкой в поле ввода (перебивает чужой плейсхолдер)."""
    if tg_token():
        rm = {"force_reply": True}
        if placeholder: rm["input_field_placeholder"] = placeholder[:64]
        _tg("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": rm})

def tg_answer_callback(cb_id, text=""):
    if tg_token():
        try: _tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})
        except Exception as e: print("[tg] answerCallback:", e)

def admin_notify(text):
    """Пересылка обратной связи Анне в личный чат (ADMIN_CHAT_ID)."""
    aid = os.environ.get("ADMIN_CHAT_ID")
    if aid and tg_token():
        try: _tg("sendMessage", {"chat_id": aid, "text": text})
        except Exception as e: print("[admin] ", e)

def _send_feedback_request(chat_id, oid):
    tg_send_buttons(chat_id,
        "Удалось изучить отчёт?\n\n"
        "Помогите улучшить сервис — оцените, насколько отчёт оказался полезным:\n"
        "1 — не получил полезной информации, 5 — получил понятные выводы и рекомендации.",
        [[{"text": str(n), "callback_data": f"rate:{n}:{oid}"} for n in range(1, 6)]])

def _send_addon_offer(chat_id, parent_oid):
    tg_send_buttons(chat_id,
        "Спасибо за обратную связь!\n\n"
        "Хотите проверить другие запросы? Первый отчёт сделан по 10 запросам. "
        "Можно отдельно проверить видимость по запросам, которых в нём не было: например, по другой услуге или направлению, "
        "в конкретном городе или регионе, среди определённого типа клиентов или в другом ценовом сегменте.\n\n"
        "Вы пришлёте свои формулировки, я покажу их на подтверждение и проверю. Выберите, сколько запросов проверить:",
        [[{"text": "1 запрос — 190 ₽",   "callback_data": f"buy:1:{parent_oid}"}],
         [{"text": "3 запроса — 490 ₽",  "callback_data": f"buy:3:{parent_oid}"}],
         [{"text": "5 запросов — 790 ₽", "callback_data": f"buy:5:{parent_oid}"}],
         [{"text": "10 запросов — 1290 ₽","callback_data": f"buy:10:{parent_oid}"}],
         [{"text": "Не сейчас",          "callback_data": "addon_no"}]])

def report_ready_text(o):
    """Сообщение, которое идёт ОТДЕЛЬНО после PDF (поэтому «PDF выше»)."""
    host = engine._host(o["site"]) if o["site"] else (o["brand"] or "ваш сайт")
    return (f"Готово! Отчёт по сайту «{host}» уже в чате.\n\n"
            "Скачайте PDF выше: внутри результаты анализа и рекомендации "
            "по улучшению видимости сайта в нейросетях.")

def tg_send_report(chat_id, pdf_path, o):
    """Сначала файл (без подписи), затем текстовое сообщение под ним."""
    tg_send_document(chat_id, pdf_path)            # файл без подписи
    tg_send_message(chat_id, report_ready_text(o)) # текст идёт ниже PDF

def deliver(o, pdf):
    """Доставка отчёта: основной канал Telegram, почта опциональный резерв."""
    sent = False
    if o["tg_chat_id"] and tg_token():
        try:
            tg_send_report(o["tg_chat_id"], pdf, o)
            sent = True
        except Exception as e:
            print("[tg] ошибка отправки:", e)
    if o["email"] and os.environ.get("SMTP_HOST"):
        try: send_report_email(o["email"], pdf, o["brand"])
        except Exception as e: print("[mail] ошибка:", e)
    return sent

# ───────────────────────── подтверждение запросов ───────────────────
def _review_text(prep, edited=False):
    """Сообщение клиенту со списком запросов на подтверждение (и при правке — на повторное подтверждение)."""
    qlist = "\n".join(f"{i}. {q['q']}" for i, q in enumerate(prep["queries"], 1))
    head = ("Обновил список запросов. Проверьте ещё раз." if edited
            else "Спасибо за оплату! Перед началом проверки подтвердите данные и выбранные запросы.")
    return (f"{head}\n\n"
            f"Бренд: {prep['brand']}\n"
            f"Сфера деятельности: {prep['niche']}\n\n"
            "Запросы — это формулировки, которые потенциальные клиенты вводят в ИИ-поиске. От них зависит весь отчёт.\n\n"
            "Как составить хороший запрос:\n"
            "• Это вопрос, на который нейросеть выдаёт СПИСОК компаний или сайтов: «Какие компании…», «На каком сайте купить…», «Кто делает…», «Найди компании, которые…».\n"
            "• Избегайте общих SEO-фраз вроде «производство … Москва» и вопросов «как выбрать / на что смотреть» — по ним нейросеть отвечает без названий, и замер будет пустым.\n"
            "• Отразите свои основные услуги или товары, целевых клиентов, географию и ценовой сегмент.\n\n"
            f"Запросы для проверки:\n\n{qlist}\n\n"
            "⚠️ После запуска проверки изменить запросы будет нельзя.\n\n"
            "Нажмите кнопку ниже: «Запустить проверку», если всё подходит, или «Изменить запросы», чтобы прислать свой список (каждый с новой строки).")

def _review_kb(oid):
    return [[{"text": "✅ Запустить проверку", "callback_data": f"go:{oid}"}],
            [{"text": "✏️ Изменить запросы", "callback_data": f"edit:{oid}"}]]

def _send_review(chat, oid, prep, edited=False, prefix=""):
    """Список запросов на подтверждение + кнопки (чтобы не набирать ОК руками)."""
    if chat:
        tg_send_buttons(chat, prefix + _review_text(prep, edited=edited), _review_kb(oid))

def _menu_kb():
    order_url = os.environ.get("ORDER_URL") or SITE
    support_url = os.environ.get("SUPPORT_URL") or "https://t.me/annakurbatovaai"
    return [[{"text": "📊 Заказать проверку видимости", "url": order_url}],
            [{"text": "🔁 Проверить другие запросы", "callback_data": "addon_menu"}],
            [{"text": "💬 Связаться с поддержкой", "url": support_url}]]

def _error_kb(oid):
    """Кнопки под сообщением об ошибке: повтор + сообщить в поддержку (уйдёт Анне)."""
    return [[{"text": "🔁 Попробовать ещё раз", "callback_data": f"retry:{oid}"}],
            [{"text": "✉️ Сообщить о проблеме в поддержку", "callback_data": f"problem:{oid}"}]]

def _send_menu(chat):
    """Главное меню бота: заказать проверку и поддержка."""
    tg_send_buttons(chat,
        "Здравствуйте! Это бот сервиса проверки AI-видимости бренда. "
        "Узнайте, рекомендуют ли нейросети вашу компанию, когда клиенты ищут в ИИ.\n\n"
        "Нажмите «Заказать проверку видимости», чтобы оформить отчёт. Если что-то не работает — «Связаться с поддержкой».",
        _menu_kb())

def _do_review(oid):
    """Готовит запросы (есть LLM-вызовы, потому фоном) и отправляет клиенту на подтверждение."""
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: return
    try:
        prep = engine.prepare(o["site"], o["niche"], o["city"], fallback_brand=o["brand_short"] or o["brand"])
    except Exception as e:
        print("[review] ошибка prepare:", e, flush=True)
        with db() as c: c.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
        return
    with db() as c:
        c.execute("UPDATE orders SET prep=?, brand=?, brand_short=?, niche=?, status='reviewing' WHERE id=?",
                  (json.dumps(prep, ensure_ascii=False), prep["brand"], prep["brand_short"], prep["niche"], oid))
    _send_review(o["tg_chat_id"], oid, prep, edited=False)

def maybe_start_review(oid):
    """После оплаты: готовим запросы (статус preparing) и одним сообщением просим подтвердить. Защита от двойного старта."""
    with db() as c:
        o = c.execute("SELECT status, tg_chat_id FROM orders WHERE id=?", (oid,)).fetchone()
        if not o or o["status"] != "paid":
            return False
        c.execute("UPDATE orders SET status='preparing' WHERE id=?", (oid,))
    threading.Thread(target=_do_review, args=(oid,), daemon=True).start()
    return True

def revise_queries(oid, edited):
    """Клиент прислал свой список: правим опечатки, ограничиваем по тарифу, показываем обновлённый список на повторное подтверждение."""
    with db() as c:
        o = c.execute("SELECT prep, tg_chat_id, kind, qn FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or not o["prep"]: return False
    cap = (o["qn"] or 10) if (o["kind"] or "main") == "addon" else 10   # main: максимум 10, addon: оплаченное число
    extra = len(edited) > cap
    edited = engine.fix_queries(edited[:cap])           # исправляем опечатки/грамматику
    prep = json.loads(o["prep"])
    prep["queries"] = [{"q": q, "group": engine._classify_query(q)} for q in edited]
    with db() as c:                                   # возвращаем в reviewing (если заказ был error/done) — ждём подтверждения
        c.execute("UPDATE orders SET prep=?, status='reviewing' WHERE id=?", (json.dumps(prep, ensure_ascii=False), oid))
    note = (f"⚠️ Вы прислали больше {cap} — взял первые {cap}.\n\n" if extra else _count_note(len(edited), cap))
    _send_review(o["tg_chat_id"], oid, prep, edited=True, prefix=note)
    return True

def start_generation(oid):
    """Запуск анализа по подтверждённому набору запросов (prep уже сохранён)."""
    with db() as c:
        o = c.execute("SELECT prep, tg_chat_id FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or not o["prep"]: return False
    with db() as c:
        c.execute("UPDATE orders SET status='processing' WHERE id=?", (oid,))
    if o["tg_chat_id"]:
        tg_send_message(o["tg_chat_id"], "Принято! Запускаю проверку по нейросетям, пришлю готовый отчёт сюда через несколько минут.")
    threading.Thread(target=generate, args=(oid,), daemon=True).start()
    return True

# ───────────────────────── генерация отчёта ──────────────────────────
def generate(order_id):
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o: return
    print(f"[generate] старт order={order_id} site={o['site']}", flush=True)
    try:
        prep = json.loads(o["prep"]) if o["prep"] else None
        data = engine.run(prep=prep) if prep else engine.run(o["brand"], o["site"], o["niche"], o["city"], brand_short=o["brand_short"])
        pdf = os.path.join(REPORTS, f"{order_id}.pdf")
        build_report.build(data, pdf)
        with db() as c:
            c.execute("UPDATE orders SET status='done', pdf=? WHERE id=?", (pdf, order_id))
            o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        sent = deliver(o, pdf)
        if sent and (o["kind"] or "main") == "main" and o["tg_chat_id"]:
            _send_feedback_request(o["tg_chat_id"], order_id)   # оценка 1-5 + оффер дозакупки после отзыва
        print(f"[generate] готово order={order_id} pdf_ok={os.path.exists(pdf)} отправлен_в_tg={sent}", flush=True)
    except Exception as e:
        tb = traceback.format_exc()
        print("[generate] ошибка:", tb, flush=True)
        with db() as c:
            c.execute("UPDATE orders SET status='error', err=? WHERE id=?", (tb[-1500:], order_id))
            o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        # Анне в личный чат — точная причина, чтобы чинить без логов Railway
        admin_notify(f"⚠️ Отчёт не собрался\nЗаказ: {order_id}\nСайт: {o['site'] if o else ''}\nОшибка: {e}\n\n{tb[-1200:]}")
        # клиенту — спокойное сообщение и кнопка повтора (запросы сохранены)
        if o and o["tg_chat_id"]:
            tg_send_buttons(o["tg_chat_id"],
                "Не удалось собрать отчёт с первой попытки — иногда нейросеть отвечает с задержкой. "
                "Запросы сохранены, ничего вводить заново не нужно. Нажмите «Попробовать ещё раз». "
                "Если не выйдет — отправьте проблему в поддержку, мы увидим и поможем вручную.",
                _error_kb(order_id))

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

SITE = "https://annakurbatova.ru"
THANKS_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Оплата прошла · Анна Курбатова</title>
<meta name=robots content="noindex">
<link rel="icon" href="__SITE__/favicons/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="__SITE__/css/site.css">
<style>
  body{background:var(--bg,#fff);color:var(--ink,#141210);overflow-x:hidden;}
  .ty-main{min-height:100vh;min-height:100svh;display:flex;flex-direction:column;
    align-items:center;justify-content:center;text-align:center;
    padding:clamp(120px,18vh,180px) 22px clamp(70px,10vh,110px);}
  .ty-card{max-width:560px;margin:0 auto;position:relative;z-index:2;}
  .ty-emoji{font-size:clamp(58px,11vw,84px);line-height:1;display:inline-block;
    animation:pop .7s cubic-bezier(.18,1.4,.4,1) both, floaty 3.4s ease-in-out 1s infinite;}
  .ty-eyebrow{display:inline-flex;align-items:center;gap:.7em;font-size:12px;font-weight:600;
    letter-spacing:.2em;text-transform:uppercase;color:var(--muted,#857F74);margin:22px 0 14px;}
  .ty-eyebrow::before{content:"";width:30px;height:2px;background:var(--coral,#DE4A2C);display:inline-block;}
  .ty-title{font-weight:500;font-size:clamp(34px,5.4vw,60px);line-height:1.04;
    letter-spacing:-.026em;margin:0;}
  .ty-sub{font-size:clamp(16px,1.5vw,20px);line-height:1.55;color:var(--ink-soft,#403B34);
    max-width:46ch;margin:18px auto 0;}
  .ty-btn{display:inline-flex;align-items:center;gap:.6em;margin-top:32px;border:0;cursor:pointer;
    font-family:inherit;font-size:16.5px;font-weight:600;text-decoration:none;border-radius:30px;
    padding:16px 30px;color:#fff;background:var(--coral,#DE4A2C);
    box-shadow:0 16px 34px -14px rgba(222,74,44,.7);
    transition:background .25s ease,transform .2s ease;}
  .ty-btn:hover{background:var(--coral-deep,#BE3A20);transform:translateY(-2px);}
  .ty-note{margin-top:18px;font-size:13.5px;color:var(--muted,#857F74);}
  .confetti{position:fixed;top:-14vh;z-index:1;pointer-events:none;will-change:transform;
    animation-name:cfall;animation-timing-function:cubic-bezier(.25,.6,.5,1);animation-fill-mode:forwards;}
  @keyframes cfall{0%{transform:translateY(0) rotateZ(0);opacity:1;}
    100%{transform:translateY(122vh) rotateZ(720deg);opacity:.95;}}
  @keyframes pop{0%{transform:scale(0) rotate(-18deg);opacity:0;}
    100%{transform:scale(1) rotate(0);opacity:1;}}
  @keyframes floaty{0%,100%{transform:translateY(0);}50%{transform:translateY(-9px);}}
  @media(max-width:760px){.nav .nav-links{display:none;}}
  @media(prefers-reduced-motion:reduce){.ty-emoji,.confetti{animation:none;}.confetti{display:none;}}
</style></head><body>

<nav class="nav solid" aria-label="Навигация">
  <a class="wordmark" href="__SITE__/">
    <img class="brand-logo" src="__SITE__/favicons/favicon.svg" alt="Анна Курбатова" width="42" height="42">
    <span class="wm-text"><span class="wm-name">Анна&nbsp;Курбатова<span class="dot">°</span></span>
    <span class="wm-desc">AI для бизнеса</span></span>
  </a>
  <div class="nav-links">
    <a href="__SITE__/#vozmojnosti">Возможности</a>
    <a href="__SITE__/#test">Бесплатный тест</a>
    <a href="__SITE__/#delayu">Как строится работа</a>
    <a href="__SITE__/#faq">Вопросы</a>
    <a href="__SITE__/blog/">Блог</a>
  </div>
  <a class="nav-cta" href="https://t.me/anna_kurbatova" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" width="18" height="18"><path d="M21 3 10.5 13.5M21 3l-6.5 18-4-8-8-4z"/></svg><span>Обсудить задачу</span></a>
</nav>

<main class="ty-main">
  <div class="ty-card">
    <div class="ty-emoji">🎉</div>
    <div class="ty-eyebrow">Оплата · успешно</div>
    <h1 class="ty-title">Оплата прошла</h1>
    <p class="ty-sub">Отчёт уже формируется. Готовый PDF появится в Telegram в течение 10 минут. Эту страницу можно закрыть.</p>
    <a class="ty-btn" href="__TGLINK__">Вернуться в Telegram &rarr;</a>
    <div class="ty-note">Письмо не придёт на почту: отчёт приходит прямо в чат бота.</div>
  </div>
</main>

<footer class="footer">
  <div class="wrap">
    <div class="footer-top">
      <div class="fw">Анна<br>Курбатова<span class="dot">°</span></div>
      <div class="fmeta">
        <a href="__SITE__/ai-audit.html">AI-аудит сайта</a>
        <a href="__SITE__/blog/">Блог</a>
        <a href="https://t.me/anna_kurbatova" target="_blank" rel="noopener">Telegram · @anna_kurbatova</a>
        <a href="https://wa.me/79851944826" target="_blank" rel="noopener">WhatsApp · +7 985 194-48-26</a>
      </div>
    </div>
    <hr>
    <div class="footer-bot">
      <span>Внешний руководитель цифрового развития и AI-внедрения</span>
      <span>ИНН 504508244657</span>
      <span>© 2026 Анна Курбатова</span>
    </div>
  </div>
</footer>

<script>
(function(){
  if(matchMedia('(prefers-reduced-motion:reduce)').matches) return;
  var colors=['#DE4A2C','#E8B33D','#2FA37C','#BE3A20','#141210','#F0C674'];
  var n = innerWidth < 600 ? 70 : 120;
  for(var i=0;i<n;i++){
    var c=document.createElement('div'); c.className='confetti';
    var s=6+Math.random()*8;
    c.style.left=(Math.random()*100)+'vw';
    c.style.width=s+'px'; c.style.height=(s*0.42)+'px';
    c.style.background=colors[i%colors.length];
    c.style.animationDelay=(Math.random()*0.7)+'s';
    c.style.animationDuration=(2.6+Math.random()*2.4)+'s';
    if(Math.random()>0.5) c.style.borderRadius='50%';
    document.body.appendChild(c);
  }
  setTimeout(function(){document.querySelectorAll('.confetti').forEach(function(e){e.remove();});},8000);
})();
</script>
</body></html>"""

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
            o = c.execute("SELECT status, tg_chat_id, kind, qn FROM orders WHERE id=?", (oid,)).fetchone()
        print(f"[notify] найден={bool(o)} статус={o['status'] if o else None} чат={o['tg_chat_id'] if o else None}", flush=True)
        if o and o["status"] in ("new", "pending"):
            if (o["kind"] or "main") == "addon":          # дозакупка: после оплаты просим запросы, а не запускаем сразу
                with db() as c:
                    c.execute("UPDATE orders SET status='await_queries', awaiting='addon_queries' WHERE id=?", (oid,))
                if o["tg_chat_id"]:
                    n = o["qn"] or 1
                    tg_send_message(o["tg_chat_id"],
                        f"Оплата получена! Пришлите {n} {_plural_q(n)} для проверки — каждый с новой строки. "
                        "Я покажу их на подтверждение, а потом запущу проверку и пришлю отчёт.")
            else:
                with db() as c:
                    c.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))   # фиксируем оплату, движок пока НЕ запускаем
                if o["tg_chat_id"]:           # клиент уже нажал Старт раньше оплаты -> показываем запросы на подтверждение
                    maybe_start_review(oid)
    return "OK"

@app.get("/thanks")
def thanks():
    oid = request.args.get("order", "")
    link = f"https://t.me/{tg_bot()}?start={oid}" if tg_bot() else SITE
    return THANKS_PAGE.replace("__TGLINK__", link).replace("__SITE__", SITE)

def _plural_q(n):
    n = abs(int(n)); d = n % 10; dd = n % 100
    if d == 1 and dd != 11: return "запрос"
    if 2 <= d <= 4 and not 12 <= dd <= 14: return "запроса"
    return "запросов"

def _count_note(n, tariff):
    """Предупреждение, если запросов меньше, чем доступно по тарифу (но запускать всё равно можно)."""
    if tariff and n < tariff:
        need = tariff - n
        return (f"⚠️ Вы прислали {n} из {tariff}. По вашему тарифу доступно до {tariff} {_plural_q(tariff)} — "
                f"добавьте ещё {need} {_plural_q(need)}, чтобы использовать проверку полностью, или подтвердите текущий набор.\n\n")
    return ""

def _parse_query_list(text):
    """Список запросов из текста клиента: чистим нумерацию, маркеры, короткие строки."""
    qs = []
    for l in text.splitlines():
        l = re.sub(r"^\s*\d+[.)]?\s+", "", l).strip(" -–—•\t").strip()   # «1.» «2)» и «10 » без точки
        if len(l) >= 6: qs.append(l)
    return qs[:12]

def tg_send_addon_payment_button(chat_id, pay_url, n, price):
    _tg("sendMessage", {"chat_id": chat_id,
        "text": f"Доп. проверка: {n} {_plural_q(n)} за {price} ₽.\n\nНажмите кнопку для оплаты. Сразу после оплаты я попрошу прислать запросы для проверки.",
        "reply_markup": {"inline_keyboard": [[{"text": f"Оплатить {price} ₽", "url": pay_url}]]}})

def _create_addon(parent_oid, n, chat_id):
    """Создаёт заказ-дозакупку (kind=addon) и шлёт кнопку оплаты на сумму по тарифу."""
    price = ADDON_PRICES.get(int(n))
    if not price: return
    with db() as c:
        p = c.execute("SELECT * FROM orders WHERE id=?", (parent_oid,)).fetchone()
    if not p:
        tg_send_message(chat_id, "Не нашёл исходный заказ. Напишите нам, поможем."); return
    aid = uuid.uuid4().hex[:16]
    with db() as c:
        c.execute("INSERT INTO orders(id,created,status,brand,brand_short,site,niche,city,email,tg_chat_id,kind,parent,qn,amount) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, time.time(), "new", p["brand"], p["brand_short"], p["site"], p["niche"], p["city"], p["email"],
                   str(chat_id), "addon", parent_oid, int(n), price))
    try:
        res = tbank_init(aid, p["email"], amount=price, description=f"Доп. проверка видимости: {n} {_plural_q(n)}")
    except Exception as e:
        print("[addon] init:", e); tg_send_message(chat_id, "Оплата временно недоступна, попробуйте позже."); return
    if res.get("Success"):
        with db() as c: c.execute("UPDATE orders SET status='pending', payment_id=? WHERE id=?", (res.get("PaymentId"), aid))
        tg_send_addon_payment_button(chat_id, res["PaymentURL"], int(n), price)
    else:
        tg_send_message(chat_id, "Не удалось создать оплату, попробуйте ещё раз чуть позже.")

def _addon_collect_queries(oid, qs):
    """Клиент прислал запросы для доп.проверки -> строим prep из родителя и показываем на подтверждение."""
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: return
    paid = o["qn"] or len(qs); raw_n = len(qs)
    qs = engine.fix_queries(qs[:paid])                        # ограничиваем оплаченным числом и правим опечатки
    over = raw_n > paid
    note = (f"⚠️ Вы прислали больше {paid} — взял первые {paid}.\n\n" if over else _count_note(len(qs), paid))
    parent_prep = None
    if o["parent"]:
        with db() as c:
            p = c.execute("SELECT prep FROM orders WHERE id=?", (o["parent"],)).fetchone()
        if p and p["prep"]: parent_prep = json.loads(p["prep"])
    prep = dict(parent_prep) if parent_prep else engine.prepare(o["site"], o["niche"], o["city"], fallback_brand=o["brand_short"] or o["brand"])
    prep["queries"] = [{"q": q, "group": engine._classify_query(q)} for q in qs]
    with db() as c:
        c.execute("UPDATE orders SET prep=?, status='reviewing', awaiting=NULL WHERE id=?",
                  (json.dumps(prep, ensure_ascii=False), oid))
    _send_review(o["tg_chat_id"], oid, prep, edited=False, prefix=note)

def _handle_callback(cq):
    data = cq.get("data") or ""
    chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
    tg_answer_callback(cq.get("id"))
    if not chat: return "OK"
    if data.startswith("go:") or data.startswith("retry:"):   # «Запустить проверку» / «Попробовать ещё раз»
        oid = data.split(":", 1)[1]
        with db() as c:
            o = c.execute("SELECT status, prep, pdf FROM orders WHERE id=?", (oid,)).fetchone()
        if o and o["status"] in ("reviewing", "error") and o["prep"]:
            start_generation(oid)                 # error -> перезапуск по сохранённым запросам
        elif o and o["status"] == "processing":
            tg_send_message(chat, "Уже запускаю проверку, отчёт будет здесь через несколько минут.")
        elif o and o["status"] == "done":
            if o["pdf"] and os.path.exists(o["pdf"]):
                with db() as c:
                    full = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                try: tg_send_report(chat, o["pdf"], full)
                except Exception as e: print("[tg] ошибка:", e)
            else:
                tg_send_message(chat, "Этот отчёт уже готов.")
        else:
            tg_send_buttons(chat, "Не получилось продолжить по этому заказу. Можно сообщить в поддержку — мы увидим и поможем вручную.",
                            [[{"text": "✉️ Сообщить о проблеме в поддержку", "callback_data": f"problem:{oid}"}]])
        return "OK"
    if data.startswith("problem:"):         # «Сообщить о проблеме» -> пересылаем Анне с сохранённой ошибкой
        oid = data.split(":", 1)[1]
        with db() as c:
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        site = (o["site"] if o else "") or "—"
        err = ((o["err"] if o and "err" in o.keys() else "") or "")
        admin_notify("🆘 Клиент сообщил о проблеме\n"
                     f"Заказ: {oid}\nСайт: {site}\nСтатус: {(o['status'] if o else '—')}\nЧат клиента: {chat}"
                     + (f"\n\nПоследняя ошибка:\n{err[-1000:]}" if err else ""))
        tg_send_message(chat, "Передал в поддержку. Мы уже разбираемся и свяжемся с вами здесь. Спасибо!")
        return "OK"
    if data.startswith("edit:"):            # кнопка «Изменить запросы»
        tg_send_force_reply(chat, "Пришлите свои запросы списком — каждый с новой строки. Я исправлю опечатки, заменю набор и снова покажу на подтверждение.",
                            "Запрос 1, запрос 2… каждый с новой строки")
        return "OK"
    if data.startswith("rate:"):
        try: _, n, oid = data.split(":", 2)
        except ValueError: return "OK"
        with db() as c:
            c.execute("UPDATE orders SET rating=?, awaiting='fb_text' WHERE id=?", (int(n), oid))
            o = c.execute("SELECT brand, site FROM orders WHERE id=?", (oid,)).fetchone()
        admin_notify(f"Оценка отчёта: {n}/5\nЗаказ: {oid}\nБренд: {(o['brand'] if o else '') or ''}\nСайт: {(o['site'] if o else '') or ''}")
        tg_send_message(chat, "Спасибо за оценку! Если хотите, в одном сообщении напишите, чего не хватило или что было особенно полезно.")
        _send_addon_offer(chat, oid)              # оффер дозакупки сразу после оценки, не дожидаясь текста
        return "OK"
    if data.startswith("buy:"):
        try: _, n, parent = data.split(":", 2)
        except ValueError: return "OK"
        _create_addon(parent, int(n), chat)
        return "OK"
    if data == "addon_no":
        tg_send_message(chat, "Хорошо! Захотите проверить другие запросы — нажмите «Проверить другие запросы» в меню (/start) в любой момент.")
        return "OK"
    if data == "addon_menu":                # дозакупка из меню — по последнему готовому основному отчёту
        with db() as c:
            o = c.execute("SELECT id FROM orders WHERE tg_chat_id=? AND status='done' AND (kind IS NULL OR kind='main') "
                          "ORDER BY created DESC LIMIT 1", (str(chat),)).fetchone()
        if o:
            _send_addon_offer(chat, o["id"])
        else:
            tg_send_message(chat, "Докупить запросы можно после готового отчёта. Сначала закажите проверку — кнопка «Заказать проверку видимости».")
        return "OK"
    return "OK"

@app.post("/telegram/webhook")
def tg_webhook():
    upd = request.get_json(force=True, silent=True) or {}
    if upd.get("callback_query"):
        return _handle_callback(upd["callback_query"])
    msg = upd.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat:
        return "OK"
    if text.startswith("/start"):
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
                    try: tg_send_report(chat, o["pdf"], o)
                    except Exception as e: print("[tg] ошибка:", e)
                elif st == "paid":      # оплачено -> готовим запросы; список придёт одним сообщением
                    maybe_start_review(oid)
                elif st == "preparing":
                    tg_send_message(chat, "Оплата получена. Готовлю запросы для проверки, пришлю их сюда в течение минуты.")
                elif st == "reviewing":
                    if o["prep"]:
                        _send_review(chat, oid, json.loads(o["prep"]))   # повторно показываем список с кнопками
                    else:
                        tg_send_message(chat, "Готовлю запросы для проверки, пришлю их сюда через минуту.")
                elif st == "await_queries":   # дозакупка оплачена -> ждём запросы клиента
                    n = o["qn"] or 1
                    tg_send_message(chat, f"Жду ваши {n} {_plural_q(n)} для проверки — каждый с новой строки.")
                elif st == "processing":
                    tg_send_message(chat, "Отчёт уже формируется, пришлю его сюда через пару минут.")
                elif st == "error":     # прошлая проверка упала -> повтор по сохранённым запросам + кнопка в поддержку
                    if o["prep"]:
                        tg_send_buttons(chat, "Прошлая проверка не завершилась. Запросы сохранены — нажмите «Попробовать ещё раз». "
                                              "Если не выйдет, отправьте проблему в поддержку.", _error_kb(oid))
                    else:
                        tg_send_buttons(chat, "Прошлая проверка не завершилась. Отправьте проблему в поддержку — мы увидим и поможем вручную.",
                                        [[{"text": "✉️ Сообщить о проблеме в поддержку", "callback_data": f"problem:{oid}"}]])
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
                    if (o["kind"] or "main") == "addon":
                        tg_send_message(chat, "Кнопка оплаты выше. Сразу после оплаты я попрошу прислать запросы для проверки.")
                    else:
                        tg_send_message(chat, "Кнопка оплаты выше. После оплаты отчёт придёт сюда автоматически.")
                return "OK"
        _send_menu(chat)
        return "OK"
    # свободный текст
    if text:
        # принимаем правку запросов не только в reviewing, но и по упавшему заказу (error); админ может и по готовому (done) — для тестов
        admin = str(chat) == str(os.environ.get("ADMIN_CHAT_ID") or "")
        states = ("reviewing", "error", "done") if admin else ("reviewing", "error")
        ph = ",".join("?" * len(states))
        with db() as c:
            o = c.execute(f"SELECT * FROM orders WHERE tg_chat_id=? AND status IN ({ph}) ORDER BY created DESC LIMIT 1",
                          (str(chat), *states)).fetchone()
        if o:                                  # подтверждение или правка запросов
            if not o["prep"]:
                tg_send_message(chat, "Секунду, ещё готовлю запросы — пришлю их сюда.")
                return "OK"
            low = text.lower().strip(" .!…")
            if low in ("ок","ok","да","да.","подтверждаю","запуск","поехали","go","ага","верно","всё верно","все верно","норм"):
                start_generation(o["id"])
            else:
                qs = _parse_query_list(text)
                if len(qs) >= 1:                  # принимаем любой непустой список; если меньше тарифа — предупредим на подтверждении
                    revise_queries(o["id"], qs)   # заменяем и показываем обновлённый список на повторное «ОК»
                else:
                    tg_send_message(chat, "Чтобы заменить запросы, пришлите их списком — каждый с новой строки. Или ответьте ОК, чтобы запустить по показанному набору.")
            return "OK"
        # обратная связь и дозакупка
        with db() as c:
            a = c.execute("SELECT * FROM orders WHERE tg_chat_id=? AND awaiting IS NOT NULL ORDER BY created DESC LIMIT 1",
                          (str(chat),)).fetchone()
        if a:
            if a["awaiting"] == "fb_text":
                with db() as c:
                    c.execute("UPDATE orders SET feedback=?, awaiting=NULL WHERE id=?", (text, a["id"]))
                admin_notify(f"Отзыв об отчёте\nЗаказ: {a['id']}\nБренд: {a['brand'] or ''}\n"
                             f"Оценка: {a['rating'] if a['rating'] is not None else '—'}/5\n\n{text}")
                tg_send_message(chat, "Спасибо за обратную связь! Учту в работе.")   # оффер уже показан после оценки
                return "OK"
            if a["awaiting"] == "addon_queries":
                qs = _parse_query_list(text)
                if len(qs) >= 1:
                    _addon_collect_queries(a["id"], qs)
                else:
                    n = a["qn"] or 1
                    tg_send_message(chat, f"Пришлите запросы для проверки — каждый с новой строки (до {n}).")
                return "OK"
        _send_menu(chat)              # нет активного заказа -> показываем меню (заказать / поддержка)
        return "OK"
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
    try:
        with db() as c:
            rows = c.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall()
            last = c.execute("SELECT id, status, site, tg_chat_id, err FROM orders ORDER BY created DESC LIMIT 1").fetchone()
        orders = {r[0]: r[1] for r in rows}
        last_order = {"id": last["id"], "status": last["status"], "site": last["site"],
                      "chat_linked": bool(last["tg_chat_id"]),
                      "err": (last["err"] or "")[-1200:]} if last else None
    except Exception as e:
        orders, last_order = {"err": str(e)}, None
    return jsonify(ok=True, terminal=TERMINAL, price=PRICE, base_url=BASE_URL,
                   notify_url=f"{BASE_URL}/tbank/notify", test_mode=os.environ.get("TEST_MODE") == "1",
                   telegram=bool(tg_token()), bot=tg_bot(), admin=bool(os.environ.get("ADMIN_CHAT_ID")),
                   orders=orders, last_order=last_order, keys=keys)

@app.get("/selftest")
def selftest():
    """Диагностика: 1 запрос на каждую ПОДКЛЮЧЁННУЮ нейросеть, показывает ответ или ошибку.
    Стоит копейки (по 1 короткому запросу). Если задан SELFTEST_TOKEN — требуем ?token=."""
    tok = os.environ.get("SELFTEST_TOKEN")
    if tok and request.args.get("token") != tok:
        abort(403)
    niche = request.args.get("niche", "мебель на заказ")
    city = request.args.get("city", "")
    prompt = engine.generate_queries(niche, city)[0]["q"]
    out = {}
    for e in engine.active_engines():
        eid = e["id"]; t0 = time.time()
        try:
            if os.environ.get("TEST_MODE") == "1" or not engine.has_key(eid):
                ans = engine.ask_mock(prompt, eid, 0, "тест"); mode = "mock"
            else:
                ans = engine.REAL_ADAPTERS[eid](prompt); mode = "real"
            out[eid] = {"ok": True, "mode": mode, "ms": int((time.time()-t0)*1000),
                        "len": len(ans or ""), "snippet": (ans or "")[:200]}
        except Exception as ex:
            out[eid] = {"ok": False, "ms": int((time.time()-t0)*1000), "error": f"{type(ex).__name__}: {str(ex)[:300]}"}
    return jsonify(prompt=prompt, test_mode=os.environ.get("TEST_MODE") == "1", engines=out)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
