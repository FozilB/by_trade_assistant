"""Зоны поддержки и сопротивления, тренд и подсказка «что сейчас ждать».

Работает только по запросу (разбор монеты): бот ничего не присылает сам.
Зона — это не линия, а диапазон, где цена уже несколько раз разворачивалась (максимумы/минимумы 1ч и 4ч).
"""
import io

import assistant_config as A
import shortsetup as S

price = S.price


# ================================================================ Зоны

def find_zones(c1, c4, px):
    """Склеивает развороты 1ч и 4ч в зоны. Возвращает (сопротивления выше, поддержки ниже, зона, в которой цена).
    Каждая зона: {"lo", "hi", "touches", "h4"}; списки отсортированы от ближней к дальней."""
    a1 = S.atr(c1) or px * 0.005
    pts = []
    for cs, k, tf in ((c1[-120:], 3, "1h"), (c4[-100:], 3, "4h")):
        highs, lows = S.pivots(cs, k)
        pts += [(p, tf) for _, p in highs + lows]
    pts.sort()
    merge = A.ZONE_MERGE_ATR1 * a1
    groups = []
    for p, tf in pts:
        if groups and p - groups[-1]["hi"] <= merge:
            g = groups[-1]
            g["hi"] = p
            g["touches"] += 1
            g["h4"] = g["h4"] or tf == "4h"
        else:
            groups.append({"lo": p, "hi": p, "touches": 1, "h4": tf == "4h"})
    half = A.ZONE_MIN_WIDTH_ATR1 * a1 / 2
    zones = []
    for g in groups:
        if g["touches"] < 2 and not g["h4"]:
            continue  # одиночный разворот на 1ч — не уровень
        mid = (g["lo"] + g["hi"]) / 2
        g["lo"], g["hi"] = min(g["lo"], mid - half), max(g["hi"], mid + half)
        zones.append(g)
    near = A.ZONE_NEAR_ATR1 * a1
    res = sorted([z for z in zones if z["lo"] > px + near], key=lambda z: z["lo"])[:A.ZONES_EACH_SIDE]
    sup = sorted([z for z in zones if z["hi"] < px - near], key=lambda z: -z["hi"])[:A.ZONES_EACH_SIDE]
    at = [z for z in zones if z["lo"] - near <= px <= z["hi"] + near]
    return res, sup, (min(at, key=lambda z: abs((z["lo"] + z["hi"]) / 2 - px)) if at else None), a1


def strength(z):
    if z["touches"] == 1:
        return "разворот 4ч"
    s = "сильная" if z["touches"] >= 3 or z["h4"] else "обычная"
    return f"{z['touches']} касан., {s}"


def fmt_zone(z):
    return f"{price(z['lo'])}–{price(z['hi'])} ({strength(z)})"


# ================================================================ Тренд

def trend(c4):
    """'down' — максимумы и минимумы 4ч снижаются, 'up' — растут, иначе 'range'."""
    highs, lows = S.pivots(c4[-100:], 3)
    sma = S.sma_last([x["c"] for x in c4], 50)
    px = c4[-1]["c"]
    hh, ll = [p for _, p in highs[-2:]], [p for _, p in lows[-2:]]
    if len(hh) == 2 and len(ll) == 2:
        if hh[1] < hh[0] and ll[1] < ll[0] and (not sma or px < sma * 1.01):
            return "down"
        if hh[1] > hh[0] and ll[1] > ll[0] and (not sma or px > sma * 0.99):
            return "up"
    if sma and len(hh) == 2 and hh[1] < hh[0] and px < sma:
        return "down"
    if sma and len(ll) == 2 and ll[1] > ll[0] and px > sma:
        return "up"
    return "range"


TREND_TEXT = {"down": "📉 вниз — максимумы и минимумы снижаются → ищем только <b>шорт</b>",
              "up": "📈 вверх — максимумы и минимумы растут → ищем только <b>лонг</b>",
              "range": "↔️ боковик — чёткого тренда нет → шорт только от сопротивления, лонг только от поддержки"}


# ================================================================ Что сейчас ждать

