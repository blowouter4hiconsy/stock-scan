"""
EPS Beat + 200일선 라지캡 스크리너
- Russell 1000 / S&P 500 유니버스
- 최근 N분기 연속 EPS 비트
- 현재가 > 200일 이동평균선 (일봉 기준)
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

# yfinance: "No earnings dates found, symbol may be delisted" 등 stderr 노이즈 억제
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

YAHOO_BATCH_CHUNK = 30
YAHOO_BATCH_PAUSE = 3.0

# ──────────────────────────────────────────
# 페이지 설정
# ──────────────────────────────────────────
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
    .metric-box { background: #f0f4ff; border-radius: 8px; padding: 12px 16px; margin: 4px 0; }
    .beat-chip  { display: inline-block; background: #e6f4ea; color: #1e7e34;
                  border-radius: 12px; padding: 2px 10px; font-size: 0.78rem; font-weight: 600; }
    .miss-chip  { display: inline-block; background: #fce8e6; color: #c62828;
                  border-radius: 12px; padding: 2px 10px; font-size: 0.78rem; font-weight: 600; }
    .stProgress > div > div { background: #1a73e8; }
    div[data-testid="stSidebar"] { background: #f7f9fc; }
</style>
""", unsafe_allow_html=True)

# ──────────────────────────────────────────
# 사이드바 설정
# ──────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ 스크리닝 조건")
    n_quarters = st.slider("연속 EPS 비트 분기 수", 1, 5, 3)
    min_mcap_b = st.slider("최소 시가총액 (십억 달러)", 1, 50, 10)
    min_price_vs_ma = st.slider("현재가 / 200일선 최소 비율", 1.00, 1.20, 1.00, 0.01,
                                help="1.05 → 200일선보다 5% 이상 위에 있어야 통과")

    st.markdown("---")
    st.markdown("### 📊 Valuation 필터")
    use_fpe_filter = st.checkbox("Forward P/E 상한 적용", value=False)
    max_fwd_pe = st.slider("Forward P/E 최대값", 10, 100, 40, 1,
                           disabled=not use_fpe_filter,
                           help="Forward P/E가 이 값보다 낮은 종목만 통과 (데이터 없는 종목은 통과 처리)")
    request_delay = st.slider(
        "종목당 API 딜레이 (초)", 0.5, 3.0, 1.2, 0.1,
        help="Yahoo 차단 시 2초 이상 권장",
    )
    batch_chunk = st.slider("시세 배치 크기 (종목/요청)", 10, 50, 30, 5,
                            help="한 번에 여러 종목 시세를 받아 API 호출 수를 줄입니다.")
    use_batch_prices = st.checkbox("배치 시세 다운로드 (Yahoo 차단 완화)", value=True)

    st.markdown("---")
    st.markdown("### 📋 종목 범위")
    universe_src = st.radio("스크리닝 대상", [
        "Russell 1000 (iShares IWB) — 시총 상위 ~1000종목",
        "S&P 500 (Wikipedia) — 500종목",
        "테스트 (상위 50종목)",
    ])
    st.caption("Russell 1000 전체 스캔 시 20~30분 소요됩니다.")

# ──────────────────────────────────────────
# 데이터 로딩 함수
# ──────────────────────────────────────────
ISHARES_IWB_PRODUCT = "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf"
TRENDSPIDER_R1000_URL = "https://trendspider.com/learning-center/russell-1000-index/"


def _browser_headers(referer: Optional[str] = None, accept_csv: bool = False) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if accept_csv:
        headers["Accept"] = "text/csv,application/octet-stream,*/*"
        headers["Referer"] = referer or ISHARES_IWB_PRODUCT
    else:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    if referer:
        headers["Referer"] = referer
    return headers


def _normalize_symbol(ticker: str) -> str:
    sym = str(ticker).strip().upper().replace('"', "")
    sym = sym.replace("/", "-")
    if sym in ("BRKB", "BRK B"):
        return "BRK-B"
    if "." in sym:
        return sym.replace(".", "-")
    return sym


def _finalize_universe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["Symbol"] = out["Symbol"].map(_normalize_symbol)
    out["Company"] = out["Company"].astype(str).str.strip()
    if "Sector" not in out.columns:
        out["Sector"] = "N/A"
    out["Sector"] = out["Sector"].astype(str).str.strip()
    out = out[out["Symbol"].str.match(r"^[A-Z][A-Z0-9.-]{0,9}$", na=False)]
    out = out.drop_duplicates(subset=["Symbol"], keep="first")
    return out[["Symbol", "Company", "Sector"]].reset_index(drop=True)


