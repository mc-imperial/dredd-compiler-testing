import argparse
import json
import sys
import tempfile
import subprocess
import os
import shutil

from pathlib import Path
from typing import Dict
from dataclasses import dataclass

from typing import List

@dataclass
class Testcase:
    mutant : int
    prog_path : Path
    kill_type : str


def get_testcases_from_reductions_dir(reductions_dir: Path, killed_mutants_dir: Path, include_timeout: bool) -> List[Testcase]:
    result : List[Testcase] = []

    for testcase in reductions_dir.glob("*"):
        if not testcase.is_dir():
            continue

        # Ensure the reduction_summary and kill_info noth exist
        reductions_summary: Path = testcase / "reduction_summary.json"
        if not reductions_summary.exists():
            continue
        mutant = str(testcase).replace(str(reductions_dir) + "/", "")
        kill_info: Path = killed_mutants_dir / mutant / "kill_info.json"
        if not kill_info.exists():
            continue
        reductions_summary_json: Dict = json.load(open(reductions_summary, "r"))
        kill_info_json: Dict = json.load(open(kill_info, "r"))

        # Check that the testcase is successfully reduced.
        if (
            reductions_summary_json["reduction_status"] != "SUCCESS"
            and not (
                include_timeout
                and reductions_summary_json["reduction_status"] == "TIMEOUT"
            )
        ):
            print(
                f"Skipping testsuite generation for mutant {mutant} creduce has status {reductions_summary_json['reduction_status']}."
            )
            continue

        # ensure some compilable source file exist
        if len(list(testcase.glob('*.c'))) == 0:
            continue

        result.append(Testcase(int(mutant), testcase, kill_info_json["kill_type"]))

    return result

