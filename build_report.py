# -*- coding: utf-8 -*-
"""
Генератор PDF-отчёта «Видимость бренда в нейросетях» v2.
Все цифры считаются из одной матрицы запрос×движок (значения 0/1/2 = в скольких
из 2 прогонов бренд упомянут). Бэкенд позже отдаёт сюда тот же JSON из живых ответов.
Запуск демо:  python3 build_report.py
"""
import os, re, json, math, datetime, html

def plural(n, one, few, many):
    n = abs(int(n)); d = n % 10; dd = n % 100
    if d == 1 and dd != 11: return one
    if 2 <= d <= 4 and not (12 <= dd <= 14): return few
    return many

_HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.environ.get("FONTS_DIR") or _HERE   # шрифты и ассеты лежат рядом со скриптом (без подпапок)
ASSETS = os.environ.get("ASSETS_DIR") or _HERE
TG_URL = "https://t.me/annakurbatovaai"
RUNS = 2  # прогонов на каждый запрос

# ── фирменные цвета (контраст серого повышен) ──────────────────────────────
INK="#1C1813"; MUTED="#5E564A"; FAINT="#8A8073"; ACCENT="#DE4A2C"; ACCENTD="#BE3A20"
PAGE="#FFFFFF"; CARD="#FFFFFF"; BORDER="#E6DFD3"; TRACK="#EFE9E0"; DARK="#141210"
CREAM="#F7ECE8"; GREEN="#2E8B57"; AMBER="#C9791A"; RED="#C13525"

def lvl(p):
    if p>=55: return GREEN
    if p>=40: return "#6F9A2E"
    if p>=25: return AMBER
    return RED
def esc(s): return html.escape(str(s))

def _join(names):
    """Человеческое перечисление: ['Премиум','Цена'] -> 'Премиум и Цена'."""
    names = [str(n) for n in names if n]
    if not names: return ""
    if len(names) == 1: return names[0]
    return ", ".join(names[:-1]) + " и " + names[-1]

def _join_groups(names):
    """Названия групп берём в кавычки и разделяем запятой (внутри названий бывает «и»)."""
    names = [str(n) for n in names if n]
    return ", ".join("«" + n + "»" for n in names)

def _matrix_verdict(d):
    """Вывод под матрицей: строится из данных, без зашитых формулировок."""
    if d.get('overall', 0) == 0:
        return ("Бренд не появился ни в одном из проверенных ответов: все ячейки 0/2. "
                "Это отправная точка, дальше задача — получить первые повторяемые упоминания.")
    rep = _join_groups(d.get('rep_groups', []))
    zero_g = _join_groups(d.get('groups_zero', []))
    if rep:
        s = f"Повторяемые упоминания (2 из 2) есть по группам: {rep}."
    else:
        s = "Повторяемого упоминания (2 из 2) пока нет: бренд появлялся максимум в одной из двух проверок."
    if zero_g:
        s += f" По группам {zero_g} упоминаний не обнаружено."
    return s

# ── расчёт всех чисел из матрицы ────────────────────────────────────────────
def compute(d):
    eng=d['engines']; qs=d['queries']
    for e in eng:
        m=sum(q['hits'].get(e['id'],0) for q in qs)
        e['mentions']=m; e['answers']=len(qs)*RUNS
        e['rate']=round(m/e['answers']*100)
    tot_m=sum(e['mentions'] for e in eng); tot_a=sum(e['answers'] for e in eng)
    d['overall']=round(tot_m/tot_a*100)
    d['total_mentions']=tot_m; d['total_answers']=tot_a
    es=sorted(eng,key=lambda e:-e['rate'])
    d['best']=es[0]; d['worst']=es[-1]
    # стабильность: запросы, где бренд в 2/2 хотя бы у одного движка, и доля 2/2 ячеек
    cells=[q['hits'].get(e['id'],0) for q in qs for e in eng]
    d['stable_cells']=sum(1 for c in cells if c==2)
    d['partial_cells']=sum(1 for c in cells if c==1)
    d['zero_cells']=sum(1 for c in cells if c==0)
    d['stable_q']=sum(1 for q in qs if any(q['hits'].get(e['id'],0)==2 for e in eng))
    # группы
    g={}
    for q in qs:
        gg=g.setdefault(q['group'],{'m':0,'mx':0,'n':0})
        gg['m']+=sum(q['hits'].get(e['id'],0) for e in eng); gg['mx']+=len(eng)*RUNS; gg['n']+=1
    d['groups']=sorted([{'name':k,'rate':round(v['m']/v['mx']*100),'n':v['n']} for k,v in g.items()],
                       key=lambda x:-x['rate'])
    # опорные / слабые каналы и сегменты — для выводов в тексте (без зашитых формулировок)
    d['strong_engines']=[e['name'] for e in es[:2]]
    d['weak_engines']=[e['name'] for e in es[::-1][:2]]
    gs=d['groups']
    d['top_groups']=[x['name'] for x in gs[:2]]
    low=[x['name'] for x in gs if x['rate']<=15]
    d['weak_groups']=low[:2] if low else [x['name'] for x in gs[::-1][:2]]
    d['prio_groups']=[x['name'] for x in sorted(gs, key=lambda x:-x['n'])[:3]]   # самые ёмкие группы (по числу запросов)
    d['zero']=d['overall']==0
    # факты по движкам и группам (для честного ненулевого сценария)
    d['mentioned_engines']=[e['name'] for e in eng if e['rate']>0]
    d['zero_engines']=[e['name'] for e in eng if e['rate']==0]
    d.setdefault('groups_zero',[x['name'] for x in gs if x['rate']==0])
    d['rep_groups']=sorted({q['group'] for q in qs if any(q['hits'].get(e['id'],0)==2 for e in eng)})
    for c in d['competitors']: c['gap']=c['rate']-d['overall']
    return d

# ── элементы ───────────────────────────────────────────────────────────────
def ring(pct, size=188, sw=17, color="#FFFFFF", track="rgba(255,255,255,.16)", tc="#FFFFFF"):
    r=(size-sw)/2; c=2*math.pi*r; dash=c*pct/100; cx=size/2
    return f'''<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">
      <circle cx="{cx}" cy="{cx}" r="{r}" fill="none" stroke="{track}" stroke-width="{sw}"/>
      <circle cx="{cx}" cy="{cx}" r="{r}" fill="none" stroke="{color}" stroke-width="{sw}"
        stroke-linecap="round" stroke-dasharray="{dash:.1f} {c:.1f}" transform="rotate(-90 {cx} {cx})"/>
      <text x="50%" y="50%" text-anchor="middle" dy=".34em" font-size="{size*0.28:.0f}"
        font-weight="800" fill="{tc}">{pct}<tspan font-size="{size*0.14:.0f}">%</tspan></text>
    </svg>'''

