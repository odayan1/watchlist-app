#!/usr/bin/env python3
# ウォッチリスト静的アプリ用データ更新スクリプト（GitHub Actions版）。
# 使い方: python3 update.py [--force]
#   株価/前日比/52週/200日線/チャートを更新し、
#   PER/PBR/配当利回り/時価総額を保存済み基準値(eps/bps/dps/sharesOut)から再計算。
#   数値が変わっていれば Google Chatスペースへ更新通知を投稿する（git commit/push はワークフロー側で実施）。
#   thesis/drivers/entryPlan/status/name/symbol など人間の入力は一切触らない。
#
#   2026-10-06追加: 以下3つの規制・指定の「新規指定/解除」をJ-QuantsとJPX公式サイトから
#   検知し、変化があればChat通知する（需給データそのものの通知が目的ではない）。
#     ①空売り価格規制（前日比-10%超下落でトリガー、翌営業日に価格規制発動）
#        → JPX公式サイトの当日分 "*_Stocks_Restricted_Nextday.csv" に載れば都度通知。
#     ②日々公表銘柄 ③増担保規制
#        → J-Quants API `/markets/margin-alert` の PubReason.DailyPublication /
#          PubReason.Restricted フラグを毎日比較し、変化があれば通知。
#          直前の状態は各銘柄オブジェクトの "regulation" に保存して差分検知する。
#          APIキーはリポジトリSecret `JQUANTS_API_KEY`（環境変数）から取得。
import json, os, re, sys, urllib.request, datetime

REPO = os.getcwd()  # actions/checkout 済みのリポジトリルートを想定
DATA = os.path.join(REPO, "data.json")
PAGE = "https://odayan1.github.io/watchlist-app/"
FORCE = "--force" in sys.argv

JQ_BASE = "https://api.jquants.com/v2"
JPX_SSREG_INDEX = "https://www.jpx.co.jp/markets/equities/ss-reg/index.html"


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


def fetch_margin_alert_flags(api_key, date_str, codes4):
    """J-Quants /markets/margin-alert から、指定した4桁コード群の
    日々公表(DailyPublication)/増担保(Restricted)フラグを取得する。
    その日のデータが無ければ空dictを返す（例外にしない＝価格更新は継続させる）。"""
    url = f"{JQ_BASE}/markets/margin-alert?date={date_str}"
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    rows = data.get("data", [])
    wanted = {c + "0": c for c in codes4}  # 4桁コード→J-Quants 5桁コード(末尾0)
    out = {}
    for r in rows:
        code4 = wanted.get(r.get("Code", ""))
        if not code4:
            continue
        reason = r.get("PubReason", {}) or {}
        out[code4] = {
            "dailyPublication": reason.get("DailyPublication") == "1",
            "marginRestricted": reason.get("Restricted") == "1",
        }
    return out


def fetch_short_sale_restricted_today(date_str):
    """JPX公式サイトの当日分「空売り価格規制トリガー銘柄（翌営業日規制）」CSVを取得し、
    4桁コードのsetを返す。当日分のリンクが無ければ空setを返す。"""
    req = urllib.request.Request(JPX_SSREG_INDEX, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    m = re.search(rf'href="([^"]*{re.escape(date_str)}_Stocks_Restricted_Nextday\.csv)"', html)
    if not m:
        return set()
    href = m.group(1)
    if href.startswith("/"):
        href = "https://www.jpx.co.jp" + href
    req2 = urllib.request.Request(href, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req2, timeout=30).read()
    text = raw.decode("cp932", "ignore")
    codes = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        first = line.split(",")[0].strip().strip('"')
        if first.isdigit():
            codes.add(first)
    return codes


def main():
    now = jst_now()
    doc = json.load(open(DATA))
    stocks = doc["stocks"] if isinstance(doc, dict) else doc
    changed, lines, reg_lines = [], [], []
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

    # --- 規制・指定チェック（2026-10-06追加） ---
    jq_date = now.strftime("%Y%m%d")
    codes4 = [s["code"] for s in stocks]
    margin_flags = {}
    api_key = os.environ.get("JQUANTS_API_KEY")
    if api_key:
        try:
            margin_flags = fetch_margin_alert_flags(api_key, jq_date, codes4)
        except Exception as e:
            print("margin-alert fetch fail", e)
    else:
        print("JQUANTS_API_KEY not set, skip margin-alert check")
    ss_restricted_today = set()
    try:
        ss_restricted_today = fetch_short_sale_restricted_today(jq_date)
    except Exception as e:
        print("ss-reg fetch fail", e)

    for s in stocks:
        code = s["code"]
        reg = s.get("regulation", {})
        old_daily = bool(reg.get("dailyPublication"))
        old_margin = bool(reg.get("marginRestricted"))
        flags = margin_flags.get(code, {"dailyPublication": False, "marginRestricted": False})
        new_daily, new_margin = flags["dailyPublication"], flags["marginRestricted"]
        if new_daily and not old_daily:
            reg_lines.append(f"🚨 {s['name']}({code}): 日々公表銘柄に新規指定されました"); changed.append(code)
        elif old_daily and not new_daily:
            reg_lines.append(f"✅ {s['name']}({code}): 日々公表指定が解除されました"); changed.append(code)
        if new_margin and not old_margin:
            reg_lines.append(f"🚨 {s['name']}({code}): 増担保規制の対象になりました"); changed.append(code)
        elif old_margin and not new_margin:
            reg_lines.append(f"✅ {s['name']}({code}): 増担保規制が解除されました"); changed.append(code)
        if code in ss_restricted_today:
            reg_lines.append(f"🚨 {s['name']}({code}): 本日-10%超下落でトリガー、明日の立会から空売り価格規制の対象になります")
            changed.append(code)
        s["regulation"] = {"dailyPublication": new_daily, "marginRestricted": new_margin, "checkedAt": now.strftime("%Y-%m-%d")}

    doc = {"asOf": now.strftime("%Y-%m-%d"), "stocks": stocks}
    json.dump(doc, open(DATA, "w"), ensure_ascii=False)

    if not changed:
        print("no price change, no push/notify")
        write_output("changed", "false")
        return

    write_output("changed", "true")
    body = (f"📈 *ウォッチリスト更新* ({now.strftime('%m/%d %H:%M')} JST)\n"
            + "\n".join(lines)
            + (f"\n\n⚠️ *規制・指定の変化*\n" + "\n".join(reg_lines) if reg_lines else "")
            + f"\n<{PAGE}|アプリを開く>")
    try:
        code = post_chat(body); print("chat posted", code)
    except Exception as e:
        print("chat post fail", e)
    print("done, changed:", changed, "reg_lines:", reg_lines)


if __name__ == "__main__":
    main()
