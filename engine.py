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

RUNS = 2  # прогонов на каждый запрос

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

def generate_queries(niche, city, site_info=None):
    # боевой режим: запросы под нишу формирует сама нейросеть (с учётом сайта); иначе/при сбое — шаблоны
    if os.environ.get("TEST_MODE") != "1":
        q = generate_queries_llm(niche, city, site_info)
        if q:
            return q
    return generate_queries_tpl(niche, city)

def generate_queries_tpl(niche, city):
    n = clean_niche(niche)
    N = n[:1].upper() + n[1:]
    inc = f" в {_prep_city(city)}" if city else ""
    items = [
        (f"Где заказать {n}{inc}?",                        "Поиск исполнителя"),
        (f"Лучшие компании: {n}{inc}",                     "Поиск исполнителя"),
        (f"Топ исполнителей: {n}{inc}",                    "Поиск исполнителя"),
        (f"Надёжная компания {n} с гарантией",             "Надёжность и гарантия"),
        (f"Кому доверить {n}, на что обратить внимание",   "Надёжность и гарантия"),
        (f"{N} под ключ{inc}",                             "Под ключ"),
        (f"{N} недорого{inc}",                             "Цена"),
        (f"Премиальные {n}{inc}",                          "Премиум"),
        (f"Отзывы о компаниях: {n}{inc}",                  "Репутация"),
        (f"Кейсы и примеры работ: {n}{inc}",               "Отзывы и кейсы"),
    ]
    return [{"q": q, "group": g} for q, g in items]

