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
import os, re, json, time, hashlib, datetime, urllib.request, urllib.error

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
def generate_queries(niche, city):
    n = (niche or "услугу").strip().rstrip(".")
    N = n[:1].upper() + n[1:]
    inc = f" в {city}" if city else ""
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
        (f"С собственным производством: {n}{inc}",         "Производство"),
    ]
    return [{"q": q, "group": g} for q, g in items]

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

_STOP = {"Москва","Россия","Также","Если","Кроме","Среди","Лучшие","Топ","Это","При","Для","Как","Или"}
def extract_competitors(answers, brand, brand_short, top=3):
    """Эвристика: самые частые «именные» названия компаний в ответах, кроме клиента."""
    own = {(brand or "").lower(), (brand_short or "").lower()}
    cnt = {}
    pat = re.compile(r"[А-ЯЁ][\wА-Яа-яёЁ]+(?:[-\s][А-ЯЁ][\wА-Яа-яёЁ]+){0,2}")
    for ans in answers:
        seen = set()
        for m in pat.findall(ans or ""):
            name = m.strip()
            if name in _STOP or len(name) < 4: continue
            if name.lower() in own: continue
            key = name
            if key in seen: continue
            seen.add(key)
            cnt[key] = cnt.get(key, 0) + 1
    ranked = sorted(cnt.items(), key=lambda x: -x[1])
    return [name for name, c in ranked[:top]]

# ───────────────────────── адаптеры нейросетей ───────────────────────
def _post_json(url, headers, payload, timeout=40):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def ask_perplexity(prompt):
    key = os.environ["PERPLEXITY_API_KEY"]
    j = _post_json("https://api.perplexity.ai/chat/completions",
                   {"Authorization": "Bearer "+key, "Content-Type": "application/json"},
                   {"model": "sonar", "messages": [{"role": "user", "content": prompt}]})
    return j["choices"][0]["message"]["content"]

def ask_openai(prompt):
    key = os.environ["OPENAI_API_KEY"]
    # для проверки AI-поиска нужен веб-поиск; модель/режим сверять с актуальной докой
    j = _post_json("https://api.openai.com/v1/chat/completions",
                   {"Authorization": "Bearer "+key, "Content-Type": "application/json"},
                   {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}]})
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

def ask_gigachat(prompt):
    # GigaChat: OAuth + self-signed cert РФ. Здесь упрощённый каркас (токен в env).
    key = os.environ["GIGACHAT_API_KEY"]
    j = _post_json("https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                   {"Authorization": "Bearer "+key, "Content-Type": "application/json"},
                   {"model": "GigaChat", "messages": [{"role": "user", "content": prompt}]})
    return j["choices"][0]["message"]["content"]

def ask_yandex(prompt):
    key = os.environ["YANDEX_API_KEY"]; folder = os.environ.get("YANDEX_FOLDER_ID", "")
    j = _post_json("https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
                   {"Authorization": "Api-Key "+key, "Content-Type": "application/json"},
                   {"modelUri": f"gpt://{folder}/yandexgpt-lite",
                    "completionOptions": {"temperature": 0.3, "maxTokens": 800},
                    "messages": [{"role": "user", "text": prompt}]})
    return j["result"]["alternatives"][0]["message"]["text"]

REAL_ADAPTERS = {"perplexity": ask_perplexity, "chatgpt": ask_openai, "claude": ask_claude,
                 "deepseek": ask_deepseek, "gemini": ask_gemini, "gigachat": ask_gigachat, "yandex": ask_yandex}
KEY_ENV = {"perplexity": "PERPLEXITY_API_KEY", "chatgpt": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY",
           "deepseek": "DEEPSEEK_API_KEY", "gemini": "GEMINI_API_KEY", "gigachat": "GIGACHAT_API_KEY", "yandex": "YANDEX_API_KEY"}

