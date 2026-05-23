"""
EPS Beat + 200일선 라지캡 스크리너
- Russell 1000 / S&P 500 유니버스
- 최근 N분기 연속 EPS 비트
- 200일선 위(강세) 또는 아래(저평가) 선택 가능
"""
from __future__ import annotations

import contextlib
import io
import logging
import warnings

import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import re
import time
from datetime import datetime, date
from typing import Callable, Optional

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

YAHOO_BATCH_CHUNK = 30
YAHOO_BATCH_PAUSE = 3.0

st.set_page_config(
    page_title="EPS Beat 스크리너",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .main-title { font-size: 2rem; font-weight: 700; color: #1a73e8; }
    .sub-title  { font-size: 1rem; color: #666; margin-bottom: 1.5rem; }
    .beat-chip  { display: inline-block; background: #e6f4ea; color: #1e7e34;
                  border-radius: 12px; padding: 2px 10px; font-size: 0.78rem; font-weight: 600; }
    .miss-chip  { display: inline-block; background: #fce8e6; color: #c62828;
                  border-radius: 12px; padding: 2px 10px; font-size: 0.78rem; font-weight: 600; }
    .above-chip { display: inline-block; background: #e8f0fe; color: #1a73e8;
                  border-radius: 12px; padding: 2px 10px; font-size: 0.78rem; font-weight: 600; }
    .below-chip { display: inline-block; background: #fff3e0; color: #e65100;
                  border-radius: 12px; padding: 2px 10px; font-size: 0.78rem; font-weight: 600; }
    .stProgress > div > div { background: #1a73e8; }
    div[data-testid="stSidebar"] { background: #f7f9fc; }
</style>
""", unsafe_allow_html=True)

# ──────────────────────────────────────────
# 사이드바
# ──────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ 스크리닝 조건")
    n_quarters = st.slider("연속 EPS 비트 분기 수", 1, 5, 3)
    min_mcap_b = st.slider("최소 시가총액 (십억 달러)", 1, 50, 10)

    st.markdown("---")
    st.markdown("### 📊 200일선 기준")
    ma_mode = st.radio(
        "200일선 필터 모드",
        ["📈 200일선 위 (강세 종목)", "📉 200일선 아래 (저평가 탐색)"],
        help=(
            "위: 추세 추종 — 이미 강한 종목\n"
            "아래: 역발상 — 어닝은 좋은데 주가가 눌린 종목"
        ),
    )
    above_mode = ma_mode.startswith("📈")

    if above_mode:
        ma_ratio_label = "현재가 / 200일선 최소 비율"
        ma_ratio_help  = "1.05 → 200일선보다 5% 이상 위에 있어야 통과"
        ma_ratio_default = 1.00
    else:
        ma_ratio_label = "현재가 / 200일선 최대 비율"
        ma_ratio_help  = "0.95 → 200일선보다 5% 이상 아래에 있어야 통과"
        ma_ratio_default = 1.00

    ma_ratio_threshold = st.slider(
        ma_ratio_label, 0.70, 1.20,
        ma_ratio_default, 0.01,
        help=ma_ratio_help,
    )

    st.markdown("---")
    st.markdown("### 💰 Valuation 필터")
    use_fpe_filter = st.checkbox("Forward P/E 상한 적용", value=False)
    max_fwd_pe = st.slider(
        "Forward P/E 최대값", 10, 100, 40, 1,
        disabled=not use_fpe_filter,
        help="Forward P/E가 이 값보다 낮은 종목만 통과 (데이터 없는 종목은 통과 처리)",
    )

    # 저평가 모드 전용 추가 필터
    if not above_mode:
        st.markdown("---")
        st.markdown("### 🔍 저평가 추가 필터")
        use_max_drawdown = st.checkbox("52주 고점 대비 하락률 하한", value=False)
        min_drawdown_pct = st.slider(
            "최소 하락률 (%)", 5, 60, 20, 5,
            disabled=not use_max_drawdown,
            help="예: 20 → 52주 고점 대비 20% 이상 하락한 종목만",
        )
    else:
        use_max_drawdown = False
        min_drawdown_pct = 0

    request_delay = st.slider("종목당 API 딜레이 (초)", 0.5, 3.0, 1.2, 0.1)
    batch_chunk   = st.slider("시세 배치 크기", 10, 50, 30, 5)
    use_batch_prices = st.checkbox("배치 시세 다운로드", value=True)

    st.markdown("---")
    st.markdown("### 📋 종목 범위")
    universe_src = st.radio("스크리닝 대상", [
        "Russell 1000 (iShares IWB) — 시총 상위 ~1000종목",
        "S&P 500 (Wikipedia) — 500종목",
        "테스트 (상위 50종목)",
    ])
    st.caption("Russell 1000 전체 스캔 시 20~30분 소요됩니다.")

# ──────────────────────────────────────────
# 유니버스 로딩 (기존 동일)
# ──────────────────────────────────────────
ISHARES_IWB_PRODUCT = "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf"

def _browser_headers(referer=None, accept_csv=False):
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if accept_csv:
        h["Accept"]  = "text/csv,application/octet-stream,*/*"
        h["Referer"] = referer or ISHARES_IWB_PRODUCT
    else:
        h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    if referer:
        h["Referer"] = referer
    return h

def _normalize_symbol(ticker):
    sym = str(ticker).strip().upper().replace('"', "")
    sym = sym.replace("/", "-")
    if sym in ("BRKB", "BRK B"): return "BRK-B"
    if "." in sym: return sym.replace(".", "-")
    return sym

def _finalize_universe(df):
    out = df.copy()
    out["Symbol"]  = out["Symbol"].map(_normalize_symbol)
    out["Company"] = out["Company"].astype(str).str.strip()
    if "Sector" not in out.columns: out["Sector"] = "N/A"
    out["Sector"]  = out["Sector"].astype(str).str.strip()
    out = out[out["Symbol"].str.match(r"^[A-Z][A-Z0-9.-]{0,9}$", na=False)]
    out = out.drop_duplicates(subset=["Symbol"], keep="first")
    return out[["Symbol","Company","Sector"]].reset_index(drop=True)

def _parse_ishares_holdings_csv(text):
    text  = text.lstrip("\ufeff")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.search(r'^"?Ticker"?', l.strip(), re.I)), None)
    if start is None: raise ValueError("Ticker 헤더 없음")
    df_raw = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    df_raw.columns = [re.sub(r'^"|"$', "", str(c)).strip() for c in df_raw.columns]
    col_map = {c.lower(): c for c in df_raw.columns}
    ticker_col = col_map.get("ticker") or col_map.get("symbol")
    if not ticker_col: raise ValueError("Ticker/Symbol 컬럼 없음")
    name_col   = col_map.get("name") or col_map.get("security") or ticker_col
    sector_col = col_map.get("sector")
    asset_col  = col_map.get("asset class")
    df_raw = df_raw.rename(columns={ticker_col: "Symbol", name_col: "Company"})
    df_raw["Sector"] = df_raw[sector_col] if sector_col else "N/A"
    if asset_col:
        asset = df_raw[asset_col].fillna("").astype(str).str.strip().str.lower()
        df_raw = df_raw[asset.isin({"equity","common stock","stocks"}) | asset.eq("")].copy()
    df_raw["Symbol"] = df_raw["Symbol"].astype(str).str.strip()
    df_raw = df_raw[~df_raw["Symbol"].str.upper().isin({"TICKER","NAN",""})]
    return _finalize_universe(df_raw)

def _load_ishares_russell1000():
    session = requests.Session()
    session.headers.update(_browser_headers())
    page_resp = session.get(ISHARES_IWB_PRODUCT, timeout=30)
    page_resp.raise_for_status()
    page_html = page_resp.text.replace("&amp;","&")
    csv_urls = []
    for m in re.finditer(r"(https://www\.ishares\.com[^\"'\s>]+\.ajax\?[^\"'\s>]*fileType=csv[^\"'\s>]*)", page_html, re.I):
        csv_urls.append(m.group(1))
    csv_urls.extend([
        f"{ISHARES_IWB_PRODUCT}/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
        "https://www.ishares.com/us/products/239707/ISHARES-RUSSELL-1000-ETF/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
    ])
    seen = set()
    csv_headers = _browser_headers(referer=ISHARES_IWB_PRODUCT, accept_csv=True)
    for url in csv_urls:
        if url in seen: continue
        seen.add(url)
        resp = session.get(url, headers=csv_headers, timeout=30)
        if resp.status_code != 200 or "Ticker" not in resp.text: continue
        df = _parse_ishares_holdings_csv(resp.text)
        if len(df) > 100: return df
    raise ValueError("iShares IWB CSV 파싱 실패")

def _load_sp500_plus_sp400():
    h = _browser_headers()
    r500 = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=h, timeout=20)
    r500.raise_for_status()
    sp500 = pd.read_html(io.StringIO(r500.text))[0][["Symbol","Security","GICS Sector"]].rename(columns={"Security":"Company","GICS Sector":"Sector"})
    r400 = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", headers=h, timeout=20)
    r400.raise_for_status()
    sp400 = pd.read_html(io.StringIO(r400.text))[0]
    sym_col    = next((c for c in sp400.columns if str(c).strip().lower() in ("symbol","ticker symbol")), None)
    company_col= next((c for c in sp400.columns if str(c).strip().lower() in ("security","company")), sym_col)
    sector_col = next((c for c in sp400.columns if "gics sector" in str(c).strip().lower()), None)
    if sym_col is None: raise ValueError("S&P 400 Symbol 컬럼 없음")
    sp400 = sp400[[sym_col, company_col] + ([sector_col] if sector_col else [])].rename(
        columns={sym_col:"Symbol", company_col:"Company", **({sector_col:"Sector"} if sector_col else {})}
    )
    if "Sector" not in sp400.columns: sp400["Sector"] = "N/A"
    return _finalize_universe(pd.concat([sp500, sp400], ignore_index=True))

def _load_wikipedia_sp500():
    r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=_browser_headers(), timeout=20)
    r.raise_for_status()
    df = pd.read_html(io.StringIO(r.text))[0][["Symbol","Security","GICS Sector"]].rename(columns={"Security":"Company","GICS Sector":"Sector"})
    return _finalize_universe(df)

def _builtin_fallback_universe():
    fallback = [
        ("AAPL","Apple Inc.","Information Technology"),("MSFT","Microsoft Corp.","Information Technology"),
        ("NVDA","NVIDIA Corp.","Information Technology"),("AMZN","Amazon.com Inc.","Consumer Discretionary"),
        ("GOOGL","Alphabet Inc. Cl A","Communication Services"),("META","Meta Platforms Inc.","Communication Services"),
        ("BRK-B","Berkshire Hathaway Cl B","Financials"),("TSLA","Tesla Inc.","Consumer Discretionary"),
        ("LLY","Eli Lilly & Co.","Health Care"),("JPM","JPMorgan Chase & Co.","Financials"),
        ("V","Visa Inc.","Financials"),("UNH","UnitedHealth Group Inc.","Health Care"),
        ("XOM","Exxon Mobil Corp.","Energy"),("MA","Mastercard Inc.","Financials"),
        ("AVGO","Broadcom Inc.","Information Technology"),("HD","Home Depot Inc.","Consumer Discretionary"),
        ("PG","Procter & Gamble Co.","Consumer Staples"),("JNJ","Johnson & Johnson","Health Care"),
        ("MRK","Merck & Co. Inc.","Health Care"),("ABBV","AbbVie Inc.","Health Care"),
        ("CRM","Salesforce Inc.","Information Technology"),("COST","Costco Wholesale Corp.","Consumer Staples"),
        ("AMD","Advanced Micro Devices","Information Technology"),("NFLX","Netflix Inc.","Communication Services"),
        ("TMO","Thermo Fisher Scientific","Health Care"),("PEP","PepsiCo Inc.","Consumer Staples"),
        ("KO","Coca-Cola Co.","Consumer Staples"),("WMT","Walmart Inc.","Consumer Staples"),
        ("ORCL","Oracle Corp.","Information Technology"),("TXN","Texas Instruments Inc.","Information Technology"),
        ("BAC","Bank of America Corp.","Financials"),("QCOM","Qualcomm Inc.","Information Technology"),
        ("AMAT","Applied Materials Inc.","Information Technology"),("HON","Honeywell International","Industrials"),
        ("CAT","Caterpillar Inc.","Industrials"),("GE","GE Aerospace","Industrials"),
        ("AMGN","Amgen Inc.","Health Care"),("LOW","Lowe's Companies Inc.","Consumer Discretionary"),
        ("GS","Goldman Sachs Group","Financials"),("MS","Morgan Stanley","Financials"),
        ("CVX","Chevron Corp.","Energy"),("COP","ConocoPhillips","Energy"),
        ("DE","Deere & Company","Industrials"),("LMT","Lockheed Martin Corp.","Industrials"),
        ("NEE","NextEra Energy Inc.","Utilities"),("ADBE","Adobe Inc.","Information Technology"),
        ("NOW","ServiceNow Inc.","Information Technology"),("INTU","Intuit Inc.","Information Technology"),
        ("KLAC","KLA Corp.","Information Technology"),("LRCX","Lam Research Corp.","Information Technology"),
        ("MU","Micron Technology Inc.","Information Technology"),("MRVL","Marvell Technology Inc.","Information Technology"),
        ("ARM","Arm Holdings PLC","Information Technology"),("PANW","Palo Alto Networks","Information Technology"),
        ("CRWD","CrowdStrike Holdings","Information Technology"),("UBER","Uber Technologies Inc.","Industrials"),
        ("BKNG","Booking Holdings Inc.","Consumer Discretionary"),("ISRG","Intuitive Surgical Inc.","Health Care"),
        ("VRT","Vertiv Holdings","Industrials"),("GEV","GE Vernova","Industrials"),
    ]
    return pd.DataFrame(fallback, columns=["Symbol","Company","Sector"])

@st.cache_data(ttl=86400)
def _get_universe_cached(source):
    if "Russell" in source:
        loaders = [
            ("iShares IWB",           _load_ishares_russell1000),
            ("S&P 500 + S&P 400",     _load_sp500_plus_sp400),
        ]
        errors = []
        for label, loader in loaders:
            try:
                df = loader()
                if len(df) > 100:
                    return df, label, ("info" if label != "iShares IWB" else "")
                errors.append(f"{label}: 종목 수 {len(df)}")
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        try:
            df = _load_wikipedia_sp500()
            return df, f"S&P 500 대체 ({'·'.join(errors)})", "warning"
        except Exception as exc:
            errors.append(f"S&P 500: {exc}")
            return _builtin_fallback_universe(), f"내장 100종목 ({'·'.join(errors)})", "error"
    if "S&P" in source:
        try:
            return _load_wikipedia_sp500(), "Wikipedia S&P 500", ""
        except Exception as exc:
            return _builtin_fallback_universe(), f"내장 100종목 ({exc})", "warning"
    return _builtin_fallback_universe(), "내장 100종목", "warning"

def get_universe(source):
    df, label, level = _get_universe_cached(source)
    if level == "info":    st.info(f"Russell 1000: **{label}**에서 {len(df)}개 종목 로딩")
    elif level == "warning": st.warning(f"종목 목록: **{label}** ({len(df)}개)")
    elif level == "error":   st.error(f"Russell 1000 로딩 실패 — **{label}** ({len(df)}개)만 사용합니다.")
    return df

# ──────────────────────────────────────────
# Yahoo Finance 헬퍼
# ──────────────────────────────────────────
@st.cache_resource
def _yf_browser_session():
    try:
        from curl_cffi.requests import Session
        return Session(impersonate="chrome")
    except Exception:
        return None

def _make_ticker(symbol):
    session = _yf_browser_session()
    return yf.Ticker(symbol, session=session) if session else yf.Ticker(symbol)

def _quiet_call(fn):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return fn()

def _yf_retry(fn, retries=3, base_delay=2.0):
    last_exc = None
    for attempt in range(retries):
        try:
            return _quiet_call(fn)
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_exc or RuntimeError("yfinance retry failed")

def yahoo_health_check():
    try:
        kwargs = dict(period="5d", interval="1d", progress=False, threads=False)
        session = _yf_browser_session()
        if session: kwargs["session"] = session
        data = _yf_retry(lambda: yf.download("SPY", **kwargs), retries=2, base_delay=2.0)
        if data is None or (hasattr(data, "empty") and data.empty):
            return False, "SPY 시세 없음 — IP 차단·레이트리밋 가능"
        return True, "SPY 시세 수신 OK"
    except Exception as exc:
        return False, str(exc)

def _batch_download_closes(symbols, chunk_size=YAHOO_BATCH_CHUNK, pause=YAHOO_BATCH_PAUSE):
    session = _yf_browser_session()
    out = {}
    kwargs = dict(period="300d", interval="1d", group_by="ticker", threads=False, progress=False, auto_adjust=True)
    if session: kwargs["session"] = session
    for start in range(0, len(symbols), chunk_size):
        chunk   = symbols[start:start+chunk_size]
        tickers = " ".join(chunk)
        try:
            data = _yf_retry(lambda t=tickers: yf.download(t, **kwargs), retries=3, base_delay=pause)
        except Exception:
            time.sleep(pause * 2); continue
        if data is None or (hasattr(data, "empty") and data.empty):
            time.sleep(pause); continue
        if len(chunk) == 1:
            sym    = chunk[0]
            closes = data["Close"] if "Close" in data.columns else data.squeeze()
            if isinstance(closes, pd.DataFrame): closes = closes.iloc[:, 0]
            out[sym] = closes.dropna()
        elif isinstance(data.columns, pd.MultiIndex):
            level0 = data.columns.get_level_values(0)
            for sym in chunk:
                if sym in level0: out[sym] = data[sym]["Close"].dropna()
        time.sleep(pause)
    return out

def _ma200_from_closes(closes):
    if closes is None or len(closes) < 201: return None
    ma200 = closes.rolling(200).mean().iloc[-1]
    price = closes.iloc[-1]
    high52 = closes.rolling(252).max().iloc[-1]   # 52주 고점
    if pd.isna(ma200) or ma200 <= 0 or pd.isna(price): return None
    return {
        "price":      round(float(price), 2),
        "ma200":      round(float(ma200), 2),
        "ratio":      round(float(price / ma200), 4),
        "high52":     round(float(high52), 2) if not pd.isna(high52) else None,
        "drawdown":   round((price - high52) / high52 * 100, 1) if not pd.isna(high52) and high52 > 0 else None,
    }

def build_ma200_cache(symbols, chunk_size, use_batch):
    cache = {}
    if use_batch and symbols:
        progress  = st.progress(0, text="배치 시세 로딩 중...")
        n_chunks  = max(1, (len(symbols) + chunk_size - 1) // chunk_size)
        for idx, start in enumerate(range(0, len(symbols), chunk_size)):
            chunk = symbols[start:start+chunk_size]
            progress.progress((idx+1)/n_chunks, text=f"시세 배치 {idx+1}/{n_chunks} ({len(chunk)}종목)")
            closes_map = _batch_download_closes(chunk, chunk_size=len(chunk))
            for sym, closes in closes_map.items():
                info = _ma200_from_closes(closes)
                if info: cache[sym] = info
        progress.empty()
    return cache

def _get_market_cap(ticker_obj):
    try:
        fi = _quiet_call(lambda: ticker_obj.fast_info)
        for key in ("market_cap", "marketCap"):
            try: val = fi[key]
            except (TypeError, KeyError): val = getattr(fi, key, None)
            if val is not None and not pd.isna(val): return float(val)
    except Exception: pass
    try:
        mcap = ticker_obj.info.get("marketCap")
        return float(mcap) if mcap else None
    except Exception: return None

def _coerce_earnings_df(raw):
    if raw is None: return None
    if isinstance(raw, dict): raw = pd.DataFrame(raw)
    if not isinstance(raw, pd.DataFrame) or raw.empty: return None
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    rename = {}
    for col in df.columns:
        key = str(col).strip().lower().replace(" ","").replace("_","")
        if key in ("reportedeps","epsactual","actual"): rename[col] = "Reported EPS"
        elif key in ("epsestimate","estimate"):          rename[col] = "EPS Estimate"
    df = df.rename(columns=rename)
    if "Reported EPS" not in df.columns or "EPS Estimate" not in df.columns: return None
    if not isinstance(df.index, pd.DatetimeIndex):
        for date_col in ("Earnings Date","Date","Quarter","period"):
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                df = df.dropna(subset=[date_col]).set_index(date_col)
                break
        else:
            try: df.index = pd.to_datetime(df.index, errors="coerce")
            except: return None
    df["Reported EPS"] = pd.to_numeric(df["Reported EPS"], errors="coerce")
    df["EPS Estimate"] = pd.to_numeric(df["EPS Estimate"], errors="coerce")
    return df.sort_index(ascending=False)

def _fetch_earnings_dataframe(ticker_obj, symbol):
    loaders = [
        lambda: ticker_obj.get_earnings_dates(limit=24),
        lambda: ticker_obj.earnings_dates,
        lambda: ticker_obj.get_earnings_history(),
    ]
    for loader in loaders:
        try:
            raw = _yf_retry(loader, retries=2, base_delay=1.5)
            df  = _coerce_earnings_df(raw)
            if df is not None and not df.empty:
                valid = df.dropna(subset=["Reported EPS","EPS Estimate"])
                if len(valid) >= 1: return df
        except Exception: continue
    return pd.DataFrame()

def _quarterly_earnings_rows(past):
    past = past.sort_index(ascending=False).copy()
    if isinstance(past.index, pd.DatetimeIndex):
        past["_q"] = past.index.to_period("Q")
        past = past.drop_duplicates(subset=["_q"], keep="first").drop(columns=["_q"])
    return past

def get_eps_beat_info(ticker_obj, symbol, n_quarters):
    try:
        earnings = _fetch_earnings_dataframe(ticker_obj, symbol)
        if earnings.empty: return False, []
        past = earnings.dropna(subset=["Reported EPS","EPS Estimate"]).copy()
        past = _quarterly_earnings_rows(past)
        if len(past) < n_quarters: return False, []
        recent  = past.head(n_quarters)
        details = []
        for idx, row in recent.iterrows():
            est  = row["EPS Estimate"]
            rep  = row["Reported EPS"]
            beat = rep > est
            surp = ((rep - est) / abs(est) * 100) if est != 0 else 0.0
            details.append({
                "date":     str(idx.date()) if hasattr(idx,"date") else str(idx),
                "estimate": round(float(est), 4),
                "reported": round(float(rep), 4),
                "surprise": round(float(surp), 2),
                "beat":     bool(beat),
            })
        return all(d["beat"] for d in details), details
    except Exception: return False, []

def get_ma200_info(ticker_obj, ma_cache=None, symbol=""):
    if ma_cache and symbol and symbol in ma_cache: return ma_cache[symbol]
    try:
        hist = _yf_retry(lambda: ticker_obj.history(period="300d", interval="1d"), retries=2)
        if hist is None or len(hist) < 201: return None
        return _ma200_from_closes(hist["Close"])
    except Exception: return None

def get_fper_info(ticker_obj, price):
    try:
        info     = _quiet_call(lambda: ticker_obj.info)
        fwd_eps  = info.get("forwardEps")
        trail_eps= info.get("trailingEps")
        fwd_pe   = info.get("forwardPE")
        trail_pe = info.get("trailingPE")
        result   = {}
        if fwd_pe   and fwd_pe   > 0: result["fwd_pe"]   = round(float(fwd_pe),   1)
        elif fwd_eps and fwd_eps > 0: result["fwd_pe"]   = round(price/float(fwd_eps), 1)
        else:                          result["fwd_pe"]   = None
        if trail_pe   and trail_pe   > 0: result["trail_pe"] = round(float(trail_pe),   1)
        elif trail_eps and trail_eps > 0: result["trail_pe"] = round(price/float(trail_eps), 1)
        else:                              result["trail_pe"] = None
        result["fwd_eps"]   = round(float(fwd_eps),   2) if fwd_eps   else None
        result["trail_eps"] = round(float(trail_eps), 2) if trail_eps else None
        return result
    except Exception:
        return {"fwd_pe":None,"trail_pe":None,"fwd_eps":None,"trail_eps":None}

# ──────────────────────────────────────────
# 핵심 스크리닝 함수
# ──────────────────────────────────────────
def screen_ticker(
    row, n_quarters, min_mcap_b,
    above_mode, ma_ratio_threshold,
    use_fpe_filter=False, max_fwd_pe=40,
    use_max_drawdown=False, min_drawdown_pct=20,
    ma_cache=None,
):
    ticker_sym = row["Symbol"]
    try:
        t = _make_ticker(ticker_sym)

        ma_info = get_ma200_info(t, ma_cache=ma_cache, symbol=ticker_sym)
        if ma_info is None: return None

        ratio = ma_info["ratio"]
        # 200일선 위/아래 필터
        if above_mode:
            if ratio < ma_ratio_threshold: return None   # 위 모드: 비율 하한
        else:
            if ratio > ma_ratio_threshold: return None   # 아래 모드: 비율 상한
            # 52주 고점 대비 하락률 필터
            if use_max_drawdown:
                drawdown = ma_info.get("drawdown")
                if drawdown is None or drawdown > -min_drawdown_pct:
                    return None   # drawdown은 음수이므로 -pct보다 커야 통과

        mcap = _get_market_cap(t)
        if mcap is None or mcap < min_mcap_b * 1e9: return None

        eps_pass, eps_details = get_eps_beat_info(t, ticker_sym, n_quarters)
        if not eps_pass: return None

        fper = get_fper_info(t, ma_info["price"]) if use_fpe_filter else {"fwd_pe":None,"trail_pe":None,"fwd_eps":None,"trail_eps":None}
        if use_fpe_filter and fper.get("fwd_pe") is not None:
            if fper["fwd_pe"] > max_fwd_pe: return None

        return {
            "Symbol":      ticker_sym,
            "Company":     row["Company"],
            "Sector":      row["Sector"],
            "Price":       ma_info["price"],
            "MA200":       ma_info["ma200"],
            "Price/MA200": ratio,
            "High52":      ma_info.get("high52"),
            "Drawdown%":   ma_info.get("drawdown"),
            "MCap($B)":    round(mcap/1e9, 1),
            "Fwd PE":      fper.get("fwd_pe"),
            "Trail PE":    fper.get("trail_pe"),
            "Fwd EPS":     fper.get("fwd_eps"),
            "Trail EPS":   fper.get("trail_eps"),
            "EPS Details": eps_details,
        }
    except Exception: return None

# ──────────────────────────────────────────
# 사이드바 — Yahoo 헬스체크
# ──────────────────────────────────────────
with st.sidebar:
    st.markdown("---")
    st.markdown("### 🌐 Yahoo Finance")
    if st.button("Yahoo 연결 테스트 (SPY)", use_container_width=True):
        ok, msg = yahoo_health_check()
        (st.success if ok else st.error)(f"{'정상' if ok else '실패'} — {msg}")
    st.caption("Cloud 환경은 IP 차단이 잦습니다. 차단 시 30~60분 대기 후 재시도.")

# ──────────────────────────────────────────
# 메인 UI
# ──────────────────────────────────────────
mode_emoji = "📈" if above_mode else "📉"
mode_label = "200일선 위 강세" if above_mode else "200일선 아래 저평가"

st.markdown('<div class="main-title">📈 EPS Beat + 200일선 스크리너</div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="sub-title">'
    f'{mode_emoji} <b>{mode_label}</b> 모드 &nbsp;|&nbsp; '
    f'최근 {n_quarters}분기 연속 EPS 비트 &nbsp;|&nbsp; '
    f'현재가/MA200 {"≥" if above_mode else "≤"} {ma_ratio_threshold:.0%}'
    f'</div>',
    unsafe_allow_html=True,
)

col_run, col_reset, _ = st.columns([2, 1, 5])
with col_run:
    run_btn = st.button("🚀 스크리닝 시작", type="primary", use_container_width=True)
with col_reset:
    if st.button("🗑️ 초기화", use_container_width=True):
        for k in ("results","run_date","run_mode"):
            st.session_state.pop(k, None)
        _get_universe_cached.clear()
        st.rerun()

# ──────────────────────────────────────────
# 스크리닝 실행
# ──────────────────────────────────────────
if run_btn:
    src_key = ("Russell 1000 (iShares IWB)" if "Russell" in universe_src
               else ("S&P 500 (Wikipedia)" if "S&P" in universe_src
               else "Russell 1000 (iShares IWB)"))
    universe_df = get_universe(src_key)
    if "테스트" in universe_src: universe_df = universe_df.head(50)

    ok, health_msg = yahoo_health_check()
    if not ok:
        st.error(f"**Yahoo Finance 연결 불가**\n\n{health_msg}\n\n· 30~60분 후 재시도 · 딜레이 2초+ · 로컬 실행 권장")
        st.stop()

    symbols  = universe_df["Symbol"].tolist()
    ma_cache = build_ma200_cache(symbols, batch_chunk, use_batch_prices)
    if use_batch_prices and len(ma_cache) < max(5, len(symbols) * 0.05):
        st.warning(f"배치 시세 미수신 ({len(ma_cache)}/{len(symbols)}). Yahoo IP 차단 가능성. 잠시 후 재시도.")

    total, results, skipped = len(universe_df), [], 0
    progress_bar = st.progress(0, text="초기화 중...")
    sc1, sc2, sc3 = st.columns(3)
    m_scanned = sc1.empty(); m_passed = sc2.empty(); m_skipped = sc3.empty()
    log_ph, log_lines = st.empty(), []

    for i, (_, row) in enumerate(universe_df.iterrows()):
        sym = row["Symbol"]
        progress_bar.progress((i+1)/total, text=f"스캔 중... {sym} ({i+1}/{total})")
        m_scanned.metric("스캔 완료", f"{i+1}/{total}")
        m_passed.metric("조건 통과", len(results))
        m_skipped.metric("제외", skipped)

        result = screen_ticker(
            row, n_quarters, min_mcap_b,
            above_mode, ma_ratio_threshold,
            use_fpe_filter, max_fwd_pe,
            use_max_drawdown, min_drawdown_pct,
            ma_cache=ma_cache,
        )
        time.sleep(request_delay)

        if result:
            results.append(result)
            log_lines.insert(0, f"✅ {sym} — 통과")
        else:
            skipped += 1
            log_lines.insert(0, f"⬜ {sym}")
        if len(log_lines) > 8: log_lines = log_lines[:8]
        log_ph.markdown("\n".join(log_lines))

        if i == 24 and len(results) == 0 and skipped >= 23:
            st.error("연속 25종목 모두 실패 — Yahoo IP 차단으로 보입니다. 스캔 중단.")
            break

    progress_bar.progress(1.0, text="스크리닝 완료!")
    st.session_state.update(results=results, run_date=datetime.now().strftime("%Y-%m-%d %H:%M"), run_mode=mode_label)

# ──────────────────────────────────────────
# 결과 표시
# ──────────────────────────────────────────
if "results" in st.session_state:
    results  = st.session_state["results"]
    run_date = st.session_state.get("run_date","")
    run_mode = st.session_state.get("run_mode", mode_label)
    is_below = "저평가" in run_mode

    st.markdown("---")
    st.markdown(
        f"### 🎯 {run_mode} — 통과 종목 {len(results)}개 "
        f"<small style='color:#999'>({run_date} 기준)</small>",
        unsafe_allow_html=True,
    )

    if not results:
        st.warning("조건을 만족하는 종목이 없습니다. 조건을 완화해보세요.")
    else:
        all_sectors = sorted(set(r["Sector"] for r in results))
        sel_sectors = st.multiselect("섹터 필터", all_sectors, default=all_sectors)
        filtered = [r for r in results if r["Sector"] in sel_sectors]

        sort_options = (
            ["Drawdown% (하락 큰 순)", "Price/MA200 (오름차순)", "Fwd PE (오름차순)", "MCap($B) (내림차순)", "Symbol (가나다)"]
            if is_below else
            ["Price/MA200 (내림차순)", "MCap($B) (내림차순)", "Fwd PE (오름차순)", "Symbol (가나다)"]
        )
        sort_by = st.selectbox("정렬 기준", sort_options)

        if "Drawdown" in sort_by:
            filtered = sorted(filtered, key=lambda x: (x["Drawdown%"] is None, x["Drawdown%"] or 0))
        elif "Price/MA200 (내림)" in sort_by:
            filtered = sorted(filtered, key=lambda x: x["Price/MA200"], reverse=True)
        elif "Price/MA200 (오름)" in sort_by:
            filtered = sorted(filtered, key=lambda x: x["Price/MA200"])
        elif "MCap" in sort_by:
            filtered = sorted(filtered, key=lambda x: x["MCap($B)"], reverse=True)
        elif "Fwd PE" in sort_by:
            filtered = sorted(filtered, key=lambda x: (x["Fwd PE"] is None, x["Fwd PE"] or 9999))
        else:
            filtered = sorted(filtered, key=lambda x: x["Symbol"])

        # 요약 테이블
        summary_rows = []
        for r in filtered:
            row_d = {
                "Symbol":      r["Symbol"],
                "Company":     r["Company"],
                "Sector":      r["Sector"],
                "Price":       f"${r['Price']:,.2f}",
                "MA200":       f"${r['MA200']:,.2f}",
                "Price/MA200": f"{r['Price/MA200']:.2%}",
            }
            if is_below:
                row_d["52W High"] = f"${r['High52']:,.2f}" if r.get("High52") else "N/A"
                row_d["Drawdown"] = f"{r['Drawdown%']:.1f}%" if r.get("Drawdown%") is not None else "N/A"
            row_d["Fwd PE"]   = f"{r['Fwd PE']:.1f}x"   if r.get("Fwd PE")   else "N/A"
            row_d["Trail PE"] = f"{r['Trail PE']:.1f}x"  if r.get("Trail PE") else "N/A"
            row_d["Fwd EPS"]  = f"${r['Fwd EPS']:.2f}"  if r.get("Fwd EPS")  else "N/A"
            row_d["MCap($B)"] = f"${r['MCap($B)']:,.1f}B"
            for qi, q in enumerate(r["EPS Details"], 1):
                sign = "+" if q["surprise"] >= 0 else ""
                row_d[f"Q-{qi} 서프라이즈"] = f"{sign}{q['surprise']:.1f}%"
            summary_rows.append(row_d)

        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

        # 종목 카드
        st.markdown("#### 📋 종목별 상세")
        for i in range(0, len(filtered), 3):
            cols = st.columns(3)
            for j, col in enumerate(cols):
                idx = i + j
                if idx >= len(filtered): break
                r = filtered[idx]
                with col:
                    with st.container(border=True):
                        chip = f"<span class='{'below-chip' if is_below else 'above-chip'}'>{'📉 MA 아래' if is_below else '📈 MA 위'}</span>"
                        st.markdown(f"**{r['Symbol']}** &nbsp; {chip}", unsafe_allow_html=True)
                        st.caption(f"{r['Company']} · {r['Sector']}")
                        c1, c2 = st.columns(2)
                        c1.metric("현재가",   f"${r['Price']:,.2f}")
                        c2.metric("200일선",  f"${r['MA200']:,.2f}")
                        c1.metric("Price/MA", f"{r['Price/MA200']:.2%}")
                        c2.metric("시총",     f"${r['MCap($B)']:,.1f}B")
                        if is_below and r.get("High52"):
                            c3, c4 = st.columns(2)
                            c3.metric("52W 고점", f"${r['High52']:,.2f}")
                            c4.metric("고점 대비", f"{r['Drawdown%']:.1f}%" if r.get("Drawdown%") else "N/A",
                                      delta_color="inverse")
                        c5, c6 = st.columns(2)
                        c5.metric("Fwd P/E",   f"{r['Fwd PE']:.1f}x"  if r.get("Fwd PE")  else "N/A")
                        c6.metric("Trail P/E", f"{r['Trail PE']:.1f}x" if r.get("Trail PE") else "N/A")
                        st.markdown("**EPS 서프라이즈**")
                        for qi, q in enumerate(r["EPS Details"], 1):
                            chip_cls = "beat-chip" if q["beat"] else "miss-chip"
                            sign = "+" if q["surprise"] >= 0 else ""
                            st.markdown(
                                f"Q-{qi} `{q['date']}` Est:`{q['estimate']}` Rep:`{q['reported']}` "
                                f"<span class='{chip_cls}'>{sign}{q['surprise']:.1f}%</span>",
                                unsafe_allow_html=True,
                            )

        # ── 다운로드 ──────────────────────────────────────────────
        def build_markdown(results_list, run_dt, n_q, mcap_b, ratio, is_below_mode):
            lines = [
                f"# {'📉 저평가 역발상' if is_below_mode else '📈 강세 추세 추종'} 스크리닝 결과", "",
                f"> **기준일**: {run_dt}  ",
                f"> **모드**: {'200일선 아래 저평가' if is_below_mode else '200일선 위 강세'}  ",
                f"> **조건**: 최근 {n_q}분기 연속 EPS 비트 | 현재가/MA200 {'≤' if is_below_mode else '≥'} {ratio:.0%} | 시총 ≥ ${mcap_b}B  ",
                f"> **통과 종목**: {len(results_list)}개", "", "---", "",
            ]
            for r in results_list:
                fpe  = f"{r['Fwd PE']:.1f}x"  if r.get("Fwd PE")  else "N/A"
                tpe  = f"{r['Trail PE']:.1f}x" if r.get("Trail PE") else "N/A"
                feps = f"${r['Fwd EPS']:.2f}"  if r.get("Fwd EPS") else "N/A"
                lines += [
                    f"## {r['Symbol']} — {r['Company']}",
                    f"**섹터**: {r['Sector']}  ", "",
                    f"| 항목 | 값 |", f"|------|-----|",
                    f"| 현재가 | ${r['Price']:,.2f} |",
                    f"| 200일선 | ${r['MA200']:,.2f} |",
                    f"| Price / MA200 | {r['Price/MA200']:.2%} |",
                ]
                if is_below_mode:
                    lines += [
                        f"| 52주 고점 | ${r['High52']:,.2f} |" if r.get("High52") else "| 52주 고점 | N/A |",
                        f"| 고점 대비 하락 | {r['Drawdown%']:.1f}% |" if r.get("Drawdown%") is not None else "| 고점 대비 하락 | N/A |",
                    ]
                lines += [
                    f"| Forward P/E | {fpe} |",
                    f"| Trailing P/E | {tpe} |",
                    f"| Forward EPS | {feps} |",
                    f"| 시가총액 | ${r['MCap($B)']:,.1f}B |", "",
                    "**EPS 서프라이즈**", "",
                    "| 분기 | 발표일 | 컨센서스 | 실제 EPS | 서프라이즈 |",
                    "|------|--------|---------|---------|-----------|",
                ]
                for qi, q in enumerate(r["EPS Details"], 1):
                    sign = "+" if q["surprise"] >= 0 else ""
                    icon = "✅" if q["beat"] else "❌"
                    lines.append(f"| Q-{qi} | {q['date']} | {q['estimate']} | {q['reported']} | {icon} {sign}{q['surprise']:.1f}% |")
                lines += ["", "---", ""]
            lines.append("*Generated by EPS Beat Screener*")
            return "\n".join(lines)

        csv = pd.DataFrame(summary_rows).to_csv(index=False, encoding="utf-8-sig")
        md  = build_markdown(filtered, run_date, n_quarters, min_mcap_b, ma_ratio_threshold, is_below)

        dl1, dl2 = st.columns(2)
        dl1.download_button("📥 CSV 다운로드", data=csv,
            file_name=f"eps_screener_{date.today()}.csv", mime="text/csv", use_container_width=True)
        dl2.download_button("📝 Markdown 다운로드", data=md.encode("utf-8"),
            file_name=f"eps_screener_{date.today()}.md", mime="text/markdown", use_container_width=True)
