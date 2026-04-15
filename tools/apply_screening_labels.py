import csv
from pathlib import Path

MAPPING = {
    "140120260310578871.pdf": "yes",
    "140120260311579609.pdf": "yes",
    "140120260312580246.pdf": "yes",
    "140120260312580339.pdf": "yes",
    "140120260312580835.pdf": "yes",
    "140120260312580918.pdf": "yes",
    "140120260312580991.pdf": "no",
    "140120260313581133.pdf": "yes",
    "140120260313581228.pdf": "no",
    "140120260313581329.pdf": "no",
    "140120260313581414.pdf": "yes",
    "140120260313581453.pdf": "yes",
    "140120260204547727.pdf": "no",
    "140120260225568450.pdf": "no",
    "140120260304575759.pdf": "yes",
    "140120260309578005.pdf": "no",
    "140120260310578917.pdf": "no",
    "140120260310578923.pdf": "no",
    "140120260310579032.pdf": "no",
    "140120260310579206.pdf": "no",
    "140120260311579461.pdf": "no",
    "140120260311579492.pdf": "yes",
    "140120260311579628.pdf": "yes",
    "140120260312580378.pdf": "unknown",
    "140120260312580582.pdf": "no",
    "140120260312580646.pdf": "no",
    "140120260312580647.pdf": "no",
    "140120260312580704.pdf": "yes",
    "140120260312580766.pdf": "yes",
    "140120260312580819.pdf": "yes",
    "140120260312581037.pdf": "no",
    "140120260313580708.pdf": "yes",
    "140120260313581078.pdf": "no",
    "140120260313581167.pdf": "yes",
    "140120260313581191.pdf": "no",
    "140120260313581313.pdf": "yes",
    "140120260313581320.pdf": "unknown",
    "140120260313581337.pdf": "no",
    "140120260313581339.pdf": "yes",
    "140120260313581377.pdf": "no",
    "140120260313581385.pdf": "no",
    "140120260313581389.pdf": "unknown",
    "140120260313581479.pdf": "yes",
    "140120260313581523.pdf": "yes",
    "140120260313581537.pdf": "yes",
    "140120260313581558.pdf": "yes",
    "140120260313581595.pdf": "unknown",
    "140120260313581653.pdf": "yes",
    "140120260313581716.pdf": "yes",
    "140120260313581728.pdf": "no",
    "140120260313581742.pdf": "no",
    "140120260313581822.pdf": "yes",
    "140120260303574773.pdf": "no",
    "140120260304575669.pdf": "yes",
    "140120260304576030.pdf": "no",
    "140120260306577666.pdf": "no",
    "140120260306577673.pdf": "no",
    "140120260311580151.pdf": "no",
    "140120260312580203.pdf": "no",
    "140120260312580243.pdf": "yes",
    "140120260312580469.pdf": "yes",
    "140120260312580491.pdf": "yes",
    "140120260312580576.pdf": "no",
    "140120260312580654.pdf": "yes",
    "140120260312580690.pdf": "no",
    "140120260312580844.pdf": "no",
    "140120260312580847.pdf": "yes",
    "140120260312580849.pdf": "no",
    "140120260312580921.pdf": "yes",
    "140120260312580943.pdf": "yes",
    "140120260312580948.pdf": "yes",
    "140120260313580717.pdf": "unknown",
    "140120260313581050.pdf": "no",
    "140120260313581088.pdf": "yes",
    "140120260313581209.pdf": "yes",
    "140120260313581230.pdf": "yes",
    "140120260313581307.pdf": "yes",
    "140120260313581310.pdf": "yes",
    "140120260313581416.pdf": "no",
    "140120260313581490.pdf": "yes",
    "140120260313581579.pdf": "no",
    "140120260313581581.pdf": "yes",
    "140120260313581606.pdf": "yes",
    "140120260313581612.pdf": "no",
    "140120260313581638.pdf": "no",
    "140120260313581677.pdf": "yes",
    "140120260313581725.pdf": "no",
    "140120260313581778.pdf": "yes",
    "140120260313581833.pdf": "yes",
}


def normalize_fieldname(name: str) -> str:
    return name.replace("\ufeff", "").strip()


def main() -> None:
    path = Path("data/eval/screening_sheet.csv")
    backup_path = Path("data/eval/screening_sheet.before_apply_backup.csv")

    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    rows = []
    updated = 0

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV header is missing.")

        raw_fieldnames = reader.fieldnames
        fieldnames = [normalize_fieldname(x) for x in raw_fieldnames]

        if "pdf" not in fieldnames:
            raise ValueError(f"'pdf' column not found. headers={fieldnames}")
        if "has_segment_table" not in fieldnames:
            raise ValueError(f"'has_segment_table' column not found. headers={fieldnames}")

        for raw_row in reader:
            row = {normalize_fieldname(k): v for k, v in raw_row.items() if k is not None}
            pdf = row.get("pdf", "").strip()
            if pdf in MAPPING:
                row["has_segment_table"] = MAPPING[pdf]
                updated += 1
            rows.append(row)

    backup_path.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"OK: updated={updated}, total_rows={len(rows)}, backup={backup_path}")


if __name__ == "__main__":
    main()