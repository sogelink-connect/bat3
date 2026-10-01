> This was a first try using Geoflow and IGNF datasets. The new version using `roofer` is here : https://github.com/ignfab/roofer-with-ignf-datasets. This repository is now archived.

# ![Graphicloads-Battery-Battery-bar-5-full 32](https://github.com/ignfab/bat3/assets/5435148/e52b75a7-9f7d-4628-8ca4-e2f237c36910) BAT3

This project is a first try at generating 3D semantized building with [geoflow-bundle](https://github.com/geoflow3d/geoflow-bundle) at scale using [IGNF](https://www.ign.fr/) datasets and services.  
The working area is an [IGNF LIDAR HD](https://geoservices.ign.fr/lidarhd) block (50kmx50km).

## Workflow

The workflow is inspired by [geoflow-bundle](https://github.com/geoflow3d/geoflow-bundle) approach for generating at scale, also implemented in [Optim3D](https://github.com/Yarroudh/Optim3D).  
The main idea is to build a quadtree based on building footprints, tile the point cloud data accordingly and generate 3D buildings using the generated vector and point cloud tiles.  
To speedup the quadtree calculation, the process was implemented here in C++ using GDAL and CGAL.

## Datasets

### Building footprints

The building footprints used are those from [IGNF BDTOPO](https://geoservices.ign.fr/bdtopo) reference vector database.  
A pre-processing task will be added at some point to make sure the footprints perfectly overlay the point cloud data.

### Point cloud

The point cloud date are the newly acquired [IGNF LIDAR HD](https://geoservices.ign.fr/lidarhd) datasets.  
The data are tiled and preprocessed to merge the two building classes (class 6 and class 67).

## Downloading datasets

### BDTOPO building footprints

Two options are possible:
* Using IGNF [Geoplateforme WFS 2.0.0](https://data.geopf.fr/wfs/ows?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetCapabilities) limited to 5000 features per query (but offering paging)
* Downloading dataset archives per [french department](https://geoservices.ign.fr/bdtopo#telechargementgpkgdep) or [french region](https://geoservices.ign.fr/bdtopo#telechargementshpreg) as GPKG files compressed with 7z

In `01_vector_data.sh` the two options are available. WFS is used by default for this example.

### LIDAR HD point cloud

Classified LIDAR HD datasets are available in COPC format in an [OVH S3 bucket](https://storage.sbg.cloud.ovh.net/v1/AUTH_63234f509d6048bca3c9fd7928720ca1/ppk-lidar/).

Two command line tools can be used to query these COPC files and extract the tiles :
* [PDAL](https://github.com/PDAL/PDAL) by defining a pipeline for each tile
* [PDAL wrench](https://github.com/PDAL/wrench) an upgraded version of PDAL supporting multi-threading and the use of VPC files.

The two approaches are available in `02_pc_data.sh`. `PDAL` is used by default in this example as it seems to be the fastest solution in the first tests.

#### Extracting tiles with PDAL

* As explained [here](https://gist.github.com/esgn/4bbf298ad76f4d72e9f3c133cbc96cf1) knowing the COPC files to extract from, it is possible to write a pipeline for each tile
* This solution offers the possibility to define all pre-processing tasks in a single pipeline JSON file
* These pipelines can be launched in parallel using `GNU parallel`

#### Extracting tiles with PDAL wrench

* PDAL wrench uses a VPC file that indexes all COPC files. This global VPC is not provided by IGNF as of now. `scripts/generate_build_vpc_commands_wfs.py` builds it by querying the [IGNF Geoplateforme WFS](https://data.geopf.fr/wfs/ows?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetCapabilities) (`IGNF_LIDAR-HD_METADONNEE:metadata` layer) to list the COPC download urls of every LIDAR HD tile (mainland France and overseas territories), then generates chunked `pdal_wrench build_vpc` commands that are run in parallel and merged with `scripts/merge_vpc_files.py`
* PDAL wrench `clip` operation is multi-threaded but not the `merge` operation. As a result, the optimal way to use this tool would be to launch some operations sequentially and others in parallel. 
* Building the global VPC fetches the COPC header of every tile over HTTP(S). `data.geopf.fr` intermittently answers `429 Too Many Requests`, and `pdal_wrench build_vpc` aborts an entire chunk (no partial output) on the first failed file. To absorb this, `02_pc_data.sh` runs this step sequentially (`build_vpc_parallel_jobs=1`, with `build_vpc_delay_seconds` between chunk launches), and `scripts/generate_build_vpc_commands_wfs.py` uses small chunks (`--chunk_size`, default 10 urls) and wraps each `pdal_wrench build_vpc` call (`--threads 1`) in a retry loop with linear backoff (`--max_retries` / `--retry_backoff_seconds`) so a transient 429 only redoes a small chunk instead of failing the whole run. Some WFS urls point to tiles that no longer exist on `data.geopf.fr` (`404 Not Found`); this is not transient, so the retry loop detects it and gives up on the chunk immediately instead of exhausting `--max_retries`
* This step is lengthy (~1.5h) and runs `GNU parallel` with `--bar` to show a live progress bar (percentage of chunks done, ETA). At the end, a summary line reports how many chunks succeeded/failed (parsed from `build_vpc_logs/parallel.log`, the `--joblog` file); per-chunk retry details are in `build_vpc_logs/*.log`
* Use `scripts/generate_build_vpc_commands_wfs.py --limit N` to only process the first N COPC urls, e.g. to test the pipeline on a handful of tiles before running it on the full ~507k tiles

## 3D building reconstruction

3D building reconstruction is done with [geoflow-bundle](https://github.com/geoflow3d/geoflow-bundle).  
IGNfab provides additional [images Docker](https://hub.docker.com/u/ignfab) to test the different workflows available in geoflow (single, batch, stream). These are the images used in this example.  
Building reconstruction tasks are launched in parallel using `GNU parallel` 

## How to use this project

This project has been tested with Ubuntu 22.04 running on a CCX53 Hetzner instance with 32 cores.  
It requires [Docker](https://docs.docker.com/engine/install/ubuntu/) to run geoflow and [Anaconda](https://docs.conda.io/projects/conda/en/latest/user-guide/install/linux.html) to get the most out of PDAL.  
It requires 700GB of free space to create the necessary point cloud tiles.

First create the conda environment `bat3` using `conda env create -f environment.yml` then activate it with `conda activate bat3`

Then three main steps of the workflow can be launched using bash script

* `01_vector_data.sh` to download vector data, create quadtree and vector tiles (1 to 2 minutes)
* `02_pc_data.sh` to create point cloud tiles based on COPC files in OVH S3 (~2.5 hours)
* `03_reconstruct.sh` to reconstruct 3D buildings with geoflow (~1.5 hours)

The CityJSON results from geoflow can be merged and filtered using [cjio](https://github.com/cityjson/cjio) if necessary.
