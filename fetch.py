#!/usr/bin/env python3
"""
ERP Engine — data fetcher + scorer + alerter  (Phase A: + liquidity layers & history)
-------------------------------------------------------------------------------------
- Keeps data.values EXACTLY as before, so the ERP engine (index.html) is unaffected.
- Adds data.liquidity: a 3-layer liquidity stress score (0-10) + confirmation conditions,
  built from FREE FRED series (SOFR/IORB, HY/IG OAS, NFCI, reserves, net liquidity).
- Appends a daily snapshot into data.history (inside data.json) -> 30-day trend.
- Still pings WEBHOOK_URL when an engine indicator's light worsens.

Env: FRED_API_KEY (required), WEBHOOK_URL (optional), YF_MOVE=1 (optional)
"""
import os, json, urllib.request, urllib.parse, datetime, math, sys

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
WEBHOOK  = os.environ.get("WEBHOOK_URL", "").strip()
TRY_MOVE = os.environ.get("YF_MOVE", "") == "1"
OUT = "data.json"
HIST_MAX = 90

# ---- engine thresholds (unchanged): id -> (polarity, t1, t2) ----
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

# ---------------- HTTP / FRED helpers ----------------
def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "erp-engine/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fred(series_id, n=1):
    if not FRED_KEY: return []
    q = urllib.parse.urlencode({"series_id": series_id, "api_key": FRED_KEY,
                                "file_type": "json", "sort_order": "desc", "limit": n})
    try:
        obs = http_json(f"https://api.stlouisfed.org/fred/series/observations?{q}").get("observations", [])
        return [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "")]
    except Exception as e:
        print(f"[warn] FRED {series_id}: {e}", file=sys.stderr); return []

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

def to_trillions(v):
    if v is None: return None
    if v > 1e6: return round(v/1e6, 2)   # millions -> $T
    if v > 1e3: return round(v/1e3, 2)   # billions -> $T
    return round(v, 2)

# ---------------- engine indicators (unchanged) ----------------
def get_net_liq_4wk():
    walcl = fred("WALCL", 8); rrp = fred("RRPONTSYD", 40); tga = fred("WTREGEN", 40)
    if not (walcl and rrp and tga): return None
    def net(w, r, t): return w/1000.0 - t/1000.0 - r
    now  = net(walcl[0][1], rrp[0][1], tga[0][1])
    wi = min(3, len(walcl)-1); ri = min(20, len(rrp)-1); ti = min(3, len(tga)-1)
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
        spr.append((sp[d1]-sp[d0])/sp[d0]); bnd.append(-(y[d1]-y[d0]))
    c = pearson(spr, bnd)
    return round(c*100, 0) if c is not None else None

def get_move_yahoo():
    if not TRY_MOVE: return None
    try:
        j = http_json("https://query1.finance.yahoo.com/v8/finance/chart/%5EMOVE?interval=1d&range=5d")
        return round(j["chart"]["result"][0]["meta"]["regularMarketPrice"], 0)
    except Exception as e:
        print(f"[warn] Yahoo MOVE: {e}", file=sys.stderr); return None

def get_indirect_pct():
    try:
        base = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
        ep = "/v1/accounting/od/auctions_query"
        q = "?filter=security_type:eq:Note,security_term:eq:10-Year&sort=-auction_date&page[size]=1&format=json"
        rec = http_json(base+ep+q).get("data", [])
        if not rec: return None
        for k, v in rec[0].items():
            if "indirect" in k.lower() and "pct" in k.lower() and v not in (None, "", "null"):
                return round(float(v), 0)
        return None
    except Exception as e:
        print(f"[warn] Treasury auctions: {e}", file=sys.stderr); return None

