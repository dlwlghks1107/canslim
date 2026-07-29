# -*- coding: utf-8 -*-
"""
윌리엄 오닐 CAN SLIM 스크리너 (미국 + 한국)
매일 실행 → 통과 종목을 텔레그램으로 발송 + 웹사이트용 JSON 생성

기준 (오닐의 원칙을 무료 데이터로 근사):
  C - 최근 분기 EPS 성장률 >= +25% (전년 동기 대비)
  A - 연간 EPS 성장률 >= +25% (최근 연도, 전년 대비)
  N - 현재가가 52주 신고가의 85% 이상 (신고가 근접)
  S - 최근 10일 평균 거래량 >= 50일 평균의 1.0배 이상
  L - 상대강도(RS): 최근 12개월 수익률이 유니버스 상위 20%
  I - 기관 보유 비율 >= 20% (미국만)
  M - 시장 방향: 대표지수가 50일 이평선 위 (아니면 경고)

* 미국 실적: yfinance / 한국 실적: 네이버 증권 '기업실적분석' 표
"""

import os
import time
import json
import traceback
from datetime import datetime, timedelta

import pandas as pd
import requests

# ============================================================
# 설정
# ============================================================
CONFIG = {
    "eps_q_growth_min": 25.0,      # C: 분기 EPS 성장률 (%)
    "eps_a_growth_min": 25.0,      # A: 연간 EPS 성장률 (%)
    "near_high_pct": 0.85,         # N: 52주 고가 대비 최소 비율
    "volume_ratio_min": 1.0,       # S: 10일/50일 거래량 비율
    "rs_percentile_min": 80,       # L: 상대강도 백분위 (0~100)
    "inst_holding_min": 0.20,      # I: 기관 보유율 (미국)
    "min_price_usd": 10,           # 저가주 제외
    "min_price_krw": 5000,
    "max_results_per_market": 15,  # 텔레그램 발송 상위 N개
    "us_universe_limit": 500,
    "kr_universe_limit": 400,      # 시가총액 상위 N개
    "kr_naver_max": 80,            # 네이버 실적 조회 최대 종목 수
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ============================================================
# 미국 시장
# ============================================================
def get_us_universe():
    """S&P 500 구성종목 (위키피디아)"""
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        tickers = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        return tickers[:CONFIG["us_universe_limit"]]
    except Exception as e:
        print(f"[US] 유니버스 로드 실패: {e}")
        return []


def screen_us():
    import yfinance as yf

    tickers = get_us_universe()
    if not tickers:
        return [], None

    # 시장 방향 (M): S&P 500 vs 50일 이평선
    spx = yf.Ticker("^GSPC").history(period="6mo")
    market_ok = None
    if len(spx) > 50:
        market_ok = bool(
            spx["Close"].iloc[-1] > spx["Close"].rolling(50).mean().iloc[-1])

    print(f"[US] {len(tickers)}개 종목 가격 다운로드...")
    px = yf.download(tickers, period="1y", auto_adjust=True,
                     progress=False, threads=True)["Close"]

    returns_12m = (px.iloc[-1] / px.iloc[0] - 1).dropna()
    rs_rank = returns_12m.rank(pct=True) * 100

    candidates = rs_rank[rs_rank >= CONFIG["rs_percentile_min"]].index.tolist()
    print(f"[US] RS 상위 통과: {len(candidates)}개 → 펀더멘털 검사")

    results = []
    for t in candidates:
        try:
            tk = yf.Ticker(t)
            info = tk.info
            price = info.get("currentPrice") or px[t].iloc[-1]
            if not price or price < CONFIG["min_price_usd"]:
                continue

            # N: 52주 고가 근접
            high52 = info.get("fiftyTwoWeekHigh")
            if not high52 or price < high52 * CONFIG["near_high_pct"]:
                continue

            # C: 분기 EPS 성장 (전년 동기 대비)
            qe = tk.quarterly_income_stmt
            c_growth = _eps_growth(qe, yoy_offset=4)
            if c_growth is None or c_growth < CONFIG["eps_q_growth_min"]:
                continue

            # A: 연간 EPS 성장
            ae = tk.income_stmt
            a_growth = _eps_growth(ae, yoy_offset=1)
            if a_growth is None or a_growth < CONFIG["eps_a_growth_min"]:
                continue

            # S: 거래량 비율
            hist = tk.history(period="3mo")
            vol_ratio = None
            if len(hist) > 50:
                vol_ratio = (hist["Volume"].tail(10).mean()
                             / hist["Volume"].tail(50).mean())
                if vol_ratio < CONFIG["volume_ratio_min"]:
                    continue

            # I: 기관 보유
            inst = info.get("heldPercentInstitutions")
            if inst is not None and inst < CONFIG["inst_holding_min"]:
                continue

            results.append({
                "market": "US",
                "ticker": t,
                "name": info.get("shortName", t),
                "price": round(float(price), 2),
                "rs": round(float(rs_rank[t]), 1),
                "c_growth": round(c_growth, 1),
                "a_growth": round(a_growth, 1),
                "pct_of_high": round(price / high52 * 100, 1),
                "vol_ratio": round(float(vol_ratio), 2) if vol_ratio else None,
                "inst": round(inst * 100, 1) if inst else None,
            })
            time.sleep(0.3)  # rate limit 배려
        except Exception:
            continue

    results.sort(key=lambda x: x["rs"], reverse=True)
    return results, market_ok


def _eps_growth(stmt, yoy_offset):
    """손익계산서에서 Diluted EPS 또는 Net Income 성장률(%) 계산"""
    try:
        for row in ["Diluted EPS", "Basic EPS", "Net Income"]:
            if row in stmt.index:
                s = stmt.loc[row].dropna()
                if len(s) > yoy_offset:
                    cur, prev = float(s.iloc[0]), float(s.iloc[yoy_offset])
                    if prev <= 0:
                        return 100.0 if cur > 0 else None  # 흑자전환
                    return (cur / prev - 1) * 100
        return None
    except Exception:
        return None


# ============================================================
# 한국 시장 - 네이버 증권 분기/연간 실적
# ============================================================
NAVER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Referer": "https://finance.naver.com/",
}


