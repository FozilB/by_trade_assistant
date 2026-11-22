"""Помощник по сделкам OKX в Telegram.

Вы сообщаете о входе (форма в Telegram или команда /in), бот отвечает планом:
вердикт (по сетапу вход или нет), стоп по графику, цели, суммы в долларах, ликвидация, предупреждения.
Потом сопровождает сделку до закрытия: безубыток, защита прибыли, цель 1 (закрыть 50%), цель 2,
стоп по новым максимумам/минимумам 1ч, Hammer/Star против сделки, перепроданность, открытие биржи у токенов акций.

Один запуск держит связь с Telegram RUN_SECONDS секунд (около 4.5 минут), затем начинается следующий.

Команды:
  /in aero short 0.553 x20 10 [стоп 0.57]   — я вошёл (монета, шорт/лонг, вход, плечо, маржа $)
  /analyze aero  (или просто: aero)         — разбор монеты без входа
  /status                                   — все открытые сделки
  /stop aero 0.56                           — изменить стоп
  /out aero [цена]                          — закрыть сопровождение
  /menu                                     — кнопки и форма
"""
import json
import os
import re
import sys
import time

import requests

import assistant_config as A
import shortsetup as S
import zones as Z

C = S.C
H1, M15, M1 = S.H1, S.M15, 60_000
STATE_FILE = os.environ.get("STATE_FILE", "state/assistant.json")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
log = S.log
price = S.price


def now_ms():
    return int(time.time() * 1000)


def webapp_url():
    if os.environ.get("WEBAPP_URL"):
        return os.environ["WEBAPP_URL"]
    repo = os.environ.get("GITHUB_REPOSITORY", "FozilB/by_trade_assistant")
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


# ================================================================ Telegram

def tg(method, payload, timeout=30):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=timeout + 10)
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"  Telegram {method}: ошибка {e}")
        return None
    if not j.get("ok"):
        log(f"  Telegram {method}: {j.get('error_code')} {j.get('description')}")
        return None
    return j.get("result")


def tg_file(method, data, files, timeout=60):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, files=files, timeout=timeout)
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"  Telegram {method}: ошибка {e}")
        return None
    if not j.get("ok"):
        log(f"  Telegram {method}: {j.get('error_code')} {j.get('description')}")
        return None
    return j.get("result")


def send_photo(png, caption=""):
    data = {"chat_id": CHAT, "caption": caption, "parse_mode": "HTML"}
    return tg_file("sendPhoto", data, {"photo": ("chart.png", png, "image/png")}) is not None


