# -*- coding: utf-8 -*-
"""
Генератор PDF-отчёта «Видимость бренда в нейросетях» v2.
Все цифры считаются из одной матрицы запрос×движок (значения 0/1/2 = в скольких
из 2 прогонов бренд упомянут). Бэкенд позже отдаёт сюда тот же JSON из живых ответов.
Запуск демо:  python3 build_report.py
"""
import os, re, json, math, datetime, html
from urllib.parse import quote

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

def _aneg(d):
    """«не появился/не появилась» с учётом пола личного бренда."""
    return "не появилась" if d.get("fem") else "не появился"

def _matrix_verdict(d):
    """Вывод под матрицей: строится из данных, без зашитых формулировок."""
    if d.get('overall', 0) == 0:
        return (f"{esc(d['brand_short'])} {_aneg(d)} ни в одном из {d['total_answers']} ответов. Следующий шаг — определить "
                "страницы сайта, которые должны отвечать на эти вопросы, и проверить, ясно ли на них описаны услуги, опыт и специализация.")
    if d.get('rep_groups'):
        s = "Повторяемое упоминание (2 из 2) уже есть по части вопросов: бренд появляется в обоих ответах на один и тот же вопрос."
    else:
        s = "Повторяемого упоминания (2 из 2) пока нет: бренд появлялся максимум в одной из двух проверок."
    return s

# ── расчёт всех чисел из матрицы ────────────────────────────────────────────
def _normalize_engines(d):
    """Дополняет данные до полных 7 нейросетей — для матрицы и старых отчётов с урезанным списком."""
    from engine import ENGINES
    by_id = {e["id"]: e for e in (d.get("engines") or [])}
    d["engines"] = [by_id[e["id"]] if e["id"] in by_id else {**e, "failed": True} for e in ENGINES]
    for q in d.get("queries") or []:
        hits = q.setdefault("hits", {})
        for e in ENGINES:
            hits.setdefault(e["id"], 0)
        ev = q.setdefault("evidence", {})
        for e in ENGINES:
            ev.setdefault(e["id"], {"brand": False, "comps": [], "others": []})

