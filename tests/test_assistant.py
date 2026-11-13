"""Проверка помощника без интернета: Telegram и OKX подменены. python -m pytest -q"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import assistant as AS  # noqa: E402
import shortsetup as S  # noqa: E402

H1, M1 = S.H1, 60_000
T0 = 1_790_000_000_000 // S.H4 * S.H4


def series(points, bar_ms, wiggle=0.0015, start=T0):
    vals = []
    for (h0, p0), (h1, p1) in zip(points, points[1:]):
        for k in range(h1 - h0):
            vals.append(p0 + (p1 - p0) * k / (h1 - h0))
    vals.append(points[-1][1])
    out, prev = [], vals[0]
    for i, v in enumerate(vals):
        c = v * (1 + wiggle * (1 if i % 3 == 0 else -1 if i % 3 == 1 else 0))
        out.append({"ts": start + i * bar_ms, "o": prev, "c": c, "h": max(prev, c) * 1.001,
                    "l": min(prev, c) * 0.999, "v": 1.0, "closed": True})
        prev = c
    return out


DOWN = [(0, 0.90), (50, 1.00), (120, 0.80), (180, 0.92), (250, 0.74), (262, 0.785), (272, 0.765), (286, 0.795)]


class Market:
    """Подменённый OKX: 1ч из сценария, 4ч — из них, 15м и 1м — около текущей цены."""
    def __init__(self):
        self.c1 = series(DOWN, H1)
        self.px = self.c1[-1]["c"]
        self.minute = []  # свечи 1м, которые «придут» при сопровождении

    def fetch(self, inst, bar, limit):
        if inst.startswith("NOPE"):
            return None
        end = self.c1[-1]["ts"] + H1
        if bar == "1H":
            return self.c1[-limit:]
        if bar == "4H":
            return S.aggregate(self.c1, S.H4)[-limit:]
        if bar == "15m":
            base = [(0, self.px * 0.99), (40, self.px * 1.006), (70, self.px * 0.998), (99, self.px)]
            return series(base, S.M15, 0.0008, start=end - 100 * S.M15)[-limit:]
        if bar == "1m":
            return (self.minute or [{"ts": end, "o": self.px, "h": self.px, "l": self.px, "c": self.px,
                                     "v": 1, "closed": False}])[-limit:]
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    sent = []
    mk = Market()
    monkeypatch.setattr(AS, "STATE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(AS, "CHAT", "42")
    monkeypatch.setattr(AS, "TOKEN", "x")
    monkeypatch.setattr(S, "fetch_candles", mk.fetch)
    monkeypatch.setattr(S, "PAUSE", 0)

    def fake_tg(method, payload, timeout=30):
        if method == "sendMessage":
            sent.append(payload["text"])
            return {"message_id": len(sent)}
        return True
    monkeypatch.setattr(AS, "tg", fake_tg)
    clock = {"t": mk.c1[-1]["ts"] + H1 + 30_000}
    monkeypatch.setattr(AS, "now_ms", lambda: clock["t"])
    return {"sent": sent, "mk": mk, "clock": clock, "state": AS.load_state()}


# ---------------------------------------------------------------- разбор ввода

def test_parse_in_variants():
    d = AS.parse_in("aerousdt.p short 0.553 x20 10".split())
    assert (d["side"], d["entry"], d["lev"], d["margin"]) == ("short", 0.553, 20, 10)
    d = AS.parse_in("aero шорт 0,553 20х 10 стоп 0.57".split())
    assert (d["lev"], d["stop"], d["entry"]) == (20, 0.57, 0.553)
    d = AS.parse_in("near 3.687 x30".split())
    assert d["side"] == "short" and d["margin"] is None
    d = AS.parse_in("btc long 90000 x10 $50".split())
    assert d["side"] == "long" and d["margin"] == 50
    with pytest.raises(ValueError):
        AS.parse_in(["aero", "short"])


def test_norm_inst():
    for s in ("aero", "AEROUSDT.P", "aero-usdt-swap", "AERO/USDT", "aerousdt"):
        assert AS.norm_inst(s) == "AERO-USDT-SWAP"
    with pytest.raises(ValueError):
        AS.norm_inst("что?")


def test_num_comma_and_layout():
    assert AS.num("77,36") == 77.36 and AS.num("77ю5") == 77.5


# ---------------------------------------------------------------- план сделки

def test_plan_short(env):
    AS.handle_text(env["state"], "/in aero short 0.795 x20 10")
    pos = env["state"]["pos"]["AERO-USDT-SWAP"]
    assert pos["stop"] > pos["entry"] > pos["t1"] > pos["t2"]
    assert pos["qty"] == pytest.approx(200 / 0.795)
    txt = env["sent"][-1]
    assert "Стоп" in txt and "Цель 1" in txt and "Ликвидация" in txt and "$" in txt


def test_plan_warns_liquidation_before_stop(env):
    AS.handle_text(env["state"], "/in aero short 0.795 x100 10 стоп 0.82")  # стоп 3%, ликвидация ~0.5%
    assert "РАНЬШЕ СТОПА" in env["sent"][-1] or "раньше стопа" in env["sent"][-1]


def test_user_stop_in_noise_warned(env):
    AS.handle_text(env["state"], "/in aero short 0.795 x20 10 стоп 0.7955")
    assert "шума" in env["sent"][-1]


def test_wrong_side_stop_rejected(env):
    AS.handle_update(env["state"], {"update_id": 1, "message": {"chat": {"id": 42}, "date": 10**10,
                                                                "text": "/in aero short 0.795 x20 10 стоп 0.70"}}, 0)
    assert env["sent"][-1].startswith("❗")
    assert not env["state"]["pos"]


def test_unknown_coin(env):
    AS.handle_update(env["state"], {"update_id": 1, "message": {"chat": {"id": 42}, "date": 10**10,
                                                                "text": "/in nope short 1 x10 5"}}, 0)
    assert "OKX не отдал" in env["sent"][-1]


def test_analyze_and_plain_symbol(env):
    AS.handle_text(env["state"], "aero")
    assert "🔍" in env["sent"][-1] and "Сопротивления" in env["sent"][-1]


def test_webapp_form(env):
    data = json.dumps({"mode": "in", "sym": "aero", "side": "short", "entry": 0.795, "lev": 20, "margin": 10, "stop": None})
    AS.handle_update(env["state"], {"update_id": 5, "message": {"chat": {"id": 42}, "date": 10**10,
                                                                "web_app_data": {"data": data}}}, 0)
    assert "AERO-USDT-SWAP" in env["state"]["pos"]


def test_foreign_chat_ignored(env):
    AS.handle_update(env["state"], {"update_id": 1, "message": {"chat": {"id": 7}, "date": 10**10, "text": "/status"}}, 0)
    assert env["sent"] == []


# ---------------------------------------------------------------- сопровождение

def minutes(prices, start):
    return [{"ts": start + i * M1, "o": p, "h": p * 1.0002, "l": p * 0.9998, "c": p, "v": 1, "closed": True}
            for i, p in enumerate(prices)]


def test_tracking_breakeven_target1_target2(env):
    st, mk, clock = env["state"], env["mk"], env["clock"]
    AS.handle_text(st, "/in aero short 0.795 x20 10")
    pos = st["pos"]["AERO-USDT-SWAP"]
    e, r, t1, t2 = pos["entry"], pos["r0"], pos["t1"], pos["t2"]
    t = clock["t"]
    path = [e, e - 0.5 * r, e - 1.1 * r, (e + t1) / 2, t1 - 0.0001, (t1 + t2) / 2, t2 - 0.0001]
    mk.minute = minutes(path, t)
    clock["t"] = t + len(path) * M1
    AS.track_all(st)
    joined = "\n".join(env["sent"])
    assert "безубыток" in joined and "цель 1" in joined and "цель 2" in joined
    assert "AERO-USDT-SWAP" not in st["pos"]
    assert "Итог сделки" in env["sent"][-1]


def test_tracking_stop_hit(env):
    st, mk, clock = env["state"], env["mk"], env["clock"]
    AS.handle_text(st, "/in aero short 0.795 x20 10")
    pos = st["pos"]["AERO-USDT-SWAP"]
    t = clock["t"]
    mk.minute = minutes([pos["entry"], pos["stop"] + 0.0005], t)
    clock["t"] = t + 2 * M1
    AS.track_all(st)
    assert "стоп" in env["sent"][-1] and "−$" in env["sent"][-1]
    assert not st["pos"]


def test_near_stop_reminder_once(env):
    st, mk, clock = env["state"], env["mk"], env["clock"]
    AS.handle_text(st, "/in aero short 0.795 x20 10")
    pos = st["pos"]["AERO-USDT-SWAP"]
    t = clock["t"]
    near = pos["stop"] - 0.1 * pos["r0"]
    mk.minute = minutes([near * 0.9997], t)
    clock["t"] = t + M1
    AS.track_all(st)
    n = sum("близко к стопу" in x for x in env["sent"])
    clock["t"] += M1
    AS.track_all(st)
    assert n == 1 and sum("близко к стопу" in x for x in env["sent"]) == 1


def test_status_stop_out_commands(env):
    st = env["state"]
    AS.handle_text(st, "/in aero short 0.795 x20 10")
    AS.handle_text(st, "/status")
    assert "AERO" in env["sent"][-1] and "R" in env["sent"][-1]
    AS.handle_text(st, "/stop aero 0.80")
    assert st["pos"]["AERO-USDT-SWAP"]["stop"] == 0.80
    AS.handle_callback(st, {"id": "q", "data": "out:AERO"})
    assert not st["pos"] and "закрыто" in env["sent"][-1]


def test_run_loop_with_updates(env, monkeypatch):
    """Цикл запуска: получает команду из getUpdates, отвечает, сохраняет позицию и offset."""
    calls = {"n": 0}

    def fake_tg(method, payload, timeout=30):
        if method == "getUpdates":
            calls["n"] += 1
            if calls["n"] == 1:
                return [{"update_id": 100, "message": {"chat": {"id": 42}, "date": 10**10,
                                                        "text": "/in aero short 0.795 x20 10"}}]
            return []
        if method == "sendMessage":
            env["sent"].append(payload["text"])
        return True
    monkeypatch.setattr(AS, "tg", fake_tg)
    monkeypatch.setattr(AS.A, "TRACK_EVERY_SEC", 0)
    AS.run(seconds=1)
    st = AS.load_state()
    assert st["offset"] == 101 and "AERO-USDT-SWAP" in st["pos"]
