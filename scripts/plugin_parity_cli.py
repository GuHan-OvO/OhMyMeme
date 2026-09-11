# pyright: basic

from pathlib import Path


# Parse the fixed validator command-line options.
def parse_arguments(argv):
    check = None
    fixture_root = Path("fixtures/plugin-parity/providers")
    report = None
    matrix = None
    ui_baseline = None
    index = 0
    while index < len(argv):
        option = argv[index]
        if option in (
            "--check",
            "--fixture-root",
            "--report",
            "--matrix",
            "--ui-baseline",
        ):
            if index + 1 >= len(argv):
                raise ValueError(f"{option}: missing value")
            target = Path(argv[index + 1])
            if option == "--check":
                check = target
            elif option == "--fixture-root":
                fixture_root = target
            elif option == "--matrix":
                matrix = target
            elif option == "--ui-baseline":
                ui_baseline = target
            else:
                report = target
            index += 2
            continue
        if option == "--help":
            print("usage: plugin_parity_baseline.py --check INPUT --report REPORT")
            raise SystemExit(0)
        raise ValueError(f"unknown option: {option}")
    if report is None:
        raise ValueError("--report: missing required option")
    return check, fixture_root, report, matrix, ui_baseline
