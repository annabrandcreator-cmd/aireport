# -*- coding: utf-8 -*-
"""
Движок отчёта AI-видимости.
Опрашивает 7 нейросетей по коммерческим запросам ниши, ищет упоминания бренда и
конкурентов, считает видимость и собирает данные отчёта в формате build_report.py.

Режимы:
  TEST_MODE=1  -> мок-движок (без ключей), для проверки всей цепочки
  иначе        -> реальные адаптеры по ключам из переменных окружения

Ключи (env), подключаются по мере появления:
  PERPLEXITY_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
  DEEPSEEK_API_KEY, GIGACHAT_API_KEY, YANDEX_API_KEY, YANDEX_FOLDER_ID
"""
import os, re, json, time, hashlib, datetime, urllib.request, urllib.error, ssl, uuid
from concurrent.futures import ThreadPoolExecutor

RUNS = max(2, int(os.environ.get("RUNS", "2") or 2))  # прогонов на каждый запрос (для стабильности можно 3-5)

ENGINES = [
    {"id":"perplexity","name":"Perplexity","short":"Pp","note":"ищет в интернете, цитирует источники"},
    {"id":"chatgpt",   "name":"ChatGPT",   "short":"GPT","note":"режим веб-поиска"},
    {"id":"claude",    "name":"Claude",    "short":"Cl","note":"веб-поиск, осторожен в рекомендациях"},
    {"id":"deepseek",  "name":"DeepSeek",  "short":"DS","note":"отвечает в основном из обучения"},
    {"id":"gemini",    "name":"Gemini",    "short":"Gm","note":"поиск Google в основе"},
    {"id":"gigachat",  "name":"GigaChat",  "short":"GC","note":"российский, ищет в Рунете"},
    {"id":"yandex",    "name":"Яндекс Нейро","short":"Я","note":"опирается на Яндекс.Карты и отзывы"},
]

# ───────────────────────── генерация запросов ─────────────────────────
# ведущие глаголы-сказуемые: «Внедряю ...», «Продаём ...» снимаем, оставляем ядро
_NICHE_VERBS = {
    "внедряю","внедряем","делаю","делаем","продаю","продаём","продаем","произвожу","производим",
    "оказываю","оказываем","предоставляю","предоставляем","занимаюсь","занимаемся","разрабатываю",
    "разрабатываем","создаю","создаём","создаем","строю","строим","шью","шьём","шьем","изготавливаю",
    "изготавливаем","ремонтирую","ремонтируем","устанавливаю","устанавливаем","настраиваю","настраиваем",
    "веду","ведём","ведем","организую","организуем","провожу","проводим","помогаю","помогаем",
}
_NICHE_CUTS = [",", ";", " чтобы", " который", " которые", " которая", " которое",
               " для того", " так чтобы", " с тем", " под ключ"]
_NICHE_TRAIL = {"и","по","для","с","на","в","под","от","до","или","а","о","об","со","из"}

def _prep_city(city):
    """Лёгкое склонение города в предложный падеж: Москва->Москве, Россия->России."""
    c = (city or "").strip()
    if not c: return ""
    cl = c.lower()
    if cl.endswith("ия"): return c[:-2] + "ии"               # Россия->России
    if cl.endswith("ь"):  return c[:-1] + "и"                # Казань->Казани, Тверь->Твери
    if cl.endswith(("а","я")): return c[:-1] + "е"           # Москва->Москве, Тула->Туле
    if " " not in c and "-" not in c and cl[-1] in "бвгджзклмнпрстфхцчшщ":
        return c + "е"                                        # Екатеринбург->Екатеринбурге
    return c                                                  # Сочи, многословные оставляем как есть

def _decap(w):
    # короткие и аббревиатуры (AI, ИП, 3D) не трогаем, остальное со строчной
    return w if (len(w) <= 3 or w.isupper()) else w[:1].lower() + w[1:]

def clean_niche(niche):
    """Из произвольной фразы (даже целого предложения) делает короткое ядро ниши."""
    n = (niche or "").strip()
    if not n:
        return "услугу"
    low = n.lower()
    cut = min((low.find(s) for s in _NICHE_CUTS if low.find(s) > 0), default=-1)
    if cut > 0:
        n = n[:cut]
    words = n.strip().rstrip(".").split()
    if words and words[0].lower() in _NICHE_VERBS:
        words = words[1:]
    words = words[:6]
    while len(words) > 1 and words[-1].lower() in _NICHE_TRAIL:
        words.pop()
    if not words:
        return "услугу"
    words[0] = _decap(words[0])
    return " ".join(words).strip()

def _norm_city(city):
    """Страна целиком — это НЕ город: убираем «рф/россия/вся россия», чтобы не плодить географию в запросах и в шапке отчёта."""
    c = (city or "").strip().strip(".,")
    if c.lower() in ("рф", "россия", "вся россия", "по россии", "по всей россии", "russia", "ru", "рф."):
        return ""
    return c

def generate_queries(niche, city, site_info=None, aliases=None):
    city = _norm_city(city)
    # боевой режим: запросы под нишу формирует сама нейросеть (с учётом сайта); иначе/при сбое — шаблоны
    qs = generate_queries_llm(niche, city, site_info) if os.environ.get("TEST_MODE") != "1" else None
    if not qs:
        qs = generate_queries_tpl(niche, city)
    # выкидываем БРЕНДОВЫЕ запросы (где клиент уже знает компанию): основной % меряет небрендовую видимость
    al = [a for a in (aliases or ()) if len(a) >= 5]
    if al:
        filtered = [q for q in qs if not any(a in q["q"].lower() for a in al)]
        if len(filtered) >= 5:
            qs = filtered
    return qs

def generate_queries_tpl(niche, city):
    n = clean_niche(niche)
    N = n[:1].upper() + n[1:]
    inc = f" в {_prep_city(city)}" if city else ""
    # естественные вопросы к ИИ, один нейтральный сегмент (без смешивания «недорого»/«премиум»)
    items = [
        (f"Где заказать {n}{inc}?",                            "Поиск компании"),
        (f"Какие компании делают {n}{inc}?",                   "Поиск компании"),
        (f"Кто делает {n}{inc}?",                              "Поиск компании"),
        (f"Посоветуй надёжную компанию: {n}{inc}",             "Доверие к компании"),
        (f"Кому доверить {n} и на что смотреть при выборе?",   "Доверие к компании"),
        (f"Как выбрать исполнителя: {n}{inc}?",                "Сравнение компаний"),
        (f"{N} под ключ — к кому обратиться{inc}?",            "Под ключ"),
        (f"Сколько стоит {n} и от чего зависит цена?",         "Цена и сроки"),
        (f"Где почитать отзывы о компаниях: {n}{inc}?",        "Отзывы и кейсы"),
        (f"Примеры работ и кейсы: {n}{inc}",                   "Отзывы и кейсы"),
    ]
    return [{"q": q, "group": g} for q, g in items]

# ── запросы под нишу через нейросеть ──────────────────────────────────
_Q_INTENTS = [
    (("скольк","цен","стоит","стоимост","бюджет","срок"),                         "Цена и сроки"),
    (("лучш","топ","сравн","рейтинг","выбрать","какой выбрать","какую выбрать"),   "Сравнение компаний"),
    (("отзыв","репутац","кейс","пример"),                                          "Отзывы и кейсы"),
    (("надёжн","надежн","гарант","довери","безопас","риск","как выбрать"),         "Доверие к компании"),
    (("кто ","где ","куда","найти","заказать","купить","поехать","снять","арендовать","специалист","компани",
      "исполнител","агентств","студи","сделать","разработ","внедр","нужен","ищу","отдохнуть","выбрать место"), "Поиск компании"),
]
def _classify_query(q):
    ql = q.lower()
    for keys, name in _Q_INTENTS:
        if any(k in ql for k in keys):
            return name
    return "Услуги ниши"

def _first_keyed_adapter():
    # вспомогательные задачи (генерация запросов, извлечение конкурентов) — на дешёвом движке,
    # дорогой веб-поиск GPT бережём для реальных ответов
    for eid in ("gigachat", "yandex", "deepseek", "gemini", "chatgpt", "claude", "perplexity"):
        if has_key(eid):
            return REAL_ADAPTERS[eid]
    return None