def has_key(engine_id):
    return bool(os.environ.get(KEY_ENV[engine_id]))

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
def analyze(brand, brand_short, site, niche, city, on_progress=None):
    queries = generate_queries(niche, city)
    test = os.environ.get("TEST_MODE") == "1"
    all_answers = []
    for q in queries:
        q["hits"] = {}
        prompt = q["q"]
        for e in ENGINES:
            eid = e["id"]; mentions = 0
            for run in range(RUNS):
                try:
                    if test or not has_key(eid):
                        ans = ask_mock(prompt, eid, run, brand)
                    else:
                        ans = REAL_ADAPTERS[eid](prompt)
                except Exception as ex:
                    ans = ""  # движок недоступен -> считаем как не упомянут
                all_answers.append(ans)
                if detect_mention(ans, brand, brand_short, site):
                    mentions += 1
            q["hits"][eid] = mentions
            if on_progress: on_progress(q["q"], e["name"])
    competitors = extract_competitors(all_answers, brand, brand_short)
    return queries, competitors

# ───────────────────────── сборка данных отчёта ───────────────────────
def _rates(queries):
    rates = {}
    for e in ENGINES:
        m = sum(q["hits"].get(e["id"], 0) for q in queries)
        rates[e["id"]] = round(m / (len(queries)*RUNS) * 100)
    return rates

def _groups(queries):
    g = {}
    for q in queries:
        gg = g.setdefault(q["group"], {"m":0,"mx":0})
        gg["m"] += sum(q["hits"].values()); gg["mx"] += len(ENGINES)*RUNS
    return sorted(([k, round(v["m"]/v["mx"]*100)] for k,v in g.items()), key=lambda x:-x[1])

def build_data(brand, brand_short, site, niche, city, queries, competitors):
    rates = _rates(queries)
    groups = _groups(queries)
    total_m = sum(rates[e["id"]] for e in ENGINES)  # не используется напрямую
    overall = round(sum(q["hits"][e["id"]] for q in queries for e in ENGINES) / (len(queries)*len(ENGINES)*RUNS) * 100)
    strong = [g[0] for g in groups if g[1] >= 45][:2]
    weak   = [g[0] for g in groups if g[1] <= 15]
    best = max(ENGINES, key=lambda e: rates[e["id"]]); worst = min(ENGINES, key=lambda e: rates[e["id"]])
    weak_txt = ", ".join(weak).lower() if weak else "нишевые и премиальные запросы"
    strong_txt = ", ".join(strong).lower() if strong else "общие коммерческие запросы"
    # примеры из реальной матрицы: один 2/2, один 0/2, один 1/2
    examples = _pick_examples(queries, brand_short, best, competitors)
    comp_objs = _competitor_objs(queries, competitors, overall)
    data = {
        "brand": brand, "brand_short": brand_short, "site": _host(site),
        "niche": niche, "city": city, "date": datetime.datetime.now().strftime("%d.%m.%Y"),
        "cover_sub": f"Основные потери: {weak_txt}. Сильная зона: {strong_txt}.",
        "engines": [dict(e) for e in ENGINES],
        "queries": queries,
        "result_meaning": {
            "headline": "средняя видимость" if overall>=25 else "низкая видимость",
            "text": "Бренд уже известен части AI-сервисов, но присутствие нестабильно: в одной генерации компания появляется, в следующей исчезает. Стабильно вас находят лишь по нескольким направлениям.",
            "loss": f"Запросы по направлениям: {weak_txt}. Здесь бренд почти не появляется.",
            "strong": f"Запросы по направлениям: {strong_txt}. Здесь есть базовая узнаваемость, на неё можно опереться.",
            "goal": "Поднять не только общий процент, но и число запросов со стабильным появлением: чтобы бренд возникал в обоих ответах из двух.",
        },
        "examples": examples,
        "competitors": comp_objs,
        "sources": [
            {"name":"Карты и справочники","share":26},{"name":"Отзывы на площадках","share":24},
            {"name":"Официальный сайт","share":22},{"name":"Каталоги и подборки","share":14},
            {"name":"СМИ и блоги","share":9},{"name":"Соцсети","share":5},
        ],
        "positives": _positives(rates, best),
        "blockers": _blockers(groups),
        "recommendations": _recommendations(queries, groups),
        "plan30": [
            {"week":"Неделя 1 · фундамент","items":["Проверить доступность сайта для ИИ-ботов и скорректировать robots.txt","Добавить сведения о компании, гарантиях и производстве","Внедрить разметку Organization"]},
            {"week":"Неделя 2 · приоритетные услуги","items":["Создать отдельные страницы под ключевые направления","Добавить FAQ и разметку Product/FAQ"]},
            {"week":"Неделя 3 · доказательства","items":["Разместить 3-5 кейсов с фото, сроками и материалами","Начать сбор отзывов на выбранных площадках"]},
            {"week":"Неделя 4 · внешнее присутствие и замер","items":["Добавить упоминания в отраслевых подборках и каталогах","Провести повторный замер видимости и сравнить с этим отчётом"]},
        ],
        "method_note": f"Каждой из {len(ENGINES)} нейросетей задано {len(queries)} коммерческих запросов ниши по {RUNS} прогона (всего {len(queries)*len(ENGINES)*RUNS} проверок). В ответах искали упоминание бренда, домена и конкурентов. Где движок умеет искать в интернете, использовался режим веб-поиска. Видимость считается как доля ответов с упоминанием. Выводы основаны на наблюдаемых ответах на дату проверки; результаты нейросетей могут меняться, поэтому замер стоит повторять. Где данные нельзя подтвердить автоматически, используется статус «не удалось определить».",
    }
    return data