def bar(label, pct, sub="", wl="150px", color=None, val=None, you=False):
    col=color or lvl(pct); v=f"{pct}%" if val is None else val
    cls="bar-l you" if you else "bar-l"
    return f'''<div class="bar"><div class="bar-row">
      <div class="{cls}" style="width:{wl}">{esc(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:{max(pct,2)}%;background:{col}"></div></div>
      <div class="bar-v" style="color:{col}">{v}</div></div>
      {f'<div class="bar-sub" style="padding-left:calc({wl} + 10px)">{esc(sub)}</div>' if sub else ''}</div>'''

ST={'проверено':'c','обнаружено':'f','не обнаружено':'a','частично':'p','не удалось определить автоматически':'u','не удалось определить':'u'}
def status(t): return f'<span class="st st--{ST.get(t,"u")}">{esc(t)}</span>'

def metrics(eff,dif,term):
    return f'''<table class="mt"><tr>
      <th>Потенциальный эффект</th><th>Сложность</th><th>Срок</th></tr><tr>
      <td>{esc(eff)}</td><td>{esc(dif)}</td><td>{esc(term)}</td></tr></table>'''

# ── CSS ────────────────────────────────────────────────────────────────────
def css():
    return f'''
@font-face{{font-family:'Gilroy';src:url('file://{FONTS}/Gilroy-Regular.ttf');font-weight:400}}
@font-face{{font-family:'Gilroy';src:url('file://{FONTS}/Gilroy-Medium.ttf');font-weight:500}}
@font-face{{font-family:'Gilroy';src:url('file://{FONTS}/Gilroy-Semibold.ttf');font-weight:600}}
@font-face{{font-family:'Gilroy';src:url('file://{FONTS}/Gilroy-Bold.ttf');font-weight:700}}
@page{{size:A4;margin:0}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Gilroy',sans-serif;color:{INK}}}
.page{{width:210mm;height:297mm;padding:16mm 15mm 13mm;position:relative;overflow:hidden;page-break-after:always;background:{PAGE}}}
.page:last-child{{page-break-after:auto}}
.page--dark{{background:#0e0b09 url('file://{ASSETS}/cover-bg.jpg') center/cover no-repeat;color:#fff;padding:0;text-shadow:0 1px 3px rgba(0,0,0,.7)}}
.kicker{{font-size:8.5pt;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:{FAINT};margin-bottom:4mm}}
h1{{font-size:30pt;font-weight:700;line-height:1.06;letter-spacing:-.5px}}
h2{{font-size:17pt;font-weight:700;letter-spacing:-.3px;margin-bottom:2mm}}
h2 .num{{color:{ACCENT};margin-right:7px}}
.sec-intro{{font-size:10pt;color:{MUTED};line-height:1.5;margin-bottom:5mm;max-width:165mm}}
/* cover */
.cover-top{{display:flex;justify-content:space-between;align-items:center;padding:13mm 15mm 0}}
.brand{{font-size:13pt;font-weight:700}} .brand .dot{{color:{ACCENT}}}
.eyebrow{{display:inline-block;font-size:8.5pt;font-weight:600;letter-spacing:2.2px;text-transform:uppercase;
  color:#fff;border:1px solid rgba(255,255,255,.28);border-radius:40px;padding:6px 13px}}
.cover-mid{{padding:9mm 15mm 0;display:flex;gap:11mm;align-items:center}}
.cover-meta{{margin-top:8mm;font-size:10.5pt;line-height:1.8;color:rgba(255,255,255,.92)}} .cover-meta b{{color:#fff;font-weight:700}}
.cover-head{{margin-top:7mm;font-size:14pt;font-weight:700;color:#fff}} .cover-head span{{color:{ACCENT}}}
.cover-sub{{margin-top:3mm;font-size:10.5pt;line-height:1.55;color:rgba(255,255,255,.96);max-width:120mm}}
.cover-foot{{position:absolute;left:15mm;right:15mm;bottom:13mm;display:flex;justify-content:space-between;
  font-size:8.5pt;color:rgba(255,255,255,.7);border-top:1px solid rgba(255,255,255,.2);padding-top:5mm}}
/* bars */
.bar{{margin:0 0 14px}} .bar-row{{display:flex;align-items:center;gap:10px;font-size:10.5pt}}
.bar-l{{flex:none;font-weight:600}} .bar-l.you{{color:{ACCENTD}}}
.bar-track{{flex:1;height:16px;background:{TRACK};border-radius:9px;overflow:hidden}}
.bar-fill{{height:16px;border-radius:9px}} .bar-v{{flex:none;width:64px;text-align:right;font-weight:700}}
.bar-sub{{font-size:8.5pt;color:{MUTED};margin-top:2px}}
/* cards / stats */
.card{{background:{CARD};border:1px solid {BORDER};border-radius:13px;padding:6mm;box-shadow:0 1px 3px rgba(20,16,12,.05)}}
.grid3{{display:flex;gap:5mm}} .grid3>*{{flex:1}}
.stat{{background:{CARD};border:1px solid {BORDER};border-radius:12px;padding:4.5mm;box-shadow:0 1px 3px rgba(20,16,12,.05)}}
.stat .n{{font-size:19pt;font-weight:700;color:{ACCENT};line-height:1}} .stat .l{{font-size:9pt;color:{MUTED};margin-top:2mm;line-height:1.4}}
.box{{border:1px solid {BORDER};border-radius:12px;padding:5mm;margin-top:4mm;background:{CARD};box-shadow:0 1px 3px rgba(20,16,12,.05)}}
.box.cream{{background:{CREAM};border:none;box-shadow:none}}
.box h4{{font-size:10pt;font-weight:700;color:{ACCENTD};margin-bottom:2mm;text-transform:uppercase;letter-spacing:.6px}}
.box p{{font-size:10pt;color:{INK};line-height:1.5}}
.two{{display:flex;gap:5mm}} .two>*{{flex:1}}
/* matrix */
table.mx{{width:100%;border-collapse:collapse;font-size:9.5pt}}
table.mx th,table.mx td{{padding:9.5px 5px;border-bottom:1px solid {BORDER}}}
table.mx th{{font-size:8pt;text-transform:uppercase;letter-spacing:.4px;color:{FAINT};font-weight:600}}
table.mx td.q{{text-align:left;font-weight:500;color:{INK};font-size:9.5pt}}
table.mx th.q{{text-align:left}}
.mx .c{{text-align:center;width:38px;font-weight:700;font-size:9pt}}
.c2{{color:{GREEN}}} .c1{{color:{AMBER}}} .c0{{color:#CBC2B3}}
.grp{{font-size:8pt;color:{FAINT}}}
/* статусы */
.st{{display:inline-block;font-size:7.5pt;font-weight:700;text-transform:uppercase;letter-spacing:.4px;
  padding:3px 8px;border-radius:20px;margin-left:6px;vertical-align:middle}}
.st--c{{background:rgba(46,139,87,.13);color:{GREEN}}} .st--f{{background:rgba(201,121,26,.14);color:{AMBER}}}
.st--a{{background:rgba(193,53,37,.12);color:{RED}}} .st--p{{background:rgba(201,121,26,.14);color:{AMBER}}}
.st--u{{background:rgba(94,86,74,.12);color:{MUTED}}}
/* examples */
.ex{{border:1px solid {BORDER};border-radius:12px;padding:6mm;margin-bottom:4.5mm;background:{CARD};box-shadow:0 1px 3px rgba(20,16,12,.05)}}
.ex .q{{font-size:11pt;font-weight:700;margin-bottom:2.5mm}}
.ex .r{{font-size:10pt;color:{MUTED};line-height:1.65}} .ex .r b{{color:{INK}}}
.ex .tag{{display:inline-block;font-size:7.5pt;font-weight:700;text-transform:uppercase;letter-spacing:.5px;padding:3px 9px;border-radius:20px;margin-bottom:3mm}}
.tag-no{{background:rgba(193,53,37,.12);color:{RED}}} .tag-yes{{background:rgba(46,139,87,.13);color:{GREEN}}} .tag-mid{{background:rgba(201,121,26,.14);color:{AMBER}}}
/* рекомендации */
.rcard{{border:1px solid {BORDER};border-radius:13px;padding:5.5mm;margin-bottom:4.5mm;background:{CARD};box-shadow:0 1px 3px rgba(20,16,12,.05)}}
.rcard-h{{display:flex;align-items:center;gap:4mm;margin-bottom:3mm}}
.rcard-n{{flex:none;width:28px;height:28px;border-radius:50%;background:{ACCENT};color:#fff;font-weight:700;font-size:11pt;text-align:center;line-height:28px}}
.rcard-h h3{{font-size:12pt;font-weight:700}}
.rlabel{{font-size:8pt;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:{FAINT};margin:3mm 0 1.5mm}}
.rcard p{{font-size:9.5pt;color:{INK};line-height:1.5}}
.rsteps{{margin:1mm 0 0 5mm}} .rsteps li{{font-size:9.5pt;color:{INK};line-height:1.55;margin-bottom:1mm}}
table.mt{{width:100%;border-collapse:collapse;margin-top:4mm}}
table.mt th{{font-size:7.5pt;text-transform:uppercase;letter-spacing:.4px;color:{FAINT};font-weight:600;text-align:left;padding:4px 6px;border-bottom:1px solid {BORDER}}}
table.mt td{{font-size:9.5pt;font-weight:700;color:{INK};padding:5px 6px}}
/* списки */
.ck{{list-style:none}} .ck li{{font-size:10pt;color:{INK};line-height:1.5;padding:3.5mm 0;border-bottom:1px solid {BORDER};display:flex;gap:9px}}
.ck li:last-child{{border-bottom:none}} .ck .m{{flex:none;font-weight:700}}
.mk-y{{color:{GREEN}}} .mk-n{{color:{RED}}}
/* недели */
.week{{margin-bottom:4mm}} .week .wh{{font-size:10pt;font-weight:700;color:{ACCENTD};margin-bottom:1.5mm}}
.week ul{{margin-left:5mm}} .week li{{font-size:9.5pt;color:{INK};line-height:1.5}}
/* footer / cta */
.foot{{position:absolute;left:15mm;right:15mm;bottom:11mm;display:flex;justify-content:space-between;
  font-size:8pt;color:{FAINT};border-top:1px solid {BORDER};padding-top:4mm}}
.cta{{background:{DARK};color:#fff;border-radius:15px;padding:7mm;margin-top:5mm}}
.cta h3{{font-size:14pt;font-weight:700;margin-bottom:2.5mm}}
.cta p{{font-size:9.5pt;color:rgba(255,255,255,.72);line-height:1.55;margin-bottom:4mm}}
.cta .btn{{display:inline-block;background:{ACCENT};color:#fff;font-weight:700;font-size:10pt;padding:10px 20px;border-radius:30px}}
.cta-row{{display:flex;gap:7mm;align-items:center}} .cta-tx{{flex:1}}
.cta-qr{{flex:none;text-decoration:none;text-align:center}}
.cta-qr img{{width:27mm;height:27mm;background:#fff;border-radius:9px;padding:2.5mm}}
.cta-qr span{{display:block;font-size:8.5pt;color:rgba(255,255,255,.72);margin-top:2mm;font-weight:600}}
.note{{font-size:9pt;color:{MUTED};line-height:1.55}}
/* автор и контакты */
.author{{display:flex;gap:8mm;align-items:center;margin-top:5mm;padding-bottom:7mm;border-bottom:1px solid {BORDER}}}
.author-ph{{width:34mm;height:34mm;border-radius:50%;flex:none}}
.author-name{{font-size:16pt;font-weight:700;margin-bottom:2.5mm}} .author-name span{{color:{ACCENT}}}
.author-tx p{{font-size:10.5pt;color:{MUTED};line-height:1.6}}
.contacts{{display:flex;justify-content:space-between;align-items:center;margin-top:8mm;gap:8mm}}
.contacts-l .ct{{font-size:11pt;color:{INK};margin-bottom:4mm}}
.contacts-l .ct b{{color:{FAINT};font-weight:600;font-size:8.5pt;text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:1mm}}
.qr{{text-decoration:none;text-align:center;flex:none}}
.qr img{{width:33mm;height:33mm;border:1px solid {BORDER};border-radius:11px;padding:3mm;background:#fff}}
.qr span{{display:block;font-size:9pt;color:{ACCENTD};margin-top:2.5mm;font-weight:700}}
'''

