import shutil
from pathlib import Path

src = Path(r"C:\Users\takuy\OneDrive\20260228テスト用A_company_view_linkfixed.xlsx")
dst = Path(r"C:\Users\takuy\OneDrive\viewer.xlsx")

print("SOURCE:", src)
print("DEST  :", dst)

if not src.exists():
    raise SystemExit("source viewer template not found")

# 既存viewerバックアップ
if dst.exists():
    backup = dst.with_name("viewer_backup_before_linkfix.xlsx")
    shutil.copy2(dst, backup)
    print("backup created:", backup)

# linkfixed版をviewerとして配置
shutil.copy2(src, dst)

print("viewer.xlsx replaced with link-enabled template")
print("DONE")
