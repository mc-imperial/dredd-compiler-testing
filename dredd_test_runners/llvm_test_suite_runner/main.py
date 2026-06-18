import argparse
import json
import os
import time
import tempfile
import datetime

from pathlib import Path
from dredd_test_runners.common.hash_file import hash_file
from dredd_test_runners.common.mutation_tree import MutationTree
from dredd_test_runners.common.run_process_with_timeout import ProcessResult, run_process_with_timeout
from dredd_test_runners.common.run_test_with_mutants import run_test_with_mutants, KillStatus

from typing import AnyStr, List, Set


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
    parser.add_argument("llvm_test_suite_root", help="Path to a checkout of the LLVM test suite.",
                        type=Path)
    parser.add_argument("llvm_test_suite_compilation_database",
                        help="Path to a compilation database for the LLVM test suite (generated using CMake).",
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
        regular_exe_path: Path = Path(temp_dir_for_generated_code, '__exe')
        dredd_covered_mutants_path: Path = Path(temp_dir_for_generated_code, '__dredd_covered_mutants')
        mutant_tracking_exe_path: Path = Path(temp_dir_for_generated_code, '__mutant_tracking_exe')
        mutant_exe_path: Path = Path(temp_dir_for_generated_code, '__mutant_exe')

        killed_mutants: Set[int] = set()
        unkilled_mutants: Set[int] = set(range(0, mutation_tree.num_mutations))

        # Make a work directory in which information about the mutant killing process will be stored. If this already
        # exists that's OK - there may be other processes working on mutant killing, or we may be continuing a job that
        # crashed previously.
        Path("work").mkdir(exist_ok=True)
        Path("work/tests").mkdir(exist_ok=True)
        Path("work/killed_mutants").mkdir(exist_ok=True)

        llvm_test_suite_compile_commands = json.load(open(args.llvm_test_suite_compilation_database, 'r'))
        regression_prefix = str(args.llvm_test_suite_root) + "/SingleSource/Regression"
        unit_tests_prefix = str(args.llvm_test_suite_root) + "/SingleSource/UnitTests"
        for test in llvm_test_suite_compile_commands:
            test_filename = test["file"]
            if not test_filename.startswith(regression_prefix) and not test_filename.startswith(unit_tests_prefix):
                print("Skipping test " + test_filename + " as it is not in a relevant directory")
                continue

            # Record time at which consideration of this test started
            analysis_timestamp_start: datetime.datetime = datetime.datetime.now()

            # We attempt to create a directory that has the same name as this test file, except that we strip off the
            # LLVM test suite prefix, and change '/' to '_'.
            test_filename_without_llvm_test_suite_prefix = test_filename[len(str(args.llvm_test_suite_root) + "/"):]
            test_directory_name = test_filename_without_llvm_test_suite_prefix.replace("/", "_")

            # Try to create the directory; if it already exists then skip this test as that means that results for this
            # test have already been computed or are being computed in parallel.
            test_output_directory: Path = Path("work/tests/" + test_directory_name)
            try:
                test_output_directory.mkdir()
            except FileExistsError:
                print("Skipping test " + test_filename + " as a directory for it already exists")
                continue

            print("Analysing kills for test " + test_filename)
            print("Remaining unkilled mutants: " + str(len(unkilled_mutants)))
            print("Mutants killed so far:       " + str(len(killed_mutants)))

            is_c: bool = os.path.splitext(test_filename)[1] == ".c"

            compiler_args = []
            components = test["command"].split(' ')
            index = 0
            while index < len(components):
                component = components[index]
                if component == '-I':
                    compiler_args.append[component]
                    compiler_args.append[components[index + 1]]
                    index += 2
                    continue
                if component.startswith('-I') or component.startswith('-D') or component.startswith(
                        '-w') or component.startswith('-W') or component.startswith('-O'):
                    compiler_args.append(component)
                index += 1
            compiler_args.append(test_filename)
            if is_c:
                compiler_args.append('-lm')

            if regular_exe_path.exists():
                os.remove(regular_exe_path)
            if dredd_covered_mutants_path.exists():
                os.remove(dredd_covered_mutants_path)

            regular_cmd = [str(args.mutated_compiler_bin_dir) + os.sep
                           + "clang" if is_c else str(
                args.mutated_compiler_bin_dir) + os.sep + "clang++"] + compiler_args + ['-o', str(regular_exe_path)]
            print("Compile command:")
            print(' '.join(regular_cmd))
            compile_time_start: float = time.time()
            regular_result: ProcessResult = run_process_with_timeout(cmd=regular_cmd, timeout_seconds=60)
            assert regular_result is not None  # We do not expect regular compilation to time out.
            compile_time_end: float = time.time()
            compile_time = compile_time_end - compile_time_start

            if regular_result.returncode != 0:
                print("Skipping test " + test_filename + " as it failed to compile. Details:")
                print(' '.join(regular_cmd))
                print(regular_result.stdout.decode('utf-8'))
                print(regular_result.stderr.decode('utf-8'))
                continue

            regular_hash = hash_file(str(regular_exe_path))

            run_time_start: float = time.time()
            regular_execution_result: ProcessResult = run_process_with_timeout(cmd=[str(regular_exe_path)],
                                                                               timeout_seconds=60)
            assert regular_execution_result is not None  # We do not expect regular compilation to time out.
            run_time_end: float = time.time()
            run_time = run_time_end - run_time_start

            tracking_environment: dict[AnyStr, AnyStr] = os.environ.copy()
            tracking_environment["DREDD_MUTANT_TRACKING_FILE"] = str(dredd_covered_mutants_path)
            exe_name: str = "clang" if is_c else "clang++"
            mutant_tracking_cmd = [str(args.mutant_tracking_compiler_bin_dir) + os.sep + exe_name]\
                + compiler_args\
                + ['-o', str(mutant_tracking_exe_path)]
            run_process_with_timeout(cmd=mutant_tracking_cmd,
                                     timeout_seconds=60,
                                     env=tracking_environment)

            # Sanity check: confirm that the mutant tracking exe is no different to the regular exe.
            assert regular_hash == hash_file(str(mutant_tracking_exe_path))

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
                mutant_result = run_test_with_mutants(mutants=[mutant],
                                                      compiler_path=str(
                                                          args.mutated_compiler_bin_dir) + os.sep + exe_name,
                                                      compiler_args=compiler_args,
                                                      compile_time=compile_time,
                                                      run_time=run_time,
                                                      binary_hash_non_mutated=regular_hash,
                                                      execution_result_non_mutated=regular_execution_result,
                                                      mutant_exe_path=mutant_exe_path)
                print("Mutant result: " + str(mutant_result))
                if mutant_result == KillStatus.SURVIVED_IDENTICAL\
                        or mutant_result == KillStatus.SURVIVED_BINARY_DIFFERENCE:
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
                        json.dump({"killing_test": test_filename_without_llvm_test_suite_prefix,
                                   "kill_type": str(mutant_result),
                                   "kill_timestamp": str(datetime.datetime.now())
                                   }, outfile)
                except FileExistsError:
                    print(f"Mutant {mutant} was independently discovered to be killed.")
                    continue

            # Now that analysis for this test case has completed, write summary information to its directory
            all_considered_mutants = killed_by_this_test\
                + covered_but_not_killed_by_this_test\
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
                json.dump({"test": test_filename_without_llvm_test_suite_prefix,
                           "covered_mutants_count": len(covered_by_this_test),
                           "killed_mutants": killed_by_this_test,
                           "skipped_mutants_count": len(already_killed_by_other_tests),
                           "survived_mutants_count": len(covered_but_not_killed_by_this_test),
                           "analysis_start_time": str(analysis_timestamp_start),
                           "analysis_end_time": str(analysis_timestamp_end),
                           }, outfile)


if __name__ == '__main__':
    main()