def footer(d): return f'<div class="foot"><span>Отчёт о видимости в нейросетях · {esc(d["brand"])}</span><span>Анна Курбатова°</span></div>'

# ── страницы ───────────────────────────────────────────────────────────────
def p_cover(d):
    return f'''<div class="page page--dark">
      <div class="cover-top"><div class="brand">Анна Курбатова<span class="dot">°</span></div>
        <div class="eyebrow">AI-видимость · GEO-аналитика</div></div>
      <div class="cover-mid"><div style="flex:1">
        <div style="font-size:9pt;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,.85);margin-bottom:5mm">Отчёт о видимости бренда в нейросетях</div>
        <h1>{esc(d['brand'])}</h1>
        <div class="cover-meta">Сайт: <b>{esc(d['site'])}</b><br>Ниша: <b>{esc(d['niche'])}</b> · {esc(d['city'])}<br>
          Движков: <b>{len(d['engines'])}</b> · запросов: <b>{len(d['queries'])}</b> · проверок: <b>{d['total_answers']}</b> · дата: <b>{esc(d['date'])}</b></div>
        <div class="cover-head">Бренд появился в <span>{d['overall']}%</span> проверок</div>
        <div class="cover-sub">{esc(d['cover_sub'])}</div>
      </div><div style="flex:none">{ring(d['overall'])}</div></div>
      <div class="cover-foot"><span>Внешний руководитель цифрового развития и AI-внедрения</span><span>annakurbatova.ru</span></div>
    </div>'''