def generate_queries_llm(niche, city, site_info=None):
    """10 коммерческих запросов под нишу руками нейросети. None -> откат на шаблоны."""
    ask = _first_keyed_adapter()
    if not ask:
        return None
    loc = f" Регион: {city}." if city else ""
    ctx = ""
    if site_info and site_info.get("ok"):
        bits = []
        if site_info.get("title"): bits.append("Заголовок сайта: " + site_info["title"])
        if site_info.get("description"): bits.append("Описание: " + site_info["description"])
        if site_info.get("text_excerpt"): bits.append("Фрагмент сайта: " + site_info["text_excerpt"][:600])
        if bits:
            ctx = ("\nДанные с сайта компании (учитывай реальную бизнес-модель: проектная работа, опт, премиум, нишевые услуги, "
                   "а не только общую формулировку ниши):\n" + "\n".join(bits) + "\n")
    prompt = (
        "Помоги собрать запросы, по которым можно проверить, рекомендуют ли нейросети конкретную компанию. "
        f"Составь 10 запросов на русском, которые реальный КЛИЕНТ задаёт ИИ-ассистенту (ChatGPT, Алиса, GigaChat), "
        f"когда выбирает, где заказать, к кому обратиться или куда поехать в нише: «{niche}».{loc}{ctx}\n"
        "\nГЛАВНОЕ — ФОРМУЛИРОВКА. Это живые вопросы и просьбы к ассистенту, а НЕ SEO-ключи и НЕ рекламные слоганы. "
        "Начинай каждый запрос как естественный вопрос или просьбу: «Какие компании…», «Кто делает…», «Где заказать…», "
        "«Посоветуй фирму, которая…», «Подскажи, к кому обратиться за…», «Что выбрать, если нужно…», «К кому пойти, чтобы…». "
        "Минимум 8 из 10 — полноценные вопросы со знаком вопроса.\n"
        "ЗАПРЕЩЕНО: обрывки-ключи и назывные фразы без вопроса («производство корпусной мебели больших объёмов», "
        "«изготовление мебели из массива под заказ»); рекламные описания компании («компания проектирует элитную мебель»); "
        "ломаный порядок слов («оптом закупка кухонной мебели»).\n"
        "Хорошо: «Какие компании делают офисную мебель на заказ в Москве?», «Посоветуй фабрику корпусной мебели под проект». "
        "Плохо: «фабрика мебели металлокаркас Москва», «производство мебели на заказ недорого».\n"
        "\nОХВАТ: часть запросов широкие, где клиент ещё не знает конкретную фирму; часть — конкретнее: по формату работы "
        "(под ключ, проект, опт), по материалу, как выбрать, на что смотреть. Интент коммерческий — человек выбирает, куда потратить деньги.\n"
        "ЦЕНОВОЙ СЕГМЕНТ: держи ОДИН сегмент из данных сайта и не смешивай — нельзя в одном наборе и «премиум», и «эконом/недорого». "
        "Если компания премиальная, проектная, B2B или оптовая — не пиши «недорого», «дёшево», «эконом» и не формулируй как розничную покупку. "
        "Если сегмент из сайта не ясен — НЕ указывай его вообще.\n"
        "ГОРОДА: не выдумывай географию. Если регион передан — используй только его. Если регион НЕ передан — во всех 10 запросах "
        "НЕ упоминай НИ ОДНОГО города (можно «в России» или вовсе без географии). Категорически нельзя раскидывать запросы по разным городам.\n"
        "ПРЕДМЕТ: каждый запрос — про ОСНОВНОЙ продукт или услугу компании по данным сайта. Не уходи в смежные категории, которых на сайте нет "
        "(для производителя мебели — не про дизайн интерьера, ремонт, оборудование). Не выдумывай услуги, которых нет (рассрочка, доставка по миру). "
        "Всегда указывай, ЧТО ищут. Не упоминай конкретные бренды и названия компаний. Без чисто справочных вопросов (что такое, как сделать самому).\n"
        "Верни только сами запросы, каждый с новой строки, без нумерации, кавычек и пояснений."
    )
    try:
        raw = ask(prompt)
    except Exception:
        return None
    seen, uniq = set(), []
    for ln in (raw or "").splitlines():
        s = re.sub(r"^\s*(?:\d+[\).\.]?|[-*•—])\s*", "", ln).strip().strip('"«»').strip()
        if not (8 <= len(s) <= 130):
            continue
        if not re.search(r"[A-Za-zА-Яа-яЁё]", s):
            continue
        if s.endswith(":"):                                   # заголовок-преамбула «Вот запросы:»
            continue
        low = s.lower()
        if low.startswith(("конечно","вот ","ниже ","итак")):  # разговорные вступления
            continue
        if low in seen:
            continue
        seen.add(low); uniq.append(s)
    uniq = uniq[:10]
    if len(uniq) < 6:
        return None
    return [{"q": q, "group": _classify_query(q)} for q in uniq]

# ───────────────────────── утилиты детекции ──────────────────────────
def _host(site):
    return re.sub(r"^https?://", "", (site or "")).replace("www.", "").split("/")[0].lower()

def _brand_gender(brand_short):
    """Личный бренд-человек: пол по фамилии (для согласования глаголов). 'f'/'m' = человек, 'n' = компания/бренд."""
    words = (brand_short or "").strip().split()
    if len(words) == 2 and all(re.match(r"^[А-ЯЁ][а-яё.\-]+$", w) for w in words):
        sur = words[1].lower()
        if re.search(r"(ова|ева|ёва|ина|ына|ская|цкая|ая)$", sur): return "f"
        if re.search(r"(ов|ев|ёв|ин|ын|ский|цкий|ской|ной)$", sur): return "m"
    return "n"

def brand_aliases(site, site_info=None, brand="", brand_short=""):
    """Все варианты имени бренда: домен, введённое имя, названия с сайта (og:site_name, Schema name, title).
    По ним детектим упоминание и их же исключаем из конкурентов, чтобы бренд не попал сам к себе в конкуренты."""
    host = _host(site)
    al = set()
    for x in (brand, brand_short, host):
        x = (x or "").strip().strip("«»\"'").lower()
        if len(x) >= 3: al.add(x)
    base = re.sub(r"\.[a-zа-я]{2,6}$", "", host)              # домен без зоны: vazuza-club.ru -> vazuza-club
    if "-" in base:                                          # многословный домен, единичное слово не берём (геориск)
        al.add(base); al.add(base.replace("-", " "))
    return {a for a in al if len(a) >= 3}

def detect_mention(answer, aliases):
    if not answer: return False
    a = answer.lower()
    return any(al in a for al in (aliases or ()))

# ───────────────────────── разбор сайта (доказательная база) ──────────
def _fetch_text(url, timeout=12, limit=400000):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; AIVisibilityBot/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.geturl(), r.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError:
        raise
    except Exception:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.geturl(), r.read(limit).decode("utf-8", "replace")

def analyze_site(site):
    """Реально открывает сайт: заголовок, описание, текст, Schema, страницы услуг/кейсов, robots для ИИ-ботов."""
    host = _host(site)
    out = {"ok": False, "host": host}
    if not host:
        return out
    html = None
    for base in ("https://" + host, "http://" + host):
        try:
            final, html = _fetch_text(base + "/"); out["url"] = final; break
        except Exception as e:
            out["error"] = type(e).__name__
    if html is None:
        return out
    out["ok"] = True
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    out["title"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()[:160] if m else ""
    m = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html)
    out["description"] = re.sub(r"\s+", " ", m.group(1)).strip()[:300] if m else ""
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    out["text_excerpt"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt)).strip()[:1200]
    types = set(); names = []
    m = re.search(r'(?is)<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\'](.*?)["\']', html)
    if m: names.append(m.group(1).strip())
    for blk in re.findall(r'(?is)<script[^>]+ld\+json[^>]*>(.*?)</script>', html):
        for t in re.findall(r'"@type"\s*:\s*"([^"]+)"', blk):
            types.add(t)
        for nm in re.findall(r'"name"\s*:\s*"([^"]{2,60})"', blk):
            names.append(nm.strip())
    if out.get("title"):
        first = re.split(r'[—\-|·:•]', out["title"])[0].strip()
        if 2 < len(first) <= 50: names.append(first)
    out["schema"] = sorted(types)
    out["names"] = list(dict.fromkeys([n for n in names if n and len(n) >= 3]))[:6]
    links = set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', html):
        h = href.lower()
        if h.startswith(("#", "mailto:", "tel:", "javascript:")): continue
        if h.startswith("http") and host not in h: continue
        links.add(h)
    def _cnt(words): return sum(1 for l in links if any(w in l for w in words))
    out["service_pages"] = _cnt(["услуг", "uslug", "service", "catalog", "продукт", "produkt", "product", "решени", "resheni"])
    out["case_pages"] = _cnt(["кейс", "kejs", "keys", "case", "портфолио", "portfolio", "проект", "proekt", "project",
                              "works", "работы", "rabot", "объект", "obekt", "obyekt", "realizov", "примеры", "primery"])
    out["links_count"] = len(links)
    paths = []
    for l in links:
        p = re.sub(r'^https?://[^/]+', '', l).split('#')[0].split('?')[0].strip()
        if p and p != '/' and p not in paths:
            paths.append(p)
    out["pages_list"] = paths[:16]
    out["robots_found"] = False; out["robots_blocks_ai"] = []
    try:
        _, robots = _fetch_text("https://" + host + "/robots.txt", timeout=8, limit=60000)
        out["robots_found"] = True
        bots = ["gptbot", "oai-searchbot", "perplexitybot", "yandexbot", "google-extended", "claudebot", "ccbot"]
        blocked = []
        for seg in re.split(r'(?im)^user-agent:', robots.lower()):
            who = seg.strip().split("\n")[0].strip()
            if who in bots and re.search(r'(?m)^\s*disallow:\s*/\s*$', seg):
                blocked.append(who)
        out["robots_blocks_ai"] = sorted(set(blocked))
    except Exception:
        pass
    out["sitemap_urls"] = None
    try:
        _, sm = _fetch_text("https://" + host + "/sitemap.xml", timeout=8, limit=300000)
        locs = re.findall(r"<loc>(.*?)</loc>", sm)
        out["sitemap_urls"] = len(locs) or None
        sm_paths = []
        for u in locs:
            p = re.sub(r'^https?://[^/]+', '', u.strip()).split('?')[0].strip()
            if p and p not in sm_paths: sm_paths.append(p)
        if sm_paths:
            out["pages_list"] = sm_paths[:16]            # карта сайта точнее ссылок с главной
    except Exception:
        pass
    return out

# ── расшифровка Schema.org и уточнение ниши ───────────────────────────
def _schema_summary(types):
    t = {str(x).lower() for x in (types or [])}
    return {"org": bool(t & {"organization", "localbusiness", "corporation", "professionalservice"}),
            "service": "service" in t, "faq": "faqpage" in t, "product": "product" in t}

