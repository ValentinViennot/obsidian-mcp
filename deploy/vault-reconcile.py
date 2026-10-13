#!/usr/bin/env python3
"""Desktop and server entry point; runs with Python's standard library only."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.vault_reconcile import Reconciler, ReconcileError


def main():
    parser = argparse.ArgumentParser(description="Automatic vault sync with retained editor versions")
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--desktop", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync")
    commands.add_parser("status")
    commands.add_parser("recovery-list")
    export = commands.add_parser("recovery-export")
    export.add_argument("transaction")
    export.add_argument("path")
    export.add_argument("destination", type=Path)
    resume = commands.add_parser("recovery-continue")
    resume.add_argument("transaction")
    resume.add_argument("--editors-quiesced", action="store_true")
    resolve = commands.add_parser("recovery-resolve")
    resolve.add_argument("transaction")
    resolve.add_argument("path")
    resolve.add_argument("--keep", choices=("live", "recovered"), required=True)
    resolve.add_argument("--sha256", required=True, help="latest retained hash shown by status")
    resolve.add_argument("--editors-quiesced", action="store_true")
    args = parser.parse_args()
    try:
        engine = Reconciler(args.vault, desktop=args.desktop)
        if args.command == "sync":
            result = engine.run()
        elif args.command == "recovery-continue":
            result = engine.continue_recovery(args.transaction, editors_quiesced=args.editors_quiesced)
        elif args.command == "recovery-resolve":
            result = engine.resolve(args.transaction, args.path, keep=args.keep,
                                    expected_sha256=args.sha256, editors_quiesced=args.editors_quiesced)
        elif args.command == "recovery-export":
            engine.export(args.transaction, args.path, args.destination)
            result = {"exported": True}
        else:
            with engine.locked():
                result = engine.status()
        print(json.dumps(result, indent=2))
        return 0
    except BlockingIOError:
        print("Another supported vault writer holds the sync lock; retry next sweep", file=sys.stderr)
        return 75
    except (ReconcileError, OSError, ValueError) as exc:
        print(f"Vault sync: {exc}", file=sys.stderr)
        return exc.code if isinstance(exc, ReconcileError) else 3


if __name__ == "__main__":
    sys.exit(main())
