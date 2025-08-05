# Vault Setup for Paperless Import

## 🔐 Vault Configuration Steps

### 1. Store the API Token in Vault

```bash
# Store the Paperless API token in Vault
vault kv put scripts-kv/paperless-ngx syno_import_api="your-actual-paperless-api-token"

# Verify the secret is stored
vault kv get scripts-kv/paperless-ngx
```

### 2. Create Vault Policy for Paperless Import

Create a policy file `paperless-import-policy.hcl`:

```hcl
# Policy for paperless-import service account
path "scripts-kv/data/paperless-ngx" {
  capabilities = ["read"]
}

path "scripts-kv/metadata/paperless-ngx" {
  capabilities = ["read"]
}
```

Apply the policy:

```bash
# Create the policy in Vault
vault policy write paperless-import paperless-import-policy.hcl

# Verify the policy
vault policy read paperless-import
```

### 3. Configure Kubernetes Authentication

If not already configured, set up Kubernetes auth method:

```bash
# Enable Kubernetes auth method (if not already enabled)
vault auth enable kubernetes

# Configure Kubernetes auth method
vault write auth/kubernetes/config \
    token_reviewer_jwt="$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
    kubernetes_host="https://kubernetes.default.svc.cluster.local:443" \
    kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
```

### 4. Create Vault Role for Paperless Import

```bash
# Create a role for the paperless-import service account
vault write auth/kubernetes/role/paperless-import \
    bound_service_account_names=paperless-import \
    bound_service_account_namespaces=paperless \
    policies=paperless-import \
    ttl=24h
```

## 🚀 Deployment Instructions

### 1. Create Namespace

```bash
kubectl create namespace paperless
```

### 2. Update NFS Configuration

Edit `k8s-cronjob.yaml` and update these sections:

```yaml
# Update the documents NFS mount
- name: documents-volume
  nfs:
    server: syno.internal.valentincloud.fr  # Your NFS server
    path: /volume1/factures                 # Your documents path
    readOnly: true

# Update the logs NFS mount
nfs:
  server: syno.internal.valentincloud.fr   # Your NFS server
  path: /volume1/paperless-logs            # Your logs path
```

### 3. Deploy

```bash
# Apply the complete manifest
kubectl apply -f k8s-cronjob.yaml
```

### 4. Verify Deployment

```bash
# Check if ServiceAccount was created
kubectl get serviceaccount paperless-import -n paperless

# Check if RBAC was configured
kubectl get role,rolebinding -n paperless

# Check CronJob
kubectl get cronjobs -n paperless

# Check PV and PVC
kubectl get pv paperless-import-logs-pv
kubectl get pvc paperless-import-logs-pvc -n paperless
```

## 🔍 Testing Vault Integration

### Test Vault Secret Access

```bash
# Create a test pod to verify Vault integration
kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: vault-test
  namespace: paperless
  annotations:
    vault.hashicorp.com/agent-inject: "true"
    vault.hashicorp.com/role: "paperless-import"
    vault.hashicorp.com/agent-inject-secret-env: "scripts-kv/paperless-ngx"
    vault.hashicorp.com/agent-inject-template-env: |
      {{ with secret "scripts-kv/paperless-ngx" }}
      {{ range \$k, \$v := .Data.data }}
      export {{ \$k }}="{{ \$v }}"
      {{ end }}
      {{ end }}
    vault.hashicorp.com/agent-pre-populate-only: "true"
    vault.hashicorp.com/tls-skip-verify: "true"
spec:
  serviceAccountName: paperless-import
  containers:
  - name: test
    image: busybox
    command: ['sleep', '3600']
  restartPolicy: Never
EOF

# Check if secrets were injected
kubectl exec vault-test -n paperless -- cat /vault/secrets/env

# Clean up test pod
kubectl delete pod vault-test -n paperless
```

## 🛠️ Troubleshooting

### Common Issues

1. **Vault Agent Injection Failed**
   ```bash
   # Check Vault agent sidecar logs
   kubectl logs <pod-name> -c vault-agent -n paperless
   
   # Check if Vault is accessible from the cluster
   kubectl run vault-test --image=busybox --rm -it --restart=Never -- wget -qO- http://vault.vault.svc.cluster.local:8200/v1/sys/health
   ```

2. **Authentication Issues**
   ```bash
   # Verify Kubernetes auth role
   vault read auth/kubernetes/role/paperless-import
   
   # Check service account token
   kubectl get serviceaccount paperless-import -n paperless -o yaml
   ```

3. **Secret Access Issues**
   ```bash
   # Test policy permissions
   vault auth -method=kubernetes role=paperless-import
   vault kv get scripts-kv/paperless-ngx
   ```

### Logs and Monitoring

```bash
# Check CronJob execution
kubectl get jobs -n paperless

# View logs from the import job
kubectl logs job/paperless-import-<timestamp> -n paperless

# Check Vault agent logs
kubectl logs job/paperless-import-<timestamp> -c vault-agent -n paperless
```

## 🔒 Security Best Practices

1. **Least Privilege**: The policy only grants read access to the specific secret path
2. **Namespace Isolation**: Service account is bound to the `paperless` namespace only
3. **Token TTL**: Vault tokens have a 24-hour TTL and are automatically renewed
4. **TLS**: Enable TLS verification in production (remove `tls-skip-verify: "true"`)

## 📊 Vault Secret Structure

Your Vault secret should contain:

```bash
# Path: scripts-kv/paperless-ngx
{
  "syno_import_api": "your-actual-paperless-api-token"
}
```

The Vault agent will inject this as:
```bash
export syno_import_api="your-actual-paperless-api-token"
```

And the script will use it as:
```bash
export PAPERLESS_API_TOKEN="${syno_import_api}"
```
