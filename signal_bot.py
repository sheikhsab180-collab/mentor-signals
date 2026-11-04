"""
Mentor Signals - CLOUD version (signal only, never trades).
Runs on GitHub Actions every 5 minutes, so your PC can be off.

Strategy (your mentor's notes):
 1. 30-minute trend from structure (HH+HL = up, LH+LL = down)
 2. 50%-75% discount zone of the last leg
 3. Price must pull back into the zone
 4. 5-minute break of structure = entry
 5. Stop beyond the 5M swing, take-profit = 2 x stop (1:2)
"""
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------- settings (from GitHub secrets / env) ----------------------
API_KEY = os.environ.get("TWELVE_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "XAU/USD").split(",") if s.strip()]
START_HOUR = int(os.environ.get("START_HOUR", "7"))     # UTC = Exness broker time
END_HOUR = int(os.environ.get("END_HOUR", "17"))
BALANCE = float(os.environ.get("BALANCE", "500"))
RISK_PCT = float(os.environ.get("RISK_PCT", "1"))
RR = 2.0
HTF_SWING, LTF_SWING = 3, 2
MAX_PULLBACK = 48          # 5M bars
RECENT_BREAK = 3           # a break counts as a fresh signal if it happened in the last 3 closed 5M candles
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# contract = units per 1.00 lot, buffer = extra price beyond the swing for the stop (covers spread)
SPECS = {
    "XAU/USD": dict(contract=100, buffer=0.30, digits=2),
    "EUR/USD": dict(contract=100000, buffer=0.00020, digits=5),
    "GBP/USD": dict(contract=100000, buffer=0.00020, digits=5),
}
DEFAULT_SPEC = dict(contract=100000, buffer=0.00020, digits=5)


# ---------------- helpers ---------------------------------------------------
def tg_send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print("Telegram error:", e)
        return False


def fetch(symbol, interval, n):
    """Returns candles newest-first: (time_utc, high, low, close)."""
    q = urllib.parse.urlencode(dict(symbol=symbol, interval=interval, outputsize=n,
                                    timezone="UTC", order="DESC", apikey=API_KEY))
    with urllib.request.urlopen("https://api.twelvedata.com/time_series?" + q, timeout=25) as r:
        d = json.load(r)
    if d.get("status") == "error" or "values" not in d:
        raise RuntimeError(d.get("message", str(d)))
    out = []
    for v in d["values"]:
        t = datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        out.append((t, float(v["high"]), float(v["low"]), float(v["close"])))
    return out


def split_forming(bars, minutes, now):
    """Separate the still-forming candle (if the feed includes it) from closed candles."""
    forming = None
    if bars and bars[0][0] + timedelta(minutes=minutes) > now:
        forming, bars = bars[0], bars[1:]
    return forming, bars


def find_swings(H, L, k, max_n):
    """Lists of (index, price), newest first. Index 0 = most recent closed candle."""
    n = len(H)
    hi, lo = [], []
    for i in range(k, n - k):
        if len(hi) < max_n and all(H[i] > H[i - j] and H[i] >= H[i + j] for j in range(1, k + 1)):
            hi.append((i, H[i]))
        if len(lo) < max_n and all(L[i] < L[i - j] and L[i] <= L[i + j] for j in range(1, k + 1)):
            lo.append((i, L[i]))
        if len(hi) >= max_n and len(lo) >= max_n:
            break
    return hi, lo


def lot_text(dist, spec):
    risk = BALANCE * RISK_PCT / 100.0
    loss_per_lot = dist * spec["contract"]
    if loss_per_lot <= 0:
        return "n/a"
    lots = math.floor(risk / loss_per_lot * 100) / 100
    if lots < 0.01:
        real = 0.01 * loss_per_lot / BALANCE * 100
        return f"0.01 (smallest lot - risks about {real:.1f}% not {RISK_PCT:g}%)"
    return f"{lots:.2f}"


# ---------------- strategy --------------------------------------------------
def analyze(sym, now, h_raw, l_raw):
    """Returns dict(stage, key, text). stage: none | forming | signal | missed."""
    spec = SPECS.get(sym, DEFAULT_SPEC)
    d = spec["digits"]
    f = lambda v: f"{v:.{d}f}"
    none = lambda why: dict(stage="none", key=None, text=why)

    _, hbars = split_forming(h_raw, 30, now)
    lform, lbars = split_forming(l_raw, 5, now)
    if len(hbars) < 60 or len(lbars) < 60:
        return none("not enough candles")
    HH, HL = [b[1] for b in hbars], [b[2] for b in hbars]
    hi, lo = find_swings(HH, HL, HTF_SWING, 6)
    if len(hi) < 3 or len(lo) < 3:
        return none("not enough swings")

    # Step 1: 30M trend
    up = down = False
    Hv = Lv = 0.0
    hIdx = lIdx = 0
    a = next((i for i, (idx, _) in enumerate(lo) if idx > hi[0][0]), -1)
    if a >= 0 and a + 1 < len(lo) and hi[0][1] > hi[1][1] and lo[a][1] > lo[a + 1][1]:
        up, Hv, hIdx, Lv, lIdx = True, hi[0][1], hi[0][0], lo[a][1], lo[a][0]
    b = next((i for i, (idx, _) in enumerate(hi) if idx > lo[0][0]), -1)
    dn = b >= 0 and b + 1 < len(hi) and lo[0][1] < lo[1][1] and hi[b][1] < hi[b + 1][1]
    if dn:
        if up:
            up = False
        else:
            down, Lv, lIdx, Hv, hIdx = True, lo[0][1], lo[0][0], hi[b][1], hi[b][0]
    if not up and not down:
        return none("no clear 30M trend")
    word = "BUY" if up else "SELL"
    if (hIdx > 0 and max(HH[:hIdx]) > Hv) or (lIdx > 0 and min(HL[:lIdx]) < Lv):
        return none("last 30M leg extended or broken")

    # Step 2: discount zone
    size = Hv - Lv
    if up:
        zTop, zBot = Hv - 0.50 * size, Hv - 0.75 * size
    else:
        zBot, zTop = Lv + 0.50 * size, Lv + 0.75 * size

    # Steps 3-4: 5M pullback into zone and structure break
    LH, LL, LC = [b[1] for b in lbars], [b[2] for b in lbars], [b[3] for b in lbars]
    lhi, llo = find_swings(LH, LL, LTF_SWING, 6)
    if len(lhi) < 2 or len(llo) < 2:
        return none("not enough 5M swings")
    found = False
    B = swPrice = 0.0
    swIdx = 0
    pool = llo if up else lhi
    other = lhi if up else llo
    for idx, price in pool:
        if idx > MAX_PULLBACK:
            break
        if up and (price > zTop or price <= Lv):
            continue
        if down and (price < zBot or price >= Hv):
            continue
        j = next((p for (i2, p) in other if i2 > idx), None)
        if j is None:
            break
        found, B, swPrice, swIdx = True, j, price, idx
        break
    if not found:
        return none("price not pulled back into the zone")

    swTime = lbars[swIdx][0]
    key = f"{sym}|{swTime.strftime('%Y-%m-%d %H:%M')}"
    buf = spec["buffer"]
    price_now = lform[3] if lform else LC[0]

    # first closed candle after the pullback swing that closed beyond the break level
    first = None
    for s in range(swIdx - 1, -1, -1):
        if (up and LC[s] > B) or (down and LC[s] < B):
            first = s
            break

    sl = swPrice - buf if up else swPrice + buf
    if first is None:
        plan_tp = B + RR * (B - sl) if up else B - RR * (sl - B)
        txt = (f"{sym}: {word} SETUP FORMING\n"
               f"30M trend is {'UP' if up else 'DOWN'} and price pulled back into the discount zone "
               f"({f(zBot)} - {f(zTop)}).\n"
               f"WAIT for a 5-minute candle to CLOSE {'ABOVE' if up else 'BELOW'} {f(B)}.\n"
               f"If it does: {word} around {f(B)}, stop loss {f(sl)}, take profit {f(plan_tp)}.\n"
               f"You will get a second message if the break happens.")
        return dict(stage="forming", key=key, text=txt)

    entry = price_now
    dist = abs(entry - sl)
    too_late = first > RECENT_BREAK - 1 or abs(entry - B) > 0.5 * abs(B - sl) or dist <= 0
    if too_late:
        return dict(stage="missed", key=key, text=f"{sym}: break already happened - too late, do not chase.")

    tp = entry + RR * dist if up else entry - RR * dist
    closed_at = lbars[first][0] + timedelta(minutes=5)
    txt = (f">>> {word} SIGNAL - {sym} <<<\n"
           f"5M candle closed {'above' if up else 'below'} {f(B)} at {closed_at.strftime('%H:%M')} UTC.\n"
           f"Entry (about): {f(entry)}\nStop loss: {f(sl)}\nTake profit: {f(tp)}  (1:{RR:g})\n"
           f"Lot for {RISK_PCT:g}% risk of {BALANCE:g}: {lot_text(dist, spec)}\n"
           f"Prices come from a data feed and can differ slightly from Exness. Check your MT5 price and the chart "
           f"before entering. If the price already moved far from the entry, skip it.")
    return dict(stage="signal", key=key, text=txt)


# ---------------- main ------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"alerts": {}, "hello": ""}


def save_state(st):
    st["alerts"] = dict(list(st["alerts"].items())[-40:])
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)


