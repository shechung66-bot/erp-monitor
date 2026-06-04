#!/usr/bin/env python3
"""
ERP Engine — data fetcher + scorer + alerter
--------------------------------------------
Pulls free macro series, scores them against the framework thresholds (identical
logic to the dashboard), writes data.json for the front-end, and pings a webhook
when any indicator's traffic light flips vs the previous run.

Run locally:   FRED_API_KEY=xxxx python fetch.py
Or via GitHub Actions cron (see .github/workflows/refresh.yml).

Env vars:
  FRED_API_KEY  (required)  free key: https://fred.stlouisfed.org/docs/api/api_key.html
  WEBHOOK_URL   (optional)  Slack or Discord incoming webhook for alerts
  YF_MOVE       (optional)  set "1" to attempt MOVE via Yahoo (best-effort, may fail in CI)
"""
import os, json, urllib.request, urllib.parse, datetime, math, sys

FRED_KEY = os.environ.get("FRED_API_KEY", "")
WEBHOOK  = os.environ.get("WEBHOOK_URL", "")
TRY_MOVE = os.environ.get("YF_MOVE", "") == "1"
OUT = "data.json"

# ---- thresholds: must match MODEL in the dashboard (id: polarity, t1, t2) ----
IND = {
    "net_liq":  ("hi", -50, 60),   "supply":   ("lo", 40, 65),
    "fraois":   ("lo", 15, 25),    "rrp":      ("hi", 120, 500),
    "tsyliq":   ("lo", 40, 70),    "slr":      ("hi", 30, 55),
    "move":     ("lo", 90, 120),   "odte":     ("lo", 45, 58),
    "realy":    ("lo", 1.4, 2.3),  "bei":      ("lo", 2.3, 2.6),
    "tp":       ("lo", 0.6, 1.3),  "indirect": ("hi", 60, 70),
    "slope":    ("lo", 25, 50),    "corr":     ("lo", -10, 25),
}
LABEL = {
    "net_liq":"净流动性4周变化","supply":"久期供给压力","fraois":"FRA-OIS",
    "rrp":"RRP余额","tsyliq":"美债流动性指数","slr":"SLR余量","move":"MOVE",
    "odte":"0DTE占比","realy":"10Y实际利率","bei":"盈亏平衡BEI","tp":"期限溢价",
    "indirect":"间接投标","slope":"10Y-2Y熊陡","corr":"股债相关性",
}
TIER1 = ["net_liq","supply","fraois","rrp","tsyliq","slr","move","odte"]
TIER2 = ["realy","bei","tp","indirect","slope","corr"]

def score(idd, v):
    pol, t1, t2 = IND[idd]
    if pol == "lo":  return 2 if v < t1 else 1 if v < t2 else 0
    return 2 if v > t2 else 1 if v > t1 else 0
def light(s): return "green" if s >= 2 else "amber" if s >= 1 else "red"

# ---------------- HTTP helpers ----------------
def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "erp-engine/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fred(series_id, n=1):
    if not FRED_KEY:
        return []
    q = urllib.parse.urlencode({"series_id": series_id, "api_key": FRED_KEY,
                                "file_type": "json", "sort_order": "desc", "limit": n})
    try:
        obs = http_json(f"https://api.stlouisfed.org/fred/series/observations?{q}").get("observations", [])
        return [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "")]
    except Exception as e:
        print(f"[warn] FRED {series_id}: {e}", file=sys.stderr)
        return []

def fred_latest(series_id):
    v = fred(series_id, 1)
    return v[0][1] if v else None

def pearson(a, b):
    n = len(a)
    if n < 3: return None
    ma, mb = sum(a)/n, sum(b)/n
    cov = sum((x-ma)*(y-mb) for x, y in zip(a, b))
    va = math.sqrt(sum((x-ma)**2 for x in a)); vb = math.sqrt(sum((y-mb)**2 for y in b))
    return cov/(va*vb) if va and vb else None

# ---------------- indicator builders ----------------
def get_net_liq_4wk():
    walcl = fred("WALCL", 8)            # weekly, $millions
    rrp   = fred("RRPONTSYD", 40)       # daily, $billions
    tga   = fred("WTREGEN", 40)         # weekly avg, $millions
    if not (walcl and rrp and tga): return None
    def net(w, r, t): return w/1000.0 - t/1000.0 - r   # all -> $B
    now  = net(walcl[0][1], rrp[0][1], tga[0][1])
    wi   = min(3, len(walcl)-1); ri = min(20, len(rrp)-1); ti = min(3, len(tga)-1)
    prev = net(walcl[wi][1], rrp[ri][1], tga[ti][1])
    return round(now - prev, 1)