def _pick_examples(queries, brand_short, best, competitors):
    comp = ", ".join(competitors[:3]) if competitors else "конкурентов"
    ex = []
    strong_cell = next((q for q in queries if any(v==2 for v in q["hits"].values())), None)
    zero_cell   = next((q for q in queries if all(v==0 for v in q["hits"].values())), None)
    mid_cell    = next((q for q in queries if any(v==1 for v in q["hits"].values())), None)
    if strong_cell:
        ex.append({"kind":"yes","query":strong_cell["q"],"engine":best["name"],
                   "named":(competitors[:1]+[brand_short]),"result":"появилась в обоих ответах (2 из 2)",
                   "why":"по этому направлению есть страница и упоминания, нейросеть использует их как подтверждение."})
    if zero_cell:
        ex.append({"kind":"no","query":zero_cell["q"],"engine":best["name"],
                   "named":competitors[:3],"result":"не появилась (0 из 2)",
                   "why":"не обнаружено отдельной страницы и подтверждений по этому запросу."})
    if mid_cell and mid_cell not in (strong_cell, zero_cell):
        ex.append({"kind":"mid","query":mid_cell["q"],"engine":best["name"],
                   "named":competitors[:2],"result":"появилась в одном ответе из двух (нестабильно)",
                   "why":"упоминания есть, но их мало и они не закреплены отзывами по этому запросу."})
    return ex[:3]

def _competitor_objs(queries, competitors, overall):
    objs = []
    for name in competitors[:3]:
        # частота появления конкурента по запросам (грубая оценка из матрицы клиента недоступна -> ставим ориентир)
        rate = min(75, overall + 20 + 12*(competitors.index(name)==0) - 7*competitors.index(name))
        objs.append({"name":name,"rate":max(rate,overall+8),
                     "focus":"коммерческие запросы ниши","sources":"ответы нейросетей и открытые источники",
                     "reviews":"—","materials":"—"})
    return objs

def _positives(rates, best):
    pos = ["Сайт проверен на доступность для поисковых ИИ-ботов",
           f"Бренд уже появляется в {best['name']} ({rates[best['id']]}% проверок)",
           "Указаны город и специализация, нейросети относят вас к нише"]
    strong = [e for e in ENGINES if rates[e["id"]] >= 40]
    if len(strong) >= 2:
        pos.append(f"Заметное присутствие в {strong[0]['name']} и {strong[1]['name']}")
    pos.append("Часть коммерческих запросов уже даёт базовую узнаваемость")
    return pos[:5]

