#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日本インディーゲーム振興メニュー - データ生成スクリプト
=====================================================
data/games.yaml を読み、各作品の情報と推定売上を data.json に書き出す。

入力ルール:
  - app_id と itch_url の少なくとも一方が必須。
  - app_id があれば Steam *公式* 公開エンドポイントから
    タイトル / header画像 / 概要 / 価格 / 発売日 / ジャンル(日本語) を自動取得。
  - app_id が無い(itch-only)場合は作者提供の title / image / description / genre を使う。

売上の3区分(revenue_kind):
  - "estimated"     : Steam のレビュー数から Boxleiter 法で推定（推定）
  - "self_reported" : itch 等で作者が自己申告した金額（自己申告）
  - "excluded"      : itch 非公開で推定不能（対象外、合計に含めない）

※ SteamDB 等のスクレイピングは ToS 違反のため行わない。
"""

import json, sys, time, datetime, urllib.request, urllib.parse

try:
    import yaml
except ImportError:
    sys.exit("PyYAML が必要です:  pip install pyyaml")

# ------------------------------------------------------------------ パラメータ
MULTIPLIER             = 30
EFFECTIVE_PRICE_FACTOR = 0.60
USD_JPY                = 155.0
TARGET_JPY             = 20_000_000_000_000
REQUEST_SLEEP          = 1.5
MAX_GENRES             = 5
UA = "indie-games-jp/1.0 (parody fan site)"

GAMES_YAML = "data/games.yaml"
OUT_JSON   = "data.json"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_appdetails(app_id):
    url = ("https://store.steampowered.com/api/appdetails?"
           + urllib.parse.urlencode({"appids": app_id, "cc": "us", "l": "japanese"}))
    node = _get(url).get(str(app_id), {})
    if not node.get("success"):
        return None
    d = node["data"]

    price_usd = 0.0
    po = d.get("price_overview")
    if po and not d.get("is_free"):
        price_usd = po.get("final", 0) / 100.0

    genres = [g.get("description", "") for g in (d.get("genres") or [])]
    genres = [g for g in genres if g][:MAX_GENRES]

    rel = d.get("release_date") or {}
    return {
        "name": d.get("name", "(no name)"),
        "header_image": d.get("header_image", ""),
        "short_description": d.get("short_description", ""),
        "price_usd": round(price_usd, 2),
        "price_jpy": round(price_usd * USD_JPY),
        "release_date": rel.get("date", ""),
        "coming_soon": bool(rel.get("coming_soon")),
        "genres": genres,
    }


def fetch_review_count(app_id):
    url = ("https://store.steampowered.com/appreviews/" + str(app_id) + "?"
           + urllib.parse.urlencode({"json": 1, "language": "all",
                                     "purchase_type": "all", "num_per_page": 0,
                                     "filter": "all"}))
    data = _get(url)
    if data.get("success") != 1:
        return 0
    qs = data.get("query_summary", {})
    total = qs.get("total_reviews")
    if total is None:
        total = qs.get("total_positive", 0) + qs.get("total_negative", 0)
    return int(total or 0)


def estimate_revenue_jpy(price_usd, reviews):
    units = reviews * MULTIPLIER
    rev_usd = units * price_usd * EFFECTIVE_PRICE_FACTOR
    return round(rev_usd * USD_JPY), units


def load_games():
    with open(GAMES_YAML, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("games") or []


def main():
    out = []
    total_est = 0
    total_self = 0
    total_reviews = 0

    for e in load_games():
        app_id   = e.get("app_id")
        itch_url = (e.get("itch_url") or "").strip()
        if not app_id and not itch_url:
            print("  ! skip: app_id も itch_url も無い entry", file=sys.stderr)
            continue

        steam = None
        reviews = 0
        if app_id:
            print("→ fetching steam", app_id, file=sys.stderr)
            try:
                steam = fetch_appdetails(app_id)
                time.sleep(REQUEST_SLEEP)
                if steam:
                    reviews = fetch_review_count(app_id)
                    time.sleep(REQUEST_SLEEP)
            except Exception as ex:                       # noqa: BLE001
                print("  ! steam error:", ex, file=sys.stderr)
                steam = None

        # --- 基本情報(Steamが取れれば優先、無ければ作者提供) ---
        if steam:
            name   = e.get("title") or steam["name"]
            image  = e.get("image") or steam["header_image"]
            desc   = e.get("description") or steam["short_description"]
            price  = steam["price_jpy"]
            relstr = steam["release_date"]
            genre  = (e.get("genre") or steam["genres"])[:MAX_GENRES]
            derived_released = not steam["coming_soon"]
        else:
            name   = e.get("title") or ("(取得失敗 App ID: %s)" % app_id if app_id else "(no title)")
            image  = e.get("image", "")
            desc   = e.get("description", "")
            price  = 0
            relstr = ""
            genre  = (e.get("genre") or [])[:MAX_GENRES]
            derived_released = None

        # --- 状態 ---
        status = e.get("status")
        if status not in ("released", "in_development"):
            if derived_released is True:
                status = "released"
            elif derived_released is False:
                status = "in_development"
            else:
                status = "in_development"

        # --- 売上の3区分 ---
        self_jpy = e.get("self_reported_jpy")
        if steam and reviews > 0 and price > 0:
            rev_jpy, units = estimate_revenue_jpy(steam["price_usd"], reviews)
            kind = "estimated"
            total_est += rev_jpy
        elif self_jpy:
            rev_jpy, units = int(self_jpy), 0
            kind = "self_reported"
            total_self += int(self_jpy)
        else:
            rev_jpy, units, kind = None, 0, "excluded"
        total_reviews += reviews

        links = dict(e.get("links", {}) or {})
        if app_id and not links.get("steam"):
            links["steam"] = "https://store.steampowered.com/app/%s" % app_id
        if itch_url and not links.get("itch"):
            links["itch"] = itch_url

        ai = e.get("ai_used")
        ai_used = None if ai is None else bool(ai)
        adult = str(e.get("adult", "")).strip().lower() in ("yes", "true", "1")

        out.append({
            "app_id": app_id,
            "itch_url": itch_url,
            "name": name,
            "header_image": image,
            "short_description": desc,
            "price_jpy": price,
            "release_date": relstr,
            "status": status,
            "ip360": e.get("ip360", "none"),
            "statement": e.get("statement", ""),
            "genre": genre,
            "ai_used": ai_used,
            "adult": adult,
            "total_reviews": reviews,
            "est_units": units,
            "revenue_kind": kind,
            "revenue_jpy": rev_jpy,
            "links": links,
        })

    # 売上の入っているものを上に（excluded は末尾）
    out.sort(key=lambda g: (g["revenue_jpy"] is None, -(g["revenue_jpy"] or 0)))

    result = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                .astimezone().strftime("%Y年%m月%d日"),
        "target_jpy": TARGET_JPY,
        "total_estimated_jpy": total_est,
        "total_self_reported_jpy": total_self,
        "total_counter_jpy": total_est + total_self,
        "total_reviews": total_reviews,
        "assumptions": {
            "boxleiter_multiplier": MULTIPLIER,
            "effective_price_factor": EFFECTIVE_PRICE_FACTOR,
            "usd_jpy": USD_JPY,
        },
        "games": out,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("✓ wrote %s  (%d games / 推定¥%s + 自己申告¥%s)"
          % (OUT_JSON, len(out), format(total_est, ","), format(total_self, ",")),
          file=sys.stderr)


if __name__ == "__main__":
    main()
