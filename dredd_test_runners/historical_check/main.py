import argparse
import json
import sys
import tempfile
import subprocess
import os
import re

from functools import partial
from pathlib import Path
from typing import Dict
from multiprocessing import Pool

from dredd_test_runners.historical_check.get_clang_llvm_releases import get_clang_llvm_releases

def check_compiler_with_test(csmith_root: Path, compiler_path: Path, test_dir_path: Path) -> bool:
    # print(f"Checking {compiler_path} on {test_dir_path}")
    compiler_args = ["-I", f"{csmith_root}/runtime", "-I", f"{csmith_root}/build/runtime", "-pedantic", "-Wall", "-fPIC"]

    test_prog_path = list(test_dir_path.glob('*.c'))
    if len(test_prog_path) == 0:
        print(f"No compilable file in {str(test_dir_path)}")
        return False
    
    with tempfile.TemporaryDirectory() as tmpdir:
        proc = subprocess.run([compiler_path, *compiler_args, "-O3", *test_prog_path, "-c"], cwd=tmpdir, capture_output=True)
        if proc.returncode != 0:
            print(f'Compilation failed for {compiler_path} with testcase {test_dir_path}:')
            print(proc.stderr.decode())
            return False

        reference_output_path = test_dir_path / 'prog.reference_output'
        is_miscompilation_test = reference_output_path.exists()

        if is_miscompilation_test:
            object_files = [os.path.basename(f).replace('.c','.o') for f in test_prog_path]
            proc = subprocess.run(['clang-15', *compiler_args, *object_files, "-o", "prog.exe"], cwd=tmpdir, capture_output=True)
            if proc.returncode != 0:
                print(f'Linking failed for {compiler_path} with testcase {test_dir_path}:')
                print(proc.stderr.decode())
                return False

            proc = subprocess.run(['./prog.exe'], cwd=tmpdir, capture_output=True)
            if proc.returncode != 0:
                print(f'Execution failed for {compiler_path} with testcase {test_dir_path}:')
                print(proc.stderr.decode())
                return False

            with open(reference_output_path, 'rb') as f:
                reference_output = f.read()
            if proc.stdout != reference_output:
                print(f'Comparison failed for {compiler_path} with testcase {test_dir_path}:')
                return False

            # print(f"Miscompilation check {compiler_path} on {test_dir_path} succeed")
        # else:
        #     print(f"Crash check {compiler_path} on {test_dir_path} succeed")
        
        return True

    print(compiler_path, test_dir_path)

def check_version_with_testsuite(version_url: str, testsuite: Path, csmith_root: Path):
    version_tar_name = version_url.split('/')[-1]
    pattern = r'(\.tar\.gz|\.tar\.bz2|\.tar\.xz|\.gz|\.bz2|\.xz)$'
    version_dir_name = re.sub(pattern, '', version_tar_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        proc = subprocess.run(["curl", "-Lo", version_tar_name, version_url], cwd=tmpdir, capture_output=True)
        if proc.returncode != 0:
            print(proc.stderr.decode())
            return

        version_path = Path(tmpdir) / version_dir_name
        version_path.mkdir()

        proc = subprocess.run(["tar", "-xf", version_tar_name, '-C', version_path, '--strip-components=1'], cwd=tmpdir, capture_output=True)
        if proc.returncode != 0:
            print(proc.stderr.decode())
            return

        compiler_path = version_path / "bin" / "clang"

        if not compiler_path.exists():
            print(f"Compiler path {compiler_path} doesn't exist.")
            return 

        test_dirs = testsuite.glob('*')
        with Pool() as pool:
            test_result = pool.map(partial(check_compiler_with_test, csmith_root, compiler_path), test_dirs)
        print(f"RESULT OF {version_dir_name}: {sum(test_result)}/{len(test_result)}")
        # for test_dir in test_dirs:
        #     check_compiler_with_test(csmith_root, compiler_path, test_dir)

    return


        

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir",
                        help="Directory containing test results. It should have subdirectories, 'testsuite'.",
                        type=Path)
    parser.add_argument("version",
                        help="The LLVM version for which the testsuite is being grown from (e.g., 14.0.0).",
                        type=str)
    parser.add_argument("csmith_root", help="Path to a checkout of Csmith, assuming that it has been built under "
                                            "'build' beneath this directory.",
                        type=Path)
    args = parser.parse_args()

    testsuite_dir = args.work_dir.resolve() / "testsuite"
    if not testsuite_dir.exists() or not testsuite_dir.is_dir():
        print(f"Error: {str(testsuite_dir)} does not exist.")
        sys.exit(1)

    future_versions = get_clang_llvm_releases(args.version)

    for version_url in future_versions:
        check_version_with_testsuite(version_url, testsuite_dir, args.csmith_root)

    pass

if __name__ == '__main__':
    main()