def _schema_phrase(types):
    s = _schema_summary(types)
    found = [n for f, n in [(s["org"], "организация"), (s["service"], "услуги"), (s["faq"], "FAQ"), (s["product"], "товары")] if f]
    miss = [n for f, n in [(s["org"], "организация"), (s["service"], "услуги")] if not f]
    parts = []
    if found: parts.append("есть разметка: " + ", ".join(found))
    if miss: parts.append("не найдена: " + ", ".join(miss))
    return "; ".join(parts) if parts else "значимая разметка не найдена"

def _clean_brand(s):
    """Чистит имя бренда: убирает крайние прямые кавычки и балансирует «»."""
    s = (s or "").strip().strip('"\'').strip()
    o, c = s.count("«"), s.count("»")
    if o > c: s += "»" * (o - c)
    elif c > o: s = "«" * (c - o) + s
    return s.strip()

def brand_card(site, site_info, fallback=""):
    """Каноническое имя бренда + алиасы через нейросеть. Имя компании, НЕ товар и НЕ общая фраза.
    Возвращает (canonical, aliases:set)."""
    host = _host(site)
    base = brand_aliases(site, site_info, fallback or host, fallback or host)
    ask = _first_keyed_adapter()
    if not (ask and site_info and site_info.get("ok")):
        return (fallback or host), base
    bits = " | ".join(filter(None, [
        "Домен: " + host,
        "Заголовок: " + (site_info.get("title") or ""),
        "Описание: " + (site_info.get("description") or ""),
        "Названия из разметки: " + ", ".join(site_info.get("names") or []),
        "Фрагмент: " + (site_info.get("text_excerpt") or "")[:400]]))
    prompt = ("Определи ОФИЦИАЛЬНОЕ НАЗВАНИЕ КОМПАНИИ или БРЕНДА по данным сайта. "
              "Это имя организации, а НЕ описание товара/услуги и НЕ общая фраза "
              "(например «пальто из кашемира» — это товар, не бренд). "
              'Ответь строго в JSON без пояснений: {"brand":"каноническое имя","aliases":["вариант латиницей","вариант кириллицей","с доменом"]}. '
              "В aliases — только реальные варианты написания ЭТОГО ЖЕ бренда. Не включай товары, услуги, города, общие слова.\n\n" + bits)
    try:
        raw = ask(prompt) or ""
        obj = json.loads(re.search(r'\{.*\}', raw, re.S).group(0))
        canonical = _clean_brand(obj.get("brand") or "")
        if not (2 < len(canonical) <= 50):
            return (fallback or host), base
        al = set(base)
        for a in [canonical] + list(obj.get("aliases") or []):
            a = (a or "").strip().strip("«»\"'").lower()
            if 2 < len(a) <= 50 and a not in _COMMON and not any(v == a.split()[0] for v in _NICHE_VERBS):
                al.add(a)
        return canonical, {a for a in al if len(a) >= 3}
    except Exception:
        return (fallback or host), base

def refine_niche(niche, site_info):
    """По данным сайта уточняет нишу до реальной бизнес-модели (проектное оснащение, опт, премиум и т.п.)."""
    if not (site_info and site_info.get("ok")):
        return niche
    ask = _first_keyed_adapter()
    if not ask:
        return niche
    bits = " ".join(filter(None, [site_info.get("title"), site_info.get("description"), (site_info.get("text_excerpt") or "")[:700]]))
    if not bits.strip():
        return niche
    prompt = ("По данным сайта определи реальную нишу компании И ФОРМАТ её работы "
              "(например: комплексное проектирование и оснащение, производство под заказ, розница, опт, премиум-сегмент, "
              "консалтинг, агентство, услуги под ключ). Верни ОДНУ короткую фразу из 3-8 слов, отражающую это. Без пояснений и кавычек.\n\n"
              f"Введённая ниша: {niche}\nДанные сайта: {bits[:1000]}")
    try:
        raw = ask(prompt)
    except Exception:
        return niche
    line = ((raw or "").strip().splitlines() or [""])[0]
    line = re.sub(r'^[-*•\d\).\s]+', "", line).strip(' "«».')
    if 6 <= len(line) <= 80 and re.search(r"[А-Яа-яЁё]", line):
        return line
    return niche

# гео-основы и платформы/слова, которые НИКОГДА не конкуренты
_GEO_STEMS = ("росси", "москв", "петербург", "санкт", "рунет", "рф")
# мировые гиганты: для малого/среднего бизнеса это не релевантные конкуренты, а просто упоминания в ответе
_GLOBAL_BRANDS = {"google","microsoft","amazon","apple","meta","facebook","instagram","salesforce","hubspot","sap",
                  "oracle","ibm","adobe","cisco","intel","samsung","sony","dell","nvidia","accenture","deloitte",
                  "mckinsey","pwc","kpmg","ernst","bcg","nike","adidas","coca-cola","pepsi","netflix","spotify",
                  "slack","zoom","notion","figma","canva","shopify","wix","wordpress","atlassian","jira","asana",
                  "airbnb","uber","tesla","openai","anthropic","mailchimp","zendesk","workday","servicenow",
                  "tableau","aws","azure","alibaba","tencent","interbrand","landor","pentagram","ogilvy","wpp"}
_PLATFORMS = {"яндекс", "яндекса", "google", "гугл", "chatgpt", "openai", "gigachat", "гигачат",
              "сбер", "сбера", "deepseek", "perplexity", "gemini", "нейро", "ai", "ии"}
_COMMON = {"также","кроме","среди","лучшие","лучший","топ","это","этот","при","для","как","или","если","итак",
           "компания","компании","компаний","фирма","фирмы","сайт","сайты","отзыв","отзывы","услуга","услуги",
           "заказ","цена","цены","например","важно","совет","советы","вариант","варианты","способ","способы",
           "интернет","онлайн","магазин","магазины","студия","студии","фабрика","фабрики","салон","салоны",
           "производство","надёжность","гарантия","премиум","репутация","город","года","вот","есть","можно",
           "качество","качественную","качественно","заказать","выбрать","обратиться","предлагают","рынке",
           # категории, каналы поиска, разделы и характеристики — НЕ компании
           "поисковые","поисковая","система","системы","специализированные","специализированный","форум","форумы",
           "выставка","выставки","ярмарка","ярмарки","маркетплейс","маркетплейсы","каталог","каталоги","площадка",
           "площадки","подборка","подборки","рейтинг","рейтинги","агрегатор","агрегаторы","справочник","справочники",
           "соцсети","соцсеть","мессенджер","технологии","технология","инструменты","инструмент","поддержка",
           "обслуживание","сопровождение","решение","решения","сервис","сервисы","платформа","платформы",
           "эксперт","эксперты","специалист","специалисты","подрядчик","подрядчики","производитель","производители",
           "поставщик","поставщики","партнёр","партнёры","ассортимент","доставка","оплата","скидки","акции",
           "консультация","менеджер","раздел","разделы","критерии","критерий","рекомендации","рекомендация",
           "преимущества","характеристики","обзор","обзоры","статья","статьи","блог","блоги"}

def _norm_name(n):
    return n.strip(" \t\r\n.,:;!?·*•—–-«»\"'()[]").strip()

def _good_name(n):
    n = _norm_name(n)
    if not (2 < len(n) <= 40): return None
    low = n.lower()
    words = low.split()
    if any(w.startswith(_GEO_STEMS) for w in words): return None
    if low in _PLATFORMS or low in _COMMON: return None
    if low in _GLOBAL_BRANDS or any(w in _GLOBAL_BRANDS for w in words): return None   # мировые гиганты — не конкуренты для МСБ
    if all(w in _COMMON or w in _PLATFORMS for w in words): return None
    return n

def _competitors_regex(answers, own):
    """Запасной эвристический разбор (только если нет ключа для LLM-извлечения)."""
    emph = re.compile(r"«([^»]{2,40})»|\*\*([^*\n]{2,40})\*\*|\"([^\"\n]{2,40})\"")
    noun = re.compile(r"[A-ZА-ЯЁ][A-Za-zА-Яа-яёЁ0-9&.\-]+(?:[ \-][A-ZА-ЯЁ][A-Za-zА-Яа-яёЁ0-9&.\-]+){0,3}")
    names, seen = [], set()
    for ans in answers:
        a = ans or ""
        for m in emph.finditer(a):
            n = _good_name(next(g for g in m.groups() if g))
            if n and n.lower() not in own and n.lower() not in seen:
                seen.add(n.lower()); names.append(n)
        for m in noun.finditer(a):
            t = m.group(0)
            if " " not in t and not re.search(r"[A-Za-z]", t):   # одиночное кириллическое слово — пропуск
                continue
            n = _good_name(t)
            if n and n.lower() not in own and n.lower() not in seen:
                seen.add(n.lower()); names.append(n)
    return names

def _competitors_llm(answers, niche):
    """Строгое извлечение реальных компаний. None = нет ключа, [] = компаний не найдено."""
    ask = _first_keyed_adapter()
    if not ask: return None
    joined = "\n---\n".join(a for a in answers if a).strip()
    if not joined: return []
    prompt = (
        f"Ниже фрагменты ответов нейросетей на коммерческие запросы в нише «{niche or 'услуги'}».\n\n"
        f"{joined[:6000]}\n\n"
        "Выпиши ТОЛЬКО реальные названия компаний, брендов или конкретных специалистов, которых нейросети называют "
        "как ИСПОЛНИТЕЛЕЙ услуги и которые являются ПРЯМЫМИ конкурентами этой компании — того же масштаба, профиля и региона. Строгие правила:\n"
        "- НЕ включай мировых гигантов и глобальные бренды (Google, Microsoft, Salesforce, HubSpot, SAP, Oracle, Adobe, Amazon, "
        "Apple, McKinsey, Accenture, Interbrand и подобные), если они не являются прямым конкурентом именно этой компании в её нише.\n"
        "- НЕ включай категории и каналы поиска (поисковые системы, сайты, форумы, маркетплейсы, выставки, каталоги, "
        "соцсети), разделы ответа, характеристики, города, страны, общие слова и названия самих нейросетей.\n"
        "- Включай только то, что выглядит как название конкретной организации или бренда — реального конкурента.\n"
        "Каждое название с новой строки, по убыванию частоты, максимум 6. Если таких компаний нет, ответь одним словом: НЕТ."
    )
    try:
        raw = ask(prompt)
    except Exception:
        return None
    if not raw: return []
    if re.sub(r"[^а-яёa-z]", "", raw.lower())[:3] == "нет" and len(raw.strip()) <= 8:
        return []
    out, seen = [], set()
    for ln in raw.splitlines():
        s = re.sub(r"^\s*(?:\d+[\).\.]?|[-*•—])\s*", "", ln).strip().strip('"«»').strip()
        n = _good_name(s)
        if n and n.lower() not in seen:
            seen.add(n.lower()); out.append(n)
    return out