def compute(d):
    _normalize_engines(d)
    eng=d['engines']; qs=d['queries']
    for e in eng:
        m=sum(q['hits'].get(e['id'],0) for q in qs)
        e['mentions']=m; e['answers']=len(qs)*RUNS
        e['rate']=None if e.get('failed') or e.get('no_key') else round(m/e['answers']*100)
    work=[e for e in eng if not e.get('failed') and not e.get('no_key')] or eng     # рабочие движки
    tot_m=sum(e['mentions'] for e in work); tot_a=sum(e['answers'] for e in work) or 1
    d['overall']=round(tot_m/tot_a*100)
    d['level']="высокая" if d['overall']>=60 else "средняя" if d['overall']>=25 else "низкая"   # единый уровень видимости
    d['total_mentions']=tot_m; d['total_answers']=tot_a
    es=sorted(work,key=lambda e:-(e['rate'] or 0))
    d['best']=es[0]; d['worst']=es[-1]
    # стабильность: только по рабочим движкам
    cells=[q['hits'].get(e['id'],0) for q in qs for e in work]
    d['stable_cells']=sum(1 for c in cells if c==2)
    d['partial_cells']=sum(1 for c in cells if c==1)
    d['zero_cells']=sum(1 for c in cells if c==0)
    d['stable_q']=sum(1 for q in qs if any(q['hits'].get(e['id'],0)==2 for e in work))
    # группы (доли — по рабочим движкам)
    g={}
    for q in qs:
        gg=g.setdefault(q['group'],{'m':0,'mx':0,'n':0})
        gg['m']+=sum(q['hits'].get(e['id'],0) for e in work); gg['mx']+=len(work)*RUNS; gg['n']+=1
    d['groups']=sorted([{'name':k,'rate':round(v['m']/v['mx']*100),'n':v['n'],'m':v['m'],'mx':v['mx']} for k,v in g.items()],
                       key=lambda x:-x['rate'])
    d['strong_engines']=[e['name'] for e in es[:2]]
    d['weak_engines']=[e['name'] for e in es[::-1][:2]]
    gs=d['groups']
    d['top_groups']=[x['name'] for x in gs[:2]]
    low=[x['name'] for x in gs if x['rate']<=15]
    d['weak_groups']=low[:2] if low else [x['name'] for x in gs[::-1][:2]]
    d['prio_groups']=[x['name'] for x in sorted(gs, key=lambda x:-x['n'])[:3]]
    d['zero']=d['overall']==0
    d['mentioned_engines']=[e['name'] for e in work if (e['rate'] or 0)>0]
    d['zero_engines']=[e['name'] for e in work if e['rate']==0]
    d.setdefault('groups_zero',[x['name'] for x in gs if x['rate']==0])
    d['rep_groups']=sorted({q['group'] for q in qs if any(q['hits'].get(e['id'],0)==2 for e in work)})
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
.ex,.fa,.rcard,.qd{{break-inside:avoid}}
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
/* наглядная диаграмма по нейросетям (блок 02) */
.ec{{margin:1mm 0 2mm}}
.ec-scale{{display:flex;justify-content:space-between;font-size:7.5pt;color:{FAINT};margin:0 13mm 1.5mm 34mm}}
.ec-row{{display:flex;align-items:center;gap:3mm;margin-bottom:1mm}}
.ec-name{{flex:none;width:31mm;font-size:10.5pt;font-weight:700;color:{INK}}}
.ec-track{{flex:1;height:8.5mm;background:{TRACK};border-radius:5px;overflow:hidden}}
.ec-fill{{height:8.5mm;border-radius:5px;min-width:3px}}
.ec-pct{{flex:none;width:13mm;text-align:right;font-size:13pt;font-weight:800}}
.ec-sub{{font-size:8.5pt;color:{MUTED};margin:0 0 3mm 34mm}}
.dn-row{{display:flex;justify-content:space-around;align-items:flex-start;gap:6mm;padding:1mm 4mm 3mm;border-bottom:1px solid {BORDER};margin-bottom:3.5mm}}
.dn{{display:flex;flex-direction:column;align-items:center;gap:1.5mm}}
.dn-name{{font-size:9pt;font-weight:600;color:{INK};text-align:center;line-height:1.2}}
/* cards / stats */
.card{{background:{CARD};border:1px solid {BORDER};border-radius:13px;padding:6mm;box-shadow:0 1px 3px rgba(20,16,12,.05)}}
.grid3{{display:flex;gap:5mm}} .grid3>*{{flex:1}}
.stat{{background:{CARD};border:1px solid {BORDER};border-radius:12px;padding:4.5mm;box-shadow:0 1px 3px rgba(20,16,12,.05)}}
.stat .n{{font-size:19pt;font-weight:700;color:{ACCENT};line-height:1}} .stat .l{{font-size:9pt;color:{MUTED};margin-top:2mm;line-height:1.4}}
.box{{border:1px solid {BORDER};border-radius:12px;padding:5mm;margin-top:4mm;background:{CARD};box-shadow:0 1px 3px rgba(20,16,12,.05);break-inside:avoid}}
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
.ex{{border:1px solid {BORDER};border-radius:12px;padding:6mm;margin-bottom:4.5mm;background:{CARD};box-shadow:0 1px 3px rgba(20,16,12,.05);break-inside:avoid}}
.ex .q{{font-size:11pt;font-weight:700;margin-bottom:2.5mm}}
.ex .r{{font-size:10pt;color:{MUTED};line-height:1.65}} .ex .r b{{color:{INK}}}
.ex .tag{{display:inline-block;font-size:7.5pt;font-weight:700;text-transform:uppercase;letter-spacing:.5px;padding:3px 9px;border-radius:20px;margin-bottom:3mm}}
.ex .quote{{background:#F7F2EA;border-left:3px solid {ACCENT};border-radius:8px;padding:3.5mm 4mm;margin:0 0 3.5mm}}
.ex .quote-h{{font-size:7.5pt;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:{FAINT};margin-bottom:1.5mm}}
.ex .quote-t{{font-size:9.5pt;font-style:italic;color:{INK};line-height:1.62}}
/* развёрнутые ответы */
.fa{{border:1px solid {BORDER};border-radius:11px;padding:5mm 5.5mm;margin-bottom:4mm;background:{CARD};box-shadow:0 1px 3px rgba(20,16,12,.05);break-inside:avoid}}
.fa-q{{font-size:10.5pt;font-weight:700;color:{INK};margin-bottom:2mm;line-height:1.35}}
.fa-h{{font-size:7.5pt;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:{FAINT};margin-bottom:2mm}}
.fa-t{{font-size:9.5pt;font-style:italic;color:{INK};line-height:1.6;margin-bottom:2.5mm}}
.fa-n{{font-size:9pt;color:{MUTED};line-height:1.5}}
.fa-yes{{color:{GREEN};font-weight:700}}
.tag-no{{background:rgba(193,53,37,.12);color:{RED}}} .tag-yes{{background:rgba(46,139,87,.13);color:{GREEN}}} .tag-mid{{background:rgba(201,121,26,.14);color:{AMBER}}}
/* рекомендации */
.rcard{{border:1px solid {BORDER};border-radius:13px;padding:5.5mm;margin-bottom:4.5mm;background:{CARD};box-shadow:0 1px 3px rgba(20,16,12,.05)}}
.rcard-h{{display:flex;align-items:flex-start;gap:4mm;margin-bottom:3mm}}
.rcard-n{{flex:none;width:28px;height:28px;border-radius:50%;background:{ACCENT};color:#fff;font-weight:700;font-size:11pt;text-align:center;line-height:28px}}
.rcard-h h3{{flex:1;min-width:0;font-size:12pt;font-weight:700;line-height:1.2;padding-top:2px}}
.rlabel{{font-size:8pt;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:{FAINT};margin:3mm 0 1.5mm}}
.rcard p{{font-size:9.5pt;color:{INK};line-height:1.5}}
.rsteps{{margin:1mm 0 0 5mm}} .rsteps li{{font-size:9.5pt;color:{INK};line-height:1.55;margin-bottom:1mm}}
table.mt{{width:100%;border-collapse:collapse;margin-top:4mm}}
table.mt th{{font-size:7.5pt;text-transform:uppercase;letter-spacing:.4px;color:{FAINT};font-weight:600;text-align:left;padding:4px 6px;border-bottom:1px solid {BORDER}}}
table.mt td{{font-size:9.5pt;font-weight:700;color:{INK};padding:5px 6px}}
/* списки */
.ck{{list-style:none}} .ck li{{font-size:9.5pt;color:{INK};line-height:1.4;padding:2.2mm 0;border-bottom:1px solid {BORDER};display:flex;gap:9px}}
.ck li:last-child{{border-bottom:none}} .ck .m{{flex:none;font-weight:700}}
.mk-y{{color:{GREEN}}} .mk-n{{color:{RED}}} .mk-w{{color:{AMBER}}}
/* недели */
.week{{padding-bottom:3.5mm;margin-bottom:3.5mm;border-bottom:1px solid {BORDER}}}
.week:last-child{{border-bottom:none;margin-bottom:0;padding-bottom:0}}
.week .wh{{font-size:11pt;font-weight:700;color:{INK};margin-bottom:3mm}}
.week ul{{margin-left:5mm}} .week li{{font-size:9.5pt;color:{INK};line-height:1.4;margin-bottom:.4mm}}
/* footer / cta */
.foot{{position:absolute;left:15mm;right:15mm;bottom:6mm;display:flex;justify-content:space-between;
  font-size:8pt;color:{FAINT};border-top:1px solid {BORDER};padding-top:3mm;background:{PAGE}}}
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
/* тип рекомендации */
.ktag{{display:inline-block;font-size:7.5pt;font-weight:700;text-transform:uppercase;letter-spacing:.5px;padding:3px 9px;border-radius:20px}}
.ktag-content{{background:rgba(46,139,87,.13);color:{GREEN}}}
.ktag-tech{{background:rgba(45,90,160,.12);color:#2D5AA0}}
.ktag-promo{{background:rgba(201,121,26,.15);color:{AMBER}}}
.rcard-h .ktag{{flex:none;white-space:nowrap;margin-top:3px}}
.rex{{background:{CREAM};border-radius:10px;padding:4mm;margin-top:1mm;font-size:9.5pt;color:{INK};line-height:1.55}} .rex b{{color:{ACCENTD}}}
.rex-note{{font-size:8pt;font-style:italic;color:{FAINT};line-height:1.4;margin:1.5mm 1mm 0}}
.rhand{{font-size:9pt;color:{MUTED};line-height:1.5;margin-top:3mm}} .rhand b{{color:{INK};font-weight:600}}
.rmeta{{display:flex;gap:8mm;margin-top:3mm;padding-top:3mm;border-top:1px solid {BORDER}}}
.rmeta div b{{color:{FAINT};font-weight:600;font-size:7.5pt;text-transform:uppercase;letter-spacing:.4px;display:block;margin-bottom:1mm}}
.rmeta div span{{font-weight:700;color:{INK};font-size:9.5pt}}
/* плашки «кому передать» */
.plashka{{border-left:3px solid {ACCENT};background:{CREAM};border-radius:0 10px 10px 0;padding:3.5mm 4mm;margin:3mm 0;font-size:9.5pt;color:{INK};line-height:1.5}} .plashka b{{color:{ACCENTD}}}
/* нумерованный чек-лист */
.chk2{{counter-reset:ck;margin-top:1mm}}
.chk2 li{{list-style:none;position:relative;padding:2.5mm 0 2.5mm 9mm;border-bottom:1px solid {BORDER};font-size:9.5pt;color:{INK};line-height:1.5}}
.chk2 li:last-child{{border-bottom:none}}
.chk2 li:before{{counter-increment:ck;content:counter(ck);position:absolute;left:0;top:2.5mm;width:6mm;height:6mm;background:{TRACK};border-radius:50%;text-align:center;line-height:6mm;font-size:8pt;font-weight:700;color:{MUTED}}}
/* словарь терминов */
.gloss{{background:#F6F2EC;border-radius:10px;padding:4mm;margin-top:3mm}}
.gloss .gh{{font-size:8pt;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:{FAINT};margin-bottom:2mm}}
.gloss p{{font-size:8.5pt;color:{MUTED};line-height:1.5;margin-bottom:1mm}} .gloss b{{color:{INK};font-weight:600}}
/* план: роли */
.prole{{margin:1.8mm 0 0}} .prole .pr{{font-size:8pt;font-weight:700;color:{FAINT};text-transform:uppercase;letter-spacing:.5px;margin-bottom:.5mm}}
/* разбор по запросам */
.qd{{padding-bottom:2.6mm;margin-bottom:2.6mm;border-bottom:1px solid {BORDER};break-inside:avoid}}
.qd:last-child{{border-bottom:none;margin-bottom:0;padding-bottom:0}}
.qd-q{{font-size:10pt;font-weight:700;color:{INK};margin-bottom:1.4mm;line-height:1.3}}
.qd-q .grp{{font-weight:500;color:{FAINT};font-size:8.5pt}}
.qd-e{{font-size:9pt;color:{INK};line-height:1.4;margin-bottom:.4mm;padding-left:4mm}} .qd-e b{{color:{MUTED}}}
.qd--hit{{background:rgba(46,139,87,.07);border:1px solid rgba(46,139,87,.32);border-left:3px solid {GREEN};border-radius:7px;padding:2.4mm 3mm;margin-bottom:2.6mm}}
.qd--hit:last-child{{margin-bottom:0;padding-bottom:2.4mm}}
.qd-badge{{display:inline-block;font-size:7.5pt;font-weight:700;color:{GREEN};background:rgba(46,139,87,.14);border-radius:20px;padding:1px 8px;margin-left:6px;vertical-align:middle}}
.qd-e--hit{{color:{GREEN}}} .qd-e--hit b{{color:{GREEN}}}
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
          Нейросетей: <b>{len([e for e in d['engines'] if not e.get('failed')])}</b> · запросов: <b>{len(d['queries'])}</b> · проверок: <b>{d['total_answers']}</b> · дата: <b>{esc(d['date'])}</b></div>
        <div class="cover-head">Бренд появился в <span>{d['overall']}%</span> проверок</div>
        <div class="cover-sub">{esc(d['cover_sub'])}</div>
      </div><div style="flex:none">{ring(d['overall'])}</div></div>
      <div class="cover-foot"><span>Внешний руководитель цифрового развития и AI-внедрения</span><span>annakurbatova.ru</span></div>
    </div>'''

def p_summary(d):
    rm=d['result_meaning']
    loss_h="Где бренд пока не появляется"
    strong_h="С чего начинать" if d.get('zero') else "На что опереться"
    if d.get('zero'):
        stat3_n="0%"; stat3_l="разницы между нейросетями нет: упоминаний не найдено нигде"
    else:
        eng=[e for e in d['engines'] if e.get('rate') is not None]; maxr=max((e['rate'] for e in eng), default=0); minr=min((e['rate'] for e in eng), default=0)
        best_names=[e['name'] for e in eng if e['rate']==maxr]; worst_names=[e['name'] for e in eng if e['rate']==minr]
        stat3_n=esc(_join(best_names))
        stat3_l=(f"результат одинаковый во всех каналах ({maxr}%)" if maxr==minr
                 else f"лучший канал ({maxr}%) · минимум у {esc(_join(worst_names))} ({minr}%)")
    return f'''<div class="page"><h2><span class="num">01</span>Что означает результат</h2>
      <div class="sec-intro">Короткий вывод по итогам {d['total_answers']} ответов: где бренд уже виден, где теряется и какая ближайшая цель.</div>
      <div class="card"><div style="font-size:13pt;font-weight:700;margin-bottom:2mm">{d['overall']}%: {esc(rm['headline'])}</div>
        <p style="font-size:10.5pt;color:{INK};line-height:1.55">{esc(rm['text'])}</p></div>
      <div class="two" style="margin-top:4mm">
        <div class="box"><h4>{loss_h}</h4><p>{esc(rm['loss'])}</p></div>
        <div class="box"><h4>{strong_h}</h4><p>{esc(rm['strong'])}</p></div></div>
      <div class="box cream"><h4>Ближайшая цель</h4><p>{esc(rm['goal'])}</p></div>
      <div class="grid3" style="margin-top:5mm">
        <div class="stat"><div class="n">{d['overall']}%</div><div class="l">{("нет видимости" if d['overall']==0 else d['level']+" видимость")} по {len([e for e in d['engines'] if not e.get('failed')])} {plural(len([e for e in d['engines'] if not e.get('failed')]),'рабочей нейросети','рабочим нейросетям','рабочим нейросетям')}</div></div>
        <div class="stat"><div class="n">{d['stable_q']} из {len(d['queries'])}</div><div class="l">запросов с повторяемым упоминанием (2/2 хотя бы в одной сети)</div></div>
        <div class="stat"><div class="n">{stat3_n}</div><div class="l">{stat3_l}</div></div></div>
      <div class="note" style="margin-top:3mm">2/2 означает, что бренд появился в обоих ответах на один и тот же вопрос.</div>
      <div class="box"><h4>Что дальше в отчёте</h4><p>Дальше: видимость по каждой нейросети, таблица повторяемости ответов, примеры реальных ответов, что показала проверка сайта и пошаговый план с приоритетами и ответственными.</p></div>
      {footer(d)}</div>'''

def _engine_fail_sub(e):
    if e.get('no_key'):
        return 'ключ не задан на сервере (переменная окружения); в расчёт видимости не входит'
    hint = (e.get('error_hint') or '').strip()
    if hint:
        return f'не удалось проверить: {esc(hint[:120])}; в расчёт видимости не входит'
    return 'нейросеть не ответила (ошибка доступа к API); в расчёт видимости не входит'

def _engine_bar(e):
    if e.get('failed') or e.get('no_key'):
        return (f'<div class="bar"><div class="bar-row">'
                f'<div class="bar-l" style="width:150px">{esc(e["name"])}</div>'
                f'<div class="bar-track" style="background:transparent"></div>'
                f'<div class="bar-v" style="color:{FAINT};font-weight:600;width:auto;white-space:nowrap;font-size:9pt">не удалось проверить</div></div>'
                f'<div class="bar-sub" style="padding-left:160px">{_engine_fail_sub(e)}</div></div>')
    return bar(e['name'], e['rate'], f"{e['mentions']} упоминаний в {e['answers']} ответах · {esc(e['note'])}", wl="150px")

def engine_chart(d):
    """Наглядная диаграмма: горизонтальные столбцы по нейросетям с процентами и шкалой 0–100%."""
    rows=""
    for e in d['engines']:
        nm=esc(e['name'])
        if e.get('rate') is None:
            rows+=(f'<div class="ec-row"><div class="ec-name">{nm}</div>'
                   f'<div class="ec-track"><div class="ec-fill" style="width:0"></div></div>'
                   f'<div class="ec-pct" style="color:{FAINT}">—</div></div>'
                   f'<div class="ec-sub">{_engine_fail_sub(e)}</div>')
        else:
            r=e['rate']; col=lvl(r)
            rows+=(f'<div class="ec-row"><div class="ec-name">{nm}</div>'
                   f'<div class="ec-track"><div class="ec-fill" style="width:{max(r,1.5)}%;background:{col}"></div></div>'
                   f'<div class="ec-pct" style="color:{col}">{r}%</div></div>'
                   f'<div class="ec-sub">{e["mentions"]} упоминаний в {e["answers"]} ответах · {esc(e["note"])}</div>')
    return f'<div class="ec"><div class="ec-scale"><span>0%</span><span>50%</span><span>100%</span></div>{rows}</div>'

def _donut(pct, col, center):
    """Круглая диаграмма (donut) одной нейросети: кольцо-прогресс на pct% с числом в центре."""
    R=15.5; C=2*math.pi*R; arc=C*max(min(pct,100),0)/100
    prog=(f'<circle cx="18" cy="18" r="{R}" fill="none" stroke="{col}" stroke-width="3.6" stroke-linecap="round" '
          f'stroke-dasharray="{arc:.2f} {C-arc:.2f}" transform="rotate(-90 18 18)"/>') if pct>0 else ''
    return (f'<svg viewBox="0 0 36 36" width="58" height="58">'
            f'<circle cx="18" cy="18" r="{R}" fill="none" stroke="{TRACK}" stroke-width="3.6"/>'
            f'{prog}'
            f'<text x="18" y="20.5" text-anchor="middle" font-size="9.5" font-weight="700" fill="{col}">{center}</text></svg>')

def engine_donuts(d):
    """Ряд круглых диаграмм по нейросетям — для наглядности рядом со столбчатой."""
    items=""
    for e in d['engines']:
        if e.get('rate') is None:
            items+=f'<div class="dn">{_donut(0, FAINT, "—")}<div class="dn-name">{esc(e["name"])}</div></div>'
        else:
            r=e['rate']; col=lvl(r)
            items+=f'<div class="dn">{_donut(r, col, f"{r}%")}<div class="dn-name">{esc(e["name"])}</div></div>'
    return f'<div class="dn-row">{items}</div>'

def p_engines(d):
    bars=engine_donuts(d)+engine_chart(d)
    me=d.get('mentioned_engines',[]); ze=d.get('zero_engines',[]); ans=d['engines'][0]['answers']; b=d['brand_short']
    total=d['total_answers']
    if not me:
        h1,strong_p="Результат проверки",f"Ни одна из проверенных нейросетей ({_join(ze)}) не упомянула {esc(b)} в этой выборке."
        h2,weak_p="Что это означает",("Сейчас бренд не попадает в рекомендации нейросетей по выбранным вопросам. Для начала стоит проверить, "
                                       "насколько точно сайт описывает ключевые услуги и достаточно ли информации о вас на других площадках.")
    else:
        mlist="; ".join(f"{esc(e['name'])} — {e['mentions']} из {e['answers']}" for e in d['engines'] if (e['rate'] or 0)>0)
        h1,strong_p="Где вас уже называют",f"Упоминания обнаружены в: {mlist}."
        if ze:
            h2,weak_p="Где пока не называют",f"В {_join(ze)} бренд не появился ни в одном из {ans} ответов."
        else:
            h2,weak_p="Где наращивать","Упоминания есть во всех нейросетях, но распределены неравномерно. Задача — повышать долю ответов с упоминанием в каждой нейросети."
    return f'''<div class="page"><h2><span class="num">02</span>Где вас находят нейросети</h2>
      <div class="sec-intro">Каждой нейросети задали {len(d['queries'])} вопросов, похожих на реальные вопросы потенциальных клиентов. Каждый вопрос проверили дважды — всего получено {total} ответов. Процент: доля ответов, где нейросеть упомянула бренд или сайт.</div>
      <div class="card">{bars}</div>
      <div class="two">
        <div class="box cream"><h4>{h1}</h4><p>{strong_p}</p></div>
        <div class="box"><h4>{h2}</h4><p>{weak_p}</p></div>
      </div>
      <div class="box"><h4>Как читать</h4><p>Процент — это доля из {ans} ответов одной нейросети, где она упомянула бренд или сайт. Чем выше, тем чаще вас видит клиент, который спрашивает совета у ИИ.</p></div>
      {footer(d)}</div>'''

def p_matrix(d):
    eng=d['engines']
    head="".join(f'<th class="c">{esc(e["short"])}</th>' for e in eng)
    rows=""
    for q in d['queries']:
        cells=""
        for e in eng:
            if e.get('failed'):
                cells+='<td class="c c0">—</td>'
            else:
                v=q['hits'].get(e['id'],0); cells+=f'<td class="c c{v}">{v}/{RUNS}</td>'
        rows+=f'<tr><td class="q">{esc(q["q"])}</td>{cells}</tr>'
    legend=" · ".join(f'{esc(e["short"])}: {esc(e["name"])}' + (" (не проверено)" if e.get("failed") else "") for e in eng)
    return f'''<div class="page"><h2><span class="num">03</span>В каких ответах бренд появляется, а в каких нет</h2>
      <div class="sec-intro">Каждый запрос проверен по {RUNS} раза. 2/{RUNS}: упоминание повторилось в обеих проверках. 1/{RUNS}: в одной из двух. 0/{RUNS}: не появился.</div>
      <table class="mx"><thead><tr><th class="q">Запрос</th>{head}</tr></thead><tbody>{rows}</tbody></table>
      <div class="two" style="margin-top:5mm">
        <div class="box"><h4>Повторяемость упоминаний</h4><p>Повторилось (2/{RUNS}): <b>{d['stable_cells']}</b> · в одной из двух (1/{RUNS}): <b>{d['partial_cells']}</b> · не обнаружено (0/{RUNS}): <b>{d['zero_cells']}</b> из {len(d['queries'])*len(eng)} ячеек.</p></div>
        <div class="box"><h4>Вывод</h4><p>{_matrix_verdict(d)}</p></div></div>
      <div class="note" style="margin-top:4mm">{legend}</div>
      {footer(d)}</div>'''

_BR_TLD=(r"(?:ru|рф|su|moscow|tatar|com|net|org|io|ai|co|me|app|dev|store|shop|online|pro|biz|info|tech|site|space|"
         r"by|kz|ua|uz|am|ge|tv|cc|us|uk|de|fr|cn|in|eu|gg|to|xyz|cloud|digital|agency|studio|team|group|"
         r"so|ly|sh|fm|im|is|la|gd|ws|gl|cm|sc|id|club|world|life|pw|top|run)")
_SELF_DOM_RE=re.compile(r"^(?:https?://)?([a-z0-9][a-z0-9-]{1,30}(?:\.[a-z0-9-]{2,})*\."+_BR_TLD+r")/?$", re.I)
def _self_url(name):
    """Имя само по себе домен -> прямой https-URL, иначе ''. """
    s=str(name or "").strip().strip("«»\"'·.,;: ").lower()
    m=_SELF_DOM_RE.match(s)
    if not m: return ""
    dom=m.group(1)
    if dom.split(".")[0]=="www": return ""
    return "https://"+dom

def _comp_url(name, lm=None):
    """URL сайта компании: 1) прямой сайт из карты ссылок отчёта, 2) имя само домен. Только реальные сайты, без поиска."""
    return (lm or {}).get(name) or _self_url(name)

def _linkify(name, lm=None, maxlen=24):
    """Имя компании -> кликабельная ссылка на её сайт (если URL известен), иначе просто текст."""
    raw=str(name or "").strip()
    if not raw: return ""
    disp=raw if len(raw)<=maxlen else raw[:maxlen-1].rstrip()+"…"
    url=_comp_url(raw, lm)
    if url:
        return f'<a href="{esc(url)}" style="color:{ACCENTD};text-decoration:none;font-weight:600">{esc(disp)} ↗</a>'
    return esc(disp)

def _qd_names(lst, lm=None, n=3):
    """Короткий список имён-ссылок: не больше n, длинные имена режем, остальное -> «и др.».
    Это держит высоту строки в 1-2 линии -> карточка запроса не переполняет страницу."""
    out=[]
    for x in lst:
        x=str(x).strip()
        if not x: continue
        out.append(_linkify(x, lm))
        if len(out)>=n: break
    tail=" и др." if len([x for x in lst if x])>len(out) else ""
    return ", ".join(out)+tail

def _qd_row(q, e, lm=None):
    if e.get('no_key'):
        return f'<div class="qd-e"><b>{esc(e["short"])}:</b> ключ не задан на сервере</div>'
    if e.get('failed'):
        hint = (e.get('error_hint') or '').strip()
        extra = f' ({esc(hint[:80])})' if hint else ''
        return f'<div class="qd-e"><b>{esc(e["short"])}:</b> не удалось проверить{extra}</div>'
    h=q['hits'].get(e['id'],0)
    ev=(q.get('evidence') or {}).get(e['id'],{})
    comps=ev.get('comps',[])                                  # подтверждённые нишевые конкуренты
    others=[o for o in ev.get('others',[]) if o not in comps]  # прочие названные игроки (площадки, домены, бренды)
    # имена ниже уже HTML-экранированы и обёрнуты в ссылки внутри _qd_names -> t выводим как HTML
    if h>=2:   t="назвал ваш бренд в обоих ответах (2/2)" + (", рядом назвал "+_qd_names(comps+others, lm) if (comps or others) else "")
    elif h==1: t="назвал ваш бренд в одном ответе (1/2)" + (", рядом назвал "+_qd_names(comps+others, lm) if (comps or others) else "")
    elif comps:t="назвал конкурентов: "+_qd_names(comps+others, lm)+"; вашего бренда нет"
    elif others:t="назвал других игроков: "+_qd_names(others, lm)+"; вашего бренда нет"
    else:      t="общий ответ без названий компаний"
    cls=" qd-e--hit" if h>=1 else ""                          # строка движка зелёным, если бренд появился
    return f'<div class="qd-e{cls}"><b>{esc(e["short"])}:</b> {t}</div>'

def p_query_detail(d):
    """Блок 04: каждый запрос отдельно — кто назвал бренд и кого из конкурентов, по каждой нейросети."""
    eng=d['engines']
    lm=d.get('link_map')
    cards=[]
    for i,q in enumerate(d['queries'],1):
        rows="".join(_qd_row(q,e,lm) for e in eng)
        hit=any((q.get('hits') or {}).get(e['id'],0)>0 for e in eng if not e.get('failed'))   # бренд появился хотя бы в одной сети
        cls="qd qd--hit" if hit else "qd"
        badge='<span class="qd-badge">✓ бренд появился</span>' if hit else ''
        cards.append(f'<div class="{cls}"><div class="qd-q">{i}. {esc(q["q"])}{badge}</div>{rows}</div>')
    legend=" · ".join(f'{esc(e["short"])} — {esc(e["name"])}' for e in eng)
    # карточек на страницу по числу нейросетей: при 6-7 сетях строки длиннее -> берём 3, чтобы не резало
    pages=[]; per=(3 if len(eng)>=6 else (4 if len(eng)>=4 else 6))
    for idx in range(0, len(cards), per):
        first=(idx==0)
        head=('<h2><span class="num">04</span>По каким вопросам бренд появляется и кого называют нейросети</h2>' if first
              else '<h2>По каким вопросам бренд появляется · продолжение</h2>')
        intro=(f'<div class="sec-intro">Каждый из {len(d["queries"])} вопросов отдельно: какая нейросеть назвала ваш бренд и кого из компаний называли. Сокращения: {legend}.</div>' if first else '')
        pages.append(f'<div class="page">{head}{intro}<div class="card" style="padding:5mm">{"".join(cards[idx:idx+per])}</div>{footer(d)}</div>')
    return pages

def p_groups(d):
    bars="".join(bar(g['name'], g['rate'], f"{g['m']} из {g['mx']} проверок · {g['n']} {plural(g['n'],'запрос','запроса','запросов')} в группе", wl="190px") for g in d['groups'])
    b=d['brand_short']
    if d.get('zero'):
        lean_h="Что делать дальше"
        loss_p=f"В этой проверке {esc(b)} {_aneg(d)} ни в одной группе вопросов. Поэтому пока нельзя выделить направление, которое уже приносит бренду видимость в нейросетях."
        lean_p="Начать стоит с вопросов о поиске компании и о ключевом товаре или услуге — это основные коммерческие сценарии, по которым клиент может искать вас через нейросеть."
        prio_p="Размер группы не говорит о её важности: по числу вопросов в группе нельзя делать вывод о приоритете. Двигаться стоит сразу по двум линиям — понятные ключевые страницы на сайте и внешние упоминания."
    else:
        lean_h="На что опереться"
        rep=_join_groups(d.get('rep_groups',[])); zg=_join_groups(d.get('groups_zero',[]))
        loss_p=(f"Упоминаний пока нет по группам: {zg}. " if zg else "По большинству групп упоминаний мало. ") + "Эти вопросы относятся к этапу выбора компании."
        lean_p=(f"Повторяемые упоминания есть по группам: {rep}. " if rep else "Повторяемых упоминаний пока мало. ") + "На них можно опереться и расширять охват на остальные запросы и нейросети."
        prio_p="Двигаться стоит по двум линиям: усилить материалы под группы без упоминаний и закрепить то, что уже сработало. Группу из одного вопроса не стоит напрямую сравнивать с группой из нескольких."
    return f'''<div class="page"><h2><span class="num">04</span>Видимость по направлениям</h2>
      <div class="sec-intro">Те же вопросы, собранные по направлениям. Видно, в каких сценариях клиенты вас находят, а в каких нет. Рядом — сколько упоминаний из всех проверок в группе.</div>
      <div class="card">{bars}</div>
      <div class="two" style="margin-top:4mm">
        <div class="box cream"><h4>Где бренд пока не появляется</h4><p>{loss_p}</p></div>
        <div class="box"><h4>{lean_h}</h4><p>{lean_p}</p></div></div>
      <div class="box"><h4>О приоритете групп</h4><p>{prio_p}</p></div>
      {footer(d)}</div>'''

def p_examples(d):
    has_comp=bool(d.get('competitors')); lm=d.get('link_map')
    cards=[]
    for ex in d['examples']:
        kind=ex['kind']
        tag={'yes':('tag-yes','Бренд появился'),'no':('tag-no','Бренда нет')}.get(kind,('tag-no','Бренда нет'))
        named=ex.get('named') or []
        named_str=', '.join(_linkify(x, lm) for x in named) if named else ''
        if kind=='yes':
            eng_line=(f"<b>{esc(ex['engine'])}:</b> назвал ваш бренд в этом ответе" + (f", рядом — {named_str}" if named_str else ""))
        elif named_str:
            eng_line=f"<b>{esc(ex['engine'])}:</b> назвал {named_str}; вашего бренда в этом ответе нет"
        else:
            eng_line=f"<b>{esc(ex['engine'])}:</b> вашего бренда в этом ответе нет"   # без ложного «компании не названы»
        quote_html=""; qlen=0
        q=ex.get('quote') or {}
        if q.get('text'):
            qt=q["text"]
            if len(qt)>250: qt=qt[:250].rsplit(" ",1)[0].rstrip(" ,.;:")+" …"
            qlen=len(qt)
            quote_html=(f'<div class="quote"><div class="quote-h">Фрагмент ответа · {esc(q.get("engine",""))}</div>'
                        f'<div class="quote-t">«{esc(qt)}»</div></div>')
        html=(f'<div class="ex"><span class="tag {tag[0]}">{tag[1]}</span>'
              f'<div class="q">{esc(ex["query"])}</div>{quote_html}'
              f'<div class="r">{eng_line}<br><b>Почему:</b> {esc(ex["why"])}</div></div>')
        h=30 + len(ex["query"])/56*6.4 + qlen/82*5.3 + len(ex["why"])/82*5.2
        cards.append((html, h))
    intro=("Пример ответа из проверки: кого называет нейросеть и появился ли ваш бренд." if len(cards)==1
           else "Несколько ответов из проверки: кого называет нейросеть, появился ли ваш бренд и что с этим делать.")
    others=d.get('others_named') or []
    if d.get('zero'):
        if has_comp:
            takeaway=("По этим вопросам нейросети уже называют другие компании (они в разделе ниже), но не ваш бренд. "
                      "Это и есть зона роста: появиться там, где сейчас показывают конкурентов. Что для этого усилить — в рекомендациях.")
        elif others:
            takeaway=(f"По этим вопросам нейросети называют сторонние площадки и магазины (например, {', '.join(_linkify(x, lm) for x in others[:4])}), но не ваш бренд. "
                      "Это и есть зона роста: попасть в ответ там, где сейчас показывают чужие площадки. Что для этого усилить — в рекомендациях.")
        else:
            takeaway=("По этим вопросам нейросети давали общий ответ без названий компаний: пока никто не занимает эти ответы. "
                      "Это редкая, но удобная ситуация — место свободно, и его можно занять первым. Что для этого усилить — в рекомендациях.")
    else:
        takeaway=("Повторяемые упоминания есть по части вопросов. По остальным бренд не появился; точную причину по одному ответу определить нельзя — "
                  "нужен разбор страниц сайта и внешних источников по этой теме.")
    boxes=(f'<div class="box cream"><h4>Что показывают примеры</h4><p>{takeaway}</p></div>'
           f'<div class="box"><h4>Что проверить</h4><p>Есть ли на сайте отдельная страница, которая прямо отвечает на такой вопрос — с конкретными фактами: что входит или из чего состоит, для кого, условия, цены или сроки, и есть ли отзывы. И упоминается ли компания по этой теме на внешних площадках.</p></div>'
           '<div class="note">Это короткие фрагменты для иллюстрации. Полные ответы целиком — в следующем разделе.</div>')
    boxes_h=98
    pages=[]; i=0; first=True; boxes_done=False
    while i < len(cards) or not boxes_done:
        budget = 206 if first else 246
        used=0.0; body=""
        while i < len(cards):
            h=cards[i][1]
            if body and used+h > budget: break
            body+=cards[i][0]; used+=h; i+=1
        tail=""
        if i >= len(cards) and not boxes_done and used + boxes_h <= budget:
            tail=boxes; boxes_done=True
        head=('<h2><span class="num">05</span>Примеры реальных ответов нейросетей</h2><div class="sec-intro">'+intro+'</div>'
              if first else '<h2>Примеры реальных ответов · продолжение</h2>')
        pages.append(f'<div class="page">{head}{body}{tail}{footer(d)}</div>')
        first=False
        if i >= len(cards) and not boxes_done:                # боксы не влезли на страницу с карточками -> отдельная страница
            pages.append(f'<div class="page"><h2>Примеры реальных ответов · итоги</h2>{boxes}{footer(d)}</div>')
            boxes_done=True
        if i >= len(cards) and boxes_done:
            break
    return "".join(pages)

def p_full_answers(d):
    """Развёрнутые ПОЛНЫЕ ответы нейросетей: по одному на каждый запрос. С обоснованием, почему не все RUNS*движки."""
    fa = d.get('full_answers') or []
    if not fa:
        return ""
    lm = d.get('link_map')
    total = d.get('answers_total') or (len(d.get('queries', []))*RUNS*len(d.get('engines', [])))
    n_eng = len([e for e in d.get('engines', []) if not e.get('failed')]) or len(d.get('engines', []))
    nq = len(d.get('queries', []))
    intro = (f"По каждому вопросу показан один наиболее показательный ответ нейросети. Всего сервис получил "
             f"{total} {plural(total,'ответ','ответа','ответов')}: {nq} {plural(nq,'вопрос','вопроса','вопросов')} × "
             f"{n_eng} {plural(n_eng,'нейросеть','нейросети','нейросетей')} × {RUNS} {plural(RUNS,'проверка','проверки','проверок')}. "
             "Поскольку ответы часто повторяются, здесь собраны только полные версии, которые лучше всего показывают, "
             "что видит потенциальный клиент, какие компании ему рекомендуют и упоминается ли среди них ваш бренд.")
    def _note(fx):
        if fx.get('hit'):
            return '<span class="fa-yes">✓ ваш бренд назван в этом ответе</span>'
        if fx.get('named'):
            return 'из конкурентов нейросеть назвала: ' + ', '.join(_linkify(x, lm) for x in fx['named'][:5]) + '; вашего бренда нет'
        return 'вашего бренда в этом ответе нет'   # без ложного «компании не названы» — компании могут быть в тексте выше
    def _card_mm(fx):
        return max(1, len(fx['text'])/86)*5.2 + max(1, len(fx['q'])/62)*6.0 + 27
    pages, idx, first = [], 0, True
    while idx < len(fa):
        budget = 196 if first else 234
        used, chunk = 0.0, []
        while idx < len(fa):
            h = _card_mm(fa[idx])
            if chunk and used + h > budget:
                break
            chunk.append(fa[idx]); used += h; idx += 1
        body = "".join(
            f'<div class="fa"><div class="fa-q">{esc(fx["q"])}</div>'
            f'<div class="fa-h">Ответ нейросети · {esc(fx["engine"])}</div>'
            f'<div class="fa-t">«{esc(fx["text"])}»</div>'
            f'<div class="fa-n">{_note(fx)}</div></div>' for fx in chunk)
        head = ('<h2><span class="num">06</span>Развёрнутые ответы нейросетей</h2>' if first
                else '<h2>Развёрнутые ответы нейросетей · продолжение</h2>')
        intro_html = f'<div class="sec-intro">{intro}</div>' if first else ''
        pages.append(f'<div class="page">{head}{intro_html}{body}{footer(d)}</div>')
        first = False
    return "".join(pages)

def _gap_phrase(gap):
    if gap > 0: return f"опережает вас на {gap} п.п."
    if gap < 0: return f"на {abs(gap)} п.п. ниже вас"
    return "наравне с вами"

def _comp_where(c):
    """Все запросы (и нейросети), где назван конкурент — чтобы «N из M» было видно по чему именно."""
    ms=c.get("mentions") or []
    if not ms: return ""
    parts=[f"«{esc(m['q'])}» ({esc(', '.join(m['engines']))})" for m in ms[:2]]
    more=f" и ещё {len(ms)-2}" if len(ms)>2 else ""
    word="запросу" if len(ms)==1 else "запросам"
    return f" Назван по {word}: " + "; ".join(parts) + more + "."

def _comp_link(c, lm=None):
    """Имя конкурента — прямая ссылка на его сайт. Если сайт определить не удалось — просто имя, без поисковой строки."""
    name=esc(c['name'])
    url=c.get('site') or _comp_url(c.get('name',''), lm)
    if url:
        return f'<a href="{esc(url)}" style="color:{ACCENTD};text-decoration:none;font-weight:700">{name} ↗</a>'
    return f'<span style="font-weight:700;color:{INK}">{name}</span>'

def p_competitors(d):
    N=d['total_answers']; bc=d.get('total_mentions',0); ov=d['overall']; lm=d.get('link_map')
    rows=[(d['brand_short'], ov, bc, True)] + [(c['name'], c['rate'], c['count'], False) for c in d['competitors']]
    rows.sort(key=lambda r:-r[1])
    # Высота секции зависит от числа компаний и длины их названий/запросов и может быть любой.
    # Поэтому: страница 1 — рейтинг (столбцы) + вывод (всегда влезает); подробные карточки по
    # каждой компании выносим на страницы-продолжения и разбиваем порциями, чтобы ничего не обрезалось.
    MAXBARS=11                               # рейтинг компактный; бренд показываем всегда
    _bars_more=0
    if len(rows)>MAXBARS:
        _bars_more=len(rows)-MAXBARS
        keep=[r for r in rows if r[3]] + [r for r in rows if not r[3]][:MAXBARS-1]
        rows=sorted(keep, key=lambda r:-r[1])
    bars=""
    for name,rate,cnt,is_you in rows:
        sub=("ваша текущая видимость" if is_you else f"{_gap_phrase(rate-ov)} · упомянут в {cnt} из {N} ответов")
        bars+=bar((esc(name)+" (вы)") if is_you else name, rate, sub, wl="150px", color=(ACCENTD if is_you else FAINT), you=is_you)
    if _bars_more>0:
        bars+=f'<div class="note" style="margin-top:1mm">…и ещё {_bars_more} компаний с меньшей видимостью.</div>'
    names_join=_join([_linkify(c['name'], lm) for c in d['competitors']][:6])
    max_c=max((c['rate'] for c in d['competitors']), default=0)
    if ov>0 and ov>=max_c:
        lead=f"«{esc(d['brand_short'])}» упомянут в {bc} из {N} ответов — чаще найденных компаний"
        if d.get('mentioned_engines'): lead+=f", но только в {_join(d['mentioned_engines'])}"
        takeaway=f"Помимо «{esc(d['brand_short'])}», в ответах встречались {names_join}. {lead}. Это хороший знак: бренд уже попадает в выдачу."
    else:
        who="ваш бренд пока нет" if ov==0 else "у части из них упоминаний больше"
        takeaway=(f"По вашим запросам нейросети называют {names_join}, а {who}. "
                  "Чаще всего дело в количестве согласованных упоминаний во внешних источниках (карты, отзывы, каталоги, публикации) и в понятных страницах под запросы. "
                  "С чего начать, чтобы появляться рядом с ними, — в рекомендациях дальше в отчёте.")
    page1=f'''<div class="page"><h2><span class="num">06</span>Какие компании ещё встречаются в ответах</h2>
      <div class="sec-intro">Компании, которые нейросети называли в ответах на ваши запросы. Где удалось определить сайт, имя кликабельно и ведёт прямо на него. Процент считается так же, как ваш: доля из {N} ответов, где встретилось название.</div>
      <div class="card">{bars}</div>
      <div class="box cream"><h4>Что это значит</h4><p>{takeaway}</p></div>
      {footer(d)}</div>'''
    # Подробные карточки по каждой компании — на страницах-продолжениях, порциями (защита от обрезки).
    comps=d['competitors']; lm=d.get('link_map')
    def _ev_card(c):
        return (f'<div class="ex"><div class="q">{_comp_link(c, lm)} · {c["rate"]}%</div>'
                f'<div class="r">Упомянут в <b>{c["count"]}</b> из <b>{c["total"]}</b> ответов нейросетей по вашим запросам.{_comp_where(c)}</div></div>')
    PER=3                                     # с запасом: даже длинные карточки (длинные имена + запросы) помещаются
    pages=[page1]
    for i in range(0, len(comps), PER):
        ev="".join(_ev_card(c) for c in comps[i:i+PER])
        intro=('<div class="sec-intro">Подробнее по каждой компании: в скольких ответах и по каким запросам её называли нейросети.</div>' if i==0 else '')
        pages.append(f'''<div class="page"><h2>Какие компании ещё встречаются · продолжение</h2>
      {intro}<div style="margin-top:2mm">{ev}</div>
      {footer(d)}</div>''')
    return "".join(pages)

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
            return ('<div class="box"><h4>Технический разбор сайта</h4><p>Проверка видимости в нейросетях выполнена полностью и от сайта не зависит. '
                    'Дополнительный технический разбор самого сайта автопроверка сделать не смогла — сайт не открылся для нашего робота '
                    '(обычно из-за защиты от ботов или загрузки контента скриптами). На результаты выше это не влияет.</p></div>')
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

def _site_facts_plain(d):
    """Факты автопроверки простым языком, со страховкой «система определила как»."""
    s=d.get('site_info') or {}
    if not s.get('ok'):
        if s and s.get('host'):
            return ("Важно: сама проверка видимости в нейросетях проведена полностью — она опрашивает нейросети и не зависит от доступа к вашему сайту. "
                    "А вот этот дополнительный технический разбор самого сайта автопроверка в этот раз выполнить не смогла: сайт не открылся для нашего робота "
                    "(частая причина — защита от ботов или контент, который подгружается скриптами). На результаты и проценты выше это никак не влияет. "
                    "Чтобы добавить и технический разбор сайта, передайте его вашему специалисту или временно откройте доступ для поисковых роботов.")
        return "Дополнительный технический разбор сайта в этот раз не проводился. На проверку видимости в нейросетях это не влияет."
    types={str(x).lower() for x in (s.get('schema') or [])}
    has_products=bool(types & {"product","offer","aggregateoffer","store","onlinestore","productgroup"}) or (s.get('product_pages',0) or 0)>0
    svc=s.get('service_pages',0); case=s.get('case_pages',0)
    bits=["Автоматическая проверка прошла по доступным страницам сайта."]
    found=[]                                                 # без точных чисел: счёт по ссылкам с главной ненадёжен для магазинов
    if has_products: found.append("каталог товаров и категории")
    if svc: found.append("страницы услуг")
    if case: found.append("страницы проектов или кейсов")
    if found: bits.append("На сайте видны " + ", ".join(found) + ".")
    has_org=bool(types & {"organization","localbusiness","corporation","professionalservice","store","hotel","lodgingbusiness"})
    has_svc="service" in types
    if has_org and has_svc: bits.append("На сайте есть техническая разметка с данными о компании и услугах.")
    elif has_org: bits.append("На сайте также есть базовая техническая разметка с информацией о компании.")
    else: bits.append("Отдельной технической разметки с данными о компании автопроверка не нашла.")
    if s.get('robots_blocks_ai'):
        bits.append("Часть AI-роботов закрыта в robots.txt — это стоит поправить в первую очередь.")
    return " ".join(bits)

def _biz_meaning(d):
    s=d.get('site_info') or {}
    types={str(x).lower() for x in (s.get('schema') or [])}
    has_products=bool(types & {"product","offer","aggregateoffer","store","onlinestore","productgroup"}) or (s.get('product_pages',0) or 0)>0
    rich = s.get('ok') and (has_products or s.get('service_pages') or s.get('case_pages') or (s.get('sitemap_urls',0) or 0)>=20)
    if rich:
        return ("На сайте уже достаточно материалов — создавать всё с нуля не нужно. Главная задача в том, чтобы связать "
                "существующие ключевые страницы (товары, услуги, направления) с вопросами, которые клиенты задают нейросетям, и описать их понятным языком с фактами.")
    return ("Пока на сайте мало отдельных понятных страниц под ключевые товары или услуги. Их стоит создать и наполнить конкретными фактами, "
            "чтобы нейросети могли использовать сайт как источник и называть компанию в ответах.")

def p_works(d):
    pos="".join(f'<li><span class="m mk-y">✓</span><span>{esc(x)}</span></li>' for x in d['positives'][:4])
    blk="".join(f'<li><span class="m mk-n">!</span><span>{esc(x)}</span></li>' for x in d['blockers'][:4])
    return f'''<div class="page"><h2><span class="num">08</span>Что показала проверка сайта</h2>
      <div class="sec-intro">Что автоматическая проверка нашла на сайте, что это значит для бизнеса и что передать техническому специалисту.</div>
      <div class="box"><h4>Что обнаружила автоматическая проверка</h4><p>{esc(_site_facts_plain(d))}</p></div>
      <div class="box cream"><h4>Что это значит для бизнеса</h4><p>{esc(_biz_meaning(d))}</p></div>
      <div class="two">
        <div class="box"><h4 style="color:{GREEN}">Что уже помогает</h4><ul class="ck">{pos}</ul></div>
        <div class="box"><h4 style="color:{RED}">Что мешает росту</h4><ul class="ck">{blk}</ul></div>
      </div>
      <div class="plashka"><b>Технические пункты</b> передайте администратору сайта, разработчику или SEO-специалисту — они собраны в чек-лист дальше в отчёте.</div>
      {footer(d)}</div>'''

_KTAG={'content':('ktag-content','Контент'),'tech':('ktag-tech','Техническая задача'),'promo':('ktag-promo','Продвижение')}
def _ktag(kind):
    c,l=_KTAG.get(kind,('ktag-content','Рекомендация')); return f'<span class="ktag {c}">{l}</span>'

def _rec_card(r, i):
    p=[f'<div class="rcard-h"><span class="rcard-n">{i}</span><h3>{esc(r["title"])}</h3>{_ktag(r.get("kind","content"))}</div>']
    if r.get("plain"):
        p.append(f'<div class="rlabel">Что это означает</div><p>{esc(r["plain"])}</p>')
    if r.get("status"):                                  # конкретный статус автопроверки: настроено / нет
        mk={'ok':('mk-y','✓'),'bad':('mk-n','✗'),'warn':('mk-w','⚠')}
        rows="".join(f'<li><span class="m {mk.get(st,("mk-w","•"))[0]}">{mk.get(st,("mk-w","•"))[1]}</span><span><b>{esc(label)}:</b> {esc(detail)}</span></li>' for label,st,detail in r["status"])
        p.append(f'<div class="rlabel">Что показала техническая проверка</div><ul class="ck">{rows}</ul>')
    if r.get("steps"):
        steps="".join(f'<li>{esc(s)}</li>' for s in r["steps"])
        p.append(f'<div class="rlabel">Что сделать</div><ul class="rsteps">{steps}</ul>')
    if r.get("example"):
        ex_html=esc(r["example"]).replace("\n","<br>")
        p.append(f'<div class="rlabel">Пример</div><div class="rex">{ex_html}</div>'
                 f'<div class="rex-note">Пример носит иллюстративный характер: он показывает структуру текста и информацию, которую стоит включить. '
                 f'Факты могут отличаться — просто замените их на свои данные.</div>')
    if r.get("handoff_note"):
        p.append(f'<div class="plashka"><b>Передайте специалисту.</b> {esc(r["handoff_note"])}</div>')
    if r.get("todo"):                                    # что настроить (только то, чего не хватает)
        items="".join(f'<li>{esc(t)}</li>' for t in r["todo"])
        p.append(f'<div class="rlabel">Что настроить</div><ul class="chk2">{items}</ul>')
    if r.get("checklist"):
        items="".join(f'<li>{esc(c)}</li>' for c in r["checklist"])
        p.append(f'<div class="rlabel">Чек-лист для специалиста</div><ul class="chk2">{items}</ul>')
    if r.get("glossary"):
        gl=" · ".join(f'<b>{esc(t)}</b> — {esc(dfn)}' for t,dfn in r["glossary"])
        p.append(f'<div class="gloss"><div class="gh">Простыми словами</div><p>{gl}</p></div>')
    p.append(f'''<div class="rmeta">
       <div><b>Приоритет</b><span>{esc(r.get("priority","—"))}</span></div>
       <div><b>Срок</b><span>{esc(r.get("term","—"))}</span></div>
       <div style="flex:1"><b>Кому передать</b><span style="font-weight:600;font-size:9pt;color:{MUTED}">{esc(r.get("handoff",""))}</span></div></div>''')
    return '<div class="rcard">'+"".join(p)+'</div>'

def _rec_chunks(recs):
    """По одной карточке на страницу: примеры под нишу и тех.статус разной длины, так надёжнее без переполнения."""
    return [[r] for r in recs]

def p_reco_page(d, items, first):
    cards="".join(_rec_card(r,i) for i,r in enumerate(items, d['_rec_n0']))
    d['_rec_n0']+=len(items)
    if first:
        head='<h2><span class="num">09</span>Что сделать, чтобы бренд появлялся в ответах</h2>'
        intro='<div class="sec-intro">Каждая задача расписана так: что это значит простыми словами, что конкретно сделать, пример и кому передать. Тип задачи помечен ярлыком: контент, продвижение или техническая работа.</div>'
    else:
        head='<h2>Что сделать, чтобы бренд появлялся в ответах · продолжение</h2>'; intro=''
    return f'<div class="page">{head}{intro}{cards}{footer(d)}</div>'

def _week_html(w):
    if w.get("groups"):
        roles="".join(f'<div class="prole"><div class="pr">{esc(g["role"])}</div><ul>{"".join(f"<li>{esc(i)}</li>" for i in g["items"])}</ul></div>' for g in w["groups"])
    else:
        roles=f'<ul>{"".join(f"<li>{esc(i)}</li>" for i in w.get("items",[]))}</ul>'
    return f'<div class="week"><div class="wh">{esc(w["week"])}</div>{roles}</div>'

def p_plan(d):
    weeks="".join(_week_html(w) for w in d['plan30'])
    return f'''<div class="page"><h2><span class="num">10</span>Что делать по неделям</h2>
      <div class="sec-intro">Последовательность действий на месяц, с ответственными за каждый блок. В конце — повторный замер, чтобы увидеть рост в цифрах.</div>
      <div class="card" style="padding:5mm">{weeks}</div>
      <div class="box"><h4>Методология и ограничения</h4><p class="note" style="font-size:8pt;color:{MUTED}">{esc(d['method_note'])}</p></div>
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
    pages=[p_cover(d), p_summary(d), p_engines(d), p_matrix(d)] + p_query_detail(d) + [p_examples(d)]
    fa_html=p_full_answers(d)                  # развёрнутые полные ответы (по одному на запрос)
    if fa_html:
        pages.append(fa_html)
    if d.get('competitors'):                  # блок конкурентов только если есть подтверждённые (>=2)
        pages.append(p_competitors(d))
    pages.append(p_works(d))
    d['_rec_n0']=1                            # сквозная нумерация карточек рекомендаций
    for idx,ch in enumerate(_rec_chunks(recs)):
        pages.append(p_reco_page(d, ch, first=(idx==0)))
    pages += [p_plan(d), p_author(d)]
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