def candle_vs_zone(c, z):
    """Как последняя закрытая свеча повела себя у зоны."""
    if c["h"] > z["hi"] and c["c"] < z["hi"] and c["o"] < z["hi"]:
        return "fake_up"      # прокол вверх тенью, закрытие обратно под зоной
    if c["l"] < z["lo"] and c["c"] > z["lo"] and c["o"] > z["lo"]:
        return "fake_down"
    if c["c"] > z["hi"] and c["o"] <= z["hi"]:
        return "break_up"
    if c["c"] < z["lo"] and c["o"] >= z["lo"]:
        return "break_down"
    return None


def trade_hint(side, entry, zone_edge, a1):
    """Стоп за зоной с запасом и максимальное плечо (ликвидация примерно вдвое дальше стопа)."""
    buf = A.ZONE_STOP_BUFFER_ATR1 * a1
    stop = zone_edge + buf if side == "short" else zone_edge - buf
    stop_pct = abs(stop - entry) / entry * 100
    max_lev = max(1, int(A.LIQ_SAFETY_PCT / stop_pct)) if stop_pct > 0 else None
    return stop, stop_pct, max_lev


def situation(px, tr, res, sup, at, a1, c1):
    """Возвращает (где цена, что делать) — простым языком."""
    last = c1[-1]
    r0, s0 = (res[0] if res else None), (sup[0] if sup else None)
    lines = []

    def hint(side, entry, edge, extra=""):
        stop, pct, lev = trade_hint(side, entry, edge, a1)
        word = "Шорт" if side == "short" else "Лонг"
        return (f"{word}: вход около {price(entry)}, стоп за зоной {price(stop)} ({pct:.2f}%), "
                f"плечо не выше x{lev}{extra}")

    if at is not None:
        mid = (at["lo"] + at["hi"]) / 2
        kind = candle_vs_zone(last, at)
        role = "сопротивления" if px <= mid or tr == "down" else "поддержки"
        if tr == "up" and px >= at["lo"]:
            role = "поддержки"
        where = f"📍 Цена <b>в зоне {role}</b> {fmt_zone(at)}"
        if kind == "fake_up":
            lines += ["🪤 Последняя свеча 1ч проколола зону вверх и закрылась обратно — <b>ложный пробой</b>.",
                      "Это сильный сигнал на шорт" + (" по тренду." if tr != "up" else ", но тренд вверх — осторожно.")]
            if tr != "up":
                lines.append(hint("short", px, last["h"]))
        elif kind == "fake_down":
            lines += ["🪤 Последняя свеча 1ч проколола зону вниз и закрылась обратно — <b>ложный пробой</b>.",
                      "Это сильный сигнал на лонг" + (" по тренду." if tr != "down" else ", но тренд вниз — осторожно.")]
            if tr != "down":
                lines.append(hint("long", px, last["l"]))
        elif kind == "break_up":
            lines += ["⬆️ Свеча 1ч закрылась <b>выше</b> зоны — пробой вверх.",
                      f"Не догоняй. Жди ретест: цена вернётся к {price(at['hi'])} сверху и удержится → лонг."]
        elif kind == "break_down":
            lines += ["⬇️ Свеча 1ч закрылась <b>ниже</b> зоны — пробой вниз.",
                      f"Не догоняй. Жди ретест: цена вернётся к {price(at['lo'])} снизу и не пробьёт → шорт."]
        elif tr == "down":
            lines += ["⏳ <b>Жди отказа</b>: Star или длинную верхнюю тень и закрытие свечи 15м/1ч "
                      f"<b>под {price(at['lo'])}</b>. Без этого не входи.",
                      hint("short", at["lo"], at["hi"], " — только после подтверждения")]
        elif tr == "up":
            lines += ["⏳ <b>Жди отскока</b>: Hammer или длинную нижнюю тень и закрытие свечи 15м/1ч "
                      f"<b>над {price(at['hi'])}</b>. Без этого не входи.",
                      hint("long", at["hi"], at["lo"], " — только после подтверждения")]
        else:
            lines += ["⏳ Боковик: жди, что сделает свеча 1ч у зоны — отказ (тогда вход от зоны) "
                      "или закрытие за зоной (тогда ждать ретест)."]
        return where, lines

    # цена между зонами
    d_res = (r0["lo"] - px) / a1 if r0 else None
    d_sup = (px - s0["hi"]) / a1 if s0 else None
    where = "📍 Цена <b>между зонами</b>"
    if r0 and s0:
        where += f": до сопротивления {S.pct(r0['lo'], px):+.2f}%, до поддержки {S.pct(s0['hi'], px):+.2f}%"
    lines.append("🚫 Посередине вход плохой: стоп далеко, а цель близко.")
    if tr == "down":
        if r0:
            lines.append(f"⏳ Жди отката вверх к сопротивлению <b>{fmt_zone(r0)}</b> и отказа там → шорт.")
        if s0 and d_sup is not None and d_res is not None and d_sup < d_res:
            lines.append(f"⚠️ Поддержка {price(s0['hi'])} близко — шортить сюда поздно, а покупать — против тренда.")
    elif tr == "up":
        if s0:
            lines.append(f"⏳ Жди отката вниз к поддержке <b>{fmt_zone(s0)}</b> и отскока там → лонг.")
        if r0 and d_sup is not None and d_res is not None and d_res < d_sup:
            lines.append(f"⚠️ Сопротивление {price(r0['lo'])} близко — покупать сюда поздно, а шортить — против тренда.")
    else:
        if r0:
            lines.append(f"⏳ Шорт — только от {fmt_zone(r0)}")
        if s0:
            lines.append(f"⏳ Лонг — только от {fmt_zone(s0)}")
    return where, lines