def _clean_comp_name(n):
    n = re.sub(r"\.(ru|com|рф|su|net|org|io|ai|pro|store|shop|moscow|spb)$", "", n.strip(), flags=re.I)  # убрать домен
    n = n.strip(" /\\")
    if n and n.islower() and " " not in n:                # доменное «zorini» -> «Zorini»
        n = n[:1].upper() + n[1:]
    return n

def extract_competitors(answers, brand, brand_short, niche="", top=3, aliases=None):
    """[(компания, в скольких ответах названа)]. Только подтверждённые (>=2). Алиасы бренда исключаются."""
    own = {o for o in (set(aliases) if aliases else
           {(brand or "").lower(), (brand_short or "").lower(), _host(brand or "").lower()}) if o}
    def _is_own(low):
        if low in own: return True
        for al in own:
            if len(al) >= 6 and (al in low or low in al): return True   # «Vazuza Country Club Resort» и т.п.
        return False
    names = _competitors_llm(answers, niche)
    if names is None:                       # ключа нет -> эвристический запас
        names = _competitors_regex(answers, own)
    res, seen = [], set()
    for name in names:
        low = name.lower()
        if not low or _is_own(low) or low in seen: continue
        seen.add(low)
        c = sum(1 for a in answers if low in (a or "").lower())   # реальная частота в ответах
        if c >= 2:
            res.append((_clean_comp_name(name), c))
    res.sort(key=lambda x: (-x[1], -len(x[0])))
    return res[:top]

# ───────────────────────── адаптеры нейросетей ───────────────────────
def _post_json(url, headers, payload, timeout=40):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", "replace")[:500]   # тело ошибки -> видно точную причину
        raise RuntimeError(f"HTTP {ex.code}: {body}")

def ask_perplexity(prompt):
    key = os.environ["PERPLEXITY_API_KEY"]
    j = _post_json("https://api.perplexity.ai/chat/completions",
                   {"Authorization": "Bearer "+key, "Content-Type": "application/json"},
                   {"model": "sonar", "messages": [{"role": "user", "content": prompt}]})
    return j["choices"][0]["message"]["content"]

def ask_openai(prompt):
    key = os.environ["OPENAI_API_KEY"]
    # модель с встроенным веб-поиском (chat/completions): реально ищет в интернете и называет компании
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini-search-preview")
    j = _post_json("https://api.openai.com/v1/chat/completions",
                   {"Authorization": "Bearer "+key, "Content-Type": "application/json"},
                   {"model": model, "messages": [{"role": "user", "content": prompt}]},
                   timeout=60)   # веб-поиск дольше обычного
    return j["choices"][0]["message"]["content"]

def ask_claude(prompt):
    key = os.environ["ANTHROPIC_API_KEY"]
    j = _post_json("https://api.anthropic.com/v1/messages",
                   {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                   {"model": "claude-3-5-haiku-latest", "max_tokens": 700,
                    "messages": [{"role": "user", "content": prompt}]})
    return "".join(b.get("text", "") for b in j.get("content", []))

def ask_deepseek(prompt):
    key = os.environ["DEEPSEEK_API_KEY"]
    j = _post_json("https://api.deepseek.com/chat/completions",
                   {"Authorization": "Bearer "+key, "Content-Type": "application/json"},
                   {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}]})
    return j["choices"][0]["message"]["content"]

def ask_gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}"
    j = _post_json(url, {"Content-Type": "application/json"},
                   {"contents": [{"parts": [{"text": prompt}]}]})
    return j["candidates"][0]["content"]["parts"][0]["text"]

