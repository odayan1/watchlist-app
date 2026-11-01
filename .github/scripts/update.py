#!/usr/bin/env python3
# ウォッチリスト静的アプリ用データ更新スクリプト（GitHub Actions版）。
# 使い方: python3 update.py [--force]
#   株価/前日比/52週/200日線/チャートを更新し、
#   PER/PBR/配当利回り/時価総額を保存済み基準値(eps/bps/dps/sharesOut)から再計算。
#   数値が変わっていれば Google Chatスペースへ更新通知を投稿する（git commit/push はワークフロー側で実施）。
#   thesis/drivers/entryPlan/status/name/symbol など人間の入力は一切触らない。
#
#   2026-10-06追加: 以下3つの規制・指定の「新規指定/解除」をJPX公式サイトから
#   検知し、変化があればChat通知する（需給データそのものの通知が目的ではない）。
#     ①空売り価格規制（前日比-10%超下落でトリガー、翌営業日に価格規制発動）
#        → JPX公式サイトの当日分 "*_Stocks_Restricted_Nextday.csv" に載れば都度通知。
#     ②日々公表銘柄 ③増担保規制
#        → JPX公式サイトの一覧表（HTML）に載っているかを毎日比較し、変化（載った/外れた）があれば通知。
#            日々公表銘柄: https://www.jpx.co.jp/markets/equities/margin-daily/index.html
#            増担保規制  : https://www.jpx.co.jp/markets/equities/margin-reg/index.html
#              （「信用取引に関する規制を行っている銘柄」の表。実施日が翌営業日の銘柄も載る）
#          直前の状態は各銘柄オブジェクトの "regulation" に保存して差分検知する。
#          ※2026-11-01: J-Quants（有料プラン）解約に伴い、J-Quants `/markets/margin-alert` から置き換え。
#          ページ取得や表の読み取りに失敗した日は前回の状態を据え置く（誤った「解除」通知を防ぐ）。
#
#   --dry-run: 取得・判定・通知文の組み立てまで行い、内容を表示するだけ。
#              data.json の書き込み・git push・Chat投稿は一切しない（テスト用）。
import html, json, os, re, sys, unicodedata, urllib.request, datetime

REPO = os.getcwd()  # actions/checkout 済みのリポジトリルートを想定
DATA = os.path.join(REPO, "data.json")
PAGE = "https://odayan1.github.io/watchlist-app/"
FORCE = "--force" in sys.argv
DRY_RUN = "--dry-run" in sys.argv  # テスト用: 書き込み・push・Chat投稿をしない

JPX_MARGIN_DAILY = "https://www.jpx.co.jp/markets/equities/margin-daily/index.html"  # 日々公表銘柄
JPX_MARGIN_REG = "https://www.jpx.co.jp/markets/equities/margin-reg/index.html"  # 信用取引に関する規制（増担保）
JPX_SSREG_INDEX = "https://www.jpx.co.jp/markets/equities/ss-reg/index.html"


