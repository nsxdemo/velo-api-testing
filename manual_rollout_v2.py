import requests
from netaddr import IPNetwork
import csv
import copy
from datetime import datetime
import time
import json
import boto3  # AWS

start_time = time.time()
vco_hostname = 'vco84-usvi1.velocloud.net'
vco_url = f'https://{vco_hostname}/portal/rest/'
enterprise_id = 923    # B&Q
profile_id = 16403     # UKBQ_Stores
license_id = 496       # PREMIUM V2 | 10 Mbps | Hosted Orchestrator | Hosted Gateway | North America, Europe, Middle East and Africa
device_type = 'edge710'
segment_id = 1         # BQ_data segment
delay = 1

now = datetime.now()
timestamp = now.strftime('%d%m%y')
base_dir = '/home/ubuntu/Projects/Velocloud/B+Q'
project_dir = f'{base_dir}/manual_rollout'
src_file = f'{project_dir}/manual.csv'
provision_log = f'{project_dir}/provision_log-{timestamp}.json'


def aws_secret():
    secret_name = "deployment_server"
    region_name = "eu-north-1"

    session = boto3.session.Session()
    client = session.client(service_name='secretsmanager', region_name=region_name)
    secret_response = client.get_secret_value(SecretId=secret_name)
    secret_dict = json.loads(secret_response['SecretString'])
    return secret_dict['v211_python']


token = f'Token {aws_secret()}'
headers = {"Content-Type": "application/json", "Authorization": token}


## functions
def get_profiles():
    url = f'{vco_url}enterprise/getEnterpriseConfigurationsPolicies'
    resp = requests.post(url, headers=headers, json={"enterpriseId": enterprise_id})
    return resp.json()

def get_licenses():
    url = f'{vco_url}license/getEnterpriseEdgeLicenses'
    resp = requests.post(url, headers=headers, json={"enterpriseId": enterprise_id})
    return resp.json()

def import_csv():
    csv_trimmed = {}
    network_info = ['Velocloud Host Name', 'Site Address', 'Site Postcode',
                    'VLAN 4 - Voice IP details', 'VLAN 10 - Data IP details', 'VLAN 20 - AP Management IP details',
                    'Store Server for DHCP Relay (4 & 20)',
                    'VLAN 4 - Voice CIDR', 'VLAN 10 - Data CIDR', 'VLAN 20 - AP Management CIDR'
                    ]

    with open(src_file, encoding="utf-8-sig") as input_file:
        reader = csv.DictReader(input_file)
        for row in reader:
            csv_trimmed[row['Velocloud Host Name']] = [row.get(key) for key in network_info]

    return csv_trimmed

def get_segments(config_id):
    url = f'{vco_url}configuration/getConfiguration'
    resp = requests.post(url, headers=headers,
                         json={"enterpriseId": enterprise_id, "configurationId": config_id, "with": ["modules"]})
    config = resp.json()
    for module in config.get('modules', []):
        if module['name'] == 'deviceSettings':
            return module['data'].get('segments', [])
    return []

def discovery():
    print(f'\nAvailable profiles:')
    for p in get_profiles():
        print(f"  {p['name']} : id={p['id']}")

    print(f'\nAvailable licenses:')
    for l in get_licenses():
        print(f"  {l['alias']} : id={l['id']}")

    print(f'\nSegments in profile (id={profile_id}):')
    for s in get_segments(profile_id):
        seg = s['segment']
        print(f"  segmentId={seg['segmentId']}  name={seg['name']}  type={seg['type']}")

def create_edge(provision_params):
    url = f'{vco_url}edge/edgeProvision'
    resp = requests.post(url, headers=headers, json=provision_params)
    return resp.status_code, resp.json()

def deploy_appliance(appliance_name, address, postcode):
    provision_params = {
        "enterpriseId": enterprise_id,
        "configurationId": profile_id,
        "edgeLicenseId": license_id,
        "modelNumber": device_type,
        "name": appliance_name,
        "site": {
            "streetAddress": address,
            "postalCode": postcode,
            "shippingSameAsLocation": 1,
        }
    }

    print(f'Creating edge {appliance_name}')

    exit_status, provision_response = create_edge(provision_params)

    if exit_status != 200:
        try:
            msg = provision_response['error']['data']['error'][0]['message']
        except (KeyError, IndexError, TypeError):
            msg = json.dumps(provision_response)[:200]
        report_string = f"{appliance_name} : error - {msg}"
        print(f'ERROR: {report_string}\n')
    else:
        report_string = (
            f"{appliance_name},"
            f"{provision_response['id']},"
            f"{provision_response['activationKey']}"
        )
        print(f'{report_string}\n')

    with open(provision_log, mode='a') as log:
        log.write(f'{report_string}\n')

    return provision_response

def edge_config(edgeId):
    url = f'{vco_url}edge/getEdgeConfigurationStack'
    resp = requests.post(url, headers=headers, json={"enterpriseId": enterprise_id, "edgeId": edgeId})
    resp.raise_for_status()
    return resp.json()

