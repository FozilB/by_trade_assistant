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
    """Как последняя закрытая свеча 1ч повела себя у зоны."""
    if c["h"] > z["hi"] and c["l"] < z["lo"]:
        return None           # проколола зону с обеих сторон — сигнала нет
    if c["h"] > z["hi"] and c["c"] < z["hi"] and c["o"] < z["hi"]:
        return "fake_up"      # прокол вверх тенью, закрытие обратно под зоной
    if c["l"] < z["lo"] and c["c"] > z["lo"] and c["o"] > z["lo"]:
        return "fake_down"    # прокол вниз тенью, закрытие обратно над зоной
    if c["c"] > z["hi"] and c["o"] <= z["hi"]:
        return "break_up"
    if c["c"] < z["lo"] and c["o"] >= z["lo"]:
        return "break_down"
    return None


# ================================================================ Star / Hammer: рабочая или ложная

def _zone_hit(lo, hi, zones):
    return next((z for z in zones if z and lo <= z["hi"] and hi >= z["lo"]), None)


def pattern_check(c1, tr, res, sup, at, a1):
    """Последняя Star/Hammer на 1ч. Возвращает (строки, info) или ([], None).
    info: kind 'star'/'hammer', status 'ok'/'wait'/'doubt'/'false', trigger — цена закрытия для подтверждения."""
    n = len(c1)
    rsi = S.rsi_series([x["c"] for x in c1])
    for back in range(A.PATTERN_LOOKBACK_1H):
        i = n - 1 - back
        if i < 15:
            break
        star, ham = S.is_star(c1, i, 10), S.is_hammer(c1, i)
        if not (star or ham):
            continue
        x, nxt = c1[i], c1[i + 1:]
        plus, minus = [], []
        if star:
            want, against, lvl_ok, lvl_bad = "down", "up", res + [at], sup
            trig, stop_side = x["l"], x["h"]
            rsi_ok = rsi[i] is not None and rsi[i] >= 55
        else:
            want, against, lvl_ok, lvl_bad = "up", "down", sup + [at], res
            trig, stop_side = x["h"], x["l"]
            rsi_ok = rsi[i] is not None and rsi[i] <= 45
        if tr == want:
            plus.append("по тренду")
        elif tr == against:
            minus.append("против тренда — такие часто ложные")
        if _zone_hit(x["l"], x["h"], lvl_ok):
            plus.append("у уровня")
        elif _zone_hit(x["l"], x["h"], lvl_bad):
            minus.append("не у того уровня (" + ("у поддержки" if star else "у сопротивления") + ")")
        else:
            minus.append("посередине, не у уровня")
        if rsi[i] is not None:
            if rsi_ok:
                plus.append(f"RSI {rsi[i]:.0f} — движение выдыхается")
            else:
                minus.append(f"RSI {rsi[i]:.0f} — " + ("роста перед ней почти не было" if star else "падения перед ним почти не было"))
        if (x["h"] - x["l"]) < 0.6 * a1:
            minus.append("свеча маленькая")
        closes = [y["c"] for y in nxt]
        confirmed = bool(closes) and (min(closes) < trig if star else max(closes) > trig)
        broken = bool(closes) and (max(closes) > stop_side if star else min(closes) < stop_side)
        if broken:
            minus.append("сломана: свеча закрылась за её " + ("максимумом" if star else "минимумом"))
        elif confirmed:
            plus.append("подтверждена закрытием свечи")
        if broken or len(minus) >= 2:
            status = "false"
        elif confirmed and not minus:
            status = "ok"
        elif not minus:
            status = "wait"
        else:
            status = "doubt"
        title = {"false": "❌ скорее ЛОЖНАЯ", "ok": "✅ рабочая, подтверждена",
                 "wait": "⏳ хорошая, но ждёт подтверждения", "doubt": "⚠️ сомнительная"}[status]
        name = "Star 🌠" if star else "Hammer 🔨"
        ago = "последняя свеча" if back == 0 else f"{back + 1} свеч. назад"
        lines = [f"{name} на 1ч ({ago}): <b>{title}</b>"]
        lines += [f"   ✓ {t}" for t in plus] + [f"   ✗ {t}" for t in minus]
        if status in ("wait", "doubt") and not confirmed:
            lines.append(f"   подтверждение: закрытие свечи {'ниже' if star else 'выше'} {price(trig)}")
        return lines, {"kind": "star" if star else "hammer", "status": status,
                       "trigger": None if confirmed or broken else trig, "extreme": stop_side}
    return [], None


