from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

root = Path(r"C:\Users\takuy\OneDrive")
files = sorted(root.glob("*company*view*.xlsx"))

def has_data_link(xlsx: Path):
    try:
        with zipfile.ZipFile(xlsx, "r") as z:
            names = set(z.namelist())
            if "xl/workbook.xml" not in names:
                return (False, "no workbook.xml")
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            # definedName の中を探索
            for dn in wb.iter():
                if dn.tag.endswith("definedName"):
                    text = (dn.text or "")
                    t = text.lower()
                    if "data.xlsx" in t or "[data" in t or "onedrive" in t:
                        return (True, text[:200])
            # externalLinks があるならそれもリンク候補
            ext = [n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")]
            if ext:
                return (True, "has externalLinks xml")
            return (False, "")
    except Exception as e:
        return (False, f"error:{e}")

print("scan targets =", len(files))
hits = []
for f in files:
    ok, note = has_data_link(f)
    if ok:
        hits.append((str(f), note))

print("\n=== HIT: files that likely reference data.xlsx ===")
if not hits:
    print("(none)")
else:
    for p, note in hits:
        print(p)
        print("  note:", note)

print("\n=== DONE ===")
