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

Run this notebook in the **Chameleon Jupyter environment** to provision a VM and bring up the integrated ML platform end-to-end.

**Image build workflow:** The custom Paperless image is built locally using ``scripts/build_and_push.ps1`` and pushed to ``ghcr.io/redes01/paperless-ngx-ml:latest``. The VM just pulls the pre-built image — no build step on Chameleon.

**Prerequisites:**
- A Chameleon project with KVM@TACC allocation
- The GHCR image has been pushed (run ``scripts/build_and_push.ps1`` on your dev machine)
"@

    # ── Part 1: VM Setup ──

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

    New-MdCell "## Step 2 — Reserve VM (12 hours)"

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

    # ── Part 2: Deploy ──

    New-MdCell "---`n# Part 2 — Deploy the integrated stack"

    New-MdCell "## Step 7 — Clone repos"

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

    New-MdCell "## Step 9 — Generate secret key and write env file"

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

    New-MdCell "## Step 10 — Pull the pre-built Paperless image from GHCR"

    New-CodeCell @'
s.execute("sg docker -c 'docker pull ghcr.io/redes01/paperless-ngx-ml:latest'")
print("Paperless image pulled.")
'@

    New-MdCell "## Step 11 — Bring up the data stack"

    New-CodeCell @'
s.execute("cd ~/paperless_data_integration && sg docker -c 'bash scripts/up_paperless_data.sh'")
print("Data stack up.")
'@

    New-MdCell "## Step 12 — Seed demo data into the data-stack Postgres"

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
## Step 14 — Wait for Paperless to become healthy and verify cross-stack DNS
"@

    New-CodeCell @'
import time
print("Waiting 45 seconds for Paperless to finish starting...")
time.sleep(45)
s.execute("cd ~/paperless_data_integration && sg docker -c 'bash scripts/verify.sh'")
'@

    New-MdCell "## Step 15 — Create Paperless superuser"

    New-CodeCell @'
# Create the initial admin account for the Paperless UI.
# Change the username/password as needed.
s.execute(
    "sg docker -c 'docker exec paperless-webserver-1 python manage.py shell -c \""
    "from django.contrib.auth.models import User; "
    "User.objects.filter(username=\\\"admin\\\").exists() or "
    "User.objects.create_superuser(\\\"admin\\\", \\\"admin@example.com\\\", \\\"admin\\\"); "
    "print(\\\"Superuser ready\\\")\"'"
)
'@

    New-MdCell @"
## Step 16 — Generate Paperless API token

This token is needed by the region slicer and any API-based testing. It authenticates REST API calls from other containers on the shared network.
"@

    New-CodeCell @'
result = s.execute(
    "sg docker -c 'docker exec paperless-webserver-1 python manage.py shell -c \""
    "from rest_framework.authtoken.models import Token; "
    "from django.contrib.auth.models import User; "
    "t, _ = Token.objects.get_or_create(user=User.objects.first()); "
    "print(t.key)\"'"
)
# Extract the token from stdout for use in later cells
PAPERLESS_TOKEN = result.stdout.strip().split("\n")[-1]
print(f"API Token: {PAPERLESS_TOKEN}")
'@

    # ── Part 3: Build and test region slicer ──

    New-MdCell "---`n# Part 3 — Region slicer"

    New-MdCell @"
## Step 17 — Build the region slicer image

Small image: ``python:3.12-slim`` + ``poppler-utils``. Takes ~30 seconds.
"@

    New-CodeCell @'
s.execute(
    "cd ~/paperless_data_integration && "
    "sg docker -c 'docker compose -f region_slicer/compose.yml build'"
)
print("Slicer image built.")
'@

    New-MdCell @"
## Step 18 — Upload a test document to Paperless