# ================================================================ Один вывод: что делать сейчас

def _plan(side, entry, stop_edge, target_zone, a1):
    """Стоп за уровнем с запасом, цель у следующей зоны, плечо ≈ 50 / стоп%."""
    buf = A.ZONE_STOP_BUFFER_ATR1 * a1
    stop = stop_edge + buf if side == "short" else stop_edge - buf
    r = abs(stop - entry)
    if target_zone:
        tgt = target_zone["hi"] + 0.1 * a1 if side == "short" else target_zone["lo"] - 0.1 * a1
    else:
        tgt = entry - 2 * r if side == "short" else entry + 2 * r
    stop_pct = r / entry * 100
    rr = abs(tgt - entry) / r if r else 0
    return {"side": side, "entry": entry, "stop": stop, "target": tgt, "rr": rr,
            "lev": max(1, min(A.MAX_LEV_HINT, int(A.LIQ_SAFETY_PCT / stop_pct))) if stop_pct else None}


def _fmt_plan(p):
    e = p["entry"]
    s = (f"   стоп {price(p['stop'])} ({S.pct(p['stop'], e):+.1f}%) · цель {price(p['target'])} "
         f"({S.pct(p['target'], e):+.1f}%) · плечо до x{p['lev']}")
    if p["rr"] < A.MIN_RR_ZONES:
        s += f"\n   ⚠️ прибыль всего {p['rr']:.1f}× от риска — слабая сделка, лучше пропустить"
    return s


