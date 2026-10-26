"""Ponte | A monthly real exchange-rate observatory. Python 3.12."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import StringIO
import html

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pycountry
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st

BASE = 'https://api.imf.org/external/sdmx/3.0'
FIRST = '1976-01'
# IMF membership list, including Liechtenstein; Euro area is a separate aggregate.
MEMBERS = '''AFG ALB DZA AND AGO ATG ARG ARM AUS AUT AZE BHS BHR BGD BRB BLR BEL BLZ BEN BTN BOL BIH BWA BRA BRN BGR BFA BDI CPV KHM CMR CAN CAF TCD CHL CHN COL COM COD COG CRI CIV HRV CYP CZE DNK DJI DMA DOM ECU EGY SLV GNQ ERI EST SWZ ETH FJI FIN FRA GAB GMB GEO DEU GHA GRC GRD GTM GIN GNB GUY HTI HND HUN ISL IND IDN IRN IRQ IRL ISR ITA JAM JPN JOR KAZ KEN KIR XKX KWT KGZ LAO LVA LBN LSO LBR LBY LIE LTU LUX MDG MWI MYS MDV MLI MLT MHL MRT MUS MEX FSM MDA MCO MNG MNE MAR MOZ MMR NAM NRU NPL NLD NZL NIC NER NGA MKD NOR OMN PAK PLW PAN PNG PRY PER PHL POL PRT QAT ROU RUS RWA WSM SMR STP SAU SEN SRB SYC SLE SGP SVK SVN SLB SOM ZAF SSD ESP LKA KNA LCA VCT SDN SUR SWE CHE SYR TJK TZA THA TLS TGO TON TTO TUN TUR TKM TUV UGA UKR ARE GBR USA URY UZB VUT VEN VNM YEM ZMB ZWE'''.split()
# Monaco is not an IMF member.
MEMBERS.remove('MCO')
EURO_START = dict.fromkeys('AUT BEL FIN FRA DEU IRL ITA LUX NLD PRT ESP'.split(), '1999-01')
EURO_START.update({'GRC':'2001-01','SVN':'2007-01','CYP':'2008-01','MLT':'2008-01','SVK':'2009-01','EST':'2011-01','LVA':'2014-01','LTU':'2015-01','HRV':'2023-01','BGR':'2026-01'})
NAMES = {'PRT': 'Portugal', 'USA': 'United States', 'GBR': 'United Kingdom',
         'KOR': 'South Korea', 'XKX': 'Kosovo', 'G163': 'Euro area',
         'COD': 'Congo, Democratic Republic of', 'COG': 'Congo, Republic of',
         'TUR': 'Türkiye', 'CIV': "Côte d’Ivoire"}
MEMBERS.append('KOR')


def country_name(code: str) -> str:
    return NAMES.get(code) or pycountry.countries.get(alpha_3=code).name


def country_label(code: str) -> str:
    if code == 'G163':
        return '🇪🇺 Euro area'
    iso2 = 'XK' if code == 'XKX' else pycountry.countries.get(alpha_3=code).alpha_2
    flag = ''.join(chr(127397 + ord(c)) for c in iso2)
    return f'{flag} {country_name(code)}'


class DataError(Exception):
    """An actionable data-service or schema error."""


def parse_csv(text: str, name: str) -> pd.DataFrame:
    """Read SDMX CSV, honoring scale and preserving observation metadata."""
    try:
        raw = pd.read_csv(StringIO(text), dtype=str)
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        raise DataError('IMF returned an unreadable CSV response.') from exc
    raw.columns = [x.strip().upper() for x in raw.columns]
    if not {'TIME_PERIOD', 'OBS_VALUE'} <= set(raw.columns):
        raise DataError('IMF response does not contain TIME_PERIOD and OBS_VALUE.')
    periods = raw['TIME_PERIOD'].str.replace(r'^(\d{4})-M(\d{2})$', r'\1-\2', regex=True)
    dates = pd.to_datetime(periods, format='%Y-%m', errors='coerce')
    values = pd.to_numeric(raw['OBS_VALUE'], errors='coerce')
    scale = pd.to_numeric(raw.get('SCALE', pd.Series('0', index=raw.index)), errors='coerce').fillna(0)
    out = pd.DataFrame({'month': dates, name: values * np.power(10.0, scale)})
    for col in ('STATUS', 'UNIT', 'EXRATE', 'REFERENCE_PERIOD', 'STRUCTURE_ID'):
        if col in raw:
            out[f'{name}_{col.lower()}'] = raw[col]
    out = out.dropna(subset=['month'])
    out = out[out['month'] >= pd.Timestamp(FIRST)]
    if out['month'].duplicated().any():
        raise DataError('IMF returned multiple observations for a month. Refusing to choose silently.')
    # Zero/negative CPI or rates cannot enter this economic calculation.
    out.loc[~np.isfinite(out[name]) | (out[name] <= 0), name] = np.nan
    return out.set_index('month').sort_index()


@st.cache_data(ttl=21600, show_spinner=False)
def fetch_series(code: str, kind: str) -> tuple[pd.DataFrame, str, str]:
    flow, key = ('CPI', f'{code}.CPI._T.IX.M') if kind == 'cpi' else ('ER', f'{code}.XDC_USD.PA_RT.M')
    url = f'{BASE}/data/dataflow/IMF.STA/{flow}/+/{key}'
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.7,
                   status_forcelist=[429, 500, 502, 503, 504], respect_retry_after_header=False)))
    try:
        with session:
            response = session.get(url, params={'startPeriod': FIRST},
                                   headers={'Accept': 'text/csv'}, timeout=(10, 45))
            if response.status_code in (204, 404):
                return pd.DataFrame(columns=[kind], index=pd.DatetimeIndex([], name='month')), url, datetime.now(timezone.utc).isoformat()
            response.raise_for_status()
            frame = parse_csv(response.text, kind)
    except requests.RequestException as exc:
        raise DataError(f'{country_name(code)} {flow}: IMF could not be reached ({type(exc).__name__}). Try Refresh data.') from exc
    return frame, url, datetime.now(timezone.utc).isoformat()


def calculate(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Reindex before sampling: missing months remain visible and exportable."""
    out = frame.reindex(pd.date_range(start, end, freq='MS')).copy()
    out.index.name = 'month'
    for col in ['p1', 'p2', 'fx1', 'fx2']:
        out[col] = pd.to_numeric(out[col], errors='coerce')
        out.loc[~np.isfinite(out[col]) | (out[col] <= 0), col] = np.nan
    out['S'] = out['fx2'] / out['fx1']
    out['RER'] = out['S'] * out['p1'] / out['p2']
    out[['S', 'RER']] = out[['S', 'RER']].replace([np.inf, -np.inf], np.nan)
    out['valid_real'] = out['RER'].notna()
    mean = out['RER'].mean()
    out['RER_star'] = mean
    out['real_index'] = 100 * out['RER'] / mean if pd.notna(mean) and mean > 0 else np.nan
    return out


