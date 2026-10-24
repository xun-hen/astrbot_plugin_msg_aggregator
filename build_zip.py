"""把插件打包成 AstrBot 能正确安装的 zip。

为什么不用系统的"压缩文件夹"：Windows 会写**反斜杠**条目名，Linux 上
`os.path.normpath` 不把反斜杠当分隔符，解包后目录结构不对，AstrBot 的收尾步骤
`os.listdir(<target>/<包名>)` 直接抛 `FileNotFoundError`。

所以这里用 zipfile 手工写条目，并保证：

* 条目名一律正斜杠；
* 第一个条目是**目录条目**（`包名/`），AstrBot 靠它 + 子条目前缀推断包根；
* 递归带上 `pages/` 与 `.astrbot-plugin/`（页面与 i18n 是功能的一部分）；
* 不带 `tests/`（有子目录时上游的 update_dir 会退化成整个插件根目录）。

用法：
    python build_zip.py                  # 写到 <插件目录>/dist/
    python build_zip.py <输出路径>
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PACKAGE_NAME = PLUGIN_DIR.name

# 非 .py 的必需文件；根目录下所有 *.py 由 collect_entries() **自动发现**。
# 为什么不在常量里手写 .py 清单：第一版就是这么写的，结果漏了 capabilities.py，
# 打包成功、安装后 ImportError——所以改成扫描，从结构上根除漏项。
REQUIRED_ROOT_FILES = (
    "metadata.yaml",
    "_conf_schema.json",
    "README.md",
    "CHANGELOG.md",
)
# 根目录里**不**进包的 .py（开发工具，运行时用不到）
EXCLUDE_ROOT_PY = {"build_zip.py"}

# 需要递归打进包里的子目录
INCLUDE_DIRS = ("pages", ".astrbot-plugin")
EXCLUDE_DIRS = {"__pycache__", "tests", ".git", "dist", "tools"}


def read_version() -> str:
    """从 metadata.yaml 读版本号。

    Returns:
        版本号（不含前导 v）；读不到返回 `dev`。
    """
    text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("version:"):
            return line.split(":", 1)[1].strip().strip("'\"").lstrip("v") or "dev"
    return "dev"


def default_output() -> Path:
    """默认输出路径：插件目录下的 `dist/`。

    Returns:
        输出路径。
    """
    return PLUGIN_DIR / "dist" / f"{PACKAGE_NAME}-{read_version()}.zip"


def check_consistency() -> list[str]:
    """版本号一致性自检（metadata / CHANGELOG），防止"包名是新版、内容是旧版"。

    Returns:
        问题列表；空表示一致。
    """
    problems: list[str] = []
    version = read_version()
    changelog = PLUGIN_DIR / "CHANGELOG.md"
    if changelog.is_file():
        text = changelog.read_text(encoding="utf-8")
        if f"## v{version}" not in text:
            problems.append(f"CHANGELOG.md 顶部没有 ## v{version} 小节")
    else:
        problems.append("缺少 CHANGELOG.md")
    return problems


def collect_entries() -> list[tuple[str, bytes]]:
    """收集要写进包里的条目（相对路径, 内容）。

    Returns:
        条目列表。

    Raises:
        SystemExit: 缺少必需文件或没有任何 .py。
    """
    entries: list[tuple[str, bytes]] = []

    py_files = sorted(
        path for path in PLUGIN_DIR.glob("*.py")
        if path.is_file() and path.name not in EXCLUDE_ROOT_PY
    )
    if not py_files:
        raise SystemExit("插件根目录下没有任何 .py，无法打包")
    if not any(path.name == "main.py" for path in py_files):
        raise SystemExit("缺少 main.py（AstrBot 从它里面找 Star 子类）")

    missing = [name for name in REQUIRED_ROOT_FILES if not (PLUGIN_DIR / name).is_file()]
    if missing:
        raise SystemExit(f"缺少文件，无法打包：{', '.join(missing)}")

    for path in py_files:
        entries.append((f"{PACKAGE_NAME}/{path.name}", path.read_bytes()))
    for name in REQUIRED_ROOT_FILES:
        entries.append((f"{PACKAGE_NAME}/{name}", (PLUGIN_DIR / name).read_bytes()))

    for dirname in INCLUDE_DIRS:
        root = PLUGIN_DIR / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            if any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix in {".pyc"}:
                continue
            rel = path.relative_to(PLUGIN_DIR).as_posix()
            entries.append((f"{PACKAGE_NAME}/{rel}", path.read_bytes()))
    return entries


def build(output: Path) -> Path:
    """执行打包。

    Args:
        output: 输出路径。

    Returns:
        实际写出的路径。

    Raises:
        SystemExit: 自检不通过。
    """
    problems = check_consistency()
    if problems:
        raise SystemExit("自检不通过，先改好再打包：\n  - " + "\n  - ".join(problems))

    entries = collect_entries()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 目录条目必须第一个写：AstrBot 靠"所有条目的公共前缀"推断包根，
        # 缺了它解包后拿不到 `<target>/<包名>` 这一层。
        zf.writestr(f"{PACKAGE_NAME}/", b"")
        for name, data in entries:
            zf.writestr(name, data)
    return output


def main() -> int:
    """命令行入口。

    Returns:
        退出码。
    """
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default_output()
    build(output)
    print(f"写出 {output}（{output.stat().st_size} 字节）")
    with zipfile.ZipFile(output) as zf:
        for info in zf.infolist():
            print(f"  {info.filename}  {info.file_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