def p_summary(d):
    rm=d['result_meaning']
    loss_h="Главная потеря" if not d.get('zero') else "Где бренда нет"
    strong_h="Сильная зона" if not d.get('zero') else "С чего начинать"
    if d.get('zero'):
        stat3_n="0%"; stat3_l="разницы между каналами нет: упоминаний не найдено нигде"
    else:
        stat3_n=esc(d['best']['name']); stat3_l=f"лучший канал ({d['best']['rate']}%) · слабее всего {esc(d['worst']['name'])} ({d['worst']['rate']}%)"
    return f'''<div class="page"><h2><span class="num">01</span>Что означает результат</h2>
      <div class="sec-intro">Короткий вывод по итогам {d['total_answers']} ответов: где бренд уже виден, где теряется и какая ближайшая цель.</div>
      <div class="card"><div style="font-size:13pt;font-weight:700;margin-bottom:2mm">{d['overall']}%: {esc(rm['headline'])}</div>
        <p style="font-size:10.5pt;color:{INK};line-height:1.55">{esc(rm['text'])}</p></div>
      <div class="two" style="margin-top:4mm">
        <div class="box"><h4>{loss_h}</h4><p>{esc(rm['loss'])}</p></div>
        <div class="box"><h4>{strong_h}</h4><p>{esc(rm['strong'])}</p></div></div>
      <div class="box cream"><h4>Ближайшая цель</h4><p>{esc(rm['goal'])}</p></div>
      <div class="grid3" style="margin-top:5mm">
        <div class="stat"><div class="n">{d['overall']}%</div><div class="l">средняя видимость по {len(d['engines'])} {plural(len(d['engines']),'нейросети','нейросетям','нейросетям')}</div></div>
        <div class="stat"><div class="n">{d['stable_q']} из {len(d['queries'])}</div><div class="l">запросов с повторяемым упоминанием (2/2 хотя бы в одной сети)</div></div>
        <div class="stat"><div class="n">{stat3_n}</div><div class="l">{stat3_l}</div></div></div>
      <div class="box"><h4>Что дальше в отчёте</h4><p>Видимость по каждой нейросети, матрица повторяемости (2/2, 1/2, 0/2), разбор по группам запросов, примеры реальных ответов, что работает и что мешает, и персональный план действий с приоритетами и сроками.</p></div>
      {footer(d)}</div>'''

def p_engines(d):
    bars="".join(bar(e['name'], e['rate'], f"{e['mentions']} упоминаний в {e['answers']} ответах · {esc(e['note'])}", wl="150px") for e in d['engines'])
    me=d.get('mentioned_engines',[]); ze=d.get('zero_engines',[]); ans=d['engines'][0]['answers']
    if not me:
        h1,strong_p="Опорные каналы","Пока ни одна сеть не называет бренд. Рост начинается с источников, на которые ссылаются нейросети: отзывы, карты и тематические каталоги."
        h2,weak_p="Где начинать работу",f"{_join(ze)} не знают бренд по этим запросам. С них и начнём набирать упоминания."
    else:
        mlist="; ".join(f"{esc(e['name'])} — {e['mentions']} из {e['answers']}" for e in d['engines'] if e['rate']>0)
        h1,strong_p="Опорные каналы",f"Упоминания обнаружены в: {mlist}."
        if ze:
            h2,weak_p="Каналы без упоминаний",f"В {_join(ze)} бренд не появился ни в одном из {ans} ответов."
        else:
            h2,weak_p="Где наращивать","Упоминания есть во всех каналах, но видимость пока низкая. Задача — повышать долю ответов с упоминанием."
    return f'''<div class="page"><h2><span class="num">02</span>Где вас находят нейросети</h2>
      <div class="sec-intro">Каждой нейросети задано {len(d['queries'])} коммерческих запросов вашей ниши по {RUNS} прогона ({d['engines'][0]['answers']} ответов на движок). Процент: доля ответов, где упомянут бренд или сайт.</div>
      <div class="card">{bars}</div>
      <div class="two">
        <div class="box cream"><h4>{h1}</h4><p>{strong_p}</p></div>
        <div class="box"><h4>{h2}</h4><p>{weak_p}</p></div>
      </div>
      <div class="box"><h4>Как читать</h4><p>Процент: доля из {d['engines'][0]['answers']} ответов, где нейросеть упомянула бренд или сайт. Чем выше, тем чаще вас видит клиент, который спрашивает совета у ИИ.</p></div>
      {footer(d)}</div>'''

def p_matrix(d):
    eng=d['engines']
    head="".join(f'<th class="c">{esc(e["short"])}</th>' for e in eng)
    rows=""
    for q in d['queries']:
        cells=""
        for e in eng:
            v=q['hits'].get(e['id'],0); cells+=f'<td class="c c{v}">{v}/{RUNS}</td>'
        rows+=f'<tr><td class="q">{esc(q["q"])}<div class="grp">{esc(q["group"])}</div></td>{cells}</tr>'
    legend=" · ".join(f'{esc(e["short"])}: {esc(e["name"])}' for e in eng)
    return f'''<div class="page"><h2><span class="num">03</span>В каких ответах бренд появляется, а в каких нет</h2>
      <div class="sec-intro">Каждый запрос проверен по {RUNS} раза. 2/{RUNS}: упоминание повторилось в обеих проверках. 1/{RUNS}: в одной из двух. 0/{RUNS}: не появился.</div>
      <table class="mx"><thead><tr><th class="q">Запрос</th>{head}</tr></thead><tbody>{rows}</tbody></table>
      <div class="two" style="margin-top:5mm">
        <div class="box"><h4>Повторяемость упоминаний</h4><p>Повторилось (2/{RUNS}): <b>{d['stable_cells']}</b> · в одной из двух (1/{RUNS}): <b>{d['partial_cells']}</b> · не обнаружено (0/{RUNS}): <b>{d['zero_cells']}</b> из {len(d['queries'])*len(eng)} ячеек.</p></div>
        <div class="box"><h4>Вывод</h4><p>{_matrix_verdict(d)}</p></div></div>
      <div class="note" style="margin-top:4mm">{legend}</div>
      {footer(d)}</div>'''

