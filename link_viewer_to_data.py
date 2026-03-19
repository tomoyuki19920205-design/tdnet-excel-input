from openpyxl import load_workbook
from pathlib import Path

viewer = Path(r"C:\Users\takuy\OneDrive\viewer.xlsx")
data_path = r"C:\Users\takuy\OneDrive\data.xlsx"

print("viewer:", viewer)

if not viewer.exists():
    raise SystemExit("viewer.xlsx not found")

wb = load_workbook(viewer)

sheet = wb.active

# ticker入力セル
ticker_cell = "C1"

# データ範囲
data_sheet = "Sheet1"

# 売上 / 粗利 / 営業利益 を data.xlsx から参照
sheet["D5"] = f'=XLOOKUP({ticker_cell},\'[{data_path}]{data_sheet}\'!A:A,\'[{data_path}]{data_sheet}\'!D:D)'
sheet["E5"] = f'=XLOOKUP({ticker_cell},\'[{data_path}]{data_sheet}\'!A:A,\'[{data_path}]{data_sheet}\'!E:E)'
sheet["F5"] = f'=XLOOKUP({ticker_cell},\'[{data_path}]{data_sheet}\'!A:A,\'[{data_path}]{data_sheet}\'!F:F)'

wb.save(viewer)

print("viewer.xlsx updated to reference data.xlsx")