# ================================================================ Star / Hammer: рабочая или ложная

def _zone_hit(lo, hi, zones):
    return next((z for z in zones if lo <= z["hi"] and hi >= z["lo"]), None)


def pattern_check(c1, tr, res, sup, at, a1):
    """Последняя Star/Hammer на 1ч (за PATTERN_LOOKBACK_1H свечей): рабочая или ложная, и почему."""
    n = len(c1)
    rsi = S.rsi_series([x["c"] for x in c1])
    for back in range(A.PATTERN_LOOKBACK_1H):
        i = n - 1 - back
        if i < 15:
            break
        star, ham = S.is_star(c1, i, 10), S.is_hammer(c1, i)
        if not (star or ham):
            continue
        x = c1[i]
        nxt = c1[i + 1:]
        plus, minus = [], []
        if star:
            name = "🌠 Star"
            if tr == "down":
                plus.append("по тренду (тренд вниз)")
            elif tr == "up":
                minus.append("против тренда вверх — в росте звёзды часто ложные")
            if _zone_hit(x["l"], x["h"], res + ([at] if at else [])):
                plus.append("у сопротивления")
            elif _zone_hit(x["l"], x["h"], sup):
                minus.append("у поддержки — отсюда цена скорее отскочит вверх")
            else:
                minus.append("не у уровня, посередине")
            if rsi[i] is not None:
                (plus if rsi[i] >= 55 else minus).append(f"RSI {rsi[i]:.0f} " + ("— рост выдыхается" if rsi[i] >= 55 else "— рост ещё не был сильным"))
            if not nxt:
                confirm = "⏳ подтверждения ещё нет: жди закрытие следующей свечи ниже " + price(x["l"])
            elif min(y["c"] for y in nxt) < x["l"]:
                plus.append("подтверждена: закрытие ниже её минимума")
                confirm = None
            elif max(y["c"] for y in nxt) > x["h"]:
                minus.append("сломана: свеча закрылась выше её максимума")
                confirm = None
            else:
                confirm = "⏳ подтверждения ещё нет: жди закрытие свечи ниже " + price(x["l"])
        else:
            name = "🔨 Hammer"
            if tr == "up":
                plus.append("по тренду (тренд вверх)")
            elif tr == "down":
                minus.append("против тренда вниз — в падении молоты часто ложные")
            if _zone_hit(x["l"], x["h"], sup + ([at] if at else [])):
                plus.append("у поддержки")
            elif _zone_hit(x["l"], x["h"], res):
                minus.append("у сопротивления — отсюда цена скорее пойдёт вниз")
            else:
                minus.append("не у уровня, посередине")
            if rsi[i] is not None:
                (plus if rsi[i] <= 45 else minus).append(f"RSI {rsi[i]:.0f} " + ("— падение выдыхается" if rsi[i] <= 45 else "— падения почти не было"))
            if not nxt:
                confirm = "⏳ подтверждения ещё нет: жди закрытие следующей свечи выше " + price(x["h"])
            elif max(y["c"] for y in nxt) > x["h"]:
                plus.append("подтверждён: закрытие выше его максимума")
                confirm = None
            elif min(y["c"] for y in nxt) < x["l"]:
                minus.append("сломан: свеча закрылась ниже его минимума")
                confirm = None
            else:
                confirm = "⏳ подтверждения ещё нет: жди закрытие свечи выше " + price(x["h"])
        if (x["h"] - x["l"]) < 0.6 * a1:
            minus.append("свеча маленькая (меньше обычного размаха) — слабый сигнал")
        broken = any("сломан" in t for t in minus)
        if broken or len(minus) >= 2:
            verdict = "❌ <b>скорее ложная</b>" if star else "❌ <b>скорее ложный</b>"
        elif not minus and confirm is None:
            verdict = "✅ <b>рабочая</b>" if star else "✅ <b>рабочий</b>"
        else:
            verdict = "⚠️ <b>сомнительная</b>" if star else "⚠️ <b>сомнительный</b>"
        ago = "на последней свече" if back == 0 else f"{back + 1} свеч. назад"
        out = [f"{name} на 1ч ({ago}): {verdict}"]
        out += [f"   ✓ {t}" for t in plus] + [f"   ✗ {t}" for t in minus]
        if confirm and not broken:
            out.append(f"   {confirm}")
        return out
    return []


