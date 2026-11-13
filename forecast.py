"""Two-year share-price-risk forecast with a random forest (chapter 7 of the story).

Runs on GitHub Actions right after analysis.py and writes data/forecast.json.

What it does
  1. Reuses the price download from analysis.py (same companies, same benchmark).
  2. For every company and every month-end builds features known on that day:
     volatility over the last 3, 6 and 12 months, beta, drawdown, 3 and 12 month return,
     market volatility, and the last published EBITDA margin, capex/sales and
     net debt/EBITDA (from data/panel.csv, with a 90 day publication lag).
  3. Target: the annualised volatility of the NEXT 12 months (horizon 1) and of months
     13 to 24 (horizon 2).
  4. Random forest regression. Walk-forward check: train only on the past, test on the
     last 3 years, compare with the naive rule "volatility stays where it is".
  5. Prediction range = forecast + the 10th and 90th percentile of the test errors.
Nothing here is investment advice. It is an association model on a small data set.
"""
import json, os, sys, datetime, warnings
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
import analysis as A

warnings.filterwarnings("ignore")
OUT = A.OUT
FEATURES = ["vol_3m", "vol_6m", "vol_12m", "beta_12m", "ret_3m", "ret_12m", "mdd_12m",
            "mkt_vol_12m", "rel_vol", "ebitda_m", "capex_s", "lev"]
LABELS = {"vol_3m": "Volatility, last 3 months", "vol_6m": "Volatility, last 6 months",
          "vol_12m": "Volatility, last 12 months", "beta_12m": "Beta vs STOXX Europe 600",
          "ret_3m": "Share return, last 3 months", "ret_12m": "Share return, last 12 months",
          "mdd_12m": "Worst fall from peak, 12 months", "mkt_vol_12m": "Market volatility (STOXX)",
          "rel_vol": "Volatility relative to market", "ebitda_m": "EBITDA margin (last report)",
          "capex_s": "Capex / sales (last report)", "lev": "Net debt / EBITDA (last report)"}
DAY = pd.Timedelta(days=1)
MIN_OBS = 150


def vol_between(r, start, end):
    x = r[(r.index > start) & (r.index <= end)]
    if len(x) < MIN_OBS:
        return np.nan
    return float(x.std(ddof=1) * np.sqrt(252) * 100)


def feats_at(px, r, br, t, fund):
    w = lambda d: r[(r.index > t - d * DAY) & (r.index <= t)]
    r12 = w(365)
    if len(r12) < 200:
        return None
    v3, v6, v12 = (float(w(d).std(ddof=1) * np.sqrt(252) * 100) if len(w(d)) > 30 else np.nan for d in (91, 182, 365))
    b12 = br[(br.index > t - 365 * DAY) & (br.index <= t)]
    j = pd.concat([r12, b12], axis=1, join="inner").dropna()
    beta = float(np.cov(j.iloc[:, 0], j.iloc[:, 1], ddof=1)[0, 1] / j.iloc[:, 1].var(ddof=1)) if len(j) > 100 and j.iloc[:, 1].var() > 0 else np.nan
    mv = float(b12.std(ddof=1) * np.sqrt(252) * 100) if len(b12) > 150 else np.nan
    p = px[(px.index > t - 365 * DAY) & (px.index <= t)]
    mdd = float((p / p.cummax() - 1).min() * 100) if len(p) > 20 else np.nan
    f = dict(vol_3m=v3, vol_6m=v6, vol_12m=v12, beta_12m=beta,
             ret_3m=float(w(91).sum() * 100), ret_12m=float(r12.sum() * 100), mdd_12m=mdd,
             mkt_vol_12m=mv, rel_vol=v12 / mv if mv and mv > 0 else np.nan)
    f.update(fund)
    return f


def fund_lookup(panel, cid, t):
    """Last fiscal year whose report was public on day t (year end + 90 days)."""
    nan = dict(ebitda_m=np.nan, capex_s=np.nan, lev=np.nan)
    if panel is None or panel.empty:
        return nan
    p = panel[panel["company"] == cid].copy()
    if p.empty:
        return nan
    p["fy"] = pd.to_datetime(p["fy_end"])
    p = p[p["fy"] + 90 * DAY <= t].sort_values("fy")
    if p.empty:
        return nan
    row = p.iloc[-1]
    return dict(ebitda_m=row.get("ebitda_m", np.nan), capex_s=row.get("capex_s", np.nan), lev=row.get("lev", np.nan))


def month_ends(idx):
    s = pd.Series(idx, index=idx)
    return list(s.groupby([idx.year, idx.month]).max().values)


def build(prices, bench, panel):
    br = A.logret(bench["close"])
    rows, now = [], []
    for cid, df in prices.items():
        px = df["close"]
        r = A.logret(px)
        last = px.index[-1]
        for t in month_ends(px.index):
            t = pd.Timestamp(t)
            if t < px.index[0] + 400 * DAY:
                continue
            f = feats_at(px, r, br, t, fund_lookup(panel, cid, t))
            if f is None:
                continue
            f.update(company=cid, t=t,
                     y1=vol_between(r, t, t + 365 * DAY) if t + 365 * DAY <= last else np.nan,
                     y2=vol_between(r, t + 365 * DAY, t + 730 * DAY) if t + 730 * DAY <= last else np.nan)
            rows.append(f)
        f = feats_at(px, r, br, last, fund_lookup(panel, cid, last))
        if f is not None:
            f.update(company=cid, t=last)
            now.append(f)
    return pd.DataFrame(rows), pd.DataFrame(now)


