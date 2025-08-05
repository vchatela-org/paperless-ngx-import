# Harbor Setup Guide for Paperless Import

## 🏗️ Harbor Configuration

### 1. Create Project in Harbor Web UI

1. **Login** to Harbor: https://registry.example.com/
2. **Create Project**:
   - Click "Projects" → "New Project"
   - **Name**: `import-paperless-ngx`
   - **Access Level**: `Public` ✅
   - **Registry**: `registry.example.com/import-paperless-ngx`
   - Enable vulnerability scanning ✅
   - Click "OK"

### 2. Docker Login to Harbor (Required for Pushing)

Even though the project is public (for pulling), you still need to authenticate to push images:

```bash
# Login to Harbor - required for pushing images
docker login registry.example.com

# Use your Harbor credentials:
# Username: admin (or your Harbor username)
# Password: your-harbor-password
```

**Note**: Public projects in Harbor only make **pulling** images public. **Pushing** still requires authentication.

### 3. Build and Push Image

```bash
# Make script executable (if needed)
chmod +x build-and-push.sh

# Build and push
./build-and-push.sh

# Or with specific tag
./build-and-push.sh v1.0.0
```

The script will:
- Build the image as `registry.example.com/import-paperless-ngx/paperless-import:latest`
- Prompt you to push to Harbor
- Handle both specific tags and latest tag

## 🔐 Kubernetes Authentication

### ✅ No Authentication Needed!

Since your Harbor project `import-paperless-ngx` is **public**, Kubernetes can pull images without any authentication. The `imagePullSecrets` configuration has been removed from the deployment manifests.

### For Private Projects (Reference Only)

If you ever need to make the project private again, you would need:

```bash
# Create the secret for Harbor authentication
kubectl create secret docker-registry harbor-secret \
  --docker-server=registry.example.com \
  --docker-username=admin \
  --docker-password=your-harbor-password \
  --namespace=paperless
```

### Option 2: Use Robot Account (More Secure)

1. **In Harbor Project**:
   - Go to "Robot Accounts" → "New Robot Account"
   - **Name**: `paperless-k8s-pull`
   - **Permissions**: ✅ Pull artifact
   - **Copy the generated token**

2. **Create Secret**:
   ```bash
   kubectl create secret docker-registry harbor-secret \
     --docker-server=registry.example.com \
     --docker-username=robot$import-paperless-ngx+paperless-k8s-pull \
     --docker-password=<robot-token> \
     --namespace=paperless
   ```

## 🚀 Deploy to Kubernetes

### 1. Update Configuration

Edit `k8s-cronjob.yaml`:
- ✅ Image is already set to Harbor
- ✅ Image pull secret is configured
- Update your Paperless API token
- Update NFS settings

### 2. Deploy

```bash
# Create namespace
kubectl create namespace paperless

# Apply the configuration
kubectl apply -f k8s-cronjob.yaml
```

### 3. Verify Deployment

```bash
# Check CronJob
kubectl get cronjobs -n paperless

# Check if image can be pulled
kubectl run test-pull --image=registry.example.com/import-paperless-ngx/paperless-import:latest --namespace=paperless --rm -it --restart=Never -- echo "Image pull successful"

# Clean up test
kubectl delete pod test-pull -n paperless --ignore-not-found
```

## 🔍 Harbor Image Information

After pushing, your image will be available at:
- **Registry**: `registry.example.com`
- **Project**: `import-paperless-ngx` (Public)
- **Repository**: `paperless-import`
- **Full URL**: `registry.example.com/import-paperless-ngx/paperless-import:latest`
- **Web UI**: https://registry.example.com/harbor/projects

## 🛠️ Troubleshooting

### Image Pull Issues

```bash
# Test Docker login
docker pull registry.example.com/import-paperless-ngx/paperless-import:latest

# Check Kubernetes secret (only needed for private projects)
kubectl get secret harbor-secret -n paperless -o yaml

# Test image pull in pod
kubectl run debug --image=registry.example.com/import-paperless-ngx/paperless-import:latest --namespace=paperless --rm -it --restart=Never -- /bin/bash
```

### Common Issues

1. **Authentication Failed**
   - **For pushing**: Always required, even for public projects
   - **For pulling**: Not required for public projects
   - Verify Harbor credentials with: `docker login registry.example.com`
   - Check if user has push access to project

2. **Image Not Found**
   - Verify image was pushed successfully
   - Check project name spelling
   - Ensure image tag exists

3. **Permission Denied**
   - Check Harbor project permissions
   - Verify robot account permissions
   - Ensure Kubernetes secret is correct

### Harbor Web UI

Monitor your images in Harbor:
1. Go to https://registry.example.com/
2. Navigate to Projects → import-paperless-ngx → Repositories
3. View vulnerability scans, tags, and pull statistics

## 🔒 Security Best Practices

1. **Use Robot Accounts** for Kubernetes instead of admin credentials
2. **Enable vulnerability scanning** in Harbor project settings
3. **Set up image signing** (optional) for production
4. **Regular token rotation** for robot accounts
5. **Monitor image pulls** in Harbor dashboard

## 📊 Harbor Features Available

- ✅ **Vulnerability Scanning**: Automatic security scanning
- ✅ **Image Signing**: Notary integration
- ✅ **Replication**: Multi-registry replication
- ✅ **Garbage Collection**: Automatic cleanup
- ✅ **Webhooks**: Integration with CI/CD
- ✅ **API Access**: Full REST API
- ✅ **RBAC**: Role-based access control
