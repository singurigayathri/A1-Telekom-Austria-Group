"""Daily price download for the A1 benchmark site.
Runs on GitHub Actions every weekday evening and writes data/benchmark_prices.json.
To change which companies are tracked, edit TICKERS below (id must match the website)."""
import json, datetime, sys, time, os
import yfinance as yf

TICKERS = {
    "A1":    "TKA.VI",    # A1 Telekom Austria
    "DTE":   "DTE.DE",    # Deutsche Telekom
    "ORA":   "ORA.PA",    # Orange
    "TEF":   "TEF.MC",    # Telefonica
    "KPN":   "KPN.AS",    # KPN
    "SCMN":  "SCMN.SW",   # Swisscom
    "PROX":  "PROX.BR",   # Proximus
    "TELIA": "TELIA.ST",  # Telia
    "TEL":   "TEL.OL",    # Telenor
}
BENCHMARK = ("BM", "^STOXX", "STOXX Europe 600")

# Symbols we are not sure Yahoo has. They are only tried and reported in the job log
# (not saved to the website), so you can see which spelling works.
PROBE = ["HT.ZA", "HT.ZG", "TLSG.LJ", "TLSG.LJU", "TEL.MK", "MKT.MK", "MPT.MK", "HTO.AT", "MTELEKOM.BD", "MTEL.BD"]

def get(ticker):
    df = None
    for attempt in range(3):                      # Yahoo sometimes fails once, so retry
        try:
            df = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=True)
        except Exception:
            df = None
        if df is not None and not df.empty:
            break
        time.sleep(3)
    if df is None or df.empty:
        raise ValueError("empty")
    out = []
    for idx, row in df.iterrows():
        c = row["Close"]
        if c == c and c > 0:                      # skip NaN / zero
            out.append([idx.strftime("%Y-%m-%d"), round(float(c), 4)])
    if len(out) < 30:
        raise ValueError("fewer than 30 rows")
    return out

def main():
    prices, used, failed = {}, {}, []
    jobs = list(TICKERS.items()) + [(BENCHMARK[0], BENCHMARK[1])]
    for sid, tk in jobs:
        try:
            prices[sid] = get(tk); used[sid] = tk
            print("OK  ", sid, tk, len(prices[sid]), "rows, last", prices[sid][-1])
        except Exception as e:
            failed.append(f"{sid}:{tk}")
            print("FAIL", sid, tk, e, file=sys.stderr)
    print("--- probe of uncertain symbols (not saved) ---")
    for tk in PROBE:
        try:
            rows = get(tk); print("PROBE OK  ", tk, len(rows), "rows, last", rows[-1])
        except Exception as e:
            print("PROBE FAIL", tk, e)
    if len(prices) < 3:
        print("Too few series downloaded, keeping the old file.", file=sys.stderr)
        sys.exit(1)           # job turns red, old data/prices.json stays
    doc = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance via yfinance, adjusted closes, local currency",
        "benchmark_name": BENCHMARK[2],
        "tickers": used,
        "failed": failed,
        "prices": prices,
    }
    os.makedirs("data", exist_ok=True)
    with open("data/benchmark_prices.json", "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("Wrote data/benchmark_prices.json:", len(prices), "series;", "failed:", failed)

if __name__ == "__main__":
    main()
