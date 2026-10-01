#!/usr/bin/env bash

# Stop at the first error
set -e

# Choose one of the options
use_vpc_only=true
use_pdal=false
use_wrench=false
use_dl_then_wrench=false

# pdal_wrench build_vpc fetches the COPC header of every tile over HTTP(S).
# Running concurrent jobs triggers 429 Too Many Requests from data.geopf.fr,
# so this step runs sequentially (one job at a time) with a small delay
# between tiles. Raise build_vpc_parallel_jobs if data.geopf.fr tolerates it.
build_vpc_parallel_jobs=1
build_vpc_delay_seconds=0.5

# Print a quick succeeded/failed summary of a build_vpc parallel run from its joblog
print_build_vpc_summary() {
    local joblog=$1
    local total=$(($(wc -l < "$joblog") - 1))
    local failed=$(awk 'NR>1 && $7!=0' "$joblog" | wc -l | tr -d ' ')
    echo "build_vpc summary: $((total-failed))/$total chunks succeeded, $failed failed (see $joblog and build_vpc_logs/*.log)"
}

# Compile and install PDAL wrench if needed
if $use_vpc_only || $use_wrench || $use_dl_then_wrench; then
    wget https://github.com/PDAL/wrench/archive/refs/tags/v1.1.tar.gz
    tar -zxvf v1.1.tar.gz
    cd wrench-1.1
    mkdir build
    cd build
    cmake -DCMAKE_INSTALL_PREFIX=$CONDA_PREFIX -DCMAKE_PREFIX_PATH=$CONDA_PREFIX ..
    make -j $(nproc)
    make install
    cd ../..
fi

# Option 0 : Build a single VPC referencing all LIDAR HD COPC files
if $use_vpc_only; then
    # Query the IGNF Geoplateforme WFS for all LIDAR HD COPC file urls (mainland
    # France and overseas territories), chunk them and generate one
    # pdal_wrench build_vpc command per chunk
    rm -rf block_urls_dir vpc build_vpc_logs build_vpc_commands.txt full.vpc
    mkdir block_urls_dir vpc build_vpc_logs
    python3 scripts/generate_build_vpc_commands_wfs.py --limit 100 --output_urls_dir block_urls_dir --output_log_dir build_vpc_logs --output_dir vpc

    # Build one VPC per chunk in parallel, throttled to avoid 429s from
    # data.geopf.fr (lengthy process, ~1.5h). --bar shows live progress/ETA.
    # Some chunks can permanently fail (e.g. a tile removed from
    # data.geopf.fr, returned as 404), so parallel's exit code is ignored
    # ("|| true") to let the script continue to the summary/merge step below
    # with whatever chunks succeeded, instead of aborting the whole run.
    parallel --bar --joblog build_vpc_logs/parallel.log -j $build_vpc_parallel_jobs --delay $build_vpc_delay_seconds < build_vpc_commands.txt || true
    print_build_vpc_summary build_vpc_logs/parallel.log

    # Merge chunk VPCs into a single full.vpc
    python3 scripts/merge_vpc_files.py --input_dir vpc --output full.vpc
fi

# Option 1 : Using PDAL pipelines in parallel to create tiles
if $use_pdal; then
    # Generate an index of COPC files in S3 as a GPKG file
    rm -rf copc_index.gpkg
    python3 scripts/generate_copc_index.py --output copc_index.gpkg

    # Generate PDAL pipelines files using this index and the actual quadtree structure
    rm -rf pdal_pipelines pdal_pc_tiles pdal_logs
    mkdir pdal_pipelines pdal_pc_tiles pdal_logs
    python3 scripts/generate_pdal_pipelines.py \
            --input_file actual_quadtree_structure.gpkg \
            --output_pipelines_dir pdal_pipelines \
            --output_tiles_dir pdal_pc_tiles \
            --output_log_dir pdal_logs \
            --tile_index copc_index.gpkg

    # Create pointcloud tiles in parallel using all cores
    time parallel --joblog pdal_logs/parallel.log -j $(nproc) < pdal_commands.txt
fi

# Option 2 : Use PDAL wrench to create tiles
if $use_wrench; then
    # Create a full VPC of all LIDAR HD COPC files (urls from the IGNF WFS)
    rm -rf block_urls_dir vpc build_vpc_logs
    mkdir block_urls_dir vpc build_vpc_logs
    python3 scripts/generate_build_vpc_commands_wfs.py --output_urls_dir block_urls_dir --output_log_dir build_vpc_logs --output_dir vpc

    # creating VPC ... is a lengthy process (~1.5h), throttled to avoid 429s
    # from data.geopf.fr. --bar shows live progress/ETA. Some chunks can
    # permanently fail (e.g. a tile removed from data.geopf.fr, returned as
    # 404), so parallel's exit code is ignored ("|| true") to continue to
    # the summary/merge step with whatever chunks succeeded.
    parallel --bar --joblog build_vpc_logs/parallel.log -j $build_vpc_parallel_jobs --delay $build_vpc_delay_seconds < build_vpc_commands.txt || true
    print_build_vpc_summary build_vpc_logs/parallel.log
    python3 scripts/merge_vpc_files.py

    # Try running sequential pdal wrench DOES NOT WORK in terms of performance
    rm -rf wrench_bbox wrench_logs wrench_pc_tiles
    mkdir wrench_bbox wrench_logs wrench_pc_tiles
    python3 scripts/generate_pdal_wrench_commands.py
    parallel --joblog wrench_logs/parallel.log -j 1 < wrench_commands.txt
fi

# Option 3 : Download directly the needed COPC files and use PDAL wrench commands to create tiles
# TODO: More tests to be done here
if $use_dl_then_wrench; then
    # Generate an index of COPC files in S3 as a GPKG file
    rm -rf copc_index.gpkg
    python3 scripts/generate_copc_index.py --output copc_index.gpkg

    # Create wget commands and download
    rm -rf raw_tiles
    mkdir raw_tiles
    python3 scripts/generate_wget_commands.py --output_dir raw_tiles
    parallel --joblog wget_joblog.log -j $(nproc) < wget_commands.txt

    # Create list of downloaded tiles
    ls raw_tiles/*.laz > file_list.txt
    # Create a VPC for the downloaded files
    pdal_wrench build_vpc --output=raw_tiles.vpc --input-file-list=file_list.txt

    # Create PDAL wrench commands and use them sequentially
    # TODO: Test with clip commands sequentially, and PDAL wrench merge and PDAL pipeline in parallel.
    rm -rf wrench_bbox wrench_logs wrench_pc_tilest
    mkdir wrench_bbox wrench_logs wrench_pc_tiles
    python3 scripts/generate_pdal_wrench_commands.py --input_vpc_file raw_tiles.vpc
    parallel --joblog wrench_logs/parallel.log -j 1 < wrench_commands.txt
fi
