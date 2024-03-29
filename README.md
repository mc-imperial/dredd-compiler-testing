# dredd-compiler-testing

Scripts to allow the Dredd mutation testing framework to be used to generate test cases that improve mutation coverage.

##

Necessary packages on AWS EC2:

```
sudo apt update
sudo apt install -y python3-pip python3.10-venv unzip zip cmake clang-15 ninja-build libzstd-dev m4
pip3 install --upgrade pip
pip3 install build
```

## Set some environment variables

Decide where the root of the experiments should be. Everything that follows will be checked out / performed under this location. E.g.:

```
export DREDD_EXPERIMENTS_ROOT=${HOME}
```

Decide which version of the LLVM project you would like to mutate and put this version in the `LLVM_VERSION` environment variable. E.g.:

```
export LLVM_VERSION=17.0.4
```


## Get Dredd and build it

```
cd ${DREDD_EXPERIMENTS_ROOT}
git clone --recursive https://github.com/mc-imperial/dredd.git
pushd dredd
    OS=ubuntu-22.04 DREDD_LLVM_SUFFIX=-stock-clang .github/workflows/install_clang.sh
popd
cmake -S dredd -B dredd/build -G Ninja -DCMAKE_C_COMPILER=clang-15 -DCMAKE_CXX_COMPILER=clang++-15
cmake --build dredd/build --target dredd
cp dredd/build/src/dredd/dredd dredd/third_party/clang+llvm/bin/
```


## Build mutated versions of clang

Check out this version of the LLVM project, and keep it as a clean version of the source code (from which versions of the source code to be mutated will be copied):

```
cd ${DREDD_EXPERIMENTS_ROOT}
git clone https://github.com/llvm/llvm-project.git llvm-${LLVM_VERSION}-clean
pushd llvm-${LLVM_VERSION}-clean
git checkout llvmorg-${LLVM_VERSION}
popd
```

Now make two copies of the LLVM project--one that will be mutated, and another that will be used for the tracking of covered mutants.

```
cp -r llvm-${LLVM_VERSION}-clean llvm-${LLVM_VERSION}-mutated
cp -r llvm-${LLVM_VERSION}-clean llvm-${LLVM_VERSION}-mutant-tracking
```

Generate a compilation database for each of these copies of LLVM, and build a core component so that all auto-generated code is in place for Dredd.

```
cd ${DREDD_EXPERIMENTS_ROOT}
for kind in mutated mutant-tracking
do
  SOURCE_DIR=llvm-${LLVM_VERSION}-${kind}/llvm
  BUILD_DIR=llvm-${LLVM_VERSION}-${kind}-build
  mkdir ${BUILD_DIR}
  cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" -G Ninja -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_CXX_FLAGS="-w" -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_PROJECTS="clang" -DCMAKE_C_COMPILER=clang-15 -DCMAKE_CXX_COMPILER=clang++-15
  # Build something minimal to ensure all auto-generated pieces of code are created.
  cmake --build "${BUILD_DIR}" --target LLVMCore
done
```

Record the location of the `dredd` executable in an environment variable.

```
export DREDD_EXECUTABLE=${DREDD_EXPERIMENTS_ROOT}/dredd/third_party/clang+llvm/bin/dredd
```

Mutate all `.cpp` files under `InstCombine` in the copy of LLVM designated for mutation:

```
cd ${DREDD_EXPERIMENTS_ROOT}
FILES_TO_MUTATE=($(ls llvm-${LLVM_VERSION}-mutated/llvm/lib/Transforms/InstCombine/*.cpp | sort))
echo ${FILES[*]}
${DREDD_EXECUTABLE} -p llvm-${LLVM_VERSION}-mutated-build/compile_commands.json --mutation-info-file llvm-mutated.json ${FILES_TO_MUTATE[*]}
```

Apply mutation tracking to all `.cpp` files under `InstCombine` in the copy of LLVM designated for mutation tracking:

```
cd ${DREDD_EXPERIMENTS_ROOT}
FILES_TO_MUTATE=($(ls llvm-${LLVM_VERSION}-mutant-tracking/llvm/lib/Transforms/InstCombine/*.cpp | sort))
echo ${FILES[*]}
${DREDD_EXECUTABLE} --only-track-mutant-coverage -p llvm-${LLVM_VERSION}-mutant-tracking-build/compile_commands.json --mutation-info-file llvm-mutant-tracking.json ${FILES_TO_MUTATE[*]}
```

Build entire LLVM project for both copies (this will take a long time):

```
cd ${DREDD_EXPERIMENTS_ROOT}
for kind in mutated mutant-tracking
do
  BUILD_DIR=llvm-${LLVM_VERSION}-${kind}-build
  cmake --build ${BUILD_DIR}
done
```