def main():
    if not (API_KEY and TG_TOKEN and TG_CHAT):
        print("Missing secrets: TWELVE_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID")
        sys.exit(1)

    now = datetime.now(timezone.utc)

    if os.environ.get("TEST") == "1":
        lines = ["Mentor Signals cloud bot: Telegram works."]
        for sym in SYMBOLS:
            try:
                bars = fetch(sym, "5min", 5)
                lines.append(f"{sym}: data OK, last price {bars[0][3]} (candle {bars[0][0].strftime('%H:%M')} UTC)")
            except Exception as e:
                lines.append(f"{sym}: DATA PROBLEM - {e}")
        tg_send("\n".join(lines))
        print("\n".join(lines))
        return

    if now.weekday() >= 5 or not (START_HOUR <= now.hour < END_HOUR):
        print("Outside trading hours - nothing to do.")
        return

    st = load_state()
    today = now.strftime("%Y-%m-%d")
    if st.get("hello") != today:
        st["hello"] = today
        tg_send(f"Good morning. Cloud bot is running and watching {', '.join(SYMBOLS)} "
                f"until {END_HOUR}:00 UTC. You'll get a message only when a setup forms or a signal fires.")

    for sym in SYMBOLS:
        try:
            h_raw = fetch(sym, "30min", 400)
            l_raw = fetch(sym, "5min", 200)
            res = analyze(sym, now, h_raw, l_raw)
        except Exception as e:
            print(sym, "error:", e)
            continue
        print(sym, res["stage"], res.get("key"), "|", res["text"].replace("\n", " "))
        key, stage = res.get("key"), res["stage"]
        if stage in ("forming", "signal") and key:
            done = st["alerts"].get(key)
            if stage == "forming" and done is None:
                if tg_send(res["text"]):
                    st["alerts"][key] = "forming"
            elif stage == "signal" and done != "signal":
                if tg_send(res["text"]):
                    st["alerts"][key] = "signal"
        elif stage == "missed" and key and key not in st["alerts"]:
            st["alerts"][key] = "missed"   # remember it, no message
    save_state(st)


if __name__ == "__main__":
    main()