def get_testcases_from_test_dir(tests_dir: Path, killed_mutants_dir: Path) -> List[Testcase]:
    result : List[Testcase] = []

    # Figure out all the tests that have killed mutants in ways for which reduction is
    # actionable. A reason for determining all such tests upfront is that after we reduce one
    # such test, it would be possible to see whether it kills any of the mutants killed by the other
    # tests, avoiding the need to reduce those tests too if so. (However, this is not implemented at
    # present and it may prove simpler to do all of the reductions and subsequently address
    # redundancy.)
    for test in tests_dir.glob('*'):
        if not test.is_dir():
            continue
        if not test.name.startswith("csmith") and not test.name.startswith("yarpgen"):
            continue
        kill_summary: Path = test / "kill_summary.json"
        if not kill_summary.exists():
            continue
        kill_summary_json: Dict = json.load(open(kill_summary, 'r'))
        for mutant in kill_summary_json["killed_mutants"]:
            mutant_summary = json.load(open(killed_mutants_dir / str(mutant) / "kill_info.json", 'r'))
            kill_type: str = mutant_summary['kill_type']
            if (kill_type == 'KillStatus.KILL_DIFFERENT_STDOUT'
                    or kill_type == 'KillStatus.KILL_RUNTIME_TIMEOUT'
                    or kill_type == 'KillStatus.KILL_DIFFERENT_EXIT_CODES'
                    or kill_type == 'KillStatus.KILL_COMPILER_CRASH'):

                testcase = tests_dir / mutant_summary['killing_test']

                # ensure some compilable source file exist
                if len(list(testcase.glob('*.c'))) == 0:
                    continue

                # Test case reduction may be feasible and useful for this kill.
                result.append(Testcase(mutant, testcase, kill_type))
    
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "work_dir",
        help="Directory containing test results. It should have subdirectories, 'killed_mutants' and 'reductions'(unless `use_unreduced_testcase` option is used) .",
        type=Path,
    )
    parser.add_argument(
        "csmith_root",
        help="Path to a checkout of Csmith, assuming that it has been built under "
        "'build' beneath this directory.",
        type=Path,
    )
    parser.add_argument(
        "--include_timeout",
        default=False,
        action="store_true",
        help="Include timed out testcase in its final form.",
    )
    parser.add_argument(
        "--use_unreduced_testcase",
        default=False,
        action="store_true",
        help="Use original unreduced program instead of reduced program.",
    )
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
    reductions_dir = work_dir / "reductions"
    if not args.use_unreduced_testcase and (not reductions_dir.exists() or not reductions_dir.is_dir()):
        print(f"Error: {str(reductions_dir)} does not exist.")
        sys.exit(1)

    testsuite_dir: Path = work_dir / "testsuite"
    Path(testsuite_dir).mkdir(exist_ok=True)

    if args.use_unreduced_testcase:
        testcases = get_testcases_from_test_dir(tests_dir, killed_mutants_dir)
    else:
        testcases = get_testcases_from_reductions_dir(reductions_dir, killed_mutants_dir, args.include_timeout)

    
    for testcase in testcases:

        # Create a directory for this test case
        current_testsuite_dir: Path = testsuite_dir / str(testcase.mutant)
        try:
            current_testsuite_dir.mkdir()
        except FileExistsError:
            continue

        # Check whether the test is miscompilation test or crash test
        testcase_is_miscompilation_check = (
            not testcase.kill_type == "KillStatus.KILL_COMPILER_CRASH"
        )

        print(f"Starting testsuite generaton for {testcase.mutant}.")

        with tempfile.TemporaryDirectory() as tmpdir:
            testfiles_path = list(testcase.prog_path.glob('*.[ch]'))
            testfiles = [os.path.basename(p) for p in testfiles_path]
            c_files = [f for f in testfiles if f.endswith('.c')]
            for filepath in testfiles_path:
                shutil.copy(filepath, Path(tmpdir))

            # Common compiler args
            compiler_args = [
                "-I",
                f"{args.csmith_root}/runtime",
                "-I",
                f"{args.csmith_root}/build/runtime",
                "-pedantic",
                "-Wall",
            ]
            if not testcase_is_miscompilation_check:
                compiler_args.append("-c")

            # compile with clang-15
            proc = subprocess.run(
                ["clang-15", *compiler_args, "-O0", *c_files] 
                + (["-o", "__clang_O0"] if testcase_is_miscompilation_check else []),
                cwd=tmpdir,
                capture_output=True,
            )
            if proc.returncode != 0:
                print(
                    f"clang -O0 compilation for {testcase.mutant} failed with return code {proc.returncode}:"
                )
                print(proc.stderr.decode())
                continue

            # Execute the clang-15 compiled binary
            if testcase_is_miscompilation_check:
                proc = subprocess.run(["./__clang_O0"], cwd=tmpdir, capture_output=True)
                clang_output_O0 = proc.stdout

            # compile with clang-15 with -O3
            proc = subprocess.run(
                ["clang-15", *compiler_args, "-O3", *c_files]
                + (["-o", "__clang_O3"] if testcase_is_miscompilation_check else []),
                cwd=tmpdir,
                capture_output=True,
            )
            if proc.returncode != 0:
                print(
                    f"clang -O3 compilation for {testcase.mutant} failed with return code {proc.returncode}:"
                )
                print(proc.stderr.decode())
                continue

            # Execute the clang-15 compiled binary
            if testcase_is_miscompilation_check:
                proc = subprocess.run(["./__clang_O3"], cwd=tmpdir, capture_output=True)
                clang_output_O3 = proc.stdout

                if clang_output_O3 != clang_output_O0:
                    print(
                        f"clang -O0 and -O3 give different output for {testcase.mutant}"
                    )
                    continue

            # compile with gcc with -O0
            proc = subprocess.run(
                ["gcc-12", *compiler_args, "-O0", *c_files]
                + (["-o", "__gcc_O0"] if testcase_is_miscompilation_check else []),
                cwd=tmpdir,
                capture_output=True,
            )
            if proc.returncode != 0:
                print(
                    f"gcc -O0 compilation for {testcase.mutant} failed with return code {proc.returncode}:"
                )
                print(proc.stderr.decode())
                continue

            # Execute the gcc compiled binary
            if testcase_is_miscompilation_check:
                proc = subprocess.run(["./__gcc_O0"], cwd=tmpdir, capture_output=True)
                gcc_output_O0 = proc.stdout

                if gcc_output_O0 != clang_output_O0:
                    print(f"gcc and clang give different output for {testcase.mutant}")
                    continue

            # compile with gcc with -O3
            proc = subprocess.run(
                ["gcc-12", *compiler_args, "-O3", *c_files]
                + (["-o", "__gcc_O3"] if testcase_is_miscompilation_check else []),
                cwd=tmpdir,
                capture_output=True,
            )
            if proc.returncode != 0:
                print(
                    f"gcc -O3 compilation for {testcase.mutant} failed with return code {proc.returncode}:"
                )
                print(proc.stderr.decode())
                continue

            # Execute the gcc compiled binary
            if testcase_is_miscompilation_check:
                proc = subprocess.run(["./__gcc_O3"], cwd=tmpdir, capture_output=True)
                gcc_output_O3 = proc.stdout

                if gcc_output_O3 != clang_output_O0:
                    print(f"gcc -O0 and -O3 give different output for {testcase.mutant}")
                    continue

            # shutil.copy(testcase.prog_path, current_testsuite_dir / "prog.c")
            for filepath in testfiles_path:
                shutil.copy(filepath, current_testsuite_dir)
            if testcase_is_miscompilation_check:
                with open(current_testsuite_dir / "prog.reference_output", "bw+") as f:
                    f.write(gcc_output_O3)

        print(f"Testsuite generaton for {testcase.mutant} succeed.")


if __name__ == "__main__":
    main()