CSS = '''<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Playfair+Display:wght@500;600;700&display=swap');
:root{--ink:#143c4b;--wine:#8b3349;--paper:#fbf8f1}
.stApp{background:var(--paper);color:var(--ink);font-family:'DM Sans',sans-serif}
.block-container{max-width:1240px;padding-top:2rem;padding-bottom:2rem}
h1,h2,h3{color:var(--ink)!important}h1{font-family:'Playfair Display',Georgia,serif!important;font-weight:600!important}
[data-testid="stSidebar"]{background:#eeeade;border-right:1px solid #d9d8cc}
[data-testid="stMetric"]{background:#fffdf8;border:1px solid #dfdfd2;border-radius:12px;padding:19px}
[data-testid="stMetricValue"]{font-family:'Playfair Display',Georgia,serif;color:#143c4b;font-size:2rem}
[data-testid="stMetricLabel"]{color:#56717a}div.stButton>button,div.stDownloadButton>button{border-radius:8px}
div.stDownloadButton>button{background:#8b3349;color:white;border:0}
.hero{position:relative;overflow:hidden;background:#143c4b;color:#fff8e8;border-radius:16px;padding:36px 38px 25px;margin-bottom:24px;min-height:240px}
.hero h1{color:#fff8e8!important;font-size:clamp(2.1rem,3.3vw,3rem);max-width:65%;line-height:1.12;margin:10px 0 12px;padding:0}
.hero p{color:#d5e0dc;max-width:490px;font-size:16px;line-height:1.6}
.eyebrow{font-size:11px;letter-spacing:3px;font-weight:700;text-transform:uppercase;color:#e6bf7b}
.bridge{position:absolute;right:-12px;bottom:0;width:38%;opacity:.85}
.wordmark{font-size:18px;letter-spacing:3px;font-weight:700;margin-bottom:20px}
.pair{font-size:13px;letter-spacing:.5px;background:#e9efe9;border:1px solid #d3dfd7;border-radius:8px;padding:12px 16px;margin:6px 0 22px}
.footer{color:#6c807f;font-size:12px;border-top:1px solid #d8ddd3;padding-top:18px;margin-top:24px}
@media(max-width:750px){.hero{padding:25px;min-height:300px}.hero h1{font-size:2.5rem;max-width:100%}.hero p{max-width:95%;font-size:14px}.bridge{width:65%;opacity:.25}.block-container{padding-top:1rem}}
</style>'''
BRIDGE = '''<svg class="bridge" viewBox="0 0 600 280" xmlns="http://www.w3.org/2000/svg" aria-label="Stylized Dom Luís I Bridge in Porto" role="img"><g fill="none" stroke="#e6bf7b" stroke-width="2"><path d="M0 237 Q150 227 300 240 T600 239 M0 253 Q150 243 300 256 T600 255" opacity=".4"/><path d="M25 100H575 M25 110H575 M50 200H550 M50 210H550"/><path d="M90 200Q300 -45 510 200 M90 210Q300 -26 510 210" stroke-width="5"/><path d="M90 110V218 M510 110V218 M140 110V155 M190 110V113 M240 75V110 M300 53V110 M360 75V110 M410 110V113 M460 110V155"/><path d="M90 110L140 155L190 110L240 75L300 110L360 75L410 110L460 155L510 110" opacity=".7"/></g><g fill="#8caca9" opacity=".65"><path d="M15 99V65H40V48H68V99ZM47 99V70H92V99ZM515 99V53H541V34H567V99ZM565 99V70H592V99Z"/></g></svg>'''


