import pdfplumber
from collections import defaultdict

pdf_path = r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\セグメントサンプル20件\5713住友金属鉱山.pdf"

with pdfplumber.open(pdf_path) as pdf:
    page = pdf.pages[17]

    words = page.extract_words(
        x_tolerance=1,
        y_tolerance=3,
        keep_blank_chars=False
    )

    rows = defaultdict(list)

    for w in words:
        y = round(w["top"], 1)
        rows[y].append(w)

    for y in sorted(rows.keys()):
        row = sorted(rows[y], key=lambda x: x["x0"])
        xs = [round(w["x0"], 1) for w in row]
        txt = [w["text"] for w in row]

        print(f"y={y}  x0={xs}")
        print("   ", " | ".join(txt))