def decide(px, tr, res, sup, at, a1, c1, pat):
    """Возвращает строки: вывод, почему, при каком условии вход, отмена."""
    last = c1[-1]
    r0, s0 = (res[0] if res else None), (sup[0] if sup else None)
    side = {"down": "short", "up": "long"}.get(tr)
    word = {"short": "ШОРТ", "long": "ЛОНГ"}
    trend_line = {"down": "Тренд вниз → ищем только шорт.", "up": "Тренд вверх → ищем только лонг.",
                  "range": "Тренда нет (боковик) → шорт только от верхней зоны, лонг только от нижней."}[tr]
    pat_ok = pat if pat and pat["status"] != "false" and ((pat["kind"] == "star") == (side == "short")) else None

    def out(head, why, cond=None, plan=None, cancel=None):
        lines = [head, trend_line] + why
        if cond:
            lines += ["", cond]
        if plan:
            lines.append(_fmt_plan(plan))
        if cancel:
            lines.append(cancel)
        return lines

    if side is None:  # боковик
        why = ["Цена " + (f"в зоне {price(at['lo'])}–{price(at['hi'])}." if at else "посередине.")]
        cond = []
        if r0:
            cond.append(f"▶️ Шорт — только у {price(r0['lo'])}–{price(r0['hi'])} после свечи-отказа")
        if s0:
            cond.append(f"▶️ Лонг — только у {price(s0['lo'])}–{price(s0['hi'])} после свечи-отскока")
        return out("⏳ <b>СЕЙЧАС: НЕ ВХОДИТЬ — ждать</b>", why, "\n".join(cond) or None)

    short = side == "short"
    fwd = s0 if short else r0  # куда идём (цель)
    back = r0 if short else s0  # откуда ждём вход

    if at:
        z = at
        kind = candle_vs_zone(last, z)
        zone_txt = f"{price(z['lo'])}–{price(z['hi'])}"
        if kind == ("fake_up" if short else "fake_down"):
            plan = _plan(side, px, last["h"] if short else last["l"], fwd, a1)
            return out(f"✅ <b>СЕЙЧАС: МОЖНО {word[side]}</b>",
                       [f"Ложный пробой зоны {zone_txt}: цену вынесло {'вверх' if short else 'вниз'} и вернуло обратно — "
                        "это ловушка для тех, кто вошёл против тренда."],
                       f"▶️ {word[side]} от {price(px)}", plan,
                       f"❌ Отмена: закрытие 1ч {'выше' if short else 'ниже'} {price(plan['stop'])}")
        if pat_ok and pat_ok["status"] == "ok":
            plan = _plan(side, px, max(z["hi"], pat_ok["extreme"]) if short else min(z["lo"], pat_ok["extreme"]), fwd, a1)
            return out(f"✅ <b>СЕЙЧАС: МОЖНО {word[side]}</b>",
                       [f"Цена в зоне {zone_txt}, {'Star' if short else 'Hammer'} подтверждена."],
                       f"▶️ {word[side]} от {price(px)}", plan,
                       f"❌ Отмена: закрытие 1ч {'выше' if short else 'ниже'} {price(plan['stop'])}")
        if kind == ("break_down" if short else "break_up"):
            edge = z["lo"] if short else z["hi"]
            plan = _plan(side, edge, z["hi"] if short else z["lo"], fwd, a1)
            return out("⏳ <b>СЕЙЧАС: НЕ ДОГОНЯТЬ — ждать ретест</b>",
                       [f"Свеча 1ч закрылась {'под' if short else 'над'} зоной {zone_txt} — пробой по тренду."],
                       f"▶️ {word[side]}, если цена вернётся к {price(edge)} и не закрепится обратно", plan)
        # ждём сигнал у зоны
        why = [f"Цена в зоне {zone_txt} — здесь уже были развороты."]
        if kind == ("fake_down" if short else "fake_up"):
            why.append(f"Последнюю свечу выносило {'вниз' if short else 'вверх'} и вернуло — уровень пока держат "
                       f"{'покупатели' if short else 'продавцы'}. {'Покупать' if short else 'Шортить'} против тренда не стоит.")
        if pat_ok and pat_ok["trigger"]:
            trig = pat_ok["trigger"]
            why.append(f"Есть {'Star' if short else 'Hammer'}, но она ещё не подтверждена.")
        else:
            trig = z["lo"] if short else z["hi"]
            why.append("Сигнала на вход (свечи-разворота) ещё нет.")
        plan = _plan(side, trig, max(z["hi"], last["h"]) if short else min(z["lo"], last["l"]), fwd, a1)
        return out("⏳ <b>СЕЙЧАС: НЕ ВХОДИТЬ — ждать сигнал</b>", why,
                   f"▶️ {word[side]}, если свеча 1ч закроется {'ниже' if short else 'выше'} {price(trig)}", plan,
                   f"❌ Отмена: закрытие 1ч {'выше' if short else 'ниже'} {price(plan['stop'])}")

    # между зонами
    why = ["Цена посередине между уровнями: стоп будет далеко, а цель близко."]
    if back:
        edge = back["lo"] if short else back["hi"]
        plan = _plan(side, edge, back["hi"] if short else back["lo"], fwd, a1)
        return out("🚫 <b>СЕЙЧАС: НЕ ВХОДИТЬ</b>", why,
                   f"▶️ Ждать {'отката вверх' if short else 'отката вниз'} к зоне {price(back['lo'])}–{price(back['hi'])}, "
                   f"там — свечу-{'отказ' if short else 'отскок'} → {word[side]}", plan)
    return out("🚫 <b>СЕЙЧАС: НЕ ВХОДИТЬ</b>", why + ["Уровня для входа рядом нет."])


def report(m):
    """(главный вывод, подробности, данные для графика)."""
    c1, c4, px = m["c1"], m["c4"], m["px"]
    tr = trend(c4)
    res, sup, at, a1 = find_zones(c1, c4, px)
    plines, pat = pattern_check(c1, tr, res, sup, at, a1)
    main = decide(px, tr, res, sup, at, a1, c1, pat)
    det = ["🔴 Сопротивления выше: " + ("; ".join(fmt_zone(z) for z in res) or "нет")]
    if at:
        det.append(f"🟡 Цена сейчас в зоне: {fmt_zone(at)}")
    det.append("🟢 Поддержки ниже: " + ("; ".join(fmt_zone(z) for z in sup) or "нет"))
    if plines:
        det += [""] + plines
    return main, det, {"trend": tr, "res": res, "sup": sup, "at": at}


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