Upload a PDF through the Paperless UI (drag-and-drop at ``http://<floating-ip>:8000``), or use the API to upload programmatically. The cell below uploads a small test PDF via the API.

If you prefer to upload manually through the UI, skip this cell and note the document ID from the URL bar (e.g. ``/documents/1``).
"@

    New-CodeCell @'
# Upload a test document via the Paperless API.
# This creates a simple PDF with text + a handwriting-like annotation.
import tempfile, os

# Create a minimal test PDF on the VM
s.execute(
    "python3 -c \""
    "from reportlab.lib.pagesizes import letter; "
    "from reportlab.pdfgen import canvas; "
    "c = canvas.Canvas('/tmp/test_doc.pdf', pagesize=letter); "
    "c.setFont('Helvetica', 12); "
    "c.drawString(72, 700, 'MEMORANDUM - Budget Report 2026'); "
    "c.drawString(72, 680, 'TO: Faculty Senate  FROM: Office of the Dean'); "
    "c.drawString(72, 640, 'The proposed budget allocates resources across three categories.'); "
    "c.setFont('Helvetica', 14); "
    "c.setFillColorRGB(0.05, 0.05, 0.5); "
    "c.drawString(400, 700, 'Approved - JW'); "
    "c.drawString(72, 500, 'Check these numbers!!'); "
    "c.save(); "
    "print('Test PDF created')\" 2>/dev/null || echo 'reportlab not available, upload manually via the UI'"
)

# Upload via API
s.execute(
    f"sg docker -c 'docker exec paperless-webserver-1 python manage.py document_importer /usr/src/paperless/consume/ 2>/dev/null' || true; "
    f"curl -s -X POST http://localhost:8000/api/documents/post_document/ "
    f"-H \"Authorization: Token {PAPERLESS_TOKEN}\" "
    f"-F document=@/tmp/test_doc.pdf "
    f"-F title=\"Test Budget Memo\" "
    f"| python3 -m json.tool 2>/dev/null || echo 'Upload via API — check Paperless UI for the document'"
)
print("Document uploaded. Check Paperless UI for the document ID.")
'@

    # ── Part 4: Verify Phase 4 (Kafka events) ──

    New-MdCell "---`n# Part 4 — Verify Kafka events (Phase 4)"

    New-MdCell @"
## Step 19 — Check that the upload event landed in Redpanda

When a document is uploaded, the ``paperless_ml`` Django signal handler publishes a ``paperless.uploads`` event to Redpanda. This cell reads the topic to verify.
"@

    New-CodeCell @'
s.execute(
    "sg docker -c 'docker exec redpanda rpk topic consume paperless.uploads "
    "--num 5 --offset start 2>/dev/null' || "
    "echo 'No events yet — upload a document first, or rpk not available'"
)
'@

    New-MdCell @"
## Step 20 — Test the region slicer (dry run)

Runs region detection on the uploaded document without uploading crops to MinIO. Confirms that the slicer can reach Paperless and the detection algorithm works on a real PDF.

**Replace ``--doc-id 1`` with the actual document ID if different.**
"@

    New-CodeCell @'
s.execute(
    f"cd ~/paperless_data_integration && "
    f"sg docker -c 'docker compose -f region_slicer/compose.yml run --rm slicer "
    f"demo.py --doc-id 1 --dry-run --paperless-token {PAPERLESS_TOKEN}'"
)
'@

    New-MdCell @"
## Step 21 — Test the region slicer (full run)

Same as above but also crops each detected region and uploads the crop PNGs to MinIO. After this, check the MinIO Console (``http://<floating-ip>:9001``) → ``paperless-images`` bucket → ``documents/1/regions/`` to see the crop files.
"@

    New-CodeCell @'
s.execute(
    f"cd ~/paperless_data_integration && "
    f"sg docker -c 'docker compose -f region_slicer/compose.yml run --rm slicer "
    f"demo.py --doc-id 1 --paperless-token {PAPERLESS_TOKEN}'"
)
'@

    # ── Part 5: Print URLs ──

    New-MdCell "---`n# Part 5 — Access URLs"

    New-MdCell "## Step 22 — Print access URLs"

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
print(f"  Paperless UI       ->  http://{floating_ip}:8000           (admin / admin)")
print(f"  HTR review         ->  http://{floating_ip}:8000/ml/htr-review")
print(f"  Semantic search    ->  http://{floating_ip}:8000/ml/search")
print(f"  MinIO Console      ->  http://{floating_ip}:9001           (admin / paperless_minio)")
print(f"  Adminer (Postgres) ->  http://{floating_ip}:5050           (user / paperless_postgres)")
print(f"  Redpanda Console   ->  http://{floating_ip}:8090")
print(f"  Qdrant Dashboard   ->  http://{floating_ip}:6333/dashboard")
print()
print(f"  API Token: {PAPERLESS_TOKEN}")
print(f"  SSH: ssh -i ~/.ssh/id_rsa_chameleon cc@{floating_ip}")
'@

    # ── Teardown ──

    New-MdCell "---`n# Teardown`n`nRun this when you are done to release VM resources."

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