def rf():
    return RandomForestRegressor(n_estimators=300, min_samples_leaf=8, max_features=0.5,
                                 random_state=0, n_jobs=-1)


def rmse(a, b):
    return float(np.sqrt(np.mean((np.asarray(a) - np.asarray(b)) ** 2)))


def run_horizon(data, now, ycol, horizon_days, test_months=36):
    d = data.dropna(subset=[ycol, "vol_12m"]).copy()
    if len(d) < 150:
        return None
    cutoff = d["t"].max() - pd.Timedelta(days=int(test_months * 30.4))
    train = d[d["t"] + horizon_days * DAY <= cutoff]      # target window ends before the test starts
    test = d[d["t"] > cutoff]
    val = None
    resid_q = None
    if len(train) >= 100 and len(test) >= 30:
        m = rf().fit(train[FEATURES], train[ycol])
        pr = m.predict(test[FEATURES])
        naive = test["vol_12m"].values
        err = test[ycol].values - pr
        resid_q = (float(np.nanpercentile(err, 10)), float(np.nanpercentile(err, 90)))
        val = dict(n_train=int(len(train)), n_test=int(len(test)),
                   test_from=test["t"].min().strftime("%Y-%m"), test_to=test["t"].max().strftime("%Y-%m"),
                   rmse_forest=round(rmse(test[ycol], pr), 2), rmse_naive=round(rmse(test[ycol], naive), 2),
                   mae_forest=round(float(np.mean(np.abs(err))), 2),
                   mae_naive=round(float(np.mean(np.abs(test[ycol].values - naive))), 2))
        val["beats_naive"] = bool(val["rmse_forest"] < val["rmse_naive"])
    final = rf().fit(d[FEATURES], d[ycol])
    pred = final.predict(now[FEATURES])
    if resid_q is None:
        s = float(np.nanstd(d[ycol] - d["vol_12m"]))
        resid_q = (-1.28 * s, 1.28 * s)
    imp = sorted(zip(FEATURES, final.feature_importances_), key=lambda x: -x[1])
    return pred, resid_q, val, [dict(f=k, label=LABELS[k], v=round(float(v) * 100, 1)) for k, v in imp]


def main():
    os.makedirs(OUT, exist_ok=True)
    print("Downloading benchmark", A.BENCH_TICKER)
    bench = A.get_prices(A.BENCH_TICKER)
    prices = {}
    for cid, (tk, name, cty) in A.UNIVERSE.items():
        try:
            prices[cid] = A.get_prices(tk); print("OK ", cid, tk, len(prices[cid]))
        except Exception as e:
            print("FAIL", cid, tk, e, file=sys.stderr)
    prices = {k: v for k, v in prices.items() if "example" not in A.UNIVERSE[k][1].lower()}
    if len(prices) < 5:
        print("Too few companies, keeping old forecast.", file=sys.stderr); sys.exit(1)
    panel = None
    try:
        panel = pd.read_csv(f"{OUT}/panel.csv")
    except Exception:
        print("No panel.csv: model runs on price features only")
    data, now = build(prices, bench, panel)
    if data.empty or now.empty:
        print("No features built.", file=sys.stderr); sys.exit(1)

    res = {}
    for key, ycol, hd in (("h1", "y1", 365), ("h2", "y2", 730)):
        out = run_horizon(data, now, ycol, hd)
        if out:
            res[key] = out
    if "h1" not in res:
        print("Not enough history for a forecast.", file=sys.stderr); sys.exit(1)

    companies = {}
    for i, row in now.reset_index(drop=True).iterrows():
        cid = row["company"]
        c = dict(name=A.UNIVERSE[cid][1], country=A.UNIVERSE[cid][2], current=round(float(row["vol_12m"]), 1),
                 as_of=row["t"].strftime("%Y-%m-%d"))
        for key in ("h1", "h2"):
            if key in res:
                pred, (q10, q90), _, _ = res[key]
                p = float(pred[i])
                c[key] = dict(pred=round(p, 1), lo=round(max(p + q10, 3.0), 1), hi=round(p + q90, 1))
        companies[cid] = c
    # monthly history of 12-month volatility, last 48 months, for the chart
    hist = {}
    for cid, g in data.groupby("company"):
        g = g.sort_values("t").tail(48)
        hist[cid] = [[t.strftime("%Y-%m"), round(float(v), 1)] for t, v in zip(g["t"], g["vol_12m"]) if v == v]
    others = [v["h1"]["pred"] for k, v in companies.items() if k != "A1" and "h1" in v]
    doc = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": "Random forest regression (scikit-learn), 300 trees, walk-forward check",
        "target": "Annualised volatility of daily share returns, %",
        "horizons": {"h1": "next 12 months", "h2": "months 13 to 24"},
        "validation": {k: res[k][2] for k in res},
        "importance": res["h1"][3],
        "companies": companies, "history": hist,
        "peer_median_h1": round(float(np.median(others)), 1) if others else None,
        "training_rows": int(len(data)), "companies_used": int(len(prices)),
        "uses_fundamentals": bool(panel is not None and not panel.empty),
        "notes": ["Overlapping monthly windows: neighbouring rows are not independent, so the test is indicative.",
                  "Ranges come from the forest's errors on the last 3 years, not from a statistical guarantee.",
                  "Fundamentals enter only as the last published margin, capex/sales and leverage."]}
    with open(f"{OUT}/forecast.json", "w") as f:
        json.dump(doc, f, separators=(",", ":"), default=lambda o: None)
    print("Wrote forecast.json:", len(companies), "companies;", {k: res[k][2] for k in res})


if __name__ == "__main__":
    main()