def p_groups(d):
    bars="".join(bar(g['name'], g['rate'], f"{g['n']} {plural(g['n'],'запрос','запроса','запросов')} в группе", wl="190px") for g in d['groups'])
    if d.get('zero'):
        loss_p=f"Бренд не появляется ни по одной группе запросов (везде 0%). В этой проверке больше всего запросов пришлось на группы: {_join_groups(d['prio_groups'])}."
        lean_p="Опереться на текущую видимость пока нельзя: её нет ни по одной группе. Точка входа: внешние упоминания (отзывы, карты, каталоги) и понятные страницы под ключевые услуги."
        prio_p="Ранжировать направления по важности на таком объёме проверки нельзя. Двигаться стоит сразу по двум линиям: понятные страницы услуг на сайте и внешние упоминания, на которые опираются нейросети."
    else:
        rep=_join_groups(d.get('rep_groups',[])); zg=_join_groups(d.get('groups_zero',[]))
        loss_p=(f"Упоминаний пока нет по группам: {zg}. " if zg else "По большинству групп упоминаний мало. ") + "Эти запросы относятся к этапу выбора поставщика."
        lean_p=(f"Повторяемые упоминания есть по группам: {rep}. " if rep else "Повторяемых упоминаний пока мало. ") + "На них можно опереться, но видимость всё ещё низкая."
        prio_p="Двигаться стоит по двум линиям: усилить материалы под группы без упоминаний и закрепить то, что уже сработало. Группы с одним запросом не стоит напрямую сравнивать с группами из нескольких запросов."
    return f'''<div class="page"><h2><span class="num">04</span>Видимость по группам запросов</h2>
      <div class="sec-intro">Те же запросы, сгруппированные по направлениям. Видно, в каких сегментах вас находят, а в каких нет.</div>
      <div class="card">{bars}</div>
      <div class="two" style="margin-top:4mm">
        <div class="box cream"><h4>Главные потери</h4><p>{loss_p}</p></div>
        <div class="box"><h4>На что опереться</h4><p>{lean_p}</p></div></div>
      <div class="box"><h4>Приоритет по сегментам</h4><p>{prio_p}</p></div>
      {footer(d)}</div>'''

def p_examples(d):
    cards=""
    for ex in d['examples']:
        tag={'yes':('tag-yes','Бренд появился'),'no':('tag-no','Бренда нет'),'mid':('tag-mid','В одном из двух')}[ex['kind']]
        named=", ".join(ex['named']) if ex['named'] else "не называл конкретные компании"
        cards+=f'''<div class="ex"><span class="tag {tag[0]}">{tag[1]}</span>
          <div class="q">{esc(ex['query'])}</div>
          <div class="r"><b>{esc(ex['engine'])} назвал:</b> {esc(named)}<br>
          <b>«{esc(d['brand_short'])}»:</b> {esc(ex['result'])}<br>
          <b>Почему:</b> {esc(ex['why'])}</div></div>'''
    n_ex=len(d['examples'])
    intro=("Пример ответа из проверки: кого называет нейросеть и появился ли ваш бренд." if n_ex==1
           else "Несколько ответов из проверки: кого называет нейросеть, появился ли ваш бренд и какая возможная причина.")
    if d.get('zero'):
        takeaway="Бренд не появился ни в одном из примеров: на эти запросы нейросеть называет другие компании или общие варианты. Чтобы попасть в ответ, нужны материалы и упоминания именно по этим запросам."
    else:
        takeaway="Повторяемые упоминания обнаружены по части запросов. По остальным бренд не появился; точные причины требуют отдельного анализа страниц сайта, внешних публикаций и источников, использованных нейросетью."
    return f'''<div class="page"><h2><span class="num">05</span>Примеры реальных ответов нейросетей</h2>
      <div class="sec-intro">{intro}</div>
      {cards}
      <div class="box cream"><h4>Что показывают примеры</h4><p>{takeaway}</p></div>
      {'<div class="box"><h4>Конкуренты</h4><p>В ответах не удалось надёжно определить конкретные компании, которых нейросети рекомендуют вместо бренда: по этим запросам они дают в основном общие советы и категории. Поэтому раздел «Кого называют вместо вас» в этот отчёт не вошёл.</p></div>' if not d.get('competitors') else ''}
      <div class="note">Приведены короткие фрагменты ответов на дату проверки. Причины отсутствия бренда указаны как возможные, а не доказанные.</div>
      {footer(d)}</div>'''

def _gap_phrase(gap):
    if gap > 0: return f"опережает вас на {gap} п.п."
    if gap < 0: return f"на {abs(gap)} п.п. ниже вас"
    return "наравне с вами"

def p_competitors(d):
    N=d['total_answers']; bc=d.get('total_mentions',0); ov=d['overall']
    rows=[(d['brand_short'], ov, bc, True)] + [(c['name'], c['rate'], c['count'], False) for c in d['competitors']]
    rows.sort(key=lambda r:-r[1])
    bars=""
    for name,rate,cnt,is_you in rows:
        sub=("ваша текущая видимость" if is_you else f"{_gap_phrase(rate-ov)} · упомянут в {cnt} из {N} ответов")
        bars+=bar((esc(name)+" (вы)") if is_you else name, rate, sub, wl="150px", color=(ACCENTD if is_you else FAINT), you=is_you)
    ev="".join(f'''<div class="ex"><div class="q">{esc(c['name'])} · {c['rate']}%</div>
          <div class="r">Упомянут в <b>{c['count']}</b> из <b>{c['total']}</b> ответов нейросетей по вашим запросам.</div></div>''' for c in d['competitors'])
    names_join=_join([c['name'] for c in d['competitors']])
    max_c=max((c['rate'] for c in d['competitors']), default=0)
    if ov>0 and ov>=max_c:
        lead=f"«{esc(d['brand_short'])}» упомянут в {bc} из {N} ответов — чаще найденных компаний"
        if d.get('mentioned_engines'): lead+=f", но только в {_join(d['mentioned_engines'])}"
        takeaway=f"Помимо «{esc(d['brand_short'])}», в ответах встречались {names_join}. {lead}. Это хороший знак: бренд уже попадает в выдачу."
    else:
        takeaway=f"Помимо «{esc(d['brand_short'])}», в ответах встречались {names_join}. У части из них упоминаний больше. Как правило, помогает количество согласованных упоминаний в открытых источниках: сайт, отзывы, карты, подборки."
    return f'''<div class="page"><h2><span class="num">06</span>Какие компании ещё встречаются в ответах</h2>
      <div class="sec-intro">Компании, которые нейросети называли в ответах на ваши запросы. Они могут упоминаться и вместе с вами. Процент считается так же, как ваш: доля из {N} ответов, где встретилось название.</div>
      <div class="card">{bars}</div>
      <div style="margin-top:4mm">{ev}</div>
      <div class="box cream"><h4>Что это значит</h4><p>{takeaway}</p></div>
      {footer(d)}</div>'''

