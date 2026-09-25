###############################################################################
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
###############################################################################

"""Periodically run PromQL queries and write the results as JSON files."""

import json
import logging
import os
import signal
import sys
import tempfile
import time

import requests
import yaml

PROMETHEUS_URL = os.environ.get('PROMETHEUS_URL', 'http://prometheus:9090')
QUERIES_FILE = os.environ.get('QUERIES_FILE', 'queries.yml')
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', '/output')
INTERVAL_SECONDS = int(os.environ.get('INTERVAL_SECONDS', '600'))
RATE_INTERVAL = os.environ.get('RATE_INTERVAL', '5m')
REQUEST_TIMEOUT = int(os.environ.get('REQUEST_TIMEOUT', '60'))
LOGGING_LEVEL = os.environ.get('LOGGING_LEVEL', 'INFO')

LOGGER = logging.getLogger(__name__)

_stop = False


def handle_signal(signum, frame):
    global _stop
    LOGGER.info('received signal %s, shutting down', signum)
    _stop = True


def load_queries(path):
    with open(path, encoding='utf-8') as fh:
        config = yaml.safe_load(fh) or {}

    queries = config.get('queries') or []
    if not queries:
        raise ValueError(f'no queries defined in {path}')

    for index, query in enumerate(queries):
        if not query.get('query'):
            raise ValueError(f'query at position {index} has no "query" value')
        if not query.get('name'):
            raise ValueError(f'query at position {index} has no "name" value')

    return queries


def run_query(promql):
    response = requests.get(
        f'{PROMETHEUS_URL}/api/v1/query',
        params={'query': promql},
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get('status') != 'success':
        raise RuntimeError(f'prometheus returned: {payload.get("error")}')

    result_type = payload['data']['resultType']
    if result_type != 'vector':
        raise RuntimeError(f'unsupported result type: {result_type}')

    rows = []
    for item in payload['data']['result']:
        row = dict(item['metric'])
        row.pop('__name__', None)
        row['Value'] = float(item['value'][1])
        rows.append(row)

    return rows


def write_json(path, rows):
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)

    # write to a temporary file first so readers never see a partial file
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(rows, fh, indent=2)
            fh.write('\n')
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def process(queries):
    for query in queries:
        name = query['name']
        output = os.path.join(OUTPUT_DIR, query.get('output', f'{name}.json'))
        rate_interval = str(query.get('rate_interval', RATE_INTERVAL))
        promql = query['query'].replace('$__rate_interval', rate_interval)

        try:
            rows = run_query(promql)
            write_json(output, rows)
            LOGGER.info('wrote %d rows for "%s" to %s', len(rows), name, output)
        except Exception as err:
            LOGGER.error('query "%s" failed: %s', name, err)


def main():
    logging.basicConfig(
        level=getattr(logging, LOGGING_LEVEL.upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(message)s'
    )

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        queries = load_queries(QUERIES_FILE)
    except Exception as err:
        LOGGER.error('could not load queries: %s', err)
        return 1

    LOGGER.info(
        'polling %s every %ss for %d queries',
        PROMETHEUS_URL, INTERVAL_SECONDS, len(queries)
    )

    while not _stop:
        process(queries)

        deadline = time.monotonic() + INTERVAL_SECONDS
        while not _stop and time.monotonic() < deadline:
            time.sleep(1)

    return 0


if __name__ == '__main__':
    sys.exit(main())