def report(m):
    """Блок для разбора монеты: тренд, зоны, где цена и что ждать. Возвращает (строки, данные для графика)."""
    c1, c4, px = m["c1"], m["c4"], m["px"]
    tr = trend(c4)
    res, sup, at, a1 = find_zones(c1, c4, px)
    where, todo = situation(px, tr, res, sup, at, a1, c1)
    lines = [f"Тренд 4ч: {TREND_TEXT[tr]}", "",
             "🔴 Сопротивления выше:" + ("" if res else " нет")]
    lines += [f"   {fmt_zone(z)}" for z in res]
    lines.append("🟢 Поддержки ниже:" + ("" if sup else " нет"))
    lines += [f"   {fmt_zone(z)}" for z in sup]
    lines += ["", where] + todo
    pat = pattern_check(c1, tr, res, sup, at, a1)
    if pat:
        lines += [""] + pat
    return lines, {"trend": tr, "res": res, "sup": sup, "at": at}


# ================================================================ Картинка

def chart_png(c1, px, z, title):
    """График 1ч со свечами и зонами. Возвращает PNG (bytes)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    cs = c1[-A.CHART_BARS_1H:]
    bg, fg = "#131722", "#d1d4dc"
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    w = 0.6
    for i, x in enumerate(cs):
        col = "#26a69a" if x["c"] >= x["o"] else "#ef5350"
        ax.plot([i, i], [x["l"], x["h"]], color=col, linewidth=0.8)
        lo, hi = sorted((x["o"], x["c"]))
        ax.add_patch(Rectangle((i - w / 2, lo), w, max(hi - lo, px * 1e-5), color=col))
    lo_v = min(x["l"] for x in cs)
    hi_v = max(x["h"] for x in cs)
    zones = [(q, "#ef5350") for q in z["res"]] + [(q, "#26a69a") for q in z["sup"]]
    if z["at"]:
        zones.append((z["at"], "#f0b90b"))
    span = hi_v - lo_v
    zones = [(q, col) for q, col in zones if q["hi"] >= lo_v - 0.25 * span and q["lo"] <= hi_v + 0.25 * span]
    for q, col in zones:
        ax.axhspan(q["lo"], q["hi"], color=col, alpha=0.28 if q["touches"] >= 3 or q["h4"] else 0.16, linewidth=0)
        ax.text(len(cs) + 0.5, (q["lo"] + q["hi"]) / 2, f" {price(q['lo'])}–{price(q['hi'])}",
                color=col, fontsize=8, va="center")
        lo_v, hi_v = min(lo_v, q["lo"]), max(hi_v, q["hi"])
    ax.axhline(px, color="#2962ff", linestyle="--", linewidth=0.9)
    ax.text(0, px, f" {price(px)}", color="#2962ff", fontsize=8, va="bottom")
    pad = (hi_v - lo_v) * 0.04
    ax.set_ylim(lo_v - pad, hi_v + pad)
    ax.set_xlim(-1, len(cs) + 12)
    ax.tick_params(colors=fg, labelsize=8)
    ax.set_xticks([])
    for s in ax.spines.values():
        s.set_color("#2a2e39")
    ax.grid(color="#2a2e39", linewidth=0.5)
    ax.set_title(title, color=fg, fontsize=11, loc="left")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()