def naver_eps_growth(ticker):
    """네이버 증권 '기업실적분석' 표에서
    (분기 EPS YoY 성장률, 연간 EPS 성장률)을 % 로 반환. 실패 시 (None, None)"""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        res = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        res.encoding = "euc-kr"
        return _parse_naver_eps(res.text)
    except Exception:
        return None, None


def _parse_naver_eps(html):
    from io import StringIO
    tables = pd.read_html(StringIO(html))

    target = None
    for t in tables:
        try:
            if t.iloc[:, 0].astype(str).str.contains("EPS").any():
                target = t
                break
        except Exception:
            continue
    if target is None:
        return None, None

    mask = target.iloc[:, 0].astype(str).str.contains(r"EPS")
    row = target[mask].iloc[0]

    annual, quarterly = [], []
    for col in target.columns[1:]:
        name = " ".join(map(str, col)) if isinstance(col, tuple) else str(col)
        if "(E)" in name:          # 컨센서스 추정치 제외
            continue
        val = pd.to_numeric(row[col], errors="coerce")
        if pd.isna(val):
            continue
        if "연간" in name:
            annual.append(float(val))
        elif "분기" in name:
            quarterly.append(float(val))

    def growth(cur, prev):
        if prev <= 0:
            return 100.0 if cur > 0 else None   # 흑자전환은 통과 취급
        return (cur / prev - 1) * 100

    # C: 최근 분기 vs 4분기 전(전년 동기) / A: 최근 연도 vs 전년
    c = growth(quarterly[-1], quarterly[-5]) if len(quarterly) >= 5 else None
    a = growth(annual[-1], annual[-2]) if len(annual) >= 2 else None
    return c, a


def screen_kr():
    from pykrx import stock

    today = datetime.now()
    date_str = _latest_kr_bizday(stock, today)

    # 시장 방향 (M): 코스피 vs 50일 이평선
    market_ok = None
    try:
        start = (today - timedelta(days=200)).strftime("%Y%m%d")
        kospi = stock.get_index_ohlcv(start, date_str, "1001")["종가"]
        if len(kospi) > 50:
            market_ok = bool(
                kospi.iloc[-1] > kospi.rolling(50).mean().iloc[-1])
    except Exception:
        pass

    # 시총 상위 유니버스 (코스피+코스닥)
    caps = []
    for mkt in ["KOSPI", "KOSDAQ"]:
        df = stock.get_market_cap(date_str, market=mkt)
        df["mkt"] = mkt
        caps.append(df)
    cap = pd.concat(caps).sort_values("시가총액", ascending=False)
    cap = cap.head(CONFIG["kr_universe_limit"])
    tickers = cap.index.tolist()

    # ── 1단계: 기술적 필터 (가격/RS/신고가/거래량) ──
    start_1y = (today - timedelta(days=370)).strftime("%Y%m%d")
    print(f"[KR] {len(tickers)}개 종목 기술적 검사...")

    rows = []
    for t in tickers:
        try:
            ohlcv = stock.get_market_ohlcv(start_1y, date_str, t)
            if len(ohlcv) < 200:
                continue
            close = ohlcv["종가"]
            price = int(close.iloc[-1])
            if price < CONFIG["min_price_krw"]:
                continue

            ret_12m = close.iloc[-1] / close.iloc[0] - 1
            high52 = close.max()
            vol = ohlcv["거래량"]
            vol_ratio = vol.tail(10).mean() / max(vol.tail(50).mean(), 1)

            rows.append({
                "ticker": t, "price": price, "ret_12m": ret_12m,
                "high52": high52, "vol_ratio": vol_ratio,
            })
            time.sleep(0.15)
        except Exception:
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        return [], market_ok
    df["rs"] = df["ret_12m"].rank(pct=True) * 100

    tech_pass = df[
        (df["rs"] >= CONFIG["rs_percentile_min"])                    # L
        & (df["price"] >= df["high52"] * CONFIG["near_high_pct"])    # N
        & (df["vol_ratio"] >= CONFIG["volume_ratio_min"])            # S
    ].sort_values("rs", ascending=False)
    tech_pass = tech_pass.head(CONFIG["kr_naver_max"])
    print(f"[KR] 기술적 통과: {len(tech_pass)}개 → 네이버 실적 검증")

    # ── 2단계: 네이버 증권 분기/연간 EPS 검증 (C, A) ──
    results = []
    for _, r in tech_pass.iterrows():
        t = r["ticker"]
        c_growth, a_growth = naver_eps_growth(t)
        time.sleep(0.5)  # 네이버 서버 배려

        # C: 분기 EPS YoY
        if c_growth is None or c_growth < CONFIG["eps_q_growth_min"]:
            continue
        # A: 연간 EPS 성장 (데이터 없으면 C로 대체 판단)
        if a_growth is not None and a_growth < CONFIG["eps_a_growth_min"]:
            continue

        results.append({
            "market": "KR",
            "ticker": t,
            "name": stock.get_market_ticker_name(t),
            "price": int(r["price"]),
            "rs": round(float(r["rs"]), 1),
            "c_growth": round(c_growth, 1),
            "a_growth": round(a_growth, 1) if a_growth is not None else None,
            "pct_of_high": round(r["price"] / r["high52"] * 100, 1),
            "vol_ratio": round(float(r["vol_ratio"]), 2),
            "inst": None,
        })

    results.sort(key=lambda x: x["rs"], reverse=True)
    return results, market_ok


