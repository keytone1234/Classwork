# Ponte

**Compare nominal and real currency prices.** A Python 3.12 / Streamlit app for exploring monthly IMF real and nominal exchange rates, with a Porto-inspired interface: azulejo blue, ruby-port accents, an original inline SVG inspired by the Dom Luís I Bridge, and three illustrated port-wine bottles in the sidebar. Portugal is the default base country; flags accompany every country selector.

## Run

Keep these three files together. With Python 3.12 installed:

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open the local URL printed by Streamlit (usually http://localhost:8501). No API key is configured or required by this app. An internet connection to `api.imf.org` is required. Optional Google Fonts enhance typography; system fonts work offline. All illustration and styling are inside `app.py`; no extra assets are required.

## Publish

For Streamlit hosting, upload these three files to a Git repository, choose `app.py` as the entry point, and select Python 3.12 in the deployment settings. Install dependencies from `requirements.txt`. No separate image assets or secrets are needed. The host must allow outbound HTTPS requests to `api.imf.org`. Run locally first with the commands above.

## Use

1. Choose Country 1 (base) and Country 2 (quote). The selectors include IMF member countries and the Euro area (G163).
2. Choose start and end months. January 1976 is the earliest allowed month. The default sample begins January 2000 and ends in the current month.
3. Select **Real**, **Nominal**, or **Both**. Both uses a labeled left index axis and right currency-unit axis. Hover for values, drag to zoom, double-click to reset, or save a chart image using Plotly's toolbar. Zooming is visual only; change the month selectors to change the statistical sample.
4. Download the selected sample as CSV, or inspect observations, source URLs, coverage, and formulas below the chart.

## Data and calculation

Source: [IMF data portal](https://data.imf.org/) and [IMF API documentation](https://data.imf.org/en/Resource-Pages/IMF-API).

Base URL: `https://api.imf.org/external/sdmx/3.0`

Requests use SDMX 3.0 dataflow routes:

```text
/data/dataflow/IMF.STA/CPI/+/{ISO3}.CPI._T.IX.M
/data/dataflow/IMF.STA/ER/+/{ISO3}.XDC_USD.PA_RT.M
```

`+` requests the current version. The app requests CSV with `Accept: text/csv` and `startPeriod=1976-01`, and additionally filters dates locally because the server can return earlier periods. It handles IMF monthly periods such as `2024-M01` and applies reported `SCALE` multipliers. Source status flags and selected metadata are retained. Responses with duplicate months are rejected rather than averaged arbitrarily.

Let `fx1` and `fx2` be local-currency units per USD, and `p1`, `p2` be the respective CPIs:

```text
S(t)          = fx2(t) / fx1(t)
RER(t)        = S(t) * p1(t) / p2(t)
RER_star      = arithmetic mean of RER over complete valid selected months
real_index(t) = 100 * RER(t) / RER_star
```

S is always Country 2 currency units per one unit of Country 1 currency. It is never normalized. The real index's mean is 100 over valid selected months. Changing either sample boundary recalculates RER_star. Matching countries produce S=1 and real_index=100 wherever the required inputs exist.

An increasing real index represents Country 1 real appreciation under the assignment's convention. Above 100 indicates a higher relative price level than its own sample average, not proof of overvaluation or an estimate of a PPP equilibrium rate.

### Euro adoption and historical units

Live verification found Portugal's national ER series ending in December 1998. From each euro member's adoption month, the app explicitly uses the IMF **G163 euro-area ER series**, retaining that country's CPI. Before adoption it retains the national ER series. This policy applies to the euro adopters in `EURO_START`, including Portugal from January 1999. The UI identifies the substitution and exports the currency regime. It does not fill a missing euro observation with a national rate.

A sample spanning adoption changes actual nominal units (e.g., Portuguese escudos to EUR). This creates a mechanical break in both S and the required RER calculation. The app prominently warns about it rather than silently converting or normalizing historical nominal observations. For a consistent currency interpretation, use a sample entirely before or after adoption. Other countries' redenominations or source breaks can also affect interpretation; no undocumented currency conversions are attempted. Euro-area historical coverage depends on what IMF publishes.

### Gaps, revisions, and failures

- All selected calendar months remain in the chart/export, including completely missing months.
- Missing, nonnumeric, nonfinite, zero, or negative inputs cannot enter calculations. No interpolation or forward filling is performed.
- Real observations need all four inputs. Nominal observations need only the two rates, so nominal coverage can be wider.
- Months lacking real observations are excluded from RER_star. No valid RER yields blank real results and an explanatory message.
- IMF provisional and other status codes are retained, not automatically excluded. CPI reference periods and available metadata are exported.
- Requests have bounded timeouts and retries for rate limits and transient errors. A failed request produces an actionable error, never invented observations or an unlabeled demo.
- Successful series are cached for six hours. **Refresh data** clears the cache. IMF may revise series; retrieval timestamps and resolved structure IDs provide context.
- Country membership and euro adoption mappings are maintained in the code. Membership is not inferred from ISO recognition. An eligible economy can have no observations for these specific keys.

## CSV fields

Every selected month is exported. Columns include `country_1`, `country_2`, `p1`, `p2`, `fx1`, `fx2`, `S`, `RER`, `valid_real`, `RER_star`, `real_index`, sample dates, UTC retrieval time, and available input metadata (status, units, currency regime, reference period, source structure). Blanks represent unavailable or invalid observations; they are never replaced by zero. UTF-8 with BOM supports common spreadsheet applications.

## Validation

The deliverable was checked with live IMF requests, numerical checks for quote direction, sample-specific rebasing, missing months, same-country identity and scale parsing, plus Streamlit app smoke checks. Endpoint availability and future schema changes remain outside the app's control. Dependency ranges support Python 3.12; install them in a clean environment for submission.
