#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Painel do perfil — 3 workspaces estilo Omarchy (Hyprland + Waybar), tema Tokyo Night.

    python3 painel/gerar.py             coleta no GitHub e regera assets/*.svg
    python3 painel/gerar.py --offline   só regera os SVGs a partir de painel/dados.json

Fontes de dados
  • Calendário de contribuições: público (inclui a contagem das privadas, porque o perfil permite).
  • PAINEL_TOKEN (fine-grained, somente leitura, do próprio dono): linguagens e horários de commit
    dos repositórios privados. Sem o token, esses números ficam como estavam no cache.

Privacidade: este repositório é público. Só números agregados saem daqui — contagens, horas,
dias da semana e linguagens. Nenhum nome de repositório, mensagem de commit ou arquivo é
gravado em painel/dados.json ou nos SVGs.

Sem dependências: só a biblioteca padrão do Python 3.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from html import escape
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA = RAIZ / 'painel'
SAIDA = RAIZ / 'assets'
CFG = json.loads((PASTA / 'config.json').read_text(encoding='utf-8'))
ICONES_ARQ = PASTA / 'icones.json'
ICONES = json.loads(ICONES_ARQ.read_text(encoding='utf-8')) if ICONES_ARQ.exists() else {}
CACHE = PASTA / 'dados.json'
FUSO = dt.timezone(dt.timedelta(hours=CFG.get('fuso_horas', -3)))
USUARIO = CFG['usuario']

# ─────────────────────────────── tokyo night ───────────────────────────────
C = dict(bg='#1a1b26', bg2='#16161e', hl='#292e42', black='#414868', fg='#c0caf5', fg2='#a9b1d6',
         comment='#565f89', dim='#3b4261', blue='#7aa2f7', cyan='#7dcfff', teal='#2ac3de',
         magenta='#bb9af7', purple='#9d7cd8', orange='#ff9e64', yellow='#e0af68',
         green='#9ece6a', green1='#73daca', red='#f7768e', mint='#2be4a3')
MONO = ("'JetBrains Mono','Cascadia Code','Cascadia Mono','Fira Code','SF Mono',Menlo,"
        "Consolas,'DejaVu Sans Mono','Liberation Mono',monospace")
CW = 0.61  # largura média de um caractere monoespaçado (em)
MESES = 'jan fev mar abr mai jun jul ago set out nov dez'.split()
SEMANA = 'seg ter qua qui sex sáb dom'.split()
SEMANA_LONGA = 'seg ter qua qui sex sáb dom'.split()


def cor(nome: str) -> str:
    return C.get(nome, nome)


def log(*a):
    print('[painel]', *a, flush=True)


# ═══════════════════════════════ coleta ═══════════════════════════════

def gql(query: str, variables: dict, token: str) -> dict:
    req = urllib.request.Request(
        'https://api.github.com/graphql',
        data=json.dumps({'query': query, 'variables': variables}).encode(),
        headers={'Authorization': f'bearer {token}', 'Content-Type': 'application/json',
                 'User-Agent': 'painel-perfil'})
    with urllib.request.urlopen(req, timeout=45) as r:
        out = json.loads(r.read().decode('utf-8'))
    if out.get('errors'):
        raise RuntimeError('; '.join(e.get('message', '?') for e in out['errors']))
    return out['data']


Q_CAL = '''query($login:String!){ user(login:$login){ id
  contributionsCollection{ contributionCalendar{ totalContributions
    weeks{ contributionDays{ date contributionCount } } } } } }'''

Q_VIEWER = 'query{ viewer{ login } }'

Q_REPOS = '''query($login:String!,$after:String){ user(login:$login){
  repositories(ownerAffiliations:OWNER, first:50, after:$after){
    pageInfo{ hasNextPage endCursor }
    nodes{ name isPrivate isFork languages(first:25){ edges{ size node{ name } } } } } } }'''

Q_HIST = '''query($owner:String!,$name:String!,$since:GitTimestamp!,$author:ID!,$after:String){
  repository(owner:$owner,name:$name){ defaultBranchRef{ target{ ... on Commit{
    history(first:100, since:$since, author:{id:$author}, after:$after){
      pageInfo{ hasNextPage endCursor } nodes{ authoredDate } } } } } } }'''


def calendario_api(token: str):
    d = gql(Q_CAL, {'login': USUARIO}, token)['user']
    dias = [(x['date'], int(x['contributionCount']))
            for w in d['contributionsCollection']['contributionCalendar']['weeks']
            for x in w['contributionDays']]
    return d['id'], sorted(dias)


def calendario_html():
    """Plano B: a página pública do calendário (não precisa de token)."""
    req = urllib.request.Request(f'https://github.com/users/{USUARIO}/contributions',
                                 headers={'User-Agent': 'Mozilla/5.0 (painel-perfil)'})
    html = urllib.request.urlopen(req, timeout=45).read().decode('utf-8', 'replace')
    datas = {}
    for tag in re.findall(r'<td[^>]*data-date="[^"]+"[^>]*>', html):
        data = re.search(r'data-date="([^"]+)"', tag).group(1)
        ident = re.search(r'\bid="([^"]+)"', tag)
        if ident:
            datas[ident.group(1)] = data
    dias = {d: 0 for d in datas.values()}
    for alvo, texto in re.findall(r'<tool-tip[^>]*\bfor="([^"]+)"[^>]*>([^<]*)</tool-tip>', html):
        m = re.match(r'\s*(\d[\d,]*)\s+contribution', texto)
        if alvo in datas and m:
            dias[datas[alvo]] = int(m.group(1).replace(',', ''))
    if not dias:
        raise RuntimeError('calendário vazio')
    return sorted(dias.items())


def privado_api(token: str, autor_id: str) -> dict:
    """Linguagens e horários de commit dos repositórios do dono (exige token do próprio dono)."""
    excluir = {r.lower() for r in CFG.get('excluir_repos', [])}
    repos, depois = [], None
    while True:
        d = gql(Q_REPOS, {'login': USUARIO, 'after': depois}, token)['user']['repositories']
        repos += d['nodes']
        if not d['pageInfo']['hasNextPage']:
            break
        depois = d['pageInfo']['endCursor']
    total = len(repos)
    privados = sum(1 for r in repos if r['isPrivate'])
    linguagens: dict[str, int] = {}
    horas, semana, commits = [0] * 24, [0] * 7, 0
    desde = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365)).strftime('%Y-%m-%dT%H:%M:%SZ')
    for r in repos:
        if r['isFork'] or r['name'].lower() in excluir:
            continue
        for e in r['languages']['edges']:
            linguagens[e['node']['name']] = linguagens.get(e['node']['name'], 0) + int(e['size'])
        depois = None
        while True:
            d = gql(Q_HIST, {'owner': USUARIO, 'name': r['name'], 'since': desde,
                             'author': autor_id, 'after': depois}, token)['repository']
            ref = d and d.get('defaultBranchRef')
            hist = ref and ref.get('target', {}).get('history')
            if not hist:
                break
            for n in hist['nodes']:
                t = dt.datetime.fromisoformat(n['authoredDate'].replace('Z', '+00:00')).astimezone(FUSO)
                horas[t.hour] += 1
                semana[t.weekday()] += 1
                commits += 1
            if not hist['pageInfo']['hasNextPage']:
                break
            depois = hist['pageInfo']['endCursor']
    return {'fonte': 'token', 'coletado': agora_utc(), 'commits': commits, 'horas': horas,
            'semana': semana, 'linguagens': linguagens, 'repos': {'total': total, 'privados': privados}}


def garantir_icones():
    """Baixa os ícones da stack que faltarem (simple-icons, CC0) e guarda em painel/icones.json."""
    faltando = [slug for slug, _, _ in CFG['stack'] if slug not in ICONES]
    if not faltando:
        return
    for slug in faltando:
        try:
            req = urllib.request.Request(f'https://cdn.jsdelivr.net/npm/simple-icons@16/icons/{slug}.svg',
                                         headers={'User-Agent': 'painel-perfil'})
            svg = urllib.request.urlopen(req, timeout=30).read().decode('utf-8')
            m = re.search(r'<path d="([^"]+)"', svg)
            if m:
                ICONES[slug] = m.group(1)
        except Exception as e:  # noqa: BLE001
            log(f'ícone {slug} indisponível: {e}')
    ICONES_ARQ.write_text(json.dumps(ICONES, ensure_ascii=False, indent=0) + '\n', encoding='utf-8')
    log(f'ícones: {len(ICONES)} no cache')


def agora_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def coletar(dados: dict) -> dict:
    painel_token = os.environ.get('PAINEL_TOKEN', '').strip()
    gh_token = os.environ.get('GITHUB_TOKEN', '').strip()
    autor_id, dono = None, False
    # 1) calendário — token do dono, token do Actions ou a página pública, nessa ordem
    for nome, tok in (('PAINEL_TOKEN', painel_token), ('GITHUB_TOKEN', gh_token)):
        if not tok:
            continue
        try:
            autor_id, dias = calendario_api(tok)
            dados['calendario'] = {'fonte': nome, 'dias': dias}
            log(f'calendário via API ({nome}): {len(dias)} dias, {sum(c for _, c in dias)} contribuições')
            break
        except Exception as e:  # noqa: BLE001
            log(f'calendário via {nome} falhou: {e}')
    else:
        try:
            dias = calendario_html()
            dados['calendario'] = {'fonte': 'html', 'dias': dias}
            log(f'calendário via página pública: {len(dias)} dias')
        except Exception as e:  # noqa: BLE001
            log(f'calendário indisponível, mantendo cache: {e}')
    # 2) dados privados agregados — só com o token do próprio dono
    if painel_token:
        try:
            login = gql(Q_VIEWER, {}, painel_token)['viewer']['login']
            dono = login.lower() == USUARIO.lower()
            if not dono:
                log(f'PAINEL_TOKEN pertence a {login}, não a {USUARIO}; ignorando dados privados')
        except Exception as e:  # noqa: BLE001
            log(f'PAINEL_TOKEN inválido ou expirado: {e}')
        if dono:
            try:
                if not autor_id:
                    autor_id, _ = calendario_api(painel_token)
                dados['privado'] = privado_api(painel_token, autor_id)
                p = dados['privado']
                log(f"privado: {p['commits']} commits, {len(p['linguagens'])} linguagens, {p['repos']['total']} repos")
            except Exception as e:  # noqa: BLE001
                log(f'coleta privada falhou, mantendo cache: {e}')
    else:
        log('sem PAINEL_TOKEN: linguagens e horários seguem do cache')
    dados['atualizado'] = agora_utc()
    return dados


# ═══════════════════════════════ métricas ═══════════════════════════════

def hoje_local() -> dt.date:
    return dt.datetime.now(FUSO).date()


def metricas(dados: dict) -> dict:
    dias = [(dt.date.fromisoformat(d), c) for d, c in dados['calendario']['dias']]
    dias.sort()
    total = sum(c for _, c in dias)
    ano = dias[-365:]
    ativos = sum(1 for _, c in ano if c > 0)
    maior_dia = max(dias, key=lambda x: (x[1], x[0])) if dias else (hoje_local(), 0)
    # streaks
    runs, ini = [], None
    for i, (d, c) in enumerate(dias):
        if c > 0 and ini is None:
            ini = i
        if (c == 0 or i == len(dias) - 1) and ini is not None:
            fim = i if c > 0 else i - 1
            runs.append((dias[ini][0], dias[fim][0], fim - ini + 1))
            ini = None
    maior = max(runs, key=lambda r: (r[2], r[1])) if runs else None
    atual = 0
    atual_ini = atual_fim = None
    j = len(dias) - 1
    if j >= 0 and dias[j][1] == 0 and dias[j][0] >= hoje_local():
        j -= 1  # o dia de hoje ainda não acabou
    while j >= 0 and dias[j][1] > 0:
        atual += 1
        atual_fim = atual_fim or dias[j][0]
        atual_ini = dias[j][0]
        j -= 1
    ult30 = dias[-30:]
    ritmo = sum(1 for _, c in ult30 if c > 0) / max(len(ult30), 1)
    p = dados.get('privado') or {}
    bruto = dict(p.get('linguagens') or {})
    outras = bruto.pop('__outras__', 0)  # "Other" agregado pelo GitHub (só no cache inicial)
    soma = (sum(bruto.values()) + outras) or 1
    ling = sorted(((k, v / soma) for k, v in bruto.items()), key=lambda x: -x[1])
    return dict(dias=dias, total=total, ativos=ativos, maior_dia=maior_dia, maior=maior,
                atual=atual, atual_ini=atual_ini, atual_fim=atual_fim, ritmo=ritmo,
                media=total / max(len(dias), 1), commits=p.get('commits', 0),
                horas=p.get('horas') or [0] * 24, semana=p.get('semana') or [0] * 7,
                linguagens=ling, repos=p.get('repos') or {'total': 0, 'privados': 0},
                atualizado=dt.datetime.fromisoformat(dados['atualizado'].replace('Z', '+00:00')).astimezone(FUSO))


# ═══════════════════════════════ svg: base ═══════════════════════════════

def n(v: float) -> str:
    s = f'{v:.2f}'.rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def num(v: int) -> str:
    return f'{v:,}'.replace(',', '.')


def ddmm(d: dt.date | None) -> str:
    return d.strftime('%d/%m') if d else '—'


def T(x, y, s, fill, tam=16, peso=None, ancora=None, extra='') -> str:
    w = f' font-weight="{peso}"' if peso else ''
    a = f' text-anchor="{ancora}"' if ancora else ''
    return f'<text x="{n(x)}" y="{n(y)}" font-size="{n(tam)}" fill="{fill}"{w}{a}{extra}>{escape(str(s))}</text>'


def TS(x, y, partes, tam=16, ancora=None, extra='') -> str:
    a = f' text-anchor="{ancora}"' if ancora else ''
    o = [f'<text x="{n(x)}" y="{n(y)}" font-size="{n(tam)}"{a}{extra} xml:space="preserve">']
    for p in partes:
        s, fill = p[0], p[1]
        peso = p[2] if len(p) > 2 else None
        w = f' font-weight="{peso}"' if peso else ''
        o.append(f'<tspan fill="{fill}"{w}>{escape(str(s))}</tspan>')
    return ''.join(o) + '</text>'


def largura(s: str, tam: float) -> float:
    return len(s) * tam * CW


ANSI = {
    'J': ["     ██╗", "     ██║", "     ██║", "██   ██║", "╚█████╔╝", " ╚════╝ "],
    'O': [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    'D': ["██████╗ ", "██╔══██╗", "██║  ██║", "██║  ██║", "██████╔╝", "╚═════╝ "],
    'A': [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    'N': ["███╗   ██╗", "████╗  ██║", "██╔██╗ ██║", "██║╚██╗██║", "██║ ╚████║", "╚═╝  ╚═══╝"],
}


def wordmark(palavra, x, y, cw, ch, grad='url(#wm)', sombra=C['dim']):
    linhas = [''.join(ANSI[c][r] for c in palavra) for r in range(6)]
    rects, caminhos = [], []
    for r, lin in enumerate(linhas):
        c = 0
        while c < len(lin):
            if lin[c] == '█':
                s = c
                while c < len(lin) and lin[c] == '█':
                    c += 1
                rects.append(f'<rect x="{n(x + s * cw)}" y="{n(y + r * ch)}" width="{n((c - s) * cw + .4)}" height="{n(ch + .4)}"/>')
                continue
            x0, y0 = x + c * cw, y + r * ch
            mx, my = x0 + cw / 2, y0 + ch / 2
            E, D_, S, I = (x0, my), (x0 + cw, my), (mx, y0), (mx, y0 + ch)
            M = (mx, my)
            seg = {'═': [(E, D_)], '║': [(S, I)], '╗': [(E, M), (M, I)], '╔': [(M, D_), (M, I)],
                   '╝': [(E, M), (S, M)], '╚': [(M, D_), (S, M)]}.get(lin[c], [])
            for a, b in seg:
                caminhos.append(f'M{n(a[0])} {n(a[1])}L{n(b[0])} {n(b[1])}')
            c += 1
    return (f'<path d="{"".join(caminhos)}" fill="none" stroke="{sombra}" stroke-width="2.2" stroke-linecap="square"/>'
            f'<g fill="{grad}">{"".join(rects)}</g>'), len(linhas[0]) * cw


def defs_base() -> str:
    return f'''<defs>
<linearGradient id="wall" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1f2335"/><stop offset="1" stop-color="#101016"/></linearGradient>
<radialGradient id="g1" cx=".1" cy=".02" r=".7"><stop offset="0" stop-color="{C['blue']}" stop-opacity=".22"/><stop offset="1" stop-color="{C['blue']}" stop-opacity="0"/></radialGradient>
<radialGradient id="g2" cx=".95" cy="1" r=".65"><stop offset="0" stop-color="{C['magenta']}" stop-opacity=".2"/><stop offset="1" stop-color="{C['magenta']}" stop-opacity="0"/></radialGradient>
<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1.5" cy="1.5" r="1" fill="{C['fg2']}" opacity=".07"/></pattern>
<linearGradient id="act" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C['blue']}"/><stop offset=".55" stop-color="{C['magenta']}"/><stop offset="1" stop-color="{C['cyan']}"/></linearGradient>
<linearGradient id="wm" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C['blue']}"/><stop offset=".55" stop-color="{C['purple']}"/><stop offset="1" stop-color="{C['magenta']}"/></linearGradient>
<linearGradient id="hot" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C['magenta']}"/><stop offset="1" stop-color="{C['blue']}"/></linearGradient>
<linearGradient id="zg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4a55ff"/><stop offset=".5" stop-color="#2719ff"/><stop offset="1" stop-color="#1c10c9"/></linearGradient>
<linearGradient id="zw" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#e6e8fb"/></linearGradient>
<clipPath id="zclip"><rect width="100" height="100" rx="22"/></clipPath>
<symbol id="zicon" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="url(#zg)"/>
<circle cx="29" cy="24" r="52" fill="#fff" opacity=".08" clip-path="url(#zclip)"/>
<path d="M33.5 38.5H65.5L33.5 72.5H66.5" fill="none" stroke="url(#zw)" stroke-width="11.2" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="72.5" cy="29" r="6.6" fill="{C['mint']}"/><circle cx="72.5" cy="29" r="2.3" fill="#0d0d1a"/></symbol>
</defs>'''


ESTILO = f'''<style>
text{{font-family:{MONO}}}
.cur{{animation:pisca 1.1s steps(1) infinite}}
@keyframes pisca{{50%{{opacity:0}}}}
.vivo{{animation:vivo 1.8s ease-in-out infinite}}
@keyframes vivo{{50%{{opacity:.25}}}}
.sobe{{transform-box:fill-box;transform-origin:50% 100%;animation:sobe .9s cubic-bezier(.2,.8,.2,1) both}}
@keyframes sobe{{from{{transform:scaleY(0)}}}}
.abre{{transform-box:fill-box;transform-origin:0 50%;animation:abre 1.2s cubic-bezier(.2,.8,.2,1) both}}
@keyframes abre{{from{{transform:scaleX(0)}}}}
.anel{{animation:anel 1.6s cubic-bezier(.2,.8,.2,1) .3s both}}
.vai{{transform-box:fill-box;animation:vai 2.6s ease-in-out infinite}}
@keyframes vai{{from{{transform:translateX(-100%)}}to{{transform:translateX(420%)}}}}
.toast{{animation:toast .7s cubic-bezier(.2,.8,.2,1) 1.2s both}}
@keyframes toast{{from{{transform:translateX(40px);opacity:0}}}}
@media (prefers-reduced-motion:reduce){{.cur,.vivo,.sobe,.abre,.anel,.vai,.toast,.digita,.cursor-digita{{animation:none}}}}
</style>'''


def waybar(w: int, ws: int, relogio: str) -> str:
    o = [f'<rect width="{w}" height="36" fill="{C["bg2"]}" opacity=".9"/>',
         f'<line x1="0" y1="36" x2="{w}" y2="36" stroke="{C["hl"]}"/>',
         '<use href="#zicon" x="16" y="9" width="18" height="18"/>']
    for i in range(1, 6):
        x = 56 + (i - 1) * 26
        if i == ws:
            o.append(f'<rect x="{x - 10}" y="7" width="22" height="22" rx="6" fill="{C["blue"]}" opacity=".18"/>')
            o.append(f'<rect x="{x - 4}" y="31" width="10" height="2.5" rx="1.2" fill="{C["blue"]}"/>')
            o.append(T(x + 1, 23, i, C['blue'], 13.5, 700, 'middle'))
        else:
            o.append(T(x + 1, 23, i, C['fg2'] if i <= 3 else C['comment'], 13.5, None, 'middle'))
    o.append(T(w / 2, 23, relogio, C['fg2'], 13.5, None, 'middle'))
    rx = w - 18
    o.append(f'<rect x="{rx - 26}" y="12" width="22" height="12" rx="2.5" fill="none" stroke="{C["fg2"]}" stroke-width="1.4"/>'
             f'<rect x="{rx - 3.5}" y="15.5" width="2.5" height="5" rx="1" fill="{C["fg2"]}"/>'
             f'<rect x="{rx - 24}" y="14" width="15" height="8" rx="1.2" fill="{C["green"]}"/>')
    vx = rx - 58
    o.append(f'<path d="M{vx} 15h4l5-4v14l-5-4h-4z" fill="{C["fg2"]}"/>'
             f'<path d="M{vx + 12} 14.5q3 3.5 0 7M{vx + 15} 12q5.5 6 0 12" fill="none" stroke="{C["fg2"]}" stroke-width="1.4" stroke-linecap="round"/>')
    sx = rx - 92
    for k, h in enumerate((4, 7, 10, 13)):
        o.append(f'<rect x="{sx + k * 5}" y="{24 - h}" width="3" height="{h}" rx="1" fill="{C["fg2"] if k < 3 else C["comment"]}"/>')
    o.append(TS(rx - 178, 23, [('cpu ', C['comment']), ('▂▃▅▂', C['cyan'])], 13))
    return ''.join(o)


def janela(x, y, w, h, ativa=False) -> str:
    borda = 'url(#act)' if ativa else C['dim']
    return (f'<rect x="{n(x)}" y="{n(y)}" width="{n(w)}" height="{n(h)}" rx="10" fill="{C["bg"]}" '
            f'stroke="{borda}" stroke-width="2"/>')


def caixa(x, y, w, h, titulo, borda=C['black']) -> str:
    """Caixa estilo btop: borda arredondada com o título cortando a linha de cima."""
    tw = sum(len(p[0]) for p in titulo) * 14.5 * CW + 18
    return (f'<rect x="{n(x)}" y="{n(y)}" width="{n(w)}" height="{n(h)}" rx="8" fill="none" stroke="{borda}" stroke-width="1.4"/>'
            f'<rect x="{n(x + 14)}" y="{n(y - 9)}" width="{n(tw)}" height="18" fill="{C["bg"]}"/>'
            + TS(x + 23, y + 5, titulo, 14.5))


def paleta(x, y) -> str:
    cores = [C['black'], C['red'], C['green'], C['yellow'], C['blue'], C['magenta'], C['cyan'], C['fg']]
    return ''.join(f'<rect x="{x + i * 32}" y="{y}" width="28" height="15" rx="3" fill="{c}"/>' for i, c in enumerate(cores))


def prompt(x, y, caminho='~', cmd='', cursor=False, tam=17) -> str:
    o = TS(x, y, [(caminho, C['blue'], 700), (' ❯ ', C['green'], 700), (cmd, C['fg'])], tam)
    if cursor:
        cx = x + largura(caminho + ' ❯ ' + cmd, tam) + 2
        o += f'<rect class="cur" x="{n(cx)}" y="{n(y - tam * .88)}" width="{n(tam * .58)}" height="{n(tam * 1.12)}" fill="{C["fg"]}"/>'
    return o


def desktop(w, h, ws, corpo, titulo, desc, relogio, estilo_extra='', defs_extra='') -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'role="img" aria-labelledby="t d"><title id="t">{escape(titulo)}</title><desc id="d">{escape(desc)}</desc>'
            f'{ESTILO}{estilo_extra and "<style>" + estilo_extra + "</style>"}{defs_base()}{defs_extra}'
            f'<clipPath id="card"><rect width="{w}" height="{h}" rx="16"/></clipPath><g clip-path="url(#card)">'
            f'<rect width="{w}" height="{h}" fill="url(#wall)"/><rect width="{w}" height="{h}" fill="url(#g1)"/>'
            f'<rect width="{w}" height="{h}" fill="url(#g2)"/><rect width="{w}" height="{h}" fill="url(#dots)"/>'
            f'{waybar(w, ws, relogio)}{corpo}</g>'
            f'<rect x=".75" y=".75" width="{w - 1.5}" height="{h - 1.5}" rx="15.5" fill="none" stroke="{C["hl"]}" stroke-width="1.5"/></svg>')


def relogio(m) -> str:
    a = m['atualizado']
    return f"{SEMANA[a.weekday()]} {a.day:02d} {MESES[a.month - 1]}  {a.strftime('%H:%M')}"


def chip(x, y, texto, c, tam=13) -> tuple[str, float]:
    w = largura(texto, tam) + 30
    s = (f'<rect x="{n(x)}" y="{n(y)}" width="{n(w)}" height="{n(tam + 11)}" rx="{n((tam + 11) / 2)}" fill="{c}" fill-opacity=".13" stroke="{c}" stroke-opacity=".5"/>'
         f'<circle cx="{n(x + 13)}" cy="{n(y + (tam + 11) / 2)}" r="3.6" fill="{c}"/>'
         + T(x + 22, y + tam + 4, texto, c, tam))
    return s, w


# ═══════════════════════════════ workspace 1 ═══════════════════════════════

def workspace1(m) -> str:
    W, H = 1200, 600
    ff = CFG['fastfetch']
    o = []
    # ── terminal principal: fastfetch
    x, y, w, h = 12, 48, 700, 540
    o.append(janela(x, y, w, h, ativa=True))
    px = x + 28
    o.append(prompt(px, y + 38, '~', 'fastfetch'))
    wm, wmw = wordmark('JODAN', px, y + 64, 12.4, 19)
    o.append(wm)
    ty = y + 214
    o.append(TS(px, ty, [(ff['titulo'][0], C['blue'], 700), ('@', C['fg2']), (ff['titulo'][1], C['magenta'], 700),
                          ('  ·  ' + ff['subtitulo'], C['comment'])], 17))
    o.append(f'<line x1="{px}" y1="{ty + 13}" x2="{px + 128}" y2="{ty + 13}" stroke="{C["comment"]}" stroke-width="1.5" stroke-dasharray="5 4"/>')
    linhas = [(k, v, cor(c)) for k, v, c in ff['linhas']]
    if m['atual'] > 0:
        linhas.append(('streak', [(f"{m['atual']} dias seguidos ", C['fg2']), ('●', C['green'])], C['green1']))
    else:
        linhas.append(('streak', 'pronto pro próximo commit', C['green1']))
    linhas.append(('12 meses', f"{num(m['total'])} contribuições · {num(m['commits'])} commits", C['red']))
    ly = ty + 44
    for i, (k, v, c) in enumerate(linhas):
        yy = ly + i * 27
        o.append(f'<rect x="{px}" y="{yy - 13}" width="3" height="16" rx="1.5" fill="{c}"/>')
        o.append(T(px + 13, yy, k, c, 17, 700))
        if isinstance(v, str):
            o.append(T(px + 136, yy, v, C['fg2'], 17))
        else:
            o.append(TS(px + 136, yy, v, 17, extra=' class="vivo"' if False else ''))
    py = ly + len(linhas) * 27 - 4
    o.append(paleta(px, py))
    o.append(prompt(px, py + 50, '~', '', cursor=True))

    # ── boas-vindas (digitando)
    bw = CFG['boas_vindas']
    x, y, w, h = 724, 48, 464, 264
    o.append(janela(x, y, w, h))
    px = x + 26
    o.append(prompt(px, y + 38, '~', bw['comando'], tam=15))
    tam = 38
    tw = largura(bw['titulo'], tam)
    by = y + 118
    o.append(f'<clipPath id="digita"><rect class="digita" x="{n(px - 2)}" y="{n(by - tam)}" width="{n(tw + 8)}" height="{n(tam * 1.4)}"/></clipPath>')
    o.append(f'<text x="{px}" y="{by}" font-size="{tam}" font-weight="800" fill="url(#hot)" clip-path="url(#digita)">{escape(bw["titulo"])}</text>')
    o.append(f'<rect class="cursor-digita cur" x="{n(px + tw + 4)}" y="{n(by - tam * .8)}" width="{n(tam * .45)}" height="{n(tam * .95)}" fill="{C["magenta"]}" opacity=".85"/>')
    o.append(T(px, by + 34, bw['linha'], C['fg2'], 16))
    o.append(T(px, by + 62, bw['dica'], C['comment'], 14))
    o.append(f'<line x1="{px}" y1="{y + h - 42}" x2="{x + w - 26}" y2="{y + h - 42}" stroke="{C["hl"]}" stroke-dasharray="3 5"/>')
    o.append(TS(px, y + h - 18, [('atualizado ', C['comment']), (m['atualizado'].strftime('%d/%m %H:%M'), C['fg2']),
                                  ('  ·  todo dia, sozinho', C['comment'])], 13))
    o.append(f'<circle class="vivo" cx="{x + w - 34}" cy="{y + h - 23}" r="5" fill="{C["green"]}"/>')
    passos = len(bw['titulo'])
    estilo = (f'.digita{{transform-box:fill-box;transform-origin:0 50%;animation:digita 1.6s steps({passos}) .5s both}}'
              f'@keyframes digita{{from{{transform:scaleX(0)}}}}'
              f'.cursor-digita{{animation:anda 1.6s steps({passos}) .5s both,pisca 1.1s steps(1) 2.2s infinite}}'
              f'@keyframes anda{{from{{transform:translateX(-{n(tw + 4)}px)}}}}')

    # ── agora: zaruvi
    ag = CFG['agora']
    x, y, w, h = 724, 324, 464, 264
    o.append(janela(x, y, w, h))
    o.append(caixa(x + 14, y + 18, w - 28, h - 32, [(ag['rotulo'], C['yellow'], 700), (' · ' + ag['status'], C['comment'])]))
    o.append(f'<use href="#zicon" x="{x + 36}" y="{y + 44}" width="76" height="76"/>')
    o.append(T(x + 132, y + 78, ag['nome'], C['fg'], 30, 800))
    for i, lin in enumerate(ag['descricao']):
        o.append(T(x + 132, y + 104 + i * 21, lin, C['fg2'], 15))
    cx, cy = x + 36, y + 150
    for texto, c in ag['tags']:
        s, wc = chip(cx, cy, texto, cor(c), 12.5)
        if cx + wc > x + w - 30:
            cx, cy = x + 36, cy + 32
            s, wc = chip(cx, cy, texto, cor(c), 12.5)
        o.append(s)
        cx += wc + 8
    yb = y + h - 42
    o.append(T(x + 36, yb + 4, 'progresso', C['comment'], 12.5))
    o.append(f'<clipPath id="trilho"><rect x="{x + 128}" y="{yb - 6}" width="{w - 164}" height="10" rx="5"/></clipPath>')
    o.append(f'<rect x="{x + 128}" y="{yb - 6}" width="{w - 164}" height="10" rx="5" fill="{C["hl"]}"/>')
    o.append(f'<g clip-path="url(#trilho)"><rect class="vai" x="{x + 128}" y="{yb - 6}" width="{(w - 164) / 4.6:.1f}" height="10" rx="5" fill="url(#hot)"/></g>')

    return desktop(W, H, 1, ''.join(o), 'Jodan Carvalho — workspace 1',
                   'Desktop estilo Omarchy (Hyprland, Waybar, Tokyo Night): fastfetch com o perfil do Jodan, '
                   'boas-vindas e o projeto atual, Zaruvi.', relogio(m), estilo_extra=estilo)


# ═══════════════════════════════ workspace 2 ═══════════════════════════════

def anel(cx, cy, r, larg, frac, grad, trilho=C['hl']) -> str:
    circ = 2 * math.pi * r
    frac = max(0.0, min(1.0, frac))
    vis = circ * frac
    return (f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(r)}" fill="none" stroke="{trilho}" stroke-width="{n(larg)}"/>'
            f'<circle class="anel" cx="{n(cx)}" cy="{n(cy)}" r="{n(r)}" fill="none" stroke="{grad}" stroke-width="{n(larg)}" '
            f'stroke-linecap="round" stroke-dasharray="{n(vis)} {n(circ)}" transform="rotate(-90 {n(cx)} {n(cy)})" '
            f'style="--c:{n(vis)}"/>')


def workspace2(m) -> str:
    W, H = 1200, 640
    o = []
    defs = (f'<defs><linearGradient id="anelg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C["blue"]}"/>'
            f'<stop offset="1" stop-color="{C["magenta"]}"/></linearGradient>'
            f'<linearGradient id="fogo" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="{C["red"]}"/>'
            f'<stop offset="1" stop-color="{C["orange"]}"/></linearGradient>'
            f'<linearGradient id="barra" x1="0" y1="1" x2="0" y2="0"><stop offset="0" stop-color="{C["blue"]}"/>'
            f'<stop offset="1" stop-color="{C["magenta"]}"/></linearGradient></defs>')
    estilo = '@keyframes anel{from{stroke-dasharray:0 2000}}'

    # ── stats
    x, y, w, h = 12, 48, 582, 284
    o.append(janela(x, y, w, h, ativa=True))
    o.append(caixa(x + 14, y + 18, w - 28, h - 32, [('stats', C['green'], 700), (' · 12 meses', C['comment'])]))
    md = m['maior_dia']
    itens = [
        ('contribuições', num(m['total']), '', C['blue']),
        ('commits', num(m['commits']), '', C['magenta']),
        ('dias ativos', num(m['ativos']), f"de {min(len(m['dias']), 365)}", C['cyan']),
        ('maior dia', num(md[1]), ddmm(md[0]), C['orange']),
        ('repositórios', num(m['repos']['total']), f"{m['repos']['privados']} privados", C['green']),
    ]
    for i, (rot, val, sub, c) in enumerate(itens):
        yy = y + 70 + i * 40
        o.append(T(x + 38, yy, '▸', c, 15, 700))
        o.append(T(x + 58, yy, rot, C['fg2'], 15.5))
        lx = x + 58 + largura(rot, 15.5) + 10
        o.append(f'<line x1="{n(lx)}" y1="{yy - 4}" x2="{x + 286}" y2="{yy - 4}" stroke="{C["dim"]}" stroke-width="2" stroke-dasharray="1.5 6" stroke-linecap="round"/>')
        o.append(T(x + 334, yy, val, C['fg'], 19, 800, 'end'))
        if sub:
            o.append(T(x + 344, yy, sub, C['comment'], 13))
    cx, cy = x + 478, y + 150
    o.append(anel(cx, cy, 54, 11, m['ritmo'], 'url(#anelg)'))
    o.append(T(cx, cy + 6, f"{round(m['ritmo'] * 100)}%", C['fg'], 26, 800, 'middle'))
    o.append(T(cx, cy + 26, 'ritmo 30d', C['comment'], 13, None, 'middle'))
    o.append(T(cx, cy + 86, f"{sum(1 for _, c in m['dias'][-30:] if c)} de 30 dias", C['fg2'], 13.5, None, 'middle'))

    # ── linguagens
    x, y, w, h = 606, 48, 582, 284
    o.append(janela(x, y, w, h))
    o.append(caixa(x + 14, y + 18, w - 28, h - 32, [('linguagens', C['magenta'], 700), (' · por bytes de código', C['comment'])]))
    ling = m['linguagens'][:6]
    resto = 1 - sum(p for _, p in ling)
    if resto > 0.0005:
        ling = ling + [('outras', resto)]
    paleta_l = [C['blue'], C['magenta'], C['cyan'], C['green'], C['yellow'], C['orange'], C['black']]
    cx, cy, r, lw = x + 152, y + 156, 74, 24
    circ = 2 * math.pi * r
    o.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{C["hl"]}" stroke-width="{lw}"/>')
    ac = 0.0
    seg = []
    for i, (nome, p) in enumerate(ling):
        comp = max(circ * p - 2.5, 0.8)
        seg.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{paleta_l[i % len(paleta_l)]}" stroke-width="{lw}" '
                   f'stroke-dasharray="{n(comp)} {n(circ)}" stroke-dashoffset="{n(-ac)}" transform="rotate(-90 {cx} {cy})"/>')
        ac += circ * p
    o.append(f'<mask id="revela"><circle class="anel" cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#fff" stroke-width="{lw + 4}" '
             f'stroke-dasharray="{n(circ + 1)} {n(circ)}" transform="rotate(-90 {cx} {cy})"/></mask>')
    o.append(f'<g mask="url(#revela)">{"".join(seg)}</g>')
    o.append(T(cx, cy + 4, len(m['linguagens']) or '—', C['fg'], 30, 800, 'middle'))
    o.append(T(cx, cy + 24, 'linguagens', C['comment'], 13, None, 'middle'))
    lx, ly = x + 284, y + 66
    for i, (nome, p) in enumerate(ling):
        yy = ly + i * 29
        c = paleta_l[i % len(paleta_l)]
        o.append(f'<rect x="{lx}" y="{yy - 10}" width="11" height="11" rx="3" fill="{c}"/>')
        o.append(T(lx + 20, yy, nome.lower(), C['fg'] if i < 3 else C['fg2'], 15, 700 if i == 0 else None))
        pct = f'{p * 100:.1f}'.replace('.', ',') + '%'
        o.append(T(x + w - 34, yy, pct, C['fg2'], 15, None, 'end'))
        o.append(f'<rect x="{lx + 20}" y="{yy + 6}" width="{n(170)}" height="3" rx="1.5" fill="{C["hl"]}"/>')
        o.append(f'<rect class="abre" style="animation-delay:{.2 + i * .08:.2f}s" x="{lx + 20}" y="{yy + 6}" width="{n(max(170 * p / max(ling[0][1], 1e-9), 2))}" height="3" rx="1.5" fill="{c}"/>')
    if not m['linguagens']:
        o.append(T(lx, ly + 40, 'aguardando o PAINEL_TOKEN', C['comment'], 13))

    # ── hábitos
    x, y, w, h = 12, 344, 582, 284
    o.append(janela(x, y, w, h))
    horas = m['horas']
    tot_h = sum(horas) or 1
    noite = sum(horas[i] for i in list(range(18, 24)) + list(range(0, 6))) / tot_h
    manha = sum(horas[6:12]) / tot_h
    tarde = sum(horas[12:18]) / tot_h
    if noite >= .5:
        perfil, pc = 'coruja noturna', C['magenta']
    elif manha >= max(tarde, noite):
        perfil, pc = 'madrugador', C['yellow']
    elif tarde >= max(manha, noite):
        perfil, pc = 'turno da tarde', C['orange']
    else:
        perfil, pc = 'ritmo equilibrado', C['cyan']
    utc = CFG.get('fuso_horas', -3)
    o.append(caixa(x + 14, y + 18, w - 28, h - 32, [('hábitos', C['cyan'], 700), (f' · commits por hora (UTC{utc:+d})'.replace('-', '−'), C['comment'])]))
    s, wc = chip(x + w - 34 - largura(perfil, 12.5) - 30, y + 34, perfil, pc, 12.5)
    o.append(s)
    base, alt = y + 222, 128
    mx = max(horas) or 1
    pico = horas.index(max(horas))
    bx0, passo, bw_ = x + 40, 15.2, 11
    for i, v in enumerate(horas):
        bh = max(alt * v / mx, 2 if v else 1)
        bx = bx0 + i * passo
        fill = C['magenta'] if i == pico and v else ('url(#barra)' if v else C['dim'])
        o.append(f'<rect class="sobe" style="animation-delay:{i * .035:.3f}s" x="{n(bx)}" y="{n(base - bh)}" width="{bw_}" height="{n(bh)}" rx="2" fill="{fill}"/>')
    o.append(f'<line x1="{bx0 - 6}" y1="{base + 1}" x2="{n(bx0 + 24 * passo)}" y2="{base + 1}" stroke="{C["dim"]}"/>')
    for hh in (0, 6, 12, 18, 23):
        o.append(T(bx0 + hh * passo + bw_ / 2, base + 18, f'{hh:02d}h', C['comment'], 12.5, None, 'middle'))
    if max(horas):
        px = bx0 + pico * passo + bw_ / 2
        o.append(T(px, base - alt - 8, f'pico {pico:02d}h', C['magenta'], 13, 700, 'middle'))
    o.append(T(bx0, y + h - 22, f'{round(noite * 100)}% dos commits entre 18h e 6h', C['fg2'], 13.5))
    sx, sy = x + 438, y + 104
    sem = m['semana']
    smx = max(sem) or 1
    o.append(T(sx, sy - 14, 'semana', C['comment'], 12.5))
    for i, v in enumerate(sem):
        yy = sy + i * 21
        o.append(T(sx, yy + 4, SEMANA_LONGA[i], C['fg2'], 13))
        o.append(f'<rect x="{sx + 36}" y="{yy - 4}" width="74" height="7" rx="3.5" fill="{C["hl"]}"/>')
        if v:
            o.append(f'<rect class="abre" style="animation-delay:{.3 + i * .06:.2f}s" x="{sx + 36}" y="{yy - 4}" width="{n(max(74 * v / smx, 4))}" height="7" rx="3.5" fill="{C["cyan"] if v != smx else C["magenta"]}"/>')

    # ── streak
    x, y, w, h = 606, 344, 582, 284
    o.append(janela(x, y, w, h))
    o.append(caixa(x + 14, y + 18, w - 28, h - 32, [('streak', C['orange'], 700), (' · dias seguidos com contribuição', C['comment'])]))
    cols = [x + 104, x + 291, x + 478]
    d0, d1 = (m['dias'][0][0], m['dias'][-1][0]) if m['dias'] else (None, None)
    def mesano(d):
        return f'{MESES[d.month - 1]}/{d.strftime("%y")}' if d else '—'
    # últimos 7 dias
    sete = m['dias'][-7:]
    o.append(T(cols[0], y + 136, num(sum(c for _, c in sete)), C['fg'], 40, 800, 'middle'))
    o.append(T(cols[0], y + 164, 'últimos 7 dias', C['fg2'], 14.5, None, 'middle'))
    o.append(T(cols[0], y + 186, f'{ddmm(sete[0][0])} → {ddmm(sete[-1][0])}' if sete else '—', C['comment'], 12, None, 'middle'))
    # atual (anel)
    rec = m['maior'][2] if m['maior'] else 0
    o.append(anel(cols[1], y + 140, 56, 9, (m['atual'] / rec) if rec else 0, 'url(#fogo)'))
    o.append(f'<path transform="translate({cols[1] - 11} {y + 64}) scale(1.1)" d="M10 0C11 6 18 8 18 15A8 8 0 0 1 2 15C2 11 4 9 6 7C6 10 8 11 9 11C9 7 7 4 10 0Z" fill="url(#fogo)"/>')
    o.append(T(cols[1], y + 150, num(m['atual']), C['fg'], 36, 800, 'middle'))
    o.append(T(cols[1], y + 170, 'dias', C['comment'], 13, None, 'middle'))
    o.append(T(cols[1], y + 222, 'streak atual', C['orange'], 14.5, 700, 'middle'))
    o.append(T(cols[1], y + 242, f"{ddmm(m['atual_ini'])} → {ddmm(m['atual_fim'])}" if m['atual'] else '—', C['comment'], 12, None, 'middle'))
    # maior
    mi = m['maior']
    o.append(T(cols[2], y + 136, num(mi[2] if mi else 0), C['fg'], 40, 800, 'middle'))
    o.append(T(cols[2], y + 164, 'maior streak', C['fg2'], 14.5, None, 'middle'))
    o.append(T(cols[2], y + 186, f'{ddmm(mi[0])} → {ddmm(mi[1])}' if mi else '—', C['comment'], 12, None, 'middle'))
    for sx in (x + 196, x + 386):
        o.append(f'<line x1="{sx}" y1="{y + 70}" x2="{sx}" y2="{y + 236}" stroke="{C["hl"]}" stroke-dasharray="3 5"/>')

    return desktop(W, H, 2, ''.join(o), 'Jodan Carvalho — workspace 2',
                   f"Estatísticas do GitHub: {m['total']} contribuições e {m['commits']} commits em 12 meses, "
                   f"linguagens, hábitos de commit e streak atual de {m['atual']} dias.",
                   relogio(m), estilo_extra=estilo, defs_extra=defs)


# ═══════════════════════════════ workspace 3 ═══════════════════════════════

def workspace3(m) -> str:
    W, H = 1200, 600
    o = []
    x, y, w, h = 12, 48, 1176, 320
    gx0, gx1 = x + 64, x + w - 30
    gtop, gbase = y + 78, y + 266
    defs = (f'<defs><linearGradient id="calor" gradientUnits="userSpaceOnUse" x1="0" y1="{gbase}" x2="0" y2="{gtop}">'
            f'<stop offset="0" stop-color="{C["blue"]}"/><stop offset=".55" stop-color="{C["magenta"]}"/>'
            f'<stop offset="1" stop-color="{C["red"]}"/></linearGradient>'
            f'<pattern id="scan" width="4" height="3" patternUnits="userSpaceOnUse"><rect width="4" height="1" fill="{C["bg"]}"/></pattern></defs>')
    o.append(janela(x, y, w, h, ativa=True))
    janela_dias = int(CFG.get('atividade_dias', 180))
    dias = m['dias'][-janela_dias:]
    rot_janela = f'{janela_dias // 30} meses' if janela_dias % 30 == 0 else f'{janela_dias} dias'
    o.append(caixa(x + 14, y + 18, w - 28, h - 32, [('atividade', C['red'], 700), (f' · contribuições por dia · {rot_janela}', C['comment'])]))
    tot_j = sum(c for _, c in dias)
    md = max(dias, key=lambda z: (z[1], z[0])) if dias else (hoje_local(), 0)
    media = f"{tot_j / max(len(dias), 1):.2f}".replace('.', ',')
    o.append(TS(x + w - 34, y + 50, [(num(tot_j), C['fg'], 700), (' contribuições  ·  média ', C['comment']),
                                      (media, C['fg'], 700), ('/dia  ·  pico ', C['comment']),
                                      (num(md[1]), C['fg'], 700), (f' em {ddmm(md[0])}', C['comment'])], 14, 'end'))
    mx = max((c for _, c in dias), default=0) or 1
    nd = len(dias)
    passo = (gx1 - gx0) / max(nd, 1)
    bw_ = max(passo * .72, 1.2)
    for frac, lbl in ((1, mx), (.5, mx / 2), (0, 0)):
        yy = gbase - (gbase - gtop) * frac
        o.append(f'<line x1="{gx0 - 6}" y1="{n(yy)}" x2="{gx1}" y2="{n(yy)}" stroke="{C["hl"]}" stroke-dasharray="2 6"/>')
        o.append(T(gx0 - 14, yy + 4, f'{lbl:.0f}' if lbl == int(lbl) else f'{lbl:.1f}'.replace('.', ','), C['comment'], 12.5, None, 'end'))
    barras, zeros = [], []
    for i, (d, c) in enumerate(dias):
        bx = gx0 + i * passo
        if c > 0:
            bh = max((gbase - gtop) * c / mx, 4)
            barras.append(f'<rect x="{n(bx)}" y="{n(gbase - bh)}" width="{n(bw_)}" height="{n(bh)}"/>')
        else:
            zeros.append(f'<rect x="{n(bx)}" y="{n(gbase - 1.5)}" width="{n(bw_)}" height="1.5"/>')
    o.append(f'<g fill="{C["dim"]}">{"".join(zeros)}</g>')
    o.append(f'<g fill="url(#calor)">{"".join(barras)}</g>')
    o.append(f'<rect x="{gx0}" y="{gtop - 4}" width="{gx1 - gx0}" height="{gbase - gtop + 4}" fill="url(#scan)" opacity=".85"/>')
    vistos = set()
    for i, (d, c) in enumerate(dias):
        if d.day == 1 and (d.year, d.month) not in vistos:
            vistos.add((d.year, d.month))
            bx = gx0 + i * passo
            o.append(f'<line x1="{n(bx)}" y1="{gbase + 4}" x2="{n(bx)}" y2="{gbase + 9}" stroke="{C["comment"]}"/>')
            rot = MESES[d.month - 1] + (f"/{d.strftime('%y')}" if d.month == 1 else '')
            o.append(T(bx + 3, gbase + 24, rot, C['comment'], 12.5))
    if dias:
        lx = gx0 + (nd - 1) * passo + bw_ / 2
        lc = dias[-1][1]
        ly = gbase - (max((gbase - gtop) * lc / mx, 4) if lc else 1.5)
        o.append(f'<line x1="{n(lx)}" y1="{gtop - 8}" x2="{n(lx)}" y2="{gbase}" stroke="{C["green"]}" stroke-opacity=".5" stroke-dasharray="2 4"/>')
        o.append(f'<circle class="vivo" cx="{n(lx)}" cy="{n(ly)}" r="4.5" fill="{C["green"]}"/>')
        o.append(T(lx - 8, gtop - 12, 'hoje', C['green'], 12.5, 700, 'end'))

    # ── stack (eza)
    x, y, w, h = 12, 380, 1176, 208
    o.append(janela(x, y, w, h))
    px = x + 28
    o.append(prompt(px, y + 38, '~', 'eza --icons --grid ~/stack', tam=16))
    itens = CFG['stack']
    colunas = 8
    cw_ = (w - 56) / colunas
    for i, (slug, nome, c) in enumerate(itens):
        col, lin = i % colunas, i // colunas
        ix, iy = px + col * cw_, y + 74 + lin * 44
        caminho = ICONES.get(slug)
        if caminho:
            o.append(f'<svg x="{n(ix)}" y="{n(iy - 2)}" width="20" height="20" viewBox="0 0 24 24"><path d="{caminho}" fill="{cor(c)}"/></svg>')
        o.append(T(ix + 30, iy + 14, nome, C['fg2'], 15))
    o.append(prompt(px, y + h - 22, '~', '', cursor=True, tam=16))
    o.append(T(x + w - 30, y + h - 22, f'{len(itens)} itens', C['comment'], 13, None, 'end'))

    return desktop(W, H, 3, ''.join(o), 'Jodan Carvalho — workspace 3',
                   f"Gráfico estilo btop das contribuições diárias recentes ({m['total']} em 12 meses) "
                   'e a stack de tecnologias listada como no eza.', relogio(m), defs_extra=defs)


# ═══════════════════════════════ main ═══════════════════════════════

def main():
    dados = json.loads(CACHE.read_text(encoding='utf-8')) if CACHE.exists() else {}
    if '--offline' not in sys.argv:
        garantir_icones()
        dados = coletar(dados)
        CACHE.write_text(json.dumps(dados, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    if not dados.get('calendario'):
        log('sem dados de calendário; nada a gerar')
        return 1
    m = metricas(dados)
    SAIDA.mkdir(exist_ok=True)
    for nome, fn in (('workspace-1.svg', workspace1), ('workspace-2.svg', workspace2), ('workspace-3.svg', workspace3)):
        svg = fn(m)
        (SAIDA / nome).write_text(svg, encoding='utf-8')
        log(f'{nome}: {len(svg.encode()) / 1024:.1f} KB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