def p_sources(d):
    bars="".join(bar(s['name'], s['share'], "", wl="175px", color=ACCENT if i==0 else "#C98A72", val=f"{s['share']}%") for i,s in enumerate(d['sources']))
    return f'''<div class="page"><h2><span class="num">07</span>Что формирует ответы нейросетей о вас и конкурентах</h2>
      <div class="sec-intro">Распределение типов источников, на которые опирались нейросети в ответах по вашей нише. Подсказывает, где усиливать присутствие.</div>
      <div class="card">{bars}</div>
      <div class="box cream"><h4>Вывод</h4><p>Больше всего весят карты, справочники и отзывы, а не сам сайт. Значит, рост видимости быстрее всего дадут внешние подтверждения: отзывы, упоминания и каталоги, а не только доработка сайта.</p></div>
      <div class="two">
        <div class="box"><h4>Где вы уже есть</h4><p>Официальный сайт и часть отзывов. Это база, но её недостаточно для стабильного появления в ответах.</p></div>
        <div class="box"><h4>Где вас мало</h4><p>Карты, справочники, отраслевые каталоги и независимые подборки. Именно они весят больше всего, и здесь у вас пробел.</p></div>
      </div>
      {footer(d)}</div>'''

def _schema_human(types):
    t={str(x).lower() for x in (types or [])}
    org=bool(t & {"organization","localbusiness","corporation","professionalservice","hotel","lodgingbusiness","restaurant","store"})
    svc="service" in t; faq="faqpage" in t; prod="product" in t
    found=[n for f,n in [(org,"организация"),(svc,"услуги"),(prod,"товары"),(faq,"FAQ")] if f]
    miss=[n for f,n in [(org,"организация"),(svc or prod,"услуги/товары")] if not f]
    s=("есть разметка: "+", ".join(found)) if found else "значимая разметка не найдена"
    if miss: s+="; не найдена: "+", ".join(miss)
    return s

def _site_evidence(d):
    s=d.get('site_info') or {}
    if not s.get('ok'):
        if s and s.get('host'):
            return '<div class="box"><h4>Проверка сайта</h4><p>Сайт не удалось открыть автоматически для проверки. Проверьте адрес и доступность для ботов.</p></div>'
        return ''
    parts=[]
    if s.get('sitemap_urls'): parts.append(f"страниц в sitemap: {s['sitemap_urls']}")
    parts.append(f"страниц услуг найдено: {s.get('service_pages',0)}")
    parts.append(f"страниц кейсов/проектов: {s.get('case_pages',0)}")
    parts.append("Schema.org: "+_schema_human(s.get('schema')))
    if s.get('robots_found'):
        parts.append("robots.txt: "+("закрывает ИИ-ботов ("+", ".join(s['robots_blocks_ai'])+")" if s.get('robots_blocks_ai') else "доступ ИИ-ботам открыт"))
    pages=s.get('pages_list') or []
    pages_html=(f'<div class="note" style="margin-top:2mm">Просмотренные страницы (выборка): {esc(", ".join(pages[:12]))}</div>') if pages else ""
    return f'<div class="box"><h4>Проверка сайта (факты автопроверки)</h4><p>{" · ".join(esc(p) for p in parts)}</p>{pages_html}</div>'

def p_works(d):
    pos="".join(f'<li><span class="m mk-y">✓</span><span>{esc(x)}</span></li>' for x in d['positives'])
    blk="".join(f'<li><span class="m mk-n">!</span><span>{esc(x)}</span></li>' for x in d['blockers'])
    return f'''<div class="page"><h2><span class="num">08</span>Что уже работает и что мешает росту</h2>
      <div class="sec-intro">На что можно опереться и что ограничивает видимость прямо сейчас.</div>
      {_site_evidence(d)}
      <div class="box"><h4 style="color:{GREEN}">Что уже помогает вашей видимости</h4><ul class="ck">{pos}</ul></div>
      <div class="box"><h4 style="color:{RED}">Что мешает росту</h4><ul class="ck">{blk}</ul></div>
      {footer(d)}</div>'''

def p_reco(d, items, n0, title_extra=""):
    cards=""
    for i,r in enumerate(items, n0):
        steps="".join(f'<li>{esc(s)}</li>' for s in r['steps'])
        cards+=f'''<div class="rcard"><div class="rcard-h"><span class="rcard-n">{i}</span><h3>{esc(r['title'])}</h3></div>
          <div class="rlabel">Почему это важно</div><p>{esc(r['why'])}</p>
          <div class="rlabel">Что сделать</div><ul class="rsteps">{steps}</ul>
          {metrics(r['effect'],r['difficulty'],r['term'])}</div>'''
    head=f'<h2><span class="num">09</span>Что усиливает AI-видимость{title_extra}</h2>' if n0==1 else f'<h2>Что усиливает AI-видимость{title_extra}</h2>'
    intro='<div class="sec-intro">Базовые шаги, которые повышают шанс попасть в ответы нейросетей. Это общие рекомендации по нише, а не выводы о конкретных страницах вашего сайта: точечный разбор сайта добавим отдельно.</div>' if n0==1 else ''
    return f'''<div class="page">{head}{intro}{cards}{footer(d)}</div>'''

def p_plan(d):
    weeks="".join(f'<div class="week"><div class="wh">{esc(w["week"])}</div><ul>{"".join(f"<li>{esc(i)}</li>" for i in w["items"])}</ul></div>' for w in d['plan30'])
    return f'''<div class="page"><h2><span class="num">10</span>Что делать по неделям</h2>
      <div class="sec-intro">Последовательность действий на месяц. В конце повторный замер, чтобы увидеть рост в цифрах.</div>
      <div class="card">{weeks}</div>
      <div class="box"><h4>Методология и ограничения</h4><p class="note" style="color:{MUTED}">{esc(d['method_note'])}</p></div>
      {footer(d)}</div>'''

