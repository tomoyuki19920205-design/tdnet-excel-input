import zipfile, os, glob, io, re
from pathlib import Path

zips = sorted(glob.glob("data/docs/**/*.zip", recursive=True), key=os.path.getsize, reverse=True)
raw = Path(zips[0]).read_bytes()
zf = zipfile.ZipFile(io.BytesIO(raw))

for name in zf.namelist():
    if os.path.basename(name).lower() == "qualitative.htm":
        data = zf.read(name)
        lines = []
        lines.append(f"File: {name}")
        lines.append(f"Size: {len(data)}")
        lines.append(f"First 20 bytes hex: {data[:20].hex()}")
        lines.append(f"BOM check: {data[:3] == bytes([0xef,0xbb,0xbf])}")
        
        head = data[:2000]
        m = re.search(rb'charset[="\s]+([a-z0-9_-]+)', head, re.IGNORECASE)
        if m:
            lines.append(f"meta charset: {m.group(1)}")
        else:
            lines.append("no meta charset found")
        
        # Try cp932
        try:
            t = data[200:400].decode("cp932")
            lines.append(f"cp932[200:400]: {t[:60]}")
        except Exception as e:
            lines.append(f"cp932 fail: {e}")
        
        # Try utf-8  
        try:
            t = data[200:400].decode("utf-8")
            lines.append(f"utf8[200:400]: {t[:60]}")
        except Exception as e:
            lines.append(f"utf8 fail: {e}")
        
        with open("tests/_enc_debug.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        break
zf.close()
