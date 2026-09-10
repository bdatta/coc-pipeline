"""
Check that this machine is ready to run the pipeline.

Run first on a fresh clone. Reports dependencies, credentials, and Atlas
connectivity separately, so a failure points at one thing rather than surfacing
halfway through a conversion.

    python tools/doctor.py
    python tools/doctor.py --config pipeline.yaml
"""

from __future__ import annotations

import argparse
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

OK, WARN, FAIL = "  OK  ", " WARN ", " FAIL "


def line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Check the local setup.")
    ap.add_argument("--config", help="also validate a pipeline YAML")
    args = ap.parse_args()
    problems = 0

    print("\n=== Python ===")
    version = sys.version_info
    if version >= (3, 10):
        line(OK, f"Python {version.major}.{version.minor}.{version.micro}")
    else:
        line(FAIL, f"Python {version.major}.{version.minor}", "3.10 or later required")
        problems += 1

    print("\n=== Required packages ===")
    for module, purpose in [("pdfplumber", "PDF parsing"),
                            ("yaml", "pipeline configuration"),
                            ("dotenv", "reading .env"),
                            ("pymongo", "Atlas storage")]:
        try:
            mod = importlib.import_module(module)
            line(OK, module, getattr(mod, "__version__", ""))
        except ImportError:
            line(FAIL, module, f"{purpose} — pip install -r requirements.txt")
            problems += 1

    print("\n=== Embedding provider ===")
    have_provider = False
    for module in ("voyageai", "openai", "sentence_transformers"):
        try:
            importlib.import_module(module)
            line(OK, module, "available")
            have_provider = True
        except ImportError:
            line(WARN, module, "not installed")
    if not have_provider:
        line(FAIL, "no embedding provider", "install voyageai or openai")
        problems += 1

    print("\n=== Project modules ===")
    try:
        from coc.pipeline_config import build_config

        line(OK, "coc package importable")
    except Exception as exc:
        line(FAIL, "coc package", str(exc))
        print("\n   Run from the repository root, not from tools/.")
        sys.exit(1)

    print("\n=== Credentials (.env) ===")
    env_file = pathlib.Path(".env")
    if env_file.exists():
        line(OK, ".env present")
    else:
        line(WARN, ".env missing", "copy .env.example to .env")

    cfg = build_config(args.config if args.config else None, {})
    missing = cfg.missing_secrets(need_embedding=True)
    for name, present in [("MONGODB_URI", bool(cfg.mongodb_uri)),
                          ("embedding API key", bool(cfg.embed_api_key))]:
        line(OK if present else FAIL, name, "set" if present else "not set")
    problems += len(missing)

    if args.config:
        print("\n=== Configuration ===")
        try:
            from coc.pipeline_config import load_yaml

            load_yaml(args.config)
            line(OK, args.config, "valid")
            print(f"   doc_id={cfg.doc_id} first_page={cfg.first_page} "
                  f"model={cfg.embed_model}@{cfg.embed_dim}")
        except Exception as exc:
            line(FAIL, args.config, str(exc)[:120])
            problems += 1

    print("\n=== Atlas connectivity ===")
    if not cfg.mongodb_uri:
        line(WARN, "skipped", "MONGODB_URI not set")
    else:
        try:
            from coc.vectorstore import get_collection, list_search_indexes

            coll = get_collection(cfg.mongodb_uri, cfg.mongodb_db,
                                  cfg.mongodb_collection)
            line(OK, "connected", f"{cfg.mongodb_db}.{cfg.mongodb_collection}, "
                                  f"{coll.estimated_document_count()} documents")
            indexes = list_search_indexes(coll)
            if not indexes:
                line(WARN, "no search index yet",
                     "created automatically on first embed")
            for idx in indexes:
                ready = idx.get("queryable") or idx.get("status") == "READY"
                dims = next((f.get("numDimensions")
                             for f in (idx.get("latestDefinition") or {}).get("fields", [])
                             if f.get("type") == "vector"), None)
                line(OK if ready else WARN, f"index '{idx.get('name')}'",
                     f"{idx.get('status')}, dims={dims}")
                if dims and int(dims) != int(cfg.embed_dim):
                    line(FAIL, "dimension mismatch",
                         f"index has {dims}, config has {cfg.embed_dim} — "
                         "queries will return nothing")
                    problems += 1
        except Exception as exc:
            line(FAIL, "connection failed", str(exc)[:140])
            print("   Common causes: IP not allow-listed in Atlas Network Access;")
            print("   an unencoded special character in the password.")
            problems += 1

    print()
    if problems:
        print(f"{problems} problem(s) to resolve before running the pipeline.")
        sys.exit(1)
    print("Ready. Next:")
    print("   cp pipeline.example.yaml pipeline.yaml   # edit for your document")
    print("   python tools/run_pipeline.py coc.pdf --config pipeline.yaml --embed --dry-run")


if __name__ == "__main__":
    main()