def _parse_ishares_holdings_csv(text: str) -> pd.DataFrame:
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if re.search(r'^"?Ticker"?', line.strip(), re.I)
        ),
        None,
    )
    if start is None:
        raise ValueError("Ticker 헤더 없음")

    df_raw = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    df_raw.columns = [
        re.sub(r'^"|"$', "", str(col)).strip() for col in df_raw.columns
    ]
    col_map = {c.lower(): c for c in df_raw.columns}

    ticker_col = col_map.get("ticker") or col_map.get("symbol")
    if not ticker_col:
        raise ValueError("Ticker/Symbol 컬럼 없음")

    name_col = col_map.get("name") or col_map.get("security") or ticker_col
    sector_col = col_map.get("sector")
    asset_col = col_map.get("asset class")

    df_raw = df_raw.rename(
        columns={ticker_col: "Symbol", name_col: "Company"}
    )
    df_raw["Sector"] = df_raw[sector_col] if sector_col else "N/A"

    if asset_col:
        asset = df_raw[asset_col].fillna("").astype(str).str.strip().str.lower()
        keep = asset.isin({"equity", "common stock", "stocks"}) | asset.eq("")
        df_raw = df_raw[keep].copy()

    df_raw["Symbol"] = df_raw["Symbol"].astype(str).str.strip()
    df_raw = df_raw[~df_raw["Symbol"].str.upper().isin({"TICKER", "NAN", ""})]
    return _finalize_universe(df_raw)


def _load_ishares_russell1000() -> pd.DataFrame:
    session = requests.Session()
    session.headers.update(_browser_headers())

    page_resp = session.get(ISHARES_IWB_PRODUCT, timeout=30)
    page_resp.raise_for_status()
    page_html = page_resp.text.replace("&amp;", "&")

    csv_urls: list[str] = []
    for match in re.finditer(
        r"(https://www\.ishares\.com[^\"'\s>]+\.ajax\?[^\"'\s>]*fileType=csv[^\"'\s>]*)",
        page_html,
        re.I,
    ):
        csv_urls.append(match.group(1))
    for ts in re.findall(
        r"(\d{10,})\.ajax\?fileType=csv[^\"'\s>]*fileName=IWB_holdings",
        page_html,
        re.I,
    ):
        csv_urls.append(
            f"{ISHARES_IWB_PRODUCT}/{ts}.ajax"
            "?fileType=csv&fileName=IWB_holdings&dataType=fund"
        )

    csv_urls.extend([
        f"{ISHARES_IWB_PRODUCT}/1467271812596.ajax"
        "?fileType=csv&fileName=IWB_holdings&dataType=fund",
        "https://www.ishares.com/us/products/239707/"
        "ISHARES-RUSSELL-1000-ETF/1467271812596.ajax"
        "?fileType=csv&fileName=IWB_holdings&dataType=fund",
    ])

    seen: set[str] = set()
    csv_headers = _browser_headers(referer=ISHARES_IWB_PRODUCT, accept_csv=True)
    for url in csv_urls:
        if url in seen:
            continue
        seen.add(url)
        resp = session.get(url, headers=csv_headers, timeout=30)
        if resp.status_code != 200 or "Ticker" not in resp.text:
            continue
        df = _parse_ishares_holdings_csv(resp.text)
        if len(df) > 100:
            return df

    tables = pd.read_html(io.StringIO(page_html))
    rows = []
    for table in tables:
        cols = {str(c).strip().lower(): c for c in table.columns}
        sym_key = next((k for k in cols if k in ("ticker", "symbol")), None)
        if not sym_key:
            continue
        name_key = next((k for k in cols if k in ("name", "security", "company name")), sym_key)
        sector_key = cols.get("sector")
        for _, row in table.iterrows():
            sym = _normalize_symbol(row[cols[sym_key]])
            if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", sym):
                continue
            rows.append({
                "Symbol": sym,
                "Company": str(row[cols[name_key]]).strip(),
                "Sector": str(row[sector_key]).strip() if sector_key else "N/A",
            })
    if rows:
        df = _finalize_universe(pd.DataFrame(rows))
        if len(df) > 100:
            return df

    raise ValueError("iShares IWB CSV/HTML에서 유효한 종목 목록을 찾지 못함")


