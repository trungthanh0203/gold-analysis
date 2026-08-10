"""
api/analyze.py
----------------
Ham serverless (Vercel Python Runtime). Moi lan giao dien goi:
    GET /api/analyze?timeframe=H1

He thong phan tich THICH UNG theo khung thoi gian (dung goi y da thao luan):

  KHUNG NGAN (M5, M15, H1) -> uu tien:
      - Chi bao co ban: RSI, MACD, MA20/50, Bollinger
      - Price action: mo hinh nen (candlestick pattern)
      - Ichimoku Kinko Hyo (Tenkan/Kijun, vi tri gia so voi may Kumo)
      - Fibonacci Retracement
      - Doi chieu da khung (confluence voi khung lon hon lien ke)

  KHUNG DAI (H4, D1, W1) -> them vao:
      - Wyckoff-inspired: heuristic co/gian bien do + huong breakout
        (LUU Y: vang giao ngay khong co du lieu khoi luong thong nhat nen
        day la phien ban DON GIAN HOA dua tren bien do gia, khong phai
        phan tich Wyckoff day du theo sach vo)
      - Elliott-inspired: dem cau truc song swing don gian bang thuat
        toan zigzag (LUU Y: day la heuristic tham khao, khong thay the
        viec dem song Elliott chuyen sau can chuyen gia)
      - Lien thi truong: xu huong chi so DXY (dong USD) - vang thuong
        tuong quan NGHICH voi DXY

  MOI KHUNG THOI GIAN cung duoc bo sung 3 yeu to Smart Money Concepts (SMC):
      - Market Structure BOS/CHoCH (Break of Structure / Change of Character)
      - Fair Value Gap (FVG) - vung khoang trong gia chua duoc lap day
      - Liquidity Sweep - phat hien hanh vi quet thanh khoan tai dinh/day cu
        (LUU Y: day la cai dat heuristic don gian hoa cua SMC, khong thay
        the viec doc bieu do thu cong theo truong phai SMC chuyen sau)

  NGOAI RA: he thong chay 1 BACKTEST DON GIAN tren chinh du lieu lich su
  vua lay ve (khong bia so) de tinh ty le % cac lan tin hieu tuong tu
  (cung huong xu huong MA + cung vung RSI) da di dung huong trong qua
  khu gan day - giup nguoi dung co them can cu tham khao ve do tin cay,
  KHONG phai la loi hua ve ket qua tuong lai.

Tat ca duoc cham diem -1/0/+1 va cong lai thanh diem hop luu -100..+100.
Khong dung pandas/numpy -> khong can requirements.txt -> cold start nhanh.

Bien moi truong can cau hinh tren Vercel:
    TWELVEDATA_API_KEY = <api key mien phi tu twelvedata.com>
"""

import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from statistics import mean, pstdev

TD_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
TD_URL = "https://api.twelvedata.com/time_series"

INTERVAL_MAP = {
    "M5": "5min", "M15": "15min", "H1": "1h",
    "H4": "4h", "D1": "1day", "W1": "1week",
}
OUTPUT_SIZE = {
    "M5": 250, "M15": 250, "H1": 250, "H4": 250, "D1": 260, "W1": 200,
}
HIGHER_TF = {
    "M5": "H1", "M15": "H4", "H1": "H4", "H4": "D1", "D1": "W1", "W1": None,
}
SHORT_TFS = {"M5", "M15", "H1"}
LONG_TFS = {"H4", "D1", "W1"}


# ---------------------------------------------------------------------------
# 1. Lay du lieu that tu TwelveData
# ---------------------------------------------------------------------------
def fetch_series(symbol: str, timeframe: str, size: int = None):
    interval = INTERVAL_MAP.get(timeframe, "1h")
    size = size or OUTPUT_SIZE.get(timeframe, 200)
    params = {
        "symbol": symbol, "interval": interval,
        "outputsize": str(size), "order": "ASC", "apikey": TD_API_KEY,
    }
    url = TD_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "gold-analysis-page/1.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode())
    if "values" not in data:
        raise RuntimeError(str(data.get("message") or data.get("code") or "Loi khong xac dinh tu TwelveData"))
    return [
        {"time": v["datetime"], "open": float(v["open"]), "high": float(v["high"]),
         "low": float(v["low"]), "close": float(v["close"])}
        for v in data["values"]
    ]


