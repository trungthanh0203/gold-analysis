"""
api/analyze.py
----------------
Ham serverless (Vercel Python Runtime). Moi lan giao dien goi:
    GET /api/analyze?timeframe=H1

He thong phan tich thich ung theo khung thoi gian:

  KHUNG NGAN (M3, M5, M15, M30, H1) -> uu tien:
      - Chi bao co ban: RSI, MACD, MA20/50, Bollinger
      - Price action: mo hinh nen (candlestick pattern)
      - Ichimoku Kinko Hyo
      - Fibonacci Retracement
      - Doi chieu da khung (confluence voi khung lon hon lien ke)

  KHUNG DAI (H4, D1, W1) -> them vao:
      - Wyckoff-inspired (don gian hoa, khong co du lieu khoi luong)
      - Elliott-inspired (heuristic swing, khong thay the phan tich chuyen sau)
      - Lien thi truong: xu huong chi so DXY

  MOI KHUNG THOI GIAN cung duoc bo sung 3 yeu to Smart Money Concepts (SMC):
      - Market Structure BOS/CHoCH, Fair Value Gap, Liquidity Sweep

  NGOAI RA: chay 1 backtest don gian tren du lieu lich su that de tinh ty le
  % thang tham khao, va 1 ban do gia (Pivot + ATR) uoc luong xac suat cham
  cac muc gia. Tat ca noi dung tra ve deu viet bang tieng Viet co dau.

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

# TwelveData khong ho tro khung "3min" truc tiep, nen M3 duoc tu xay dung
# bang cach lay du lieu 1min that roi gop moi 3 nen lai (van la du lieu that).
BASE_INTERVAL = {
    "M3": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
    "H1": "1h", "H4": "4h", "D1": "1day", "W1": "1week",
}
AGGREGATE_FACTOR = {"M3": 3}
INTERVAL_MAP = BASE_INTERVAL
OUTPUT_SIZE = {
    "M3": 750, "M5": 250, "M15": 250, "M30": 250, "H1": 250, "H4": 250, "D1": 260, "W1": 200,
}
HIGHER_TF = {
    "M3": "M30", "M5": "H1", "M15": "H4", "M30": "H4",
    "H1": "H4", "H4": "D1", "D1": "W1", "W1": None,
}
SHORT_TFS = {"M3", "M5", "M15", "M30", "H1"}
LONG_TFS = {"H4", "D1", "W1"}

# Nhan hien thi tieng Viet cho tung yeu to trong bang chi tiet dong gop diem
# (backend van dung key ky thuat khong dau lam ID noi bo cho on dinh, nhung
# gui kem bang nay de frontend hien thi dung tieng Viet co dau).
DETAIL_LABELS_VI = {
    "RSI": "RSI", "MACD": "MACD", "MA_Trend": "Xu hướng MA", "Bollinger": "Bollinger",
    "Candle_Pattern": "Mô hình nến", "Fibonacci": "Fibonacci", "Da_khung": "Đa khung",
    "SMC_Structure": "Cấu trúc SMC", "SMC_FVG": "FVG (SMC)", "SMC_Liquidity": "Thanh khoản SMC",
    "Ichimoku": "Ichimoku", "Wyckoff": "Wyckoff", "Elliott": "Elliott", "DXY": "DXY (USD)",
}
VI_TYPE = {"bullish": "tăng", "bearish": "giảm"}


# ---------------------------------------------------------------------------
# 1. Lay du lieu that tu TwelveData (+ tu gop nen cho M3)
# ---------------------------------------------------------------------------
def aggregate_candles(candles, group_size):
    grouped = []
    for i in range(0, len(candles) - group_size + 1, group_size):
        chunk = candles[i:i + group_size]
        grouped.append({
            "time": chunk[0]["time"],
            "open": chunk[0]["open"],
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"],
        })
    return grouped


def fetch_series(symbol: str, timeframe: str, size: int = None):
    base_interval = BASE_INTERVAL.get(timeframe, "1h")
    size = size or OUTPUT_SIZE.get(timeframe, 200)
    params = {
        "symbol": symbol, "interval": base_interval,
        "outputsize": str(size), "order": "ASC", "apikey": TD_API_KEY,
    }
    url = TD_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "gold-analysis-page/1.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode())
    if "values" not in data:
        raise RuntimeError(str(data.get("message") or data.get("code") or "Lỗi không xác định từ TwelveData"))
    candles = [
        {"time": v["datetime"], "open": float(v["open"]), "high": float(v["high"]),
         "low": float(v["low"]), "close": float(v["close"])}
        for v in data["values"]
    ]
    factor = AGGREGATE_FACTOR.get(timeframe, 1)
    if factor > 1:
        candles = aggregate_candles(candles, factor)
    return candles


# ---------------------------------------------------------------------------
# 2. Chi bao co ban (dung cho moi khung)
# ---------------------------------------------------------------------------
def sma(vals, period):
    return mean(vals[-period:]) if len(vals) >= period else None


def sma_series(vals, period):
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
# 2b. Backtest don gian - ty le % tin hieu tuong tu da dung huong trong qua khu
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
# 2c. Ban do gia: Pivot Points (3 muc len / 3 muc xuong) + xac suat cham muc
#     (thuc nghiem lich su) + danh gia kha nang phan ung tai tung muc
# ---------------------------------------------------------------------------
def compute_atr_series(candles, period=14):
    n = len(candles)
    tr = [None] * n
    for i in range(1, n):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr = [None] * n
    for i in range(period, n):
        vals = [tr[j] for j in range(i - period + 1, i + 1) if tr[j] is not None]
        if len(vals) == period:
            atr[i] = mean(vals)
    return atr


def pivot_center(candles):
    if len(candles) < 2:
        return None
    ref = candles[-2]
    H, L, C = ref["high"], ref["low"], ref["close"]
    return round((H + L + C) / 3, 2)


def empirical_move_distribution(candles, lookahead=20):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    atr = compute_atr_series(candles, 14)
    n = len(candles)
    up_moves, down_moves = [], []
    for i in range(20, n - lookahead):
        if not atr[i]:
            continue
        future_high = max(highs[i + 1:i + 1 + lookahead])
        future_low = min(lows[i + 1:i + 1 + lookahead])
        up_moves.append((future_high - closes[i]) / atr[i])
        down_moves.append((closes[i] - future_low) / atr[i])
    current_atr = atr[-1]
    return up_moves, down_moves, current_atr


def prob_reach(moves_list, target_atr_distance):
    if not moves_list or target_atr_distance is None:
        return None
    count = sum(1 for m in moves_list if m >= target_atr_distance)
    return round(count / len(moves_list) * 100, 1)


def confluence_check(level_price, fib, bb_upper, bb_lower, fvg_zones, tolerance_pct=0.003):
    matches = []
    if fib:
        for name, val in fib["levels"].items():
            if abs(val - level_price) / level_price < tolerance_pct:
                matches.append(f"Fibonacci {name}")
    if bb_upper and abs(bb_upper - level_price) / level_price < tolerance_pct:
        matches.append("dải trên Bollinger")
    if bb_lower and abs(bb_lower - level_price) / level_price < tolerance_pct:
        matches.append("dải dưới Bollinger")
    for g in fvg_zones:
        if g["bottom"] <= level_price <= g["top"]:
            matches.append(f"vùng Fair Value Gap {VI_TYPE.get(g['type'], g['type'])}")
    nearest_round = round(level_price / 10) * 10
    if abs(nearest_round - level_price) < 2:
        matches.append(f"mốc tâm lý tròn số (~{nearest_round})")
    return matches


def reaction_label(matches):
    if len(matches) >= 2:
        return ("Cao", f"Trùng với {', '.join(matches)} -> khả năng cao giá sẽ PHẢN ỨNG (đảo chiều hoặc "
                        f"giằng co) tại đây thay vì đi xuyên qua ngay.")
    if len(matches) == 1:
        return ("Trung bình", f"Trùng với {matches[0]} -> có thể có phản ứng nhẹ, nhưng cũng có khả năng "
                               f"giá chỉ giằng co rồi TIẾP DIỄN xu hướng cũ, chưa đủ mạnh để khẳng định đảo chiều.")
    return ("Thấp", "Không trùng vùng kỹ thuật đáng chú ý nào khác -> nhiều khả năng giá sẽ TIẾP DIỄN "
                     "(đi xuyên qua mức này) hơn là đảo chiều mạnh tại đây.")


def build_price_map(candles, signal):
    pivot = pivot_center(candles)
    if pivot is None or len(candles) < 90:
        return None
    up_moves, down_moves, current_atr = empirical_move_distribution(candles, lookahead=20)
    if not current_atr:
        return None
    closes = [c["close"] for c in candles]
    fib = fibonacci_position(candles, 50)
    bb_l, bb_m, bb_u = bollinger(closes, 20, 2)
    fvg_zones = find_all_fvg(candles, 40)
    last_close = candles[-1]["close"]

    atr_multiples = {"1": 1.0, "2": 2.0, "3": 3.5}
    pivots = {"pivot": pivot}
    for suffix, mult in atr_multiples.items():
        pivots[f"R{suffix}"] = round(pivot + mult * current_atr, 2)
        pivots[f"S{suffix}"] = round(pivot - mult * current_atr, 2)

    def build_level(name, price, direction):
        if direction == "up" and price <= last_close:
            return {"label": name, "price": price, "probability": None, "already_passed": True,
                    "reaction_confidence": "—", "reaction_note": "Giá hiện đã ở trên mức này."}
        if direction == "down" and price >= last_close:
            return {"label": name, "price": price, "probability": None, "already_passed": True,
                    "reaction_confidence": "—", "reaction_note": "Giá hiện đã ở dưới mức này."}
        dist_atr = abs(price - last_close) / current_atr
        prob = prob_reach(up_moves if direction == "up" else down_moves, dist_atr)
        matches = confluence_check(price, fib, bb_u, bb_l, fvg_zones)
        conf_label, conf_text = reaction_label(matches)
        return {"label": name, "price": price, "probability": prob, "already_passed": False,
                "reaction_confidence": conf_label, "reaction_note": conf_text}

    up_levels = [build_level(n, pivots[n], "up") for n in ("R1", "R2", "R3")]
    down_levels = [build_level(n, pivots[n], "down") for n in ("S1", "S2", "S3")]

    side_text = "TRÊN" if last_close > pivot else "DƯỚI"
    lean_text = "tăng" if last_close > pivot else "giảm"
    narrative = (f"Hệ thống đa yếu tố đánh giá xu hướng hiện tại là {signal['verdict']} "
                 f"(điểm hợp lưu {signal['score']}/100). Giá đang ở {side_text} đường pivot trung tâm "
                 f"({pivot}), thiên về phía {lean_text} trong ngắn hạn. Các mức R1-R3/S1-S3 được giãn cách "
                 f"theo biến động thực tế (ATR = {round(current_atr,2)}) nên càng xa giá hiện tại thì xác "
                 f"suất chạm tới càng thấp. Đây là vùng giá tham khảo - không phải điểm vào/thoát lệnh bắt "
                 f"buộc, bạn tự cân nhắc kết hợp với khẩu vị rủi ro của mình.")

    return {"pivot": pivot, "up_levels": up_levels, "down_levels": down_levels,
            "narrative": narrative, "atr": round(current_atr, 2)}


# ---------------------------------------------------------------------------
# 3. Price action - mo hinh nen
# ---------------------------------------------------------------------------
def _body(c): return abs(c["close"] - c["open"])
def _range(c): return c["high"] - c["low"]
def _is_bull(c): return c["close"] > c["open"]
def _is_bear(c): return c["close"] < c["open"]


def detect_candle_pattern(candles):
    if len(candles) < 3:
        return "Chưa đủ dữ liệu", 0
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    body1, body3, small2 = _body(c1), _body(c3), _body(c2)
    mid1 = (c1["open"] + c1["close"]) / 2
    if _is_bear(c1) and body1 > 0 and small2 < body1 * 0.4 and _is_bull(c3) and c3["close"] > mid1:
        return "Morning Star (Sao Mai)", 1
    if _is_bull(c1) and body1 > 0 and small2 < body1 * 0.4 and _is_bear(c3) and c3["close"] < mid1:
        return "Evening Star (Sao Hôm)", -1
    prev, curr = candles[-2], candles[-1]
    if _is_bear(prev) and _is_bull(curr) and curr["close"] >= prev["open"] and curr["open"] <= prev["close"]:
        return "Bullish Engulfing (Nến bao trùm tăng)", 1
    if _is_bull(prev) and _is_bear(curr) and curr["open"] >= prev["close"] and curr["close"] <= prev["open"]:
        return "Bearish Engulfing (Nến bao trùm giảm)", -1
    c = candles[-1]
    body, rng = _body(c), _range(c)
    if rng > 0:
        lower_wick = min(c["open"], c["close"]) - c["low"]
        upper_wick = c["high"] - max(c["open"], c["close"])
        if lower_wick > body * 2 and upper_wick < body * 0.6:
            return "Hammer (Búa)", 1
        if upper_wick > body * 2 and lower_wick < body * 0.6:
            return "Shooting Star (Sao băng)", -1
        if body / rng < 0.1:
            return "Doji (lưỡng lự)", 0
    return "Không có mô hình rõ rệt", 0


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
            "swing_low": round(swing_low, 2), "levels": {k: round(v, 2) for k, v in levels.items()}}


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
    idx_cloud_base = idx_now - displacement

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
    highs = [p for t, p in pivots if t == "H"]
    lows = [p for t, p in pivots if t == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return {"label": "Chưa đủ điểm xoay chiều để xác định cấu trúc BOS/CHoCH.", "direction": 0}

    last_high, prev_high = highs[-1], highs[-2]
    last_low, prev_low = lows[-1], lows[-2]
    current_close = candles[-1]["close"]
    uptrend_context = last_high > prev_high
    downtrend_context = last_low < prev_low

    if uptrend_context and current_close > last_high:
        return {"label": f"BOS tăng: giá vượt đỉnh cũ {round(last_high,2)} -> xác nhận tiếp diễn xu hướng tăng.", "direction": 1}
    if uptrend_context and current_close < last_low:
        return {"label": f"CHoCH: giá phá đáy {round(last_low,2)} dù đang trong xu hướng tăng -> cảnh báo có thể đảo chiều giảm.", "direction": -1}
    if downtrend_context and current_close < last_low:
        return {"label": f"BOS giảm: giá vượt đáy cũ {round(last_low,2)} -> xác nhận tiếp diễn xu hướng giảm.", "direction": -1}
    if downtrend_context and current_close > last_high:
        return {"label": f"CHoCH: giá vượt đỉnh {round(last_high,2)} dù đang trong xu hướng giảm -> cảnh báo có thể đảo chiều tăng.", "direction": 1}
    return {"label": "Cấu trúc thị trường hiện chưa có tín hiệu BOS/CHoCH rõ rệt.", "direction": 0}


def find_all_fvg(candles, lookback=40):
    window = candles[-lookback:] if len(candles) >= lookback else candles
    gaps = []
    for i in range(len(window) - 2):
        c1, c3 = window[i], window[i + 2]
        if c1["high"] < c3["low"]:
            gaps.append({"type": "bullish", "top": c3["low"], "bottom": c1["high"]})
        elif c1["low"] > c3["high"]:
            gaps.append({"type": "bearish", "top": c1["low"], "bottom": c3["high"]})
    return gaps


def fair_value_gap(candles, lookback=40):
    gaps = find_all_fvg(candles, lookback)
    if not gaps:
        return {"label": "Không phát hiện Fair Value Gap đáng chú ý trong dữ liệu gần đây.", "direction": 0}
    nearest = gaps[-1]
    price = candles[-1]["close"]
    inside = nearest["bottom"] <= price <= nearest["top"]
    zone = f"{round(nearest['bottom'],2)} - {round(nearest['top'],2)}"
    if inside and nearest["type"] == "bullish":
        return {"label": f"Giá đang trong vùng Fair Value Gap tăng ({zone}) -> tiềm năng hỗ trợ kỹ thuật.", "direction": 1}
    if inside and nearest["type"] == "bearish":
        return {"label": f"Giá đang trong vùng Fair Value Gap giảm ({zone}) -> tiềm năng kháng cự kỹ thuật.", "direction": -1}
    return {"label": f"FVG {VI_TYPE.get(nearest['type'], nearest['type'])} gần nhất ở vùng {zone}, giá chưa quay lại test.", "direction": 0}


# ---------------------------------------------------------------------------
# 5c. Goi y lenh cho (Buy/Sell Limit/Stop) tu hop luu Fibonacci + FVG
# ---------------------------------------------------------------------------
def pending_order_suggestion(candles, score):
    fib = fibonacci_position(candles, 50)
    if not fib:
        return None
    bias = 1 if score > 0 else (-1 if score < 0 else 0)
    if bias == 0:
        return None

    last_close = candles[-1]["close"]
    swing_high, swing_low = fib["swing_high"], fib["swing_low"]
    extension = swing_high - swing_low
    if extension <= 0:
        return None
    gaps = find_all_fvg(candles, 40)
    golden_low, golden_high = fib["levels"]["61.8%"], fib["levels"]["38.2%"]

    if bias > 0:
        if last_close >= swing_high * 0.998:
            entry = round(swing_high * 1.0015, 2)
            sl = round(swing_high - extension * 0.25, 2)
            tp = round(entry + extension, 2)
            return {"order_type": "Buy Stop", "entry": entry, "sl": sl, "tp": tp,
                    "reason": f"Giá đang ở sát đỉnh sóng gần nhất ({swing_high}) trong xu hướng tăng -> chờ giá XÁC NHẬN phá vỡ bằng lệnh Buy Stop phía trên, tránh vào sớm khi chưa breakout thật."}
        matched = [g for g in gaps if g["type"] == "bullish" and g["bottom"] <= golden_high and g["top"] >= golden_low]
        if matched:
            g = matched[-1]
            entry = round((g["top"] + g["bottom"]) / 2, 2)
            sl = round(g["bottom"] - extension * 0.1, 2)
            tp = round(swing_high, 2)
            return {"order_type": "Buy Limit", "entry": entry, "sl": sl, "tp": tp,
                    "reason": f"Vùng hợp lưu Fibonacci (38.2%-61.8%: {golden_low}-{golden_high}) trùng với Fair Value Gap tăng ({g['bottom']}-{g['top']}) -> đặt lệnh Buy Limit chờ giá hồi về đây trước khi tiếp tục tăng."}
        entry = golden_low
        sl = round(swing_low - extension * 0.1, 2)
        tp = round(swing_high, 2)
        return {"order_type": "Buy Limit", "entry": entry, "sl": sl, "tp": tp,
                "reason": f"Chưa tìm thấy Fair Value Gap trùng khớp, đặt lệnh Buy Limit tại mức Fibonacci 61.8% ({entry}) - vùng hồi thoái lui phổ biến trước khi tiếp diễn xu hướng tăng."}

    if last_close <= swing_low * 1.002:
        entry = round(swing_low * 0.9985, 2)
        sl = round(swing_low + extension * 0.25, 2)
        tp = round(entry - extension, 2)
        return {"order_type": "Sell Stop", "entry": entry, "sl": sl, "tp": tp,
                "reason": f"Giá đang ở sát đáy sóng gần nhất ({swing_low}) trong xu hướng giảm -> chờ giá XÁC NHẬN phá vỡ bằng lệnh Sell Stop phía dưới, tránh vào sớm khi chưa breakout thật."}
    matched = [g for g in gaps if g["type"] == "bearish" and g["bottom"] <= golden_high and g["top"] >= golden_low]
    if matched:
        g = matched[-1]
        entry = round((g["top"] + g["bottom"]) / 2, 2)
        sl = round(g["top"] + extension * 0.1, 2)
        tp = round(swing_low, 2)
        return {"order_type": "Sell Limit", "entry": entry, "sl": sl, "tp": tp,
                "reason": f"Vùng hợp lưu Fibonacci (38.2%-61.8%: {golden_low}-{golden_high}) trùng với Fair Value Gap giảm ({g['bottom']}-{g['top']}) -> đặt lệnh Sell Limit chờ giá hồi lên vùng này trước khi tiếp tục giảm."}
    entry = golden_high
    sl = round(swing_high + extension * 0.1, 2)
    tp = round(swing_low, 2)
    return {"order_type": "Sell Limit", "entry": entry, "sl": sl, "tp": tp,
            "reason": f"Chưa tìm thấy Fair Value Gap trùng khớp, đặt lệnh Sell Limit tại mức Fibonacci 38.2% ({entry}) - vùng hồi lên phổ biến trước khi tiếp diễn xu hướng giảm."}


def liquidity_sweep(candles, lookback=30, check_recent=5):
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if len(window) < check_recent + 5:
        return {"label": "Chưa đủ dữ liệu để kiểm tra quét thanh khoản.", "direction": 0}
    ref = window[:-check_recent]
    ref_high = max(c["high"] for c in ref)
    ref_low = min(c["low"] for c in ref)
    for c in window[-check_recent:]:
        if c["high"] > ref_high and c["close"] < ref_high:
            return {"label": f"Phát hiện quét thanh khoản tại đỉnh cũ {round(ref_high,2)} rồi đóng cửa dưới lại -> tín hiệu đảo chiều giảm.", "direction": -1}
        if c["low"] < ref_low and c["close"] > ref_low:
            return {"label": f"Phát hiện quét thanh khoản tại đáy cũ {round(ref_low,2)} rồi đóng cửa trên lại -> tín hiệu đảo chiều tăng.", "direction": 1}
    return {"label": "Chưa phát hiện hành vi quét thanh khoản rõ rệt gần đây.", "direction": 0}


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
        return {"phase": "Thoát tích lũy, breakout hướng lên", "direction": 1, "contracting": True}
    if contracting and last_close < range_low:
        return {"phase": "Thoát phân phối, breakout hướng xuống", "direction": -1, "contracting": True}
    if contracting:
        return {"phase": "Đang trong giai đoạn biên độ co hẹp (tích lũy/phân phối), chưa breakout", "direction": 0, "contracting": True}
    return {"phase": "Biên độ chưa co hẹp rõ rệt, chưa thấy dấu hiệu tích lũy/phân phối", "direction": 0, "contracting": False}


# ---------------------------------------------------------------------------
# 7. Elliott-inspired swing heuristic (uu tien cho khung DAI)
# ---------------------------------------------------------------------------
def zigzag_pivots(candles, threshold_pct=0.015, window=200):
    data = candles[-window:] if len(candles) > window else candles
    pivots = []
    if len(data) < 5:
        return pivots
    last_pivot_price = data[0]["close"]
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
        return {"structure": "Chưa đủ điểm xoay chiều (pivot) để xác định cấu trúc sóng", "direction": 0}
    last4 = pivots[-4:]
    highs = [p for t, p in last4 if t == "H"]
    lows = [p for t, p in last4 if t == "L"]
    higher_highs = len(highs) >= 2 and highs == sorted(highs)
    higher_lows = len(lows) >= 2 and lows == sorted(lows)
    lower_highs = len(highs) >= 2 and highs == sorted(highs, reverse=True)
    lower_lows = len(lows) >= 2 and lows == sorted(lows, reverse=True)
    if higher_highs and higher_lows:
        return {"structure": "Cấu trúc đỉnh cao hơn - đáy cao hơn (giống sóng đẩy tăng)", "direction": 1}
    if lower_highs and lower_lows:
        return {"structure": "Cấu trúc đỉnh thấp hơn - đáy thấp hơn (giống sóng đẩy giảm)", "direction": -1}
    return {"structure": "Cấu trúc sóng đan xen, chưa rõ hướng (có thể đang trong sóng điều chỉnh)", "direction": 0}


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
            return {"direction": -1, "change_pct": round(change_pct, 2)}
        if change_pct < -0.15:
            return {"direction": 1, "change_pct": round(change_pct, 2)}
        return {"direction": 0, "change_pct": round(change_pct, 2)}
    except Exception:
        return None


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
# 10. Tong hop hop luu - bo yeu to thich ung theo khung thoi gian
# ---------------------------------------------------------------------------
def build_signal(timeframe, candles, higher_tf_info):
    closes = [c["close"] for c in candles]
    if len(closes) < 90:
        return {"score": 0, "verdict": "CHƯA ĐỦ DỮ LIỆU", "details": {}, "values": {}, "reasons": [],
                "summary": f"Chỉ nhận được {len(closes)} nến, cần tối thiểu ~90 nến để tính đủ các yếu tố phân tích.",
                "detail_labels": DETAIL_LABELS_VI}

    is_short = timeframe in SHORT_TFS
    last_close = closes[-1]
    r = rsi(closes, 14)
    macd_v, macd_s = macd(closes, 12, 26, 9)
    ma20, ma50 = sma(closes, 20), sma(closes, 50)
    bb_l, bb_m, bb_u = bollinger(closes, 20, 2)
    pattern_name, pattern_dir = detect_candle_pattern(candles)
    fib = fibonacci_position(candles, 50)

    details, reasons = {}, []

    if r < 30:
        details["RSI"] = 1; reasons.append(f"RSI đang ở {r}, vùng quá bán -> áp lực bán có thể đã suy yếu.")
    elif r > 70:
        details["RSI"] = -1; reasons.append(f"RSI đang ở {r}, vùng quá mua -> rủi ro điều chỉnh giảm.")
    else:
        details["RSI"] = 0; reasons.append(f"RSI đang ở {r}, vùng trung tính.")

    if macd_s is not None and macd_v > macd_s:
        details["MACD"] = 1; reasons.append(f"MACD ({macd_v}) trên đường tín hiệu ({macd_s}) -> động lượng nghiêng tăng.")
    else:
        details["MACD"] = -1; reasons.append(f"MACD ({macd_v}) dưới đường tín hiệu ({macd_s}) -> động lượng nghiêng giảm.")

    if ma20 > ma50:
        details["MA_Trend"] = 1; reasons.append(f"MA20 ({round(ma20,2)}) trên MA50 ({round(ma50,2)}) -> xu hướng tăng.")
    else:
        details["MA_Trend"] = -1; reasons.append(f"MA20 ({round(ma20,2)}) dưới MA50 ({round(ma50,2)}) -> xu hướng giảm.")

    if last_close <= bb_l:
        details["Bollinger"] = 1; reasons.append(f"Giá chạm/xuyên dải dưới Bollinger ({bb_l}) -> khả năng bật lại.")
    elif last_close >= bb_u:
        details["Bollinger"] = -1; reasons.append(f"Giá chạm/xuyên dải trên Bollinger ({bb_u}) -> áp lực chốt lời.")
    else:
        details["Bollinger"] = 0; reasons.append(f"Giá dao động trong dải Bollinger ({bb_l} - {bb_u}).")

    details["Candle_Pattern"] = pattern_dir
    reasons.append(f"Mô hình nến (price action): {pattern_name}"
                    + (" -> tín hiệu tăng giá." if pattern_dir == 1 else
                       " -> tín hiệu giảm giá." if pattern_dir == -1 else " -> chưa rõ hướng."))

    if fib:
        near = fib["proximity"] < 0.05
        trend_up = ma20 > ma50
        if near and trend_up:
            details["Fibonacci"] = 1
            reasons.append(f"Giá gần mức Fibonacci {fib['nearest_level']} ({fib['nearest_value']}) trong xu hướng tăng -> vùng hỗ trợ tiềm năng.")
        elif near and not trend_up:
            details["Fibonacci"] = -1
            reasons.append(f"Giá gần mức Fibonacci {fib['nearest_level']} ({fib['nearest_value']}) trong xu hướng giảm -> vùng kháng cự tiềm năng.")
        else:
            details["Fibonacci"] = 0
            reasons.append(f"Giá chưa tiếp cận mức Fibonacci đáng chú ý (gần nhất: {fib['nearest_level']}).")
    else:
        details["Fibonacci"] = 0
        reasons.append("Chưa đủ dữ liệu sóng giá để xác định Fibonacci.")

    if higher_tf_info:
        details["Da_khung"] = higher_tf_info["direction"]
        huong = "tăng" if higher_tf_info["direction"] > 0 else "giảm"
        reasons.append(f"Khung lớn hơn ({higher_tf_info['timeframe']}) đang xu hướng {huong} -> dùng đối chiếu, tránh ngược xu hướng lớn.")
    else:
        details["Da_khung"] = 0
        reasons.append("Đây là khung lớn nhất được hỗ trợ (W1), không có khung cao hơn để đối chiếu.")

    pivots = zigzag_pivots(candles)
    structure = market_structure_bos_choch(candles, pivots)
    details["SMC_Structure"] = structure["direction"]
    reasons.append(f"[SMC - Cấu trúc thị trường] {structure['label']}")

    fvg = fair_value_gap(candles)
    details["SMC_FVG"] = fvg["direction"]
    reasons.append(f"[SMC - Fair Value Gap] {fvg['label']}")

    sweep = liquidity_sweep(candles)
    details["SMC_Liquidity"] = sweep["direction"]
    reasons.append(f"[SMC - Thanh khoản] {sweep['label']}")

    ichimoku_data, wyckoff_data, elliott_data, dxy_data = None, None, None, None

    if is_short:
        ichimoku_data = ichimoku(candles)
        if ichimoku_data:
            if ichimoku_data["price_above_cloud"] and ichimoku_data["tenkan_above_kijun"]:
                details["Ichimoku"] = 1
                reasons.append("Giá đang ở TRÊN mây Kumo (Ichimoku) và Tenkan cắt lên trên Kijun -> tín hiệu tăng mạnh theo Ichimoku.")
            elif ichimoku_data["price_below_cloud"] and not ichimoku_data["tenkan_above_kijun"]:
                details["Ichimoku"] = -1
                reasons.append("Giá đang ở DƯỚI mây Kumo (Ichimoku) và Tenkan cắt xuống dưới Kijun -> tín hiệu giảm mạnh theo Ichimoku.")
            else:
                details["Ichimoku"] = 0
                reasons.append("Giá đang ở trong hoặc gần mây Kumo (Ichimoku) -> xu hướng chưa rõ ràng, nên thận trọng.")
        else:
            details["Ichimoku"] = 0
            reasons.append("Chưa đủ dữ liệu lịch sử để tính mây Kumo (Ichimoku).")
    else:
        wyckoff_data = wyckoff_heuristic(candles)
        if wyckoff_data:
            details["Wyckoff"] = wyckoff_data["direction"]
            reasons.append(f"[Wyckoff-inspired, đơn giản hóa vì không có dữ liệu khối lượng] {wyckoff_data['phase']}.")
        else:
            details["Wyckoff"] = 0

        elliott_data = elliott_heuristic(candles)
        details["Elliott"] = elliott_data["direction"]
        reasons.append(f"[Elliott-inspired, tham khảo] {elliott_data['structure']}.")

        dxy_data = dxy_trend(timeframe)
        if dxy_data:
            details["DXY"] = dxy_data["direction"]
            if dxy_data["direction"] == 1:
                reasons.append(f"Chỉ số DXY (đồng USD) giảm {abs(dxy_data['change_pct'])}% gần đây -> USD yếu thường hỗ trợ giá vàng tăng.")
            elif dxy_data["direction"] == -1:
                reasons.append(f"Chỉ số DXY (đồng USD) tăng {dxy_data['change_pct']}% gần đây -> USD mạnh thường gây áp lực lên giá vàng.")
            else:
                reasons.append("Chỉ số DXY (đồng USD) đi ngang, chưa tạo áp lực rõ rệt lên giá vàng.")
        else:
            details["DXY"] = 0
            reasons.append("Không lấy được dữ liệu DXY lúc này (có thể gói miễn phí không hỗ trợ chỉ số này) -> bỏ qua yếu tố liên thị trường.")

    raw = sum(details.values())
    max_score = len(details)
    score = int((raw / max_score) * 100) if max_score else 0
    buy_n = sum(1 for v in details.values() if v > 0)
    sell_n = sum(1 for v in details.values() if v < 0)

    if score >= 50:
        verdict = "MUA MẠNH" if score >= 75 else "MUA"
    elif score <= -50:
        verdict = "BÁN MẠNH" if score <= -75 else "BÁN"
    else:
        verdict = "TRUNG LẬP"

    mode_label = ("Chế độ khung NGẮN HẠN: ưu tiên Price Action + Ichimoku + Fibonacci"
                  if is_short else
                  "Chế độ khung DÀI HẠN: kết hợp thêm Wyckoff/Elliott (heuristic) + liên thị trường DXY")
    summary = (f"{mode_label}. {buy_n}/{max_score} yếu tố nghiêng mua, {sell_n}/{max_score} nghiêng bán. "
               f"Điểm hợp lưu: {score}/100 -> khuyến nghị: {verdict}.")

    values = {"rsi": r, "macd": macd_v, "macd_signal": macd_s, "ma20": round(ma20, 2), "ma50": round(ma50, 2),
              "bb_lower": bb_l, "bb_mid": bb_m, "bb_upper": bb_u, "pattern_name": pattern_name,
              "higher_tf_label": higher_tf_info["timeframe"] if higher_tf_info else "Không có",
              "analysis_mode": "NGAN_HAN" if is_short else "DAI_HAN"}
    if ichimoku_data: values["ichimoku"] = ichimoku_data
    if wyckoff_data: values["wyckoff_phase"] = wyckoff_data["phase"]
    if elliott_data: values["elliott_structure"] = elliott_data["structure"]
    if dxy_data: values["dxy_change_pct"] = dxy_data["change_pct"]

    return {"score": score, "verdict": verdict, "details": details, "values": values,
            "reasons": reasons, "summary": summary, "detail_labels": DETAIL_LABELS_VI}


# ---------------------------------------------------------------------------
# 11. Vercel Python handler
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        timeframe = qs.get("timeframe", ["H1"])[0]

        if timeframe not in INTERVAL_MAP:
            self._send(400, {"error": f"Khung thời gian không hợp lệ. Chọn trong: {list(INTERVAL_MAP)}"})
            return
        if not TD_API_KEY:
            self._send(500, {"error": "Chưa cấu hình TWELVEDATA_API_KEY trên Vercel (xem file hướng dẫn)."})
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
                        "direction": "MUA" if side == "buy" else "BÁN",
                        "win_rate": stats["win_rate"],
                        "samples": stats["samples"],
                        "lookahead": bt["lookahead"],
                        "low_confidence": stats["samples"] < 15,
                    }

            pending_order = pending_order_suggestion(candles, signal["score"])
            price_map = build_price_map(candles, signal)

            self._send(200, {
                "timeframe": timeframe,
                "candles": candles[-150:],
                "moving_averages": ma_lines,
                "signal": signal,
                "backtest": backtest_info,
                "pending_order": pending_order,
                "price_map": price_map,
            })
        except Exception as e:
            self._send(502, {"error": f"Lỗi lấy dữ liệu từ TwelveData: {e}"})

    def _send(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
