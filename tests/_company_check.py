import zipfile, os, glob, io, re, sys
from pathlib import Path
sys.path.insert(0, ".")
from src.events.summary_financials import _decode_html_bytes
from bs4 import BeautifulSoup

zips = sorted(glob.glob("data/docs/**/*.zip", recursive=True), key=os.path.getsize, reverse=True)
results = []
for zp in zips[:3]:
    raw = Path(zp).read_bytes()
    zf = zipfile.ZipFile(io.BytesIO(raw))
    company = ""
    ticker = ""
    
    # 1) ticker from filename
    for name in zf.namelist():
        m = re.search(r"-(\d{4,5})-\d{4}", os.path.basename(name))
        if m:
            ticker = m.group(1)
            break

    # 2) company name from any ixbrl htm (search all ix tags for FilerName/CompanyName)
    for name in zf.namelist():
        bn = os.path.basename(name).lower()
        if not bn.endswith((".htm", ".html")):
            continue
        try:
            content = _decode_html_bytes(zf.read(name))
            if "ix:nonnumeric" not in content.lower() and "ix:nonNumeric" not in content.lower():
                continue
            soup = BeautifulSoup(content, "html.parser")
            for tag in soup.find_all(re.compile(r"ix:non", re.I)):
                tag_name = tag.get("name", "")
                if "FilerName" in tag_name or "CompanyName" in tag_name:
                    t = tag.get_text(strip=True)
                    if t and len(t) < 50:
                        company = t
                        break
            if company:
                break
        except:
            continue
    
    zf.close()
    results.append(f"{os.path.basename(zp)}: ticker={ticker}, company={company}")

with open("tests/_company_info.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(results))