# ---------------------------------------------------------------------------
# 2. Chi bao co ban (dung cho moi khung)
# ---------------------------------------------------------------------------
def sma(vals, period):
    return mean(vals[-period:]) if len(vals) >= period else None


def sma_series(vals, period):
    """Tra ve MA CA CHUOI (khong chi 1 gia tri cuoi) de ve duong len chart."""
    out = []
    for i in range(len(vals)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(round(mean(vals[i - period + 1:i + 1]), 2))
    return out


def ema_series(vals, period):
    if len(vals) < period:
        return []
    k = 2 / (period + 1)
    out = [mean(vals[:period])]
    for v in vals[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(vals, period=14):
    if len(vals) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_gain, avg_loss = mean(gains[:period]), mean(losses[:period])
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def macd(vals, fast=12, slow=26, signal=9):
    if len(vals) < slow + signal:
        return None, None
    ema_fast, ema_slow = ema_series(vals, fast), ema_series(vals, slow)
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    signal_line = ema_series(macd_line, signal)
    if not signal_line:
        return round(macd_line[-1], 3), None
    return round(macd_line[-1], 3), round(signal_line[-1], 3)


def bollinger(vals, period=20, mult=2):
    if len(vals) < period:
        return None, None, None
    window = vals[-period:]
    m = mean(window)
    sd = pstdev(window)
    return round(m - mult * sd, 2), round(m, 2), round(m + mult * sd, 2)


# ---------------------------------------------------------------------------
# 2b. Backtest don gian - tinh ty le % tin hieu tuong tu da dung huong
#     trong qua khu (dung CHINH du lieu that vua lay ve, khong bia so)
# ---------------------------------------------------------------------------
def rolling_sma(vals, period):
    out = [None] * len(vals)
    for i in range(period - 1, len(vals)):
        out[i] = mean(vals[i - period + 1:i + 1])
    return out


def rolling_rsi(vals, period=14):
    out = [None] * len(vals)
    if len(vals) < period + 1:
        return out
    gains, losses = [0.0], [0.0]
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_gain = avg_loss = None
    for i in range(period, len(vals)):
        if avg_gain is None:
            avg_gain = mean(gains[1:period + 1]); avg_loss = mean(losses[1:period + 1])
        else:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = 100.0 if avg_loss == 0 else round(100 - (100 / (1 + avg_gain / avg_loss)), 2)
    return out


def backtest_winrate(candles, lookahead=10):
    """Quet qua toan bo du lieu lich su da lay ve: tai moi diem trong qua
    khu, kiem tra xem 'thiet lap' (setup) co giong tinh trang hien tai
    khong (xu huong MA20/50 + vung RSI), roi xem gia co di dung huong sau
    N nen tiep theo hay khong. Tra ve ty le % thang cho ca 2 chieu MUA/BAN
    dua tren mau du lieu THAT, khong phai cong thuc suy dien."""
    closes = [c["close"] for c in candles]
    n = len(closes)
    if n < 90:
        return None
    ma20s, ma50s, rsis = rolling_sma(closes, 20), rolling_sma(closes, 50), rolling_rsi(closes, 14)

    buy_total = buy_win = sell_total = sell_win = 0
    for i in range(50, n - lookahead):
        if ma20s[i] is None or ma50s[i] is None or rsis[i] is None:
            continue
        if ma20s[i] > ma50s[i] and 40 <= rsis[i] <= 70:
            buy_total += 1
            if closes[i + lookahead] > closes[i]:
                buy_win += 1
        elif ma20s[i] < ma50s[i] and 30 <= rsis[i] <= 60:
            sell_total += 1
            if closes[i + lookahead] < closes[i]:
                sell_win += 1

    result = {}
    if buy_total > 0:
        result["buy"] = {"win_rate": round(buy_win / buy_total * 100, 1), "samples": buy_total}
    if sell_total > 0:
        result["sell"] = {"win_rate": round(sell_win / sell_total * 100, 1), "samples": sell_total}
    result["lookahead"] = lookahead
    return result


# ---------------------------------------------------------------------------
# 3. Price action - mo hinh nen
# ---------------------------------------------------------------------------
def _body(c): return abs(c["close"] - c["open"])
def _range(c): return c["high"] - c["low"]
def _is_bull(c): return c["close"] > c["open"]
def _is_bear(c): return c["close"] < c["open"]


def detect_candle_pattern(candles):
    if len(candles) < 3:
        return "Chua du du lieu", 0
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    body1, body3, small2 = _body(c1), _body(c3), _body(c2)
    mid1 = (c1["open"] + c1["close"]) / 2
    if _is_bear(c1) and body1 > 0 and small2 < body1 * 0.4 and _is_bull(c3) and c3["close"] > mid1:
        return "Morning Star (Sao Mai)", 1
    if _is_bull(c1) and body1 > 0 and small2 < body1 * 0.4 and _is_bear(c3) and c3["close"] < mid1:
        return "Evening Star (Sao Hom)", -1
    prev, curr = candles[-2], candles[-1]
    if _is_bear(prev) and _is_bull(curr) and curr["close"] >= prev["open"] and curr["open"] <= prev["close"]:
        return "Bullish Engulfing (Nen bao trum tang)", 1
    if _is_bull(prev) and _is_bear(curr) and curr["open"] >= prev["close"] and curr["close"] <= prev["open"]:
        return "Bearish Engulfing (Nen bao trum giam)", -1
    c = candles[-1]
    body, rng = _body(c), _range(c)
    if rng > 0:
        lower_wick = min(c["open"], c["close"]) - c["low"]
        upper_wick = c["high"] - max(c["open"], c["close"])
        if lower_wick > body * 2 and upper_wick < body * 0.6:
            return "Hammer (Bua)", 1
        if upper_wick > body * 2 and lower_wick < body * 0.6:
            return "Shooting Star (Sao boi)", -1
        if body / rng < 0.1:
            return "Doji (luong luy)", 0
    return "Khong co mo hinh ro ret", 0


# ---------------------------------------------------------------------------
# 4. Fibonacci Retracement
# ---------------------------------------------------------------------------
def fibonacci_position(candles, lookback=50):
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if len(window) < 10:
        return None
    swing_high = max(c["high"] for c in window)
    swing_low = min(c["low"] for c in window)
    diff = swing_high - swing_low
    if diff <= 0:
        return None
    last_close = candles[-1]["close"]
    levels = {"23.6%": swing_high - 0.236 * diff, "38.2%": swing_high - 0.382 * diff,
              "50.0%": swing_high - 0.5 * diff, "61.8%": swing_high - 0.618 * diff}
    nearest_name, nearest_val = min(levels.items(), key=lambda kv: abs(kv[1] - last_close))
    proximity = abs(nearest_val - last_close) / diff
    return {"nearest_level": nearest_name, "nearest_value": round(nearest_val, 2),
            "proximity": round(proximity, 4), "swing_high": round(swing_high, 2),
            "swing_low": round(swing_low, 2)}


# ---------------------------------------------------------------------------
# 5. Ichimoku Kinko Hyo (uu tien cho khung NGAN)
# ---------------------------------------------------------------------------
def ichimoku(candles, tenkan_p=9, kijun_p=26, senkou_b_p=52, displacement=26):
    if len(candles) < senkou_b_p + displacement + 1:
        return None

    def hh_ll(window):
        return max(c["high"] for c in window), min(c["low"] for c in window)

    def line_at(i, period):
        if i - period + 1 < 0:
            return None
        hh, ll = hh_ll(candles[i - period + 1:i + 1])
        return (hh + ll) / 2

    n = len(candles)
    idx_now = n - 1
    idx_cloud_base = idx_now - displacement  # may Kumo hien tai duoc "ve" tu du lieu cach day 26 nen

    tenkan_now = line_at(idx_now, tenkan_p)
    kijun_now = line_at(idx_now, kijun_p)
    if idx_cloud_base < 0 or tenkan_now is None or kijun_now is None:
        return None

    tenkan_base = line_at(idx_cloud_base, tenkan_p)
    kijun_base = line_at(idx_cloud_base, kijun_p)
    senkou_b_base = line_at(idx_cloud_base, senkou_b_p)
    if None in (tenkan_base, kijun_base, senkou_b_base):
        return None

    senkou_a = (tenkan_base + kijun_base) / 2
    senkou_b = senkou_b_base
    cloud_top, cloud_bottom = max(senkou_a, senkou_b), min(senkou_a, senkou_b)
    price = candles[idx_now]["close"]

    return {
        "tenkan": round(tenkan_now, 2), "kijun": round(kijun_now, 2),
        "senkou_a": round(senkou_a, 2), "senkou_b": round(senkou_b, 2),
        "cloud_top": round(cloud_top, 2), "cloud_bottom": round(cloud_bottom, 2),
        "price_above_cloud": price > cloud_top, "price_below_cloud": price < cloud_bottom,
        "tenkan_above_kijun": tenkan_now > kijun_now,
    }


# ---------------------------------------------------------------------------
# 5b. Smart Money Concepts (SMC) - ap dung cho MOI khung thoi gian
# ---------------------------------------------------------------------------
def market_structure_bos_choch(candles, pivots):
    """BOS (Break of Structure) = gia pha vo dinh/day cu theo huong xu huong
    hien tai -> xac nhan tiep dien. CHoCH (Change of Character) = gia pha
    vo nguoc huong xu huong hien tai -> canh bao dao chieu."""
    highs = [p for t, p in pivots if t == "H"]
    lows = [p for t, p in pivots if t == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return {"label": "Chua du diem xoay chieu de xac dinh cau truc BOS/CHoCH.", "direction": 0}

    last_high, prev_high = highs[-1], highs[-2]
    last_low, prev_low = lows[-1], lows[-2]
    current_close = candles[-1]["close"]
    uptrend_context = last_high > prev_high
    downtrend_context = last_low < prev_low

    if uptrend_context and current_close > last_high:
        return {"label": f"BOS tang: gia vuot dinh cu {round(last_high,2)} -> xac nhan tiep dien xu huong tang.", "direction": 1}
    if uptrend_context and current_close < last_low:
        return {"label": f"CHoCH: gia pha day {round(last_low,2)} du dang trong xu huong tang -> canh bao co the dao chieu giam.", "direction": -1}
    if downtrend_context and current_close < last_low:
        return {"label": f"BOS giam: gia vuot day cu {round(last_low,2)} -> xac nhan tiep dien xu huong giam.", "direction": -1}
    if downtrend_context and current_close > last_high:
        return {"label": f"CHoCH: gia vuot dinh {round(last_high,2)} du dang trong xu huong giam -> canh bao co the dao chieu tang.", "direction": 1}
    return {"label": "Cau truc thi truong hien chua co tin hieu BOS/CHoCH ro ret.", "direction": 0}


def fair_value_gap(candles, lookback=40):
    """FVG: khoang trong gia giua nen 1 va nen 3 (nen 2 khong lap day) -
    theo SMC day thuong la vung gia se quay lai 'lap day' truoc khi tiep
    tuc di theo huong cu."""
    window = candles[-lookback:] if len(candles) >= lookback else candles
    gaps = []
    for i in range(len(window) - 2):
        c1, c3 = window[i], window[i + 2]
        if c1["high"] < c3["low"]:
            gaps.append({"type": "bullish", "top": c3["low"], "bottom": c1["high"]})
        elif c1["low"] > c3["high"]:
            gaps.append({"type": "bearish", "top": c1["low"], "bottom": c3["high"]})
    if not gaps:
        return {"label": "Khong phat hien Fair Value Gap dang chu y trong du lieu gan day.", "direction": 0}
    nearest = gaps[-1]
    price = candles[-1]["close"]
    inside = nearest["bottom"] <= price <= nearest["top"]
    zone = f"{round(nearest['bottom'],2)} - {round(nearest['top'],2)}"
    if inside and nearest["type"] == "bullish":
        return {"label": f"Gia dang trong vung Fair Value Gap tang ({zone}) -> tiem nang ho tro ky thuat.", "direction": 1}
    if inside and nearest["type"] == "bearish":
        return {"label": f"Gia dang trong vung Fair Value Gap giam ({zone}) -> tiem nang khang cu ky thuat.", "direction": -1}
    return {"label": f"FVG {nearest['type']} gan nhat o vung {zone}, gia chua quay lai test.", "direction": 0}


def liquidity_sweep(candles, lookback=30, check_recent=5):
    """Phat hien hanh vi 'quet thanh khoan': gia pha vo dinh/day cu trong
    choc lat roi dong cua nguoc lai - dau hieu 'san' lenh dung/cat lo cua
    dam dong truoc khi dao chieu, thuong duoc trader SMC dung de vao lenh."""
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if len(window) < check_recent + 5:
        return {"label": "Chua du du lieu de kiem tra quet thanh khoan.", "direction": 0}
    ref = window[:-check_recent]
    ref_high = max(c["high"] for c in ref)
    ref_low = min(c["low"] for c in ref)
    for c in window[-check_recent:]:
        if c["high"] > ref_high and c["close"] < ref_high:
            return {"label": f"Phat hien quet thanh khoan tai dinh cu {round(ref_high,2)} roi dong cua duoi lai -> tin hieu dao chieu giam.", "direction": -1}
        if c["low"] < ref_low and c["close"] > ref_low:
            return {"label": f"Phat hien quet thanh khoan tai day cu {round(ref_low,2)} roi dong cua tren lai -> tin hieu dao chieu tang.", "direction": 1}
    return {"label": "Chua phat hien hanh vi quet thanh khoan ro ret gan day.", "direction": 0}



def wyckoff_heuristic(candles, recent_n=10, prior_n=20):
    if len(candles) < recent_n + prior_n:
        return None
    recent = candles[-recent_n:]
    prior = candles[-(recent_n + prior_n):-recent_n]
    recent_range = max(c["high"] for c in recent) - min(c["low"] for c in recent)
    prior_range = max(c["high"] for c in prior) - min(c["low"] for c in prior)
    if prior_range <= 0:
        return None
    contracting = recent_range < prior_range * 0.7
    last_close = candles[-1]["close"]
    range_high = max(c["high"] for c in recent[:-1])
    range_low = min(c["low"] for c in recent[:-1])
    if contracting and last_close > range_high:
        return {"phase": "Thoat tich luy, breakout huong len", "direction": 1, "contracting": True}
    if contracting and last_close < range_low:
        return {"phase": "Thoat phan phoi, breakout huong xuong", "direction": -1, "contracting": True}
    if contracting:
        return {"phase": "Dang trong giai doan bien do co hep (tich luy/phan phoi), chua breakout", "direction": 0, "contracting": True}
    return {"phase": "Bien do chua co hep ro ret, chua thay dau hieu tich luy/phan phoi", "direction": 0, "contracting": False}


# ---------------------------------------------------------------------------
# 7. Elliott-inspired swing heuristic (uu tien cho khung DAI)
# ---------------------------------------------------------------------------
def zigzag_pivots(candles, threshold_pct=0.015, window=200):
    data = candles[-window:] if len(candles) > window else candles
    pivots = []
    if len(data) < 5:
        return pivots
    last_pivot_price = data[0]["close"]
    last_pivot_type = None  # 'H' or 'L'
    trend = None
    for c in data:
        if trend is None:
            if c["close"] > last_pivot_price * (1 + threshold_pct):
                trend = "up"
            elif c["close"] < last_pivot_price * (1 - threshold_pct):
                trend = "down"
            continue
        if trend == "up":
            if c["high"] > last_pivot_price:
                last_pivot_price = c["high"]
            elif c["low"] < last_pivot_price * (1 - threshold_pct):
                pivots.append(("H", last_pivot_price))
                last_pivot_price = c["low"]
                trend = "down"
        else:
            if c["low"] < last_pivot_price:
                last_pivot_price = c["low"]
            elif c["high"] > last_pivot_price * (1 + threshold_pct):
                pivots.append(("L", last_pivot_price))
                last_pivot_price = c["high"]
                trend = "up"
    return pivots


def elliott_heuristic(candles):
    pivots = zigzag_pivots(candles)
    if len(pivots) < 4:
        return {"structure": "Chua du diem xoay chieu (pivot) de xac dinh cau truc song", "direction": 0}
    last4 = pivots[-4:]
    highs = [p for t, p in last4 if t == "H"]
    lows = [p for t, p in last4 if t == "L"]
    higher_highs = len(highs) >= 2 and highs == sorted(highs)
    higher_lows = len(lows) >= 2 and lows == sorted(lows)
    lower_highs = len(highs) >= 2 and highs == sorted(highs, reverse=True)
    lower_lows = len(lows) >= 2 and lows == sorted(lows, reverse=True)
    if higher_highs and higher_lows:
        return {"structure": "Cau truc dinh cao hon - day cao hon (giong song day tang)", "direction": 1}
    if lower_highs and lower_lows:
        return {"structure": "Cau truc dinh thap hon - day thap hon (giong song day giam)", "direction": -1}
    return {"structure": "Cau truc song dan xen, chua ro huong (co the dang trong song dieu chinh)", "direction": 0}


# ---------------------------------------------------------------------------
# 8. Lien thi truong - xu huong DXY (uu tien cho khung DAI)
# ---------------------------------------------------------------------------
def dxy_trend(timeframe):
    try:
        candles = fetch_series("DXY", timeframe, size=60)
        closes = [c["close"] for c in candles]
        if len(closes) < 20:
            return None
        slope_ref = closes[-20]
        last = closes[-1]
        change_pct = (last - slope_ref) / slope_ref * 100
        if change_pct > 0.15:
            return {"direction": -1, "change_pct": round(change_pct, 2)}  # DXY tang -> bat loi cho vang
        if change_pct < -0.15:
            return {"direction": 1, "change_pct": round(change_pct, 2)}  # DXY giam -> ho tro vang
        return {"direction": 0, "change_pct": round(change_pct, 2)}
    except Exception:
        return None  # neu goi khong duoc (han che du lieu chi so), bo qua yeu to nay


# ---------------------------------------------------------------------------
# 9. Doi chieu da khung
# ---------------------------------------------------------------------------
def higher_timeframe_trend(higher_tf_label, higher_candles):
    closes = [c["close"] for c in higher_candles]
    ma20, ma50 = sma(closes, 20), sma(closes, 50)
    if ma20 is None or ma50 is None:
        return None
    direction = 1 if ma20 > ma50 else -1
    return {"timeframe": higher_tf_label, "direction": direction, "ma20": round(ma20, 2), "ma50": round(ma50, 2)}


# ---------------------------------------------------------------------------
# 10. Tong hop hop luu - bo yeu to THICH UNG theo khung thoi gian
# ---------------------------------------------------------------------------
def build_signal(timeframe, candles, higher_tf_info):
    closes = [c["close"] for c in candles]
    if len(closes) < 90:
        return {"score": 0, "verdict": "CHUA DU DU LIEU", "details": {}, "values": {}, "reasons": [],
                "summary": f"Chi nhan duoc {len(closes)} nen, can toi thieu ~90 nen de tinh du cac yeu to phan tich."}

    is_short = timeframe in SHORT_TFS
    last_close = closes[-1]
    r = rsi(closes, 14)
    macd_v, macd_s = macd(closes, 12, 26, 9)
    ma20, ma50 = sma(closes, 20), sma(closes, 50)
    bb_l, bb_m, bb_u = bollinger(closes, 20, 2)
    pattern_name, pattern_dir = detect_candle_pattern(candles)
    fib = fibonacci_position(candles, 50)

    details, reasons = {}, []

    # --- Chi bao co ban (moi khung) ---
    if r < 30:
        details["RSI"] = 1; reasons.append(f"RSI dang o {r}, vung qua ban -> ap luc ban co the da suy yeu.")
    elif r > 70:
        details["RSI"] = -1; reasons.append(f"RSI dang o {r}, vung qua mua -> rui ro dieu chinh giam.")
    else:
        details["RSI"] = 0; reasons.append(f"RSI dang o {r}, vung trung tinh.")

    if macd_s is not None and macd_v > macd_s:
        details["MACD"] = 1; reasons.append(f"MACD ({macd_v}) tren duong tin hieu ({macd_s}) -> dong luong nghieng tang.")
    else:
        details["MACD"] = -1; reasons.append(f"MACD ({macd_v}) duoi duong tin hieu ({macd_s}) -> dong luong nghieng giam.")

    if ma20 > ma50:
        details["MA_Trend"] = 1; reasons.append(f"MA20 ({round(ma20,2)}) tren MA50 ({round(ma50,2)}) -> xu huong tang.")
    else:
        details["MA_Trend"] = -1; reasons.append(f"MA20 ({round(ma20,2)}) duoi MA50 ({round(ma50,2)}) -> xu huong giam.")

    if last_close <= bb_l:
        details["Bollinger"] = 1; reasons.append(f"Gia cham/xuyen dai duoi Bollinger ({bb_l}) -> kha nang bat lai.")
    elif last_close >= bb_u:
        details["Bollinger"] = -1; reasons.append(f"Gia cham/xuyen dai tren Bollinger ({bb_u}) -> ap luc chot loi.")
    else:
        details["Bollinger"] = 0; reasons.append(f"Gia dao dong trong dai Bollinger ({bb_l} - {bb_u}).")

    details["Candle_Pattern"] = pattern_dir
    reasons.append(f"Mo hinh nen (price action): {pattern_name}"
                    + (" -> tin hieu tang gia." if pattern_dir == 1 else
                       " -> tin hieu giam gia." if pattern_dir == -1 else " -> chua ro huong."))

    if fib:
        near = fib["proximity"] < 0.05
        trend_up = ma20 > ma50
        if near and trend_up:
            details["Fibonacci"] = 1
            reasons.append(f"Gia gan muc Fibonacci {fib['nearest_level']} ({fib['nearest_value']}) trong xu huong tang -> vung ho tro tiem nang.")
        elif near and not trend_up:
            details["Fibonacci"] = -1
            reasons.append(f"Gia gan muc Fibonacci {fib['nearest_level']} ({fib['nearest_value']}) trong xu huong giam -> vung khang cu tiem nang.")
        else:
            details["Fibonacci"] = 0
            reasons.append(f"Gia chua tiep can muc Fibonacci dang chu y (gan nhat: {fib['nearest_level']}).")
    else:
        details["Fibonacci"] = 0
        reasons.append("Chua du du lieu song gia de xac dinh Fibonacci.")

    if higher_tf_info:
        details["Da_khung"] = higher_tf_info["direction"]
        huong = "tang" if higher_tf_info["direction"] > 0 else "giam"
        reasons.append(f"Khung lon hon ({higher_tf_info['timeframe']}) dang xu huong {huong} -> dung doi chieu, tranh nguoc xu huong lon.")
    else:
        details["Da_khung"] = 0
        reasons.append("Day la khung lon nhat duoc ho tro (W1), khong co khung cao hon de doi chieu.")

    # --- Smart Money Concepts (SMC) - ap dung cho moi khung thoi gian ---
    pivots = zigzag_pivots(candles)
    structure = market_structure_bos_choch(candles, pivots)
    details["SMC_Structure"] = structure["direction"]
    reasons.append(f"[SMC - Cau truc thi truong] {structure['label']}")

    fvg = fair_value_gap(candles)
    details["SMC_FVG"] = fvg["direction"]
    reasons.append(f"[SMC - Fair Value Gap] {fvg['label']}")

    sweep = liquidity_sweep(candles)
    details["SMC_Liquidity"] = sweep["direction"]
    reasons.append(f"[SMC - Thanh khoan] {sweep['label']}")

    ichimoku_data, wyckoff_data, elliott_data, dxy_data = None, None, None, None

    if is_short:
        # --- Uu tien: Ichimoku cho khung ngan ---
        ichimoku_data = ichimoku(candles)
        if ichimoku_data:
            if ichimoku_data["price_above_cloud"] and ichimoku_data["tenkan_above_kijun"]:
                details["Ichimoku"] = 1
                reasons.append(f"Gia dang o TREN may Kumo (Ichimoku) va Tenkan cat len tren Kijun -> tin hieu tang manh theo Ichimoku.")
            elif ichimoku_data["price_below_cloud"] and not ichimoku_data["tenkan_above_kijun"]:
                details["Ichimoku"] = -1
                reasons.append(f"Gia dang o DUOI may Kumo (Ichimoku) va Tenkan cat xuong duoi Kijun -> tin hieu giam manh theo Ichimoku.")
            else:
                details["Ichimoku"] = 0
                reasons.append("Gia dang o trong hoac gan may Kumo (Ichimoku) -> xu huong chua ro rang, nen than trong.")
        else:
            details["Ichimoku"] = 0
            reasons.append("Chua du du lieu lich su de tinh may Kumo (Ichimoku).")
    else:
        # --- Them cho khung dai: Wyckoff, Elliott, DXY ---
        wyckoff_data = wyckoff_heuristic(candles)
        if wyckoff_data:
            details["Wyckoff"] = wyckoff_data["direction"]
            reasons.append(f"[Wyckoff-inspired, don gian hoa vi khong co du lieu khoi luong] {wyckoff_data['phase']}.")
        else:
            details["Wyckoff"] = 0

        elliott_data = elliott_heuristic(candles)
        details["Elliott"] = elliott_data["direction"]
        reasons.append(f"[Elliott-inspired, tham khao] {elliott_data['structure']}.")

        dxy_data = dxy_trend(timeframe)
        if dxy_data:
            details["DXY"] = dxy_data["direction"]
            if dxy_data["direction"] == 1:
                reasons.append(f"Chi so DXY (dong USD) giam {abs(dxy_data['change_pct'])}% gan day -> USD yeu thuong ho tro gia vang tang.")
            elif dxy_data["direction"] == -1:
                reasons.append(f"Chi so DXY (dong USD) tang {dxy_data['change_pct']}% gan day -> USD manh thuong gay ap luc len gia vang.")
            else:
                reasons.append("Chi so DXY (dong USD) di ngang, chua tao ap luc ro ret len gia vang.")
        else:
            details["DXY"] = 0
            reasons.append("Khong lay duoc du lieu DXY luc nay (co the goi mien phi khong ho tro chi so nay) -> bo qua yeu to lien thi truong.")

    raw = sum(details.values())
    max_score = len(details)
    score = int((raw / max_score) * 100) if max_score else 0
    buy_n = sum(1 for v in details.values() if v > 0)
    sell_n = sum(1 for v in details.values() if v < 0)

    if score >= 50:
        verdict = "MUA MANH" if score >= 75 else "MUA"
    elif score <= -50:
        verdict = "BAN MANH" if score <= -75 else "BAN"
    else:
        verdict = "TRUNG LAP"

    mode_label = ("Che do khung NGAN HAN: uu tien Price Action + Ichimoku + Fibonacci"
                  if is_short else
                  "Che do khung DAI HAN: ket hop them Wyckoff/Elliott (heuristic) + lien thi truong DXY")
    summary = (f"{mode_label}. {buy_n}/{max_score} yeu to nghieng mua, {sell_n}/{max_score} nghieng ban. "
               f"Diem hop luu: {score}/100 -> khuyen nghi: {verdict}.")

    values = {"rsi": r, "macd": macd_v, "macd_signal": macd_s, "ma20": round(ma20, 2), "ma50": round(ma50, 2),
              "bb_lower": bb_l, "bb_mid": bb_m, "bb_upper": bb_u, "pattern_name": pattern_name,
              "higher_tf_label": higher_tf_info["timeframe"] if higher_tf_info else "Khong co",
              "analysis_mode": "NGAN_HAN" if is_short else "DAI_HAN"}
    if ichimoku_data: values["ichimoku"] = ichimoku_data
    if wyckoff_data: values["wyckoff_phase"] = wyckoff_data["phase"]
    if elliott_data: values["elliott_structure"] = elliott_data["structure"]
    if dxy_data: values["dxy_change_pct"] = dxy_data["change_pct"]

    return {"score": score, "verdict": verdict, "details": details, "values": values,
            "reasons": reasons, "summary": summary}


# ---------------------------------------------------------------------------
# 11. Vercel Python handler
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        timeframe = qs.get("timeframe", ["H1"])[0]

        if timeframe not in INTERVAL_MAP:
            self._send(400, {"error": f"Khung thoi gian khong hop le. Chon trong: {list(INTERVAL_MAP)}"})
            return
        if not TD_API_KEY:
            self._send(500, {"error": "Chua cau hinh TWELVEDATA_API_KEY tren Vercel (xem file huong dan)."})
            return

        try:
            candles = fetch_series("XAU/USD", timeframe)

            higher_tf_info = None
            higher_label = HIGHER_TF.get(timeframe)
            if higher_label:
                higher_candles = fetch_series("XAU/USD", higher_label, size=100)
                higher_tf_info = higher_timeframe_trend(higher_label, higher_candles)

            signal = build_signal(timeframe, candles, higher_tf_info)

            closes_all = [c["close"] for c in candles]
            ma_lines = {
                "ma14": sma_series(closes_all, 14)[-150:],
                "ma34": sma_series(closes_all, 34)[-150:],
                "ma100": sma_series(closes_all, 100)[-150:],
            }

            bt = backtest_winrate(candles)
            backtest_info = None
            if bt:
                side = "buy" if signal["score"] >= 0 else "sell"
                stats = bt.get(side)
                if stats:
                    backtest_info = {
                        "direction": "MUA" if side == "buy" else "BAN",
                        "win_rate": stats["win_rate"],
                        "samples": stats["samples"],
                        "lookahead": bt["lookahead"],
                        "low_confidence": stats["samples"] < 15,
                    }

            self._send(200, {
                "timeframe": timeframe,
                "candles": candles[-150:],
                "moving_averages": ma_lines,
                "signal": signal,
                "backtest": backtest_info,
            })
        except Exception as e:
            self._send(502, {"error": f"Loi lay du lieu tu TwelveData: {e}"})

    def _send(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