def _latest_kr_bizday(stock, dt):
    """해당 날짜 기준 가장 최근 영업일 찾기"""
    for i in range(10):
        d = (dt - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv(d, d, "005930")
            if len(df) > 0:
                return d
        except Exception:
            continue
    return dt.strftime("%Y%m%d")


# ============================================================
# 텔레그램
# ============================================================
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("텔레그램 토큰/챗ID 미설정 → 발송 생략")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
    for c in chunks:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": c,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=30)
        if r.status_code != 200:
            print(f"텔레그램 발송 실패: {r.text}")


def format_message(us, kr, us_market_ok, kr_market_ok):
    today = datetime.now().strftime("%Y-%m-%d")
    n = CONFIG["max_results_per_market"]
    lines = [f"📊 <b>CAN SLIM 스크리너</b> — {today}\n"]

    def market_flag(ok, name):
        if ok is None:
            return f"⚪ {name}: 판단 불가"
        return (f"🟢 {name}: 상승 추세 (50일선 위)" if ok
                else f"🔴 {name}: 하락 추세 — 오닐 원칙상 신규 매수 자제!")

    lines.append(market_flag(us_market_ok, "미국(S&P500)"))
    lines.append(market_flag(kr_market_ok, "한국(KOSPI)"))
    lines.append("")

    for label, data in [("🇺🇸 미국", us), ("🇰🇷 한국", kr)]:
        lines.append(f"<b>{label} — {len(data)}개 통과</b>")
        if not data:
            lines.append("  (조건 통과 종목 없음)")
        for s in data[:n]:
            price = (f"${s['price']:,}" if s["market"] == "US"
                     else f"{s['price']:,}원")
            lines.append(
                f"• <b>{s['name']}</b> ({s['ticker']}) {price}\n"
                f"   RS {s['rs']} | EPS성장 {s['c_growth']}% | "
                f"고가대비 {s['pct_of_high']}%"
            )
        lines.append("")

    lines.append("⚠️ 투자 판단 참고용이며 매수 추천이 아닙니다.")
    return "\n".join(lines)


# ============================================================
# 메인
# ============================================================
def main():
    us, kr = [], []
    us_ok = kr_ok = None

    try:
        us, us_ok = screen_us()
        print(f"[US] 최종 통과: {len(us)}개")
    except Exception:
        traceback.print_exc()

    try:
        kr, kr_ok = screen_kr()
        print(f"[KR] 최종 통과: {len(kr)}개")
    except Exception:
        traceback.print_exc()

    # 텔레그램 발송
    send_telegram(format_message(us, kr, us_ok, kr_ok))

    # 웹사이트용 데이터 저장 (docs/index.html 앱이 이 JSON을 읽음)
    os.makedirs("docs/history", exist_ok=True)
    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().isoformat(),
        "market": {"us_uptrend": us_ok, "kr_uptrend": kr_ok},
        "us": us, "kr": kr,
    }
    with open("docs/results.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # 날짜별 히스토리 + 목록(manifest)
    day_path = f"docs/history/{payload['date']}.json"
    with open(day_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    manifest_path = "docs/history/manifest.json"
    dates = []
    if os.path.exists(manifest_path):
        try:
            dates = json.load(open(manifest_path, encoding="utf-8"))
        except Exception:
            dates = []
    if payload["date"] not in dates:
        dates.append(payload["date"])
    dates = sorted(dates, reverse=True)[:90]  # 최근 90일 보관
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(dates, f)
    print(f"완료: results.json + history/{payload['date']}.json")


if __name__ == "__main__":
    main()
