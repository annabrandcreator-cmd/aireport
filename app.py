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
import os, json, time, hashlib, sqlite3, threading, smtplib, ssl, urllib.request, uuid, re, traceback, html, csv, io
from datetime import timedelta
from email.message import EmailMessage
from flask import Flask, request, redirect, jsonify, send_file, abort, session

import engine, build_report

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH") or os.path.join(APP_DIR, "orders.db")
REPORTS = os.environ.get("REPORTS_DIR") or os.path.join(APP_DIR, "reports")
os.makedirs(REPORTS, exist_ok=True)

VERSION = "v96"                           # маркер сборки -> видно в /health, чтобы убедиться что задеплоен свежий код
TERMINAL = os.environ.get("TBANK_TERMINAL", "1782125233968DEMO").strip()  # .strip() — от случайных пробелов/переноса при вставке
PRICE = int(os.environ.get("PRICE_RUB", "1290"))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").strip().rstrip("/")
if BASE_URL and not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL      # подстраховка: добавим схему, если её забыли
TBANK_INIT = "https://securepay.tinkoff.ru/v2/Init"
TBANK_CANCEL = "https://securepay.tinkoff.ru/v2/Cancel"   # возврат/отмена (для чека возврата, тест-кейс №8)
def tg_token(): return os.environ.get("TELEGRAM_BOT_TOKEN", "")            # читаем live при каждом вызове
def tg_bot():   return os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024     # тело запроса не больше 512 КБ — отсекаем «толстые» мусорные POST
# вход в CRM — по паролю (см. /admin/login). Куку входа подписываем; ключ берём из env или из ADMIN_TOKEN, отдельной настройки не нужно
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("crm-sk:" + (os.environ.get("ADMIN_TOKEN") or "dev")).encode()).hexdigest()
app.permanent_session_lifetime = timedelta(days=14)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("BASE_URL", "").startswith("https")

# ───────────── конфиг безопасности и промокодов (всё из env, секретов в коде нет) ─────────────
SITE_ORIGIN = "https://annakurbatova.ru"                                               # внешний домен (стили/иконки на странице «спасибо»)
PROMO_FREE_CODE = (os.environ.get("PROMO_FREE_CODE", "ascend") or "").strip().lower()   # промокод 100% скидки (для тестов); меняется через env
PROMO_FREE_DAILY_MAX = int(os.environ.get("PROMO_FREE_DAILY_MAX", "25") or 25)          # потолок бесплатных заказов в сутки — страховка, если код утечёт
TG_WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "").strip()                     # секрет телеграм-вебхука: задан -> проверяем заголовок
RATE_LIMIT_OFF = os.environ.get("RATE_LIMIT_OFF") == "1"                                # аварийный выключатель лимитов

_RL = {}; _RL_LOCK = threading.Lock()                                                   # простой счётчик частоты запросов (в памяти воркера)
def rate_ok(key, limit, per_sec):
    """True, пока для ключа (обычно IP+маршрут) не превышен лимит за окно per_sec секунд."""
    if RATE_LIMIT_OFF:
        return True
    now = time.time(); cutoff = now - per_sec
    with _RL_LOCK:
        q = _RL.setdefault(key, [])
        while q and q[0] < cutoff:
            q.pop(0)
        if len(q) >= limit:
            return False
        q.append(now)
        if len(_RL) > 5000:                                                             # лёгкая уборка, чтобы словарь не рос
            for k in [k for k, v in list(_RL.items()) if not v or v[-1] < cutoff]:
                _RL.pop(k, None)
        return True

def client_ip():
    """IP клиента за прокси (Railway/Caddy): первый из X-Forwarded-For, иначе remote_addr."""
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else "") or (request.remote_addr or "?")

def _mask_email(e):
    """a***@mail.ru — почту клиента в наших интерфейсах целиком не показываем (персональные данные)."""
    e = (e or "").strip()
    if "@" not in e:
        return ""
    name, dom = e.split("@", 1)
    return ((name[0] + "***") if name else "***") + "@" + dom

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,24}$")
def _valid_email(e):
    return bool(_EMAIL_RE.match((e or "").strip()))

@app.after_request
def _security_headers(resp):
    """Базовые заголовки безопасности на каждый ответ (п.4 чек-листа)."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    resp.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; "
        f"img-src 'self' data: {SITE_ORIGIN}; "
        f"style-src 'self' 'unsafe-inline' {SITE_ORIGIN}; "
        "script-src 'self' 'unsafe-inline'; "
        f"font-src 'self' data: {SITE_ORIGIN}; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
    if request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp

@app.errorhandler(Exception)
def _on_error(e):
    """Клиенту — нейтральное сообщение, подробности только в лог сервера (п.11 чек-листа)."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e                                       # 403/404/405 и т.п. отдаём как есть
    print("[error]", repr(e), flush=True)
    return err_page("Что-то пошло не так. Попробуйте ещё раз или напишите нам.", 500)

# ───────────────────────── хранилище заказов ─────────────────────────
def db():
    # WAL + busy_timeout: чтобы одновременные заказы (две генерации в потоках) не падали на «database is locked»
    c = sqlite3.connect(DB, timeout=15); c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass
    return c

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
                         ("kind","TEXT"),("parent","TEXT"),("qn","INTEGER"),("amount","INTEGER"),("err","TEXT"),
                         ("promo","TEXT"),("tg_username","TEXT"),("tg_name","TEXT")]:
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

def tbank_receipt(email, rub, name=None):
    """Объект чека (54-ФЗ) для метода Init. СНО и НДС зависят от системы налогообложения —
    задаются переменными окружения (по умолчанию УСН-доходы, без НДС, услуга).
    В подпись (Token) чек не входит — ТБанк его не учитывает."""
    item = {
        "Name": (name or "Отчёт о видимости бренда в нейросетях")[:128],
        "Price": rub * 100,
        "Quantity": 1,
        "Amount": rub * 100,                                 # = Price * Quantity
        "Tax": os.environ.get("TBANK_VAT", "none"),          # НДС: none — для УСН/без НДС
        "PaymentMethod": "full_payment",                     # полный расчёт
        "PaymentObject": os.environ.get("TBANK_PAYMENT_OBJECT", "service"),  # услуга
        "MeasurementUnit": "шт",                             # обязателен для ФФД 1.2; в 1.05 игнорируется
    }
    rcpt = {"Taxation": os.environ.get("TBANK_TAXATION", "usn_income"), "Items": [item]}
    # E-mail для чека НЕ собираем сами (минимизация перс. данных) — его вводит покупатель
    # на платёжной форме ТБанка, и сервис «Чеки» отправляет чек туда. Передаём контакт,
    # только если он почему-то задан (резервная почта в окружении) — обычно нет.
    em = (email or os.environ.get("TBANK_RECEIPT_EMAIL", "")).strip()
    phone = os.environ.get("TBANK_RECEIPT_PHONE", "").strip()
    if em:        rcpt["Email"] = em[:64]
    elif phone:   rcpt["Phone"] = phone
    # иначе Email/Phone не передаём — почту покупателя соберёт форма ТБанка
    return rcpt

def _receipt_on():
    return os.environ.get("TBANK_RECEIPT", "").strip().lower() in ("1", "true", "yes", "on")

