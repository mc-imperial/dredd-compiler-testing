import argparse
import enum
import json
import os
import random
import shutil
import tempfile
import time
import datetime

from dredd_test_runners.common.mutation_tree import MutationTree
from dredd_test_runners.common.run_process_with_timeout import ProcessResult, run_process_with_timeout

from pathlib import Path
from typing import List, Optional, Set

# Fixed locations, as per the fuzzing machine's setup.
GFAUTO_VENV_BIN: Path = Path("/data/graphicsfuzz/gfauto/.venv/bin")
GFAUTO_FUZZING_ROOT: Path = Path("/data/gfauto_fuzzing")
AMBER_BINARY: Path = Path("/data/gfauto_fuzzing/binaries/built_in/"
                          "gfbuild-amber-8c3bfef40c2387944fdc81746e2e3249e4da5566-Linux_x64_Debug/"
                          "amber/bin/amber")
MESA_MUTATED_ICD: Path = Path("/data/mesa-26.1.4-mutated/builddir/src/gallium/targets/lavapipe/"
                              "lvp_devenv_icd.x86_64.json")
MESA_MUTANT_TRACKING_ICD: Path = Path("/data/mesa-26.1.4-mutant-tracking/builddir/src/gallium/targets/lavapipe/"
                                      "lvp_devenv_icd.x86_64.json")


class KillStatus(enum.Enum):
    SURVIVED = 0
    KILL_TIMEOUT = 1
    KILL_ABNORMAL_EXIT = 2


def still_testing(start_time_for_overall_testing: float,
                  time_of_last_kill: float,
                  total_test_time: int,
                  maximum_time_since_last_kill: int) -> bool:
    if 0 < total_test_time < int(time.time() - start_time_for_overall_testing):
        return False
    if 0 < maximum_time_since_last_kill < int(time.time() - time_of_last_kill):
        return False
    return True


def run_amber_test(amber_test_file: Path,
                   icd_path: Path,
                   timeout_seconds: int,
                   extra_env: Optional[dict] = None) -> Optional[ProcessResult]:
    env = os.environ.copy()
    env["VK_ICD_FILENAMES"] = str(icd_path)
    if extra_env is not None:
        env.update(extra_env)
    cmd = [str(AMBER_BINARY), str(amber_test_file), "--disable-spirv-val", "-d"]
    return run_process_with_timeout(cmd=cmd, timeout_seconds=timeout_seconds, env=env)


def run_amber_test_with_mutant(mutant: int,
                               amber_test_file: Path,
                               timeout_seconds: int) -> KillStatus:
    result: Optional[ProcessResult] = run_amber_test(
        amber_test_file=amber_test_file,
        icd_path=MESA_MUTATED_ICD,
        timeout_seconds=timeout_seconds,
        extra_env={"DREDD_ENABLED_MUTATION": str(mutant)})
    if result is None:
        return KillStatus.KILL_TIMEOUT
    if result.returncode != 0:
        # This also covers segfaults and other abnormal terminations, which manifest as a
        # negative return code.
        return KillStatus.KILL_ABNORMAL_EXIT
    return KillStatus.SURVIVED


