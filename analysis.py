"""Thesis analysis for the A1 benchmark site.

Runs on GitHub Actions (weekly, or by hand) and writes:
  data/thesis_results.json   everything the website's Thesis tab shows
  data/panel.csv             the company-year dataset behind the regressions

What it does
  1. Downloads up to 10 years of daily prices (price and total return), FX rates,
     and annual financial statements from Yahoo Finance via yfinance.
  2. Builds a company-year panel: realised volatility over each fiscal year,
     plus EBITDA margin, capex/sales and net debt/EBITDA of that year.
  3. Panel regressions of volatility on fundamentals (pooled, year effects,
     company + year effects, with beta control), cluster-robust by company.
  4. Robustness: total return, EUR-converted returns, lagged fundamentals, without A1.
  5. GARCH(1,1) for every company.
  6. Event study (market model, cumulative abnormal returns) for events.csv
     plus earnings dates where Yahoo has them.

Edit UNIVERSE to change the companies. Edit events.csv to change the events.
"""
import json, os, sys, datetime, warnings
import numpy as np
import pandas as pd
import yfinance as yf
import statsmodels.formula.api as smf
from arch import arch_model

warnings.filterwarnings("ignore")

# id -> (Yahoo ticker, name, country). Companies Yahoo does not return are skipped and listed.
UNIVERSE = {
    "A1":    ("TKA.VI",     "A1 Telekom Austria",  "Austria"),
    "DTE":   ("DTE.DE",     "Deutsche Telekom",    "Germany"),
    "ORA":   ("ORA.PA",     "Orange",              "France"),
    "TEF":   ("TEF.MC",     "Telefonica",          "Spain"),
    "KPN":   ("KPN.AS",     "KPN",                 "Netherlands"),
    "SCMN":  ("SCMN.SW",    "Swisscom",            "Switzerland"),
    "PROX":  ("PROX.BR",    "Proximus",            "Belgium"),
    "TELIA": ("TELIA.ST",   "Telia Company",       "Sweden"),
    "TEL":   ("TEL.OL",     "Telenor",             "Norway"),
    "MTEL":  ("MTELEKOM.BD","Magyar Telekom",      "Hungary"),
    "OPL":   ("OPL.WA",     "Orange Polska",       "Poland"),
    "OTE":   ("HTO.AT",     "OTE (Hellenic Telecom)", "Greece"),
    "VOD":   ("VOD.L",      "Vodafone",            "UK"),
    "ELISA": ("ELISA.HE",   "Elisa",               "Finland"),
    "TELE2": ("TEL2-B.ST",  "Tele2",               "Sweden"),
    "HT":    ("HT.ZA",      "Hrvatski Telekom",    "Croatia"),
    "TLSG":  ("TLSG.LJ",    "Telekom Slovenije",   "Slovenia"),
}
BENCH_TICKER = "^STOXX"
# Yahoo FX symbol that gives units of local currency per 1 EUR, and the price unit divisor
FX = {".SW": ("EURCHF=X", 1), ".ST": ("EURSEK=X", 1), ".OL": ("EURNOK=X", 1),
      ".WA": ("EURPLN=X", 1), ".L": ("EURGBP=X", 100), ".BD": ("EURHUF=X", 1)}

PERIOD = "10y"
GARCH_YEARS = 5
MIN_OBS_YEAR = 200          # trading days needed in a fiscal-year window
EST_WINDOW = 250            # event study estimation window (trading days)
EST_GAP = 30                # gap between estimation window and event
OUT = "data"


