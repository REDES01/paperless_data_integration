# Build provision_chameleon.ipynb from a list of cell specs.
# Run: .\scripts\build_notebook.ps1

$ErrorActionPreference = 'Stop'
$outPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'provision_chameleon.ipynb'

function New-MdCell($text) {
    [ordered]@{
        cell_type = 'markdown'
        metadata = [ordered]@{}
        source = $text
    }
}

function New-CodeCell($text) {
    [ordered]@{
        cell_type = 'code'
        execution_count = $null
        metadata = [ordered]@{}
        outputs = @()
        source = $text
    }
}

$cells = @(
    New-MdCell @"
# Paperless-ngx ML Data Integration — Chameleon Deployment

Run this notebook in the **Chameleon Jupyter environment** to provision a VM and bring up the integrated ML platform (Paperless-ngx UI + data stack + shared network) end-to-end.

**What this notebook does:**
1. Reserves an ``m1.xlarge`` VM on KVM@TACC for 12 hours
2. Assigns a floating IP and opens security groups for every service port
3. Installs Docker on the VM
4. Clones repos (``paperless_data``, ``paperless_data_integration``) into ``~/``
5. Creates the shared ``paperless_ml_net`` Docker network
6. Generates a ``PAPERLESS_SECRET_KEY`` and writes ``paperless/docker-compose.env``
7. Pulls the pre-built Paperless custom image from GHCR
8. Brings up the data stack with the network override
9. Seeds demo data into the data-stack Postgres
10. Brings up the Paperless stack with the network override
11. Verifies cross-stack DNS works
12. Prints access URLs for every UI

**Image build workflow:**
The custom Paperless image (with ML UI + Django views + Kafka producer) is built **locally** using ``scripts/build_and_push.ps1`` and pushed to ``ghcr.io/redes01/paperless-ngx-ml:latest``. The VM just pulls the pre-built image — no build step on Chameleon, no GitHub CDN timeouts.

**Prerequisites:**
- A Chameleon project with KVM@TACC allocation
- The GHCR image has been pushed (run ``scripts/build_and_push.ps1`` on your dev machine)
"@

    New-MdCell "# Part 1 — VM Setup"

    New-MdCell "## Step 1 — Select project and site"

    New-CodeCell @'
from chi import server, context, lease, network
import chi, os, datetime

context.version = "1.0"
context.choose_project()
context.choose_site(default="KVM@TACC")
username = os.getenv("USER")
print(f"Username: {username}")
'@

    New-MdCell @"
## Step 2 — Reserve VM (12 hours)

The integration runs both the data stack and Paperless on the same VM, so we need more RAM and CPU than the data-only notebook. ``m1.xlarge`` (8 vCPU, 16 GiB RAM) is the recommended minimum.
"@

    New-CodeCell @'
l = lease.Lease(
    f"lease-paperless-integration-{username}",
    duration=datetime.timedelta(hours=12)
)
l.add_flavor_reservation(id=chi.server.get_flavor_id("m1.xlarge"), amount=1)
l.submit(idempotent=True)
l.show()
'@

    New-MdCell "## Step 3 — Launch VM instance"

    New-CodeCell @'
s = server.Server(
    f"node-paperless-integration-{username}",
    image_name="CC-Ubuntu24.04",
    flavor_name=l.get_reserved_flavors()[0].name,
)
s.submit(idempotent=True)
'@

    New-MdCell "## Step 4 — Assign floating IP"

    New-CodeCell @'
s.associate_floating_ip()
s.refresh()
s.show(type="widget")
'@

    New-MdCell "## Step 5 — Open security groups"

    New-CodeCell @'
security_groups = [
    {"name": "allow-ssh",  "port": 22,   "description": "SSH"},
    {"name": "allow-8000", "port": 8000, "description": "Paperless UI"},
    {"name": "allow-5050", "port": 5050, "description": "Adminer (PostgreSQL UI)"},
    {"name": "allow-9001", "port": 9001, "description": "MinIO Console"},
    {"name": "allow-8090", "port": 8090, "description": "Redpanda Console"},
    {"name": "allow-6333", "port": 6333, "description": "Qdrant"},
]

for sg in security_groups:
    secgroup = network.SecurityGroup({"name": sg["name"], "description": sg["description"]})
    secgroup.add_rule(direction="ingress", protocol="tcp", port=sg["port"])
    secgroup.submit(idempotent=True)
    s.add_security_group(sg["name"])

print(f"Security groups applied: {[sg['name'] for sg in security_groups]}")
'@

    New-MdCell "## Step 6 — Install Docker"

    New-CodeCell @'
s.refresh()
s.check_connectivity()
s.execute("curl -sSL https://get.docker.com/ | sudo sh")
s.execute("sudo groupadd -f docker; sudo usermod -aG docker $USER")
print("Docker installed.")
'@

    New-MdCell @"
---
# Part 2 — Deploy the integrated stack
"@

    New-MdCell @"
## Step 7 — Clone repos

Only the data platform and the integration repo are needed on the VM. The Paperless custom image is pre-built and pulled from GHCR — no need to clone the fork source.
"@

    New-CodeCell @'
DATA_REPO        = "https://github.com/REDES01/paperless_data.git"
INTEGRATION_REPO = "https://github.com/REDES01/paperless_data_integration.git"

s.execute("rm -rf ~/paperless_data ~/paperless_data_integration")
s.execute(f"git clone {DATA_REPO} ~/paperless_data")
s.execute(f"git clone {INTEGRATION_REPO} ~/paperless_data_integration")
s.execute("ls ~/")
print("Repos cloned.")
'@

    New-MdCell "## Step 8 — Create the shared Docker network"

    New-CodeCell @'
s.execute("cd ~/paperless_data_integration && sg docker -c 'bash scripts/create_network.sh'")
print("Shared network ready.")
'@

    New-MdCell @"
## Step 9 — Generate ``PAPERLESS_SECRET_KEY`` and write env file

Source builds of Paperless-ngx require an explicit secret key. We generate one on the VM and write it into ``paperless/docker-compose.env``. The env file also contains the ``PAPERLESS_ML_DB*`` and ``PAPERLESS_ML_KAFKA_*`` vars.
"@

    New-CodeCell @'
s.execute(
    "cd ~/paperless_data_integration/paperless && "
    "cp docker-compose.env.example docker-compose.env && "
    "SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))') && "
    "sed -i \"s|PAPERLESS_SECRET_KEY=replace-me-with-a-real-secret|PAPERLESS_SECRET_KEY=$SECRET|\" docker-compose.env && "
    "grep PAPERLESS_SECRET_KEY docker-compose.env"
)
print("Secret key written.")
'@

    New-MdCell @"
## Step 10 — Pull the pre-built Paperless image from GHCR

The custom image (Angular frontend with ML pages + Django ML views + Kafka producer) is built locally using ``scripts/build_and_push.ps1`` and pushed to ``ghcr.io/redes01/paperless-ngx-ml:latest``. This avoids the 10-15 min build on the VM and eliminates GitHub CDN timeout issues during Docker builds.

If the image is not yet pushed, run on your dev machine first:
```
cd paperless_data_integration
.\scripts\build_and_push.ps1
```
"@

    New-CodeCell @'
s.execute("sg docker -c 'docker pull ghcr.io/redes01/paperless-ngx-ml:latest'")
print("Paperless image pulled.")
'@

    New-MdCell "## Step 11 — Bring up the data stack"

    New-CodeCell @'
s.execute("cd ~/paperless_data_integration && sg docker -c 'bash scripts/up_paperless_data.sh'")
print("Data stack up.")
'@

    New-MdCell @"
## Step 12 — Seed demo data into the data-stack Postgres

Inserts 3 fake handwritten regions across 2 fake documents so the HTR review page has something to display before the real HTR preprocessing service exists. Idempotent.
"@

    New-CodeCell @'
s.execute(
    "cat ~/paperless_data_integration/seed/phase1_demo_seed.sql | "
    "sg docker -c 'docker exec -i postgres psql -U user -d paperless'"
)
print("Demo seed inserted.")
'@

    New-MdCell "## Step 13 — Bring up the Paperless stack"

    New-CodeCell @'
s.execute("cd ~/paperless_data_integration && sg docker -c 'bash scripts/up_paperless.sh'")
print("Paperless stack up.")
'@

    New-MdCell @"
## Step 14 — Verify cross-stack DNS

Runs ``getent hosts`` from inside the Paperless webserver container against ``postgres``, ``minio``, ``redpanda``, and ``qdrant``. All four should resolve to private IPs on ``paperless_ml_net``.
"@

    New-CodeCell @'
import time
time.sleep(30)
s.execute("cd ~/paperless_data_integration && sg docker -c 'bash scripts/verify.sh'")
'@

    New-MdCell "## Step 15 — Print access URLs"

    New-CodeCell @'
s.refresh()
addresses = s.addresses
floating_ip = None
for net, addrs in addresses.items():
    for addr in addrs:
        if addr.get("OS-EXT-IPS:type") == "floating":
            floating_ip = addr["addr"]

print(f"Floating IP: {floating_ip}")
print()
print(f"  Paperless UI     ->  http://{floating_ip}:8000")
print(f"  HTR review       ->  http://{floating_ip}:8000/ml/htr-review")
print(f"  Semantic search  ->  http://{floating_ip}:8000/ml/search")
print(f"  MinIO Console    ->  http://{floating_ip}:9001     (admin / paperless_minio)")
print(f"  Adminer          ->  http://{floating_ip}:5050     (user / paperless_postgres, DB=paperless)")
print(f"  Redpanda Console ->  http://{floating_ip}:8090")
print(f"  Qdrant           ->  http://{floating_ip}:6333/dashboard")
print()
print(f"  SSH:  ssh -i ~/.ssh/id_rsa_chameleon cc@{floating_ip}")
'@

    New-MdCell @"
---
# Teardown

Run this when you are done to release VM resources.
"@

    New-CodeCell @'
# Uncomment to release VM
# s = server.get_server(f"node-paperless-integration-{username}")
# server.delete_server(s.id)
# l = lease.get_lease(f"lease-paperless-integration-{username}")
# lease.delete_lease(l.id)
# print("VM resources released.")
'@
)

$nb = [ordered]@{
    cells = $cells
    metadata = [ordered]@{
        kernelspec = [ordered]@{
            display_name = 'Python 3 (ipykernel)'
            language = 'python'
            name = 'python3'
        }
        language_info = [ordered]@{
            name = 'python'
        }
    }
    nbformat = 4
    nbformat_minor = 5
}

$json = $nb | ConvertTo-Json -Depth 50
$json = $json -replace '\\/', '/'
Set-Content -Path $outPath -Value $json -Encoding UTF8
Write-Host "Wrote $outPath ($([int]((Get-Item $outPath).Length/1024)) KB)"