def p_author(d):
    return f'''<div class="page">
      <div class="author" style="margin-top:6mm">
        <img class="author-ph" src="file://{ASSETS}/avatar.png">
        <div class="author-tx">
          <div class="author-name">Анна Курбатова<span>°</span></div>
          <p>Создатель сервиса AI-видимости, AI-консультант и бренд-маркетолог. Проектирую и внедряю цифровые системы и AI-решения для бизнеса: сайты, внутренние сервисы, автоматизацию и аналитические инструменты.</p>
        </div>
      </div>
      <div class="cta" style="margin-top:6mm"><h3>Усилим основу для появления бренда в ответах нейросетей</h3>
        <p>Реализация рекомендаций может включать доработку текущего сайта, разработку нового сайта с нуля или создание отдельного цифрового решения под задачи компании. На консультации определим приоритеты, объём работ и подходящий формат реализации.</p>
        <span class="btn">Записаться на консультацию →</span></div>
      <div class="contacts">
        <div class="contacts-l">
          <div class="ct"><b>Telegram-канал</b>@annakurbatovaai</div>
          <div class="ct"><b>WhatsApp</b>+7 985 194-48-26</div>
          <div class="ct"><b>Сайт</b>annakurbatova.ru</div>
        </div>
        <a class="qr" href="{TG_URL}"><img src="file://{ASSETS}/qr_tg.png"><span>Открыть Telegram-канал</span></a>
      </div>
      {footer(d)}</div>'''

def build(data, out):
    d=compute(data)
    recs=d['recommendations']
    pages=[p_cover(d), p_summary(d), p_engines(d), p_matrix(d), p_groups(d), p_examples(d)]
    if d.get('competitors'):                  # блок конкурентов только если есть подтверждённые (>=2)
        pages.append(p_competitors(d))
    pages += [p_works(d), p_reco(d, recs[:2], 1), p_reco(d, recs[2:], 3), p_plan(d), p_author(d)]
    body="".join(pages)
    # секции перенумеровываются последовательно (часть страниц может быть скрыта)
    cnt=[0]
    def _renum(m):
        cnt[0]+=1; return f'<span class="num">{cnt[0]:02d}</span>'
    body=re.sub(r'<span class="num">\d+</span>', _renum, body)
    doc=f'<!doctype html><html><head><meta charset="utf-8"><style>{css()}</style></head><body>{body}</body></html>'
    from weasyprint import HTML
    HTML(string=doc).write_pdf(out)
    return out