# ----------------------------------------------------------------------------- helpers
def clean_index(df):
    df = df.copy()
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def get_prices(ticker):
    df = yf.Ticker(ticker).history(period=PERIOD, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise ValueError("no price data")
    df = clean_index(df)
    close = df["Close"].astype(float)
    adj = df["Adj Close"].astype(float) if "Adj Close" in df.columns else close
    out = pd.DataFrame({"close": close, "adj": adj}).dropna()
    out = out[(out["close"] > 0) & (out["adj"] > 0)]
    if len(out) < 500:
        raise ValueError(f"only {len(out)} rows")
    return out


def logret(s):
    return np.log(s).diff().dropna()


def pick(df, names):
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def get_fundamentals(ticker):
    tk = yf.Ticker(ticker)
    inc, cf, bs = tk.income_stmt, tk.cashflow, tk.balance_sheet
    rev = pick(inc, ["Total Revenue", "Operating Revenue"])
    ebit = pick(inc, ["Operating Income", "EBIT"])
    ebitda_row = pick(inc, ["EBITDA", "Normalized EBITDA"])
    da = pick(cf, ["Depreciation And Amortization", "Depreciation Amortization Depletion",
                   "Depreciation & Amortization"])
    capex = pick(cf, ["Capital Expenditure", "Purchase Of PPE"])
    debt = pick(bs, ["Total Debt"])
    cash = pick(bs, ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"])
    nd_row = pick(bs, ["Net Debt"])
    if rev is None:
        raise ValueError("no revenue")
    f = pd.DataFrame({"rev": rev})
    if ebit is not None and da is not None:
        f["ebitda"] = ebit.astype(float) + da.astype(float)       # same definition as the dashboard
    elif ebitda_row is not None:
        f["ebitda"] = ebitda_row.astype(float)
    if ebit is not None:
        f["ebit"] = ebit.astype(float)
    if capex is not None:
        f["capex"] = capex.astype(float).abs()
    if debt is not None and cash is not None:
        f["nd"] = debt.astype(float) - cash.astype(float)
    elif nd_row is not None:
        f["nd"] = nd_row.astype(float)
    f.index = pd.to_datetime(f.index)
    f = f.sort_index().astype(float)
    for c in ("ebitda", "capex", "nd"):
        if c not in f.columns:
            f[c] = np.nan
    f["ebitda_m"] = f["ebitda"] / f["rev"] * 100
    f["capex_s"] = f["capex"] / f["rev"] * 100
    f["lev"] = f["nd"] / f["ebitda"]
    return f


def window_stats(ret, bench_ret, end, days=365):
    """Annualised vol (%) of log returns over the 12 months ending at `end`, and beta vs benchmark."""
    start = end - pd.Timedelta(days=days)
    r = ret[(ret.index > start) & (ret.index <= end)]
    if len(r) < MIN_OBS_YEAR:
        return None, None, len(r)
    vol = float(r.std(ddof=1) * np.sqrt(252) * 100)
    b = bench_ret[(bench_ret.index > start) & (bench_ret.index <= end)]
    j = pd.concat([r, b], axis=1, join="inner").dropna()
    beta = None
    if len(j) >= 100 and j.iloc[:, 1].var() > 0:
        beta = float(np.cov(j.iloc[:, 0], j.iloc[:, 1], ddof=1)[0, 1] / j.iloc[:, 1].var(ddof=1))
    return vol, beta, len(r)


# ----------------------------------------------------------------------------- panel
def build_panel(prices, fundamentals, fx_rates, bench):
    bret = logret(bench["adj"])
    rows = []
    for cid, px in prices.items():
        if cid not in fundamentals:
            continue
        tk = UNIVERSE[cid][0]
        r_price = logret(px["close"])
        r_tr = logret(px["adj"])
        # EUR-converted total return
        r_eur = None
        for suf, (fxs, div) in FX.items():
            if tk.endswith(suf) and fxs in fx_rates:
                fxr = fx_rates[fxs].reindex(px.index).ffill()
                eur_price = px["adj"] / div / fxr
                r_eur = logret(eur_price.dropna())
                break
        if r_eur is None:
            r_eur = r_tr
        f = fundamentals[cid]
        for end, row in f.iterrows():
            vol, beta, n = window_stats(r_price, bret, end)
            if vol is None:
                continue
            vol_tr, _, _ = window_stats(r_tr, bret, end)
            vol_eur, _, _ = window_stats(r_eur, bret, end)
            rows.append(dict(company=cid, year=int(end.year), fy_end=end.strftime("%Y-%m-%d"),
                             vol=vol, vol_tr=vol_tr, vol_eur=vol_eur, beta=beta, days=n,
                             ebitda_m=row["ebitda_m"], capex_s=row["capex_s"], lev=row["lev"],
                             rev=row["rev"]))
    panel = pd.DataFrame(rows)
    if panel.empty:
        return panel
    panel = panel.sort_values(["company", "year"]).reset_index(drop=True)
    for c in ("ebitda_m", "capex_s", "lev"):
        panel[c + "_lag"] = panel.groupby("company")[c].shift(1)
        # lag only valid if the previous row is exactly one year earlier
        prev_year = panel.groupby("company")["year"].shift(1)
        panel.loc[panel["year"] - prev_year != 1, c + "_lag"] = np.nan
    return panel


def usable(panel):
    p = panel.dropna(subset=["vol", "ebitda_m", "capex_s", "lev"]).copy()
    p = p[(p["rev"] > 0) & (p["ebitda_m"] > 0) & (p["ebitda_m"] < 80) & (p["lev"] > -2) & (p["lev"] < 15)]
    return p


def fit(df, y, xs, year_fe, comp_fe, extra=None, label=""):
    xs = list(xs) + (extra or [])
    d = df.dropna(subset=[y] + xs).copy()
    if len(d) < 12 or d["company"].nunique() < 4:
        return {"label": label, "ok": False, "reason": f"too few observations ({len(d)})"}
    formula = f"{y} ~ " + " + ".join(xs)
    if year_fe:
        formula += " + C(year)"
    if comp_fe:
        formula += " + C(company)"
    try:
        groups = pd.factorize(d["company"])[0]
        m = smf.ols(formula, data=d).fit(cov_type="cluster", cov_kwds={"groups": groups})
    except Exception as e:
        return {"label": label, "ok": False, "reason": str(e)[:120]}
    coefs = {}
    for x in xs:
        coefs[x] = {"b": float(m.params[x]), "se": float(m.bse[x]),
                    "t": float(m.tvalues[x]), "p": float(m.pvalues[x])}
    return {"label": label, "ok": True, "n": int(len(d)), "companies": int(d["company"].nunique()),
            "r2": float(m.rsquared), "coefs": coefs,
            "year_fe": bool(year_fe), "company_fe": bool(comp_fe)}


def regressions(panel):
    p = usable(panel)
    X = ["ebitda_m", "capex_s", "lev"]
    models = [
        fit(p, "vol", X, False, False, label="M1 Pooled"),
        fit(p, "vol", X, True, False, label="M2 + year effects"),
        fit(p, "vol", X, True, True, label="M3 + company and year effects"),
        fit(p, "vol", X, True, True, extra=["beta"], label="M4 = M3 + market beta"),
    ]
    rob = [
        fit(p, "vol_tr", X, True, True, label="Total return (dividends included)"),
        fit(p, "vol_eur", X, True, True, label="EUR-converted returns"),
        fit(p, "vol", [x + "_lag" for x in X], True, True, label="Lagged fundamentals (t-1)"),
        fit(p[p["company"] != "A1"], "vol", X, True, True, label="Without A1"),
        fit(p[p["year"] <= 2022], "vol", X, True, False, label="Years up to 2022"),
        fit(p[p["year"] >= 2023], "vol", X, True, False, label="Years from 2023"),
    ]
    return models, rob, p


# ----------------------------------------------------------------------------- GARCH
def garch_all(prices):
    out, series = [], {}
    for cid, px in prices.items():
        r = logret(px["close"]) * 100
        r = r[r.index >= r.index[-1] - pd.Timedelta(days=365 * GARCH_YEARS)]
        if len(r) < 500:
            continue
        try:
            res = arch_model(r, mean="Constant", vol="GARCH", p=1, q=1, dist="normal").fit(disp="off")
            om, al, be = (float(res.params["omega"]), float(res.params["alpha[1]"]), float(res.params["beta[1]"]))
            pers = al + be
            lr = float(np.sqrt(om / (1 - pers)) * np.sqrt(252)) if pers < 1 else None
            cv = res.conditional_volatility * np.sqrt(252)
            out.append({"id": cid, "n": int(len(r)), "omega": om, "alpha": al, "beta": be,
                        "persistence": float(pers), "long_run_vol": lr,
                        "last_cond_vol": float(cv.iloc[-1]), "loglik": float(res.loglikelihood)})
            tail = cv.iloc[-252:]
            series[cid] = [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in tail.items()]
        except Exception as e:
            print("GARCH FAIL", cid, str(e)[:80], file=sys.stderr)
    return out, series


# ----------------------------------------------------------------------------- events
def read_events(prices):
    ev = []
    if os.path.exists("events.csv"):
        df = pd.read_csv("events.csv", comment="#")
        for _, r in df.iterrows():
            try:
                ev.append((str(r["id"]).strip(), pd.Timestamp(r["date"]), str(r["label"]).strip()))
            except Exception:
                pass
    # earnings dates from Yahoo where available (best effort)
    for cid in prices:
        try:
            e = yf.Ticker(UNIVERSE[cid][0]).get_earnings_dates(limit=40)
            if e is not None and len(e):
                for d in e.index:
                    d = pd.Timestamp(d).tz_localize(None) if getattr(d, "tzinfo", None) else pd.Timestamp(d)
                    if d <= pd.Timestamp.today():
                        ev.append((cid, d.normalize(), "Earnings"))
        except Exception:
            pass
    return ev


def event_study(prices, bench, events):
    mret = logret(bench["adj"])
    res = []
    for cid, date, label in events:
        if cid not in prices:
            continue
        r = logret(prices[cid]["adj"])
        j = pd.concat([r.rename("r"), mret.rename("m")], axis=1, join="inner").dropna()
        pos = j.index.searchsorted(date)                 # event day = first trading day on/after date
        if pos >= len(j) or pos < EST_WINDOW + EST_GAP + 5:
            continue
        est = j.iloc[pos - EST_GAP - EST_WINDOW: pos - EST_GAP]
        if len(est) < 100:
            continue
        X = np.column_stack([np.ones(len(est)), est["m"].values])
        coef, *_ = np.linalg.lstsq(X, est["r"].values, rcond=None)
        resid = est["r"].values - X @ coef
        sig = float(resid.std(ddof=2))
        def car(a, b):
            w = j.iloc[pos + a: pos + b + 1]
            if len(w) != (b - a + 1):
                return None, None
            ar = w["r"].values - (coef[0] + coef[1] * w["m"].values)
            c = float(ar.sum()); t = c / (sig * np.sqrt(len(w)))
            return c * 100, float(t)
        c1, t1 = car(-1, 1)
        c5, t5 = car(-5, 5)
        if c1 is None:
            continue
        res.append({"id": cid, "date": date.strftime("%Y-%m-%d"), "label": label,
                    "car_1_1": c1, "t_1_1": t1, "car_5_5": c5, "t_5_5": t5})
    # aggregate by label
    agg = []
    df = pd.DataFrame(res)
    if len(df):
        df["grp"] = np.where(df["label"] == "Earnings", "Earnings", "Other events")
        for g, d in df.groupby("grp"):
            for col, lab in (("car_1_1", "[-1,+1]"), ("car_5_5", "[-5,+5]")):
                v = d[col].dropna()
                if len(v) >= 2:
                    se = v.std(ddof=1) / np.sqrt(len(v))
                    agg.append({"group": g, "window": lab, "n": int(len(v)), "mean_car": float(v.mean()),
                                "t": float(v.mean() / se) if se > 0 else None})
    return res, agg


# ----------------------------------------------------------------------------- main
def main():
    os.makedirs(OUT, exist_ok=True)
    failed = []
    print("Downloading benchmark", BENCH_TICKER)
    bench = get_prices(BENCH_TICKER)
    prices, fundamentals = {}, {}
    for cid, (tk, name, cty) in UNIVERSE.items():
        try:
            prices[cid] = get_prices(tk)
            print("OK  prices", cid, tk, len(prices[cid]))
        except Exception as e:
            failed.append(f"{cid}:{tk} prices ({str(e)[:40]})"); print("FAIL prices", cid, tk, e, file=sys.stderr); continue
        try:
            fundamentals[cid] = get_fundamentals(tk)
            print("OK  fundamentals", cid, len(fundamentals[cid]), "years")
        except Exception as e:
            failed.append(f"{cid}:{tk} fundamentals ({str(e)[:40]})"); print("FAIL fundamentals", cid, e, file=sys.stderr)
    if len(prices) < 5:
        print("Too few companies, keeping old files.", file=sys.stderr); sys.exit(1)

    fx_rates = {}
    for fxs in sorted({v[0] for v in FX.values()}):
        try:
            h = clean_index(yf.Ticker(fxs).history(period=PERIOD, interval="1d"))
            fx_rates[fxs] = h["Close"].astype(float).dropna()
        except Exception as e:
            print("FAIL fx", fxs, e, file=sys.stderr)

    panel = build_panel(prices, fundamentals, fx_rates, bench)
    if panel.empty:
        models, rob, used = [], [], panel
    else:
        models, rob, used = regressions(panel)
        panel.to_csv(f"{OUT}/panel.csv", index=False, float_format="%.4f")
    garch, garch_series = garch_all(prices)
    events = read_events(prices)
    ev_rows, ev_agg = event_study(prices, bench, events)

    doc = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance via yfinance",
        "universe": [{"id": c, "ticker": UNIVERSE[c][0], "name": UNIVERSE[c][1], "country": UNIVERSE[c][2],
                      "price_days": int(len(prices[c])), "fund_years": int(len(fundamentals.get(c, [])))}
                     for c in prices],
        "failed": failed,
        "panel": {"rows": int(len(panel)), "usable_rows": int(len(used)),
                  "companies": int(used["company"].nunique()) if len(used) else 0,
                  "years": [int(used["year"].min()), int(used["year"].max())] if len(used) else None},
        "models": models, "robustness": rob, "garch": garch, "garch_series": garch_series,
        "events": ev_rows, "event_summary": ev_agg,
    }
    with open(f"{OUT}/thesis_results.json", "w") as f:
        json.dump(doc, f, separators=(",", ":"), default=lambda o: None)
    print("Wrote thesis_results.json:", len(prices), "companies,", len(panel), "panel rows,",
          len(garch), "GARCH fits,", len(ev_rows), "events. Failed:", failed)


if __name__ == "__main__":
    main()
