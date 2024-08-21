# =============================================================================
# Copyright (c) 2018-2024, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing permissions and limitations under
# the License.
# =============================================================================

find_package(Threads REQUIRED)

add_library(cudf_datagen STATIC common/generate_input.cu)
target_compile_features(cudf_datagen PUBLIC cxx_std_17 cuda_std_17)

target_compile_options(
  cudf_datagen PUBLIC "$<$<COMPILE_LANGUAGE:CXX>:${CUDF_CXX_FLAGS}>"
                      "$<$<COMPILE_LANGUAGE:CUDA>:${CUDF_CUDA_FLAGS}>"
)

target_link_libraries(
  cudf_datagen
  PUBLIC GTest::gmock GTest::gtest benchmark::benchmark nvbench::nvbench Threads::Threads cudf
         cudftestutil nvtx3::nvtx3-cpp
  PRIVATE $<TARGET_NAME_IF_EXISTS:conda_env>
)

target_include_directories(
  cudf_datagen
  PUBLIC "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}>" "$<BUILD_INTERFACE:${CUDF_SOURCE_DIR}>"
         "$<BUILD_INTERFACE:${CUDF_SOURCE_DIR}/src>"
)

# ##################################################################################################
# * compiler function -----------------------------------------------------------------------------

# Use an OBJECT library so we only compile these helper source files only once
add_library(
  cudf_benchmark_common OBJECT
  "${CUDF_SOURCE_DIR}/tests/utilities/random_seed.cpp"
  synchronization/synchronization.cpp
  io/cuio_common.cpp
  common/table_utilities.cpp
  common/benchmark_utilities.cpp
  common/nvbench_utilities.cpp
)
target_link_libraries(cudf_benchmark_common PRIVATE cudf_datagen $<TARGET_NAME_IF_EXISTS:conda_env>)
add_custom_command(
  OUTPUT CUDF_BENCHMARKS
  COMMAND echo Running benchmarks
  COMMAND mkdir -p results
  VERBATIM
  COMMENT "Running cudf benchmarks."
  USES_TERMINAL
)

# This function takes in a benchmark name and benchmark source and handles setting all of the
# associated properties and linking to build the benchmark
function(ConfigureBench CMAKE_BENCH_NAME)
  add_executable(${CMAKE_BENCH_NAME} ${ARGN})
  set_target_properties(
    ${CMAKE_BENCH_NAME}
    PROPERTIES RUNTIME_OUTPUT_DIRECTORY "$<BUILD_INTERFACE:${CUDF_BINARY_DIR}/benchmarks>"
               INSTALL_RPATH "\$ORIGIN/../../../lib"
               CXX_STANDARD 17
               CXX_STANDARD_REQUIRED ON
               # For std:: support of __int128_t. Can be removed once using cuda::std
               CXX_EXTENSIONS ON
               CUDA_STANDARD 17
               CUDA_STANDARD_REQUIRED ON
  )
  target_link_libraries(
    ${CMAKE_BENCH_NAME} PRIVATE cudf_benchmark_common cudf_datagen benchmark::benchmark_main
                                $<TARGET_NAME_IF_EXISTS:conda_env>
  )
  add_custom_command(
    OUTPUT CUDF_BENCHMARKS
    COMMAND ${CMAKE_BENCH_NAME} --benchmark_out_format=json
            --benchmark_out=results/${CMAKE_BENCH_NAME}.json
    APPEND
    COMMENT "Adding ${CMAKE_BENCH_NAME}"
  )

  install(
    TARGETS ${CMAKE_BENCH_NAME}
    COMPONENT testing
    DESTINATION bin/benchmarks/libcudf
    EXCLUDE_FROM_ALL
  )
endfunction()

# This function takes in a benchmark name and benchmark source for nvbench benchmarks and handles
# setting all of the associated properties and linking to build the benchmark
function(ConfigureNVBench CMAKE_BENCH_NAME)
  add_executable(${CMAKE_BENCH_NAME} ${ARGN} fixture/nvbench_main.cpp)
  set_target_properties(
    ${CMAKE_BENCH_NAME}
    PROPERTIES RUNTIME_OUTPUT_DIRECTORY "$<BUILD_INTERFACE:${CUDF_BINARY_DIR}/benchmarks>"
               INSTALL_RPATH "\$ORIGIN/../../../lib"
  )
  target_link_libraries(
    ${CMAKE_BENCH_NAME} PRIVATE cudf_benchmark_common cudf_datagen nvbench::nvbench
                                $<TARGET_NAME_IF_EXISTS:conda_env>
  )
  install(
    TARGETS ${CMAKE_BENCH_NAME}
    COMPONENT testing
    DESTINATION bin/benchmarks/libcudf
    EXCLUDE_FROM_ALL
  )
endfunction()

# ##################################################################################################
# * join benchmark --------------------------------------------------------------------------------
ConfigureNVBench(
  JOIN_NVBENCH join/left_join.cu join/conditional_join.cu join/join.cu join/mixed_join.cu
  join/distinct_join.cu
)

# ##################################################################################################
# * groupby benchmark -----------------------------------------------------------------------------
ConfigureBench(
  GROUPBY_BENCH groupby/group_sum.cpp groupby/group_nth.cpp groupby/group_shift.cpp
  groupby/group_struct_values.cpp groupby/group_no_requests.cpp groupby/group_scan.cpp
)

ConfigureNVBench(
  GROUPBY_NVBENCH groupby/group_max.cpp groupby/group_max_multithreaded.cpp
  groupby/group_nunique.cpp groupby/group_rank.cpp groupby/group_struct_keys.cpp
)

add_custom_target(
  run_benchmarks
  DEPENDS CUDF_BENCHMARKS
  COMMENT "Custom command for running cudf benchmarks."
)