# ── демо-данные: мебель на заказ. Матрица hits = в скольких из 2 прогонов упомянут ──
def H(p,g,c,ds,gm,gc,y): return {"perplexity":p,"chatgpt":g,"claude":c,"deepseek":ds,"gemini":gm,"gigachat":gc,"yandex":y}
SAMPLE={
 "brand":"Мебельная мастерская «Дубрава»","brand_short":"Дубрава",
 "site":"dubrava-mebel.ru","niche":"мебель на заказ","city":"Москва","date":"22 июня 2026",
 "cover_sub":"Основные потери: запросы о премиальной мебели, изделиях из массива и собственном производстве. Сильная зона: кухни и встроенная мебель в Москве.",
 "engines":[
   {"id":"perplexity","name":"Perplexity","short":"Pp","note":"ищет в интернете, цитирует источники"},
   {"id":"chatgpt","name":"ChatGPT","short":"GPT","note":"режим веб-поиска"},
   {"id":"claude","name":"Claude","short":"Cl","note":"веб-поиск, осторожен в рекомендациях"},
   {"id":"deepseek","name":"DeepSeek","short":"DS","note":"отвечает в основном из обучения"},
   {"id":"gemini","name":"Gemini","short":"Gm","note":"поиск Google в основе"},
   {"id":"gigachat","name":"GigaChat","short":"GC","note":"российский, слабое присутствие бренда"},
   {"id":"yandex","name":"Яндекс Нейро","short":"Я","note":"опирается на Яндекс.Карты и отзывы"},
 ],
 "queries":[
   {"q":"Где заказать кухню на заказ в Москве?","group":"Кухни на заказ","hits":H(2,2,2,2,1,1,0)},
   {"q":"Кухни на заказ с собственным производством","group":"Кухни на заказ","hits":H(2,2,1,1,1,1,0)},
   {"q":"Шкаф-купе по индивидуальным размерам, к кому обратиться","group":"Шкафы и гардеробные","hits":H(1,1,2,1,1,1,0)},
   {"q":"Гардеробная на заказ в Москве","group":"Шкафы и гардеробные","hits":H(1,1,1,1,1,0,1)},
   {"q":"Встроенная мебель в новостройку на заказ","group":"Встроенная мебель","hits":H(2,2,1,1,1,1,0)},
   {"q":"Мебель на заказ для квартиры в новостройке","group":"Встроенная мебель","hits":H(2,1,1,0,0,1,1)},
   {"q":"Мебель из массива дерева на заказ, отзывы","group":"Мебель из массива","hits":H(1,0,1,0,0,0,0)},
   {"q":"Надёжная мастерская мебели на заказ с гарантией","group":"Надёжность и гарантия","hits":H(1,1,0,1,1,0,0)},
   {"q":"Дизайнерская мебель на заказ премиум-класса","group":"Премиальная мебель","hits":H(0,0,0,0,0,0,0)},
   {"q":"Эксклюзивная премиум-мебель на заказ в Москве","group":"Премиальная мебель","hits":H(0,0,0,0,0,0,0)},
 ],
 "result_meaning":{
   "headline":"средняя видимость",
   "text":"Бренд уже известен отдельным AI-сервисам, но присутствие нестабильно: в одной генерации компания появляется, в следующей исчезает. Стабильно вас находят лишь по нескольким запросам о кухнях и встроенной мебели.",
   "loss":"Запросы о премиальной и дизайнерской мебели, изделиях из массива и собственном производстве. В этих сегментах бренд почти не появляется.",
   "strong":"Общие запросы о кухнях и встроенной мебели в Москве. Здесь уже есть базовая узнаваемость, на неё можно опереться.",
   "goal":"Поднять не только общий процент, но и число запросов со стабильным появлением: чтобы бренд возникал в обоих ответах из двух, а не через раз.",
 },
 "examples":[
   {"kind":"yes","query":"Где заказать кухню на заказ в Москве?","engine":"ChatGPT","named":["Мебель-Сити","Дубрава","ЛорентМебель"],
    "result":"появилась в обоих ответах (2 из 2)","why":"есть страница о кухнях и отзывы по этому направлению, нейросеть использует их как подтверждение."},
   {"kind":"no","query":"Дизайнерская мебель на заказ премиум-класса","engine":"ChatGPT","named":["Мебель-Сити","ЛорентМебель","Гранд-Мебель"],
    "result":"не появилась (0 из 2)","why":"не обнаружено отдельной страницы и кейсов по премиум-сегменту, мало внешних подтверждений по этим запросам."},
   {"kind":"mid","query":"Мебель из массива дерева на заказ, отзывы","engine":"Perplexity","named":["Гранд-Мебель","Дубрава"],
    "result":"появилась в одном ответе из двух (нестабильно)","why":"упоминания есть, но их мало и они не закреплены отзывами именно по этому запросу."},
 ],
 "competitors":[
   {"name":"Мебель-Сити","rate":70,"focus":"кухни, индивидуальные размеры","sources":"Яндекс.Карты, сайт, отраслевые подборки","reviews":4,"materials":12},
   {"name":"ЛорентМебель","rate":55,"focus":"гардеробные, встроенная мебель","sources":"блог, каталоги, отзывы","reviews":3,"materials":9},
   {"name":"Гранд-Мебель","rate":48,"focus":"кухни, надёжность и гарантия","sources":"структурированные данные, FAQ, карты","reviews":3,"materials":7},
 ],
 "sources":[
   {"name":"Карты и справочники","share":26},{"name":"Отзывы на площадках","share":24},
   {"name":"Официальный сайт","share":22},{"name":"Каталоги и подборки","share":14},
   {"name":"СМИ и блоги","share":9},{"name":"Соцсети","share":5},
 ],
 "positives":[
   "Сайт доступен для поисковых ИИ-ботов (проверено)",
   "Бренд появляется в Perplexity и ChatGPT (60% и 50% проверок)",
   "Указаны город и специализация, нейросети верно относят вас к нише",
   "Есть упоминания по коммерческим запросам о кухнях",
   "Кухни и встроенная мебель уже имеют базовую узнаваемость",
 ],
 "blockers":[
   "Не обнаружено отдельных страниц под приоритетные услуги (кухни, гардеробные, встроенная мебель)",
   "Премиальный сегмент без посадочных страниц и кейсов: видимость 0%",
   "Мало внешних отзывов и упоминаний относительно конкурентов",
   "Нестабильные упоминания: часто бренд появляется в одном ответе из двух",
   "Часть данных о производстве и гарантиях не удалось определить автоматически",
 ],
 "recommendations":[
   {"title":"Усилить страницы коммерческих услуг","status":"обнаружено",
    "found":"На сайте есть общая информация о мебели на заказ, но не обнаружено отдельных страниц под кухни, гардеробные и встроенную мебель.",
    "why":"По нишевым и премиальным запросам бренд не появился в 14 из 20 проверок. Отдельные страницы дают нейросетям конкретные факты, которые можно процитировать.",
    "steps":["Создать страницы «Кухни на заказ», «Гардеробные», «Встроенная мебель»","Добавить примеры проектов, сроки, материалы и гарантию","Разместить ответы на 7-10 частых вопросов","Добавить данные о собственном производстве"],
    "effect":"Высокий","difficulty":"Средняя","term":"5-7 дней"},
   {"title":"Закрепить присутствие в премиум-сегменте","status":"не обнаружено",
    "found":"Не обнаружено страниц и кейсов по дизайнерской и премиальной мебели. В премиум-запросах видимость 0%.",
    "why":"Это самый маржинальный сегмент, и именно там вас не находят. Конкуренты показывают по нему реализованные проекты.",
    "steps":["Сделать страницу «Дизайнерская мебель премиум»","Показать 3-5 проектов с фото, материалами и бюджетом","Описать технологии и премиальные материалы","Добавить отзывы клиентов премиум-сегмента"],
    "effect":"Высокий","difficulty":"Средняя","term":"7-10 дней"},
   {"title":"Нарастить внешние подтверждения","status":"обнаружено",
    "found":"Обнаружено ограниченное число отзывов и упоминаний на внешних площадках по сравнению с конкурентами (у лидера 4 источника отзывов).",
    "why":"Источники ответов нейросетей наполовину состоят из карт и отзывов. Такие упоминания увеличивают количество доступных нейросетям подтверждений о компании.",
    "steps":["Собрать свежие отзывы на Яндекс.Картах и профильных площадках","Попасть в отраслевые подборки «лучшие мастерские мебели»","Привести данные о компании к единому виду на всех площадках"],
    "effect":"Высокий","difficulty":"Низкая","term":"2-3 недели"},
   {"title":"Открыть и описать сайт для ИИ-поиска","status":"частично",
    "found":"Доступ для поисковых ИИ-ботов открыт, но часть данных о производстве и гарантиях не удалось определить автоматически.",
    "why":"Если доступ ограничен или данные не структурированы, нейросетям сложнее использовать страницы сайта как прямой источник.",
    "steps":["Проверить robots.txt для OAI-SearchBot, PerplexityBot, YandexBot","Добавить разметку Organization, Product и FAQ","Явно указать гарантии, сроки и собственное производство"],
    "effect":"Средний","difficulty":"Низкая","term":"2-3 дня"},
 ],
 "plan30":[
   {"week":"Неделя 1 · фундамент","items":["Проверить доступность сайта для ИИ-ботов и скорректировать robots.txt","Добавить сведения о компании, гарантиях и производстве","Внедрить разметку Organization"]},
   {"week":"Неделя 2 · приоритетные услуги","items":["Создать страницы кухонь, гардеробных и встроенной мебели","Добавить FAQ и разметку Product/FAQ на эти страницы"]},
   {"week":"Неделя 3 · доказательства","items":["Разместить 3-5 кейсов с фото, сроками и материалами","Начать сбор отзывов на выбранных площадках"]},
   {"week":"Неделя 4 · внешнее присутствие и замер","items":["Добавить упоминания в отраслевых подборках и каталогах","Провести повторный замер видимости и сравнить с этим отчётом"]},
 ],
 "method_note":"Каждой из 7 нейросетей задано 10 коммерческих запросов вашей ниши по 2 прогона (всего 140 проверок). В ответах искали упоминание бренда, домена и конкурентов. Где движок умеет искать в интернете, использовался режим веб-поиска, он отражает то, что реально увидит клиент. Видимость считается как доля ответов с упоминанием. Выводы основаны на том, что удалось наблюдать в ответах на дату проверки; результаты нейросетей могут меняться, поэтому замер стоит повторять. Где данные нельзя подтвердить автоматически, используется статус «не удалось определить».",
}

if __name__=="__main__":
    import argparse
    ap=argparse.ArgumentParser(description="Сборка PDF-отчёта из данных движка")
    ap.add_argument("--data", help="JSON-файл с данными отчёта (из engine.py). Без него собирается демо.")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__),"Отчёт-AI-видимость-пример.pdf"))
    a=ap.parse_args()
    data=SAMPLE
    if a.data:
        with open(a.data,encoding="utf-8") as f: data=json.load(f)
    build(data, a.out)
    print("PDF:", a.out, os.path.getsize(a.out)//1024, "KB")