def _load_trendspider_russell1000() -> pd.DataFrame:
    resp = requests.get(
        TRENDSPIDER_R1000_URL,
        headers=_browser_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    html = resp.text

    rows: list[dict] = []
    for table in pd.read_html(io.StringIO(html)):
        sym_col = next(
            (c for c in table.columns if str(c).strip().lower() in ("symbol", "ticker")),
            None,
        )
        if sym_col is None:
            continue
        name_col = next(
            (
                c
                for c in table.columns
                if str(c).strip().lower() in ("company name", "name", "company")
            ),
            None,
        )
        for _, row in table.iterrows():
            sym = _normalize_symbol(row[sym_col])
            if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", sym):
                continue
            company = str(row[name_col]).strip() if name_col else sym
            rows.append({"Symbol": sym, "Company": company, "Sector": "N/A"})

    if len(rows) < 100:
        for sym, company in re.findall(
            r"\|\s*\[([A-Z][A-Z0-9.-]{0,9})\]\([^)]+\)\s*\|\s*\[([^\]]+)\]",
            html,
        ):
            rows.append({
                "Symbol": _normalize_symbol(sym),
                "Company": company.strip(),
                "Sector": "N/A",
            })

    df = _finalize_universe(pd.DataFrame(rows))
    if len(df) < 100:
        raise ValueError(f"종목 수 부족: {len(df)}")
    return df


def _load_sp500_plus_sp400() -> pd.DataFrame:
    headers = _browser_headers()
    sp500_resp = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers=headers,
        timeout=20,
    )
    sp500_resp.raise_for_status()
    sp500 = pd.read_html(io.StringIO(sp500_resp.text))[0]
    sp500 = sp500[["Symbol", "Security", "GICS Sector"]].rename(
        columns={"Security": "Company", "GICS Sector": "Sector"}
    )

    sp400_resp = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        headers=headers,
        timeout=20,
    )
    sp400_resp.raise_for_status()
    sp400 = pd.read_html(io.StringIO(sp400_resp.text))[0]
    symbol_col = next(
        (c for c in sp400.columns if str(c).strip().lower() in ("symbol", "ticker symbol")),
        None,
    )
    if symbol_col is None:
        raise ValueError("S&P 400 Symbol 컬럼 없음")
    company_col = next(
        (c for c in sp400.columns if str(c).strip().lower() in ("security", "company")),
        symbol_col,
    )
    sector_col = next(
        (c for c in sp400.columns if "gics sector" in str(c).strip().lower()),
        None,
    )
    sp400 = sp400[[symbol_col, company_col] + ([sector_col] if sector_col else [])].copy()
    sp400 = sp400.rename(
        columns={
            symbol_col: "Symbol",
            company_col: "Company",
            **({sector_col: "Sector"} if sector_col else {}),
        }
    )
    if "Sector" not in sp400.columns:
        sp400["Sector"] = "N/A"

    merged = pd.concat([sp500, sp400], ignore_index=True)
    return _finalize_universe(merged)


def _load_wikipedia_sp500() -> pd.DataFrame:
    resp = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers=_browser_headers(),
        timeout=20,
    )
    resp.raise_for_status()
    df = pd.read_html(io.StringIO(resp.text))[0]
    df = df[["Symbol", "Security", "GICS Sector"]].rename(
        columns={"Security": "Company", "GICS Sector": "Sector"}
    )
    return _finalize_universe(df)


def _builtin_fallback_universe() -> pd.DataFrame:
    """내장 100종목 (모든 원격 소스 실패 시)"""
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
    return pd.DataFrame(fallback, columns=["Symbol", "Company", "Sector"])


@st.cache_data(ttl=86400)
def _get_universe_cached(source: str) -> tuple[pd.DataFrame, str, str]:
    """
    Returns: (dataframe, source_label, ui_level)
    ui_level: '' | 'info' | 'warning' | 'error'
    """
    if "Russell" in source:
        loaders = [
            ("iShares IWB", _load_ishares_russell1000),
            ("TrendSpider", _load_trendspider_russell1000),
            ("S&P 500 + S&P 400 (Russell 1000 근사)", _load_sp500_plus_sp400),
        ]
        errors: list[str] = []
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
            detail = " · ".join(errors) if errors else "원인 미상"
            return (
                df,
                f"S&P 500 대체 (Russell 실패: {detail})",
                "warning",
            )
        except Exception as exc:
            errors.append(f"S&P 500: {exc}")
            detail = " · ".join(errors)
            return (
                _builtin_fallback_universe(),
                f"내장 100종목 ({detail})",
                "error",
            )

    if "S&P" in source:
        try:
            return _load_wikipedia_sp500(), "Wikipedia S&P 500", ""
        except Exception as exc:
            return _builtin_fallback_universe(), f"내장 100종목 ({exc})", "warning"

    return _builtin_fallback_universe(), "내장 100종목", "warning"


def get_universe(source: str = "Russell 1000 (iShares IWB)") -> pd.DataFrame:
    df, label, level = _get_universe_cached(source)
    if level == "info":
        st.info(f"Russell 1000: **{label}**에서 {len(df)}개 종목 로딩")
    elif level == "warning":
        st.warning(f"종목 목록: **{label}** ({len(df)}개)")
    elif level == "error":
        st.error(f"Russell 1000 로딩 실패 — **{label}** ({len(df)}개)만 사용합니다.")
    return df


@st.cache_resource
def _yf_browser_session():
    """curl_cffi 브라우저 위장 세션 (있으면 Yahoo 차단 완화)"""
    try:
        from curl_cffi.requests import Session
        return Session(impersonate="chrome")
    except Exception:
        return None


def _make_ticker(symbol: str):
    session = _yf_browser_session()
    return yf.Ticker(symbol, session=session) if session else yf.Ticker(symbol)


def _yf_retry(fn: Callable, retries: int = 3, base_delay: float = 2.0):
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return _quiet_call(fn)
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("yfinance retry failed")


def yahoo_health_check() -> tuple[bool, str]:
    """스캔 전 Yahoo 접속 가능 여부 (SPY 5일봉)"""
    try:
        kwargs = dict(period="5d", interval="1d", progress=False, threads=False)
        session = _yf_browser_session()
        if session:
            kwargs["session"] = session
        data = _yf_retry(lambda: yf.download("SPY", **kwargs), retries=2, base_delay=2.0)
        if data is None or (hasattr(data, "empty") and data.empty):
            return False, "SPY 시세 없음 — IP 차단·레이트리밋 가능"
        return True, "SPY 시세 수신 OK"
    except Exception as exc:
        return False, str(exc)


