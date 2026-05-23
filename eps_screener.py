"""
EPS Beat + 200일선 라지캡 스크리너
- S&P 500 종목 대상
- 최근 3분기 연속 EPS 비트
- 현재가 > 200일 이동평균선 (일봉 기준)
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import time
import json
from datetime import datetime, date
import traceback

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
    request_delay = st.slider("종목당 API 딜레이 (초)", 0.1, 1.0, 0.3, 0.1)

    st.markdown("---")
    st.markdown("### 📋 종목 범위")
    universe = st.radio("스크리닝 대상", ["S&P 500 전체 (~500종목)", "테스트 (상위 50종목)"])
    st.caption("전체 스캔 시 10~20분 소요됩니다.")

# ──────────────────────────────────────────
# 데이터 로딩 함수
# ──────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_sp500_list():
    """위키피디아에서 S&P 500 종목 목록 가져오기"""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    df = pd.read_html(url)[0]
    df = df[['Symbol', 'Security', 'GICS Sector']].rename(
        columns={'Security': 'Company', 'GICS Sector': 'Sector'}
    )
    # 일부 티커 형식 보정 (BRK.B → BRK-B)
    df['Symbol'] = df['Symbol'].str.replace('.', '-', regex=False)
    return df


def get_eps_beat_info(ticker_obj, n_quarters: int):
    """
    최근 n_quarters 분기 모두 EPS 비트했는지 확인
    Returns: (passed: bool, details: list of dicts)
    """
    try:
        earnings = ticker_obj.get_earnings_dates(limit=20)
        if earnings is None or earnings.empty:
            return False, []

        # 실제 발표된 분기만 필터 (Reported EPS 존재)
        past = earnings.dropna(subset=['Reported EPS', 'EPS Estimate']).copy()
        if len(past) < n_quarters:
            return False, []

        recent = past.head(n_quarters)
        details = []
        for idx, row in recent.iterrows():
            est  = row['EPS Estimate']
            rep  = row['Reported EPS']
            beat = rep > est
            surp = ((rep - est) / abs(est) * 100) if est != 0 else 0.0
            details.append({
                'date':     str(idx.date()) if hasattr(idx, 'date') else str(idx),
                'estimate': round(float(est), 4),
                'reported': round(float(rep), 4),
                'surprise': round(float(surp), 2),
                'beat':     bool(beat),
            })

        all_beat = all(d['beat'] for d in details)
        return all_beat, details

    except Exception:
        return False, []


def get_ma200_info(ticker_obj):
    """
    200일 이동평균선 대비 현재가 확인
    Returns: (passed: bool, price, ma200, ratio) or None on failure
    """
    try:
        hist = ticker_obj.history(period="300d", interval="1d")
        if hist is None or len(hist) < 201:
            return None

        closes = hist['Close']
        ma200  = closes.rolling(200).mean().iloc[-1]
        price  = closes.iloc[-1]
        ratio  = price / ma200

        return {
            'price':  round(float(price),  2),
            'ma200':  round(float(ma200),  2),
            'ratio':  round(float(ratio),  4),
        }
    except Exception:
        return None


def screen_ticker(row, n_quarters, min_mcap_b, min_price_vs_ma):
    """단일 종목 스크리닝, 통과하면 결과 dict 반환, 아니면 None"""
    ticker_sym = row['Symbol']
    try:
        t = yf.Ticker(ticker_sym)

        # 시가총액 체크
        info   = t.fast_info
        mcap   = getattr(info, 'market_cap', None)
        if mcap is None or mcap < min_mcap_b * 1e9:
            return None

        # EPS 비트 체크
        eps_pass, eps_details = get_eps_beat_info(t, n_quarters)
        if not eps_pass:
            return None

        # 200일선 체크
        ma_info = get_ma200_info(t)
        if ma_info is None:
            return None
        if ma_info['ratio'] < min_price_vs_ma:
            return None

        return {
            'Symbol':     ticker_sym,
            'Company':    row['Company'],
            'Sector':     row['Sector'],
            'Price':      ma_info['price'],
            'MA200':      ma_info['ma200'],
            'Price/MA200':ma_info['ratio'],
            'MCap($B)':   round(mcap / 1e9, 1),
            'EPS Details':eps_details,
        }

    except Exception:
        return None


# ──────────────────────────────────────────
# 메인 UI
# ──────────────────────────────────────────
st.markdown('<div class="main-title">📈 EPS Beat + 200일선 스크리너</div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="sub-title">S&P 500 라지캡 | 최근 {n_quarters}분기 연속 EPS 비트 | 현재가 > 200일 이동평균선</div>',
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
        st.rerun()

# ──────────────────────────────────────────
# 스크리닝 실행
# ──────────────────────────────────────────
if run_btn:
    sp500 = get_sp500_list()
    if "테스트" in universe:
        sp500 = sp500.head(50)

    total   = len(sp500)
    results = []
    errors  = []
    skipped = 0

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

        result = screen_ticker(row, n_quarters, min_mcap_b, min_price_vs_ma)
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

    progress_bar.progress(1.0, text="스크리닝 완료!")
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
        sort_by = st.selectbox("정렬 기준", ['Price/MA200 (내림차순)', 'MCap($B) (내림차순)', 'Symbol (가나다)'])

        if sort_by.startswith('Price/MA200'):
            filtered = sorted(filtered, key=lambda x: x['Price/MA200'], reverse=True)
        elif sort_by.startswith('MCap'):
            filtered = sorted(filtered, key=lambda x: x['MCap($B)'], reverse=True)
        else:
            filtered = sorted(filtered, key=lambda x: x['Symbol'])

        # 요약 테이블
        summary_rows = []
        for r in filtered:
            eps = r['EPS Details']
            row_d = {
                'Symbol':      r['Symbol'],
                'Company':     r['Company'],
                'Sector':      r['Sector'],
                'Price':       f"${r['Price']:,.2f}",
                'MA200':       f"${r['MA200']:,.2f}",
                'Price/MA200': f"{r['Price/MA200']:.2%}",
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
                        st.markdown("**EPS 서프라이즈 (최신 → 과거)**")
                        for qi, q in enumerate(r['EPS Details'], 1):
                            chip_cls = "beat-chip" if q['beat'] else "miss-chip"
                            sign     = "+" if q['surprise'] >= 0 else ""
                            st.markdown(
                                f"Q-{qi} `{q['date']}` &nbsp; Est: `{q['estimate']}` → Rep: `{q['reported']}` &nbsp;"
                                f"<span class='{chip_cls}'>{sign}{q['surprise']:.1f}%</span>",
                                unsafe_allow_html=True,
                            )

        # CSV 다운로드
        csv = df_display.to_csv(index=False, encoding='utf-8-sig')
        st.download_button(
            "📥 결과 CSV 다운로드",
            data=csv,
            file_name=f"eps_screener_{date.today()}.csv",
            mime="text/csv",
        )
