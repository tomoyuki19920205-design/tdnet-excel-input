import openpyxl
from pathlib import Path

viewer_path = Path(r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_final_materialized_v1.xlsx")  # ←ここをViewer実ファイルに
wb = openpyxl.load_workbook(viewer_path, data_only=False, keep_links=True)

links = []
if hasattr(wb, "_external_links") and wb._external_links:
    for l in wb._external_links:
        try:
            links.append(str(l.file_link.Target))
        except Exception:
            links.append(str(l))

print("=== external links ===")
for x in links:
    print(x)

wb.close()