## Build and interactive install steps

```
cd ${DREDD_EXPERIMENTS_ROOT}
git clone https://github.com/mc-imperial/dredd-compiler-testing.git
pushd dredd-compiler-testing
python3 -m build
python3 -m pip install -e .
popd
```

## Scripts to figure out which Dredd-induced mutants are killed by the LLVM test suite

```
git clone https://github.com/llvm/llvm-test-suite.git
cd llvm-test-suite
git checkout llvmorg-${LLVM_VERSION}
cd ..
# Make sure that llvm-size is on your path. It is available from the just-built compiler, or from the compiler under dredd's third party directory. TODO: decide which one to use in instructions.
cmake -G Ninja -S llvm-test-suite -B llvm-test-suite-build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

You point it at:

- A checkout of the LLVM test suite
- A compilation database for the test suite
- The mutated compiler and mutant tracking compiler
- Associated JSON files with mutation info

It considers the tests in the suite in turn and determines which
mutants they kill.

Command to invoke `llvm-test-suite-runner`:

```
llvm-test-suite-runner llvm-mutated.json llvm-mutant-tracking.json llvm-${LLVM_VERSION}-mutated-build/bin llvm-${LLVM_VERSION}-mutant-tracking-build/bin $(pwd)/llvm-test-suite llvm-test-suite-build/compile_commands.json
```

To run many instances in parallel (16):

```
for i in `seq 1 16`; do llvm-test-suite-runner llvm-mutated.json llvm-mutant-tracking.json llvm-${LLVM_VERSION}-mutated-build/bin llvm-${LLVM_VERSION}-mutant-tracking-build/bin $(pwd)/llvm-test-suite llvm-test-suite-build/compile_commands.json & done
```

To kill them:

```
pkill -9 -f llvm-test-suite
```

Watch out for left over `clang` processes!




# LLVM regression test runner

```
for kind in mutated mutant-tracking
do
  pushd llvm-${LLVM_VERSION}-${kind}
    git apply ../dredd-compiler-testing/lit-patches/${kind}.patch
  popd
done
```

Command to invoke regression test suite runner:

```
llvm-regression-tests-runner llvm-mutated.json llvm-mutant-tracking.json llvm-${LLVM_VERSION}-mutated-build/bin llvm-${LLVM_VERSION}-mutant-tracking-build/bin llvm-${LLVM_VERSION}-mutated/llvm/test/Transforms/InstCombine llvm-${LLVM_VERSION}-mutant-tracking/llvm/test/Transforms/InstCombine
```

To run many instances in parallel (16):

```
for i in `seq 1 16`; do llvm-regression-tests-runner llvm-mutated.json llvm-mutant-tracking.json llvm-${LLVM_VERSION}-mutated-build/bin llvm-${LLVM_VERSION}-mutant-tracking-build/bin llvm-${LLVM_VERSION}-mutated/llvm/test/Transforms/InstCombine llvm-${LLVM_VERSION}-mutant-tracking/llvm/test/Transforms/InstCombine & done
```

To kill them: TODO


# Csmith runner

Get and build Csmith:

```
cd ${DREDD_EXPERIMENTS_ROOT}
git clone https://github.com/csmith-project/csmith.git
cmake -S csmith -B csmith/build -G Ninja
cmake --build csmith/build
```

```
csmith-runner llvm-mutated.json llvm-mutant-tracking.json llvm-${LLVM_VERSION}-mutated-build/bin/clang llvm-${LLVM_VERSION}-mutant-tracking-build/bin/clang ${DREDD_EXPERIMENTS_ROOT}/csmith
```

To run many instances in parallel (16):

```
for i in `seq 1 16`; do csmith-runner llvm-mutated.json llvm-mutant-tracking.json llvm-${LLVM_VERSION}-mutated-build/bin/clang llvm-${LLVM_VERSION}-mutant-tracking-build/bin/clang ${DREDD_EXPERIMENTS_ROOT}/csmith & done
```

To kill them:

```
pkill -9 -f csmith-runner
```


# Results analysis

To see a list of the Csmith tests that have led to "actionable" kills (kills for which test case reduction will lead to a runnable killing test case with oracle), do:

```
cd ${DREDD_EXPERIMENTS_ROOT}
analyse-results work
```

# Reductions

```
cd ${DREDD_EXPERIMENTS_ROOT}
reduce-new-kills work ${DREDD_EXPERIMENTS_ROOT}/llvm-${LLVM_VERSION}-mutated-build/bin/clang ${DREDD_EXPERIMENTS_ROOT}/csmith
```