def _batch_download_closes(
    symbols: list[str],
    chunk_size: int = YAHOO_BATCH_CHUNK,
    pause: float = YAHOO_BATCH_PAUSE,
) -> dict[str, pd.Series]:
    """종목별 종가 시계열 — 개별 history() 대신 download() 배치"""
    session = _yf_browser_session()
    out: dict[str, pd.Series] = {}
    kwargs = dict(
        period="300d",
        interval="1d",
        group_by="ticker",
        threads=False,
        progress=False,
        auto_adjust=True,
    )
    if session:
        kwargs["session"] = session

    for start in range(0, len(symbols), chunk_size):
        chunk = symbols[start : start + chunk_size]
        tickers = " ".join(chunk)
        try:
            data = _yf_retry(
                lambda t=tickers: yf.download(t, **kwargs),
                retries=3,
                base_delay=pause,
            )
        except Exception:
            time.sleep(pause * 2)
            continue

        if data is None or (hasattr(data, "empty") and data.empty):
            time.sleep(pause)
            continue

        if len(chunk) == 1:
            sym = chunk[0]
            closes = data["Close"] if "Close" in data.columns else data.squeeze()
            if isinstance(closes, pd.DataFrame):
                closes = closes.iloc[:, 0]
            out[sym] = closes.dropna()
        elif isinstance(data.columns, pd.MultiIndex):
            level0 = data.columns.get_level_values(0)
            for sym in chunk:
                if sym in level0:
                    out[sym] = data[sym]["Close"].dropna()

        time.sleep(pause)
    return out


def _ma200_from_closes(closes: pd.Series) -> Optional[dict]:
    if closes is None or len(closes) < 201:
        return None
    ma200 = closes.rolling(200).mean().iloc[-1]
    price = closes.iloc[-1]
    if pd.isna(ma200) or ma200 <= 0 or pd.isna(price):
        return None
    ratio = price / ma200
    return {
        "price": round(float(price), 2),
        "ma200": round(float(ma200), 2),
        "ratio": round(float(ratio), 4),
    }


