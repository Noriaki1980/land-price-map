#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_data.py — 国土数値情報 地価公示データ(L01)の取得と正規化(最新年版のみ)

方針転換の経緯:
  当初は1983年〜現在の全年をスライダーで見せる構想で、年ごとにファイルを
  ダウンロード・マージする設計にしていた。しかし実際のL01データ構造を
  調べたところ、最新年度版1ファイルの中に既に「昭和58年の公示価格
  (L01_062)」から最新年の価格まで横持ちで全年分含まれていることが判明。
  年次スライダー機能自体を見送ることになったため、この横持ち構造から
  「最新年の価格」と「対前年変動率」だけを取り出すシンプルな構成にした。

  jreit-map の scraper.py と同じ思想は維持:
    - 列名をハードコードせず find_col 方式で特定する
    - 値のパターンで列を推定するヒューリスティクス(v_price等)を使う
    - 検証(validate)に落ちたら出力せずエラーで止める

出力:
  data/raw/L01-{yy}_GML.zip      ... ダウンロードした元データ(手動配置も可)
  data/points.json               ... 正規化済み [{id, lat, lng, use, price, yoy}, ...]
  data/mapping_report.json       ... 列推定結果と統計(人間検証用)

使い方:
  pip install requests
  python fetch_data.py --year 2026
  python fetch_data.py --year 2026 --no-download   # data/raw に手動配置済みの場合
