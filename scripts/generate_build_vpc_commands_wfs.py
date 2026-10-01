import requests
import argparse
import sys
import os
import math
import time
from tqdm import tqdm

# The public WFS endpoint occasionally returns transient 5xx/timeout errors
# when paging through a large number of pages, hence the retry logic below.
MAX_RETRIES = 1
RETRY_BACKOFF_SECONDS = 5

def get_with_retries(session, url, params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=60)
            response.raise_for_status()
            return response
        except (requests.exceptions.RequestException,) as exc:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError("unreachable")


def parse_args():
    parser = argparse.ArgumentParser(
        "Query the IGNF Geoplateforme WFS for all LIDAR HD COPC file urls "
        "(mainland France and overseas territories) and generate files and "
        "commands in order to build a complete VPC"
    )
    parser.add_argument("--wfs_url", "-w",
                        help="WFS base url",
                        default="https://data.geopf.fr/wfs/ows")
    parser.add_argument("--typename", "-t",
                        help="WFS feature type holding the LIDAR HD tiles metadata",
                        default="IGNF_LIDAR-HD_METADONNEE:metadata")
    parser.add_argument("--url_property", "-p",
                        help="WFS feature property holding the COPC download url",
                        default="url_npl")
    parser.add_argument("--page_size",
                        help="number of features fetched per WFS request (server capped at 5000)",
                        type=int, default=5000)
    parser.add_argument("--chunk_size", "-c",
                        help="number of COPC urls per pdal_wrench build_vpc command "
                             "(kept small since data.geopf.fr rate-limits COPC header "
                             "reads and a failed chunk is retried in full)",
                        type=int, default=10)
    parser.add_argument("--wrench_threads",
                        help="--threads value passed to pdal_wrench build_vpc",
                        type=int, default=1)
    parser.add_argument("--max_retries",
                        help="number of attempts per chunk before giving up "
                             "(data.geopf.fr intermittently returns 429 Too Many Requests)",
                        type=int, default=5)
    parser.add_argument("--retry_backoff_seconds",
                        help="base delay before retrying a failed chunk, "
                             "multiplied by the attempt number (linear backoff)",
                        type=int, default=5)
    parser.add_argument("--output_urls_dir", "-u",
                        help="output directory for chunked url list files",
                        default="block_urls_dir")
    parser.add_argument("--output_log_dir", "-l",
                        help="output directory for pdal_wrench build_vpc logs",
                        default="build_vpc_logs")
    parser.add_argument("--output_dir", "-o",
                        help="output vpc directory",
                        default="vpc")
    parser.add_argument("--output_cmd_file", "-f",
                        help="output commands file",
                        default="build_vpc_commands.txt")
    parser.add_argument("--limit",
                        help="stop after this many COPC urls (useful to test on a small subset)",
                        type=int, default=None)
    return parser.parse_args()


def get_total_features(session, wfs_url, typename):
    response = get_with_retries(session, wfs_url, params={
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": typename,
        "RESULTTYPE": "hits",
    })
    # numberMatched="507791" is an XML attribute on the root element
    marker = 'numberMatched="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return int(response.text[start:end])


def iter_copc_urls(session, wfs_url, typename, url_property, page_size, total, limit=None):
    nb_pages = math.ceil(total / page_size)
    nb_yielded = 0
    for page in tqdm(range(nb_pages), desc="Fetching COPC urls from WFS"):
        start_index = page * page_size
        response = get_with_retries(session, wfs_url, params={
            "SERVICE": "WFS",
            "VERSION": "2.0.0",
            "REQUEST": "GetFeature",
            "TYPENAMES": typename,
            "COUNT": page_size,
            "STARTINDEX": start_index,
            "OUTPUTFORMAT": "application/json",
            "PROPERTYNAME": url_property,
        })
        data = response.json()
        for feature in data["features"]:
            url = feature["properties"].get(url_property)
            if url:
                yield url
                nb_yielded += 1
                if limit is not None and nb_yielded >= limit:
                    return


def build_retrying_command(pdal_wrench_cmd, log_filepath, max_retries, backoff_seconds):
    # pdal_wrench build_vpc aborts the whole chunk (no partial output) on the
    # first failed file, and data.geopf.fr intermittently answers "429 Too
    # Many Requests" to COPC header reads. Retrying the whole chunk with a
    # growing delay absorbs these transient failures without aborting the
    # overall pipeline. A "404 Not Found" is not transient (the tile was
    # removed/moved on data.geopf.fr) so it gives up immediately instead of
    # burning through max_retries attempts.
    return (
        f"n=0; until {pdal_wrench_cmd} >> {log_filepath} 2>&1; do "
        f"n=$((n+1)); "
        f"if grep -qiE '\"status\" ?: ?404|<title>404' {log_filepath}; then "
        f"echo \"giving up: 404 Not Found (tile no longer available)\" >> {log_filepath}; exit 1; fi; "
        f"if [ $n -ge {max_retries} ]; then echo \"giving up after {max_retries} attempts\" >> {log_filepath}; exit 1; fi; "
        f"echo \"retry $n\" >> {log_filepath}; "
        f"sleep $((n*{backoff_seconds})); "
        f"done"
    )


def main():
    args = parse_args()

    session = requests.Session()
    total = get_total_features(session, args.wfs_url, args.typename)
    print(str(total) + " LIDAR HD tiles found on WFS")

    chunk_files = []
    chunk_file = None
    nb_in_chunk = 0
    chunk_index = 0
    for url in iter_copc_urls(session, args.wfs_url, args.typename, args.url_property,
                               args.page_size, total, args.limit):
        if chunk_file is None or nb_in_chunk >= args.chunk_size:
            if chunk_file is not None:
                chunk_file.close()
            chunk_filename = "chunk_{:05d}.txt".format(chunk_index)
            chunk_filepath = os.path.join(args.output_urls_dir, chunk_filename)
            chunk_file = open(chunk_filepath, "w")
            chunk_files.append(chunk_filepath)
            chunk_index += 1
            nb_in_chunk = 0
        chunk_file.write(url + "\n")
        nb_in_chunk += 1
    if chunk_file is not None:
        chunk_file.close()

    with open(args.output_cmd_file, "w") as dest:
        for urls_filepath in chunk_files:
            filename = os.path.basename(urls_filepath).split('.')[0]
            output_vpc_filepath = os.path.join(args.output_dir, filename + ".vpc")
            output_log_filepath = os.path.join(args.output_log_dir, filename + ".log")
            pdal_wrench_cmd = (
                f"pdal_wrench build_vpc --output={output_vpc_filepath} "
                f"--input-file-list={urls_filepath} --threads {args.wrench_threads}"
            )
            row = build_retrying_command(pdal_wrench_cmd, output_log_filepath,
                                          args.max_retries, args.retry_backoff_seconds)
            dest.write(row + "\n")

    print(str(len(chunk_files)) + " url chunks written to " + args.output_urls_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