WINE_BOTTLES = '''<svg viewBox="0 0 280 180" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Three Portuguese port-wine bottles against a Porto skyline" style="display:block;width:100%;max-width:280px;margin:4px auto 12px">
<g fill="none" stroke="#c4a367" stroke-width="1.1" opacity=".65"><path d="M12 155H268M16 162Q76 158 140 163T265 162M170 152V99H188V88H205V152M210 152V76H218V45H226V76H234V152M238 152V109H261V152M176 104H199M176 116H199M176 128H199M243 118H256M243 132H256"/></g>
<g transform="translate(37 44)"><path d="M20 0H34V31Q34 38 42 45Q49 51 49 64V110Q49 115 44 115H10Q5 115 5 110V64Q5 51 13 45Q20 38 20 31Z" fill="#254437" stroke="#17382c" stroke-width="1.5"/><path d="M21 2H33V24H21Z" fill="#8b3349"/><path d="M24 6V22M12 68V100" stroke="#ffffff" opacity=".18" stroke-width="3"/><rect x="10" y="67" width="34" height="34" rx="1" fill="#f3e7cb"/><text x="27" y="79" text-anchor="middle" fill="#294539" font-family="Georgia,serif" font-size="8">PORTO</text><text x="27" y="91" text-anchor="middle" fill="#8b3349" font-family="Arial,sans-serif" font-size="5">RUBY</text></g>
<g transform="translate(94 12)"><path d="M22 0H38V41Q38 49 47 57Q55 64 55 78V141Q55 147 49 147H11Q5 147 5 141V78Q5 64 13 57Q22 49 22 41Z" fill="#1b382e" stroke="#102d24" stroke-width="1.5"/><path d="M23 1H37V29H23Z" fill="#793045"/><path d="M26 6V26M13 84V130" stroke="#ffffff" opacity=".18" stroke-width="3"/><rect x="11" y="83" width="38" height="44" rx="1" fill="#f3e7cb"/><circle cx="30" cy="93" r="4" fill="none" stroke="#b89454"/><text x="30" y="107" text-anchor="middle" fill="#294539" font-family="Georgia,serif" font-size="9">PORTO</text><text x="30" y="118" text-anchor="middle" fill="#8b3349" font-family="Arial,sans-serif" font-size="5">RESERVA</text></g>
<g transform="translate(155 48)"><path d="M17 0H31V31Q31 38 38 44Q45 50 45 63V106Q45 111 40 111H8Q3 111 3 106V63Q3 50 10 44Q17 38 17 31Z" fill="#304c37" stroke="#17382c" stroke-width="1.5"/><path d="M18 1H30V25H18Z" fill="#c4a367"/><path d="M21 6V23M10 66V98" stroke="#ffffff" opacity=".18" stroke-width="3"/><rect x="8" y="64" width="32" height="34" rx="1" fill="#f3e7cb"/><text x="24" y="77" text-anchor="middle" fill="#294539" font-family="Georgia,serif" font-size="8">PORTO</text><text x="24" y="89" text-anchor="middle" fill="#8b3349" font-family="Arial,sans-serif" font-size="5">TAWNY</text></g></svg>'''