def say(text, markup=None):
    p = {"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    return tg("sendMessage", p) is not None


def menu_markup():
    return {"keyboard": [[{"text": "➕ Новая сделка / анализ", "web_app": {"url": webapp_url()}}],
                         [{"text": "📊 Статус"}, {"text": "❓ Помощь"}]],
            "resize_keyboard": True, "is_persistent": True}


def pos_markup(inst):
    b = S.base_of(inst)
    return {"inline_keyboard": [[{"text": "📊 Статус", "callback_data": f"st:{b}"},
                                 {"text": "🛑 Стоп", "callback_data": f"sl:{b}"},
                                 {"text": "✅ Закрыть", "callback_data": f"out:{b}"}]]}


HELP = ("🤖 <b>Помощник по сделкам</b>\n\n"
        "💡 <b>Перед входом напишите название монеты</b> (например <code>aero</code>) — "
        "бот скажет, стоит ли входить.\n\n"
        "<b>➕ Новая сделка / анализ</b> — кнопка внизу открывает форму.\n\n"
        "Или командами:\n"
        "<code>/in aero short 0.553 x20 10</code> — вошёл: монета, шорт/лонг, вход, плечо, маржа $\n"
        "   можно добавить свой стоп: <code>стоп 0.57</code>\n"
        "<code>aero</code> или <code>/levels aero</code> — разбор: зоны, что ждать, Star/Hammer, картинка\n"
        "<code>/status</code> — открытые сделки\n"
        "<code>/stop aero 0.56</code> — изменить стоп\n"
        "<code>/out aero</code> — закрыть сопровождение\n"
        "<code>/day</code> — итог дня: сделки, убытки, можно ли ещё входить\n\n"
        "🧯 <b>Правила дисциплины</b>: пауза после убытка, не больше "
        f"{A.MAX_LOSSES_PER_DAY} убытков в день, без доливок в минус, стоп не отодвигается дальше, "
        f"стоп по времени ({A.TIME_STOP_HOURS:g} ч на месте — совет выйти), "
        f"стоп не больше {A.MAX_LOSS_PCT_OF_MARGIN:g}% маржи. Если правило нарушено — бот спросит подтверждение.\n\n"
        "Числа можно писать с запятой. Ответ обычно за несколько секунд; "
        "раз в ~5 минут бывает пауза до минуты, пока перезапускается бот.")


# ================================================================ Разбор ввода

def num(s):
    s = str(s).strip().replace(",", ".").replace("ю", ".").replace("б", ".")
    return float(s)


def norm_inst(sym):
    s = sym.strip().upper().replace("/", "").replace(" ", "")
    for suf in (".P", "-USDT-SWAP", "-SWAP", "-USDT", "USDT"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    s = s.rstrip("-")
    if not re.fullmatch(r"[A-Z0-9]{1,20}", s):
        raise ValueError(f"не понял монету «{sym}»")
    return f"{s}-USDT-SWAP"


SIDE = {"short": "short", "шорт": "short", "s": "short", "sell": "short", "ш": "short",
        "long": "long", "лонг": "long", "l": "long", "buy": "long", "л": "long"}


def parse_in(args):
    """/in aero short 0.553 x20 10 [стоп 0.57] -> dict. Порядок после монеты почти любой."""
    if not args:
        raise ValueError("нужно: /in монета шорт/лонг вход x плечо маржа")
    out = {"sym": args[0], "side": None, "entry": None, "lev": None, "margin": None, "stop": None}
    rest, i = args[1:], 0
    plain = []
    while i < len(rest):
        t = rest[i].lower()
        if t in SIDE:
            out["side"] = SIDE[t]
        elif t in ("стоп", "stop", "sl", "с"):
            out["stop"] = num(rest[i + 1])
            i += 1
        elif re.fullmatch(r"[xх×]\d+([.,]\d+)?|\d+([.,]\d+)?[xх×]", t):
            out["lev"] = num(t.strip("xх×"))
        elif t.startswith("$") or t.endswith("$"):
            out["margin"] = num(t.strip("$"))
        else:
            plain.append(num(t))
        i += 1
    if plain:
        out["entry"] = plain[0]
    if len(plain) > 1 and out["margin"] is None:
        out["margin"] = plain[1]
    out["side"] = out["side"] or "short"
    if not out["entry"] or out["entry"] <= 0:
        raise ValueError("не вижу цену входа")
    return out


# ================================================================ Данные и расчёт

def load_market(inst):
    cs4 = S.fetch_candles(inst, "4H", 150)
    cs1 = S.fetch_candles(inst, "1H", 120)
    cs15 = S.fetch_candles(inst, "15m", 100)
    if not cs4 or not cs1 or not cs15:
        raise ValueError(f"OKX не отдал свечи {inst} — проверьте название монеты")
    return {"c4": [x for x in cs4 if x["closed"]], "c1": [x for x in cs1 if x["closed"]],
            "c15": [x for x in cs15 if x["closed"]], "px": cs15[-1]["c"], "c1_all": cs1}


def chart_stop(side, entry, c15):
    a15 = S.atr(c15) or entry * 0.003
    win = c15[-A.STOP_PIVOT_BARS_15M:]
    highs, lows = S.pivots(win, 2)
    buf = A.STOP_BUFFER_ATR15 * a15
    if side == "short":
        cands = [p for _, p in highs if p > entry + 0.3 * a15]
        base = min(cands) if cands else max(x["h"] for x in c15[-16:])
        recent = max(x["h"] for x in c15[-4:])
        base = max(base, recent)
        stop = max(base + buf, entry + A.MIN_STOP_ATR15 * a15)
    else:
        cands = [p for _, p in lows if p < entry - 0.3 * a15]
        base = max(cands) if cands else min(x["l"] for x in c15[-16:])
        recent = min(x["l"] for x in c15[-4:])
        base = min(base, recent)
        stop = min(base - buf, entry - A.MIN_STOP_ATR15 * a15)
    return stop, a15


def levels(side, entry, m):
    """Поддержки (для шорта, ниже входа) или сопротивления (для лонга, выше) по 1ч и 4ч, от ближних к дальним."""
    h1, l1 = S.pivots(m["c1"][-120:], 3)
    h4, l4 = S.pivots(m["c4"][-100:], 3)
    if side == "short":
        return sorted({p for _, p in l1 + l4 if p < entry}, reverse=True)
    return sorted({p for _, p in h1 + h4 if p > entry})


def targets(side, entry, r, m):
    a1 = S.atr(m["c1"]) or r
    sgn = -1 if side == "short" else 1
    lv = [x for x in levels(side, entry, m) if abs(x - entry) >= A.TARGET_MIN_R * r]
    off = -sgn * 0.1 * a1  # цель чуть раньше уровня
    if lv:
        t1, note = lv[0] + off, "уровень " + price(lv[0])
        more = [x for x in lv[1:] if abs(x - entry) >= abs(t1 - entry) + 0.5 * r]
        t2 = more[0] + off if more else entry + sgn * max(A.FALLBACK_T2_R * r, 1.6 * abs(t1 - entry))
    else:
        t1, note = entry + sgn * A.FALLBACK_T1_R * r, f"уровня нет, {A.FALLBACK_T1_R:g}R"
        t2 = entry + sgn * A.FALLBACK_T2_R * r
    return t1, t2, note


def liq_price(side, entry, lev):
    if not lev:
        return None
    mm = A.MAINT_MARGIN_PCT / 100
    return entry * (1 + 1 / lev - mm) if side == "short" else entry * (1 - 1 / lev + mm)


def warnings(inst, side, entry, stop, r, liq, m):
    w = []
    sgn = 1 if side == "short" else -1
    if liq is not None and (liq - stop) * sgn <= 0:
        dist = abs(stop - entry) / entry
        safe = int(1 / (dist + A.MAINT_MARGIN_PCT / 100 + 0.005))
        w.append(f"🚨 Ликвидация {price(liq)} <b>раньше стопа</b>! Плечо не выше x{max(safe, 1)}.")
    c1, c15 = m["c1"], m["c15"]
    if side == "short":
        if any(S.is_hammer(c1, len(c1) - 1 - j) for j in range(3)) or any(S.is_hammer(c15, len(c15) - 1 - j) for j in range(4)):
            w.append("⚠️ Свежий Hammer — возможен отскок вверх.")
    else:
        if any(S.is_star(c1, len(c1) - 1 - j, 10) for j in range(3)) or any(S.is_star(c15, len(c15) - 1 - j, 10) for j in range(4)):
            w.append("⚠️ Свежая Star — возможен откат вниз.")
    rsi15 = S.rsi_series([x["c"] for x in c15])[-1]
    if rsi15 is not None and ((side == "short" and rsi15 <= 30) or (side == "long" and rsi15 >= 70)):
        w.append(f"⚠️ RSI(15м) {rsi15:.0f} — {'перепроданность' if side == 'short' else 'перекупленность'}, вход у края движения.")
    lv = levels(side, entry, m)
    if lv and abs(lv[0] - entry) < r:
        w.append(f"⚠️ {'Поддержка' if side == 'short' else 'Сопротивление'} {price(lv[0])} ближе, чем стоп — для прибыли места мало.")
    day = c1[-24:]
    if day:
        if side == "short":
            moved = (max(x["h"] for x in day) - entry) / max(x["h"] for x in day) * 100
        else:
            moved = (entry - min(x["l"] for x in day)) / min(x["l"] for x in day) * 100
        if moved > A.CHASE_PCT:
            w.append(f"⚠️ Цена уже прошла {moved:.1f}% за сутки в вашу сторону — вход вдогонку.")
    sma = S.sma_last([x["c"] for x in m["c4"]], 50)
    if sma and ((side == "short" and entry > sma) or (side == "long" and entry < sma)):
        w.append(f"⚠️ Против тренда на 4ч: цена {'выше' if side == 'short' else 'ниже'} средней линии ({price(sma)}).")
    block = S.stock_block(inst, now_ms(), 60)
    if block:
        w.append(f"⚠️ Скоро {block} — возможен гэп.")
    return w


def make_plan(inst, side, entry, lev, margin, user_stop, m):
    stop_c, a15 = chart_stop(side, entry, m["c15"])
    notes = []
    if user_stop:
        if (user_stop - entry) * (1 if side == "short" else -1) <= 0:
            raise ValueError(f"стоп {price(user_stop)} не с той стороны от входа")
        stop = user_stop
        if abs(user_stop - entry) < A.MIN_STOP_ATR15 * a15:
            notes.append(f"⚠️ Ваш стоп ближе обычного шума (1 ATR 15м = {abs(a15 / entry * 100):.2f}%). "
                         f"По графику: {price(stop_c)}.")
    else:
        stop = stop_c
    r = abs(stop - entry)
    t1, t2, t1_note = targets(side, entry, r, m)
    notional = lev * margin if lev and margin else None
    qty = notional / entry if notional else None
    liq = liq_price(side, entry, lev)
    return {"inst": inst, "side": side, "entry": entry, "lev": lev, "margin": margin, "stop": stop, "stop0": stop,
            "r0": r, "t1": t1, "t2": t2, "t1_note": t1_note, "qty": qty, "notional": notional, "liq": liq,
            "warn": notes + warnings(inst, side, entry, stop, r, liq, m), "stop_chart": stop_c, "user_stop": bool(user_stop)}


def usd(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):.2f}"


# Перевод причин сканера на простой язык
PLAIN = (
    ("MA50", "на 4ч нет падения: цена выше средней линии"),
    ("вне диапазона", "отскок слишком большой — это уже не отскок"),
    ("поздно", "цена уже сильно упала, вход поздний"),
    ("Hammer", "рядом Hammer — возможен отскок вверх"),
    ("мало места", "до поддержки близко, прибыли будет мало"),
    ("не у сопротивления", "цена не у сопротивления"),
    ("свечи-отказа", "нет свечи-разворота вниз на 1ч"),
    ("RSI", "рост ещё не выдохся"),
    ("максимум", "на 4ч нет нисходящего тренда"),
)


def plain_reason(d):
    t = str(d)
    for key, text in PLAIN:
        if key in t:
            return text
    return t


def verdict(p, m):
    """Одна строка вверху: по сетапу вход или нет, и почему."""
    e = p["entry"]
    stop_pct = abs(p["stop"] - e) / e * 100
    far = stop_pct > A.FAR_STOP_PCT
    bad, good = [], None
    if m is not None and p["side"] == "short":
        kind, d = S.detect(m["c4"], m["c1"], e, m["c15"])
        if kind == "signal":
            good = "нисходящий тренд, цена у сопротивления, есть свеча-разворот"
        elif kind == "watch":
            bad.append("цена у сопротивления, но свечи-разворота ещё нет — рано")
        else:
            bad.append(plain_reason(d))
    if bad:
        text = "⛔ <b>Вход не по сетапу</b>\n" + "\n".join(f"• {x}" for x in bad)
        if far:
            text += f"\n• стоп далеко — {stop_pct:.1f}% от входа"
        return text
    if far:
        return f"⛔ <b>Стоп слишком далеко ({stop_pct:.1f}%) — место для входа плохое</b>"
    if good:
        return f"✅ <b>Вход по сетапу</b>: {good}"
    return None


def fmt_plan(p, m=None):
    side, e = p["side"], p["entry"]
    b = S.base_of(p["inst"])
    arrow = "🔻 SHORT" if side == "short" else "🟢 LONG"
    head = f"{arrow} <b>{b}</b> · вход {price(e)}"
    if p["lev"] and p["margin"]:
        head += f" · x{p['lev']:g} · маржа ${p['margin']:g} · позиция ${p['notional']:.0f}"
    lines = []
    v = verdict(p, m)
    if v:
        lines += [v, ""]
    lines += [head, ""]
    q = p["qty"]
    stop_pct = abs(p["stop"] - e) / e * 100
    loss = (f" → потеряете ${q * p['r0']:.2f} ({q * p['r0'] / p['margin'] * 100:.0f}% маржи)") if q else ""
    src = "ваш" if p["user_stop"] else "за ближайшим максимумом на 15м"
    lines.append(f"🛑 <b>Стоп: {price(p['stop'])}</b> ({S.pct(p['stop'], e):+.2f}%, {src}){loss}")
    if p["margin"] and stop_pct > A.FAR_STOP_PCT:
        limit = p["margin"] * A.RISK_PCT_OF_MARGIN / 100
        size = limit / (stop_pct / 100)
        extra = f" (маржа ${size / p['lev']:.1f} при x{p['lev']:g})" if p["lev"] else ""
        lines.append(f"💡 Чтобы потерять не больше ${limit:.2f} ({A.RISK_PCT_OF_MARGIN:g}% маржи), "
                     f"позиция должна быть около ${size:.0f}{extra}.")
    half = A.TAKE_AT_T1_PCT / 100
    g1 = f" → +${q * abs(e - p['t1']) * half:.2f}" if q else ""
    g2 = f" → ещё +${q * abs(e - p['t2']) * (1 - half):.2f}" if q else ""
    lines.append(f"🎯 <b>Цель 1: {price(p['t1'])}</b> ({S.pct(p['t1'], e):+.2f}%) — закройте половину{g1}")
    lines.append(f"🎯 <b>Цель 2: {price(p['t2'])}</b> ({S.pct(p['t2'], e):+.2f}%){g2}")
    lines.append(f"На каждый $1 риска — около ${abs(p['t1'] - e) / p['r0']:.1f} прибыли.")
    if p["liq"]:
        ok = (p["liq"] - p["stop"]) * (1 if side == "short" else -1) > 0
        lines.append(f"Ликвидация ≈ {price(p['liq'])} {'✅ дальше стопа' if ok else '🚨 РАНЬШЕ СТОПА'}")
    if p["warn"]:
        lines += [""] + p["warn"]
    lines += ["", "Слежу за ценой: напишу, когда переносить стоп и когда забирать прибыль."]
    return "\n".join(lines)


def analyze(inst, m=None):
    m = m or load_market(inst)
    px = m["px"]
    b = S.base_of(inst)
    c4, c1 = m["c4"], m["c1"]
    rsi1 = S.rsi_series([x["c"] for x in c1])[-1]
    rsi4 = S.rsi_series([x["c"] for x in c4])[-1]
    zlines, z = Z.report(m)
    lines = [f"🔍 <b>{b}</b> · цена {price(px)}",
             f"RSI: 1ч {rsi1:.0f} · 4ч {rsi4:.0f}" if rsi1 and rsi4 else "RSI: —", ""] + zlines
    if z["trend"] != "up":
        kind, d = S.detect(c4, c1, px, m["c15"])
        if kind == "signal":
            lines += ["", "✅ <b>Сейчас есть сетап «шорт от нижнего максимума»</b>"]
        elif kind == "watch":
            lines += ["", f"👀 Сетап сканера: цена у сопротивления {price(d['level'][1])} — ждать свечу-разворот на 1ч"]
        else:
            lines += ["", f"⛔ Сетап сканера для шорта: нет — {plain_reason(d)}"]
    side = "long" if z["trend"] == "up" else "short"
    p = make_plan(inst, side, px, None, None, None, m)
    lines += ["", f"Если {'лонг' if side == 'long' else 'шорт'} прямо отсюда: стоп {price(p['stop'])} "
                  f"({S.pct(p['stop'], px):+.2f}%), цель 1 {price(p['t1'])} ({S.pct(p['t1'], px):+.2f}%)",
              f"<a href=\"{S.chart_link(inst)}\">График TradingView</a>"]
    return "\n".join(lines), z


def cmd_analyze(inst):
    """Разбор монеты по запросу: картинка с зонами и текст."""
    m = load_market(inst)
    text, z = analyze(inst, m)
    try:
        png = Z.chart_png(m["c1"], m["px"], z, f"{S.base_of(inst)} · 1ч · зоны поддержки и сопротивления")
        send_photo(png)
    except Exception as e:  # картинка не должна ломать разбор
        log(f"  График не построен: {e}")
    return say(text)


# ================================================================ Позиции

def pnl(pos, px):
    sgn = 1 if pos["side"] == "short" else -1
    move = (pos["entry"] - px) * sgn
    left = 1 - (A.TAKE_AT_T1_PCT / 100 if pos.get("t1_hit") else 0)
    usd_open = pos["qty"] * move * left if pos.get("qty") else None
    return move / pos["r0"], usd_open


def fmt_status(pos, px):
    r, u = pnl(pos, px)
    b = S.base_of(pos["inst"])
    flags = []
    if pos.get("be"):
        flags.append("безубыток")
    if pos.get("t1_hit"):
        flags.append(f"цель 1 взята ({usd(pos.get('banked', 0))})")
    s = (f"{'🔻' if pos['side'] == 'short' else '🟢'} <b>{b}</b> · цена {price(px)} · {r:+.2f}R"
         + (f" · {usd(u)}" if u is not None else "") + "\n"
         f"вход {price(pos['entry'])} · стоп {price(pos['stop'])} · цели {price(pos['t1'])} / {price(pos['t2'])}")
    return s + (f"\n{', '.join(flags)}" if flags else "")


def last_price(inst):
    cs = S.fetch_candles(inst, "1m", 1)
    return cs[-1]["c"] if cs else None


def local_day(ms):
    return int((ms / 1000 + A.DAY_TZ_HOURS * 3600) // 86400)


def remember_closed(state, pos, r, total):
    """Запоминает итог закрытой сделки — для правил дисциплины и /day."""
    if r is None:
        return
    loss = total < 0 if pos.get("qty") else r < 0
    state.setdefault("hist", []).append({"inst": pos["inst"], "t": now_ms(), "opened": pos.get("opened"), "r": round(r, 3),
                                         "usd": round(total, 2) if pos.get("qty") else None,
                                         "margin": pos.get("margin"), "loss": bool(loss)})
    state["hist"] = state["hist"][-A.HISTORY_KEEP:]


def close_pos(state, inst, px, why):
    pos = state["pos"].pop(inst)
    r, u = pnl(pos, px) if px else (None, None)
    total = (u or 0) + pos.get("banked", 0)
    if r is not None and pos.get("t1_hit"):
        r = (r * (1 - A.TAKE_AT_T1_PCT / 100)
             + abs(pos["entry"] - pos["t1"]) / pos["r0"] * A.TAKE_AT_T1_PCT / 100)
    remember_closed(state, pos, r, total)
    txt = f"✅ <b>{S.base_of(inst)}</b> · сопровождение закрыто ({why})"
    if px:
        txt += f" по {price(px)}"
        if pos.get("qty"):
            txt += f"\nИтог сделки ≈ {usd(total)}"
        if r is not None and r > 0 and why.startswith(("по вашей", "по кнопке")):
            for name, lvl in (("цели 1", pos["t1"]), ("цели 2", pos["t2"])):
                if abs(px - lvl) <= 0.3 * pos["r0"]:
                    txt += f"\n👍 Закрыто у {name} — выход по плану."
                    break
            else:
                nxt = pos["t2"] if pos.get("t1_hit") else pos["t1"]
                txt += f"\nЗакрыто раньше цели: до неё оставалось ещё {abs(nxt - px) / px * 100:.1f}%."
    say(txt)


def track(state, inst, now):
    """Проверка одной позиции по свечам с прошлой проверки. Возвращает False, если позиция закрыта."""
    pos = state["pos"][inst]
    b = S.base_of(inst)
    short = pos["side"] == "short"
    gap_min = (now - pos["checked"]) / M1
    bar, ms = ("1m", M1) if gap_min <= 290 else ("15m", M15)
    cs = S.fetch_candles(inst, bar, int(min(300, gap_min * M1 / ms + 3)))
    if not cs:
        return True
    q = pos.get("qty")
    half = A.TAKE_AT_T1_PCT / 100
    for x in cs:
        if x["ts"] + ms <= pos["checked"]:
            continue
        hit_stop = x["h"] >= pos["stop"] if short else x["l"] <= pos["stop"]
        best = x["l"] if short else x["h"]
        pos["best_r"] = max(pos.get("best_r", 0.0), (pos["entry"] - best) * (1 if short else -1) / pos["r0"])
        if hit_stop:
            if pos.get("be") or pos.get("t1_hit"):
                close_pos(state, inst, pos["stop"], f"стоп {price(pos['stop'])} задет, он был в безубытке или выше")
            else:
                close_pos(state, inst, pos["stop"], f"🛑 стоп {price(pos['stop'])} задет — по плану, без сожалений")
            return False
        reached = lambda lvl: best <= lvl if short else best >= lvl  # noqa: E731
        if not pos.get("be") and reached(pos["entry"] - (1 if short else -1) * A.BREAKEVEN_AT_R * pos["r0"]):
            pos["be"] = True
            pos["stop"] = pos["entry"]
            say(f"🔒 <b>{b}</b> · цена прошла в вашу сторону столько же, сколько вы рискуете. "
                f"Перенесите стоп в безубыток — на цену входа <b>{price(pos['entry'])}</b>.\n"
                f"Теперь убытка уже не будет.", pos_markup(inst))
        sgn = 1 if short else -1
        if pos.get("be") and not pos.get("locked") and reached(pos["entry"] - sgn * A.LOCK_AT_R * pos["r0"]):
            lock = pos["entry"] - sgn * 0.5 * A.LOCK_AT_R * pos["r0"]
            better = lock < pos["stop"] if short else lock > pos["stop"]
            if better:
                pos["locked"] = True
                pos["stop"] = lock
                left = 1 - (half if pos.get("t1_hit") else 0)
                gain = (f" Даже при развороте вы заберёте около +${q * abs(pos['entry'] - lock) * left:.2f}.") if q else ""
                say(f"🔐 <b>{b}</b> · прибыль уже вдвое больше риска. "
                    f"Перенесите стоп на <b>{price(lock)}</b>.{gain}", pos_markup(inst))
        if not pos.get("t1_hit") and reached(pos["t1"]):
            pos["t1_hit"] = True
            pos["banked"] = q * abs(pos["entry"] - pos["t1"]) * half if q else 0
            if not pos.get("be"):
                pos["be"], pos["stop"] = True, pos["entry"]
            say(f"🎯 <b>{b}</b> · цель 1 {price(pos['t1'])}! Закройте {A.TAKE_AT_T1_PCT}%"
                + (f" ({usd(pos['banked'])})" if q else "") + f", стоп — в безубыток {price(pos['entry'])}.\n"
                f"Остаток ведём к цели 2 {price(pos['t2'])}.", pos_markup(inst))
        if pos.get("t1_hit") and reached(pos["t2"]):
            close_pos(state, inst, pos["t2"], f"🎯🎯 цель 2 {price(pos['t2'])} — закройте остаток")
            return False
    px = cs[-1]["c"]
    near = abs(pos["stop"] - px) <= A.NEAR_STOP_R * pos["r0"] and not pos.get("be")
    if near and not pos.get("warned_near"):
        pos["warned_near"] = True
        say(f"⏳ <b>{b}</b> · цена {price(px)} близко к стопу {price(pos['stop'])}.\n"
            f"Не двигайте и не закрывайте раньше — стоп стоит за уровнем, это и есть план. Риск известен заранее.")
    elif abs(pos["stop"] - px) > 0.75 * pos["r0"]:
        pos["warned_near"] = False
    time_stop(pos, inst, px, now)
    pos["checked"] = now
    if now - pos.get("slow", 0) >= 5 * M1:
        pos["slow"] = now
        slow_checks(pos, inst, px, now)
    return True


def time_stop(pos, inst, px, now):
    """Стоп по времени: сделка давно открыта, а цена так и не пошла в нашу сторону — предложить выйти."""
    if pos.get("warn_time") or pos.get("be") or pos.get("t1_hit"):
        return
    hours = (now - pos["opened"]) / H1
    if hours < A.TIME_STOP_HOURS or pos.get("best_r", 0.0) >= A.TIME_STOP_MIN_R:
        return
    pos["warn_time"] = True
    b = S.base_of(inst)
    r, u = pnl(pos, px)
    now_txt = f"{r:+.2f}R" + (f" ({usd(u)})" if u is not None else "")
    say(f"⏰ <b>{b}</b> · сделка открыта {hours:.0f} ч, а цена так и не пошла в вашу сторону "
        f"(лучшее движение +{pos.get('best_r', 0.0):.2f}R, сейчас {now_txt}).\n"
        "Сделка не работает по плану. Спокойный выход сейчас — в ноль или с маленьким минусом — "
        "это правильное решение, а не слабость. Деньги, которые стоят на месте, — тоже риск.\n"
        f"Если оставляете — стоп не отодвигать и не доливать. Закрыли: <code>/out {b.lower()}</code>",
        pos_markup(inst))


def slow_checks(pos, inst, px, now):
    """Раз в ~5 минут: стоп по структуре 1ч, Hammer/Star против сделки, RSI, открытие биржи."""
    b = S.base_of(inst)
    short = pos["side"] == "short"
    cs1 = S.fetch_candles(inst, "1H", 60)
    if cs1:
        c1 = [x for x in cs1 if x["closed"]]
        a1 = S.atr(c1) or pos["r0"]
        if pos.get("be") and len(c1) > 10:
            highs, lows = S.pivots(c1, 2)
            after = [(i, p) for i, p in (highs if short else lows) if c1[i]["ts"] >= pos["opened"]]
            if after:
                lvl = after[-1][1]
                new = lvl + 0.1 * a1 if short else lvl - 0.1 * a1
                better = (pos["stop"] - new) if short else (new - pos["stop"])
                beyond = new > px if short else new < px
                if better >= A.TRAIL_MIN_STEP_R * pos["r0"] and beyond:
                    pos["stop"] = new
                    say(f"📉 <b>{b}</b> · новый {'нижний максимум' if short else 'повышающийся минимум'} 1ч {price(lvl)}.\n"
                        f"Подтяните стоп: <b>{price(new)}</b>.", pos_markup(inst))
        last = c1[-1] if c1 else None
        if last and last["ts"] >= pos["opened"] and pos.get("warn_c1") != last["ts"]:
            i = len(c1) - 1
            against = S.is_hammer(c1, i) if short else S.is_star(c1, i, 10)
            if against:
                pos["warn_c1"] = last["ts"]
                say(f"⚠️ <b>{b}</b> · на 1ч {'Hammer' if short else 'Star'} против сделки — возможен "
                    f"{'отскок вверх' if short else 'откат вниз'}. Можно зафиксировать часть или подтянуть стоп.")
    cs15 = S.fetch_candles(inst, "15m", 40)
    if cs15:
        r = S.rsi_series([x["c"] for x in cs15 if x["closed"]])[-1]
        ext = r is not None and ((short and r <= A.RSI_EXTREME) or (not short and r >= 100 - A.RSI_EXTREME))
        if ext and now - pos.get("warn_rsi", 0) > 2 * H1:
            pos["warn_rsi"] = now
            say(f"⚠️ <b>{b}</b> · RSI(15м) {r:.0f} — {'перепроданность' if short else 'перекупленность'}, "
                f"возможен отскок. Хороший момент забрать часть прибыли.")
    block = S.stock_block(inst, now, A.STOCK_WARN_MIN)
    if block and pos.get("warn_stock") != block + time.strftime("%Y%m%d"):
        pos["warn_stock"] = block + time.strftime("%Y%m%d")
        say(f"🔔 <b>{b}</b> · {block}. Возможен резкий гэп — проверьте стоп.")


# ================================================================ Команды

def today_stats(state, now):
    day = local_day(now)
    hist = [h for h in state.get("hist", []) if local_day(h["t"]) == day]
    losses = [h for h in hist if h["loss"]]
    usd_sum = sum(h["usd"] for h in hist if h.get("usd") is not None)
    return hist, losses, usd_sum


def discipline(state, p, now):
    """Нарушенные правила для нового входа: отыгрыш после убытка, лимит убытков, рост ставки, большое плечо."""
    out = []
    hist = state.get("hist", [])
    _, losses, usd_sum = today_stats(state, now)
    last_loss = max((h["t"] for h in hist if h["loss"]), default=None)
    if last_loss is not None:
        passed = (now - last_loss) / M1
        if passed < A.COOLDOWN_AFTER_LOSS_MIN:
            out.append(f"после убыточной сделки прошло всего {passed:.0f} мин (пауза {A.COOLDOWN_AFTER_LOSS_MIN} мин). "
                       "Вход сразу после убытка — это почти всегда отыгрыш.")
    if len(losses) >= A.MAX_LOSSES_PER_DAY:
        money = f", итог дня {usd(usd_sum)}" if any(h.get("usd") is not None for h in losses) else ""
        out.append(f"сегодня уже {len(losses)} убыточные сделки{money}. Лимит на день — {A.MAX_LOSSES_PER_DAY}.")
    margins = sorted(h["margin"] for h in hist[-10:] if h.get("margin"))
    if p["margin"] and len(margins) >= 3:
        usual = margins[len(margins) // 2]
        if p["margin"] >= A.SIZE_JUMP_X * usual:
            out.append(f"маржа ${p['margin']:g} — в {p['margin'] / usual:.1f} раза больше обычной (${usual:g}). "
                       "Увеличивать ставку, чтобы вернуть потерянное, — главный путь к сливу.")
    if p["qty"] and p["margin"]:
        loss_pct = p["qty"] * p["r0"] / p["margin"] * 100
        if loss_pct > A.MAX_LOSS_PCT_OF_MARGIN:
            stop_pct = p["r0"] / p["entry"] * 100
            ok_lev = max(1, int(A.MAX_LOSS_PCT_OF_MARGIN / stop_pct))
            out.append(f"на стопе вы потеряете {loss_pct:.0f}% маржи (лимит {A.MAX_LOSS_PCT_OF_MARGIN:g}%). "
                       f"Для стопа {stop_pct:.2f}% плечо не выше x{ok_lev}.")
    if p["liq"] is not None and (p["liq"] - p["stop"]) * (1 if p["side"] == "short" else -1) <= 0:
        out.append(f"ликвидация {price(p['liq'])} наступит раньше стопа — сделка закроется с потерей всей маржи.")
    return out


def confirm_markup(inst):
    b = S.base_of(inst)
    return {"inline_keyboard": [[{"text": "Не вхожу / уже закрыл", "callback_data": f"drop:{b}"}],
                                [{"text": "Я уже в сделке — всё равно вести", "callback_data": f"force:{b}"}]]}


def cmd_in(state, d, force=False):
    inst = norm_inst(d["sym"])
    b = S.base_of(inst)
    if inst in state["pos"]:
        pos = state["pos"][inst]
        px = last_price(inst)
        r = pnl(pos, px)[0] if px else 0
        if r < 0:
            return say(f"🚫 <b>{b}</b> · сделка уже открыта и сейчас в минусе ({r:+.2f}R).\n"
                       "Новый вход — это <b>доливка в убыток</b>: средняя цена лучше не станет, "
                       "а ликвидация подойдёт ближе. Бот такие доливки не сопровождает.\n"
                       "Если стоп по плану ещё не задет — просто держите стоп. Если план сломан — "
                       f"закройте позицию и напишите <code>/out {b.lower()}</code>.", pos_markup(inst))
        return say(f"По <b>{b}</b> уже есть сделка ({r:+.2f}R). Одна монета — одна сделка: "
                   f"сначала закройте её (<code>/out {b.lower()}</code>), потом новый вход.", pos_markup(inst))
    m = load_market(inst)
    lev = float(d["lev"]) if d.get("lev") else None
    margin = float(d["margin"]) if d.get("margin") else None
    p = make_plan(inst, d["side"], float(d["entry"]), lev, margin, float(d["stop"]) if d.get("stop") else None, m)
    now = now_ms()
    broken = discipline(state, p, now)
    if broken and not force:
        state.setdefault("pending", {})[inst] = dict(d, ts=now)
        lines = [f"🧯 <b>{b}</b> · <b>стоп, нарушены правила</b>:"] + [f"• {x}" for x in broken]
        lines += ["", "Если вы ещё не вошли — не входите. Если уже вошли — лучшее решение сейчас: "
                      "закрыть позицию с маленьким минусом, пока он маленький.",
                  "Сделку не веду, пока не нажмёте кнопку."]
        return say("\n".join(lines), confirm_markup(inst))
    if broken:
        p["warn"] = ["🧯 Вход вопреки правилам: " + "; ".join(broken)] + p["warn"]
        p["forced"] = True
    say(fmt_plan(p, m), pos_markup(inst))
    p.update({"opened": now, "checked": now, "slow": now})
    p.pop("warn", None)
    state["pos"][inst] = p
    state.get("pending", {}).pop(inst, None)


def cmd_day(state):
    now = now_ms()
    hist, losses, usd_sum = today_stats(state, now)
    lines = ["📅 <b>Итог дня</b>"]
    if not hist:
        lines.append("Закрытых сделок сегодня нет.")
    else:
        for h in hist:
            money = f" · {usd(h['usd'])}" if h.get("usd") is not None else ""
            lines.append(f"{'❌' if h['loss'] else '✅'} {S.base_of(h['inst'])} · {h['r']:+.2f}R{money}")
        if any(h.get("usd") is not None for h in hist):
            lines.append(f"Всего: <b>{usd(usd_sum)}</b>")
    last_loss = max((h["t"] for h in state.get("hist", []) if h["loss"]), default=None)
    wait = A.COOLDOWN_AFTER_LOSS_MIN - (now - last_loss) / M1 if last_loss else 0
    if len(losses) >= A.MAX_LOSSES_PER_DAY:
        lines.append(f"\n⛔ Убытков сегодня: {len(losses)} из {A.MAX_LOSSES_PER_DAY}. На сегодня торговля закончена.")
    elif wait > 0:
        lines.append(f"\n⏸ Пауза после убытка: ещё {wait:.0f} мин.")
    else:
        lines.append(f"\n🟢 Можно входить по плану. Убытков сегодня: {len(losses)} из {A.MAX_LOSSES_PER_DAY}.")
    say("\n".join(lines))


def find_pos(state, sym):
    inst = norm_inst(sym)
    if inst not in state["pos"]:
        raise ValueError(f"сделки по {S.base_of(inst)} нет. Открытые: "
                         + (", ".join(S.base_of(k) for k in state["pos"]) or "нет"))
    return inst


def cmd_status(state, only=None):
    if not state["pos"]:
        say("Открытых сделок нет. Нажмите «➕ Новая сделка / анализ» или напишите /in.", menu_markup())
        return
    for inst, pos in list(state["pos"].items()):
        if only and inst != only:
            continue
        px = last_price(inst)
        say(fmt_status(pos, px) if px else f"{S.base_of(inst)}: нет цены с OKX", pos_markup(inst))


def handle_text(state, text):
    t = text.strip()
    if not t:
        return
    low = t.lower()
    if low in ("📊 статус",):
        return cmd_status(state)
    if low in ("❓ помощь",):
        return say(HELP, menu_markup())
    parts = t.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]
    if cmd in ("/start", "/menu", "/help", "меню", "помощь"):
        return say(HELP, menu_markup())
    if cmd in ("/in", "in", "/вход", "вход"):
        return cmd_in(state, parse_in(args))
    if cmd in ("/analyze", "/a", "/анализ", "анализ", "/levels", "/уровни", "уровни"):
        if not args:
            raise ValueError("какую монету? Например: /analyze aero")
        return cmd_analyze(norm_inst(args[0]))
    if cmd in ("/status", "/s", "статус"):
        return cmd_status(state, find_pos(state, args[0]) if args else None)
    if cmd in ("/stop", "/sl", "стоп"):
        if len(args) < 2:
            raise ValueError("нужно: /stop монета цена")
        inst = find_pos(state, args[0])
        pos = state["pos"][inst]
        new = num(args[1])
        sgn = 1 if pos["side"] == "short" else -1
        if (new - pos["entry"]) * sgn < 0 and not pos.get("be"):
            raise ValueError("стоп не с той стороны от входа")
        if (new - pos["stop"]) * sgn > 0:
            return say(f"🚫 <b>{S.base_of(inst)}</b> · стоп {price(pos['stop'])} отодвигать дальше нельзя "
                       f"(вы хотите {price(new)}) — так убыток только растёт.\n"
                       "Стоп стоит там, где план перестаёт работать. Если цена туда дошла — план не сработал, "
                       "и выйти по стопу — правильно.", pos_markup(inst))
        pos["stop"] = new
        pos["warned_near"] = False
        return say(f"🛑 <b>{S.base_of(inst)}</b> · стоп теперь {price(new)}.", pos_markup(inst))
    if cmd in ("/day", "/today", "итог", "день"):
        return cmd_day(state)
    if cmd in ("/out", "/close", "закрыть"):
        if not args:
            raise ValueError("какую сделку закрыть? /out aero")
        inst = find_pos(state, args[0])
        px = num(args[1]) if len(args) > 1 else last_price(inst)
        return close_pos(state, inst, px, "по вашей команде")
    if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9.\-/]{2,25}", t):  # просто название монеты — анализ
        return cmd_analyze(norm_inst(t))
    say("Не понял команду.\n\n" + HELP, menu_markup())


def handle_webapp(state, data):
    d = json.loads(data)
    if d.get("mode") == "analyze":
        return cmd_analyze(norm_inst(d["sym"]))
    d = {"sym": d["sym"], "side": d.get("side") or "short", "entry": num(d["entry"]),
         "lev": num(d["lev"]) if d.get("lev") else None, "margin": num(d["margin"]) if d.get("margin") else None,
         "stop": num(d["stop"]) if d.get("stop") else None}
    return cmd_in(state, d)


def handle_callback(state, cq):
    tg("answerCallbackQuery", {"callback_query_id": cq["id"]}, timeout=5)
    kind, _, sym = (cq.get("data") or "").partition(":")
    if kind == "st":
        return cmd_status(state, find_pos(state, sym))
    if kind == "out":
        inst = find_pos(state, sym)
        return close_pos(state, inst, last_price(inst), "по кнопке")
    if kind in ("force", "drop"):
        inst = norm_inst(sym)
        d = state.get("pending", {}).pop(inst, None)
        if d is None:
            return say(f"По {sym} нет входа, ожидающего решения.")
        if kind == "drop":
            return say(f"👍 <b>{sym}</b> · правильно. Сделку не веду. Отдых — тоже часть торговли.")
        d.pop("ts", None)
        return cmd_in(state, d, force=True)
    if kind == "sl":
        inst = find_pos(state, sym)
        return say(f"Напишите новый стоп так: <code>/stop {sym.lower()} ЦЕНА</code>\n"
                   f"Сейчас: {price(state['pos'][inst]['stop'])}")


def handle_update(state, u, fresh_cutoff):
    msg = u.get("message")
    cq = u.get("callback_query")
    if not msg and not cq:
        return
    chat = str((msg or {}).get("chat", {}).get("id") or ((cq or {}).get("message") or {}).get("chat", {}).get("id") or "")
    if chat != CHAT:
        log(f"  чужой чат {chat} — пропуск")
        return
    if msg and msg.get("date", 0) * 1000 < fresh_cutoff:
        log("  старое сообщение — пропуск")
        return
    try:
        if cq:
            handle_callback(state, cq)
        elif msg.get("web_app_data"):
            handle_webapp(state, msg["web_app_data"]["data"])
        elif msg.get("text"):
            handle_text(state, msg["text"])
    except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        say(f"❗ {e}")
    except Exception as e:  # noqa: BLE001 — бот не должен падать из-за одной команды
        log(f"  ошибка: {e!r}")
        say(f"❗ Внутренняя ошибка: {e}")


# ================================================================ Цикл

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("pos", {})
    s.setdefault("offset", None)
    s.setdefault("hist", [])
    s.setdefault("pending", {})
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def track_all(state):
    now = now_ms()
    for inst in list(state["pos"]):
        try:
            track(state, inst, now)
        except Exception as e:  # noqa: BLE001
            log(f"  сопровождение {inst}: {e!r}")


def run(seconds=None):
    if not TOKEN or not CHAT:
        log("Нет секретов TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        sys.exit(1)
    seconds = A.RUN_SECONDS if seconds is None else seconds
    state = load_state()
    t_end = time.time() + seconds
    fresh_cutoff = now_ms() - A.IGNORE_OLDER_MIN * M1 if state["offset"] is None else 0
    track_all(state)
    save_state(state)
    next_track = time.time() + A.TRACK_EVERY_SEC
    while time.time() < t_end:
        wait = int(max(1, min(25, min(t_end, next_track) - time.time())))
        p = {"timeout": wait, "allowed_updates": ["message", "callback_query"]}
        if state["offset"] is not None:
            p["offset"] = state["offset"]
        ups = tg("getUpdates", p, timeout=wait)
        if ups is None:
            time.sleep(3)
        for u in ups or []:
            state["offset"] = u["update_id"] + 1
            handle_update(state, u, fresh_cutoff)
            save_state(state)
        if time.time() >= next_track:
            track_all(state)
            save_state(state)
            next_track = time.time() + A.TRACK_EVERY_SEC
    save_state(state)
    log(f"Запуск завершён: открытых сделок {len(state['pos'])}")


def main():
    if "--test" in sys.argv or os.environ.get("TEST_TELEGRAM") == "1":
        ok = say("✅ Помощник на связи. Нажмите кнопку внизу, чтобы открыть форму.\n\n" + HELP, menu_markup())
        sys.exit(0 if ok else 1)
    run()


if __name__ == "__main__":
    main()
