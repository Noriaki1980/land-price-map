#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inspect_columns.py — L01 zipの全列名とサンプル値、ユニーク率を一覧表示する調査用スクリプト。
fetch_data.py の find_col がどの列を誤検出しているか切り分けるために使う。

使い方:
  python inspect_columns.py data/raw/L01-2024.zip
"""
import sys
import zipfile
import io
import json
from pathlib import Path

def main():
    zip_path = Path(sys.argv[1])
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        geo_names = [n for n in names if n.lower().endswith(".geojson")]
        shp_names = [n for n in names if n.lower().endswith(".shp")]

        feats = []
        if geo_names:
            for n in geo_names:
                gj = json.loads(z.read(n).decode("utf-8"))
                feats.extend(f.get("properties") or {} for f in gj.get("features", []))
        elif shp_names:
            import shapefile
            for n in shp_names:
                stem = n[:-4]
                dbf = stem + ".dbf"
                if dbf not in names:
                    continue
                r = shapefile.Reader(shp=io.BytesIO(z.read(n)),
                                      dbf=io.BytesIO(z.read(dbf)), encoding="cp932")
                fields = [f[0] for f in r.fields[1:]]
                for sr in r.shapeRecords():
                    feats.append(dict(zip(fields, sr.record)))
        else:
            print("shp/geojsonが見つかりません")
            return

    print(f"features: {len(feats)}")
    if not feats:
        return

    cols = [c for c in feats[0].keys()]
    print(f"columns: {cols}\n")

    for c in cols:
        vals = [f.get(c) for f in feats]
        non_null = [v for v in vals if v not in (None, "")]
        uniq = len(set(non_null))
        sample = non_null[:5]
        print(f"- {c:20s} n={len(non_null):6d} uniq={uniq:6d} "
              f"uniq_ratio={uniq/max(len(non_null),1):.3f} sample={sample}")

if __name__ == "__main__":
    main()
