# Build provision_chameleon.ipynb from a list of cell specs.
# Run: .\scripts\build_notebook.ps1
#
# Why a builder script? Authoring a 33-cell notebook in raw JSON is painful;
# this lets us keep cell content in heredocs and regenerate on demand.

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

# Use single-quoted PS here-strings (@'...'@) for any cell whose body
# contains $ or ${...} so PowerShell does not try to interpolate them.
# Use double-quoted (@"..."@) only when there are no shell variables inside.

$cells = @(
    New-MdCell @"
# Paperless-ngx ML Data Integration — Chameleon Deployment

Run this notebook in the **Chameleon Jupyter environment** to provision a VM and bring up the integrated ML platform (Paperless-ngx UI + data stack + shared network) end-to-end.

**What this notebook does:**
1. Reserves an ``m1.xlarge`` VM on KVM@TACC for 12 hours
2. Assigns a floating IP and opens security groups for every service port
3. Installs Docker on the VM
4. Clones the three sibling repos (``paperless_data``, ``paperless-ngx``, ``paperless_data_integration``) into ``~/``
5. Creates the shared ``paperless_ml_net`` Docker network
6. Generates a ``PAPERLESS_SECRET_KEY`` and writes ``paperless/docker-compose.env``
7. Builds the custom Paperless image (slow — frontend + runtime)
8. Brings up the data stack with the network override
9. Seeds 3 fake handwritten regions so the HTR review page demos out of the box
10. Brings up the Paperless stack with the network override
11. Verifies cross-stack DNS works
12. Prints access URLs for every UI

**Prerequisites:**
- A Chameleon project with KVM@TACC allocation
- Three sibling repos pushed to GitHub and publicly cloneable (URLs configured in Step 7)
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
# Part 2 — Clone repos and bring up the integrated stack
"@

    New-MdCell @"
## Step 7 — Clone the three repos

Edit the three repo URLs below if your forks live elsewhere. Default layout on the VM:

- ``~/paperless_data/`` — data team repo
- ``~/paperless-ngx-fork/`` — Paperless UI fork (cloned from the ``paperless-ngx`` GitHub repo, but renamed locally so the build context paths in the compose file resolve correctly)
- ``~/paperless_data_integration/`` — this repo
"@

    New-CodeCell @'
DATA_REPO        = "https://github.com/REDES01/paperless_data.git"
FORK_REPO        = "https://github.com/REDES01/paperless-ngx.git"
INTEGRATION_REPO = "https://github.com/REDES01/paperless_data_integration.git"

s.execute("rm -rf ~/paperless_data ~/paperless-ngx-fork ~/paperless_data_integration")
s.execute(f"git clone {DATA_REPO} ~/paperless_data")
s.execute(f"git clone {FORK_REPO} ~/paperless-ngx-fork")
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

Source builds of Paperless-ngx require an explicit secret key. We generate one on the VM and sed-replace the placeholder in ``paperless/docker-compose.env``. The example file also contains the ``PAPERLESS_ML_DB*`` env vars that the new Phase 1 Django views need to reach the data-stack Postgres.
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
## Step 10 — Build the Paperless custom image

This compiles the Angular frontend (``pnpm install`` + ``ng build``) and assembles the Python runtime image. **First build typically takes 10–15 minutes** on ``m1.xlarge``. Subsequent builds are fast due to layer caching.
"@

    New-CodeCell @'
s.execute(
    "cd ~/paperless_data_integration && "
    "sg docker -c 'docker compose -p paperless "
    "-f paperless/docker-compose.yml "
    "-f overrides/paperless.override.yml "
    "build webserver'"
)
print("Paperless image built.")
'@

    New-MdCell "## Step 11 — Bring up the data stack"

    New-CodeCell @'
s.execute("cd ~/paperless_data_integration && sg docker -c 'bash scripts/up_paperless_data.sh'")
print("Data stack up.")
'@

    New-MdCell @"
## Step 12 — Seed demo data into the data-stack Postgres

Inserts 3 fake handwritten regions across 2 fake documents into the data-stack Postgres so the HTR review page has something real to display before Phase 2 (the actual HTR preprocessing service) exists. Idempotent — safe to re-run.
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
time.sleep(30)  # give Paperless a moment to finish starting
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
