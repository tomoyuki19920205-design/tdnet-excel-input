from pathlib import Path
import zipfile, xml.etree.ElementTree as ET

viewer = Path(r"C:\Users\takuy\OneDrive\viewer__linked_tmp.xlsx")
NS = {"r":"http://schemas.openxmlformats.org/package/2006/relationships"}

def parse_rels(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out=[]
    for rel in root.findall("r:Relationship", NS):
        out.append({
            "Id": rel.attrib.get("Id",""),
            "Type": rel.attrib.get("Type",""),
            "Target": rel.attrib.get("Target",""),
            "TargetMode": rel.attrib.get("TargetMode",""),
        })
    return out

with zipfile.ZipFile(viewer,"r") as z:
    names=set(z.namelist())
    ext_xmls = sorted([n for n in names if n.startswith("xl/externalLinks/") and n.endswith(".xml")])
    print("[externalLinks xml count]:", len(ext_xmls))
    ext_targets=[]
    for x in ext_xmls:
        rels_path = x.replace("externalLinks/", "externalLinks/_rels/") + ".rels"
        if rels_path in names:
            for r in parse_rels(z.read(rels_path)):
                if r["TargetMode"] == "External":
                    ext_targets.append(r["Target"])
    ext_targets=sorted(set(ext_targets))
    print("[external targets]:")
    for t in ext_targets: print(" ", t)