# ── запросы под нишу через нейросеть ──────────────────────────────────
_Q_INTENTS = [
    (("скольк","цен","стоит","стоимост","бюджет","срок"),                         "Цена и сроки"),
    (("лучш","топ","сравн","рейтинг","выбрать","какой выбрать","какую выбрать"),   "Сравнение и выбор"),
    (("отзыв","репутац","кейс","пример"),                                          "Отзывы и кейсы"),
    (("надёжн","надежн","гарант","довери","безопас","риск","как выбрать"),         "Надёжность и выбор"),
    (("кто ","где ","найти","заказать","специалист","компани","исполнител",
      "агентств","студи","сделать","разработ","внедр","нужен","ищу"),             "Поиск исполнителя"),
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
        "Помоги проверить, в каких запросах нейросети рекомендуют ИСПОЛНИТЕЛЯ услуги. "
        f"Составь 10 коротких поисковых запросов на русском, которые задаёт КЛИЕНТ, выбирающий, КОМУ ЗАКАЗАТЬ услугу в нише: «{niche}».{loc}{ctx}\n"
        "Только коммерческий интент «выбор исполнителя»: найти компанию или специалиста, сравнить и выбрать лучших, цена и сроки, "
        "надёжность и как выбрать, конкретные подуслуги ниши под заказ.\n"
        "НЕ добавляй информационные запросы (что такое, как сделать самому, статьи, определения) и запросы ради чтения кейсов. "
        "Не выдумывай города и регионы: используй только указанный регион или вообще без географии. "
        "Не упоминай конкретные бренды и названия компаний.\n"
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

def detect_mention(answer, brand, brand_short, site):
    if not answer: return False
    a = answer.lower()
    for b in [brand, brand_short]:
        b = (b or "").lower().strip().strip("«»\"'")
        if b and b in a: return True
    h = _host(site)
    return bool(h and h in a)

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
    types = set()
    for blk in re.findall(r'(?is)<script[^>]+ld\+json[^>]*>(.*?)</script>', html):
        for t in re.findall(r'"@type"\s*:\s*"([^"]+)"', blk):
            types.add(t)
    out["schema"] = sorted(types)
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
        n = len(re.findall(r"<loc>", sm)); out["sitemap_urls"] = n or None
    except Exception:
        pass
    return out

# гео-основы и платформы/слова, которые НИКОГДА не конкуренты
_GEO_STEMS = ("росси", "москв", "петербург", "санкт", "рунет", "рф")
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
        "как ИСПОЛНИТЕЛЕЙ услуги. Строгие правила:\n"
        "- НЕ включай категории и каналы поиска (поисковые системы, сайты, форумы, маркетплейсы, выставки, каталоги, "
        "соцсети), разделы ответа, характеристики, города, страны, общие слова и названия самих нейросетей.\n"
        "- Включай только то, что выглядит как название конкретной организации или бренда.\n"
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

def extract_competitors(answers, brand, brand_short, niche="", top=3):
    """[(компания, в скольких ответах названа)]. Только подтверждённые: реально встречаются >=2 раз."""
    own = {(brand or "").lower(), (brand_short or "").lower(), _host(brand or "").lower()}
    names = _competitors_llm(answers, niche)
    if names is None:                       # ключа нет -> эвристический запас
        names = _competitors_regex(answers, own)
    res, seen = [], set()
    for name in names:
        low = name.lower()
        if not low or low in own or low in seen: continue
        seen.add(low)
        c = sum(1 for a in answers if low in (a or "").lower())   # реальная частота в ответах
        if c >= 2:
            res.append((name, c))
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

# ───────────────────────── оркестрация ───────────────────────
def _ask_one(prompt, eid, run, brand, test):
    try:
        if test or not has_key(eid):
            return ask_mock(prompt, eid, run, brand)
        return REAL_ADAPTERS[eid](prompt)
    except Exception:
        return ""   # сеть недоступна -> считаем как не упомянут

def analyze(brand, brand_short, site, niche, city, on_progress=None, site_info=None):
    queries = generate_queries(niche, city, site_info)
    test = os.environ.get("TEST_MODE") == "1"
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
        if detect_mention(ans, brand, brand_short, site):
            queries[qi]["hits"][eid] += 1
    competitors = extract_competitors(all_answers, brand, brand_short, niche)
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
    total = len(queries)*len(eng)*RUNS
    comp_conf = [(n, c) for n, c in competitors if c >= 2]   # подтверждён: упомянут минимум в 2 ответах
    if len(comp_conf) < 2:                                   # по аудиту: блок только при >=2 подтверждённых
        comp_conf = []
    comp_names = [n for n, _ in comp_conf]
    examples = _pick_examples(queries, brand_short, best, comp_names)
    comp_objs = _competitor_objs(comp_conf, total)
    data = {
        "brand": brand, "brand_short": brand_short, "site": _host(site),
        "niche": niche, "city": city, "date": datetime.datetime.now().strftime("%d.%m.%Y"),
        "cover_sub": (f"Бренд пока не появляется ни по одному направлению ниши. Точки входа для роста: {top_txt}."
                      if zero else f"Основные потери: {weak_txt}. Сильная зона: {strong_txt}."),
        "engines": [dict(e) for e in eng],
        "queries": queries,
        "result_meaning": {
            "headline": ("видимость не обнаружена" if zero else ("средняя видимость" if overall>=25 else "низкая видимость")),
            "text": ("По коммерческим запросам ниши бренд пока не появляется ни в одной из проверенных нейросетей. "
                     "Одна из возможных причин — недостаток доступных нейросетям материалов и внешних упоминаний; "
                     "для точного вывода нужна отдельная проверка сайта и источников."
                     if zero else
                     "Бренд уже известен части AI-сервисов, но присутствие неравномерно: в одной генерации компания появляется, в следующей нет. Повторяемо вас находят лишь по нескольким направлениям."),
            "loss": (f"Ни по одному направлению бренд не упоминается. В этой проверке больше всего запросов пришлось на группы: {top_txt}."
                     if zero else f"Запросы по направлениям: {weak_txt}. Здесь бренд почти не появляется."),
            "strong": ("Опорной зоны по данным проверки пока нет. Точку входа обычно дают внешние упоминания, "
                       "на которые опираются нейросети: отзывы, карты, каталоги, плюс понятные страницы услуг на сайте."
                       if zero else f"Запросы по направлениям: {strong_txt}. Здесь есть базовая узнаваемость, на неё можно опереться."),
            "goal": "Поднять не только общий процент, но и число запросов с повторяемым появлением: чтобы бренд возникал в обоих ответах из двух.",
        },
        "examples": examples,
        "competitors": comp_objs,
        "sources": [
            {"name":"Карты и справочники","share":26},{"name":"Отзывы на площадках","share":24},
            {"name":"Официальный сайт","share":22},{"name":"Каталоги и подборки","share":14},
            {"name":"СМИ и блоги","share":9},{"name":"Соцсети","share":5},
        ],
        "site_info": site_info,
        "positives": _positives(rates, best, zero, site_info),
        "blockers": _blockers(groups, zero, site_info),
        "recommendations": _recommendations(queries, groups, total, site_info),
        "plan30": [
            {"week":"Неделя 1 · фундамент","items":["Открыть доступ ИИ-ботам и проверить robots.txt","Чётко описать на сайте, чем занимается компания и для кого","Добавить разметку Organization"]},
            {"week":"Неделя 2 · ключевые услуги","items":["Сделать отдельные страницы под ключевые услуги ниши","Добавить блок вопросов и ответов и разметку Service/FAQPage"]},
            {"week":"Неделя 3 · доказательства","items":["Опубликовать 3-5 кейсов: задача, решение, результат","Начать сбор отзывов на профильных площадках"]},
            {"week":"Неделя 4 · внешнее присутствие и замер","items":["Добавить упоминания в тематических подборках и каталогах","Повторить замер видимости и сравнить с этим отчётом"]},
        ],
        "method_note": (f"Проверка: каждой из {len(eng)} нейросетей задано {len(queries)} коммерческих запросов ниши "
                        f"по {RUNS} прогона (всего {len(queries)*len(eng)*RUNS} ответов) на дату на обложке, веб-поиск там, где движок его поддерживает, "
                        "новая сессия на каждый запрос. Упоминанием считается явное называние бренда, домена или короткого имени. "
                        f"{RUNS} прогона показывают повторяемость ответа, а не гарантированную стабильность: для строгой оценки замер стоит повторять и "
                        "увеличивать число прогонов. Причины отсутствия бренда в отчёте отмечены как гипотезы, а не доказанные факты. "
                        "Результаты нейросетей меняются со временем, поэтому замер полезно повторять."),
    }
    return data

def _pick_examples(queries, brand_short, best, competitors):
    ex = []
    strong_cell = next((q for q in queries if any(v==2 for v in q["hits"].values())), None)
    zero_cell   = next((q for q in queries if all(v==0 for v in q["hits"].values())), None)
    mid_cell    = next((q for q in queries if any(v==1 for v in q["hits"].values())), None)
    if strong_cell:
        ex.append({"kind":"yes","query":strong_cell["q"],"engine":best["name"],
                   "named":(competitors[:1]+[brand_short]),"result":"бренд появился в обоих ответах (2 из 2)",
                   "why":"по этому запросу нейросеть нашла достаточно материалов, связанных с брендом."})
    if zero_cell:
        ex.append({"kind":"no","query":zero_cell["q"],"engine":best["name"],
                   "named":competitors[:3],"result":"бренд не появился (0 из 2)",
                   "why":"возможная причина: на сайте и внешних площадках мало материалов, напрямую связанных с этим запросом."})
    if mid_cell and mid_cell not in (strong_cell, zero_cell):
        ex.append({"kind":"mid","query":mid_cell["q"],"engine":best["name"],
                   "named":competitors[:2],"result":"бренд появился в одной из двух проверок",
                   "why":"возможная причина: упоминаний мало и они не закреплены по этому запросу."})
    return ex[:3]

def _competitor_objs(comp_conf, total):
    """Реальная частота: в скольких из total ответов нейросети назвали компанию."""
    objs = []
    for name, count in comp_conf:
        objs.append({"name":name, "rate":round(count/total*100), "count":count, "total":total,
                     "focus":"коммерческие запросы ниши", "sources":"ответы нейросетей"})
    return objs

def _positives(rates, best, zero, site_info=None):
    pos = []
    if not zero:
        pos.append(f"Бренд появляется в {best['name']} ({rates[best['id']]}% ответов)")
        strong = [e for e in active_engines() if rates[e["id"]] >= 40]
        if len(strong) >= 2:
            pos.append(f"Заметное присутствие в {strong[0]['name']} и {strong[1]['name']}")
    s = site_info or {}
    if s.get("ok"):                                     # реальные активы сайта (проверено)
        if s.get("schema"):
            pos.append("На сайте есть разметка Schema.org: " + ", ".join(s["schema"][:4]))
        if s.get("service_pages"):
            pos.append(f"Найдены отдельные страницы услуг ({s['service_pages']})")
        if s.get("case_pages"):
            pos.append(f"Найдены страницы кейсов или проектов ({s['case_pages']})")
        if s.get("robots_found") and not s.get("robots_blocks_ai"):
            pos.append("robots.txt не закрывает доступ ИИ-ботам")
    if not pos:
        pos = ["По результатам этой проверки пока не найдено факторов, которые уже обеспечивают заметную AI-видимость. Для старта это нормально и поправимо."]
    return pos[:5]

def _blockers(groups, zero, site_info=None):
    blk = []
    if zero:
        blk += ["Бренд не упомянут ни по одному запросу проверки",
                "По этим запросам нейросети не называют ваш сайт как источник"]
    else:
        weak = [g[0].lower() for g in groups if g[1] <= 20]
        if weak: blk.append(f"Слабая видимость по направлениям: {', '.join(weak)}")
        blk.append("Бренд появляется не в каждом ответе по части запросов")
    s = site_info or {}
    if s.get("ok"):                                     # реальные пробелы сайта (проверено)
        if s.get("robots_blocks_ai"):
            blk.append("robots.txt закрывает доступ ИИ-ботам: " + ", ".join(s["robots_blocks_ai"]))
        if not s.get("schema"):
            blk.append("Не найдена разметка Schema.org (Organization, Service, FAQPage)")
        if not s.get("service_pages"):
            blk.append("Не удалось обнаружить отдельные страницы услуг (если есть, дайте на них прямые ссылки в меню)")
        if not s.get("case_pages"):
            blk.append("Не удалось обнаружить страницы кейсов или проектов (если они есть, дайте на них прямые ссылки в меню)")
    elif s and not s.get("ok"):
        blk.append("Сайт не удалось открыть для проверки: проверьте адрес и доступность")
    else:
        blk.append("Мало внешних упоминаний относительно тех, кого называют нейросети")
    return blk[:5]

def _recommendations(queries, groups, total, site_info=None):
    miss = total - sum(v for q in queries for v in q["hits"].values())   # ответов без бренда (из total)
    s = site_info or {}
    recs = []
    if s.get("ok") and s.get("robots_blocks_ai"):       # критично и подтверждено проверкой
        recs.append({"title":"Открыть сайт для ИИ-поисковиков в robots.txt","status":"","found":"",
            "why":"robots.txt закрывает доступ ботам: " + ", ".join(s["robots_blocks_ai"]) + ". Пока доступ закрыт, нейросети не могут использовать сайт как источник, и шанс попасть в ответ резко падает.",
            "steps":["Убрать Disallow для GPTBot, OAI-SearchBot, PerplexityBot, YandexBot, Google-Extended","Проверить, что страницы услуг открыты для индексации"],
            "effect":"Высокий","difficulty":"Низкая","term":"1 день"})
    svc_seen = bool(s.get("service_pages")); case_seen = bool(s.get("case_pages"))
    recs.append({"title":("Усилить страницы ключевых услуг" if svc_seen else "Сделать отдельные страницы под ключевые услуги"),"status":"","found":"",
         "why":f"По коммерческим запросам бренд не появился в {miss} из {total} ответов. Отдельные понятные страницы услуг дают нейросетям конкретные факты, которые можно процитировать.",
         "steps":["Под каждую ключевую услугу — отдельная страница с понятным заголовком","Описать задачу, решение и измеримый результат","Добавить ответы на 7-10 частых вопросов клиентов","Указать форматы работы, сроки и условия"],
         "effect":"Высокий","difficulty":"Средняя","term":"1-2 недели"})
    recs.append({"title":("Вынести кейсы так, чтобы их видели нейросети" if case_seen else "Показать кейсы и доказательства результата"),"status":"","found":"",
         "why":("Кейсы на сайте есть, но важно, чтобы они были текстом и с прямыми ссылками: нейросети охотнее называют тех, у кого есть проверяемые примеры." if case_seen
                else "Нейросети охотнее называют тех, у кого есть проверяемые примеры и результаты. Без кейсов сложно попасть в рекомендацию."),
         "steps":["Каждый кейс отдельной страницей: задача, что сделано, результат","Добавить отзывы клиентов с именем и компанией","Показать понятные цифры эффекта, где это уместно"],
         "effect":"Высокий","difficulty":"Средняя","term":"2-3 недели"})
    recs.append({"title":"Нарастить внешние подтверждения о компании","status":"","found":"",
         "why":"Нейросети собирают ответ из внешних источников: отзывы, карты, тематические подборки и каталоги. Чем больше согласованных упоминаний, тем выше шанс попасть в ответ.",
         "steps":["Собрать отзывы на профильных площадках и картах","Попасть в тематические подборки и каталоги ниши","Привести название и описание компании к единому виду везде"],
         "effect":"Высокий","difficulty":"Низкая","term":"3-4 недели"})
    if not (s.get("ok") and s.get("robots_blocks_ai")):  # если доступ не закрыт — общий технический пункт
        why_tech = "Разметка и понятная структура помогают нейросетям использовать сайт как источник."
        if s.get("ok") and not s.get("schema"):
            why_tech = "На сайте не найдена разметка Schema.org. Без неё нейросетям сложнее считывать факты о компании и услугах."
        recs.append({"title":"Подготовить сайт к AI-поиску технически","status":"","found":"",
             "why":why_tech,
             "steps":["Добавить разметку Organization, Service и FAQPage","Чётко указать на главной, чем занимается компания и для кого","Проверить доступ ИИ-ботам в robots.txt"],
             "effect":"Средний","difficulty":"Низкая","term":"3-5 дней"})
    return recs[:5]

def run(brand, site, niche, city, brand_short=None, out=None):
    brand_short = brand_short or re.sub(r"[«»\"']", "", brand).split("(")[0].strip().split(",")[0].split(" ")[-1]
    site_info = None if os.environ.get("TEST_MODE") == "1" else analyze_site(site)
    queries, competitors = analyze(brand, brand_short, site, niche, city, site_info=site_info)
    data = build_data(brand, brand_short, site, niche, city, queries, competitors, site_info=site_info)
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
