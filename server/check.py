"""Check deployment completeness without starting mail or contacting clients."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="store_true", help="Initialize/check the configured MySQL schema")
    args = parser.parse_args()
    from app import app
    from mail import tracking_report  # Imported lazily during real mail rendering.
    required = {"/api/v1/health", "/api/v1/upload", "/api/v1/device/register",
                "/api/v1/web-usage/upload", "/api/v1/app-timeline/upload",
                "/api/v1/app-summary/complete"}
    missing = required - app.openapi()["paths"].keys()
    if missing:
        raise RuntimeError("Missing API routes: " + ", ".join(sorted(missing)))
    if args.database:
        from db import conn
        conn.init_schema()
        assert conn.ping()
    print(json.dumps({"ok": True, "modules": True, "routes": True,
                      "database": "passed" if args.database else "not_checked"}))


if __name__ == "__main__":
    main()
