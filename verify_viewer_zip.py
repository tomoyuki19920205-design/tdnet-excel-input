import zipfile
from pathlib import Path

p = Path(r"C:\Users\takuy\OneDrive\viewer.xlsx")
print("viewer:", p, "exists=", p.exists(), "size=", p.stat().st_size)

try:
    with zipfile.ZipFile(p, "r") as z:
        bad = z.testzip()
        print("zip_test:", "OK" if bad is None else f"BAD:{bad}")
        print("entries:", len(z.namelist()))
except Exception as e:
    print("ZIP OPEN FAILED:", repr(e))
