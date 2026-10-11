"""Zips packaging/dist/报销助手 into receipt-claim-windows-x64-v<version>.zip (an ASCII file name: GitHub mangles others)
and prints its SHA-256."""
import hashlib
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))
from version import __version__  # noqa: E402

name = f"receipt-claim-windows-x64-v{__version__}"
shutil.copy2(HERE / "使用说明.txt", HERE / "dist" / "报销助手" / "使用说明.txt")
archive = shutil.make_archive(str(HERE / name), "zip", root_dir=HERE / "dist", base_dir="报销助手")
digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
(HERE / f"{name}.zip.sha256").write_text(f"{digest}  {name}.zip\n", "utf-8")
print(f"{archive}  {Path(archive).stat().st_size / 1e6:.0f} MB\nSHA-256 {digest}")