def build_ma200_cache(
    symbols: list[str],
    chunk_size: int,
    use_batch: bool,
) -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if use_batch and symbols:
        progress = st.progress(0, text="배치 시세 로딩 중...")
        closes_map = {}
        n_chunks = max(1, (len(symbols) + chunk_size - 1) // chunk_size)
        for idx, start in enumerate(range(0, len(symbols), chunk_size)):
            chunk = symbols[start : start + chunk_size]
            progress.progress(
                (idx + 1) / n_chunks,
                text=f"시세 배치 {idx + 1}/{n_chunks} ({len(chunk)}종목)",
            )
            closes_map.update(_batch_download_closes(chunk, chunk_size=len(chunk)))
        progress.empty()
        for sym, closes in closes_map.items():
            info = _ma200_from_closes(closes)
            if info:
                cache[sym] = info
    return cache


def _get_market_cap(ticker_obj) -> Optional[float]:
    try:
        fi = _quiet_call(lambda: ticker_obj.fast_info)
        for key in ("market_cap", "marketCap"):
            val = None
            try:
                val = fi[key]  # FastInfo는 dict-like
            except (TypeError, KeyError):
                val = getattr(fi, key, None)
            if val is not None and not pd.isna(val):
                return float(val)
    except Exception:
        pass
    try:
        mcap = ticker_obj.info.get("marketCap")
        return float(mcap) if mcap else None
    except Exception:
        return None


def _quiet_call(fn: Callable):
    """yfinance print/stderr 메시지 억제"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return fn()


def _coerce_earnings_df(raw) -> Optional[pd.DataFrame]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = pd.DataFrame(raw)
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None

    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    rename: dict = {}
    for col in df.columns:
        key = str(col).strip().lower().replace(" ", "").replace("_", "")
        if key in ("reportedeps", "epsactual", "actual"):
            rename[col] = "Reported EPS"
        elif key in ("epsestimate", "estimate"):
            rename[col] = "EPS Estimate"
    df = df.rename(columns=rename)

    if "Reported EPS" not in df.columns or "EPS Estimate" not in df.columns:
        return None

    if not isinstance(df.index, pd.DatetimeIndex):
        for date_col in ("Earnings Date", "Date", "Quarter", "period"):
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                df = df.dropna(subset=[date_col]).set_index(date_col)
                break
        else:
            try:
                df.index = pd.to_datetime(df.index, errors="coerce")
            except Exception:
                return None

    df["Reported EPS"] = pd.to_numeric(df["Reported EPS"], errors="coerce")
    df["EPS Estimate"] = pd.to_numeric(df["EPS Estimate"], errors="coerce")
    return df.sort_index(ascending=False)


def _fetch_earnings_dataframe(ticker_obj, symbol: str) -> pd.DataFrame:
    """
    Yahoo/yfinance 실적 API는 종목별로 깨지는 경우가 많음 (HLT 등).
    여러 소스를 순서대로 시도하고, 실패 시 빈 DataFrame 반환.
    """
    loaders: list[Callable] = [
        lambda: ticker_obj.get_earnings_dates(limit=24),
        lambda: ticker_obj.earnings_dates,
        lambda: ticker_obj.get_earnings_history(),
    ]

    for loader in loaders:
        try:
            raw = _yf_retry(loader, retries=2, base_delay=1.5)
            df = _coerce_earnings_df(raw)
            if df is not None and not df.empty:
                valid = df.dropna(subset=["Reported EPS", "EPS Estimate"])
                if len(valid) >= 1:
                    return df
        except Exception:
            continue

    return pd.DataFrame()


def _quarterly_earnings_rows(past: pd.DataFrame) -> pd.DataFrame:
    """Yahoo 중복 행 제거 — 분기당 1행"""
    past = past.sort_index(ascending=False).copy()
    if isinstance(past.index, pd.DatetimeIndex):
        past["_q"] = past.index.to_period("Q")
        past = past.drop_duplicates(subset=["_q"], keep="first").drop(columns=["_q"])
    return past


def get_eps_beat_info(ticker_obj, symbol: str, n_quarters: int):
    """
    최근 n_quarters 분기 모두 EPS 비트했는지 확인
    Returns: (passed: bool, details: list of dicts)
    """
    try:
        earnings = _fetch_earnings_dataframe(ticker_obj, symbol)
        if earnings.empty:
            return False, []

        past = earnings.dropna(subset=["Reported EPS", "EPS Estimate"]).copy()
        past = _quarterly_earnings_rows(past)
        if len(past) < n_quarters:
            return False, []

        recent = past.head(n_quarters)
        details = []
        for idx, row in recent.iterrows():
            est = row["EPS Estimate"]
            rep = row["Reported EPS"]
            beat = rep > est
            surp = ((rep - est) / abs(est) * 100) if est != 0 else 0.0
            details.append({
                "date": str(idx.date()) if hasattr(idx, "date") else str(idx),
                "estimate": round(float(est), 4),
                "reported": round(float(rep), 4),
                "surprise": round(float(surp), 2),
                "beat": bool(beat),
            })

        return all(d["beat"] for d in details), details

    except Exception:
        return False, []


def get_ma200_info(ticker_obj, ma_cache: Optional[dict] = None, symbol: str = ""):
    """200일선 — ma_cache에 있으면 Yahoo 재호출 없음"""
    if ma_cache and symbol and symbol in ma_cache:
        return ma_cache[symbol]
    try:
        hist = _yf_retry(lambda: ticker_obj.history(period="300d", interval="1d"), retries=2)
        if hist is None or len(hist) < 201:
            return None
        return _ma200_from_closes(hist["Close"])
    except Exception:
        return None


def get_fper_info(ticker_obj, price: float):
    """
    Forward P/E Ratio 계산
    - forwardEps: 다음 12개월 컨센서스 EPS (yfinance info)
    - trailingEps: TTM EPS (fallback)
    Returns dict or None
    """
    try:
        info = _quiet_call(lambda: ticker_obj.info)
        fwd_eps     = info.get('forwardEps')
        trail_eps   = info.get('trailingEps')
        fwd_pe      = info.get('forwardPE')      # 야후가 직접 제공할 때
        trail_pe    = info.get('trailingPE')

        result = {}

        # Forward P/E
        if fwd_pe and fwd_pe > 0:
            result['fwd_pe']  = round(float(fwd_pe), 1)
        elif fwd_eps and fwd_eps > 0:
            result['fwd_pe']  = round(price / float(fwd_eps), 1)
        else:
            result['fwd_pe']  = None

        # Trailing P/E (비교용)
        if trail_pe and trail_pe > 0:
            result['trail_pe'] = round(float(trail_pe), 1)
        elif trail_eps and trail_eps > 0:
            result['trail_pe'] = round(price / float(trail_eps), 1)
        else:
            result['trail_pe'] = None

        # Forward EPS 원값도 저장
        result['fwd_eps']   = round(float(fwd_eps),   2) if fwd_eps   else None
        result['trail_eps'] = round(float(trail_eps),  2) if trail_eps else None

        return result
    except Exception:
        return {'fwd_pe': None, 'trail_pe': None, 'fwd_eps': None, 'trail_eps': None}


def screen_ticker(
    row,
    n_quarters,
    min_mcap_b,
    min_price_vs_ma,
    use_fpe_filter=False,
    max_fwd_pe=40,
    ma_cache: Optional[dict] = None,
):
    """단일 종목 스크리닝, 통과하면 결과 dict 반환, 아니면 None"""
    ticker_sym = row["Symbol"]
    try:
        t = _make_ticker(ticker_sym)

        ma_info = get_ma200_info(t, ma_cache=ma_cache, symbol=ticker_sym)
        if ma_info is None:
            return None
        if ma_info["ratio"] < min_price_vs_ma:
            return None

        # 시가총액·EPS는 종목별 호출 (배치 불가)
        mcap = _get_market_cap(t)
        if mcap is None or mcap < min_mcap_b * 1e9:
            return None

        eps_pass, eps_details = get_eps_beat_info(t, ticker_sym, n_quarters)
        if not eps_pass:
            return None

        fper = (
            get_fper_info(t, ma_info["price"])
            if use_fpe_filter
            else {"fwd_pe": None, "trail_pe": None, "fwd_eps": None, "trail_eps": None}
        )

        # Forward P/E 필터 (데이터 있는 경우만 적용)
        if use_fpe_filter and fper.get('fwd_pe') is not None:
            if fper['fwd_pe'] > max_fwd_pe:
                return None

        return {
            'Symbol':      ticker_sym,
            'Company':     row['Company'],
            'Sector':      row['Sector'],
            'Price':       ma_info['price'],
            'MA200':       ma_info['ma200'],
            'Price/MA200': ma_info['ratio'],
            'MCap($B)':    round(mcap / 1e9, 1),
            'Fwd PE':      fper.get('fwd_pe'),
            'Trail PE':    fper.get('trail_pe'),
            'Fwd EPS':     fper.get('fwd_eps'),
            'Trail EPS':   fper.get('trail_eps'),
            'EPS Details': eps_details,
        }

    except Exception:
        return None


with st.sidebar:
    st.markdown("---")
    st.markdown("### 🌐 Yahoo Finance")
    if st.button("Yahoo 연결 테스트 (SPY)", use_container_width=True):
        ok, msg = yahoo_health_check()
        if ok:
            st.success(f"연결 정상 — {msg}")
        else:
            st.error(f"연결 실패 — {msg}")
    st.caption(
        "Cloud·과도한 스캔 시 IP 차단됨. 차단 시 30~60분 대기, "
        "딜레이 2초+, 로컬 실행 권장."
    )


# ──────────────────────────────────────────
# 메인 UI
# ──────────────────────────────────────────
st.markdown('<div class="main-title">📈 EPS Beat + 200일선 스크리너</div>', unsafe_allow_html=True)
_universe_label = (
    "Russell 1000" if "Russell" in universe_src
    else ("S&P 500" if "S&P" in universe_src else "테스트 유니버스")
)
st.markdown(
    f'<div class="sub-title">{_universe_label} | 최근 {n_quarters}분기 연속 EPS 비트 | '
    f'현재가/200일선 ≥ {min_price_vs_ma:.0%}</div>',
    unsafe_allow_html=True,
)

# 결과 캐시 초기화 버튼
col_run, col_reset, _ = st.columns([2, 1, 5])
with col_run:
    run_btn = st.button("🚀 스크리닝 시작", type="primary", use_container_width=True)
with col_reset:
    if st.button("🗑️ 초기화", use_container_width=True):
        st.session_state.pop('results', None)
        st.session_state.pop('run_date', None)
        _get_universe_cached.clear()
        st.rerun()

# ──────────────────────────────────────────
# 스크리닝 실행
# ──────────────────────────────────────────
if run_btn:
    if "Russell" in universe_src:
        src_key = "Russell 1000 (iShares IWB)"
    elif "S&P" in universe_src:
        src_key = "S&P 500 (Wikipedia)"
    else:
        src_key = "Russell 1000 (iShares IWB)"   # 테스트도 Russell 1000에서 50개

    sp500 = get_universe(src_key)
    if "테스트" in universe_src:
        sp500 = sp500.head(50)

    ok, health_msg = yahoo_health_check()
    if not ok:
        st.error(
            f"**Yahoo Finance에 연결되지 않습니다.**\n\n{health_msg}\n\n"
            "· 30~60분 후 다시 시도\n"
            "· 종목당 딜레이 2초 이상\n"
            "· **테스트(50종목)** 으로 먼저 확인\n"
            "· Streamlit Cloud는 IP 차단이 잦음 → **로컬 PC**에서 실행 권장"
        )
        st.stop()

    symbols = sp500["Symbol"].tolist()
    ma_cache = build_ma200_cache(symbols, batch_chunk, use_batch_prices)
    if use_batch_prices and len(ma_cache) < max(5, len(symbols) * 0.05):
        st.warning(
            f"배치 시세가 거의 수신되지 않았습니다 ({len(ma_cache)}/{len(symbols)}). "
            "Yahoo IP 차단 가능성이 큽니다. 잠시 후 다시 시도하세요."
        )

    total   = len(sp500)
    results = []
    skipped = 0
    no_ma_count = 0

    progress_bar = st.progress(0, text="초기화 중...")
    status_col1, status_col2, status_col3 = st.columns(3)
    scanned_metric  = status_col1.empty()
    passed_metric   = status_col2.empty()
    skipped_metric  = status_col3.empty()

    log_placeholder = st.empty()
    log_lines = []

    for i, (_, row) in enumerate(sp500.iterrows()):
        sym = row['Symbol']
        pct = (i + 1) / total
        progress_bar.progress(pct, text=f"스캔 중... {sym} ({i+1}/{total})")
        scanned_metric.metric("스캔 완료", f"{i+1}/{total}")
        passed_metric.metric("조건 통과", len(results))
        skipped_metric.metric("데이터 없음/제외", skipped)

        if use_batch_prices and sym not in ma_cache:
            no_ma_count += 1

        result = screen_ticker(
            row, n_quarters, min_mcap_b, min_price_vs_ma,
            use_fpe_filter, max_fwd_pe, ma_cache=ma_cache,
        )
        time.sleep(request_delay)

        if result:
            results.append(result)
            log_lines.insert(0, f"✅ {sym} — 통과")
        else:
            skipped += 1
            log_lines.insert(0, f"⬜ {sym}")

        if len(log_lines) > 8:
            log_lines = log_lines[:8]
        log_placeholder.markdown("\n".join(log_lines))

        # 초반에 전부 실패하면 Yahoo 차단으로 조기 중단
        if i == 24 and len(results) == 0 and skipped >= 23:
            st.error(
                "연속 25종목 모두 실패했습니다. Yahoo Finance IP 차단·레이트리밋으로 보입니다. "
                "스캔을 중단합니다. 30~60분 후 딜레이를 늘려 다시 시도하세요."
            )
            break

    progress_bar.progress(1.0, text="스크리닝 완료!")
    if no_ma_count > total * 0.8:
        st.warning(
            f"시세 데이터 미수신 {no_ma_count}/{total}건 — Yahoo 차단 시 배치 로딩이 실패합니다."
        )
    st.session_state['results']  = results
    st.session_state['run_date'] = datetime.now().strftime("%Y-%m-%d %H:%M")

# ──────────────────────────────────────────
# 결과 표시
# ──────────────────────────────────────────
if 'results' in st.session_state:
    results  = st.session_state['results']
    run_date = st.session_state.get('run_date', '')

    st.markdown(f"---")
    st.markdown(f"### 🎯 조건 통과 종목 — {len(results)}개  <small style='color:#999'>({run_date} 기준)</small>",
                unsafe_allow_html=True)

    if not results:
        st.warning("조건을 만족하는 종목이 없습니다. 조건을 완화해보세요.")
    else:
        # 섹터 필터
        all_sectors = sorted(set(r['Sector'] for r in results))
        sel_sectors = st.multiselect("섹터 필터", all_sectors, default=all_sectors)
        filtered = [r for r in results if r['Sector'] in sel_sectors]

        # 정렬
        sort_by = st.selectbox("정렬 기준", [
            'Price/MA200 (내림차순)', 'MCap($B) (내림차순)',
            'Fwd PE (오름차순)', 'Symbol (가나다)'
        ])

        if sort_by.startswith('Price/MA200'):
            filtered = sorted(filtered, key=lambda x: x['Price/MA200'], reverse=True)
        elif sort_by.startswith('MCap'):
            filtered = sorted(filtered, key=lambda x: x['MCap($B)'], reverse=True)
        elif sort_by.startswith('Fwd PE'):
            filtered = sorted(filtered, key=lambda x: (x['Fwd PE'] is None, x['Fwd PE'] or 9999))
        else:
            filtered = sorted(filtered, key=lambda x: x['Symbol'])

        # 요약 테이블
        summary_rows = []
        for r in filtered:
            eps = r['EPS Details']
            fpe_str   = f"{r['Fwd PE']:.1f}x"   if r.get('Fwd PE')   else "N/A"
            tpe_str   = f"{r['Trail PE']:.1f}x"  if r.get('Trail PE') else "N/A"
            feps_str  = f"${r['Fwd EPS']:.2f}"   if r.get('Fwd EPS')  else "N/A"
            row_d = {
                'Symbol':      r['Symbol'],
                'Company':     r['Company'],
                'Sector':      r['Sector'],
                'Price':       f"${r['Price']:,.2f}",
                'MA200':       f"${r['MA200']:,.2f}",
                'Price/MA200': f"{r['Price/MA200']:.2%}",
                'Fwd PE':      fpe_str,
                'Trail PE':    tpe_str,
                'Fwd EPS':     feps_str,
                'MCap($B)':    f"${r['MCap($B)']:,.1f}B",
            }
            for qi, q in enumerate(eps, 1):
                row_d[f"Q-{qi} 서프라이즈"] = f"+{q['surprise']:.1f}%" if q['surprise'] >= 0 else f"{q['surprise']:.1f}%"
            summary_rows.append(row_d)

        df_display = pd.DataFrame(summary_rows)
        st.dataframe(df_display, use_container_width=True, hide_index=True)

        # 상세 카드
        st.markdown("#### 📋 종목별 상세")
        cols_per_row = 3
        for i in range(0, len(filtered), cols_per_row):
            cols = st.columns(cols_per_row)
            for j, col in enumerate(cols):
                idx = i + j
                if idx >= len(filtered):
                    break
                r = filtered[idx]
                with col:
                    with st.container(border=True):
                        st.markdown(f"**{r['Symbol']}** &nbsp; <small>{r['Company']}</small>", unsafe_allow_html=True)
                        st.caption(r['Sector'])
                        sub1, sub2 = st.columns(2)
                        sub1.metric("현재가",   f"${r['Price']:,.2f}")
                        sub2.metric("200일선",  f"${r['MA200']:,.2f}")
                        sub1.metric("Price/MA", f"{r['Price/MA200']:.2%}")
                        sub2.metric("시총",     f"${r['MCap($B)']:,.1f}B")
                        sub3, sub4 = st.columns(2)
                        sub3.metric("Fwd P/E",
                                    f"{r['Fwd PE']:.1f}x" if r.get('Fwd PE') else "N/A",
                                    help="Forward P/E = 현재가 / 향후 12개월 컨센서스 EPS")
                        sub4.metric("Trail P/E",
                                    f"{r['Trail PE']:.1f}x" if r.get('Trail PE') else "N/A",
                                    help="Trailing P/E = 현재가 / TTM EPS")
                        if r.get('Fwd EPS'):
                            st.caption(f"Fwd EPS: ${r['Fwd EPS']:.2f}  |  Trail EPS: ${r['Trail EPS']:.2f}" if r.get('Trail EPS') else f"Fwd EPS: ${r['Fwd EPS']:.2f}")
                        st.markdown("**EPS 서프라이즈 (최신 → 과거)**")
                        for qi, q in enumerate(r['EPS Details'], 1):
                            chip_cls = "beat-chip" if q['beat'] else "miss-chip"
                            sign     = "+" if q['surprise'] >= 0 else ""
                            st.markdown(
                                f"Q-{qi} `{q['date']}` &nbsp; Est: `{q['estimate']}` → Rep: `{q['reported']}` &nbsp;"
                                f"<span class='{chip_cls}'>{sign}{q['surprise']:.1f}%</span>",
                                unsafe_allow_html=True,
                            )

        # ── 다운로드 버튼 ────────────────────────────────────────
        def build_markdown(results_list, run_dt, n_q, mcap_b, ratio):
            lines = []
            lines.append(f"# 📈 EPS Beat + 200일선 스크리닝 결과")
            lines.append(f"")
            lines.append(f"> **기준일**: {run_dt}  ")
            lines.append(f"> **조건**: 최근 {n_q}분기 연속 EPS 비트 | 현재가/MA200 ≥ {ratio:.0%} | 시총 ≥ ${mcap_b}B  ")
            lines.append(f"> **통과 종목**: {len(results_list)}개")
            lines.append(f"")
            lines.append(f"---")
            lines.append(f"")

            for r in results_list:
                fpe  = f"{r['Fwd PE']:.1f}x"  if r.get('Fwd PE')   else "N/A"
                tpe  = f"{r['Trail PE']:.1f}x" if r.get('Trail PE') else "N/A"
                feps = f"${r['Fwd EPS']:.2f}"  if r.get('Fwd EPS')  else "N/A"

                lines.append(f"## {r['Symbol']} — {r['Company']}")
                lines.append(f"**섹터**: {r['Sector']}  ")
                lines.append(f"")
                lines.append(f"| 항목 | 값 |")
                lines.append(f"|------|-----|")
                lines.append(f"| 현재가 | ${r['Price']:,.2f} |")
                lines.append(f"| 200일선 | ${r['MA200']:,.2f} |")
                lines.append(f"| Price / MA200 | {r['Price/MA200']:.2%} |")
                lines.append(f"| Forward P/E | {fpe} |")
                lines.append(f"| Trailing P/E | {tpe} |")
                lines.append(f"| Forward EPS | {feps} |")
                lines.append(f"| 시가총액 | ${r['MCap($B)']:,.1f}B |")
                lines.append(f"")
                lines.append(f"**EPS 서프라이즈 (최신 → 과거)**")
                lines.append(f"")
                lines.append(f"| 분기 | 발표일 | 컨센서스 EPS | 실제 EPS | 서프라이즈 |")
                lines.append(f"|------|--------|-------------|---------|-----------|")
                for qi, q in enumerate(r['EPS Details'], 1):
                    sign = "+" if q['surprise'] >= 0 else ""
                    beat_icon = "✅" if q['beat'] else "❌"
                    lines.append(
                        f"| Q-{qi} | {q['date']} | {q['estimate']} "
                        f"| {q['reported']} | {beat_icon} {sign}{q['surprise']:.1f}% |"
                    )
                lines.append(f"")
                lines.append(f"---")
                lines.append(f"")

            lines.append(f"*Generated by EPS Beat Screener*")
            return "\n".join(lines)

        csv = df_display.to_csv(index=False, encoding='utf-8-sig')
        md  = build_markdown(filtered, run_date, n_quarters, min_mcap_b, min_price_vs_ma)

        dl1, dl2 = st.columns(2)
        dl1.download_button(
            "📥 CSV 다운로드",
            data=csv,
            file_name=f"eps_screener_{date.today()}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        dl2.download_button(
            "📝 Markdown 다운로드",
            data=md.encode("utf-8"),
            file_name=f"eps_screener_{date.today()}.md",
            mime="text/markdown",
            use_container_width=True,
        )
