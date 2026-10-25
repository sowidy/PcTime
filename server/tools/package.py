"""Build a server-only ZIP, excluding real configuration and local artifacts."""
import argparse
from pathlib import Path
import zipfile

SERVER = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    required = ("app.py", "api/tracking.py", "api/tracking_schemas.py", "db/tracking.py",
                "mail/tracking_report.py", "db/schema.sql", "run.py", "check.py")
    for name in required:
        if not (SERVER / name).is_file():
            raise RuntimeError("Missing runtime file: " + name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SERVER.rglob("*")):
            relative = path.relative_to(SERVER)
            if not path.is_file() or any(p in ("__pycache__", ".venv", ".git") for p in relative.parts):
                continue
            if path.name.startswith(".env") and relative.as_posix() != ".env.example":
                continue
            allowed = (path.suffix in (".py", ".sql", ".md", ".yaml", ".example")
                       or relative.as_posix() in ("requirements.txt", "Dockerfile", ".dockerignore"))
            if allowed:
                archive.write(path, "server/" + relative.as_posix())
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
