#!/usr/bin/env python3
# ウォッチリスト静的アプリ用データ更新スクリプト（GitHub Actions版）。
# 使い方: python3 update.py [--force]
#   株価/前日比/52週/200日線/チャートを更新し、
#   PER/PBR/配当利回り/時価総額を保存済み基準値(eps/bps/dps/sharesOut)から再計算。
#   数値が変わっていれば Google Chatスペースへ更新通知を投稿する（git commit/push はワークフロー側で実施）。
#   thesis/drivers/entryPlan/status/name/symbol など人間の入力は一切触らない。
import json, os, sys, urllib.request, datetime

REPO = os.getcwd()  # actions/checkout 済みのリポジトリルートを想定
DATA = os.path.join(REPO, "data.json")
PAGE = "https://odayan1.github.io/watchlist-app/"
FORCE = "--force" in sys.argv


def jst_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def fetch_chart(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    r = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
    m = r["meta"]
    ts, close = r["timestamp"], r["indicators"]["quote"][0]["close"]
    pairs = [(t, c) for t, c in zip(ts, close) if c is not None]
    ts = [p[0] for p in pairs]; close = [p[1] for p in pairs]
    sma = [round(sum(close[i-199:i+1])/200, 1) if i >= 199 else None for i in range(len(close))]
    start = max(0, len(close)-260)
    return {
        "price": round(m["regularMarketPrice"], 1),
        "changePct": round(m.get("regularMarketChangePercent", m.get("fulldayChangePercent", 0)), 2),
        "high52": round(m["fiftyTwoWeekHigh"], 1),
        "low52": round(m["fiftyTwoWeekLow"], 1),
        "currency": m.get("currency", "JPY"),
        "sma200": sma[-1],
        "chart": {"ts": ts[start:], "close": close[start:], "sma": sma[start:]},
    }


def fmt_cap(v):
    if v is None: return None
    if v >= 1e12: return f"約{v/1e12:.2f}兆円"
    return f"約{v/1e8:,.0f}億円"


def post_chat(text):
    hook = os.environ["GCHAT_WEBHOOK"]
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(hook, data=data, headers={"Content-Type": "application/json; charset=UTF-8"})
    return urllib.request.urlopen(req, timeout=30).status


def write_output(key, value):
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"{key}={value}\n")


def main():
    now = jst_now()
    doc = json.load(open(DATA))
    stocks = doc["stocks"] if isinstance(doc, dict) else doc
    changed, lines = [], []
    for s in stocks:
        try:
            q = fetch_chart(s["symbol"])
        except Exception as e:
            print("fetch fail", s["code"], e); continue
        old_price = s.get("price")
        s.update({k: q[k] for k in ("price", "changePct", "high52", "low52", "currency", "sma200", "chart")})
        if s.get("eps"): s["per"] = round(q["price"]/s["eps"], 2)
        if s.get("bps"): s["pbr"] = round(q["price"]/s["bps"], 2)
        if s.get("dps"): s["divYield"] = round(s["dps"]/q["price"]*100, 2)
        if s.get("sharesOut"): s["marketCap"] = fmt_cap(s["sharesOut"]*q["price"])
        vs200 = (q["price"]/q["sma200"]-1)*100 if q["sma200"] else None
        arrow = "▲" if q["changePct"] >= 0 else "▼"
        v = f"（200日線{'+' if vs200 and vs200>=0 else ''}{vs200:.0f}%）" if vs200 is not None else ""
        lines.append(f"・{s['name']}({s['code']}): ¥{round(q['price']):,} {arrow}{q['changePct']:+.2f}% {v}")
        if old_price != q["price"] or FORCE:
            changed.append(s["code"])

    doc = {"asOf": now.strftime("%Y-%m-%d"), "stocks": stocks}
    json.dump(doc, open(DATA, "w"), ensure_ascii=False)

    if not changed:
        print("no price change, no push/notify")
        write_output("changed", "false")
        return

    write_output("changed", "true")
    body = (f"📈 *ウォッチリスト更新* ({now.strftime('%m/%d %H:%M')} JST)\n"
            + "\n".join(lines)
            + f"\n<{PAGE}|アプリを開く>")
    try:
        code = post_chat(body); print("chat posted", code)
    except Exception as e:
        print("chat post fail", e)
    print("done, changed:", changed)


if __name__ == "__main__":
    main()
