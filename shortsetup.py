"""Сканер OKX «шорт от нижнего максимума» -> сообщения в Telegram.

Сетап выведен из прибыльных шортов (AERO, NEAR, ONDO, ZHONGJI):
  4ч  — тренд вниз (цена ниже MA50, последний максимум ниже предыдущего),
        цена отскочила от минимума на 3-12% к сопротивлению (прошлый максимум, пробитая поддержка, MA50);
  1ч  — у сопротивления закрылась свеча-отказ (Star, медвежье поглощение или длинная верхняя тень),
        RSI(1ч) поднимался до 55-80 и развернулся вниз;
  15м — точный стоп за вершиной отскока с запасом от шума.
Фильтры: мало места до поддержки (меньше 2R), свежий Hammer, поздний вход, скорое открытие биржи для токенов акций.

Сообщения: 👀 Наблюдение -> 🔻 Сигнал -> ❌ Отбой / 🎯 Цель / 🛑 Стоп.

Запуск:  python scanner.py              — обычный проход (раз в 5 минут из GitHub Actions)
         python scanner.py --test       — тестовое сообщение в Telegram
         python scanner.py --backtest   — проверка правил на истории OKX
"""
import bisect
import csv
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

import config as C

OKX = "https://www.okx.com"
TASHKENT = dt.timezone(dt.timedelta(hours=5))
H1 = 3_600_000
H4 = 4 * H1
M15 = 900_000
STATE_FILE = os.environ.get("STATE_FILE", "state/state.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PAUSE = float(os.environ.get("REQUEST_PAUSE", C.REQUEST_PAUSE))
T0 = time.time()


def log(msg):
    print(msg, flush=True)


def now_ms():
    return int(time.time() * 1000)


def fmt_time(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, TASHKENT).strftime("%d.%m %H:%M")


def price(x):
    if x >= 1000:
        s = f"{x:.2f}"
    elif x >= 1:
        s = f"{x:.4f}"
    else:
        s = f"{x:.5g}" if x >= 1e-4 else f"{x:.8f}"  # 5 значащих цифр: 0.79131, 0.017408
    return s.rstrip("0").rstrip(".") if "." in s and "e" not in s else s


def base_of(inst):
    return inst.split("-")[0]


def chart_link(inst, interval="60"):
    return f"https://www.tradingview.com/chart/?symbol=OKX:{base_of(inst)}USDT.P&interval={interval}"


# ================================================================ Индикаторы

def sma_last(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi_series(closes, n=14):
    """RSI по Уайлдеру (как в TradingView). None, пока свечей не хватает."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain, loss = gain / n, loss / n

    def val(g, l):
        return (50.0 if g == 0 else 100.0) if l == 0 else 100.0 - 100.0 / (1.0 + g / l)

    out[n] = val(gain, loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = val(gain, loss)
    return out


def atr(cs, n=14):
    """ATR последней свечи по Уайлдеру. None, если свечей мало."""
    if len(cs) < n + 1:
        return None
    trs = []
    for k, x in enumerate(cs):
        if k == 0:
            trs.append(x["h"] - x["l"])
        else:
            pc = cs[k - 1]["c"]
            trs.append(max(x["h"] - x["l"], abs(x["h"] - pc), abs(x["l"] - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def pivots(cs, k):
    """Подтверждённые максимумы и минимумы: выше (ниже) k свечей слева и не ниже (не выше) k свечей справа.
    Возвращает (highs, lows): списки (индекс, цена)."""
    highs, lows = [], []
    for i in range(k, len(cs) - k):
        h, l = cs[i]["h"], cs[i]["l"]
        left, right = cs[i - k:i], cs[i + 1:i + k + 1]
        if h > max(x["h"] for x in left) and h >= max(x["h"] for x in right):
            highs.append((i, h))
        if l < min(x["l"] for x in left) and l <= min(x["l"] for x in right):
            lows.append((i, l))
    return highs, lows


def is_star(cs, i, lookback):
    """Падающая звезда по правилам индикатора «Hammer & ShootingStar Candle Detector» (MoriFX)."""
    if i < lookback:
        return False
    c = cs[i]
    body = abs(c["o"] - c["c"])
    up = abs(c["h"] - c["o"])
    low = abs(c["l"] - c["c"])
    return (up > 2 * body and up > 2 * low
            and c["h"] >= max(x["h"] for x in cs[i - lookback + 1:i + 1])
            and c["o"] <= cs[i - 1]["c"])


def is_hammer(cs, i, lookback=10):
    """Молот — зеркало звезды MoriFX: длинная нижняя тень на локальном минимуме."""
    if i < lookback:
        return False
    c = cs[i]
    body = abs(c["o"] - c["c"])
    low = abs(c["o"] - c["l"])
    up = abs(c["h"] - c["c"])
    return (low > 2 * body and low > 2 * up
            and c["l"] <= min(x["l"] for x in cs[i - lookback + 1:i + 1])
            and c["o"] >= cs[i - 1]["c"])


def is_bear_engulfing(cs, i):
    if i < 1:
        return False
    p, c = cs[i - 1], cs[i]
    return p["c"] > p["o"] and c["c"] < c["o"] and c["o"] >= p["c"] and c["c"] <= p["o"]


def is_pin(cs, i):
    """Длинная верхняя тень и закрытие внизу свечи на локальном максимуме (свеча-отказ без формы MoriFX)."""
    if i < 6:
        return False
    c = cs[i]
    rng = c["h"] - c["l"]
    if rng <= 0:
        return False
    up = c["h"] - max(c["o"], c["c"])
    return (up / rng >= C.PIN_MIN_UPPER and (c["c"] - c["l"]) / rng <= C.PIN_MAX_CLOSE
            and c["h"] >= max(x["h"] for x in cs[i - 5:i + 1]))


def rejection_kind(cs, i):
    if is_star(cs, i, C.STAR_LOOKBACK):
        return "Star"
    if is_bear_engulfing(cs, i):
        return "медвежье поглощение"
    if is_pin(cs, i):
        return "длинная верхняя тень"
    return None


def aggregate(cs1, bar_ms):
    """Свечи старшего таймфрейма из свечей 1ч. closed=True только у полных свечей."""
    groups = {}
    for c in cs1:
        groups.setdefault(c["ts"] // bar_ms, []).append(c)
    out = []
    per = bar_ms // H1
    for b in sorted(groups):
        g = groups[b]
        out.append({"ts": b * bar_ms, "o": g[0]["o"], "h": max(x["h"] for x in g), "l": min(x["l"] for x in g),
                    "c": g[-1]["c"], "v": sum(x["v"] for x in g),
                    "closed": len(g) == per and all(x["closed"] for x in g)})
    return out


# ================================================================ Логика сетапа (общая для живого режима и истории)

def context_4h(cs4, px):
    """Контекст 4ч: тренд вниз и отскок. cs4 — только закрытые свечи. Возвращает dict или (None, причина)."""
    k = C.PIVOT_BARS_4H
    if len(cs4) < max(C.SMA_PERIOD, 40) + 1:
        return None, "мало свечей 4ч"
    closes = [x["c"] for x in cs4]
    sma = sma_last(closes, C.SMA_PERIOD)
    if px >= sma * (1 + C.SMA_TOLERANCE_PCT / 100):
        return None, "цена выше MA50 4ч"
    window = cs4[-100:]
    highs, lows = pivots(window, k)
    # ph1 — последний максимум, который цена с тех пор не перебила (потолок отскока);
    # мелкие максимумы внутри самого отскока, уже перебитые, пропускаются
    i1 = ph1 = None
    for j, ph in reversed(highs):
        if ph > max([x["h"] for x in window[j + 1:]] + [px]):
            i1, ph1 = j, ph
            break
    if ph1 is None:
        return None, "все максимумы 4ч перебиты — тренда вниз нет"
    ph2 = next((ph for j, ph in reversed(highs) if j < i1 and ph > ph1), None)
    if ph2 is None:
        return None, "нет более высокого максимума до последнего — нижнего максимума нет"
    after = window[i1 + 1:]
    if not after:
        return None, "после максимума нет свечей"
    li = min(range(len(after)), key=lambda j: after[j]["l"])
    low = after[li]["l"]
    bounce_part = after[li + 1:]
    top = max([x["h"] for x in bounce_part] + [px])
    bounce = (top / low - 1) * 100
    if not C.BOUNCE_MIN_PCT <= bounce <= C.BOUNCE_MAX_PCT:
        return None, f"отскок {bounce:.1f}% вне диапазона"
    a4 = atr(cs4)
    tol = max(C.ZONE_ATR * a4, px * C.ZONE_PCT / 100)
    levels = [("прошлый максимум 4ч", ph1)]
    for (j, pl) in lows:  # пробитые поддержки над минимумом отскока
        if low < pl < ph1 and any(x["c"] < pl for x in window[j + 1:]):
            levels.append(("пробитая поддержка 4ч", pl))
    if C.USE_SMA_AS_LEVEL and sma > low:
        levels.append(("MA50 4ч", sma))
    supports = sorted({pl for (_, pl) in lows if pl < px} | {low}, reverse=True)
    return {"sma": sma, "ph1": ph1, "ph2": ph2, "ph1_ts": window[i1]["ts"], "low": low,
            "low_ts": after[li]["ts"], "top": top, "bounce": bounce, "atr4": a4, "tol": tol,
            "levels": levels, "supports": supports,
            "hammer4": any(is_hammer(cs4, len(cs4) - 1 - j) for j in range(C.HAMMER_BARS_4H))}, None


def hit_level(levels, hi, tol):
    """Уровень, до которого дотянулся максимум hi (с допуском tol). Ближайший по цене."""
    hits = [(abs(hi - lv), name, lv) for name, lv in levels if lv - tol <= hi <= lv + tol]
    if not hits:
        return None
    _, name, lv = min(hits)
    return name, lv


def detect(cs4, cs1, px, cs15=None):
    """Проверка сетапа. cs4, cs1 — закрытые свечи (от старых к новым), px — текущая цена, cs15 — свечи 15м (по желанию).
    Возвращает (вид, данные): ("signal", {...}), ("watch", {...}) или (None, причина)."""
    ctx, why = context_4h(cs4, px)
    if not ctx:
        return None, why
    if len(cs1) < 30:
        return None, "мало свечей 1ч"
    rsi1 = rsi_series([x["c"] for x in cs1])
    i = len(cs1) - 1
    c = cs1[i]
    recent_hi = max(x["h"] for x in cs1[-C.STOP_LOOKBACK_1H:])
    lvl = hit_level(ctx["levels"], recent_hi, ctx["tol"])
    kind = rejection_kind(cs1, i)
    peak = max(r for r in rsi1[-4:] if r is not None) if any(r is not None for r in rsi1[-4:]) else None
    rsi_ok = (peak is not None and C.RSI_PEAK_MIN <= peak <= C.RSI_PEAK_MAX
              and rsi1[i] is not None and rsi1[i] < peak)

    if lvl and kind and rsi_ok:
        # --- стоп
        a1 = atr(cs1)
        a15 = atr(cs15) if cs15 and len(cs15) > 15 else None
        noise = a15 if a15 else a1 / 2
        stop_base = max(recent_hi, c["h"])
        stop = stop_base + max(C.STOP_BUFFER_ATR15 * noise, px * 0.001)
        if stop - px < C.MIN_STOP_ATR15 * noise:
            stop = px + C.MIN_STOP_ATR15 * noise
        r = stop - px
        if r <= 0:
            return None, "цена выше стопа"
        drop = (stop_base - px) / stop_base * 100
        if drop > C.LATE_PCT:
            return None, f"поздно: цена уже {drop:.1f}% ниже вершины отскока"
        hammer1 = any(is_hammer(cs1, i - j) for j in range(C.HAMMER_BARS_1H))
        if hammer1 or ctx["hammer4"]:
            return None, "свежий Hammer — возможен отскок"
        # --- цели
        sup = [s for s in ctx["supports"] if s < px]
        if sup:
            s1 = sup[0]
            if px - s1 < C.MIN_RR * r:
                return None, f"мало места: поддержка {price(s1)} ближе {C.MIN_RR:g}R"
            t1 = s1 + 0.1 * a1
            further = [s for s in sup[1:] if s < t1 - 0.5 * r]
            t2 = further[0] + 0.1 * a1 if further else px - C.T2_MULT * (px - t1)
            t1_note = "поддержка 4ч"
        else:
            t1 = px - C.FALLBACK_T1_R * r
            t2 = px - C.T2_MULT * (px - t1)
            t1_note = f"уровня ниже нет, {C.FALLBACK_T1_R:g}R"
        return "signal", {"entry": px, "stop": stop, "t1": t1, "t2": t2, "r": r, "rr": (px - t1) / r,
                          "kind": kind, "level": lvl, "rsi_peak": peak, "rsi": rsi1[i], "ctx": ctx,
                          "candle_ts": c["ts"], "t1_note": t1_note, "noise_tf": "15м" if a15 else "1ч"}

    # --- наблюдение: цена у сопротивления, ждём свечу-отказ
    near = hit_level(ctx["levels"], max(recent_hi, px), ctx["tol"])
    if near and px >= near[1] - 2 * ctx["tol"] and rsi1[i] is not None and rsi1[i] >= C.RSI_WATCH_MIN:
        return "watch", {"level": near, "rsi": rsi1[i], "ctx": ctx, "price": px}
    if not lvl:
        return None, "цена не у сопротивления"
    if not kind:
        return None, "нет свечи-отказа на 1ч"
    return None, f"RSI(1ч) не подходит (пик {peak:.0f})" if peak else "RSI(1ч) не посчитан"


# ================================================================ Токены акций

def stock_block(inst, ts_ms, minutes=None):
    """Если до открытия настоящей биржи меньше minutes минут — текст предупреждения, иначе None."""
    market = C.STOCK_TOKENS.get(base_of(inst))
    if not market:
        return None
    minutes = C.STOCK_BLOCK_MIN if minutes is None else minutes
    now = dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc)
    if market == "cn":
        tz, opens = ZoneInfo("Asia/Shanghai"), [(9, 30), (13, 0)]
    else:
        tz, opens = ZoneInfo("America/New_York"), [(9, 30)]
    local = now.astimezone(tz)
    for day in (0, 1):
        d = (local + dt.timedelta(days=day)).date()
        if d.weekday() >= 5:
            continue
        for hh, mm in opens:
            o = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            delta = (o - local).total_seconds() / 60
            if 0 <= delta <= minutes:
                t = o.astimezone(TASHKENT).strftime("%H:%M")
                return f"открытие биржи ({'Китай' if market == 'cn' else 'США'}) в {t} по Ташкенту"
    return None


# ================================================================ OKX

def okx_get(path, params, tries=3):
    for attempt in range(tries):
        try:
            r = requests.get(OKX + path, params=params, timeout=15)
            if r.status_code == 200:
                j = r.json()
                if j.get("code") == "0":
                    return j.get("data", [])
                log(f"  OKX отказал: code={j.get('code')} {j.get('msg')} {params}")
                if j.get("code") == "51001":
                    return None
            else:
                log(f"  HTTP {r.status_code}: {path} {params}")
        except (requests.RequestException, ValueError) as e:
            log(f"  Ошибка запроса: {e}")
        if attempt < tries - 1:
            time.sleep(min(max(PAUSE, 0.5) * (2 ** attempt), 30))
    return None


def parse_rows(rows):
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
             "v": float(r[5]), "closed": r[8] == "1"} for r in reversed(rows)]


def fetch_candles(inst, bar, limit):
    """Свечи от старых к новым; у последней closed=False, пока она не закрылась."""
    data = okx_get("/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": str(limit)})
    time.sleep(PAUSE)
    return parse_rows(data) if data else None


def fetch_history_1h(inst, start_ms):
    """Свечи 1ч от start_ms до сейчас (постранично, по 100)."""
    out, after = [], None
    while True:
        params = {"instId": inst, "bar": "1H", "limit": "100"}
        if after:
            params["after"] = str(after)
        data = okx_get("/api/v5/market/history-candles", params)
        time.sleep(max(PAUSE, 0.12))
        if not data:
            break
        rows = parse_rows(data)
        out = rows + out
        after = rows[0]["ts"]
        if after <= start_ms or len(data) < 100:
            break
    return [x for x in out if x["ts"] >= start_ms]


def top_instruments(limit):
    data = okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
    time.sleep(PAUSE)
    if not data:
        return None
    rows = []
    for d in data:
        inst = d.get("instId", "")
        if not inst.endswith("-USDT-SWAP") or inst in C.EXCLUDE:
            continue
        try:
            last = float(d["last"])
            turnover = float(d["volCcy24h"]) * last
        except (KeyError, ValueError, TypeError):
            continue
        if turnover >= C.MIN_TURNOVER_USDT:
            rows.append((turnover, inst, last))
    rows.sort(reverse=True)
    return [(inst, last) for _, inst, last in rows[:limit]]


# ================================================================ Telegram

def send(text):
    if DRY_RUN:
        log("----- [DRY_RUN] сообщение -----\n" + text + "\n-----")
        return True
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("  Нет секретов TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=15)
    except requests.RequestException as e:
        log(f"  Telegram: ошибка запроса: {e}")
        return False
    if r.status_code != 200:
        log(f"  Telegram: HTTP {r.status_code}: {r.text[:200]}")
        return False
    return True


def pct(a, b):
    return (a / b - 1) * 100


def msg_signal(inst, s):
    b, ctx = base_of(inst), s["ctx"]
    e, st, t1, t2 = s["entry"], s["stop"], s["t1"], s["t2"]
    return (f"🔻 <b>SHORT {b}</b> · сигнал\n\n"
            f"Вход: <b>{price(e)}</b>\n"
            f"🛑 Стоп: <b>{price(st)}</b> (+{pct(st, e):.2f}%, за вершиной отскока, запас от шума {s['noise_tf']})\n"
            f"🎯 Цель 1: <b>{price(t1)}</b> ({pct(t1, e):.2f}%, {s['t1_note']}) — закрыть 50%\n"
            f"🎯 Цель 2: <b>{price(t2)}</b> ({pct(t2, e):.2f}%)\n"
            f"Риск/прибыль до цели 1: 1 к {s['rr']:.1f}\n\n"
            f"Почему:\n"
            f"• 4ч: тренд вниз, максимумы {price(ctx['ph2'])} → {price(ctx['ph1'])}, цена ниже MA50\n"
            f"• отскок {ctx['bounce']:.1f}% от {price(ctx['low'])} к уровню {price(s['level'][1])} ({s['level'][0]})\n"
            f"• 1ч: {s['kind']}, RSI {s['rsi_peak']:.0f} → {s['rsi']:.0f}\n\n"
            f"Для помощника: <code>/in {b.lower()}usdt short {price(e)}</code>\n"
            f"<a href=\"{chart_link(inst)}\">График TradingView</a>")


def msg_watch(inst, w):
    b, ctx = base_of(inst), w["ctx"]
    return (f"👀 <b>{b}</b> · наблюдение\n\n"
            f"Цена {price(w['price'])} у сопротивления {price(w['level'][1])} ({w['level'][0]}).\n"
            f"4ч: тренд вниз, отскок {ctx['bounce']:.1f}% от {price(ctx['low'])}. RSI(1ч) {w['rsi']:.0f}.\n"
            f"Жду свечу-отказ на 1ч. Отбой, если 15м закроется выше {price(ctx['ph1'])}.\n"
            f"<a href=\"{chart_link(inst)}\">График TradingView</a>")


# ================================================================ Живой режим

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    for k in ("watch", "signals", "last_signal"):
        s.setdefault(k, {})
    s.setdefault("last_full", 0)
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def track(state, now, stats):
    """Каждый запуск: наблюдения (отбой) и выданные сигналы (стоп, цель, поздно) по свечам 15м."""
    for inst in list(state["watch"]):
        w = state["watch"][inst]
        if now - w["created"] > C.WATCH_HOURS * H1:
            del state["watch"][inst]
            continue
        cs = fetch_candles(inst, "15m", 20)
        if not cs:
            continue
        closed = [x for x in cs if x["closed"] and x["ts"] >= w["created"] - M15]
        if any(x["c"] > w["ph1"] for x in closed):
            if send(f"❌ <b>{base_of(inst)}</b> · отбой\n\nЦена закрылась выше прошлого максимума {price(w['ph1'])}: "
                    f"нижний максимум сломан, шорт отменяется."):
                stats["sent"] += 1
            del state["watch"][inst]

    for inst in list(state["signals"]):
        s = state["signals"][inst]
        if now - s["created"] > C.TRACK_HOURS * H1:
            del state["signals"][inst]
            continue
        cs = fetch_candles(inst, "15m", 100)
        if not cs:
            continue
        b = base_of(inst)
        for x in cs:
            if x["ts"] + M15 <= s["checked"]:
                continue
            if not s.get("t1_hit") and x["h"] >= s["stop"]:
                send(f"🛑 <b>{b}</b> · стоп {price(s['stop'])} задет раньше цели. Сигнал закрыт (−1R).")
                del state["signals"][inst]
                break
            if not s.get("t1_hit") and x["l"] <= s["t1"]:
                s["t1_hit"] = True
                send(f"🎯 <b>{b}</b> · цель 1 {price(s['t1'])} достигнута (+{s['rr']:.1f}R). "
                     f"Если в сделке — закрыть 50% и перенести стоп в безубыток {price(s['entry'])}.")
            if s.get("t1_hit") and x["l"] <= s["t2"]:
                send(f"🎯🎯 <b>{b}</b> · цель 2 {price(s['t2'])} достигнута. Сигнал закрыт.")
                del state["signals"][inst]
                break
            if s.get("t1_hit") and x["h"] >= s["entry"]:
                send(f"↩️ <b>{b}</b> · после цели 1 цена вернулась к входу {price(s['entry'])}. Сигнал закрыт.")
                del state["signals"][inst]
                break
            if (not s.get("t1_hit") and not s.get("late_sent")
                    and x["l"] <= s["entry"] - C.LATE_NOTICE_R * s["r"]):
                s["late_sent"] = True
                send(f"⏩ <b>{b}</b> · цена уже ушла вниз на {C.LATE_NOTICE_R:g}R от входа. "
                     f"Если не вошли — не догоняйте, ждите следующий отскок.")
        else:
            s["checked"] = now


def full_scan(state, now, stats):
    coins = top_instruments(C.TOP_COINS)
    if coins is None:
        log("Не удалось получить список монет OKX")
        stats["errors"] += 1
        return
    log(f"Монет для проверки: {len(coins)}")
    for inst, last in coins:
        cs4 = fetch_candles(inst, "4H", 150)
        cs1 = fetch_candles(inst, "1H", 120)
        if not cs4 or not cs1:
            stats["errors"] += 1
            continue
        stats["scanned"] += 1
        px = cs1[-1]["c"]
        c4 = [x for x in cs4 if x["closed"]]
        c1 = [x for x in cs1 if x["closed"]]
        kind, data = detect(c4, c1, px)
        if kind == "signal":  # для точного стопа нужны 15м
            cs15 = fetch_candles(inst, "15m", 100)
            kind, data = detect(c4, c1, px, [x for x in cs15 if x["closed"]] if cs15 else None)
        if kind is None:
            continue
        if kind == "watch":
            key = f"{data['ctx']['low_ts']}"
            if inst in state["watch"] or state.get("watched_low", {}).get(inst) == key or inst in state["signals"]:
                continue
            if send(msg_watch(inst, data)):
                state["watch"][inst] = {"created": now, "ph1": data["ctx"]["ph1"]}
                state.setdefault("watched_low", {})[inst] = key
                stats["watch"] += 1
            continue
        # сигнал
        if now - state["last_signal"].get(inst, 0) < C.COOLDOWN_HOURS * H1:
            log(f"  {inst}: сигнал пропущен — пауза {C.COOLDOWN_HOURS} ч по монете")
            continue
        block = stock_block(inst, now)
        if block:
            log(f"  {inst}: сигнал пропущен — {block}")
            send(f"⏸ <b>{base_of(inst)}</b> · сетап есть, но сигнал не даю: {block}. Возможен гэп.")
            state["last_signal"][inst] = now
            continue
        if send(msg_signal(inst, data)):
            stats["signals"] += 1
            state["last_signal"][inst] = now
            state["watch"].pop(inst, None)
            state["signals"][inst] = {"created": now, "checked": now, "entry": data["entry"], "stop": data["stop"],
                                      "t1": data["t1"], "t2": data["t2"], "r": data["r"], "rr": data["rr"]}
        else:
            stats["send_failed"] += 1


def run():
    state = load_state()
    now = now_ms()
    stats = {"scanned": 0, "errors": 0, "watch": 0, "signals": 0, "sent": 0, "send_failed": 0}
    try:
        track(state, now, stats)
        last_closed_1h = (now // H1) * H1 - H1  # начало последней закрытой свечи 1ч
        if state["last_full"] < last_closed_1h:
            full_scan(state, now, stats)
            state["last_full"] = last_closed_1h
        else:
            log("Новой свечи 1ч нет — только сопровождение наблюдений и сигналов")
        state["last_signal"] = {k: v for k, v in state["last_signal"].items() if now - v < 2 * 86_400_000}
    finally:
        save_state(state)
    log(f"Итог: монет {stats['scanned']}, ошибок {stats['errors']}, наблюдений {stats['watch']}, "
        f"сигналов {stats['signals']}, на сопровождении {len(state['signals'])}, "
        f"время {(time.time() - T0) / 60:.1f} мин")
    if stats["send_failed"]:
        sys.exit(1)


# ================================================================ Проверка на истории

def simulate(c1, entry_i, sig):
    """Исход сигнала по следующим свечам 1ч. Стоп и цель в одной свече — считаем стоп (осторожно)."""
    e, st, t1, r = sig["entry"], sig["stop"], sig["t1"], sig["r"]
    for j in range(entry_i + 1, min(entry_i + 1 + C.BACKTEST_MAX_HOURS, len(c1))):
        x = c1[j]
        if x["h"] >= st:
            return "стоп", -1.0, j - entry_i
        if x["l"] <= t1:
            return "цель 1", (e - t1) / r, j - entry_i
    last = c1[min(entry_i + C.BACKTEST_MAX_HOURS, len(c1) - 1)]
    return "время вышло", (e - last["c"]) / r, min(C.BACKTEST_MAX_HOURS, len(c1) - 1 - entry_i)


def backtest_coin(inst, c1, test_from):
    """Проход по закрытым свечам 1ч: на каждой — проверка сетапа так, как это сделал бы живой бот."""
    c4_all = aggregate(c1, H4)
    ends4 = [x["ts"] + H4 for x in c4_all]
    trades, last_sig = [], -10 ** 15
    for i in range(len(c1)):
        x = c1[i]
        cut = x["ts"] + H1  # момент закрытия свечи
        if x["ts"] < test_from:
            continue
        k = bisect.bisect_right(ends4, cut)
        cs4 = c4_all[max(0, k - 150):k]
        cs1 = c1[max(0, i - 119):i + 1]
        kind, sig = detect(cs4, cs1, x["c"])
        if kind != "signal" or cut - last_sig < C.COOLDOWN_HOURS * H1 or stock_block(inst, cut):
            continue
        last_sig = cut
        res, rr, hours = simulate(c1, i, sig)
        trades.append({"монета": base_of(inst), "время": fmt_time(cut), "вход": price(sig["entry"]),
                       "стоп": price(sig["stop"]), "цель1": price(sig["t1"]), "стоп_%": round(pct(sig["stop"], sig["entry"]), 2),
                       "свеча": sig["kind"], "уровень": sig["level"][0], "итог": res, "R": round(rr, 2), "часов": hours})
    return trades


def backtest():
    days = int(os.environ.get("BACKTEST_DAYS") or C.BACKTEST_DAYS)
    coins = top_instruments(C.BACKTEST_COINS)
    if not coins:
        log("Не удалось получить список монет OKX")
        sys.exit(1)
    now = now_ms()
    test_from = (now // H1 - days * 24) * H1
    start = test_from - 30 * 86_400_000  # месяц до начала — для MA50 и максимумов 4ч
    all_trades = []
    for n, (inst, _) in enumerate(coins, 1):
        c1 = [x for x in fetch_history_1h(inst, start) if x["closed"]]
        if len(c1) < 400:
            log(f"[{n}/{len(coins)}] {inst}: мало истории, пропуск")
            continue
        t = backtest_coin(inst, c1, test_from)
        all_trades += t
        log(f"[{n}/{len(coins)}] {inst}: сигналов {len(t)}")
    os.makedirs("backtest", exist_ok=True)
    with open("backtest/trades.csv", "w", newline="", encoding="utf-8-sig") as f:
        if all_trades:
            w = csv.DictWriter(f, fieldnames=list(all_trades[0]), delimiter=";")
            w.writeheader()
            w.writerows(all_trades)
    report = backtest_report(all_trades, days, len(coins))
    log(report)
    send(report)


def backtest_report(trades, days, coins):
    if not trades:
        return f"📊 Проверка на истории ({days} дн., {coins} монет): сигналов не было. Правила слишком строгие — ослабим."
    wins = [t for t in trades if t["итог"] == "цель 1"]
    losses = [t for t in trades if t["итог"] == "стоп"]
    other = [t for t in trades if t["итог"] == "время вышло"]
    total = sum(t["R"] for t in trades)
    avg_win = sum(t["R"] for t in wins) / len(wins) if wins else 0
    by_kind = {}
    for t in trades:
        k = by_kind.setdefault(t["свеча"], [0, 0.0])
        k[0] += 1
        k[1] += t["R"]
    kinds = "\n".join(f"• {k}: {v[0]} сигн., итог {v[1]:+.1f}R" for k, v in by_kind.items())
    return (f"📊 <b>Проверка на истории</b> · {days} дн., {coins} монет\n\n"
            f"Сигналов: {len(trades)} (≈{len(trades) / days * 7:.1f} в неделю)\n"
            f"🎯 Цель 1: {len(wins)} ({len(wins) / len(trades) * 100:.0f}%), в среднем +{avg_win:.1f}R\n"
            f"🛑 Стоп: {len(losses)} ({len(losses) / len(trades) * 100:.0f}%)\n"
            f"⏳ Время вышло: {len(other)}\n"
            f"Итог: <b>{total:+.1f}R</b>, на сигнал {total / len(trades):+.2f}R\n\n"
            f"По свечам:\n{kinds}\n\n"
            f"R — риск сделки. При риске $1 на сделку итог {total:+.1f}R = {total:+.1f}$.\n"
            f"Все сделки — в файле trades.csv (артефакт запуска).")


def main():
    if "--test" in sys.argv or os.environ.get("TEST_TELEGRAM") == "1":
        ok = send("✅ Сканер «шорт от нижнего максимума»: бот на связи. Сигналы будут приходить сюда.")
        sys.exit(0 if ok else 1)
    if "--backtest" in sys.argv or os.environ.get("BACKTEST") == "1":
        backtest()
        return
    run()


if __name__ == "__main__":
    main()
