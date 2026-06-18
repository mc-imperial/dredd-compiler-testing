import argparse
import json
import os
import tempfile
import time
import datetime

from enum import Enum
from pathlib import Path
from dredd_test_runners.common.mutation_tree import MutationTree
from dredd_test_runners.common.run_process_with_timeout import ProcessResult, run_process_with_timeout

from typing import AnyStr, Dict, List, Set


class KillStatus(Enum):
    SURVIVED = 1
    KILL_TIMEOUT = 2
    KILL_FAIL = 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mutation_info_file",
                        help="File containing information about mutations, generated when Dredd was used to actually "
                             "mutate the source code.",
                        type=Path)
    parser.add_argument("mutation_info_file_for_mutant_coverage_tracking",
                        help="File containing information about mutations, generated when Dredd was used to "
                             "instrument the source code to track mutant coverage; this will be compared against the "
                             "regular mutation info file to ensure that tracked mutants match applied mutants.",
                        type=Path)
    parser.add_argument("mutated_compiler_bin_dir",
                        help="Path to the bin directory of the Dredd-mutated compiler.",
                        type=Path)
    parser.add_argument("mutant_tracking_compiler_bin_dir",
                        help="Path to the bin directory of the compiler instrumented to track mutants.",
                        type=Path)
    parser.add_argument("regression_tests_root",
                        help="Path to a directory under which the regression tests to be run will be found.",
                        type=Path)
    parser.add_argument("regression_tests_mutant_tracking_root",
                        help="Corresponding path to this directory under the mutant tracking build of the compiler.",
                        type=Path)
    args = parser.parse_args()

    assert args.mutation_info_file != args.mutation_info_file_for_mutant_coverage_tracking

    print("Building the real mutation tree...")
    with open(args.mutation_info_file, 'r') as json_input:
        mutation_tree = MutationTree(json.load(json_input))
    print("Built!")
    print("Building the mutation tree associated with mutant coverage tracking...")
    with open(args.mutation_info_file_for_mutant_coverage_tracking, 'r') as json_input:
        mutation_tree_for_coverage_tracking = MutationTree(json.load(json_input))
    print("Built!")
    print("Checking that the two mutation trees match...")
    assert mutation_tree.mutation_id_to_node_id == mutation_tree_for_coverage_tracking.mutation_id_to_node_id
    assert mutation_tree.parent_map == mutation_tree_for_coverage_tracking.parent_map
    assert mutation_tree.num_nodes == mutation_tree_for_coverage_tracking.num_nodes
    assert mutation_tree.num_mutations == mutation_tree_for_coverage_tracking.num_mutations
    print("Check complete!")

    with tempfile.TemporaryDirectory() as temp_dir_for_generated_code:
        dredd_covered_mutants_path: Path = Path(temp_dir_for_generated_code, '__dredd_covered_mutants')

        killed_mutants: Set[int] = set()
        unkilled_mutants: Set[int] = set(range(0, mutation_tree.num_mutations))

        # Make a work directory in which information about the mutant killing process will be stored. If this already
        # exists that's OK - there may be other processes working on mutant killing, or we may be continuing a job that
        # crashed previously.
        Path("work").mkdir(exist_ok=True)
        Path("work/tests").mkdir(exist_ok=True)
        Path("work/killed_mutants").mkdir(exist_ok=True)

        # Find all the regression tests under the regression tests root directory. These are all the files with the
        # '.ll' extension.
        tests = []
        for root, _, files in os.walk(args.regression_tests_root):
            for file in files:
                if os.path.splitext(file)[1] == ".ll":
                    tests.append(os.path.join(root, file))
        tests.sort()

        for test_filename in tests:

            # We attempt to create a directory that has the same name as this test file, except that we strip off the
            # regression tests root prefix, and change '/' to '_'.
            test_filename_without_prefix = test_filename[len(str(args.regression_tests_root) + os.sep):]
            test_directory_name = test_filename_without_prefix.replace("/", "_")

            # Try to create the directory; if it already exists then skip this test as that means that results for this
            # test have already been computed or are being computed in parallel.
            test_output_directory: Path = Path("work/tests/" + test_directory_name)
            try:
                test_output_directory.mkdir()
            except FileExistsError:
                print("Skipping test " + test_filename + " as a directory for it already exists")
                continue

            # Record time at which consideration of this test started
            analysis_timestamp_start: datetime.datetime = datetime.datetime.now()

            test_time_start: float = time.time()
            test_result: ProcessResult = run_process_with_timeout(
                cmd=[args.mutated_compiler_bin_dir / "llvm-lit",
                     test_filename],
                timeout_seconds=60)
            test_time_end: float = time.time()
            test_time = test_time_end - test_time_start
            if test_result.returncode != 0:
                print(f"Skipping test {test_filename} as it returned non-zero result {test_result.returncode}.")
                print(f"stdout: {test_result.stdout}")
                print(f"stderr: {test_result.stderr}")
                continue
            if "PASS" not in test_result.stdout.decode('utf-8'):
                print(f"Skipping test {test_filename} as it is not expected to pass.")
                print(f"stdout: {test_result.stdout}")
                print(f"stderr: {test_result.stderr}")
                continue

            if dredd_covered_mutants_path.exists():
                os.remove(dredd_covered_mutants_path)

            tracking_environment: Dict[AnyStr, AnyStr] = os.environ.copy()
            tracking_environment["DREDD_MUTANT_TRACKING_FILE"] = str(dredd_covered_mutants_path)
            test_in_mutant_tracking_build = str(args.regression_tests_mutant_tracking_root) + test_filename[len(str(
                args.regression_tests_root)):]
            mutant_tracking_cmd = [str(args.mutant_tracking_compiler_bin_dir / "llvm-lit"),
                                   test_in_mutant_tracking_build]
            mutant_tracking_result: ProcessResult = run_process_with_timeout(cmd=mutant_tracking_cmd,
                                                                             timeout_seconds=60,
                                                                             env=tracking_environment)
            if mutant_tracking_result.returncode != 0:
                print(
                    f"Warning: skipping test {test_filename} "
                    f"as the regular and mutant-tracking compilers yield different results")
                print(f"Return codes: {test_result.returncode} vs. {mutant_tracking_result.returncode}")
                print(
                    f"stdout: {test_result.stdout.decode('utf-8')} vs. {mutant_tracking_result.stdout.decode('utf-8')}")
                print(
                    f"stdout: {test_result.stderr.decode('utf-8')} vs. {mutant_tracking_result.stderr.decode('utf-8')}")
                continue

            # Load file contents into a list. We go from list to set to list to eliminate duplicates.
            covered_by_this_test: List[int] = list(set([int(line.strip()) for line in
                                                        open(dredd_covered_mutants_path, 'r').readlines()]))
            covered_by_this_test.sort()
            candidate_mutants_for_this_test: List[int] = ([m for m in covered_by_this_test if m not in killed_mutants])
            print("Number of mutants to try: " + str(len(candidate_mutants_for_this_test)))

            already_killed_by_other_tests: List[int] = ([m for m in covered_by_this_test if m in killed_mutants])
            killed_by_this_test: List[int] = []
            covered_but_not_killed_by_this_test: List[int] = []

            for mutant in candidate_mutants_for_this_test:
                mutant_path = Path("work/killed_mutants/" + str(mutant))
                if mutant_path.exists():
                    print("Skipping mutant " + str(mutant) + " as it is noted as already killed.")
                    unkilled_mutants.remove(mutant)
                    killed_mutants.add(mutant)
                    already_killed_by_other_tests.append(mutant)
                    continue
                print("Trying mutant " + str(mutant))
                mutated_environment = os.environ.copy()
                mutated_environment["DREDD_ENABLED_MUTATION"] = str(mutant)
                mutated_test_result: ProcessResult = run_process_with_timeout(
                    cmd=[args.mutated_compiler_bin_dir / "llvm-lit",
                         test_filename],
                    timeout_seconds=int(max(1.0, 5.0 * test_time)),
                    env=mutated_environment)

                if mutated_test_result is None:
                    mutant_result = KillStatus.KILL_TIMEOUT
                elif mutated_test_result.returncode != 0:
                    mutant_result = KillStatus.KILL_FAIL
                else:
                    mutant_result = KillStatus.SURVIVED

                print("Mutant result: " + str(mutant_result))
                if mutant_result == KillStatus.SURVIVED:
                    covered_but_not_killed_by_this_test.append(mutant)
                    continue

                unkilled_mutants.remove(mutant)
                killed_mutants.add(mutant)
                killed_by_this_test.append(mutant)
                print(f"Kill! Mutants killed so far: {len(killed_mutants)}")
                try:
                    mutant_path.mkdir()
                    print("Writing kill info to file.")
                    with open(mutant_path / "kill_info.json", "w") as outfile:
                        json.dump({"killing_test": test_filename_without_prefix, "kill_type": str(mutant_result), "kill_timestamp": str(datetime.datetime.now())},
                                  outfile)
                except FileExistsError:
                    print(f"Mutant {mutant} was independently discovered to be killed.")
                    continue

            # Now that analysis for this test case has completed, write summary information to its directory
            all_considered_mutants = killed_by_this_test \
                + covered_but_not_killed_by_this_test \
                + already_killed_by_other_tests
            all_considered_mutants.sort()
            # We should have put every mutant into some bucket or other
            assert covered_by_this_test == all_considered_mutants
            killed_by_this_test.sort()
            covered_but_not_killed_by_this_test.sort()
            already_killed_by_other_tests.sort()

            # Record time at which consideration of this test ended
            analysis_timestamp_end: datetime.datetime = datetime.datetime.now()

            with open(test_output_directory / "kill_summary.json", "w") as outfile:
                json.dump({"test": test_filename_without_prefix,
                           "covered_mutants_count": len(covered_by_this_test),
                           "killed_mutants": killed_by_this_test,
                           "skipped_mutants_count": len(already_killed_by_other_tests),
                           "survived_mutants_count": len(covered_but_not_killed_by_this_test),
                           "analysis_start_time": str(analysis_timestamp_start),
                           "analysis_end_time": str(analysis_timestamp_end),
                           }, outfile)


if __name__ == '__main__':
    main()