def make_network(tmpl, vlan_id, name, cidr_ip, cidr_prefix, netmask, relay_ip=None):
    n = copy.deepcopy(tmpl)
    n['vlanId']     = vlan_id
    n['name']       = name
    n['segmentId']  = segment_id
    n['cidrIp']     = cidr_ip
    n['cidrPrefix'] = int(cidr_prefix)
    n['netmask']    = netmask
    n['override']   = True
    if relay_ip:
        n['dhcp']['enabled']   = True
        n['dhcp']['dhcpRelay'] = {'enabled': True, 'servers': [relay_ip], 'sourceFromSecondaryIp': False}
    else:
        n['dhcp']['dhcpRelay']['enabled'] = False
        n['dhcp']['dhcpRelay']['servers'] = []
    return n

def push_vlan_config(hostname, edge_id, csv_row):
    config_response = edge_config(edge_id)
    time.sleep(delay)

    edgeSpecificProfile = config_response[0]
    for module in edgeSpecificProfile['modules']:
        if module['name'] == 'deviceSettings':
            device_settings = module['data']
            module_id = module['id']

    ip1, cidr1 = csv_row[3], csv_row[7]
    ip2, cidr2 = csv_row[4], csv_row[8]
    ip3, cidr3 = csv_row[5], csv_row[9]
    relay      = csv_row[6]
    netmask1   = str(IPNetwork(f'{ip1}/{cidr1}').netmask)
    netmask2   = str(IPNetwork(f'{ip2}/{cidr2}').netmask)
    netmask3   = str(IPNetwork(f'{ip3}/{cidr3}').netmask)

    print(f'  VLAN4  : {ip1}/{cidr1}')
    print(f'  VLAN10 : {ip2}/{cidr2}')
    print(f'  VLAN20 : {ip3}/{cidr3}')
    print(f'  Relay  : {relay}')

    tmpl = device_settings['lan']['networks'][0]
    device_settings['lan']['networks'] = [
        make_network(tmpl, 4,  'Voice',         ip1, cidr1, netmask1, relay),
        make_network(tmpl, 10, 'Data',          ip2, cidr2, netmask2),
        make_network(tmpl, 20, 'AP Management', ip3, cidr3, netmask3, relay),
    ]
    device_settings['ha']['enabled'] = True
    device_settings['ntp'] = {
        'enabled': True,
        'servers': [{'server': '0.pool.ntp.org'}, {'server': '1.pool.ntp.org'}]
    }

    # Ensure BQ_data segment (segment_id=1) is declared in the edge-specific segments array.
    # Without this, the edge raises MGD_DEVICE_CONFIG_ERROR as it cannot resolve the segmentId
    # referenced by the VLAN networks.
    existing_seg_ids = [s['segment']['segmentId'] for s in device_settings.get('segments', [])]
    if segment_id not in existing_seg_ids:
        seg_template = copy.deepcopy(device_settings['segments'][0])
        seg_template['segment'] = {'segmentId': segment_id, 'name': 'BQ_data', 'type': 'REGULAR'}
        device_settings['segments'].append(seg_template)
        print(f'  Added segmentId={segment_id} (BQ_data) to edge-specific segments')

    update_params = {
        "enterpriseId": enterprise_id,
        "id": module_id,
        "returnData": True,
        "_update": {"data": device_settings}
    }

    url = f'{vco_url}configuration/updateConfigurationModule'
    resp = requests.post(url, headers=headers, json=update_params)
    resp_body = resp.json()

    if resp.status_code == 200 and 'error' not in resp_body:
        print(f'  Config pushed OK\n')
    else:
        print(f'  Config push FAILED: HTTP {resp.status_code} {json.dumps(resp_body)[:200]}\n')

def main():
    # >>>>>> Run discovery first to find profile_id and license_id <<<<<<<<
    # discovery()
    # return

    if profile_id is None or license_id is None:
        print("ERROR: Run discovery() first to find profile_id and license_id.")
        return

    # >>>>>> Phase 1: Provision edges from CSV <<<<<<<<<<
    csv_trimmed = import_csv()
    edge_list = {}

    print('=' * 60)
    print('PHASE 1 - Provisioning')
    print('=' * 60)
    for hostname, site in csv_trimmed.items():
        try:
            response = deploy_appliance(site[0], site[1], site[2])
            edge_list[site[0]] = response['id']
        except Exception as e:
            print(f"Error deploying {site[0]}: {e}")

    # >>>>>> Phase 2: Push VLAN config to each edge <<<<<<<
    print('=' * 60)
    print('PHASE 2 - VLAN Configuration')
    print('=' * 60)
    for hostname, edge_id in edge_list.items():
        print(f'{hostname} (id={edge_id})')
        try:
            push_vlan_config(hostname, edge_id, csv_trimmed[hostname])
        except Exception as e:
            print(f"  Error configuring {hostname}: {e}\n")

    end_time = time.time()
    print(f'Total run time: {end_time - start_time:.3f} seconds')
    print(f'Provisioning log: {provision_log}')

if __name__ == "__main__":
    main()