def get_stock_bond_corr(n=30):
    sp = dict(fred("SP500", 80)); y = dict(fred("DGS10", 80))
    dates = sorted(set(sp) & set(y))
    if len(dates) < n+2: return None
    dates = dates[-(n+1):]
    spr, bnd = [], []
    for i in range(1, len(dates)):
        d0, d1 = dates[i-1], dates[i]
        if sp[d0] == 0: continue
        spr.append((sp[d1]-sp[d0])/sp[d0])
        bnd.append(-(y[d1]-y[d0]))            # bond return proxy = -Δyield
    c = pearson(spr, bnd)
    return round(c*100, 0) if c is not None else None

def get_move_yahoo():
    if not TRY_MOVE: return None
    try:
        u = "https://query1.finance.yahoo.com/v8/finance/chart/%5EMOVE?interval=1d&range=5d"
        j = http_json(u)
        return round(j["chart"]["result"][0]["meta"]["regularMarketPrice"], 0)
    except Exception as e:
        print(f"[warn] Yahoo MOVE: {e}", file=sys.stderr); return None

def get_indirect_pct():
    """Best-effort: most recent 10-Year Note auction, indirect bidder accepted %."""
    try:
        base = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
        ep = "/v1/accounting/od/auctions_query"
        q = "?filter=security_type:eq:Note,security_term:eq:10-Year&sort=-auction_date&page[size]=1&format=json"
        rec = http_json(base+ep+q).get("data", [])
        if not rec: return None
        r = rec[0]
        for k, v in r.items():                # find an 'indirect ... pct' field defensively
            if "indirect" in k.lower() and "pct" in k.lower() and v not in (None, "", "null"):
                return round(float(v), 0)
        return None
    except Exception as e:
        print(f"[warn] Treasury auctions: {e}", file=sys.stderr); return None

# ---------------- assemble ----------------
def collect():
    vals, src = {}, {}
    def put(idd, v, source):
        if v is not None: vals[idd] = v; src[idd] = source

    put("net_liq", get_net_liq_4wk(), "FRED WALCL-TGA-RRP")
    put("rrp",     fred_latest("RRPONTSYD"), "FRED RRPONTSYD")
    put("realy",   fred_latest("DFII10"), "FRED DFII10")
    put("bei",     fred_latest("T10YIE"), "FRED T10YIE")
    put("tp",      fred_latest("THREEFYTP10"), "FRED THREEFYTP10 (KW)")
    d10, d2 = fred_latest("DGS10"), fred_latest("DGS2")
    if d10 is not None and d2 is not None:
        put("slope", round((d10-d2)*100, 0), "FRED DGS10-DGS2")
    put("corr",    get_stock_bond_corr(), "FRED SP500/DGS10 30d")
    put("move",    get_move_yahoo(), "Yahoo ^MOVE")
    put("indirect", get_indirect_pct(), "Treasury auctions")
    # context (not scored): VIX + 10Y level for the valuation engine
    ctx = {"vix": fred_latest("VIXCLS"), "y10": d10}
    return vals, src, ctx

def health(ids, vals):
    sc = [score(i, vals[i]) for i in ids if i in vals]
    return round(sum(sc)/len(sc)/2*100) if sc else None

def main():
    vals, src, ctx = collect()
    h1 = health(TIER1, vals); h2 = health(TIER2, vals)
    imp_erp = round(2.8 - (h1/100)*2.7, 2) if h1 is not None else None
    imp_10y = round(5.0 - (h2/100)*1.4, 2) if h2 is not None else None

    lights = {i: light(score(i, v)) for i, v in vals.items()}
    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "values": vals, "lights": lights, "sources": src, "context": ctx,
        "tier_health": {"one": h1, "two": h2},
        "implied": {"erp": imp_erp, "y10": imp_10y},
    }

    # ---- diff vs previous run for alerts ----
    prev = {}
    if os.path.exists(OUT):
        try: prev = json.load(open(OUT)).get("lights", {})
        except Exception: pass
    rank = {"green": 0, "amber": 1, "red": 2}
    flips = [(i, prev.get(i), lights[i]) for i in lights
             if i in prev and rank[lights[i]] > rank[prev[i]]]   # worsened only
    reds = [i for i in lights if lights[i] == "red"]

    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=2)
    print(f"wrote {OUT}: 一阶={h1} 二阶={h2} 隐含ERP={imp_erp}% 隐含10Y={imp_10y}% "
          f"reds={[LABEL[i] for i in reds]}")

    if WEBHOOK and flips:
        lines = [f"⚠️ ERP 监控 · 灯色恶化 ({out['generated_at']})"]
        for i, a, b in flips:
            lines.append(f"• {LABEL[i]}: {a}→{b}  (现值 {vals[i]})")
        lines.append(f"一阶健康度 {h1} / 二阶 {h2} · 框架隐含 ERP {imp_erp}% · 公允10Y {imp_10y}%")
        msg = "\n".join(lines)
        body = json.dumps({"text": msg, "content": msg}).encode()   # Slack(text)/Discord(content)
        try:
            urllib.request.urlopen(urllib.request.Request(
                WEBHOOK, data=body, headers={"Content-Type": "application/json"}), timeout=20)
            print("alert sent")
        except Exception as e:
            print(f"[warn] webhook: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