def jst_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def fetch_chart(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    r = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
    m = r["meta"]
    q = r["indicators"]["quote"][0]
    # 2026-10-21: Lightweight Charts（ローソク足＋出来高）用に始値/高値/安値/出来高も保存。2年分まるごと保持（移動平均の計算用）
    rows = [(t, o, h, l, c, v) for t, o, h, l, c, v in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"])
            if None not in (o, h, l, c)]
    ts = [x[0] for x in rows]; close = [x[4] for x in rows]
    sma = [round(sum(close[i-199:i+1])/200, 1) if i >= 199 else None for i in range(len(close))]
    start = 0
    return {
        "price": round(m["regularMarketPrice"], 1),
        "changePct": round(m.get("regularMarketChangePercent", m.get("fulldayChangePercent", 0)), 2),
        "high52": round(m["fiftyTwoWeekHigh"], 1),
        "low52": round(m["fiftyTwoWeekLow"], 1),
        "currency": m.get("currency", "JPY"),
        "sma200": sma[-1],
        "chart": {"ts": ts[start:], "open": [round(x[1], 1) for x in rows], "high": [round(x[2], 1) for x in rows],
                  "low": [round(x[3], 1) for x in rows], "close": [round(c, 1) for c in close],
                  "volume": [int(x[5] or 0) for x in rows], "sma": sma[start:]},
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


def _jpx_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def _parse_jpx_tables(page_html):
    """JPXのページをh2見出しごとに区切り、各見出し直下の表から {コード: 日付} を取り出す。
    日付は「コード」列のすぐ右の列（指定日/実施日/解除日）。
    戻り値: {見出し文字列: {コード: 日付}}。「コード」列の見出し行がある表だけを対象にする。"""
    out = {}
    for part in re.split(r"<h2[^>]*>", page_html)[1:]:
        if "</h2>" not in part:
            continue
        head, body = part.split("</h2>", 1)
        title = unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]+>", "", head))).strip()
        tbl = re.search(r"<table.*?</table>", body, re.S)
        if not tbl:
            continue
        rows, ci = {}, None
        for tr in re.findall(r"<tr.*?</tr>", tbl.group(0), re.S):
            cells = [unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            if ci is None:
                if "コード" in cells:
                    ci = cells.index("コード")
                continue
            if len(cells) > ci + 1 and re.fullmatch(r"[0-9][0-9A-Z]{3}", cells[ci].upper()):
                rows[cells[ci].upper()] = cells[ci + 1]
        if ci is not None:
            out[title] = rows
    return out


def fetch_jpx_list(url, current_title, released_title):
    """JPX公式サイトの「指定中の銘柄」と「解除された銘柄（解除後1週間程度掲載）」の表を取得する。
    戻り値: (指定中 {コード: 指定日/実施日}, 解除済み {コード: 解除日})。
    想定の見出し・表が見つからない（ページの作りが変わった等）ときは例外にする
    ＝呼び出し側で前回の状態を据え置き、誤った「解除」通知を出さないため。"""
    tables = _parse_jpx_tables(_jpx_get(url))
    if current_title not in tables:
        raise RuntimeError(f"JPXページに「{current_title}」の表が見つかりません: {url}")
    return tables[current_title], tables.get(released_title, {})


def fetch_margin_status():
    """日々公表銘柄・増担保規制（信用取引に関する規制）の現在の指定状況をJPX公式サイトから取得する。
    戻り値: {"daily": (指定中, 解除済み) または None, "restricted": (指定中, 解除済み) または None}
    取得に失敗した方は None（＝前回状態を据え置く）。"""
    out = {"daily": None, "restricted": None}
    try:
        out["daily"] = fetch_jpx_list(JPX_MARGIN_DAILY, "日々公表銘柄", "日々公表銘柄を解除された銘柄")
    except Exception as e:
        print("margin-daily fetch fail", e)
    try:
        out["restricted"] = fetch_jpx_list(JPX_MARGIN_REG, "信用取引に関する規制を行っている銘柄",
                                           "信用取引に関する規制を解除された銘柄")
    except Exception as e:
        print("margin-reg fetch fail", e)
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
        cur = q.get("currency") or "JPY"
        ptxt = f"¥{round(q['price']):,}" if cur == "JPY" else f"${q['price']:,.2f}"
        lines.append(f"・{s['name']}({s['code']}): {ptxt} {arrow}{q['changePct']:+.2f}% {v}")
        if old_price != q["price"] or FORCE:
            changed.append(s["code"])

    # --- 規制・指定チェック（2026-10-06追加） ---
    jq_date = now.strftime("%Y%m%d")
    jp_stocks = [s for s in stocks if str(s.get("symbol", "")).endswith(".T")]  # 規制チェックは日本株のみ
    margin = fetch_margin_status()
    ss_restricted_today = set()
    try:
        ss_restricted_today = fetch_short_sale_restricted_today(jq_date)
    except Exception as e:
        print("ss-reg fetch fail", e)

    for s in jp_stocks:
        code = s["code"]
        reg = s.get("regulation", {})
        old_daily = bool(reg.get("dailyPublication"))
        old_margin = bool(reg.get("marginRestricted"))
        flags_d, flags_r = margin["daily"], margin["restricted"]
        # 取得に失敗した方は前回状態を据え置く（失敗＝全銘柄「解除」と誤判定しないため）
        new_daily = (code.upper() in flags_d[0]) if flags_d else old_daily
        new_margin = (code.upper() in flags_r[0]) if flags_r else old_margin
        if new_daily and not old_daily:
            reg_lines.append(f"🚨 {s['name']}({code}): 日々公表銘柄に新規指定されました（指定日 {flags_d[0][code.upper()]}）"); changed.append(code)
        elif old_daily and not new_daily:
            d = flags_d[1].get(code.upper())
            reg_lines.append(f"✅ {s['name']}({code}): 日々公表指定が解除されました" + (f"（解除日 {d}）" if d else "")); changed.append(code)
        if new_margin and not old_margin:
            reg_lines.append(f"🚨 {s['name']}({code}): 増担保規制の対象になりました（実施日 {flags_r[0][code.upper()]}）"); changed.append(code)
        elif old_margin and not new_margin:
            d = flags_r[1].get(code.upper())
            reg_lines.append(f"✅ {s['name']}({code}): 増担保規制が解除されました" + (f"（解除日 {d}）" if d else "")); changed.append(code)
        if code in ss_restricted_today:
            reg_lines.append(f"🚨 {s['name']}({code}): 本日-10%超下落でトリガー、明日の立会から空売り価格規制の対象になります")
            changed.append(code)
        s["regulation"] = {"dailyPublication": new_daily, "marginRestricted": new_margin, "checkedAt": now.strftime("%Y-%m-%d")}

    body = (f"📈 *ウォッチリスト更新* ({now.strftime('%m/%d %H:%M')} JST)\n"
            + "\n".join(lines)
            + (f"\n\n⚠️ *規制・指定の変化*\n" + "\n".join(reg_lines) if reg_lines else "")
            + f"\n<{PAGE}|アプリを開く>")
    if DRY_RUN:
        print("[dry-run] data.json/Chatには何もしません。changed:", changed)
        print("[dry-run] regulation:", {s["code"]: s.get("regulation") for s in jp_stocks})
        print("[dry-run] 通知文:\n" + body)
        return

    doc = {"asOf": now.strftime("%Y-%m-%d"), "stocks": stocks}
    json.dump(doc, open(DATA, "w"), ensure_ascii=False)

    if not changed:
        print("no price change, no push/notify")
        write_output("changed", "false")
        return

    write_output("changed", "true")
    try:
        code = post_chat(body); print("chat posted", code)
    except Exception as e:
        print("chat post fail", e)
    print("done, changed:", changed, "reg_lines:", reg_lines)


if __name__ == "__main__":
    main()