def tbank_init(order_id, email, amount=None, description=None, with_receipt=None):
    password = os.environ["TBANK_PASSWORD"].strip()  # из секрета, не из кода; .strip() от случайных пробелов/переноса
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
    # Чек (54-ФЗ) добавляем, только если онлайн-касса реально подключена к терминалу:
    # включается флагом TBANK_RECEIPT=1. Иначе ТБанк отклонит Init и оплата не создастся.
    # /tbank/selftest зовёт с with_receipt=True, чтобы протестировать чек, не трогая боевую оплату.
    if with_receipt if with_receipt is not None else _receipt_on():
        payload["Receipt"] = tbank_receipt(email, rub, payload["Description"])
    payload["Token"] = tbank_token(payload, password)
    req = urllib.request.Request(TBANK_INIT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

# ───────────────────────── письма ───────────────────────────────────
def report_filename(brand):
    """Имя файла отчёта с названием компании: Отчёт-AI-видимость-<Компания>.pdf"""
    b = re.sub(r'[«»"\'<>:/\\|?*\r\n\t]', "", (brand or "")).strip()
    b = re.sub(r"\s+", " ", b)[:60].strip(" -–—.")
    return f"Отчёт-AI-видимость-{b}.pdf" if b else "Отчёт-AI-видимость.pdf"

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
                           filename=report_filename(brand))
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

def tg_send_document(chat_id, pdf_path, caption="", filename=None):
    with open(pdf_path, "rb") as f: content = f.read()
    return _tg("sendDocument", {"chat_id": str(chat_id), "caption": caption},
               {"document": (filename or "Отчёт-AI-видимость.pdf", content, "application/pdf")})

def tg_send_payment_button(chat_id, pay_url, brand):
    _tg("sendMessage", {"chat_id": chat_id,
        "text": f"Заказ на отчёт о видимости «{brand}» в нейросетях принят.\n\nНажмите кнопку, чтобы оплатить {PRICE} ₽. Сразу после оплаты я пришлю готовый отчёт сюда, в этот чат.",
        "reply_markup": {"inline_keyboard": [[{"text": f"Оплатить {PRICE} ₽", "url": pay_url}]]}})

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
    tg_send_document(chat_id, pdf_path, filename=report_filename(o["brand_short"] or o["brand"]))  # файл с названием компании
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
    if prep.get("niche_unknown") or not prep.get("queries"):   # нишу не определили -> спрашиваем у клиента, а не выдумываем
        with db() as c:
            c.execute("UPDATE orders SET prep=?, brand=?, brand_short=?, status='await_niche' WHERE id=?",
                      (json.dumps(prep, ensure_ascii=False), prep["brand"], prep["brand_short"], oid))
        _ask_niche(o["tg_chat_id"], o["site"])
        return
    with db() as c:
        c.execute("UPDATE orders SET prep=?, brand=?, brand_short=?, niche=?, status='reviewing' WHERE id=?",
                  (json.dumps(prep, ensure_ascii=False), prep["brand"], prep["brand_short"], prep["niche"], oid))
    _send_review(o["tg_chat_id"], oid, prep, edited=False)

def _ask_niche(chat, site):
    """Не удалось определить нишу по сайту -> просим клиента написать её одной фразой."""
    host = engine._host(site) if site else "вашему сайту"
    tg_send_force_reply(chat,
        f"Не удалось автоматически определить, чем занимается компания по сайту {host}. "
        "Напишите одной фразой вашу нишу — чем вы занимаетесь, какие услуги или товары "
        "(например: загородный отель в Подмосковье, стоматология, интернет-магазин косметики, производство мебели). "
        "Я подберу запросы под неё.",
        "Ваша ниша одной фразой")

