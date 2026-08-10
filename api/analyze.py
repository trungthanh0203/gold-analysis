"""
api/analyze.py
----------------
Ham serverless (Vercel Python Runtime). Moi lan giao dien goi:
    GET /api/analyze?timeframe=H1
ham nay se:
  1) Goi TwelveData /time_series de lay MOT LAN nguyen chuoi nen lich su that
     (toi da 200-300 nen) cho dung khung thoi gian duoc chon.
  2) Tinh chi bao ky thuat (RSI, MACD, MA, Bollinger) bang thuat toan Python
     thuan (KHONG dung pandas/numpy) -> khong ton thoi gian cai dat thu vien
     nang, ham khoi dong (cold start) nhanh nhat co the.
  3) Cham diem hop luu tin hieu + sinh giai thich bang tieng Viet.
  4) Tra JSON cho frontend ve.

Khong luu tru gi ca -> khong can database, khong rui ro "over bo nho":
moi lan phan tich la 1 lan goi TwelveData lay du lieu THAT moi nhat, nhanh
va chinh xac hon nhieu so voi tu gom tick.

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
    "M5": 200, "M15": 200, "H1": 200, "H4": 200, "D1": 250, "W1": 200,
}


# ---------------------------------------------------------------------------
# 1. Lay du lieu nen that tu TwelveData (1 lan goi = nguyen chuoi lich su)
# ---------------------------------------------------------------------------
def fetch_series(timeframe: str):
    interval = INTERVAL_MAP.get(timeframe, "1h")
    size = OUTPUT_SIZE.get(timeframe, 200)
    params = {
        "symbol": "XAU/USD",
        "interval": interval,
        "outputsize": str(size),
        "order": "ASC",  # cu -> moi, thuan tien tinh chi bao
        "apikey": TD_API_KEY,
    }
    url = TD_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "gold-analysis-page/1.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode())

    if "values" not in data:
        msg = data.get("message") or data.get("code") or "Loi khong xac dinh tu TwelveData"
        raise RuntimeError(str(msg))

    candles = []
    for v in data["values"]:
        candles.append({
            "time": v["datetime"],
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
        })
    return candles


# ---------------------------------------------------------------------------
# 2. Chi bao ky thuat - thuan Python, khong can pandas/numpy
# ---------------------------------------------------------------------------
def sma(vals, period):
    return mean(vals[-period:]) if len(vals) >= period else None


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
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain, avg_loss = mean(gains[:period]), mean(losses[:period])
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


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
# 3. Cham diem hop luu + giai thich (muc "phan tich & danh gia")
# ---------------------------------------------------------------------------
def build_signal(candles):
    closes = [c["close"] for c in candles]
    if len(closes) < 55:
        return {
            "score": 0, "verdict": "CHUA DU DU LIEU",
            "details": {}, "values": {}, "reasons": [],
            "summary": f"Chi nhan duoc {len(closes)} nen, can toi thieu ~55 nen de tinh du chi bao.",
        }

    last_close = closes[-1]
    r = rsi(closes, 14)
    macd_v, macd_s = macd(closes, 12, 26, 9)
    ma20, ma50 = sma(closes, 20), sma(closes, 50)
    bb_l, bb_m, bb_u = bollinger(closes, 20, 2)

    details, reasons = {}, []

    if r < 30:
        details["RSI"] = 1
        reasons.append(f"RSI dang o {r}, thuoc vung qua ban (<30) -> ap luc ban co the da suy yeu, kha nang hoi phuc tang gia.")
    elif r > 70:
        details["RSI"] = -1
        reasons.append(f"RSI dang o {r}, thuoc vung qua mua (>70) -> gia co the da tang nong, rui ro dieu chinh giam tang len.")
    else:
        details["RSI"] = 0
        reasons.append(f"RSI dang o {r}, nam trong vung trung tinh (30-70) -> chua co tin hieu qua mua/qua ban ro rang.")

    if macd_s is not None and macd_v > macd_s:
        details["MACD"] = 1
        reasons.append(f"Duong MACD ({macd_v}) dang nam tren duong tin hieu ({macd_s}) -> dong luong ngan han nghieng ve phia tang.")
    else:
        details["MACD"] = -1
        reasons.append(f"Duong MACD ({macd_v}) dang nam duoi duong tin hieu ({macd_s}) -> dong luong ngan han nghieng ve phia giam.")

    if ma20 > ma50:
        details["MA_Trend"] = 1
        reasons.append(f"MA20 ({round(ma20,2)}) dang cao hon MA50 ({round(ma50,2)}) - cau truc golden cross -> xu huong trung han la tang.")
    else:
        details["MA_Trend"] = -1
        reasons.append(f"MA20 ({round(ma20,2)}) dang thap hon MA50 ({round(ma50,2)}) - cau truc death cross -> xu huong trung han nghieng ve giam.")

    if last_close <= bb_l:
        details["Bollinger"] = 1
        reasons.append(f"Gia ({last_close}) dang cham hoac xuyen dai duoi Bollinger ({bb_l}) -> bien dong ve vung thap, kha nang bat lai ky thuat.")
    elif last_close >= bb_u:
        details["Bollinger"] = -1
        reasons.append(f"Gia ({last_close}) dang cham hoac xuyen dai tren Bollinger ({bb_u}) -> bien dong len vung cao, ap luc chot loi co the xuat hien.")
    else:
        details["Bollinger"] = 0
        reasons.append(f"Gia ({last_close}) dang dao dong trong dai Bollinger ({bb_l} - {bb_u}) -> chua co tin hieu cuc bien ro ret.")

    raw = sum(details.values())
    max_score = len(details)
    score = int((raw / max_score) * 100)
    buy_n = sum(1 for v in details.values() if v > 0)
    sell_n = sum(1 for v in details.values() if v < 0)

    if score >= 50:
        verdict = "MUA MANH" if score >= 75 else "MUA"
    elif score <= -50:
        verdict = "BAN MANH" if score <= -75 else "BAN"
    else:
        verdict = "TRUNG LAP"

    summary = (f"{buy_n}/{max_score} chi bao nghieng ve phia mua, {sell_n}/{max_score} nghieng ve phia ban tren "
               f"{len(candles)} nen du lieu that gan nhat. Diem hop luu: {score}/100 -> khuyen nghi: {verdict}.")

    return {
        "score": score, "verdict": verdict, "details": details,
        "values": {"rsi": r, "macd": macd_v, "macd_signal": macd_s,
                    "ma20": round(ma20, 2), "ma50": round(ma50, 2),
                    "bb_lower": bb_l, "bb_mid": bb_m, "bb_upper": bb_u},
        "reasons": reasons, "summary": summary,
    }


# ---------------------------------------------------------------------------
# 4. Vercel Python handler
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
            candles = fetch_series(timeframe)
            signal = build_signal(candles)
            self._send(200, {
                "timeframe": timeframe,
                "candles": candles[-150:],  # gioi han payload cho nhe, du de ve chart
                "signal": signal,
            })
        except Exception as e:
            self._send(502, {"error": f"Loi lay du lieu tu TwelveData: {e}"})

    def _send(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