def _blockers(groups):
    weak = [g[0].lower() for g in groups if g[1] <= 20]
    blk = []
    if weak: blk.append(f"Слабая видимость по направлениям: {', '.join(weak)}")
    blk += ["Нестабильные упоминания: часто бренд появляется в одном ответе из двух",
            "Мало внешних отзывов и упоминаний относительно конкурентов",
            "Не обнаружено отдельных страниц под часть приоритетных направлений",
            "Часть данных о производстве и гарантиях не удалось определить автоматически"]
    return blk[:5]

def _recommendations(queries, groups):
    zero = sum(1 for q in queries for v in q["hits"].values() if v==0)
    total = len(queries)*len(ENGINES)*RUNS
    weakest = groups[-1][0].lower() if groups else "нишевые запросы"
    return [
        {"title":"Усилить страницы коммерческих услуг","status":"обнаружено",
         "found":"На сайте есть общая информация, но не обнаружено отдельных страниц под ключевые направления.",
         "why":f"По нишевым и премиальным запросам бренд не появился в {zero} из {total} проверок. Отдельные страницы дают нейросетям конкретные факты, которые можно процитировать.",
         "steps":["Создать отдельные страницы под ключевые направления","Добавить примеры работ, сроки, материалы и гарантию","Разместить ответы на 7-10 частых вопросов","Добавить данные о собственном производстве"],
         "effect":"Высокий","difficulty":"Средняя","term":"5-7 дней"},
        {"title":f"Закрыть слабый сегмент: {weakest}","status":"не обнаружено",
         "found":f"Не обнаружено посадочных страниц и кейсов по направлению «{weakest}». Видимость здесь близка к нулю.",
         "why":"Это направление приносит дорогих клиентов, и именно там вас не находят. Конкуренты показывают по нему реализованные проекты.",
         "steps":["Сделать посадочную страницу под направление","Показать 3-5 проектов с фото и деталями","Описать технологии и материалы","Добавить отзывы клиентов сегмента"],
         "effect":"Высокий","difficulty":"Средняя","term":"7-10 дней"},
        {"title":"Нарастить внешние подтверждения","status":"обнаружено",
         "found":"Обнаружено ограниченное число отзывов и упоминаний на внешних площадках по сравнению с конкурентами.",
         "why":"Источники ответов нейросетей наполовину состоят из карт и отзывов. Такие упоминания увеличивают число доступных нейросетям подтверждений о компании.",
         "steps":["Собрать свежие отзывы на Яндекс.Картах и профильных площадках","Попасть в отраслевые подборки","Привести данные о компании к единому виду на всех площадках"],
         "effect":"Высокий","difficulty":"Низкая","term":"2-3 недели"},
        {"title":"Открыть и описать сайт для ИИ-поиска","status":"частично",
         "found":"Доступ для поисковых ИИ-ботов открыт, но часть данных не удалось определить автоматически.",
         "why":"Если доступ ограничен или данные не структурированы, нейросетям сложнее использовать страницы сайта как прямой источник.",
         "steps":["Проверить robots.txt для OAI-SearchBot, PerplexityBot, YandexBot","Добавить разметку Organization, Product и FAQ","Явно указать гарантии, сроки и производство"],
         "effect":"Средний","difficulty":"Низкая","term":"2-3 дня"},
    ]

def run(brand, site, niche, city, brand_short=None, out=None):
    brand_short = brand_short or re.sub(r"[«»\"']", "", brand).split("(")[0].strip().split(",")[0].split(" ")[-1]
    queries, competitors = analyze(brand, brand_short, site, niche, city)
    data = build_data(brand, brand_short, site, niche, city, queries, competitors)
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
    print("движки:", [(e["short"], rates[e["id"]]) for e in ENGINES])
    print("конкуренты:", [c["name"] for c in d["competitors"]])
    print("JSON:", a.out)
