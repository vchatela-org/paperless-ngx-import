# Paperless-NGX Import

A set of scripts and containerized jobs to automatically import documents into **Paperless-NGX**, handling **deduplication, tagging, logging, and queue monitoring**.  
Supports **local execution, Docker, and Kubernetes CronJobs** with **Vault + Harbor integration**.

---

## Features

- 🔄 Automatic deduplication & tagging from folder structure  
- 🔐 Secure API token retrieval via HashiCorp Vault  
- 📂 NFS storage support for documents and logs  
- 🐳 Containerized with Kubernetes CronJob support  
- 📊 Detailed logging & stuck-task detection  
- ✅ Harbor registry integration for image hosting  

---

## 1. Local Script Setup

### Prerequisites
- Python virtual environment (`~/paperless-venv`)  
- Dependencies:
  - `hvac` (Vault client)
  - `requests` (HTTP library)
- Access to Paperless-NGX API + Vault

### Install
```bash
~/paperless-venv/bin/pip install -r requirements.txt
````

### Run

```bash
cd "/mnt/z/tools/Docker Apps/Paperless-ngx"
~/paperless-venv/bin/python import_to_paperless.py
```

### Script Workflow

1. Retrieves API token from Vault
2. Scans watch directory
3. Deduplicates by checksum + filename
4. Creates tags based on folder structure
5. Uploads documents
6. Monitors task queue

### Task Queue Status

* **Active**: `PENDING`, `RECEIVED`, `STARTED`, `RETRY`
* **Completed**: `SUCCESS`, `FAILURE`, `REVOKED`

Utilities:

```bash
~/paperless-venv/bin/python check_tasks.py
~/paperless-venv/bin/python -c "from import_to_paperless import acknowledge_completed_tasks; acknowledge_completed_tasks()"
```

### Host-Specific Watch Paths

* **Valentin-PC**: `/mnt/z/factures/`
* **docker-vm**: `/mnt/factures/`
* **default**: Valentin-PC

Ignored: `#recycle`, `@eaDir`, `.url`, `.pkpass`, `.xlsx`, `.xls`, `.html`, `.ini`, `.lnk`, `.exe`, `.msi`, `.bat`, `.cmd`

---

## 2. Docker & Kubernetes Setup

### Build Image

```bash
./build-and-push.sh
```

### Run Locally

```bash
docker run --rm \
  -e PAPERLESS_API_URL="https://paperless.example.com/api" \
  -e PAPERLESS_API_TOKEN="your-token" \
  -v /path/to/documents:/mnt/documents:ro \
  -v ./logs:/app/logs \
  your-registry.com/paperless-import:latest
```

### Kubernetes Deployment

```bash
kubectl create namespace paperless
kubectl apply -f k8s-cronjob.yaml
```

Edit `k8s-cronjob.yaml` to:

* Use your Harbor image
* Update NFS mounts for `/mnt/documents` + `/app/logs`
* Configure schedule (default: hourly)

### Logs & Monitoring

```bash
kubectl get cronjobs -n paperless
kubectl get jobs -n paperless
kubectl logs -n paperless job/paperless-import-<job-id>
```

---

## 3. Vault Setup (Kubernetes)

### Store Token

```bash
vault kv put scripts-kv/paperless-ngx syno_import_api="your-api-token"
```

### Policy (`paperless-import-policy.hcl`)

```hcl
path "scripts-kv/data/paperless-ngx" { capabilities = ["read"] }
path "scripts-kv/metadata/paperless-ngx" { capabilities = ["read"] }
```

```bash
vault policy write paperless-import paperless-import-policy.hcl
```

### Kubernetes Auth

```bash
vault auth enable kubernetes
vault write auth/kubernetes/config \
  token_reviewer_jwt="$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
  kubernetes_host="https://kubernetes.default.svc.cluster.local:443" \
  kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
```

### Vault Role

```bash
vault write auth/kubernetes/role/paperless-import \
    bound_service_account_names=paperless-import \
    bound_service_account_namespaces=paperless \
    policies=paperless-import \
    ttl=24h
```

### Test Integration

```bash
kubectl apply -f vault-test-pod.yaml
kubectl exec vault-test -n paperless -- cat /vault/secrets/env
kubectl delete pod vault-test -n paperless
```

Vault secret structure:

```json
{ "syno_import_api": "your-api-token" }
```

Injected as:

```bash
export PAPERLESS_API_TOKEN="${syno_import_api}"
```

---

## 4. Harbor Setup

### Create Project

* URL: `https://harbor.k3s.internal.valentincloud.fr/`
* Project: `import-paperless-ngx` (Public, with vuln scanning)
* Registry: `harbor.k3s.internal.valentincloud.fr/import-paperless-ngx`

### Login & Push

```bash
docker login harbor.k3s.internal.valentincloud.fr
./build-and-push.sh
```

Image URL:

```
harbor.k3s.internal.valentincloud.fr/import-paperless-ngx/paperless-import:latest
```

### Kubernetes Pull

✅ Public projects: no `imagePullSecrets` needed
🔒 For private projects: use Harbor robot accounts

---

## 5. Logging & Exit Codes

* Logs → `stdout` + `/app/logs/paperless_import_YYYYMMDD.log`
* Auto-rotated with `LOG_RETENTION_DAYS` (default 30)

Exit codes:

* `0` → success
* `1` → partial errors
* `2` → critical errors

---

## 6. Troubleshooting

* **API failures** → check `PAPERLESS_API_URL` + token
* **No files** → check NFS + `WATCH_DIR`
* **Permission issues** → container UID = `1000`
* **Vault failures** → check agent sidecar logs & policies
* **Harbor pull issues** → test with `docker pull` or `kubectl run test-pull`

---

## 7. Security Best Practices

* Vault policy = least privilege
* Namespace isolation (`paperless`)
* Vault token TTL = 24h
* Prefer Harbor **robot accounts** for K8s pulls
* Enable Harbor vulnerability scanning
* Use TLS verification in production

---