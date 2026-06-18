import requests
import time
import json
import os
from datetime import datetime

# provide the vco hostname, enterprise id, API_token and base_dir


start_time = time.time()
vco_hostname = ''
vco_rest_url = f'https://{vco_hostname}/portal/rest/'
vco_jsonrpc_url = f'https://{vco_hostname}/portal/'
enterprise_id =
delay = 1

base_dir = '/root/api'
now = datetime.now()
timestamp = now.strftime('%d%m%y')
output_dir = f'{base_dir}/dest_metrics-{timestamp}'

API_TOKEN = ''
token = f'Token {API_TOKEN}'
headers = {"Content-Type": "application/json", "Authorization": token}

DAY_MS = 24 * 60 * 60 * 1000


def prompt_options():
    print('Select lookback period:')
    print('  1) 1 day')
    print('  2) 7 days')
    print('  3) 14 days')
    choice = input('Enter choice [2]: ').strip()
    lookback_days = {'1': 1, '2': 7, '3': 14}.get(choice, 7)

    limit_input = input('Enter result limit [100]: ').strip()
    limit = int(limit_input) if limit_input else 100

    print(f'\nLookback: {lookback_days} days, Limit: {limit}\n')
    return lookback_days, limit


def discover_edges():
    url = f'{vco_rest_url}enterprise/getEnterpriseEdges'
    params = {
        "enterpriseId": enterprise_id,
        "with": ["site"]
    }
    resp = requests.post(url, headers=headers, json=params)
    resp.raise_for_status()
    return resp.json()


def build_metrics_payload(edge_id, start_ms, end_ms, limit):
    return {
        "id": int(time.time()),
        "jsonrpc": "2.0",
        "method": "metrics/getEdgeDestMetrics",
        "params": {
            "enterpriseId": enterprise_id,
            "edgeId": edge_id,
            "metrics": [
                "totalBytes", "bytesRx", "bytesTx",
                "totalPackets", "packetsRx", "packetsTx"
            ],
            "sort": "bytesRx",
            "sortOrder": "DESC",
            "attribute": "destDomain",
            "interval": {
                "start": start_ms,
                "end": end_ms
            },
            "limit": limit,
            "filters": [
                {"field": "route", "op": "=", "values": [3, 2, 4]}
            ]
        }
    }


def build_series_payload(edge_id, destinations, start_ms, end_ms):
    return {
        "id": int(time.time()),
        "jsonrpc": "2.0",
        "method": "metrics/getEdgeDestSeries",
        "params": {
            "enterpriseId": enterprise_id,
            "edgeId": edge_id,
            "metrics": ["bytesRx", "bytesTx"],
            "destinations": destinations,
            "attribute": "destDomain",
            "interval": {
                "start": start_ms,
                "end": end_ms
            },
            "maxSamples": 512,
            "filters": [
                {"field": "route", "op": "=", "values": [3, 2, 4]}
            ]
        }
    }


def call_jsonrpc(payload):
#    print(f'  --- REQUEST ---')
#    print(f'  POST {vco_jsonrpc_url}')
#    print(f'  Headers: {json.dumps(headers)}')
#    print(f'  Body: {json.dumps(payload)}')
#    print(f'  ---------------')
    resp = requests.post(vco_jsonrpc_url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


def main():
    lookback_days, limit = prompt_options()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (lookback_days * DAY_MS)

    print(f'Discovering edges from enterprise {enterprise_id}...')
    all_edges = discover_edges()
    connected = [e for e in all_edges if e.get('edgeState') == 'CONNECTED']
    skipped = len(all_edges) - len(connected)
    print(f'Found {len(all_edges)} edges total, {len(connected)} connected, {skipped} skipped\n')

    if not connected:
        print('No connected edges found. Exiting.')
        return

    os.makedirs(output_dir, exist_ok=True)

    metrics_file = f'{output_dir}/all_edges_metrics-{timestamp}.json'
    series_file = f'{output_dir}/all_edges_series-{timestamp}.json'
    print(f'Output files:')
    print(f'  Metrics: {metrics_file}')
    print(f'  Series:  {series_file}\n')

    print('=' * 60)
    print(f'COLLECTING DESTINATION METRICS ({lookback_days}d, limit={limit})')
    print('=' * 60)

    all_metrics = []
    all_series = []
    metrics_ok = 0
    series_ok = 0
    fail_count = 0

    for edge in connected:
        edge_id = edge['id']
        edge_name = edge['name']
        print(f'\n{edge_name} (id={edge_id})')

        try:
            metrics_payload = build_metrics_payload(edge_id, start_ms, end_ms, limit)
            metrics_response = call_jsonrpc(metrics_payload)
            time.sleep(delay)

            records = metrics_response.get('result', [])
            print(f'  DestMetrics: {len(records)} records')
            all_metrics.append({
                "edgeId": edge_id,
                "edgeName": edge_name,
                "request": metrics_payload,
                "response": metrics_response
            })
            metrics_ok += 1

            dest_names = [r['name'] for r in records if r.get('name')]
            if not dest_names:
                print(f'  DestSeries:  skipped (no destinations)')
                continue

            print(f'  DestSeries:  querying {len(dest_names)} destinations...')
            series_payload = build_series_payload(edge_id, dest_names, start_ms, end_ms)
            series_response = call_jsonrpc(series_payload)
            time.sleep(delay)

            series_count = len(series_response.get('result', []))
            print(f'  DestSeries:  {series_count} series')
            all_series.append({
                "edgeId": edge_id,
                "edgeName": edge_name,
                "request": series_payload,
                "response": series_response
            })
            series_ok += 1

        except Exception as e:
            print(f'  ERROR: {e}')
            fail_count += 1

    with open(metrics_file, 'w') as f:
        json.dump(all_metrics, f, indent=2)

    with open(series_file, 'w') as f:
        json.dump(all_series, f, indent=2)

    end_time = time.time()
    print(f'\n{"=" * 60}')
    print(f'SUMMARY')
    print(f'  DestMetrics collected: {metrics_ok}')
    print(f'  DestSeries collected:  {series_ok}')
    print(f'  Failed: {fail_count}')
    print(f'  Output: {output_dir}/')
    print(f'  Total run time: {end_time - start_time:.3f} seconds')


if __name__ == "__main__":
    main()
