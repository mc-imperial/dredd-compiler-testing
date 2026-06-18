import argparse
import jinja2
import json
import os
import shutil
import stat
import sys
import subprocess
import signal
import datetime

from dredd_test_runners.common.constants import (DEFAULT_RUNTIME_TIMEOUT,
                                                 MIN_TIMEOUT_FOR_MUTANT_COMPILATION,
                                                 TIMEOUT_MULTIPLIER_FOR_MUTANT_COMPILATION,
                                                 MIN_TIMEOUT_FOR_MUTANT_EXECUTION,
                                                 TIMEOUT_MULTIPLIER_FOR_MUTANT_EXECUTION)
from dredd_test_runners.common.run_process_with_timeout import ProcessResult, run_process_with_timeout

from pathlib import Path
from typing import Dict, List, Optional


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir",
                        help="Directory containing test results. It should have subdirectories, 'tests' and "
                             "'killed_mutants'.",
                        type=Path)
    parser.add_argument("mutated_compiler_executable",
                        help="Path to the executable for the Dredd-mutated compiler.",
                        type=Path)
    parser.add_argument("csmith_root",
                        help="Path to Csmith checkout, built in 'build' directory under this path.",
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

    killed_mutant_to_test_info: Dict[int, Dict] = {}

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
                # Test case reduction may be feasible and useful for this kill.
                killed_mutant_to_test_info[mutant] = mutant_summary
    
    reduction_queue: List[int] = list(killed_mutant_to_test_info.keys())
    reduction_queue.sort()

    reductions_dir: Path = work_dir / "reductions"
    Path(reductions_dir).mkdir(exist_ok=True)
        
    while reduction_queue:
        mutant_to_reduce = reduction_queue.pop(0)
        current_reduction_dir: Path = reductions_dir / str(mutant_to_reduce)
        try:
            current_reduction_dir.mkdir()
        except FileExistsError:
            print(f"Skipping reduction for mutant {mutant_to_reduce} as {current_reduction_dir} already exists.")
            continue

        mutant_summary = killed_mutant_to_test_info[mutant_to_reduce]

        print(f"Preparing to reduce mutant {mutant_to_reduce}. Details: {mutant_summary}")

        is_yarpgen_testcase = mutant_summary["killing_test"].startswith("yarpgen")

        if is_yarpgen_testcase:
            # inline init.h in func.c
            with open(tests_dir / killed_mutant_to_test_info[mutant_to_reduce]['killing_test'] / 'init.h', 'r') as init_f:
                init_content = init_f.read()
            with open(tests_dir / killed_mutant_to_test_info[mutant_to_reduce]['killing_test'] / 'func.c', 'r') as func_f:
                func_content = func_f.read()
            combined_file_content = func_content.replace('#include "init.h"', init_content)

            # Add SENTINEL comment to add as seperator btween func.c and driver.c
            combined_file_content += "\n// SENTINEL\n"

            # Add content of driver.c
            with open(tests_dir / killed_mutant_to_test_info[mutant_to_reduce]['killing_test'] / 'driver.c', 'r') as driver_f:
                combined_file_content += driver_f.read()

            # write the combined file into combined.c
            with open(current_reduction_dir / 'combined.c', 'w') as combined_f:
                combined_f.write(combined_file_content)
            program_to_check = "combined.c"
        else:
            shutil.copy(src=tests_dir / killed_mutant_to_test_info[mutant_to_reduce]['killing_test'] / 'prog.c',
                    dst=current_reduction_dir / 'prog.c')
            program_to_check = "prog.c"

        interestingness_test_template_file = \
            "interesting_crash.py.template"\
            if mutant_summary['kill_type'] == 'KillStatus.KILL_COMPILER_CRASH'\
            else "interesting_miscompilation.py.template"

        interestingness_test_template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(
                searchpath=os.path.dirname(os.path.realpath(__file__)))).get_template(interestingness_test_template_file)
        open(current_reduction_dir / 'interesting.py', 'w').write(interestingness_test_template.render(
            program_to_check=program_to_check,
            mutated_compiler_executable=args.mutated_compiler_executable,
            csmith_root=args.csmith_root,
            mutation_ids=str(mutant_to_reduce),
            min_timeout_for_mutant_compilation=MIN_TIMEOUT_FOR_MUTANT_COMPILATION,
            timeout_multiplier_for_mutant_compilation=TIMEOUT_MULTIPLIER_FOR_MUTANT_COMPILATION,
            min_timeout_for_mutant_execution=MIN_TIMEOUT_FOR_MUTANT_EXECUTION,
            timeout_multiplier_for_mutant_execution=TIMEOUT_MULTIPLIER_FOR_MUTANT_EXECUTION,
            default_runtime_timeout=DEFAULT_RUNTIME_TIMEOUT,
            csmith_original_warnings_dir=os.path.abspath(current_reduction_dir)
        ))

        # Make the interestingness test executable.
        st = os.stat(current_reduction_dir / 'interesting.py')
        os.chmod(current_reduction_dir / 'interesting.py', st.st_mode | stat.S_IEXEC)

        # Run creduce with 12 hour timeout and store in logfile
        reduction_start_time: datetime.datetime = datetime.datetime.now()
        reduction_status = ""
        with open(os.path.join(current_reduction_dir, 'reduction_log.txt'), 'wb') as logfile:
            try:
                creduce_proc = subprocess.Popen(['creduce', 'interesting.py', program_to_check, '--n', '1'],
                                              cwd=current_reduction_dir, stdout=logfile, stderr=logfile,
                                              start_new_session=True)
                creduce_proc.wait(timeout=43200)
                if creduce_proc.returncode != 0:
                    print(f"Reduction of {mutant_to_reduce} failed with exit code {creduce_proc.returncode}")
                    reduction_status = "FAILED"
                else:
                    print(f"Reduction of {mutant_to_reduce} succeed.")
                    reduction_status = "SUCCESS"
            except subprocess.TimeoutExpired:
                print(f"Reduction of {mutant_to_reduce} timed out.")
                reduction_status = "TIMEOUT"
                os.killpg(os.getpgid(creduce_proc.pid), signal.SIGTERM)
            except Exception as exp:
                print(f"Reduction of {mutant_to_reduce} failed with an exception: {exp}")
                reduction_summary = "EXCPETION"
                os.killpg(os.getpgid(creduce_proc.pid), signal.SIGTERM)
        reduction_end_time: datetime.datetime = datetime.datetime.now()

        # Split the combined file into seperate file in necessary
        if is_yarpgen_testcase:
            with open(current_reduction_dir / 'combined.c', 'r') as combined_f:
                combined_file_content = combined_f.read()
            seperated_contents = combined_file_content.split("// SENTINEL\n")
            if len(seperated_contents) < 2:
                # SENTINEL comment not present in combined file
                raise Exception("SENTINEL comment not present in combined file.")
            for i in range(len(seperated_contents)):
                with open(current_reduction_dir / f"file_{i}.c", 'w') as seperated_f:
                    seperated_f.write(seperated_contents[i])
            os.remove(current_reduction_dir / 'combined.c')


        # Store reduction information
        with open(os.path.join(current_reduction_dir, 'reduction_summary.json'), 'w') as summary_file:
            json.dump({"reduction_status": reduction_status,
                       "reduction_start_time": str(reduction_start_time),
                       "reduction_end_time": str(reduction_end_time),
                       }, summary_file)


if __name__ == '__main__':
    main()
