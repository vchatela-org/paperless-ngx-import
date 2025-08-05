# Paperless-NGX Import Docker Container

A containerized version of the Paperless-NGX import script, designed for Kubernetes CronJobs with NFS storage.

## Features

- ✅ **Container-ready**: No more host-specific paths or dependencies
- ✅ **Environment-based configuration**: All settings via environment variables
- ✅ **NFS support**: Perfect for Kubernetes with NFS storage
- ✅ **Kubernetes CronJob ready**: Designed for scheduled execution
- ✅ **Simplified**: No Vault dependency - API token via environment
- ✅ **Logging**: Stdout logging for Kubernetes + file logging

## Quick Start

### 1. Build the Image

```bash
# Update registry URL in build-and-push.sh
./build-and-push.sh
```

### 2. Test Locally with Docker Compose

```bash
# Edit docker-compose.yml with your settings
docker-compose up
```

### 3. Deploy to Kubernetes

```bash
# Set up Vault integration (see VAULT-SETUP.md for details)
vault kv put scripts-kv/paperless-ngx syno_import_api="your-api-token"

# Edit k8s-cronjob.yaml with your NFS settings
kubectl apply -f k8s-cronjob.yaml
```

## Configuration

### Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `PAPERLESS_API_URL` | Paperless-NGX API endpoint | `https://paperless.example.com/api` |
| `PAPERLESS_API_TOKEN` | API token for authentication | `your-api-token-here` |

**Note**: In Kubernetes, the API token is sourced from HashiCorp Vault instead of environment variables.

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCH_DIR` | `/mnt/documents` | Directory to scan for documents |
| `IGNORED_PATHS` | `/mnt/` | Comma-separated paths to ignore from tagging |
| `IGNORED_FOLDERS` | `#recycle,@eaDir` | Comma-separated folder names to ignore |
| `IGNORED_EXTENSIONS` | `.url,.pkpass,.xlsx,...` | Comma-separated file extensions to ignore |
| `LOG_RETENTION_DAYS` | `30` | Number of days to keep log files |

## Docker Usage

### Basic Run

```bash
docker run --rm \
  -e PAPERLESS_API_URL="https://paperless.example.com/api" \
  -e PAPERLESS_API_TOKEN="your-token" \
  -v /path/to/documents:/mnt/documents:ro \
  -v ./logs:/app/logs \
  your-registry.com/paperless-import:latest
```

### With Custom Configuration

```bash
docker run --rm \
  -e PAPERLESS_API_URL="https://paperless.example.com/api" \
  -e PAPERLESS_API_TOKEN="your-token" \
  -e WATCH_DIR="/mnt/documents" \
  -e IGNORED_FOLDERS="#recycle,@eaDir,.DS_Store" \
  -e LOG_RETENTION_DAYS="7" \
  -v /path/to/documents:/mnt/documents:ro \
  -v ./logs:/app/logs \
  your-registry.com/paperless-import:latest
```

## Kubernetes Deployment

### 1. Create Namespace

```bash
kubectl create namespace paperless
```

### 2. Set up Vault Integration

See [VAULT-SETUP.md](VAULT-SETUP.md) for complete instructions:

```bash
# Store API token in Vault
vault kv put scripts-kv/paperless-ngx syno_import_api="your-api-token"

# Create Vault policy and role (see VAULT-SETUP.md)
```

### 3. Update Configuration

Edit `k8s-cronjob.yaml`:

- Update the image name to your registry
- Update NFS server and path settings for documents and logs
- Update the schedule if needed (default: every hour)

### 4. Deploy

```bash
kubectl apply -f k8s-cronjob.yaml
```

### 4. Monitor

```bash
# Check CronJob status
kubectl get cronjobs -n paperless

# Check recent jobs
kubectl get jobs -n paperless

# Check logs
kubectl logs -n paperless job/paperless-import-<job-id>
```

## NFS Configuration

The container expects documents to be mounted at `/mnt/documents`. For Kubernetes with NFS:

```yaml
volumes:
- name: documents-volume
  nfs:
    server: your-nfs-server.local
    path: /path/to/documents
    readOnly: true
```

## Logging

The container logs to both:
- **stdout/stderr**: For Kubernetes log collection
- **File**: `/app/logs/paperless_import_YYYYMMDD.log`

Log files are automatically rotated and cleaned up based on `LOG_RETENTION_DAYS`.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Completed with some errors |
| 2 | Critical errors (configuration, API issues) |

## Troubleshooting

### Common Issues

1. **API Connection Failed**
   - Check `PAPERLESS_API_URL` is correct
   - Verify `PAPERLESS_API_TOKEN` is valid
   - Ensure network connectivity from container

2. **No Files Found**
   - Verify NFS mount is working
   - Check `WATCH_DIR` path
   - Ensure files aren't in ignored folders/extensions

3. **Permission Issues**
   - Container runs as user ID 1000
   - Ensure NFS permissions allow read access

### Debug Commands

```bash
# Test API connectivity
curl -H "Authorization: Token your-token" \
     "https://paperless.example.com/api/documents/?page_size=1"

# Check NFS mount
kubectl exec -it deployment/test -- ls -la /mnt/documents

# View detailed logs
kubectl logs -f job/paperless-import-<job-id> -n paperless
```

## Building and Customization

### Build Script

The `build-and-push.sh` script:
1. Builds the Docker image
2. Shows image size
3. Optionally pushes to your registry

### Customizing the Script

The main script is `import_to_paperless_docker.py`. Key areas for customization:

- **File filtering**: Modify `IGNORED_EXTENSIONS` or `IGNORED_FOLDERS`
- **Tagging logic**: Update `get_tags_from_path()` function
- **Error handling**: Adjust retry logic or timeouts
- **Logging**: Modify log format or levels

## License

Same as the original Paperless-NGX import script.