def get_cnn_fng():
    """CNN Fear & Greed via its public dataviz endpoint (best-effort; browser UA)."""
    try:
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        req = urllib.request.Request("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                                     headers={"User-Agent": ua, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
        fg = j.get("fear_and_greed", {})
        if fg.get("score") is not None:
            return {"score": round(float(fg["score"])), "rating": fg.get("rating")}
    except Exception as e:
        print(f"[warn] CNN F&G: {e}", file=sys.stderr)
    return None

def collect():
    vals, src = {}, {}
    def put(idd, v, s):
        if v is not None: vals[idd] = v; src[idd] = s
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
    ctx = {"vix": fred_latest("VIXCLS"), "y10": d10}
    return vals, src, ctx

def health(ids, vals):
    sc = [score(i, vals[i]) for i in ids if i in vals]
    return round(sum(sc)/len(sc)/2*100) if sc else None

# ---------------- NEW: liquidity stress layers ----------------
def build_liquidity(vals, ctx):
    sofr = fred_latest("SOFR"); iorb = fred_latest("IORB")
    sofr_iorb = round(sofr - iorb, 2) if (sofr is not None and iorb is not None) else None
    hy   = fred_latest("BAMLH0A0HYM2")
    ig   = fred_latest("BAMLC0A0CM")
    nfci = fred_latest("NFCI")
    reserves = to_trillions(fred_latest("WRESBAL"))
    walcl = fred_latest("WALCL"); tga = fred_latest("WTREGEN"); rrp_b = vals.get("rrp")
    netliq_t = round(walcl/1e6 - (tga/1e6 if tga else 0) - ((rrp_b or 0)/1e3), 2) if walcl else None

    vix = ctx.get("vix"); move = vals.get("move"); corr = vals.get("corr"); netchg = vals.get("net_liq")

    # stress points: 0 loose / 1 watch / 2 tight  (higher = more stress)
    P, RAW = {}, {}
    def add(k, raw, pt):
        if raw is not None and pt is not None: P[k] = pt; RAW[k] = raw
    add("sofr_iorb", sofr_iorb, None if sofr_iorb is None else (2 if sofr_iorb>=0.05 else 1 if sofr_iorb>=0 else 0))
    add("rrp",       rrp_b,     None if rrp_b is None else (2 if rrp_b<50 else 1 if rrp_b<300 else 0))
    add("reserves",  reserves,  None if reserves is None else (2 if reserves<3.0 else 1 if reserves<3.3 else 0))
    add("netliq",    netchg,    None if netchg is None else (2 if netchg<-100 else 1 if netchg<50 else 0))
    add("hy_oas",    hy,        None if hy is None else (2 if hy>5 else 1 if hy>3.5 else 0))
    add("nfci",      nfci,      None if nfci is None else (2 if nfci>0.1 else 1 if nfci>-0.2 else 0))
    add("vix",       vix,       None if vix is None else (2 if vix>25 else 1 if vix>18 else 0))
    add("move",      move,      None if move is None else (2 if move>120 else 1 if move>90 else 0))
    add("corr",      corr,      None if corr is None else (2 if corr>40 else 1 if corr>0 else 0))

    LAYERS = {"repo":["sofr_iorb","rrp"], "balance":["reserves","netliq"],
              "intermediation":["hy_oas","nfci","vix","move","corr"]}
    layers, lscores = {}, []
    for name, ids in LAYERS.items():
        pts = [P[i] for i in ids if i in P]
        sc = round(sum(pts)/len(pts)/2*10, 1) if pts else None
        if sc is not None: lscores.append(sc)
        layers[name] = {"score": sc, "items": {i: {"value": RAW[i], "pt": P[i]} for i in ids if i in P}}
    stress = round(sum(lscores)/len(lscores), 1) if lscores else None
    label = None
    if stress is not None:
        label = "宽松" if stress < 3 else "偏紧 / 警戒" if stress < 6 else "紧张"

    confirmations = {
        "sofr_iorb_positive": {"value": sofr_iorb, "triggered": sofr_iorb is not None and sofr_iorb > 0,
                               "desc": "SOFR-IORB 连续转正 = Repo 真正变紧的第一确认"},
        "hy_widening":        {"value": hy, "triggered": hy is not None and hy > 4.5,
                               "desc": "HY 信用利差明显走阔 = 压力传到风险资产融资"},
        "nfci_positive":      {"value": nfci, "triggered": nfci is not None and nfci > 0,
                               "desc": "NFCI 转正 = 金融条件由松转紧"},
        "vix_above20":        {"value": vix, "triggered": vix is not None and vix > 20,
                               "desc": "VIX 升破 20 = 波动率确认"},
    }
    return {
        "stress": stress, "label": label,
        "layers": layers, "confirmations": confirmations,
        "extra": {"netliq_t": netliq_t, "reserves_t": reserves, "hy_oas": hy, "ig_oas": ig,
                  "nfci": nfci, "sofr": sofr, "iorb": iorb, "sofr_iorb": sofr_iorb,
                  "vix": vix, "move": move, "corr": corr},
    }

# ---------------- main ----------------
def main():
    vals, src, ctx = collect()
    h1 = health(TIER1, vals); h2 = health(TIER2, vals)
    imp_erp = round(2.8 - (h1/100)*2.7, 2) if h1 is not None else None
    imp_10y = round(5.0 - (h2/100)*1.4, 2) if h2 is not None else None
    lights = {i: light(score(i, v)) for i, v in vals.items()}
    liquidity = build_liquidity(vals, ctx)

    today = datetime.date.today().isoformat()
    prev_lights, history = {}, []
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT))
            prev_lights = old.get("lights", {})
            history = old.get("history", [])
        except Exception: pass
    if liquidity["stress"] is not None:
        history = [h for h in history if h.get("date") != today]   # dedup same day
        history.append({"date": today, "stress": liquidity["stress"],
                        "sofr_iorb": liquidity["extra"]["sofr_iorb"],
                        "hy_oas": liquidity["extra"]["hy_oas"],
                        "nfci": liquidity["extra"]["nfci"],
                        "netliq_t": liquidity["extra"]["netliq_t"]})
        history = history[-HIST_MAX:]

    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "values": vals, "lights": lights, "sources": src, "context": ctx,
        "tier_health": {"one": h1, "two": h2},
        "implied": {"erp": imp_erp, "y10": imp_10y},
        "liquidity": liquidity, "history": history,
        "sentiment": {"cnn": get_cnn_fng()},
    }
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=2)
    print(f"wrote {OUT}: 一阶={h1} 二阶={h2} 流动性压力={liquidity['stress']}/10 "
          f"({liquidity['label']}) 历史{len(history)}天")

    if WEBHOOK:
        rank = {"green": 0, "amber": 1, "red": 2}
        flips = [(i, prev_lights.get(i), lights[i]) for i in lights
                 if i in prev_lights and rank[lights[i]] > rank[prev_lights[i]]]
        if flips:
            lines = [f"⚠️ ERP 监控 · 灯色恶化 ({out['generated_at']})"]
            for i, a, b in flips:
                lines.append(f"• {LABEL[i]}: {a}→{b} (现值 {vals[i]})")
            lines.append(f"流动性压力 {liquidity['stress']}/10 · 一阶 {h1} · 二阶 {h2}")
            msg = "\n".join(lines)
            try:
                urllib.request.urlopen(urllib.request.Request(
                    WEBHOOK, data=json.dumps({"text": msg, "content": msg}).encode(),
                    headers={"Content-Type": "application/json"}), timeout=20)
                print("alert sent")
            except Exception as e:
                print(f"[warn] webhook: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
