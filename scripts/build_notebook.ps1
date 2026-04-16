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

**Image build workflow:** The custom Paperless image is built locally using ``scripts/build_and_push.ps1`` and pushed to ``ghcr.io/redes01/paperless-ngx-ml:latest``. The VM just pulls the pre-built image.

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

    New-MdCell "## Step 14 — Wait for Paperless to become healthy and verify cross-stack DNS"
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

    New-MdCell "## Step 16 — Generate Paperless API token"
    New-CodeCell @'
# Fetch token for the admin user specifically (NOT User.objects.first(),
# which returns AnonymousUser and gives a token with no permissions).
result = s.execute(
    "sg docker -c 'docker exec paperless-webserver-1 python manage.py shell -c \""
    "from rest_framework.authtoken.models import Token; "
    "from django.contrib.auth.models import User; "
    "t, _ = Token.objects.get_or_create(user=User.objects.get(username=\\\"admin\\\")); "
    "print(t.key)\"'"
)
PAPERLESS_TOKEN = result.stdout.strip().split("\n")[-1]
print(f"API Token: {PAPERLESS_TOKEN}")
'@

    New-MdCell "---`n# Part 3 — Region slicer + sample documents"

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
## Step 18 — Upload the sample documents to Paperless

Uploads two committed sample files from ``sample_documents/``:

- ``sample_budget_memo.pdf`` — 2-page PDF with printed text + simulated handwriting
- ``sample_scan.jpeg`` — a scanned-image document (tests the slicer's image-file path)

Paperless processes each upload asynchronously (Tesseract OCR, thumbnail, classification). Expect ~30-60 seconds before the documents are query-able via the API.
"@
    New-CodeCell @'
# Upload both sample files via the Paperless REST API.
s.execute(
    f"for f in ~/paperless_data_integration/sample_documents/sample_budget_memo.pdf "
    f"~/paperless_data_integration/sample_documents/sample_scan.jpeg; do "
    f"echo \"Uploading $f...\"; "
    f"curl -s -X POST http://localhost:8000/api/documents/post_document/ "
    f"-H \"Authorization: Token {PAPERLESS_TOKEN}\" "
    f"-F \"document=@$f\"; "
    f"echo; done"
)
print("\nUploads submitted. Waiting 60 seconds for Paperless to ingest them...")

import time
time.sleep(60)

# List documents to see what Paperless assigned
s.execute(
    f"curl -s http://localhost:8000/api/documents/ "
    f"-H \"Authorization: Token {PAPERLESS_TOKEN}\" "
    f"| python3 -m json.tool | head -80"
)
'@

    New-MdCell @"
## Step 19 — Capture document IDs for the slicer

After the uploads process, grab the integer IDs Paperless assigned. Use these for the slicer runs below.
"@
    New-CodeCell @'
import json

# Fetch document list and extract (id, title) pairs
r = s.execute(
    f"curl -s http://localhost:8000/api/documents/ "
    f"-H \"Authorization: Token {PAPERLESS_TOKEN}\""
)
try:
    data = json.loads(r.stdout.strip().split("\n")[-1])
    docs = [(d["id"], d["title"]) for d in data.get("results", [])]
    print(f"Found {len(docs)} document(s):")
    for doc_id, title in docs:
        print(f"  id={doc_id}  title={title!r}")
    # Pick the first one as default test target
    DOC_ID = docs[0][0] if docs else 1
    print(f"\nUsing DOC_ID = {DOC_ID} for slicer tests below.")
except Exception as exc:
    print(f"Failed to parse document list: {exc}")
    DOC_ID = 1
'@

    New-MdCell "---`n# Part 4 — Verify Kafka events (Phase 4)"

    New-MdCell @"
## Step 20 — Check upload events in Redpanda

When a document is uploaded, the ``paperless_ml`` Django signal handler publishes a ``paperless.uploads`` event. You should see one event per uploaded sample document.
"@
    New-CodeCell @'
s.execute(
    "sg docker -c 'timeout 5 docker exec redpanda rpk topic consume paperless.uploads "
    "--offset start 2>/dev/null; true' || echo 'No events yet'"
)
'@

    New-MdCell "---`n# Part 5 — Test the region slicer"

    New-MdCell @"
## Step 21 — Slicer dry run with Tesseract output preview

Detects handwritten regions without uploading crops to MinIO, and previews Paperless's Tesseract OCR output for the same document.
"@
    New-CodeCell @'
s.execute(
    f"cd ~/paperless_data_integration && "
    f"sg docker -c 'docker compose -f region_slicer/compose.yml run --rm slicer "
    f"demo.py --doc-id {DOC_ID} --dry-run --print-ocr --paperless-token {PAPERLESS_TOKEN}'"
)
'@

    New-MdCell @"
## Step 22 — Full slicer run with merge_text demo

Runs the full pipeline (detect + crop + upload to MinIO), then demonstrates ``SlicerResult.merge_text()`` with placeholder HTR outputs. This is the ``merged_text`` string that Phase 2 will write to Postgres and Phase 3 will chunk + upsert to Qdrant.

After this, check the MinIO Console at ``http://<floating-ip>:9001`` → ``paperless-images`` bucket → ``documents/{DOC_ID}/regions/`` to see the crop PNG files.
"@
    New-CodeCell @'
s.execute(
    f"cd ~/paperless_data_integration && "
    f"sg docker -c 'docker compose -f region_slicer/compose.yml run --rm slicer "
    f"demo.py --doc-id {DOC_ID} --demo-merge --paperless-token {PAPERLESS_TOKEN}'"
)
'@

    New-MdCell "---`n# Part 6 — Access URLs"

    New-MdCell "## Step 23 — Print access URLs"
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