def main():
    start_time_for_overall_testing: float = time.time()
    time_of_last_kill: float = start_time_for_overall_testing

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
    parser.add_argument("--spirv_fuzz",
                        action="store_true",
                        help="If set, fuzz using spirv-fuzz rather than glsl-fuzz: the spirv-fuzz references and "
                             "donors are used, and gfauto_fuzz is invoked with a spirv-fuzz iteration instead of a "
                             "glsl-fuzz iteration.")
    parser.add_argument("--fuzz_timeout",
                        default=60,
                        help="Time in seconds to allow for a run of gfauto_fuzz to generate and validate a test.",
                        type=int)
    parser.add_argument("--run_timeout",
                        default=60,
                        help="Time in seconds to allow for running the generated Amber test under the mutant "
                             "tracking version of Mesa (without any mutation enabled).",
                        type=int)
    parser.add_argument("--run_timeout_multiplier",
                        default=5,
                        help="When running the Amber test with a mutant enabled, the timeout is this multiple of the "
                             "time the test took under the mutant tracking version of Mesa (to account for mutants "
                             "that lead to timeouts).",
                        type=int)
    parser.add_argument("--seed",
                        help="Seed for random number generator.",
                        type=int)
    parser.add_argument("--total_test_time",
                        default=86400,
                        help="Total time to allow for testing, in seconds. Default is 24 hours. To test indefinitely, "
                             "pass 0.",
                        type=int)
    parser.add_argument("--maximum_time_since_last_kill",
                        default=86400,
                        help="Cease testing if a kill has not occurred for this length of time. Default is 24 hours. "
                             "To test indefinitely, pass 0.",
                        type=int)
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

    if args.seed is not None:
        random.seed(args.seed)

    test_name_prefix: str = "spirv_fuzz_" if args.spirv_fuzz else "glsl_fuzz_"

    # Set up an environment equivalent to having activated the gfauto virtual environment.
    gfauto_environment = os.environ.copy()
    gfauto_environment["VIRTUAL_ENV"] = str(GFAUTO_VENV_BIN.parent)
    gfauto_environment["PATH"] = str(GFAUTO_VENV_BIN) + os.pathsep + gfauto_environment["PATH"]
    # Ensure any PYTHONHOME setting does not interfere with the virtual environment.
    gfauto_environment.pop("PYTHONHOME", None)

    # Make a unique working directory for this process under the gfauto fuzzing root, and populate
    # it with the settings file (which refers to already-downloaded binaries by absolute path, so
    # that they are re-used rather than re-downloaded), and the relevant references and donors.
    unique_work_dir: Path = Path(tempfile.mkdtemp(prefix="fuzzing_process_", dir=str(GFAUTO_FUZZING_ROOT)))
    print(f"Unique working directory for this process: {unique_work_dir}")
    shutil.copy(src=GFAUTO_FUZZING_ROOT / "settings.json", dst=unique_work_dir / "settings.json")
    if args.spirv_fuzz:
        directories_to_copy = ["spirv_fuzz_references", "spirv_fuzz_donors"]
    else:
        directories_to_copy = ["references", "donors"]
    for directory in directories_to_copy:
        shutil.copytree(src=GFAUTO_FUZZING_ROOT / directory, dst=unique_work_dir / directory)

    gfauto_temp_dir: Path = unique_work_dir / "temp"
    mutant_tracking_file: Path = unique_work_dir / "__dredd_covered_mutants"

    killed_mutants: Set[int] = set()
    unkilled_mutants: Set[int] = set(range(0, mutation_tree.num_mutations))

    # Make a work directory in which information about the mutant killing process will be stored. If this already
    # exists that's OK - there may be other processes working on mutant killing, or we may be continuing a job that
    # crashed previously.
    Path("work").mkdir(exist_ok=True)
    Path("work/tests").mkdir(exist_ok=True)
    Path("work/killed_mutants").mkdir(exist_ok=True)

    while still_testing(total_test_time=args.total_test_time,
                        maximum_time_since_last_kill=args.maximum_time_since_last_kill,
                        start_time_for_overall_testing=start_time_for_overall_testing,
                        time_of_last_kill=time_of_last_kill):

        # Pick an iteration seed, and try to create a directory for the associated test. It is very unlikely that
        # the directory already exists, but this could happen if two test workers pick the same seed. If it does
        # happen, this worker silently moves on to another seed.
        iteration_seed: int = random.randint(0, 2 ** 32 - 1)
        test_name: str = test_name_prefix + str(iteration_seed)
        test_output_directory: Path = Path("work/tests/" + test_name)
        try:
            test_output_directory.mkdir()
        except FileExistsError:
            continue

        # Clear out state from the previous iteration.
        if gfauto_temp_dir.exists():
            shutil.rmtree(gfauto_temp_dir)
        if mutant_tracking_file.exists():
            os.remove(mutant_tracking_file)

        # Generate a test using gfauto, from within this process's unique working directory.
        gfauto_fuzz_cmd: List[str] = [str(GFAUTO_VENV_BIN / "gfauto_fuzz"),
                                      "--iteration_seed", str(iteration_seed),
                                      "--keep_temp",
                                      "--glsl_fuzz_iterations", "0" if args.spirv_fuzz else "1",
                                      "--spirv_fuzz_iterations", "1" if args.spirv_fuzz else "0"]
        gfauto_fuzz_result: Optional[ProcessResult] = run_process_with_timeout(
            cmd=gfauto_fuzz_cmd,
            timeout_seconds=args.fuzz_timeout,
            env=gfauto_environment,
            cwd=str(unique_work_dir))
        if gfauto_fuzz_result is None:
            print(f"gfauto_fuzz timed out (seed {iteration_seed})")
            continue
        gfauto_fuzz_stdout: str = gfauto_fuzz_result.stdout.decode('utf-8')
        if gfauto_fuzz_result.returncode != 0 \
                or "STATUS SUCCESS" not in gfauto_fuzz_stdout \
                or "Stopping due to iteration_seed" not in gfauto_fuzz_stdout:
            print(f"gfauto_fuzz did not succeed (seed {iteration_seed})")
            print(f"stdout: {gfauto_fuzz_stdout}")
            print(f"stderr: {gfauto_fuzz_result.stderr.decode('utf-8')}")
            continue

        # The temp directory should now contain exactly one subdirectory, with a hex name. That subdirectory
        # should contain a no_opt test and an opt_O test; one of these is selected at random.
        temp_subdirectories: List[Path] = [entry for entry in gfauto_temp_dir.iterdir() if entry.is_dir()] \
            if gfauto_temp_dir.exists() else []
        if len(temp_subdirectories) != 1:
            print(f"Expected exactly one subdirectory under {gfauto_temp_dir}; found {len(temp_subdirectories)}.")
            continue
        hex_name: str = temp_subdirectories[0].name
        chosen_test: str = hex_name + ("_no_opt_test" if random.randint(0, 1) == 0 else "_opt_O_test")
        amber_file_from_gfauto: Path = \
            gfauto_temp_dir / hex_name / chosen_test / "results" / "host" / "result" / "test.amber"
        if not amber_file_from_gfauto.exists():
            print(f"Expected Amber test file {amber_file_from_gfauto} does not exist.")
            continue

        # Copy the Amber test to the shared work directory - this is the analogue of the Csmith program.
        amber_test_file: Path = (test_output_directory / "test.amber").resolve()
        shutil.copy(src=amber_file_from_gfauto, dst=amber_test_file)

        # Record time at which consideration of this test started
        analysis_timestamp_start: datetime.datetime = datetime.datetime.now()

        # Run the test using the version of Mesa that has been compiled with mutant tracking, to determine
        # which mutants the test covers. Time how long this takes: the timeout used when running the test
        # with a mutant enabled is a multiple of this.
        tracking_run_start: float = time.time()
        tracking_run_result: Optional[ProcessResult] = run_amber_test(
            amber_test_file=amber_test_file,
            icd_path=MESA_MUTANT_TRACKING_ICD,
            timeout_seconds=args.run_timeout,
            extra_env={"DREDD_MUTANT_TRACKING_FILE": str(mutant_tracking_file)})
        tracking_run_end: float = time.time()
        run_time: float = tracking_run_end - tracking_run_start

        if tracking_run_result is None:
            print("Mutant tracking run of Amber timed out.")
            continue
        if tracking_run_result.returncode != 0:
            print("Mutant tracking run of Amber failed.")
            print(f"stdout: {tracking_run_result.stdout.decode('utf-8')}")
            print(f"stderr: {tracking_run_result.stderr.decode('utf-8')}")
            continue
        if not mutant_tracking_file.exists():
            print("Mutant tracking run of Amber did not produce a coverage file.")
            continue

        # The timeout used when running the test with a mutant enabled, to account for mutants that lead
        # to Amber running for a long time or hanging.
        timeout_for_mutant_runs: int = max(1, int(args.run_timeout_multiplier * run_time) + 1)

        # Load file contents into a list. We go from list to set to list to eliminate duplicates.
        with open(mutant_tracking_file, 'r') as covered_mutants_file:
            covered_by_this_test: List[int] = list(set([int(line.strip())
                                                        for line in covered_mutants_file]))
        covered_by_this_test.sort()
        candidate_mutants_for_this_test: List[int] = ([m for m in covered_by_this_test if m not in killed_mutants])
        print("Number of mutants to try: " + str(len(candidate_mutants_for_this_test)))

        already_killed_by_other_tests: List[int] = ([m for m in covered_by_this_test if m in killed_mutants])
        killed_by_this_test: List[int] = []
        covered_but_not_killed_by_this_test: List[int] = []

        for mutant in candidate_mutants_for_this_test:

            if not still_testing(total_test_time=args.total_test_time,
                                 maximum_time_since_last_kill=args.maximum_time_since_last_kill,
                                 start_time_for_overall_testing=start_time_for_overall_testing,
                                 time_of_last_kill=time_of_last_kill):
                break

            mutant_path = Path("work/killed_mutants/" + str(mutant))
            if mutant_path.exists():
                print("Skipping mutant " + str(mutant) + " as it is noted as already killed.")
                unkilled_mutants.remove(mutant)
                killed_mutants.add(mutant)
                already_killed_by_other_tests.append(mutant)
                continue
            print("Trying mutant " + str(mutant))
            mutant_result: KillStatus = run_amber_test_with_mutant(mutant=mutant,
                                                                   amber_test_file=amber_test_file,
                                                                   timeout_seconds=timeout_for_mutant_runs)
            print("Mutant result: " + str(mutant_result))
            if mutant_result == KillStatus.SURVIVED:
                covered_but_not_killed_by_this_test.append(mutant)
                continue

            unkilled_mutants.remove(mutant)
            killed_mutants.add(mutant)
            killed_by_this_test.append(mutant)
            time_of_last_kill = time.time()
            print(f"Kill! Mutants killed so far: {len(killed_mutants)}")
            try:
                mutant_path.mkdir()
                print("Writing kill info to file.")
                with open(mutant_path / "kill_info.json", "w") as outfile:
                    json.dump({"killing_test": test_name,
                               "kill_type": str(mutant_result),
                               "kill_timestamp": str(datetime.datetime.now()),
                               }, outfile)
            except FileExistsError:
                print(f"Mutant {mutant} was independently discovered to be killed.")
                continue

        terminating_test_process: bool = not still_testing(
            total_test_time=args.total_test_time,
            maximum_time_since_last_kill=args.maximum_time_since_last_kill,
            start_time_for_overall_testing=start_time_for_overall_testing,
            time_of_last_kill=time_of_last_kill)

        all_considered_mutants = killed_by_this_test \
            + covered_but_not_killed_by_this_test \
            + already_killed_by_other_tests
        all_considered_mutants.sort()

        if covered_by_this_test != all_considered_mutants:
            assert terminating_test_process
            terminated_early: bool = True
        else:
            terminated_early: bool = False

        killed_by_this_test.sort()
        covered_but_not_killed_by_this_test.sort()
        already_killed_by_other_tests.sort()

        # Record time at which consideration of this test ended
        analysis_timestamp_end: datetime.datetime = datetime.datetime.now()

        with open(test_output_directory / "kill_summary.json", "w") as outfile:
            json.dump({"terminated_early": terminated_early,
                       "covered_mutants_count": len(covered_by_this_test),
                       "killed_mutants": killed_by_this_test,
                       "skipped_mutants_count": len(already_killed_by_other_tests),
                       "survived_mutants_count": len(covered_but_not_killed_by_this_test),
                       "analysis_start_time": str(analysis_timestamp_start),
                       "analysis_end_time": str(analysis_timestamp_end),
                       }, outfile)


if __name__ == '__main__':
    main()