def _set_niche(oid, niche_text):
    """Клиент прислал нишу вручную -> пересобираем запросы и показываем на подтверждение."""
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o: return
    niche = (niche_text or "").strip()[:120]
    try:
        prep = engine.prepare(o["site"], niche, o["city"], fallback_brand=o["brand_short"] or o["brand"])
    except Exception as e:
        print("[niche] ошибка prepare:", e, flush=True)
        prep = {"site": o["site"], "city": o["city"], "niche": niche, "brand": o["brand"], "brand_short": o["brand_short"], "queries": []}
    prep["niche"] = niche; prep["niche_unknown"] = False
    if not prep.get("queries"):                              # на всякий случай — соберём по введённой нише
        try:
            qs = engine.generate_queries(niche, o["city"], prep.get("site_info"))
            prep["queries"] = [{"q": x["q"], "group": x["group"]} for x in qs]
        except Exception as e:
            print("[niche] ошибка generate:", e, flush=True)
    with db() as c:
        c.execute("UPDATE orders SET prep=?, niche=?, status='reviewing', awaiting=NULL WHERE id=?",
                  (json.dumps(prep, ensure_ascii=False), niche, oid))
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
        failed_nets = data.get("failed_engines") or []
        if failed_nets:                       # сеть не ответила даже после спасательного прохода -> сигнал Анне
            admin_notify("⚠️ В отчёте не ответили нейросети: " + ", ".join(failed_nets) + "\n"
                         f"Заказ: {order_id} · {o['site']}\n"
                         "Клиент получил отчёт без них. Проверь /selftest и при необходимости перегенерируй заказ.")
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
ORDER_FORM = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отчёт о видимости бизнеса в нейросетях · __PRICE__ ₽ · Анна Курбатова</title>
<meta name="description" content="Проверим, рекомендуют ли вас нейросети: ChatGPT, Яндекс Нейро и ещё 5 AI-сервисов по 10 коммерческим запросам. Отчёт с конкурентами и планом действий придёт в Telegram.">
<meta name="theme-color" content="#DE4A2C">
<meta property="og:title" content="Отчёт о видимости бизнеса в нейросетях">
<meta property="og:description" content="Проверим ваш бизнес по 10 запросам в 7 нейросетях. Кого рекомендуют вместо вас и что изменить. Отчёт придёт в Telegram.">
<style>
@font-face{font-family:"Gilroy";src:url("/fonts/Gilroy-Regular.ttf") format("truetype");font-weight:400;font-display:swap}
@font-face{font-family:"Gilroy";src:url("/fonts/Gilroy-Medium.ttf") format("truetype");font-weight:500;font-display:swap}
@font-face{font-family:"Gilroy";src:url("/fonts/Gilroy-Semibold.ttf") format("truetype");font-weight:600;font-display:swap}
@font-face{font-family:"Gilroy";src:url("/fonts/Gilroy-Bold.ttf") format("truetype");font-weight:700;font-display:swap}
:root{--bg:#FFFFFF;--ink:#141210;--ink-soft:#403B34;--muted:#857F74;--line-strong:#D6CFC2;--coral:#DE4A2C;--coral-deep:#BE3A20;--pad:clamp(22px,5vw,84px);--ease:cubic-bezier(.22,.61,.36,1)}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{font-family:"Gilroy",system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--ink);line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
::selection{background:var(--coral);color:#fff}
.nav{position:fixed;top:0;left:0;right:0;z-index:20;display:flex;align-items:center;justify-content:space-between;padding:18px var(--pad);transition:background .4s var(--ease),box-shadow .4s var(--ease)}
.nav.solid{background:rgba(255,255,255,.86);backdrop-filter:blur(10px);box-shadow:0 1px 0 rgba(20,18,16,.06)}
.wm{display:flex;flex-direction:column;line-height:1.05}
.wm-name{font-size:17px;font-weight:600;letter-spacing:-.01em}
.wm-name .dot{color:var(--coral)}
.wm-desc{font-size:10px;font-weight:600;letter-spacing:.24em;text-transform:uppercase;color:var(--muted);margin-top:3px}
.nav-cta{display:inline-flex;align-items:center;gap:.55em;font-size:14px;font-weight:600;color:#fff;background:var(--ink);padding:11px 20px;border-radius:6px;transition:background .3s var(--ease),transform .3s var(--ease)}
.nav-cta:hover{background:var(--coral);transform:translateY(-1px)}
.nav-cta svg{width:16px;height:16px}
#ord{max-width:940px;margin:0 auto;padding:clamp(112px,14vh,150px) var(--pad) clamp(56px,8vh,96px);min-height:100vh;min-height:100svh;display:flex;flex-direction:column;justify-content:center}
.ord-head{text-align:center;margin-bottom:8px}
.ord-eyebrow{display:inline-flex;align-items:center;gap:.7em;font-size:12px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin-bottom:18px}
.ord-eyebrow::before{content:"";width:30px;height:2px;background:var(--coral);display:inline-block}
.ord-title{font-weight:500;font-size:clamp(30px,4.4vw,52px);line-height:1.05;letter-spacing:-.024em;margin:0}
.ord-title b{color:var(--coral);font-weight:500}
.ord-sub{font-size:clamp(16px,1.4vw,20px);line-height:1.5;color:var(--ink-soft);max-width:62ch;margin:18px auto 0}
.ord-form{max-width:480px;margin:34px auto 0;display:flex;flex-direction:column;gap:12px;width:100%}
.ord-form label{text-align:left;font-size:13px;color:var(--muted);margin:6px 0 -4px 2px}
.ord-form input{background:#fff;border:1.5px solid var(--line-strong);border-radius:12px;color:var(--ink);font-family:inherit;font-size:16px;padding:15px 16px;outline:0;transition:border-color .25s var(--ease);width:100%}
.ord-form input:focus{border-color:var(--ink)}
.ord-form input::placeholder{color:var(--muted)}
.ord-btn{margin-top:8px;border:0;cursor:pointer;font-family:inherit;font-size:16.5px;font-weight:600;border-radius:12px;padding:17px;color:#fff;background:var(--coral);box-shadow:0 14px 32px -14px rgba(222,74,44,.7);transition:background .25s var(--ease),transform .2s var(--ease)}
.ord-btn:hover{background:var(--coral-deep);transform:translateY(-1px)}
.ord-badges{display:flex;justify-content:center;flex-wrap:wrap;gap:8px 20px;margin-top:22px;color:var(--ink-soft);font-size:13.5px}
.ord-badges span{display:inline-flex;align-items:center;gap:7px}
.ord-badges b{color:var(--ink);font-weight:600}
.ord-hint{text-align:center;color:var(--muted);font-size:13.5px;margin-top:16px;line-height:1.55;max-width:480px;margin-left:auto;margin-right:auto}
.ord-hint a{color:var(--muted);text-decoration:underline;text-underline-offset:2px}
.ord-form .hp{position:absolute!important;left:-9999px;width:1px;height:1px;opacity:0;overflow:hidden;pointer-events:none}
.footer{border-top:1px solid var(--line-strong);padding:26px var(--pad);display:flex;flex-wrap:wrap;gap:6px 18px;justify-content:center;text-align:center;color:var(--muted);font-size:12.5px}
.footer a{color:var(--muted);text-decoration:underline;text-underline-offset:2px}
.footer a:hover{color:var(--ink)}
</style></head><body>
<nav class="nav">
  <a class="wm" href="https://annakurbatova.ru/"><span class="wm-name">Анна&nbsp;Курбатова<span class="dot">°</span></span><span class="wm-desc">AI для бизнеса</span></a>
  <a class="nav-cta" href="https://t.me/anna_kurbatova" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"><path d="M21 3 10.5 13.5M21 3l-6.5 18-4-8-8-4z"/></svg><span>Обсудить задачу</span></a>
</nav>
<main><div id="ord">
  <div class="ord-head">
    <span class="ord-eyebrow">AI-видимость · отчёт</span>
    <h1 class="ord-title">Узнайте, рекомендуют&nbsp;ли вас <b>нейросети</b></h1>
    <p class="ord-sub">Проверим по 10 запросам в 7 нейросетях, покажем кого советуют вместо вас. Отчёт придёт в Telegram.</p>
  </div>
  <form class="ord-form" method="post" action="/create-payment">
    <input type="text" name="site" placeholder="Адрес сайта — ваш-сайт.ру" aria-label="Адрес сайта" required>
    <input type="text" name="niche" placeholder="Чем занимаетесь — напр. стоматология" aria-label="Чем занимаетесь" required>
    <input type="text" name="city" placeholder="Город (если важен регион для поиска)" aria-label="Город">
    <input type="email" name="email" placeholder="E-mail (для чека)" aria-label="E-mail для чека" required>
    <input class="hp" type="text" name="company" tabindex="-1" autocomplete="off" aria-hidden="true">
    <input type="text" name="promo" placeholder="Промокод (если есть)" aria-label="Промокод" autocomplete="off" maxlength="40">
    <button class="ord-btn" type="submit">Получить отчёт за __PRICE__ ₽ &rarr;</button>
  </form>
  <div class="ord-badges"><span><b>7</b> нейросетей</span><span><b>140</b> проверок</span><span>отчёт за <b>10 минут</b></span></div>
  <div class="ord-hint">Оплата картой или СБП. После оплаты — переход в Telegram-бот за отчётом. Чек придёт на указанный e-mail.<br>Нажимая кнопку, вы соглашаетесь с <a href="https://annakurbatova.ru/privacy.html" target="_blank" rel="noopener">политикой конфиденциальности</a>.</div>
</div></main>
<footer class="footer">
  <span>© 2026 Анна Курбатова</span><span>ИНН 504508244657</span>
  <a href="https://annakurbatova.ru/privacy.html" target="_blank" rel="noopener">Политика конфиденциальности</a>
</footer>
<script>(function(){var n=document.querySelector('.nav');if(!n)return;var f=function(){n.classList.toggle('solid',window.scrollY>40)};f();window.addEventListener('scroll',f,{passive:true})})();</script>
</body></html>"""

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
    return ORDER_FORM.replace("__PRICE__", str(PRICE))        # цена на витрине из PRICE_RUB

@app.get("/fonts/<fn>")
def fonts(fn):                                                # отдаём шрифты Gilroy того же домена (без CORS-проблем)
    if not re.fullmatch(r"Gilroy-(Regular|Medium|Semibold|Bold)\.ttf", fn or ""):
        abort(404)
    return send_file(os.path.join(APP_DIR, "fonts", fn), mimetype="font/ttf", max_age=2592000)

@app.post("/create-payment")
def create_payment():
    if not rate_ok(f"cp:{client_ip()}", 6, 120):                      # не больше 6 заявок за 2 минуты с одного IP
        return err_page("Слишком много попыток подряд. Подождите минуту и попробуйте снова.", 429)
    f = request.get_json(force=True, silent=True) or request.form
    if (f.get("company") or "").strip():                              # honeypot: настоящий клиент это скрытое поле не видит -> заполнил бот
        return redirect("/", code=302)                               # тихо уводим, заказ не создаём
    site = (f.get("site") or "").strip()[:200]
    email = (f.get("email") or "").strip()[:200]
    niche = (f.get("niche") or "").strip()[:200]
    city = (f.get("city") or "").strip()[:100]
    promo = (f.get("promo") or "").strip()[:40]
    if not site or "." not in site or len(site) < 4:
        return err_page("Укажите корректный адрес сайта.", 400)
    if not _valid_email(email):                                      # чек по 54-ФЗ уходит на эту почту -> она должна быть валидной
        return err_page("Укажите корректный e-mail — на него придёт чек.", 400)
    if not niche:
        return err_page("Коротко укажите, чем вы занимаетесь.", 400)
    brand = ((f.get("brand") or "").strip() or engine._host(site))[:200]
    short = ((f.get("brand_short") or "").strip() or re.sub(r"[«»\"']", "", brand).split(",")[0])[:200]
    # промокод 100% скидки: заказ становится бесплатным (для тестов). Защита от утечки — суточный лимит.
    free = bool(promo) and PROMO_FREE_CODE and promo.lower() == PROMO_FREE_CODE
    if free:
        since = time.time() - 86400
        with db() as c:
            used = c.execute("SELECT COUNT(*) FROM orders WHERE promo IS NOT NULL AND promo<>'' AND created>=?",
                             (since,)).fetchone()[0]
        if used >= PROMO_FREE_DAILY_MAX:
            free = False                                            # суточный лимит бесплатных исчерпан -> обычная оплата
    order_id = uuid.uuid4().hex[:16]
    with db() as c:
        c.execute("INSERT INTO orders(id,created,status,brand,brand_short,site,niche,city,email,amount,promo) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (order_id, time.time(), "new", brand, short, site, niche, city, email,
                   (0 if free else None), (PROMO_FREE_CODE if free else None)))
    bot = tg_bot()
    if not bot:
        return err_page("Сервис временно недоступен, напишите нам в Telegram.", 503)
    # Ведём клиента в бот: там он жмёт Старт. Платный заказ -> кнопка оплаты; бесплатный по промо -> сразу к отчёту.
    link = f"https://t.me/{bot}?start={order_id}"
    if request.is_json:
        return jsonify(redirect=link, orderId=order_id)
    return redirect(link, code=302)

@app.post("/tbank/notify")
def tbank_notify():
    data = request.get_json(force=True, silent=True) or {}
    password = os.environ.get("TBANK_PASSWORD", "").strip()
    got = data.get("Token", "")
    ok = tbank_token(data, password) == got
    print(f"[notify] order={data.get('OrderId')} status={data.get('Status')} success={data.get('Success')} token_ok={ok}", flush=True)
    if not ok:
        abort(403)                              # подпись не сошлась
    if data.get("Status") in ("CONFIRMED", "AUTHORIZED") and data.get("Success"):
        _on_payment_confirmed(data.get("OrderId"), src="webhook")
    return "OK"

def _on_payment_confirmed(oid, src="webhook"):
    """Единая реакция на подтверждённую оплату — из вебхука ИЛИ из опроса статуса. Идемпотентна:
    срабатывает один раз (только пока заказ ещё new/pending), поэтому два источника не дублируют сообщение."""
    if not oid:
        return False
    with db() as c:
        o = c.execute("SELECT status, tg_chat_id, kind, qn FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] not in ("new", "pending"):
        return False                                         # уже обработан другим источником или клиентом
    is_addon = (o["kind"] or "main") == "addon"
    with db() as c:                                          # АТОМАРНО «забираем» заказ: вебхук и опрос не сработают дважды
        if is_addon:
            claimed = c.execute("UPDATE orders SET status='await_queries', awaiting='addon_queries' "
                                "WHERE id=? AND status IN ('new','pending')", (oid,)).rowcount
        else:
            claimed = c.execute("UPDATE orders SET status='paid' WHERE id=? AND status IN ('new','pending')", (oid,)).rowcount
    if not claimed:
        return False
    print(f"[paid:{src}] order={oid} оплата подтверждена -> {'await_queries' if is_addon else 'paid'}", flush=True)
    if is_addon:                                             # дозакупка: после оплаты просим запросы, а не запускаем сразу
        if o["tg_chat_id"]:
            n = o["qn"] or 1
            tg_send_message(o["tg_chat_id"],
                f"Оплата получена! Пришлите {n} {_plural_q(n)} для проверки — каждый с новой строки. "
                "Я покажу их на подтверждение, а потом запущу проверку и пришлю отчёт.")
    else:
        if o["tg_chat_id"]:                                   # есть чат клиента -> сразу показываем запросы на подтверждение
            maybe_start_review(oid)
    return True

def tbank_check_order(oid):
    """Спрашиваем у TBank статус по заказу (CheckOrder, по OrderId). True, если хоть один платёж подтверждён."""
    password = os.environ.get("TBANK_PASSWORD", "").strip()
    if not password:
        return False
    payload = {"TerminalKey": TERMINAL, "OrderId": str(oid)}
    payload["Token"] = tbank_token(payload, password)
    try:
        req = urllib.request.Request("https://securepay.tinkoff.ru/v2/CheckOrder",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print("[checkorder]", oid, e, flush=True)
        return False
    if not d.get("Success"):
        return False
    return any((p.get("Status") in ("CONFIRMED", "AUTHORIZED")) for p in (d.get("Payments") or []))

def _payment_poller():
    """Подстраховка на случай, если вебхук от TBank не дошёл: бот сам опрашивает статус «висящих» оплаченных
    заказов и автоматически отвечает клиенту, даже если он не нажал «вернуться в Telegram» на сайте."""
    gap = max(8, int(os.environ.get("PAYMENT_POLL_SEC", "25") or 25))
    while True:
        time.sleep(gap)
        try:
            with db() as c:                                  # ждём оплату: есть чат клиента, заказ свежий (до 2 часов)
                rows = c.execute("SELECT id FROM orders WHERE status IN ('new','pending') "
                                 "AND tg_chat_id IS NOT NULL AND tg_chat_id<>'' AND created > ?",
                                 (time.time() - 7200,)).fetchall()
            for r in rows:
                if tbank_check_order(r["id"]):
                    _on_payment_confirmed(r["id"], src="poll")
        except Exception as e:
            print("[poller]", e, flush=True)

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
        if l.startswith("/"): continue                                   # команды (/selftest и пр.) — не запросы
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
        st = o["status"] if o else None
        if o and st in ("reviewing", "error") and o["prep"]:
            start_generation(oid)                 # error -> перезапуск по сохранённым запросам
        elif o and st == "processing":
            tg_send_message(chat, "Уже запускаю проверку, отчёт будет здесь через несколько минут.")
        elif o and st == "done":
            if o["pdf"] and os.path.exists(o["pdf"]):
                with db() as c:
                    full = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                try: tg_send_report(chat, o["pdf"], full)
                except Exception as e: print("[tg] ошибка:", e)
            else:
                tg_send_message(chat, "Этот отчёт уже готов.")
        # ── промежуточные состояния не должны вести в тупик ──
        elif o and st in ("paid", "preparing"):   # оплачено, запросы ещё готовятся -> мягко подождать и до-кикнуть подготовку
            tg_send_message(chat, "Готовлю запросы для проверки — пришлю их сюда в течение минуты.")
            try: maybe_start_review(oid)
            except Exception as e: print("[go] maybe_start_review:", e)
        elif o and st == "reviewing" and not o["prep"]:
            tg_send_message(chat, "Секунду, ещё готовлю запросы — пришлю их сюда.")
            try: maybe_start_review(oid)
            except Exception: pass
        elif o and st == "await_niche":
            _ask_niche(chat, (o["site"] if "site" in o.keys() else "") or "")
        elif o and st == "await_queries":
            tg_send_message(chat, "Пришлите ваши запросы для проверки — каждый с новой строки.")
        elif o and st in ("new", "pending"):
            tg_send_message(chat, "Сначала нужно оплатить — кнопка оплаты выше. Если её не видно, нажмите /start.")
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
    if TG_WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_WEBHOOK_SECRET:
        abort(403)                                                   # секрет задан, но не совпал -> чужой запрос
    if not rate_ok(f"tw:{client_ip()}", 120, 60):
        return "OK"                                                  # перебор частоты -> молча игнорируем, Телеграму не ошибаемся
    upd = request.get_json(force=True, silent=True) or {}
    if upd.get("callback_query"):
        return _handle_callback(upd["callback_query"])
    msg = upd.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat:
        return "OK"
    frm = msg.get("from") or {}; chobj = msg.get("chat") or {}        # профиль клиента из телеграма (для CRM)
    tg_uname = (frm.get("username") or chobj.get("username") or "").strip().lstrip("@")[:64]
    tg_nm = " ".join(x for x in [frm.get("first_name") or chobj.get("first_name"),
                                 frm.get("last_name") or chobj.get("last_name")] if x).strip()[:120]
    if tg_uname or tg_nm:                                             # дописываем профиль во все заказы этого чата, где он пуст
        try:
            with db() as c:
                c.execute("UPDATE orders SET tg_username=COALESCE(NULLIF(?,''),tg_username), "
                          "tg_name=COALESCE(NULLIF(?,''),tg_name) WHERE tg_chat_id=?", (tg_uname, tg_nm, str(chat)))
        except Exception: pass
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        oid = parts[1].strip() if len(parts) > 1 else ""
        if oid:
            with db() as c:
                o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if o:
                with db() as c:
                    c.execute("UPDATE orders SET tg_chat_id=?, tg_username=COALESCE(NULLIF(?,''),tg_username), "
                              "tg_name=COALESCE(NULLIF(?,''),tg_name) WHERE id=?", (str(chat), tg_uname, tg_nm, oid))
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
                elif st == "await_niche":     # ждём нишу: сайт не разобрался автоматически
                    _ask_niche(chat, o["site"])
                elif st == "processing":
                    tg_send_message(chat, "Отчёт уже формируется, пришлю его сюда через пару минут.")
                elif st == "error":     # прошлая проверка упала -> повтор по сохранённым запросам + кнопка в поддержку
                    if o["prep"]:
                        tg_send_buttons(chat, "Прошлая проверка не завершилась. Запросы сохранены — нажмите «Попробовать ещё раз». "
                                              "Если не выйдет, отправьте проблему в поддержку.", _error_kb(oid))
                    else:
                        tg_send_buttons(chat, "Прошлая проверка не завершилась. Отправьте проблему в поддержку — мы увидим и поможем вручную.",
                                        [[{"text": "✉️ Сообщить о проблеме в поддержку", "callback_data": f"problem:{oid}"}]])
                elif st == "new" and (o["promo"] or ""):   # бесплатный заказ по промокоду -> без оплаты сразу к отчёту
                    tg_send_message(chat, "Промокод принят, проверка бесплатна. Готовлю запросы — пришлю их сюда на подтверждение.")
                    _on_payment_confirmed(oid, src="promo")
                elif st == "new":       # создаём платёж и шлём кнопку оплаты прямо в чат
                    try:
                        res = tbank_init(oid, o["email"])
                        if res.get("Success"):
                            with db() as c:
                                c.execute("UPDATE orders SET status='pending', payment_id=? WHERE id=?", (res.get("PaymentId"), oid))
                            tg_send_payment_button(chat, res["PaymentURL"], o["brand"])
                        else:
                            admin_notify("⚠️ Init вернул отказ\nЗаказ: " + str(oid) +
                                         f"\nErrorCode: {res.get('ErrorCode')} · {res.get('Message')} · {res.get('Details')}")
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
        if text.startswith("/"):                 # /selftest, /health и т.п., набранные в чат — это команды, НЕ запросы.
            # Никогда не трактуем их как нишу/запросы/правку — иначе случайная команда портит заказ.
            tg_send_message(chat, "Это команда, а не запрос — на заказ она не влияет. "
                                  "Чтобы открыть меню, отправьте /start. Чтобы изменить запросы — нажмите «Изменить запросы» под списком.")
            return "OK"
        with db() as c:                          # ждём нишу от клиента (сайт не разобрался автоматически)
            nq = c.execute("SELECT id FROM orders WHERE tg_chat_id=? AND status='await_niche' ORDER BY created DESC LIMIT 1",
                           (str(chat),)).fetchone()
        if nq:
            _set_niche(nq["id"], text)
            return "OK"
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
    # готовность к боевому запуску — видно одним взглядом
    readiness = {
        "engines_with_keys": sum(1 for v in keys.values() if v),     # сколько из 7 нейросетей подключено
        "live_terminal": "DEMO" not in (TERMINAL or "").upper(),     # False = ещё тестовый терминал
        "receipt_on": _receipt_on(),                                  # чек 54-ФЗ включён
        "taxation": os.environ.get("TBANK_TAXATION", "usn_income"),
        "tbank_password_set": bool(os.environ.get("TBANK_PASSWORD")),
        "admin_token_set": bool(os.environ.get("ADMIN_TOKEN")),
        "admin_chat_set": bool(os.environ.get("ADMIN_CHAT_ID")),
        "telegram_set": bool(tg_token()),
        "base_url_https": (BASE_URL or "").startswith("https://"),
        "test_mode_off": os.environ.get("TEST_MODE") != "1",
    }
    return jsonify(ok=True, version=VERSION, terminal=TERMINAL, price=PRICE, base_url=BASE_URL,
                   notify_url=f"{BASE_URL}/tbank/notify", test_mode=os.environ.get("TEST_MODE") == "1",
                   telegram=bool(tg_token()), bot=tg_bot(), admin=bool(os.environ.get("ADMIN_CHAT_ID")),
                   readiness=readiness, orders=orders, last_order=last_order, keys=keys)

@app.get("/selftest")
def selftest():
    """Диагностика: 1 запрос на каждую ПОДКЛЮЧЁННУЮ нейросеть, показывает ответ или ошибку.
    Дёргает платные API, поэтому ВСЕГДА под токеном: ?token=<SELFTEST_TOKEN или ADMIN_TOKEN>."""
    tok = (os.environ.get("SELFTEST_TOKEN") or os.environ.get("ADMIN_TOKEN") or "").strip()
    if not tok or request.args.get("token") != tok:                  # нет токена в env или не совпал -> закрыто
        abort(403)
    if not rate_ok(f"st:{client_ip()}", 6, 60):
        abort(429)
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

# ───────────────────────── мини-CRM (заказы и клиенты) ───────────────
_ST_RU = {"new":"Новая заявка","pending":"Ожидает оплаты","paid":"Оплачено","preparing":"В работе",
          "reviewing":"Согласование","await_niche":"Уточнение ниши","await_queries":"Ожидание запросов",
          "processing":"Генерация отчёта","done":"Выиграно","error":"Ошибка"}
_ST_COLOR = {"done":"#2E8B57","error":"#C13525","processing":"#C9791A","reviewing":"#C9791A",
             "await_niche":"#C9791A","await_queries":"#C9791A","paid":"#2D5AA0","preparing":"#2D5AA0",
             "pending":"#8a8a8a","new":"#8a8a8a"}
_PAID_STATES = {"paid","preparing","reviewing","await_niche","await_queries","processing","done","error"}

def _admin_ok():
    if session.get("admin"):                                         # вошёл по паролю (кука)
        return True
    token = os.environ.get("ADMIN_TOKEN")                            # либо прямой доступ по токену в ссылке (для CSV/закладок)
    return bool(token) and request.args.get("key") == token

def _login_html(err=""):
    err_html = f'<div class=err>{html.escape(err)}</div>' if err else ""
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<title>Вход в CRM</title>
<style>
@font-face{{font-family:"Gilroy";src:url("/fonts/Gilroy-Regular.ttf") format("truetype");font-weight:400;font-display:swap}}
@font-face{{font-family:"Gilroy";src:url("/fonts/Gilroy-Medium.ttf") format("truetype");font-weight:500;font-display:swap}}
@font-face{{font-family:"Gilroy";src:url("/fonts/Gilroy-Semibold.ttf") format("truetype");font-weight:600;font-display:swap}}
@font-face{{font-family:"Gilroy";src:url("/fonts/Gilroy-Bold.ttf") format("truetype");font-weight:700;font-display:swap}}
*{{box-sizing:border-box}}
body{{font-family:"Gilroy",-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;min-height:100vh;display:flex;
  align-items:center;justify-content:center;background:#fff;color:#111;padding:20px}}
.card{{width:100%;max-width:340px;border:1px solid #e6e6e6;border-radius:16px;padding:30px 26px}}
h1{{font-size:19px;margin:0 0 4px;letter-spacing:-.01em}}
.sub{{color:#8a8a8a;font-size:13px;margin:0 0 20px}}
label{{display:block;font-size:12px;color:#8a8a8a;margin:0 0 6px;text-transform:uppercase;letter-spacing:.05em}}
input{{width:100%;border:1px solid #d9d9d9;border-radius:10px;padding:13px 14px;font:inherit;font-size:16px;color:#111;outline:0}}
input:focus{{border-color:#111}}
button{{width:100%;margin-top:14px;border:0;border-radius:10px;padding:14px;background:#111;color:#fff;
  font:inherit;font-size:15px;font-weight:600;cursor:pointer}}
button:hover{{background:#000}}
.err{{background:#f5f5f5;border:1px solid #e0e0e0;color:#111;border-radius:9px;padding:9px 12px;font-size:13px;margin-bottom:14px}}
</style></head><body>
<form class=card method=post action="/admin/login">
  <h1>CRM · вход</h1>
  <div class=sub>Заявки и клиенты сервиса AI-видимости</div>
  {err_html}
  <label for=pw>Пароль</label>
  <input id=pw type=password name=password autocomplete=current-password autofocus required>
  <button type=submit>Войти</button>
</form>
</body></html>"""

@app.get("/admin/login")
def admin_login_form():
    if session.get("admin"):
        return redirect("/admin")
    return _login_html()

@app.post("/admin/login")
def admin_login():
    if not rate_ok(f"login:{client_ip()}", 8, 300):                  # не больше 8 попыток за 5 минут с одного IP
        return _login_html("Слишком много попыток. Подождите пару минут."), 429
    token = (os.environ.get("ADMIN_TOKEN") or "").strip()
    pw = (request.form.get("password") or "").strip()
    if token and pw == token:
        session["admin"] = True; session.permanent = True
        return redirect("/admin")
    if not token:
        return _login_html("На сервере не задан ADMIN_TOKEN — впишите его в переменные окружения."), 401
    return _login_html("Неверный пароль."), 401

@app.get("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")

def _order_amount(r):
    try:
        a = r["amount"]
        if a is not None: return a                       # явная сумма (в т.ч. 0 для бесплатного промо) уважается
        return PRICE if (r["kind"] or "main") == "main" else 0
    except Exception: return PRICE

@app.get("/tbank/selftest")
def tbank_selftest():
    """Проверка формирования чека (54-ФЗ): делает Init на текущем терминале с объектом Receipt
    и показывает ответ ТБанка. Пароль читается из TBANK_PASSWORD (env) — в коде и ответе не светится.
    Доступ: /tbank/selftest?key=ADMIN_TOKEN  (можно &email=...)."""
    if not _admin_ok(): abort(403)
    oid = "rcpttest" + uuid.uuid4().hex[:8]
    email = request.args.get("email", os.environ.get("TBANK_RECEIPT_EMAIL", "test@example.com"))
    sent = tbank_receipt(email, PRICE, None)
    raw_pw = os.environ.get("TBANK_PASSWORD", "")
    # безопасная диагностика пары терминал/пароль (сам пароль НЕ показываем):
    diag = {"terminal": TERMINAL, "password_set": bool(raw_pw),
            "password_len": len(raw_pw.strip()),
            "password_had_spaces": raw_pw != raw_pw.strip()}
    try:
        res = tbank_init(oid, email, with_receipt=True)   # принудительно с чеком — это и тестируем
    except Exception as e:
        return jsonify(ok=False, error=str(e), diag=diag, sent_receipt=sent), 200
    return jsonify(ok=bool(res.get("Success")), order_id=oid, diag=diag,
                   receipt_enabled_in_payments=_receipt_on(),
                   error_code=res.get("ErrorCode"), message=res.get("Message"), details=res.get("Details"),
                   payment_url=res.get("PaymentURL"), sent_receipt=sent), 200

@app.get("/tbank/refund")
def tbank_refund():
    """Возврат с чеком (тест-кейс №8 «чек возврата»). Делает Cancel по оплаченному заказу с объектом Receipt.
    Заказ должен быть оплачен (есть payment_id). Доступ: /tbank/refund?key=ADMIN_TOKEN&order=<id>[&amount=<руб>]."""
    if not _admin_ok(): abort(403)
    oid = (request.args.get("order") or "").strip()
    with db() as c:
        if oid:
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        else:   # без параметра order — берём последний оплаченный заказ (удобно для теста №8)
            o = c.execute("SELECT * FROM orders WHERE payment_id IS NOT NULL AND payment_id<>'' "
                          "ORDER BY created DESC LIMIT 1").fetchone()
    if not o:
        return jsonify(ok=False, error="нет заказа с оплатой (payment_id). Сначала проведи тестовую оплату."), 200
    pid = (o["payment_id"] if "payment_id" in o.keys() else None)
    if not pid:
        return jsonify(ok=False, error="у заказа нет payment_id — по нему не было оплаты"), 200
    rub = int(request.args.get("amount") or _order_amount(o))
    password = os.environ["TBANK_PASSWORD"].strip()
    payload = {"TerminalKey": TERMINAL, "PaymentId": str(pid), "Amount": rub * 100}
    payload["Receipt"] = tbank_receipt(o["email"], rub, "Возврат: отчёт о видимости бренда в нейросетях")
    payload["Token"] = tbank_token(payload, password)
    try:
        req = urllib.request.Request(TBANK_CANCEL, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 200
    return jsonify(ok=bool(res.get("Success")), order_id=oid, payment_id=str(pid),
                   error_code=res.get("ErrorCode"), message=res.get("Message"),
                   status=res.get("Status"), details=res.get("Details")), 200

def _admin_range():
    """Диапазон дат для CRM. Возвращает (lo_ts, hi_ts, frm, to, rng). Пусто -> всё время."""
    rng = (request.args.get("range") or "").strip()
    frm = (request.args.get("from") or "").strip()
    to  = (request.args.get("to") or "").strip()
    now = time.time()
    if rng in ("today", "7", "30"):
        lo = (time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) if rng == "today"
              else now - int(rng) * 86400)
        return lo, now + 1, "", "", rng
    lo, hi = 0.0, now + 1
    try:
        if frm: lo = time.mktime(time.strptime(frm, "%Y-%m-%d"))
    except Exception: frm = ""
    try:
        if to: hi = time.mktime(time.strptime(to, "%Y-%m-%d")) + 86400          # день «to» включительно
    except Exception: to = ""
    return lo, hi, frm, to, ("custom" if (frm or to) else "all")

def _order_kind_ru(r):
    if r["promo"] or "": return "промо"
    return "доп" if (r["kind"] or "main") == "addon" else "основной"

def _is_revenue(r):
    return r["status"] in _PAID_STATES and not (r["promo"] or "")               # промо/тест в выручку не идёт

def _tg_profile(r):
    """Текстовое представление телеграм-профиля клиента (для CSV)."""
    u = (r["tg_username"] or "").strip().lstrip("@")
    nm = (r["tg_name"] or "").strip()
    cid = (r["tg_chat_id"] or "").strip()
    parts = [nm] if nm else []
    if u: parts.append("@" + u)
    elif cid: parts.append("id" + cid)
    return " · ".join(parts)

def _tg_cell(r):
    """Кликабельный профиль клиента в Telegram для CRM: имя + @username (ссылка на чат)."""
    u = (r["tg_username"] or "").strip().lstrip("@")
    nm = html.escape((r["tg_name"] or "").strip())
    cid = (r["tg_chat_id"] or "").strip()
    if u:
        label = f"{nm} · @{html.escape(u)}" if nm else "@" + html.escape(u)
        return f'<a class=tg href="https://t.me/{html.escape(u, quote=True)}" target=_blank rel=noopener>{label}</a>'
    if cid:                                                                      # без username — открываем профиль по id (Telegram Desktop)
        return f'<a class=tg href="tg://user?id={html.escape(cid, quote=True)}">{nm or "Профиль"}</a>'
    return '<span class=mut>—</span>'

@app.get("/admin")
def admin():
    if not _admin_ok():
        return redirect("/admin/login")
    flt = (request.args.get("status") or "").strip()
    lo, hi, frm, to, rng = _admin_range()
    with db() as c:
        rows = c.execute("SELECT * FROM orders WHERE created>=? AND created<? ORDER BY created DESC LIMIT 3000",
                         (lo, hi)).fetchall()
    key = html.escape(request.args.get("key") or "")
    def _url(**over):                                                            # ссылка с сохранением текущих параметров
        p = {"key": request.args.get("key") or ""}
        for k in ("range", "from", "to", "status"):
            v = request.args.get(k)
            if v: p[k] = v
        p.update(over)
        p = {k: v for k, v in p.items() if v not in ("", None)}
        return "?" + "&".join(f"{k}={html.escape(str(v), quote=True)}" for k, v in p.items())
    paid = [r for r in rows if _is_revenue(r)]
    revenue = sum(_order_amount(r) for r in paid)
    rated = [r["rating"] for r in rows if r["rating"] is not None]
    avg = round(sum(rated) / len(rated), 1) if rated else "—"
    cards = [("Заявок", len(rows)), ("Оплачено", len(paid)),
             ("Выручка, ₽", f"{revenue:,}".replace(",", " ")),
             ("Выиграно", sum(1 for r in rows if r["status"] == "done")),
             ("Ошибок", sum(1 for r in rows if r["status"] == "error")),
             ("Промо/тест", sum(1 for r in rows if (r["promo"] or ""))),
             ("Ср. оценка", avg)]
    cardhtml = "".join(f'<div class=card><div class=cv>{v}</div><div class=cl>{l}</div></div>' for l, v in cards)
    show = [r for r in rows if (not flt or r["status"] == flt)]
    def _stcls(st):
        if st == "done": return "s-done"
        if st == "error": return "s-err"
        if st in ("new", "pending"): return "s-new"
        return "s-mid"
    trs = ""
    for r in show:
        dt = time.strftime("%d.%m.%Y %H:%M", time.localtime(r["created"] or 0))
        st = r["status"] or ""
        raw_site = (r["site"] or "").strip()
        site_disp = html.escape(raw_site[:60])
        site_href = html.escape("https://" + re.sub(r"^https?://", "", raw_site), quote=True)
        site_cell = f'<a href="{site_href}" target=_blank rel=noopener>{site_disp}</a>' if raw_site else ""
        amt = _order_amount(r) if r["status"] in _PAID_STATES else None
        rating = f'{r["rating"]}/5' if r["rating"] is not None else ""
        fb = html.escape((r["feedback"] or "")[:120])
        trs += (f'<tr><td class=dt>{dt}</td><td class=b>{html.escape(r["brand"] or "")}</td>'
                f'<td>{_tg_cell(r)}</td>'
                f'<td>{site_cell}</td><td>{html.escape((r["niche"] or "")[:44])}</td>'
                f'<td>{_order_kind_ru(r)}</td>'
                f'<td><span class="st {_stcls(st)}">{_ST_RU.get(st, st)}</span></td>'
                f'<td class=num>{"" if amt is None else amt}</td>'
                f'<td class=em>{html.escape(_mask_email(r["email"]))}</td>'
                f'<td class=num>{rating}</td><td class=fb>{fb}</td></tr>')
    ranges = [("today", "Сегодня"), ("7", "7 дней"), ("30", "30 дней"), ("", "Всё время")]
    rbar = "".join(f'<a class="q{" on" if (rng==rk or (rk=="" and rng=="all")) else ""}" '
                   f'href="{_url(range=rk, **{"from":"", "to":""})}">{lbl}</a>' for rk, lbl in ranges)
    statuses = [("", "все")] + [(s, _ST_RU[s]) for s in ("done", "processing", "reviewing", "pending", "error")]
    sbar = "".join(f'<a class="q{" on" if flt==sk else ""}" href="{_url(status=sk)}">{lbl}</a>' for sk, lbl in statuses)
    st_hidden = f'<input type=hidden name=status value="{html.escape(flt, quote=True)}">' if flt else ""
    period = (f"{frm or '…'} — {to or '…'}" if rng == "custom" else
              {"today": "сегодня", "7": "последние 7 дней", "30": "последние 30 дней"}.get(rng, "всё время"))
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<title>CRM · заявки</title>
<style>
@font-face{{font-family:"Gilroy";src:url("/fonts/Gilroy-Regular.ttf") format("truetype");font-weight:400;font-display:swap}}
@font-face{{font-family:"Gilroy";src:url("/fonts/Gilroy-Medium.ttf") format("truetype");font-weight:500;font-display:swap}}
@font-face{{font-family:"Gilroy";src:url("/fonts/Gilroy-Semibold.ttf") format("truetype");font-weight:600;font-display:swap}}
@font-face{{font-family:"Gilroy";src:url("/fonts/Gilroy-Bold.ttf") format("truetype");font-weight:700;font-display:swap}}
:root{{--ink:#111;--soft:#444;--mut:#8a8a8a;--line:#e6e6e6;--line2:#efefef;--bg:#fff;--hov:#f6f6f6}}
*{{box-sizing:border-box}}
body{{font-family:"Gilroy",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink);padding:22px clamp(14px,3vw,30px);font-size:13px;-webkit-font-smoothing:antialiased}}
a{{color:var(--ink)}}
.top{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:4px}}
h1{{font-size:18px;font-weight:700;margin:0;letter-spacing:-.01em}}
.sub{{color:var(--mut);font-size:12px;margin:2px 0 16px}}
.acts{{display:flex;gap:8px;flex-shrink:0}}
.csv{{font-size:12px;font-weight:600;border:1px solid var(--ink);border-radius:7px;padding:7px 12px;text-decoration:none;white-space:nowrap}}
.csv:hover{{background:var(--ink);color:#fff}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:9px;margin-bottom:18px}}
.card{{border:1px solid var(--line);border-radius:11px;padding:13px 15px;background:#fff}}
.cv{{font-size:22px;font-weight:750;letter-spacing:-.02em;line-height:1}}
.cl{{font-size:11px;color:var(--mut);margin-top:6px;text-transform:uppercase;letter-spacing:.05em}}
.filters{{display:flex;flex-wrap:wrap;align-items:center;gap:7px 8px;margin-bottom:14px}}
.lbl{{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin-right:2px}}
.q{{display:inline-block;padding:5px 11px;border:1px solid var(--line);border-radius:20px;color:var(--soft);text-decoration:none;font-size:12px;background:#fff}}
.q:hover{{border-color:#bbb}} .q.on{{background:var(--ink);color:#fff;border-color:var(--ink)}}
.dr{{display:inline-flex;align-items:center;gap:6px;margin-left:auto}}
.dr input[type=date]{{border:1px solid var(--line);border-radius:7px;padding:5px 8px;font:inherit;font-size:12px;color:var(--ink)}}
.dr button{{border:1px solid var(--ink);background:#fff;border-radius:7px;padding:6px 12px;font:inherit;font-size:12px;font-weight:600;cursor:pointer}}
.dr button:hover{{background:var(--ink);color:#fff}}
.sep{{flex-basis:100%;height:0}}
.wrap{{border:1px solid var(--line);border-radius:12px;overflow:hidden}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th,td{{text-align:left;padding:9px 11px;border-bottom:1px solid var(--line2);vertical-align:top}}
th{{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;background:#fafafa}}
tr:last-child td{{border-bottom:0}} tbody tr:hover td{{background:var(--hov)}}
.b{{font-weight:600}} .dt{{white-space:nowrap;color:var(--soft)}} .num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
.em{{color:var(--soft);white-space:nowrap}} .fb{{color:var(--soft);max-width:220px}}
.tg{{white-space:nowrap;text-decoration:underline;text-underline-offset:2px}} .tg:hover{{color:#000}} .mut{{color:var(--mut)}}
.st{{display:inline-block;border-radius:20px;padding:2px 9px;font-size:11px;white-space:nowrap;border:1px solid var(--line)}}
.s-done{{background:var(--ink);color:#fff;border-color:var(--ink)}}
.s-err{{background:#fff;color:var(--ink);border-color:var(--ink);font-weight:600}}
.s-mid{{background:#ececec;color:#333}} .s-new{{background:#f5f5f5;color:#8a8a8a}}
.empty{{padding:30px;text-align:center;color:var(--mut)}}
</style></head><body>
<div class=top><h1>CRM · заявки и клиенты</h1><div class=acts><a class=csv href="/admin/export.csv{_url()}">Скачать CSV</a><a class=csv href="/admin/logout">Выйти</a></div></div>
<div class=sub>Период: {period} · показано {len(show)} из {len(rows)}</div>
<div class=cards>{cardhtml}</div>
<div class=filters>
  <span class=lbl>Период</span>{rbar}
  <form class=dr method=get><input type=hidden name=key value="{key}">{st_hidden}
    <input type=date name=from value="{html.escape(frm, quote=True)}">
    <input type=date name=to value="{html.escape(to, quote=True)}"><button>Применить</button></form>
  <span class=sep></span>
  <span class=lbl>Статус</span>{sbar}
</div>
<div class=wrap><table><thead><tr><th>Дата</th><th>Бренд</th><th>Telegram</th><th>Сайт</th><th>Ниша</th><th>Тип</th><th>Статус</th><th>₽</th><th>Email</th><th>Оценка</th><th>Отзыв</th></tr></thead>
<tbody>{trs or '<tr><td colspan=11 class=empty>За выбранный период заявок нет</td></tr>'}</tbody></table></div>
</body></html>"""

@app.get("/admin/export.csv")
def admin_export():
    if not _admin_ok():
        return redirect("/admin/login")
    lo, hi, frm, to, rng = _admin_range()                                       # та же фильтрация по датам, что и в CRM
    flt = (request.args.get("status") or "").strip()
    with db() as c:
        rows = c.execute("SELECT * FROM orders WHERE created>=? AND created<? ORDER BY created DESC",
                         (lo, hi)).fetchall()
    if flt:
        rows = [r for r in rows if r["status"] == flt]
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["id","дата","бренд","сайт","ниша","город","тип","статус","сумма","email",
                "телеграм","telegram_id","оценка","отзыв","payment_id"])
    for r in rows:
        dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"] or 0))
        w.writerow([r["id"], dt, r["brand"] or "", r["site"] or "", r["niche"] or "", r["city"] or "",
                    _order_kind_ru(r), r["status"] or "",
                    (_order_amount(r) if r["status"] in _PAID_STATES else 0), _mask_email(r["email"]),
                    _tg_profile(r), r["tg_chat_id"] or "", (r["rating"] if r["rating"] is not None else ""),
                    (r["feedback"] or "").replace("\n"," "), r["payment_id"] or ""])
    return app.response_class(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=orders.csv"})

# Фоновый опрос статуса оплаты (подстраховка к вебхуку) — запускается при импорте модуля,
# поэтому работает и под gunicorn, а не только при прямом запуске. Идемпотентность обеспечена
# атомарным «забором» заказа в _on_payment_confirmed, так что несколько воркеров не дублируют сообщение.
if os.environ.get("TEST_MODE") != "1" and os.environ.get("DISABLE_PAYMENT_POLLER") != "1":
    threading.Thread(target=_payment_poller, daemon=True).start()
    print("[poller] фоновый опрос статуса оплаты запущен", flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