def main() -> None:
    st.set_page_config(page_title='Ponte | Currency, in perspective', page_icon='🇵🇹', layout='wide')
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown('<div class="wordmark">PONTE <span style="color:#8b3349">↗</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="hero"><div class="eyebrow">The currency observatory · Inspired by Porto</div><h1>Compare nominal and<br>real currency prices.</h1><p>Explore monthly exchange rates and inflation across countries and time.</p>' + BRIDGE + '</div>', unsafe_allow_html=True)
    choices = sorted(set(MEMBERS + ['G163']), key=country_name)
    with st.sidebar:
        st.markdown('### Build your comparison')
        st.caption('Two economies. One clearer perspective.')
        c1 = st.selectbox('Country 1 · base', choices, index=choices.index('PRT'), format_func=country_label)
        c2 = st.selectbox('Country 2 · quote', choices, index=choices.index('USA'), format_func=country_label)
        st.divider()
        st.markdown('**Sample period**')
        months = pd.date_range(FIRST, pd.Timestamp.today().replace(day=1).normalize(), freq='MS').strftime('%Y-%m').tolist()
        start = st.selectbox('Start month', months, index=months.index('2000-01'))
        end = st.selectbox('End month', months, index=len(months)-1)
        st.caption('Available from January 1976. Coverage varies by series.')
        if st.button('↻ Refresh data', use_container_width=True):
            fetch_series.clear()
            st.rerun()
        st.divider()
        st.markdown(WINE_BOTTLES, unsafe_allow_html=True)
        st.markdown('**A little Porto, wherever you are.**')
        st.caption('Azulejo blue. Ruby port. A silhouette inspired by the Dom Luís I Bridge.')
    if start > end:
        st.error('Choose a start month on or before the end month.')
        return
    st.markdown(f'<div class="pair">{html.escape(country_label(c1))} → {html.escape(country_label(c2))} &nbsp; · &nbsp; <b>S = Country 2 currency units per 1 Country 1 currency unit</b></div>', unsafe_allow_html=True)
    jobs = list(dict.fromkeys([(c1, 'cpi'), (c1, 'fx'), (c2, 'cpi'), (c2, 'fx')] + ([('G163', 'fx')] if c1 in EURO_START or c2 in EURO_START else [])))
    results, problems = {}, []
    with st.spinner('Crossing the data bridge… retrieving monthly IMF observations.'):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {job: pool.submit(fetch_series, *job) for job in jobs}
            for job, future in futures.items():
                try:
                    results[job] = future.result()
                except DataError as exc:
                    problems.append(str(exc))
    if problems:
        st.error('The live comparison is temporarily unavailable. No sample data has been substituted.')
        for problem in problems:
            st.caption(problem)
        st.info('Check your internet connection and try Refresh data. IMF outages or rate limits can affect access.')
        return
    frames = []
    for code, kind, col in [(c1,'cpi','p1'),(c2,'cpi','p2'),(c1,'fx','fx1'),(c2,'fx','fx2')]:
        raw = results[(code, kind)][0].copy()
        if kind == 'fx' and code in EURO_START:
            adoption = pd.Timestamp(EURO_START[code])
            euro = results[('G163', 'fx')][0]
            raw = pd.concat([raw.loc[raw.index < adoption], euro.loc[euro.index >= adoption]]).sort_index()
            raw['fx_currency_regime'] = np.where(raw.index >= adoption, 'EUR (G163)', 'national currency')
        frames.append(raw.rename(columns={name: name.replace(kind, col, 1) for name in raw.columns}))
    # Joining separately avoids duplicate columns even when the countries match.
    frame = pd.concat(frames, axis=1)
    sample = calculate(frame, start, end)
    valid = sample.dropna(subset=['real_index'])
    total, count = len(sample), len(valid)
    latest = valid.iloc[-1] if count else None
    a,b,c,d = st.columns(4)
    a.metric('Latest real index', f'{latest.real_index:,.2f}' if count else '—', help='Latest valid real observation inside your selected sample. Sample average = 100.')
    nominal = sample['S'].dropna()
    b.metric('Latest nominal rate', f'{nominal.iloc[-1]:,.5g}' if len(nominal) else '—', help='Actual reported currency units of Country 2 per unit of Country 1. Never normalized.')
    c.metric('Valid real months', f'{count:,} / {total:,}', help='Months with both positive CPIs and both positive exchange rates.')
    d.metric('Sample RER average', f'{sample.RER_star.iloc[0]:,.5g}' if count else '—', help='RER*: arithmetic mean across valid real observations in this sample.')
    st.write('')
    left, right = st.columns([3,2])
    with left:
        st.subheader('Currency, in perspective')
        st.caption(f'{start} to {end} · Monthly observations · IMF')
    with right:
        view = st.radio('Chart series', ['Real', 'Nominal', 'Both'], horizontal=True, index=2)
    fig = make_subplots(specs=[[{'secondary_y': True}]])
    if view in ('Real','Both'):
        fig.add_trace(go.Scatter(x=sample.index,y=sample.real_index,name='Real index',line={'color':'#8b3349','width':2.6},connectgaps=False,hovertemplate='%{y:.2f} index points<extra>Real</extra>'), secondary_y=False)
        fig.add_hline(y=100,line_dash='dot',line_color='#9c9d8b',annotation_text='Sample average · 100',secondary_y=False)
    if view in ('Nominal','Both'):
        fig.add_trace(go.Scatter(x=sample.index,y=sample.S,name='Nominal S',line={'color':'#1b7e8b','width':2.2},connectgaps=False,hovertemplate='%{y:,.6g} quote units / base unit<extra>Nominal</extra>'), secondary_y=view=='Both')
    fig.update_layout(height=440,paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='#fffdf8',font={'family':'DM Sans, Arial','color':'#234957'},margin={'l':20,'r':20,'t':35,'b':20},hovermode='x unified',legend={'orientation':'h','y':1.12,'x':0},dragmode='zoom')
    fig.update_xaxes(showgrid=False,title=None,rangeslider_visible=False)
    fig.update_yaxes(gridcolor='#e7e8df',zeroline=False,title_text='Real index · sample mean = 100' if view!='Nominal' else 'S · Country 2 units / Country 1 unit',secondary_y=False)
    fig.update_yaxes(showgrid=False,visible=view=='Both',title_text='S · Country 2 units / Country 1 unit',secondary_y=True)
    st.plotly_chart(fig,use_container_width=True,config={'displaylogo':False,'scrollZoom':False,'toImageButtonOptions':{'filename':'ponte-currency-chart','scale':2}})
    if not count:
        st.warning('No complete real observations in this period. Try a different date range or country pair. Nominal observations remain available where both exchange rates exist.')
    elif count < total:
        st.info(f'{total-count:,} months lack at least one required input. Gaps are left visible; no interpolation or forward filling is used. Latest real observation: {valid.index[-1]:%B %Y}.')
    else:
        st.caption(f'Latest real observation: {valid.index[-1]:%B %Y}. Drag to zoom; double-click to reset. Chart zoom does not change the sample average: use the month selectors to rebase.')
    for code in dict.fromkeys([c1, c2]):
        if code in EURO_START:
            adoption = EURO_START[code]
            st.caption(f'{country_name(code)}: national exchange rates before {adoption}; IMF Euro area (G163) exchange rates from {adoption}. CPI remains country-specific.')
            if start < adoption <= end:
                st.warning(f'This sample crosses {country_name(code)}’s euro adoption. Nominal units switch to EUR, creating a mechanical break in S and RER. Select a single currency regime for economic interpretation.')
    st.caption('Historical currencies may change units, including at euro adoption or redenomination. National and euro-area sources are joined only at the documented adoption dates. Inspect units below before interpreting long-run nominal movements.')
    export = sample.copy()
    export.insert(0,'country_1',c1)
    export.insert(1,'country_2',c2)
    export['sample_start'], export['sample_end'] = start, end
    export['retrieved_utc'] = max(item[2] for item in results.values())
    export.index = export.index.strftime('%Y-%m')
    st.download_button('↓ Download sample CSV',export.to_csv(index=True,na_rep='').encode('utf-8-sig'),file_name=f'ponte_{c1}_{c2}_{start}_{end}.csv',mime='text/csv',help='Every selected month, including gaps, with raw inputs, metadata, and calculations.')
    tab1,tab2,tab3 = st.tabs(['How to read it','Observations','Sources & coverage'])
    with tab1:
        x,y = st.columns(2)
        with x:
            st.markdown('#### The calculation')
            st.latex(r'S_t = \frac{\mathrm{LCU}_{2,t}/\mathrm{USD}}{\mathrm{LCU}_{1,t}/\mathrm{USD}}')
            st.latex(r'RER_t = S_t\frac{P_{1,t}}{P_{2,t}},\quad RER^* = \frac{1}{N}\sum_{t\in V}RER_t')
            st.latex(r'\mathrm{Real\ Index}_t=100\frac{RER_t}{RER^*}')
        with y:
            st.markdown('#### A tasting note for the numbers')
            st.write('Above 100 means Country 1 has a higher relative price level, after currency conversion, than its own selected-sample average. Under this convention, an increase represents real appreciation of Country 1 relative to Country 2.')
            st.write('This is a comparison over time, not a purchasing-power-parity fair-value estimate. Changing the selected months changes the benchmark. CPI base years affect the level of RER*, but fixed CPI rescaling cancels out of the real index.')
            st.caption('V contains months with all four valid inputs; N is their count. A nominal observation needs only the two exchange rates. If both countries are identical, valid S and the real index equal 1 and 100 respectively.')
    with tab2:
        st.dataframe(export,use_container_width=True)
        st.caption('p1/p2: CPI; fx1/fx2: local currency per USD; S: quote/base; RER_star: selected-sample mean. Blank cells are missing or invalid observations. Status and unit fields are retained when IMF provides them.')
    with tab3:
        st.markdown('[IMF data portal](https://data.imf.org/) · [IMF API documentation](https://data.imf.org/en/Resource-Pages/IMF-API)')
        for (code,kind),(raw,url,stamp) in results.items():
            values = raw[kind].dropna()
            coverage = f'{values.index.min():%Y-%m} to {values.index.max():%Y-%m}' if len(values) else 'No observations returned'
            st.markdown(f'**{country_label(code)} · {kind.upper()}**: {coverage} · {len(values):,} observations  \n[Exact IMF series]({url}) · Retrieved {stamp[:19]} UTC')
            units = raw.get(f'{kind}_unit',pd.Series(dtype=str)).dropna().unique()
            if len(units):
                st.caption('Reported unit codes: ' + ', '.join(map(str,units)))
        st.caption('CPIs: CPI._T.IX.M. Exchange rates: XDC_USD.PA_RT.M (monthly period averages). Euro area code: G163. Requested from 1976-01; filtered locally as well. Cache lifetime: six hours. Source data can be revised. IMF status flags are exported without selectively suppressing provisional observations.')
    st.markdown('<div class="footer">PONTE · A Portuguese perspective on a connected world. &nbsp; | &nbsp; Live IMF data. Thoughtful comparisons.</div>',unsafe_allow_html=True)


if __name__ == '__main__':
    main()
