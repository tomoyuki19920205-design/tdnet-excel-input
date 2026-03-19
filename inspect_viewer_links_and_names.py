from pathlib import Path
import sys
import openpyxl

viewer = Path(r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_final_materialized_v1.xlsx")

print("[VIEWER]", viewer)
print("[EXISTS]", viewer.exists())
if not viewer.exists():
    print("ERROR: viewer file not found. Path is wrong.")
    sys.exit(1)

try:
    wb = openpyxl.load_workbook(viewer, data_only=False, keep_links=True)
except Exception as e:
    print("ERROR: failed to load workbook:", repr(e))
    sys.exit(1)

print("\n=== SHEETS ===")
print(wb.sheetnames)

print("\n=== EXTERNAL LINKS (_external_links) ===")
links = []
try:
    ext = getattr(wb, "_external_links", None)
    if ext:
        for l in ext:
            try:
                links.append(str(l.file_link.Target))
            except Exception:
                links.append(str(l))
except Exception as e:
    print("WARN: reading _external_links failed:", repr(e))

if links:
    for x in links: print(x)
else:
    print("(none found by openpyxl)")

print("\n=== DEFINED NAMES (first 80) ===")
names = list(wb.defined_names.definedName)
print("count =", len(names))
for dn in names[:80]:
    # definedName は attr が無い場合があるので安全に
    nm = getattr(dn, "name", "")
    tx = getattr(dn, "text", "")
    # data.xlsx を参照してそうなものだけ強調
    if "data.xlsx" in str(tx).lower() or "onedrive" in str(tx).lower() or "[" in str(tx):
        print("**", nm, "=", tx)
    else:
        print(nm, "=", tx)

wb.close()
print("\n[DONE]")
