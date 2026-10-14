"""Build a local install ZIP; exclude personal configuration and indexed text."""

from __future__ import annotations

import json
from pathlib import Path
import stat
from zipfile import ZIP_DEFLATED, ZipFile


# Explicit names prevent local notes or credentials added under these folders
# from silently becoming release attachments.
PACKAGE_FILES = (
    "main.py", "settings_ui.py", "plugin.json", "config.example.json",
    "requirements.txt", "README.md", "CHANGELOG.md", "LICENSE",
    "paragraph_search/engine.py", "paragraph_search/matching.py",
    "paragraph_search/plugin.py", "paragraph_search/settings.py",
    "paragraph_search/settings_server.py", "Images/app.svg",
    "paragraph_search/diagnostics.py", "samples/diagnostics/人工样例.txt",
    "samples/work/项目记录.txt", "samples/writing/雨夜素材.md",
    "ui/app.css", "ui/app.js", "ui/index.html", "ui/type.css",
    "Themes/ParagraphBox Mint.xaml",
    "docs/benchmark-0.2.3.json", "docs/benchmark-search-0.2.4.json",
    "docs/paragraphbox-context.png", "docs/paragraphbox-index.jpg",
    "docs/paragraphbox-performance.jpg", "docs/paragraphbox-reader-narrow.png",
    "docs/paragraphbox-reader.png", "docs/paragraphbox-search-timing.png",
    "docs/paragraphbox-settings.jpg", "docs/verification-0.2.1.md",
    "docs/verification-0.2.2.md", "docs/verification-0.2.3.md",
    "docs/verification-0.2.4.md", "docs/verification-0.2.5.md",
    "docs/verification-0.2.6.md",
    "docs/verification-0.2.7.md",
    "docs/verification-0.2.8.md", "docs/paragraphbox-search-scopes.jpg", "docs/paragraphbox-search-guidance-narrow.jpg",
    "docs/paragraphbox-reader-navigation.jpg", "docs/paragraphbox-reader-navigation-narrow.jpg",
    "docs/paragraphbox-diagnostics.jpg",
    "docs/publication.md",
)


def build_package(base: Path, output: Path | None = None) -> Path:
    base = base.resolve()
    files = []
    for name in PACKAGE_FILES:
        path = base / name
        for part in (path, *path.parents):
            if part == base:
                break
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
            if part.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"Package files cannot use links or junctions: {name}")
        if not path.is_file() or not path.resolve().is_relative_to(base):
            raise ValueError(f"Package file must be inside the project: {name}")
        files.append(path)
    manifest = json.loads((base / "plugin.json").read_text(encoding="utf-8"))
    output = output or base / "dist" / f"Flow.Launcher.Plugin.ParagraphBox-{manifest['Version']}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, arcname=path.relative_to(base).as_posix())
    return output


def main() -> None:
    print(build_package(Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    main()
