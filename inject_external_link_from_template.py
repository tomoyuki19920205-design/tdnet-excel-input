import shutil
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET
from datetime import datetime

VIEWER = Path(r"C:\Users\takuy\OneDrive\viewer.xlsx")
TEMPLATE = Path(r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_linkfixed.xlsx")  # 参照型リンク入りテンプレ
OUT = Path(r"C:\Users\takuy\OneDrive\viewer__linked_tmp.xlsx")

if not VIEWER.exists():
    raise SystemExit(f"viewer not found: {VIEWER}")
if not TEMPLATE.exists():
    raise SystemExit(f"template not found: {TEMPLATE}")

def pick_template_external_parts(z):
    names = set(z.namelist())

    # 1) externalLinks 本体（例: xl/externalLinks/externalLink1.xml と rels）
    ext_parts = [n for n in names if n.startswith("xl/externalLinks/")]
    if not ext_parts:
        raise SystemExit("template has no xl/externalLinks/* parts (unexpected)")

    # 2) workbook.xml から externalReference を拾う
    wb_xml = z.read("xl/workbook.xml")
    wb_root = ET.fromstring(wb_xml)

    # 名前空間は無視して endswith で拾う
    extref_rids = []
    for e in wb_root.iter():
        if e.tag.endswith("externalReference"):
            rid = e.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            if rid:
                extref_rids.append(rid)
    if not extref_rids:
        # 旧形式で externalReferences が無いケースもあるので、テンプレの workbook.xml.rels から externalLink を拾う
        pass

    # 3) workbook.xml.rels から externalLink への Relationship を拾う（Target は internal path）
    rels_xml = z.read("xl/_rels/workbook.xml.rels")
    rels_root = ET.fromstring(rels_xml)

    tpl_extlink_rel = None
    for r in rels_root.iter():
        if r.tag.endswith("Relationship"):
            typ = r.attrib.get("Type","")
            tgt = r.attrib.get("Target","")
            if "externalLink" in typ:
                tpl_extlink_rel = {
                    "Type": typ,
                    "Target": tgt,  # 例: externalLinks/externalLink1.xml
                }
                break

    if not tpl_extlink_rel:
        raise SystemExit("template workbook.xml.rels has no externalLink relationship (unexpected)")

    return ext_parts, tpl_extlink_rel

def next_rid(existing_ids):
    # rId1.. の最大+1
    mx = 0
    for x in existing_ids:
        if x.startswith("rId"):
            try:
                mx = max(mx, int(x[3:]))
            except:
                pass
    return f"rId{mx+1}"

def inject(viewer_zip, template_zip):
    v_names = set(viewer_zip.namelist())

    # テンプレから externalLinks 部品を取得
    ext_parts, tpl_extlink_rel = pick_template_external_parts(template_zip)

    # viewer の workbook.xml / rels を読む
    v_wb_path = "xl/workbook.xml"
    v_rels_path = "xl/_rels/workbook.xml.rels"
    if v_wb_path not in v_names or v_rels_path not in v_names:
        raise SystemExit("viewer missing workbook.xml or workbook.xml.rels (unexpected)")

    v_wb_root = ET.fromstring(viewer_zip.read(v_wb_path))
    v_rels_root = ET.fromstring(viewer_zip.read(v_rels_path))

    # 既存 rId を集める
    existing_ids = []
    for r in v_rels_root.iter():
        if r.tag.endswith("Relationship"):
            rid = r.attrib.get("Id","")
            if rid:
                existing_ids.append(rid)

    new_rid = next_rid(existing_ids)

    # 1) workbook.xml.rels に externalLink relationship を追加
    #    <Relationship Id="rIdX" Type=".../externalLink" Target="externalLinks/externalLink1.xml"/>
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    # rootタグにnsが付いてるのでそのまま追加（タグ名は同一に合わせる）
    rel_tag = None
    for r in v_rels_root.iter():
        if r.tag.endswith("Relationship"):
            rel_tag = r.tag
            break
    if rel_tag is None:
        rel_tag = f"{{{rel_ns}}}Relationship"

    new_rel = ET.Element(rel_tag, {
        "Id": new_rid,
        "Type": tpl_extlink_rel["Type"],
        "Target": tpl_extlink_rel["Target"],
    })
    v_rels_root.append(new_rel)

    # 2) workbook.xml に externalReferences を追加/追記
    #    <externalReferences><externalReference r:id="rIdX"/></externalReferences>
    # タグのnsはそのまま viewer に合わせる（存在しない場合は workbook のnsで作る）
    wb_ns = ""
    if v_wb_root.tag.startswith("{"):
        wb_ns = v_wb_root.tag.split("}")[0].strip("{")

    def qn(local):
        return f"{{{wb_ns}}}{local}" if wb_ns else local

    # externalReferences 探す
    extrefs = None
    for ch in list(v_wb_root):
        if ch.tag.endswith("externalReferences"):
            extrefs = ch
            break
    if extrefs is None:
        # workbook の最後（closing前）に挿入
        extrefs = ET.Element(qn("externalReferences"))
        v_wb_root.append(extrefs)

    extref = ET.Element(qn("externalReference"))
    extref.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"] = new_rid
    extrefs.append(extref)

    # 3) テンプレの xl/externalLinks/* を viewer にコピー（同名があれば上書き）
    #    ここは bytes をそのままコピーするので、外部ターゲット（file:///...data.xlsx）はテンプレ通りになる
    copied_parts = {}
    for p in ext_parts:
        copied_parts[p] = template_zip.read(p)

    # 変更した xml を bytes 化
    v_wb_xml_new = ET.tostring(v_wb_root, encoding="utf-8", xml_declaration=True)
    v_rels_xml_new = ET.tostring(v_rels_root, encoding="utf-8", xml_declaration=True)

    return v_wb_xml_new, v_rels_xml_new, copied_parts, new_rid

# 既存 viewer をバックアップ
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = VIEWER.with_name(f"viewer_backup_before_inject_{ts}.xlsx")
shutil.copy2(VIEWER, backup)
print("backup:", backup)

with zipfile.ZipFile(VIEWER, "r") as vz, zipfile.ZipFile(TEMPLATE, "r") as tz:
    new_wb_xml, new_rels_xml, copied_parts, new_rid = inject(vz, tz)

    # 新しい xlsx を作る（既存の全ファイルをコピーし、workbook.xml / rels を差し替え、externalLinks を追加）
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as outz:
        for item in vz.infolist():
            name = item.filename
            if name == "xl/workbook.xml":
                outz.writestr(name, new_wb_xml)
            elif name == "xl/_rels/workbook.xml.rels":
                outz.writestr(name, new_rels_xml)
            elif name.startswith("xl/externalLinks/"):
                # 既存があってもテンプレ側に置換するので一旦スキップ
                continue
            else:
                outz.writestr(name, vz.read(name))

        # externalLinks 部品を追加
        for p, b in copied_parts.items():
            outz.writestr(p, b)

print("created:", OUT)
print("added externalReference rid:", new_rid)
