"""
EPS Beat + 200일선 라지캡 스크리너
- S&P 500 종목 대상
- 최근 3분기 연속 EPS 비트
- 현재가 > 200일 이동평균선 (일봉 기준)
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import io
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

    st.markdown("---")
    st.markdown("### 📊 Valuation 필터")
    use_fpe_filter = st.checkbox("Forward P/E 상한 적용", value=False)
    max_fwd_pe = st.slider("Forward P/E 최대값", 10, 100, 40, 1,
                           disabled=not use_fpe_filter,
                           help="Forward P/E가 이 값보다 낮은 종목만 통과 (데이터 없는 종목은 통과 처리)")
request_delay = st.slider("종목당 API 딜레이 (초)", 0.1, 1.0, 0.3, 0.1)

st.markdown("---")
@@ -248,7 +255,49 @@
return None


def screen_ticker(row, n_quarters, min_mcap_b, min_price_vs_ma):
def get_fper_info(ticker_obj, price: float):
    """
    Forward P/E Ratio 계산
    - forwardEps: 다음 12개월 컨센서스 EPS (yfinance info)
    - trailingEps: TTM EPS (fallback)
    Returns dict or None
    """
    try:
        info = ticker_obj.info
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


def screen_ticker(row, n_quarters, min_mcap_b, min_price_vs_ma,
                  use_fpe_filter=False, max_fwd_pe=40):
"""단일 종목 스크리닝, 통과하면 결과 dict 반환, 아니면 None"""
ticker_sym = row['Symbol']
try:
@@ -272,15 +321,27 @@
if ma_info['ratio'] < min_price_vs_ma:
return None

        # Forward P/E 계산
        fper = get_fper_info(t, ma_info['price'])

        # Forward P/E 필터 (데이터 있는 경우만 적용)
        if use_fpe_filter and fper.get('fwd_pe') is not None:
            if fper['fwd_pe'] > max_fwd_pe:
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
@@ -336,7 +397,8 @@
passed_metric.metric("조건 통과", len(results))
skipped_metric.metric("데이터 없음/제외", skipped)

        result = screen_ticker(row, n_quarters, min_mcap_b, min_price_vs_ma)
        result = screen_ticker(row, n_quarters, min_mcap_b, min_price_vs_ma,
                               use_fpe_filter, max_fwd_pe)
time.sleep(request_delay)

if result:
@@ -374,26 +436,37 @@
filtered = [r for r in results if r['Sector'] in sel_sectors]

# 정렬
        sort_by = st.selectbox("정렬 기준", ['Price/MA200 (내림차순)', 'MCap($B) (내림차순)', 'Symbol (가나다)'])
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
@@ -422,6 +495,15 @@
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
