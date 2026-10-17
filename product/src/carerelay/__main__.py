"""Command-line smoke path for the imported AfterWard FHIR foundation."""

import argparse
import json
from pathlib import Path

from .handoff import prepare_handoff


def main():
    parser = argparse.ArgumentParser(description="Prepare an AfterWard draft from a synthetic FHIR fixture.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--patient", required=True)
    parser.add_argument("--encounter", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = prepare_handoff(bundle, patient_id=args.patient, encounter_id=args.encounter, document_id=args.document)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"AfterWard draft written to {args.output}")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