"""

import argparse
import json
import re
import statistics
import sys
import zipfile
from collections import Counter
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

BASE = Path(__file__).parent
RAW_DIR = BASE / "data" / "raw"
OUT_PATH = BASE / "data" / "points.json"
REPORT_PATH = BASE / "data" / "mapping_report.json"

# ---------------------------------------------------------------------------
# ダウンロード
# ---------------------------------------------------------------------------

# 全国版のURL。年度によりファイル名の末尾が変わる可能性があるため候補を順に試す。
URL_CANDIDATES = [
    "https://nlftp.mlit.go.jp/ksj/gml/data/L01/L01-{yy}/L01-{yy}_GML.zip",
    "https://nlftp.mlit.go.jp/ksj/gml/data/L01/L01-{yy}/L01-{yy}.zip",
]

def year_yy(year: int) -> str:
    return f"{year % 100:02d}"

def download_year(year: int) -> Path | None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_DIR / f"L01-{year}_GML.zip"
    if dest.exists():
        print(f"取得済み: {dest.name}")
        return dest
    if requests is None:
        print("requests 未インストールのためダウンロード不可")
        return None
    for tmpl in URL_CANDIDATES:
        url = tmpl.format(yy=year_yy(year))
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 200 and r.content[:2] == b"PK":
                dest.write_bytes(r.content)
                print(f"ダウンロード成功: {url}")
                return dest
        except requests.RequestException as e:
            print(f"{url} -> {e}")
    print(f"自動取得失敗。国土数値情報サイトから手動DLして {dest} に配置してください。\n"
          f"  https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-L01-{year}.html "
          f"の「全国」行からダウンロードしてください。")
    return None

# ---------------------------------------------------------------------------
# 読み込み(geojson優先、shpはフォールバック)
# ---------------------------------------------------------------------------

def load_features(zip_path: Path) -> list[dict]:
    feats: list[dict] = []
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        geo_names = [n for n in names if n.lower().endswith(".geojson")]
        shp_names = [n for n in names if n.lower().endswith(".shp")]

        if geo_names:
            for n in geo_names:
                gj = json.loads(z.read(n).decode("utf-8"))
                for f in gj.get("features", []):
                    props = dict(f.get("properties") or {})
                    geom = f.get("geometry") or {}
                    if geom.get("type") == "Point":
                        lng, lat = geom["coordinates"][:2]
                        props["_lat"], props["_lng"] = lat, lng
                        feats.append(props)
        elif shp_names:
            import shapefile  # pyshp; pip install pyshp
            import io
            for n in shp_names:
                stem = n[:-4]
                dbf = stem + ".dbf"
                if dbf not in names:
                    continue
                r = shapefile.Reader(shp=io.BytesIO(z.read(n)),
                                      dbf=io.BytesIO(z.read(dbf)), encoding="cp932")
                fields = [f[0] for f in r.fields[1:]]
                for sr in r.shapeRecords():
                    props = dict(zip(fields, sr.record))
                    if sr.shape.points:
                        lng, lat = sr.shape.points[0]
                        props["_lat"], props["_lng"] = lat, lng
                        feats.append(props)
        else:
            raise RuntimeError(f"{zip_path.name} に .shp / .geojson が見つからない")
    return feats

# ---------------------------------------------------------------------------
# find_col: 値ヒューリスティクスによる列推定
# ---------------------------------------------------------------------------

USE_CATEGORIES = {"住宅地", "宅地見込地", "商業地", "準工業地", "工業地",
                  "市街化調整区域内宅地", "市街化調整区域内現況林地", "林地"}
USE_CODE_RE = re.compile(r"^0\d\d$")
MUNI_CODE_RE = re.compile(r"^\d{5}$")

def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.replace(",", "").strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return float(s) if "." in s else int(s)
    return None

def column_values(feats: list[dict], col: str, limit: int = 3000):
    return [f.get(col) for f in feats[:limit]]

def find_col(feats: list[dict], validator, exclude=()) -> tuple[str | None, float]:
    best, best_score = None, 0.0
    cols = [c for c in feats[0].keys() if not c.startswith("_") and c not in exclude]
    for c in cols:
        score = validator(column_values(feats, c))
        if score > best_score:
            best, best_score = c, score
    return best, best_score

def v_price(values) -> float:
    """公示価格(円/m2): 全件正の整数、中央値が現実的レンジ。"""
    nums = [_num(v) for v in values]
    ok = [n for n in nums if n is not None]
    if len(ok) < len(values) * 0.99 or not ok:
        return 0.0
    if any(n <= 0 for n in ok):
        return 0.0
    med = statistics.median(ok)
    if not (1_000 <= med <= 5_000_000) or max(ok) > 200_000_000:
        return 0.0
    spread = len({len(str(int(n))) for n in ok})
    return 0.7 + min(spread, 6) * 0.05

def v_yoy(values) -> float:
    """対前年変動率(%): 実数、-100〜数百%程度、ゼロ付近に集中(正規分布的)。"""
    nums = [_num(v) for v in values]
    ok = [n for n in nums if n is not None]
    if len(ok) < len(values) * 0.9 or not ok:
        return 0.0
    if min(ok) < -100 or max(ok) > 1000:
        return 0.0
    med = abs(statistics.median(ok))
    if med > 20:  # 変動率の中央値はふつう小さい
        return 0.0
    has_decimal = any(isinstance(v, str) and "." in v or
                       isinstance(v, float) and v != int(v) for v in values if v not in (None, ""))
    return 0.85 + (0.1 if has_decimal else 0)

def v_use_category(values) -> float:
    vals = [str(v).strip() for v in values if v not in (None, "")]
    if not vals:
        return 0.0
    hit = sum(1 for v in vals if v in USE_CATEGORIES or USE_CODE_RE.fullmatch(v))
    ratio = hit / len(vals)
    kinds = len(set(vals))
    return ratio if ratio > 0.95 and 2 <= kinds <= 10 else 0.0

def v_muni_code(values) -> float:
    vals = [str(_num(v)).zfill(5) if _num(v) is not None else str(v).strip()
            for v in values if v not in (None, "")]
    if not vals:
        return 0.0
    hit = sum(1 for v in vals if MUNI_CODE_RE.fullmatch(v) and 1 <= int(v[:2]) <= 47)
    return hit / len(vals) if hit / len(vals) > 0.98 else 0.0

def v_unique_text(values) -> float:
    """一意キー(住所/標準地番号)候補。"""
    vals = [str(v).strip() for v in values if v not in (None, "")]
    if len(vals) < len(values) * 0.99 or not vals:
        return 0.0
    avg_len = sum(len(v) for v in vals) / len(vals)
    if avg_len < 6:
        return 0.0
    uniq_ratio = len(set(vals)) / len(vals)
    return uniq_ratio if uniq_ratio >= 0.995 else 0.0

# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------

USE_CODE_TO_LABEL = {
    "000": "住宅地", "003": "宅地見込地", "005": "商業地",
    "007": "準工業地", "009": "工業地", "010": "市街化調整区域内宅地",
    "013": "林地",
}

def detect_columns(feats: list[dict]) -> dict:
    price_col, price_s = find_col(feats, v_price)
    yoy_col, yoy_s = find_col(feats, v_yoy, exclude=(price_col,))
    use_col, use_s = find_col(feats, v_use_category)
    muni_col, muni_s = find_col(feats, v_muni_code)
    addr_col, addr_s = find_col(
        feats, v_unique_text, exclude=(price_col, yoy_col, muni_col))
    return {
        "price": {"col": price_col, "score": round(price_s, 3)},
        "yoy":   {"col": yoy_col,   "score": round(yoy_s, 3)},
        "use":   {"col": use_col,   "score": round(use_s, 3)},
        "muni_code": {"col": muni_col, "score": round(muni_s, 3)},
        "address":   {"col": addr_col, "score": round(addr_s, 3)},
    }

def normalize_use(v) -> str:
    s = str(v).strip()
    return USE_CODE_TO_LABEL.get(s, s)

def normalize(feats: list[dict], colmap: dict) -> list[dict]:
    out = []
    pc = colmap["price"]["col"]
    yc = colmap["yoy"]["col"]
    uc = colmap["use"]["col"]
    mc = colmap["muni_code"]["col"]
    ac = colmap["address"]["col"]
    for f in feats:
        price = _num(f.get(pc))
        if price is None or price <= 0:
            continue
        lat, lng = f.get("_lat"), f.get("_lng")
        if lat is None or not (20 < lat < 46) or not (122 < lng < 154):
            continue
        muni = str(_num(f.get(mc)) or f.get(mc, "")).zfill(5)
        addr = str(f.get(ac, "")).strip()
        use = normalize_use(f.get(uc, ""))
        yoy = _num(f.get(yc)) if yc else None
        out.append({
            "id": f"{muni}-{addr}",
            "lat": round(lat, 6), "lng": round(lng, 6),
            "use": use, "price": int(price),
            "yoy": yoy,
            "address": addr,
        })
    return out

# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------

def validate(rows: list[dict], colmap: dict) -> list[str]:
    errs = []
    for key in ("price", "use", "muni_code", "address"):
        if colmap[key]["col"] is None or colmap[key]["score"] < 0.6:
            errs.append(f"{key} 列の推定に失敗 (score={colmap[key]['score']})")
    if colmap["yoy"]["col"] is None or colmap["yoy"]["score"] < 0.6:
        errs.append(f"yoy 列の推定に失敗(変動率の色分けが使えません) "
                    f"(score={colmap['yoy']['score']})")
    if not (15_000 <= len(rows) <= 35_000):
        errs.append(f"件数異常: {len(rows)} 件")
    if not rows:
        return errs
    prices = [r["price"] for r in rows]
    med = statistics.median(prices)
    if not (10_000 <= med <= 1_000_000):
        errs.append(f"価格中央値が異常: {med:,} 円/m2")
    uses = Counter(r["use"] for r in rows)
    if uses and uses.most_common(1)[0][0] != "住宅地":
        errs.append(f"用途内訳が不自然: {dict(uses.most_common(3))}")
    ids = [r["id"] for r in rows]
    dup = len(ids) - len(set(ids))
    if dup > len(ids) * 0.01:
        errs.append(f"ID重複 {dup} 件: 住所列の推定ミスの可能性")
    return errs

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True, help="対象年(例: 2026)")
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()

    zip_path = RAW_DIR / f"L01-{args.year}_GML.zip"
    if not args.no_download:
        zip_path = download_year(args.year) or zip_path
    if not zip_path.exists():
        print(f"データが見つかりません: {zip_path}")
        sys.exit(1)

    feats = load_features(zip_path)
    if not feats:
        print("features が空です")
        sys.exit(1)

    colmap = detect_columns(feats)
    rows = normalize(feats, colmap)
    errs = validate(rows, colmap)

    report = {
        "year": args.year,
        "status": "ok" if not errs else "failed",
        "n_features": len(feats),
        "n_normalized": len(rows),
        "columns": colmap,
        "errors": errs,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    if errs:
        print(f"検証NG: {errs}")
        print(f"詳細: {REPORT_PATH}")
        sys.exit(1)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    print(f"OK: {len(rows):,} 地点 -> {OUT_PATH}")

if __name__ == "__main__":
    main()
