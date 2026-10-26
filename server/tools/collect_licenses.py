"""Stage notices and installed dependency licenses for an independent component."""
import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import sys

COMPONENT = Path(__file__).resolve().parents[1]
LEGAL_FILES = ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")


def stage(destination, roots):
    """Collect license evidence, not a legal certification of binary/source compliance."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name in LEGAL_FILES:
        source = COMPONENT / name
        if not source.is_file():
            raise RuntimeError("Missing required legal file: " + str(source))
        shutil.copy2(source, destination / name)
    shutil.copytree(COMPONENT / "licenses", destination / "licenses", dirs_exist_ok=True)
    pending, seen, records = list(roots), set(), []
    while pending:
        name = pending.pop(0)
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            package = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            if name in roots:
                raise RuntimeError("Required distribution not installed: " + name) from None
            # A platform-specific requirement can be absent in this build environment.
            continue
        copied = []
        for entry in package.files or ():
            parts = Path(str(entry)).parts
            legal = (any(part.lower() == "licenses" for part in parts)
                     or Path(str(entry)).name.lower().startswith(("license", "copying", "notice", "authors")))
            if not legal:
                continue
            source = Path(package.locate_file(entry))
            if not source.is_file():
                continue
            # Wheel entries may contain ../ paths. Keep every output inside its package directory.
            safe = [part for part in parts if part not in ("..", ".", "/", "\\")]
            relative = Path("licenses/dependencies") / canonical / Path(*safe)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(relative.as_posix())
        records.append({"name": package.metadata["Name"], "version": package.version,
                        "license": package.metadata.get("License-Expression") or package.metadata.get("License"),
                        "files": sorted(copied)})
        for requirement in package.requires or ():
            if ";" in requirement and re.search(r"\bextra\b", requirement.split(";", 1)[1]):
                continue
            match = re.match(r"[A-Za-z0-9_.-]+", requirement)
            if match:
                pending.append(match.group())
    # Frozen clients redistribute the interpreter too. Preserve its shipped notices.
    for source in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
        if source.is_file():
            target = destination / "licenses/dependencies/python" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            break
    manifest = {"component": COMPONENT.name, "roots": list(roots),
                "distributions": sorted(records, key=lambda r: r["name"].lower()),
                "scope": "Installed-package notices; bundled native libraries and source obligations require release review."}
    (destination / "licenses/DEPENDENCIES.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--packages", nargs="+", required=True)
    args = parser.parse_args()
    manifest = stage(args.output, args.packages)
    print(json.dumps({"output": str(args.output), "distributions": len(manifest["distributions"])}))


if __name__ == "__main__":
    main()
