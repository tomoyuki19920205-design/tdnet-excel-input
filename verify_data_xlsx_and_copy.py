import pandas as pd
from pathlib import Path

xlsx = Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\data.xlsx")
print("[CHECK] exists:", xlsx.exists(), "size:", xlsx.stat().st_size if xlsx.exists() else None)

df = pd.read_excel(xlsx)
# ticker列名が違う可能性があるので候補探索
cands = [c for c in df.columns if str(c).lower() in ("ticker","code","company_code")]
print("[COLUMNS] ticker candidates:", cands)

# まず ticker 列を推定
ticker_col = cands[0] if cands else None
if ticker_col is None:
    # それっぽい列をざっくり探す
    for c in df.columns:
        if "ticker" in str(c).lower() or "code" in str(c).lower():
            ticker_col = c
            break
print("[USE] ticker_col =", ticker_col)

targets = {"25900","66350","80570"}
if ticker_col:
    s = df[ticker_col].astype(str)
    hit = df[s.isin(targets)]
    print("[HIT] rows =", len(hit))
    if len(hit) > 0:
        # 主要列だけ表示（無ければある分だけ）
        show_cols = [ticker_col]
        for k in ("fiscal_year_end","period","quarter","sales","operating_profit","op"):
            if k in df.columns: show_cols.append(k)
        # 重複除去して先頭10
        print(hit[show_cols].drop_duplicates().head(10).to_string(index=False))
else:
    print("[WARN] ticker列が特定できませんでした。列名を確認してください。")

# OneDrive側へコピー
src = Path(r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\data.xlsx")
dst = Path(r"C:\Users\takuy\OneDrive\data.xlsx")
dst.write_bytes(src.read_bytes())
print("[COPY] ->", dst, "done")
