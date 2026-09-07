import argparse
import sys
from pathlib import Path

from scripts.baseline_contracts import canonical_bytes
from scripts.task_14_fault_matrix import new_execution_seal, run_matrix, validate_matrix


def _execution_payload(evidence_root):
    seal = new_execution_seal()
    matrix = run_matrix(evidence_root, seal)
    if not validate_matrix(matrix, seal):
        raise RuntimeError("invalid fault matrix execution")
    return {
        "execution_identity": seal.run_id,
        "fault_matrix": matrix.report_rows(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(canonical_bytes(_execution_payload(args.evidence_root)).decode("utf-8"))
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
