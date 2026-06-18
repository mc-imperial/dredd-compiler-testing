import argparse
import json
import sys

from pathlib import Path
from typing import Dict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir",
                        help="Directory containing test results. It should have subdirectories, 'tests' and 'killed_mutants'.",
                        type=Path)
    args = parser.parse_args()
    work_dir: Path = args.work_dir
    if not work_dir.exists() or not work_dir.is_dir():
        print(f"Error: {str(work_dir)} is not a working directory.")
        sys.exit(1)
    tests_dir = work_dir / "tests"
    if not tests_dir.exists() or not tests_dir.is_dir():
        print(f"Error: {str(tests_dir)} does not exist.")
        sys.exit(1)
    killed_mutants_dir = work_dir / "killed_mutants"
    if not killed_mutants_dir.exists() or not killed_mutants_dir.is_dir():
        print(f"Error: {str(killed_mutants_dir)} does not exist.")
        sys.exit(1)

    for test in tests_dir.glob('*'):
        if not test.is_dir():
            continue
        if not test.name.startswith("csmith"):
            continue
        kill_summary: Path = test / "kill_summary.json"
        if not kill_summary.exists():
            continue
        kill_summary_json: Dict = json.load(open(kill_summary, 'r'))
        for mutant in kill_summary_json["killed_mutants"]:
            mutant_summary = json.load(open(killed_mutants_dir / str(mutant) / "kill_info.json", 'r'))
            kill_type: str = mutant_summary['kill_type']
            if kill_type == 'KillStatus.KILL_DIFFERENT_STDOUT':
                print(mutant_summary)
            elif kill_type == 'KillStatus.KILL_RUNTIME_TIMEOUT':
                print(mutant_summary)
            elif kill_type == 'KillStatus.KILL_DIFFERENT_EXIT_CODES':
                print(mutant_summary)
            elif kill_type == 'KillStatus.KILL_COMPILER_CRASH':
                print(mutant_summary)
            elif kill_type == 'KillStatus.KILL_COMPILER_TIMEOUT':
                pass
            else:
                print(kill_type)
                assert(False)


if __name__ == '__main__':
    main()