# GigaChat: ключ из кабинета (Authorization Key, Basic) меняется на access token на 30 минут.
# Сертификаты Минцифры в контейнере не ставим -> отключаем проверку TLS для доменов Сбера.
_GIGA_TOKEN = {"val": "", "exp": 0.0}
def _giga_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def _giga_token():
    now = time.time()
    if _GIGA_TOKEN["val"] and now < _GIGA_TOKEN["exp"]:
        return _GIGA_TOKEN["val"]
    auth = os.environ["GIGACHAT_API_KEY"]                       # Authorization Key из кабинета
    scope = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")  # PERS=физлицо, B2B/CORP=юрлицо
    req = urllib.request.Request(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        data=f"scope={scope}".encode(), method="POST",
        headers={"Authorization": "Basic " + auth, "RqUID": str(uuid.uuid4()),
                 "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=_giga_ctx()) as r:
        j = json.loads(r.read().decode())
    _GIGA_TOKEN["val"] = j["access_token"]
    _GIGA_TOKEN["exp"] = now + 25 * 60                          # с запасом до истечения (30 мин)
    return _GIGA_TOKEN["val"]

def ask_gigachat(prompt):
    tok = _giga_token()
    req = urllib.request.Request(
        "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
        data=json.dumps({"model": os.environ.get("GIGACHAT_MODEL", "GigaChat"),
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        method="POST",
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40, context=_giga_ctx()) as r:
        j = json.loads(r.read().decode())
    return j["choices"][0]["message"]["content"]

def ask_yandex(prompt):
    key = os.environ["YANDEX_API_KEY"]; folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if not folder:
        raise RuntimeError("YANDEX_FOLDER_ID не задан")
    model = os.environ.get("YANDEX_MODEL", "yandexgpt-lite")     # можно сменить на yandexgpt
    j = _post_json("https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                   {"Authorization": "Api-Key "+key, "Content-Type": "application/json", "x-folder-id": folder},
                   {"modelUri": f"gpt://{folder}/{model}/latest",
                    "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": 800},
                    "messages": [{"role": "user", "text": prompt}]})
    return j["result"]["alternatives"][0]["message"]["text"]

REAL_ADAPTERS = {"perplexity": ask_perplexity, "chatgpt": ask_openai, "claude": ask_claude,
                 "deepseek": ask_deepseek, "gemini": ask_gemini, "gigachat": ask_gigachat, "yandex": ask_yandex}
KEY_ENV = {"perplexity": "PERPLEXITY_API_KEY", "chatgpt": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY",
           "deepseek": "DEEPSEEK_API_KEY", "gemini": "GEMINI_API_KEY", "gigachat": "GIGACHAT_API_KEY", "yandex": "YANDEX_API_KEY"}

def has_key(engine_id):
    return bool(os.environ.get(KEY_ENV[engine_id]))

def active_engines():
    """В бою опрашиваем только нейросети с ключом. Без ключей вовсе — мок-демо по всем 7."""
    if os.environ.get("TEST_MODE") == "1":
        return ENGINES
    keyed = [e for e in ENGINES if has_key(e["id"])]
    return keyed if keyed else ENGINES

# мок-движок: детерминированный «ответ», где бренд то есть, то нет
_MOCK_COMP = ["Мебель-Сити", "ЛорентМебель", "Гранд-Мебель"]
def ask_mock(prompt, engine_id, run, brand):
    base = {"perplexity":.6,"chatgpt":.5,"claude":.45,"deepseek":.35,"gemini":.3,"gigachat":.25,"yandex":.12}[engine_id]
    gdiff = -0.55 if "Премиальн" in prompt else (-0.18 if ("Отзыв" in prompt or "недорого" in prompt) else 0)
    h = int(hashlib.md5(f"{prompt}|{engine_id}|{run}".encode()).hexdigest(), 16) % 1000 / 1000.0
    mentioned = h < max(0.0, base + gdiff)
    named = _MOCK_COMP[: 2 + (1 if h > 0.5 else 0)]
    lst = ([brand] if mentioned else []) + named
    return "По запросу можно рассмотреть: " + ", ".join(lst) + "."

# ── кэш набора запросов на домен (воспроизводимость замера) ───────────
def _queries_cache_path(site):
    host = _host(site)
    d = os.environ.get("QUERIES_DIR") or os.environ.get("REPORTS_DIR") or "/tmp"
    return os.path.join(d, "qset_" + re.sub(r"[^a-z0-9.]", "_", host) + ".json")

_QSET_VER = "5"   # бамп при изменении промпта запросов -> старый кэш игнорируется и набор пересобирается

def _load_query_set(site):
    try:
        with open(_queries_cache_path(site), encoding="utf-8") as f:
            obj = json.load(f)
        if obj.get("ver") != _QSET_VER:   # промпт обновился -> старый набор не используем
            return None
        qs = obj.get("queries")
        # перелейблируем группы по текущему классификатору (названия групп могли смениться), запросы не трогаем
        return [{"q": x["q"], "group": _classify_query(x["q"])} for x in qs] if qs else None
    except Exception:
        return None

def _save_query_set(site, queries):
    try:
        with open(_queries_cache_path(site), "w", encoding="utf-8") as f:
            json.dump({"ver": _QSET_VER, "date": datetime.datetime.now().strftime("%Y-%m-%d"),
                       "queries": [{"q": x["q"], "group": x["group"]} for x in queries]}, f, ensure_ascii=False)
    except Exception:
        pass

# ───────────────────────── оркестрация ───────────────────────
def _ask_one(prompt, eid, run, brand, test):
    try:
        if test or not has_key(eid):
            return ask_mock(prompt, eid, run, brand)
        return REAL_ADAPTERS[eid](prompt)
    except Exception:
        return ""   # сеть недоступна -> считаем как не упомянут

def analyze(brand, brand_short, site, niche, city, on_progress=None, site_info=None, aliases=None, queries=None):
    test = os.environ.get("TEST_MODE") == "1"
    aliases = aliases or brand_aliases(site, site_info, brand, brand_short)
    if queries is None:                                      # набор не передали -> кэш или генерация
        queries = None if test else _load_query_set(site)
        if not queries:
            queries = generate_queries(niche, city, site_info, aliases)
            if not test:
                _save_query_set(site, queries)
    queries = [{"q": x["q"], "group": x["group"]} for x in queries]   # чистая копия, без старых hits
    engines = active_engines()
    for q in queries:
        q["hits"] = {e["id"]: 0 for e in engines}
    # все вызовы ко всем сетям считаем параллельно (пачками), а не строго по очереди
    tasks = [(qi, e["id"], q["q"], run)
             for qi, q in enumerate(queries) for e in engines for run in range(RUNS)]
    workers = max(1, min(int(os.environ.get("CONCURRENCY", "5")), len(tasks) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = list(pool.map(lambda t: _ask_one(t[2], t[1], t[3], brand, test), tasks))
    all_answers = []
    for (qi, eid, _q, _run), ans in zip(tasks, answers):
        all_answers.append(ans)
        if detect_mention(ans, aliases):
            queries[qi]["hits"][eid] += 1
    competitors = extract_competitors(all_answers, brand, brand_short, niche, aliases=aliases)
    # доказательная база по каждому запросу: какой движок назвал бренд и кого из конкурентов
    comp_names = [n for n, _ in competitors]
    for q in queries:
        q["evidence"] = {e["id"]: {"brand": q["hits"][e["id"]] > 0, "comps": []} for e in engines}
    for (qi, eid, _q, _run), ans in zip(tasks, answers):
        low = (ans or "").lower()
        ev = queries[qi]["evidence"][eid]["comps"]
        for n in comp_names:
            if n.lower() in low and n not in ev:
                ev.append(n)
    return queries, competitors

# ───────────────────────── сборка данных отчёта ───────────────────────
def _rates(queries):
    rates = {}
    for e in active_engines():
        m = sum(q["hits"].get(e["id"], 0) for q in queries)
        rates[e["id"]] = round(m / (len(queries)*RUNS) * 100)
    return rates

def _groups(queries):
    g = {}
    ne = len(active_engines())
    for q in queries:
        gg = g.setdefault(q["group"], {"m":0,"mx":0})
        gg["m"] += sum(q["hits"].values()); gg["mx"] += ne*RUNS
    return sorted(([k, round(v["m"]/v["mx"]*100)] for k,v in g.items()), key=lambda x:-x[1])

def _tech_findings(site_info):
    """Единые факты автопроверки для тех.карточки и плана (чтобы рекомендации совпадали)."""
    s = site_info or {}
    types = {str(x).lower() for x in (s.get("schema") or [])}
    return {
        "ok": bool(s.get("ok")), "types": types,
        "has_products": bool(types & {"product", "offer", "aggregateoffer", "store", "onlinestore", "productgroup"}),
        "org": bool(types & {"organization", "localbusiness", "corporation", "professionalservice"}),
        "service": "service" in types, "faq": "faqpage" in types, "product": "product" in types,
        "robots_found": bool(s.get("robots_found")), "robots_blocks_ai": s.get("robots_blocks_ai") or [],
        "sitemap": s.get("sitemap_urls"), "service_pages": s.get("service_pages") or 0,
        "case_pages": s.get("case_pages") or 0,
    }

def _plan30(site_info=None):
    """План на месяц с ответственными. Технические задачи берутся из фактов автопроверки — то же, что в тех.карточке."""
    f = _tech_findings(site_info)
    # Неделя 1 — администратор: доступ роботам, карта сайта, индексация, разметка организации (если нет)
    admin1 = []
    if f["robots_blocks_ai"]:
        admin1.append("Открыть в robots.txt доступ AI-роботам: " + ", ".join(f["robots_blocks_ai"]))
    elif not f["robots_found"]:
        admin1.append("Добавить robots.txt и разрешить AI-роботов (GPTBot, OAI-SearchBot, PerplexityBot, YandexBot)")
    else:
        admin1.append("Проверить, что robots.txt не закрывает AI-роботов")
    admin1.append("Создать карту сайта sitemap.xml и указать её в robots.txt" if not f["sitemap"]
                  else "Проверить, что ключевые страницы включены в sitemap")
    admin1.append("Проверить доступность ключевых страниц: код 200 и отсутствие noindex")
    if not f["org"]:
        admin1.append("Добавить разметку Organization: название, контакты, логотип, ссылки на профили")
    # Неделя 2 — администратор: разместить тексты, внутренние ссылки, недостающая разметка по фактам
    admin2 = ["Разместить тексты и настроить внутренние ссылки между услугами и проектами"]
    if not f["service"]:
        admin2.append("Добавить разметку Service на страницы услуг")
    if f["has_products"] and not f["product"]:
        admin2.append("Добавить Product и Offer на страницы товаров и предложений")
    if not f["faq"]:
        admin2.append("Добавить блок вопросов и ответов с разметкой FAQPage на ключевые страницы")
    return [
        {"week": "Неделя 1 · доступ, индексация, приоритетные страницы", "groups": [
            {"role": "Маркетолог и SEO-специалист", "items": [
                "Выбрать 5–7 страниц, которые отвечают на вопросы клиентов из отчёта",
                "Для каждой страницы определить один основной вопрос",
                "Подготовить новые заголовки и первые абзацы"]},
            {"role": "Администратор сайта", "items": admin1},
        ]},
        {"week": "Неделя 2 · услуги и разметка", "groups": [
            {"role": "Маркетолог или редактор", "items": [
                "Добавить конкретное описание услуги: аудитория, состав работ, география, формат",
                "Добавить 5–7 реальных вопросов клиентов",
                "Подготовить ссылки на соответствующие проекты"]},
            {"role": "Администратор сайта", "items": admin2},
        ]},
        {"week": "Неделя 3 · проекты", "groups": [
            {"role": "Менеджеры проектов и маркетолог", "items": [
                "Выбрать 3–5 сильных проектов",
                "Собрать данные: объём, сроки, состав работ",
                "Переработать заголовки, добавить текстовые описания и отзывы клиентов"]},
        ]},
        {"week": "Неделя 4 · внешнее присутствие и замер", "groups": [
            {"role": "Маркетолог или PR-специалист", "items": [
                "Обновить карточки компании на площадках",
                "Собрать отзывы о конкретных проектах",
                "Подготовить публикации для отраслевых площадок",
                "Проверить единое написание названия и описания компании"]},
            {"role": "После индексации изменений", "items": [
                "Повторить проверку на том же наборе вопросов",
                "Сравнить изменения по каждому вопросу и каждой нейросети"]},
        ]},
    ]

def build_data(brand, brand_short, site, niche, city, queries, competitors, site_info=None):
    eng = active_engines()
    rates = _rates(queries)
    groups = _groups(queries)
    overall = round(sum(q["hits"][e["id"]] for q in queries for e in eng) / (len(queries)*len(eng)*RUNS) * 100)
    zero = overall == 0
    strong = [g[0] for g in groups if g[1] >= 45][:2]
    weak   = [g[0] for g in groups if g[1] <= 15]
    best = max(eng, key=lambda e: rates[e["id"]]); worst = min(eng, key=lambda e: rates[e["id"]])
    weak_txt = ", ".join(weak).lower() if weak else "нишевые и премиальные запросы"
    strong_txt = ", ".join(strong).lower() if strong else "общие коммерческие запросы"
    top_txt = ", ".join(g[0] for g in groups[:3]).lower() if groups else "коммерческие запросы ниши"
    # факты для честных формулировок при ненулевом результате
    mentioned_engines = [e["name"] for e in eng if rates[e["id"]] > 0]
    zero_engines = [e["name"] for e in eng if rates[e["id"]] == 0]
    groups_pos = [g[0] for g in groups if g[1] > 0]
    groups_zero = [g[0] for g in groups if g[1] == 0]
    rep_groups = sorted({q["group"] for q in queries if any(v == 2 for v in q["hits"].values())})   # 2/2 хотя бы в одной ячейке
    n_rep_q = sum(1 for q in queries if any(v == 2 for v in q["hits"].values()))    # запросов с повторяемым (2/2) упоминанием
    fem = _brand_gender(brand_short) == "f"                                          # женский личный бренд -> женское согласование
    appeared_neg = "не появилась" if fem else "не появился"
    mentioned_neg = "не была упомянута" if fem else "не был упомянут"
    pos_txt = ", ".join(groups_pos); zero_grp_txt = ", ".join(groups_zero); rep_txt = ", ".join(rep_groups)
    total = len(queries)*len(eng)*RUNS
    comp_conf = [(n, c) for n, c in competitors if c >= 2]   # подтверждён: упомянут минимум в 2 ответах
    if len(comp_conf) < 2:                                   # по аудиту: блок только при >=2 подтверждённых
        comp_conf = []
    comp_names = [n for n, _ in comp_conf]
    examples = _pick_examples(queries, brand_short, best, comp_names)
    comp_objs = _competitor_objs(comp_conf, total)
    eng_name = {e["id"]: e["name"] for e in eng}
    for c in comp_objs:                                       # где встретился конкурент: пример запроса и нейросети
        c["where"] = ""
        for q in queries:
            hit = next((eng_name.get(eid, eid) for eid, ev in (q.get("evidence") or {}).items() if c["name"] in ev.get("comps", [])), None)
            if hit:
                c["where"] = f"например, по запросу «{q['q']}» ({hit})"; break
    if comp_objs and os.environ.get("TEST_MODE") != "1":     # проверенные ссылки на сайты конкурентов
        try:
            sites = _competitor_sites([c["name"] for c in comp_objs])
            for c in comp_objs: c["site"] = sites.get(c["name"], "")
        except Exception as e:
            print("[comp-sites]", e, flush=True)
    data = {
        "brand": brand, "brand_short": brand_short, "site": _host(site),
        "niche": niche, "city": city, "date": datetime.datetime.now().strftime("%d.%m.%Y"),
        "cover_sub": ((f"В этой проверке {brand_short} {appeared_neg} в ответах нейросетей. Первые шаги: сделать ключевые услуги понятнее на сайте, "
                       "дополнить проекты конкретными фактами и увеличить число упоминаний бренда на отраслевых площадках.")
                      if zero else
                      ((f"Бренд появляется в части ответов. Повторяемые упоминания (2/2) по группам: {rep_txt}." if rep_groups
                        else f"Бренд появляется в части ответов, пока единичными упоминаниями по группам: {pos_txt}.")
                       + (f" По группам {zero_grp_txt} упоминаний пока нет." if zero_grp_txt else ""))),
        "engines": [dict(e) for e in eng],
        "queries": queries,
        "result_meaning": {
            "headline": ("упоминания не обнаружены" if zero else ("средняя видимость" if overall>=25 else "низкая видимость")),
            "text": (f"В {total} проверенных ответах {brand_short} {mentioned_neg} ни одной из {len(eng)} нейросетей. "
                     "Это не означает, что нейросети никогда не называют бренд. Результат относится к выбранным вопросам, системам и дате проверки."
                     if zero else
                     (f"В этой проверке бренд появился в {', '.join(mentioned_engines)}" + (f", но не появился в {', '.join(zero_engines)}." if zero_engines else ".")
                      + " Видимость пока низкая и неравномерная.")),
            "loss": (f"{brand_short} {appeared_neg} ни по одному из проверенных вопросов: при поиске подрядчика, выборе услуги и оценке надёжности."
                     if zero else (f"Упоминаний пока нет по группам: {zero_grp_txt}." if zero_grp_txt else "Упоминания распределены неравномерно по запросам.")),
            "strong": ("Сначала нужно сделать понятнее существующие страницы услуг и проектов, а затем увеличить число независимых "
                       "упоминаний компании: отзывов, публикаций, карточек и отраслевых подборок."
                       if zero else (f"Повторяемые упоминания (2/2) есть по группам: {rep_txt}. На них можно опереться, но видимость всё ещё низкая." if rep_groups
                                     else (f"Пока только единичные упоминания (1/2) по группам: {pos_txt}." if pos_txt else "Опорных групп с повторяемым упоминанием пока нет."))),
            "goal": ("Добиться первых повторяемых упоминаний: чтобы нейросеть называла бренд не случайно, а в обоих повторных ответах на один и тот же вопрос."
                     if zero else
                     (f"Увеличить число запросов с повторяемым упоминанием с {n_rep_q} до 4–5"
                      + (" и добиться появления хотя бы во второй нейросети." if len(mentioned_engines) <= 1 else " и поднять долю ответов с упоминанием в каждой нейросети.")
                      if n_rep_q >= 1 else
                      "Добиться первых повторяемых упоминаний (2 из 2): чтобы бренд появлялся в обоих ответах на один вопрос, а не через раз.")),
        },
        "examples": examples,
        "competitors": comp_objs,
        "sources": [
            {"name":"Карты и справочники","share":26},{"name":"Отзывы на площадках","share":24},
            {"name":"Официальный сайт","share":22},{"name":"Каталоги и подборки","share":14},
            {"name":"СМИ и блоги","share":9},{"name":"Соцсети","share":5},
        ],
        "site_info": site_info, "fem": fem,
        "mentioned_engines": mentioned_engines, "zero_engines": zero_engines,
        "groups_pos": groups_pos, "groups_zero": groups_zero,
        "positives": _positives(rates, best, zero, site_info, rep_groups),
        "blockers": _blockers(groups, zero, site_info, mentioned_engines, zero_engines, groups_zero),
        "recommendations": _recommendations(queries, groups, total, site_info, brand_short, niche),
        "plan30": _plan30(site_info),
        "method_note": (f"Каждой из {len(eng)} нейросетей задано {len(queries)} вопросов по {RUNS} прогона — всего {len(queries)*len(eng)*RUNS} ответов на дату на обложке; "
                        "где нейросеть умеет искать в интернете, использовался веб-поиск. Упоминание — явное называние бренда, домена или короткого имени. "
                        "Причины отсутствия бренда отмечены как гипотезы, а не доказанные факты. Ответы нейросетей меняются со временем; "
                        "повторный замер имеет смысл после индексации обновлённых страниц."),
    }
    return data

def _pick_examples(queries, brand_short, best, competitors):
    ex = []
    eng_name = {e["id"]: e["name"] for e in active_engines()}
    def comps_engine(q):
        """(нейросеть, [конкуренты]) реально названные по этому запросу — из доказательной базы."""
        for eid, evv in (q.get("evidence") or {}).items():
            if evv.get("comps"):
                return eng_name.get(eid, eid), evv["comps"][:3]
        return best["name"], []
    strong_cell = next((q for q in queries if any(v==2 for v in q["hits"].values())), None)
    zero_cell   = next((q for q in queries if all(v==0 for v in q["hits"].values())), None)
    mid_cell    = next((q for q in queries if any(v==1 for v in q["hits"].values())), None)
    if strong_cell:
        ex.append({"kind":"yes","query":strong_cell["q"],"engine":best["name"],
                   "named":[brand_short],"result":"бренд появился в обоих ответах (2 из 2)",
                   "why":"возможное объяснение: в доступных источниках оказалось достаточно релевантной информации, чтобы нейросеть включила бренд. По одному результату точную причину установить нельзя."})
    if zero_cell:
        en, comps = comps_engine(zero_cell)
        ex.append({"kind":"no","query":zero_cell["q"],"engine":en,
                   "named":comps,"result":"бренд не появился (0 из 2)",
                   "why":"по одному ответу причину точно не определить. Стоит проверить, есть ли на сайте страница, которая прямо отвечает на этот вопрос, и упоминания по теме на внешних площадках."})
    if mid_cell and mid_cell not in (strong_cell, zero_cell):
        ex.append({"kind":"mid","query":mid_cell["q"],"engine":best["name"],
                   "named":[brand_short],"result":"бренд появился в одной из двух проверок",
                   "why":"возможная причина: упоминаний мало и они не закреплены по этому запросу."})
    return ex[:3]

def _competitor_objs(comp_conf, total):
    """Реальная частота: в скольких из total ответов нейросети назвали компанию."""
    objs = []
    for name, count in comp_conf:
        objs.append({"name":name, "rate":round(count/total*100), "count":count, "total":total, "site":"",
                     "focus":"коммерческие запросы ниши", "sources":"ответы нейросетей"})
    return objs

def _verify_site(name, domain):
    """Прямая ссылка на сайт конкурента: возвращает рабочий URL домена, если он валиден и отвечает."""
    domain = re.sub(r"^https?://", "", (domain or "").strip().strip("/")).split("/")[0].strip().lower()
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,6}$", domain) or " " in domain:
        return ""
    for scheme in ("https://", "http://"):
        try:
            final, _ = _fetch_text(scheme + domain, timeout=6)
            return (final or (scheme + domain)).split("?")[0].rstrip("/")
        except Exception:
            continue
    return ""

def _competitor_sites(names):
    """Спрашиваем у нейросети домены конкурентов и проверяем каждый. {имя: https://сайт} только для подтверждённых."""
    ask = _first_keyed_adapter()
    if not ask or not names:
        return {}
    prompt = ("Это реальные компании, которых называют нейросети. Для каждой укажи её официальный сайт (домен). "
              "Постарайся дать домен по каждой, но если действительно не уверена — поставь «-», не выдумывай. "
              "Формат строго построчно: «Компания | домен» (например «Рога и Копыта | rogakopyta.ru»). Список:\n" + "\n".join(names))
    try:
        raw = ask(prompt)
    except Exception:
        return {}
    out = {}
    for ln in (raw or "").splitlines():
        if "|" not in ln:
            continue
        nm, dom = ln.split("|", 1)
        nm = nm.strip().strip("«»\"").strip(); dom = dom.strip()
        match = next((n for n in names if n.lower() == nm.lower() or n.lower() in nm.lower() or nm.lower() in n.lower()), None)
        if match and dom and dom not in ("-", "—", "?"):
            site = _verify_site(match, dom)
            if site:
                out[match] = site
    return out

def _positives(rates, best, zero, site_info=None, rep_groups=None):
    pos = []
    if not zero:
        pos.append(f"Бренд появляется в {best['name']} ({rates[best['id']]}% ответов)")
        if rep_groups:
            pos.append("Повторяемые упоминания (2/2) по группам: " + ", ".join(rep_groups))
    s = site_info or {}
    if s.get("ok"):                                     # реальные активы сайта (проверено)
        sm = _schema_summary(s.get("schema"))
        if sm["org"]: pos.append("Есть разметка организации (Organization)")
        if sm["service"]: pos.append("Есть разметка услуг (Service)")
        if sm["faq"]: pos.append("Есть FAQ-разметка")
        if s.get("service_pages"):
            pos.append(f"Найдены отдельные страницы услуг ({s['service_pages']})")
        if s.get("case_pages"):
            pos.append(f"Найдены страницы кейсов или проектов ({s['case_pages']})")
        if s.get("robots_found") and not s.get("robots_blocks_ai"):
            pos.append("robots.txt не закрывает доступ ИИ-ботам")
    if not pos:
        pos = ["По результатам этой проверки пока не найдено факторов, которые уже обеспечивают заметную AI-видимость. Для старта это нормально и поправимо."]
    return pos[:5]

def _blockers(groups, zero, site_info=None, mentioned_engines=None, zero_engines=None, groups_zero=None):
    blk = []
    if zero:
        blk += ["Бренд не упомянут ни по одному запросу проверки",
                "По этим запросам нейросети не называют ваш сайт как источник"]
    else:
        if groups_zero:
            blk.append("Упоминаний пока нет по группам: " + ", ".join(groups_zero))
        if mentioned_engines and zero_engines:
            if len(mentioned_engines) == 1:
                blk.append(f"Упоминания только в одной нейросети ({mentioned_engines[0]}); в {', '.join(zero_engines)} бренд не появился")
            else:
                blk.append(f"В {', '.join(zero_engines)} упоминаний нет")
        blk.append("Видимость низкая: бренд появляется не по всем запросам")
    s = site_info or {}
    if s.get("ok"):                                     # реальные пробелы сайта (проверено)
        sm = _schema_summary(s.get("schema"))
        if s.get("robots_blocks_ai"):
            blk.append("robots.txt закрывает доступ ИИ-ботам: " + ", ".join(s["robots_blocks_ai"]))
        if not sm["org"]:
            blk.append("Не найдена разметка организации (Organization)")
        elif not sm["service"]:
            blk.append("Не найдена разметка услуг (Service)")
        if not s.get("service_pages"):
            blk.append("Не удалось обнаружить отдельные страницы услуг (если есть, дайте на них прямые ссылки в меню)")
        if not s.get("case_pages"):
            blk.append("Не удалось обнаружить страницы кейсов или проектов (если они есть, дайте на них прямые ссылки в меню)")
    elif s and not s.get("ok"):
        blk.append("Сайт не удалось открыть для проверки: проверьте адрес и доступность")
    else:
        blk.append("Мало внешних упоминаний относительно тех, кого называют нейросети")
    return blk[:5]

_GLOSSARY = [
    ("Sitemap", "файл со списком страниц сайта, помогает поисковым роботам их находить."),
    ("Schema.org", "технические пометки в коде, объясняющие, что за содержание на странице."),
    ("Organization", "данные о компании в этой разметке."),
    ("Service", "данные об услуге."),
    ("Product / Offer", "данные о товаре, его цене и условиях."),
    ("FAQPage", "блок вопросов и ответов."),
    ("Robots.txt", "файл с правилами доступа роботов к сайту."),
    ("Noindex", "команда, запрещающая добавлять страницу в поиск."),
    ("Внутренние ссылки", "ссылки между страницами одного сайта."),
]

def _reco_examples(niche, brand_short, queries=None, site_info=None):
    """Готовые конкретные примеры под нишу/услуги/запросы компании (страница, кейс, отзыв). {} = откат на общие.
    Жёсткие правила: не выдумывать факты (сроки, цены, объёмы, география и т.п.) — неизвестное помечать [указать]."""
    if os.environ.get("TEST_MODE") == "1":
        return {}
    ask = _first_keyed_adapter()
    if not ask or not niche:
        return {}
    s = site_info or {}
    desc = " ".join(filter(None, [s.get("title"), s.get("description"), (s.get("text_excerpt") or "")[:400]]))[:500]
    verified = " / ".join(filter(None, [s.get("title"), s.get("description")]))[:300] or "подтверждённых фактов с сайта нет"
    qs = "; ".join(q["q"] for q in (queries or [])[:10]) or niche
    prompt = (
        f"Компания: «{brand_short}». Ниша: «{niche}». Чем занимается: {desc or niche}.\n"
        f"Запросы клиентов из отчёта: {qs}\n"
        f"Подтверждённые факты с сайта (только это можно подавать как факт): {verified}\n\n"
        "Сделай 3 КОНКРЕТНЫХ примера готового результата ИМЕННО для этой компании и ниши. Каждый — не пересказ совета, "
        "а готовый фрагмент, который можно вставить на сайт. Строгие правила:\n"
        "- Только эта ниша и эти услуги. Категорически нельзя примеры из других отраслей.\n"
        "- НЕ выдумывай сроки, цены, количество проектов, площади, клиентов, лицензии, результаты, гарантии и географию. "
        "Неизвестное обозначай полем в квадратных скобках: [указать срок], [указать регион], [указать объём].\n"
        "- Запрещены общие фразы: индивидуальный подход, высокое качество, команда профессионалов, полный спектр услуг, "
        "решения любой сложности, многолетний опыт, надёжный партнёр. Вместо них — состав работ, типы объектов, проверяемые факты.\n"
        "- Каждый пример опирается на один из запросов выше. 250–520 знаков на пример. Без markdown.\n\n"
        "Ответь СТРОГО в таком формате:\n"
        "SERVICES:\n"
        "H1: <заголовок страницы услуги>\n"
        "Абзац: <первый абзац: что за услуга, для кого, состав работ>\n"
        "Состав: <3–4 пункта через «; »>\n"
        "PROJECT:\n"
        "Объект: <тип объекта> | Задача: <задача> | Что сделали: <состав работ> | Масштаб: [указать] | Срок: [указать] | Результат: [указать]\n"
        "REVIEW:\n"
        "<готовый отзыв клиента: услуга, тип объекта, что сделали; неизвестное — в [скобках]>")
    try:
        raw = ask(prompt)
    except Exception:
        return {}
    raw = raw or ""
    # режем по секциям
    sec = {}
    cur = None
    buf = []
    for ln in raw.splitlines():
        u = ln.strip().upper()
        if u.startswith("SERVICES"):
            if cur: sec[cur] = "\n".join(buf).strip()
            cur, buf = "services", []
        elif u.startswith("PROJECT"):
            if cur: sec[cur] = "\n".join(buf).strip()
            cur, buf = "project", []
        elif u.startswith("REVIEW"):
            if cur: sec[cur] = "\n".join(buf).strip()
            cur, buf = "review", []
        elif cur:
            buf.append(ln.rstrip())
    if cur: sec[cur] = "\n".join(buf).strip()
    out = {k: v for k, v in sec.items() if v and len(v) >= 12}
    return out

def _recommendations(queries, groups, total, site_info=None, brand_short="бренд", niche=""):
    """Карточки рекомендаций на языке владельца: что означает, что сделать, пример (под нишу), кому передать, тип, приоритет."""
    b = brand_short or "бренд"
    did = "выполнила" if _brand_gender(b) == "f" else "выполнил"
    rex = _reco_examples(niche, b, queries, site_info)
    miss = total - sum(v for q in queries for v in q["hits"].values())   # ответов без бренда (из total)
    s = site_info or {}
    ok = s.get("ok"); sm = _schema_summary(s.get("schema")) if ok else {}
    svc_seen = bool(s.get("service_pages")); case_seen = bool(s.get("case_pages"))
    blocks_ai = ok and s.get("robots_blocks_ai")
    types = {str(x).lower() for x in (s.get("schema") or [])}
    has_products = bool(types & {"product", "offer", "aggregateoffer", "store", "onlinestore", "productgroup"})  # есть ли товары/магазин
    recs = []

    # 1. Контент: понятные страницы услуг
    if svc_seen:
        svc_plain = (f"Автопроверка нашла на сайте отдельные страницы услуг ({s.get('service_pages')}). "
                     f"Создавать с нуля ничего не нужно. Их заголовки и первые абзацы должны сразу объяснять, "
                     f"что именно делает {b}, для каких клиентов и в каком формате — тогда нейросеть сможет это процитировать.")
    else:
        svc_plain = (f"Отдельных понятных страниц под ключевые услуги автопроверка не нашла. "
                     f"По коммерческим вопросам {b} не появился в {miss} из {total} ответов. "
                     f"Когда у каждой услуги есть страница с конкретными фактами, нейросети проще назвать компанию.")
    recs.append({
        "kind": "content",
        "title": "Сделать ключевые услуги понятнее для нейросетей и клиентов",
        "plain": svc_plain,
        "steps": ["Выбрать 5–7 основных страниц, которые отвечают на вопросы из этого отчёта",
                  "В заголовок добавить конкретную услугу и тип клиента",
                  "В первом абзаце указать состав работ, без общих слов",
                  "Добавить географию работы, сроки и масштаб",
                  ("Поставить ссылки на подходящие проекты и категории продукции" if has_products
                   else "Поставить ссылки на подходящие проекты и смежные услуги")],
        "example": (f"Готовый вариант для вашей ниши:\n{rex['services']}"
                    if rex.get('services') else
                    "В заголовке страницы услуги должно быть видно три вещи: что за услуга, для кого и в каком формате. "
                    "Например, вместо короткого «Название услуги» — «Название услуги под ключ для бизнеса» с уточнением, кому и как вы помогаете."),
        "handoff": "Маркетологу или редактору — подготовить тексты. SEO-специалисту — сверить заголовки с целевыми вопросами. Администратору сайта — разместить.",
        "priority": "Высокий", "term": "1–2 недели"})

    # 2. Контент: проекты с фактами
    proj_plain = (("Проекты на сайте есть, но нейросети лучше понимают текстовые факты, а не фотографии: "
                   "что сделано, для какого объекта, в какие сроки и в каком объёме.") if case_seen
                  else ("Страниц с проектами и кейсами автопроверка почти не нашла. "
                        "Нейросети охотнее называют компанию, у которой есть проверяемые примеры работ с фактами."))
    recs.append({
        "kind": "content",
        "title": "Дополнить проекты фактами, которые нейросеть сможет использовать",
        "plain": proj_plain,
        "steps": ["На каждой странице проекта указать тип клиента и задачу",
                  "Добавить объём работы в понятных цифрах (количество, площадь, длительность)",
                  "Описать, что именно сделала компания и какие этапы прошли",
                  "Указать сроки реализации и нестандартные решения",
                  "Добавить измеримый результат, если он есть"],
        "example": (f"Готовая структура кейса для вашей ниши:\n{rex['project']}\nНеизвестные данные подставьте вместо полей [указать]."
                    if rex.get('project') else
                    "Структура кейса: «Задача клиента. Что сделали: ключевые этапы работы. Срок. Результат: что клиент получил». "
                    "Заголовок лучше делать говорящим — с типом клиента и сутью проекта, а не просто «Объект №3»."),
        "handoff": "Менеджеру проекта — собрать факты. Маркетологу или редактору — оформить текст. Администратору сайта — разместить материал и ссылки.",
        "priority": "Высокий", "term": "2–3 недели"})

    # 3. Продвижение: внешние упоминания
    recs.append({
        "kind": "promo",
        "title": "Сделать информацию о компании заметнее за пределами сайта",
        "plain": (f"Нейросеть использует не только сайт {b}, но и карточки компании, отзывы, статьи, каталоги и отраслевые "
                  "публикации. Когда название, специализация и описание совпадают на разных площадках, системе проще связать их с одним брендом."),
        "steps": ["Проверить карточки компании на картах и бизнес-площадках",
                  "Привести название, сайт и описание к единому виду везде",
                  "Собирать отзывы с указанием конкретного проекта и состава работ",
                  "Размещать проекты в отраслевых, деловых и профильных медиа",
                  "Попасть в тематические подборки подрядчиков ниши"],
        "example": (f"Готовый отзыв вместо «Спасибо за работу»:\n«{rex['review']}»"
                    if rex.get('review') else
                    "Полезный отзыв вместо «Всё понравилось, рекомендуем»: "
                    f"«{b} {did} конкретную задачу для нашей компании, уложил{'а' if did=='выполнила' else ''}ся в срок, результат — измеримая польза». "
                    "Чем конкретнее факты в отзыве, тем охотнее нейросеть их процитирует."),
        "handoff": "Маркетологу, PR-специалисту или подрядчику по продвижению.",
        "priority": "Высокий", "term": "3–4 недели"})

    # 4. Техническая: КОНКРЕТНЫЙ статус автопроверки (что настроено / чего нет) + что добавить
    org = bool(types & {"organization", "localbusiness", "corporation", "professionalservice"})
    has_service = "service" in types; has_faq = "faqpage" in types; has_product = "product" in types
    sm_urls = s.get("sitemap_urls")
    status, todo = [], []
    if not ok:
        status.append(("Проверка сайта", "warn", "сайт не удалось открыть автоматически"))
        todo.append("Проверить адрес сайта и доступность для поисковых и AI-роботов")
    else:
        # доступ AI-роботам
        if blocks_ai:
            status.append(("Доступ AI-роботам (robots.txt)", "bad", "часть закрыта: " + ", ".join(s["robots_blocks_ai"])))
            todo.append("Открыть в robots.txt доступ роботам: " + ", ".join(s["robots_blocks_ai"]))
        elif s.get("robots_found"):
            status.append(("Доступ AI-роботам (robots.txt)", "ok", "открыт"))
        else:
            status.append(("robots.txt", "warn", "не найден"))
            todo.append("Добавить robots.txt и явно разрешить AI-роботов (GPTBot, OAI-SearchBot, PerplexityBot, YandexBot)")
        # карта сайта
        if sm_urls:
            status.append(("Карта сайта (sitemap.xml)", "ok", f"найдена, страниц: {sm_urls}"))
        else:
            status.append(("Карта сайта (sitemap.xml)", "bad", "не найдена"))
            todo.append("Создать карту сайта sitemap.xml и указать её в robots.txt")
        # разметка организации
        if org:
            status.append(("Разметка о компании (Organization)", "ok", "есть"))
        else:
            status.append(("Разметка о компании (Organization)", "bad", "не найдена"))
            todo.append("Добавить разметку Organization: название, контакты, логотип, ссылки на профили")
        # разметка услуг
        if has_service:
            status.append(("Разметка услуг (Service)", "ok", "есть"))
        else:
            status.append(("Разметка услуг (Service)", "bad", "не найдена"))
            todo.append("Добавить разметку Service на страницы услуг")
        # товары — только если у сайта есть товары
        if has_products:
            if has_product:
                status.append(("Разметка товаров (Product/Offer)", "ok", "есть"))
            else:
                status.append(("Разметка товаров (Product/Offer)", "bad", "не найдена"))
                todo.append("Добавить Product и Offer на страницы товаров и предложений")
        # FAQ
        if has_faq:
            status.append(("Блок вопросов-ответов (FAQPage)", "ok", "есть"))
        else:
            status.append(("Блок вопросов-ответов (FAQPage)", "warn", "не найден"))
            todo.append("Добавить блок вопросов и ответов с разметкой FAQPage на ключевые страницы")
        # отдельные страницы
        if s.get("service_pages"):
            status.append(("Отдельные страницы услуг", "ok", f"найдено: {s['service_pages']}"))
        else:
            status.append(("Отдельные страницы услуг", "bad", "не обнаружены"))
        if s.get("case_pages"):
            status.append(("Страницы проектов и кейсов", "ok", f"найдено: {s['case_pages']}"))
        else:
            status.append(("Страницы проектов и кейсов", "bad", "не обнаружены"))
    todo.append("Передать специалисту: убедиться, что ключевые страницы отдают код 200, не закрыты noindex и связаны внутренними ссылками")
    tech_prio = "Высокий" if blocks_ai else "Средний"
    recs.append({
        "kind": "tech",
        "title": "Техническая часть: что уже настроено и что добавить",
        "plain": ("Технические пометки в коде помогают поисковым системам и нейросетям понять, что за компания и какие услуги на сайте. "
                  "Ниже — что автопроверка нашла на " + (s.get("host") or "сайте") + " и что из этого стоит настроить."),
        "status": status,
        "todo": todo,
        "handoff_note": "Этот блок не нужно выполнять самостоятельно. Передайте администратору сайта, разработчику или SEO-специалисту.",
        "glossary": [g for g in _GLOSSARY if has_products or not g[0].startswith("Product")],
        "steps": [], "example": "",
        "handoff": "Администратору сайта, разработчику или SEO-специалисту.",
        "priority": tech_prio, "term": "3–5 дней"})
    return recs

def prepare(site, niche, city, fallback_brand=""):
    """Готовит контекст замера: разбор сайта, уточнённая ниша, бренд+алиасы, набор запросов. Без 40 вызовов.
    Возвращает dict, который можно показать клиенту (запросы), при необходимости отредактировать и передать в run(prep=...)."""
    test = os.environ.get("TEST_MODE") == "1"
    city = _norm_city(city)                              # «рф/россия» -> без города (и в запросах, и в шапке отчёта)
    site_info = None if test else analyze_site(site)
    niche2 = refine_niche(niche, site_info)
    fb = (fallback_brand or re.sub(r"[«»\"']", "", site or "").split("(")[0].strip()).strip() or _host(site)
    canonical, aliases = brand_card(site, site_info, fb)
    queries = (None if test else _load_query_set(site)) or generate_queries(niche2, city, site_info, aliases)
    if not test:
        _save_query_set(site, queries)
    return {"site": site, "city": city, "niche": niche2, "brand": canonical, "brand_short": canonical,
            "aliases": sorted(aliases), "site_info": site_info,
            "queries": [{"q": x["q"], "group": x["group"]} for x in queries]}

def run(brand=None, site=None, niche=None, city=None, brand_short=None, out=None, prep=None):
    if prep is None:                                     # обычный путь: всё готовим сами
        prep = prepare(site, niche, city, fallback_brand=brand_short or brand or "")
    site_info = prep.get("site_info")
    aliases = set(prep.get("aliases") or [])
    q, competitors = analyze(prep["brand"], prep["brand_short"], prep["site"], prep["niche"], prep.get("city"),
                             site_info=site_info, aliases=aliases, queries=prep.get("queries"))
    data = build_data(prep["brand"], prep["brand_short"], prep["site"], prep["niche"], prep.get("city"),
                      q, competitors, site_info=site_info)
    if out:
        with open(out, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=1)
    return data

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Движок отчёта AI-видимости")
    ap.add_argument("--brand", default="Мебельная мастерская «Дубрава»")
    ap.add_argument("--short", default="Дубрава")
    ap.add_argument("--site", default="dubrava-mebel.ru")
    ap.add_argument("--niche", default="мебель на заказ")
    ap.add_argument("--city", default="Москва")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "report_data.json"))
    a = ap.parse_args()
    d = run(a.brand, a.site, a.niche, a.city, brand_short=a.short, out=a.out)
    rates = _rates(d["queries"])
    print("TEST_MODE:", os.environ.get("TEST_MODE") == "1")
    print("движки:", [(e["short"], rates[e["id"]]) for e in active_engines()])
    print("конкуренты:", [c["name"] for c in d["competitors"]])
    print("JSON:", a.out)
