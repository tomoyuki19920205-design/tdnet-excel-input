import zipfile
from pathlib import Path
p = Path(r"C:\Users\takuy\OneDrive\viewer__linked_tmp.xlsx")
with zipfile.ZipFile(p,"r") as z:
    bad=z.testzip()
    print("tmp zip_test:", "OK" if bad is None else f"BAD:{bad}", "entries=", len(z.namelist()))
