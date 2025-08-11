#!/bin/bash

# Paperless Import Docker Build and Push Script
# This script builds the Docker image and pushes it to your private registry

# Configuration - Update these variables for your setup
REGISTRY_URL="registry.example.com"
PROJECT_NAME="homelab"  # Public project name
IMAGE_NAME="paperless-import"
IMAGE_TAG=${1:-latest}
FULL_IMAGE_NAME="${REGISTRY_URL}/${PROJECT_NAME}/${IMAGE_NAME}:${IMAGE_TAG}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Building Paperless Import Docker Image${NC}"
echo "Registry: ${REGISTRY_URL}"
echo "Project: ${PROJECT_NAME}"
echo "Image: ${IMAGE_NAME}"
echo "Tag: ${IMAGE_TAG}"
echo "Full name: ${FULL_IMAGE_NAME}"
echo "----------------------------------------"

# Check if Dockerfile exists
if [ ! -f "Dockerfile" ]; then
    echo -e "${RED}Error: Dockerfile not found in current directory${NC}"
    exit 1
fi

# Check if requirements.txt exists
if [ ! -f "requirements.txt" ]; then
    echo -e "${RED}Error: requirements.txt not found in current directory${NC}"
    exit 1
fi

# Check if Docker containerized script exists
if [ ! -f "import_to_paperless_docker.py" ]; then
    echo -e "${RED}Error: import_to_paperless_docker.py not found in current directory${NC}"
    exit 1
fi

# Build the Docker image
echo -e "${YELLOW}Building Docker image...${NC}"
if docker build -t "${FULL_IMAGE_NAME}" .; then
    echo -e "${GREEN}✓ Docker image built successfully${NC}"
else
    echo -e "${RED}✗ Failed to build Docker image${NC}"
    exit 1
fi

# Tag with latest if not already latest
if [ "${IMAGE_TAG}" != "latest" ]; then
    docker tag "${FULL_IMAGE_NAME}" "${REGISTRY_URL}/${PROJECT_NAME}/${IMAGE_NAME}:latest"
    echo -e "${GREEN}✓ Tagged image as latest${NC}"
fi

# Show image size
echo -e "${YELLOW}Image size:${NC}"
docker images "${FULL_IMAGE_NAME}" --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}"

# Optionally push to registry
read -p "Push to Harbor registry? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Pushing to Harbor registry...${NC}"
    
    # Check if logged in to Harbor (required for pushing)
    echo -e "${YELLOW}Checking Harbor authentication...${NC}"
    if ! docker system info 2>/dev/null | grep -q "${REGISTRY_URL}"; then
        echo -e "${YELLOW}You need to login to Harbor to push images:${NC}"
        echo "  docker login ${REGISTRY_URL}"
        echo ""
        read -p "Login now? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            if ! docker login "${REGISTRY_URL}"; then
                echo -e "${RED}✗ Harbor login failed${NC}"
                exit 1
            fi
        else
            echo -e "${YELLOW}Skipping push - login required${NC}"
            exit 0
        fi
    fi
    
    # Note: Harbor project is public for pulls, but authentication required for pushes
    echo -e "${YELLOW}Note: Harbor project is PUBLIC for pulls, but authentication required for pushes.${NC}"
    
    # Push specific tag
    if docker push "${FULL_IMAGE_NAME}"; then
        echo -e "${GREEN}✓ Successfully pushed ${FULL_IMAGE_NAME}${NC}"
    else
        echo -e "${RED}✗ Failed to push ${FULL_IMAGE_NAME}${NC}"
        echo -e "${YELLOW}Make sure you're logged in: docker login ${REGISTRY_URL}${NC}"
        exit 1
    fi
    
    # Push latest tag if different
    if [ "${IMAGE_TAG}" != "latest" ]; then
        if docker push "${REGISTRY_URL}/${PROJECT_NAME}/${IMAGE_NAME}:latest"; then
            echo -e "${GREEN}✓ Successfully pushed ${REGISTRY_URL}/${PROJECT_NAME}/${IMAGE_NAME}:latest${NC}"
        else
            echo -e "${RED}✗ Failed to push latest tag${NC}"
            exit 1
        fi
    fi
    
    echo -e "${GREEN}✓ All images pushed successfully to Harbor!${NC}"
    echo -e "${GREEN}✓ Available at: https://registry.example.com/harbor/projects/${PROJECT_NAME}/repositories${NC}"
    echo -e "${GREEN}✓ Project is PUBLIC - no authentication needed for pulls${NC}"
else
    echo -e "${YELLOW}Skipping registry push${NC}"
fi

echo -e "${GREEN}Build process completed!${NC}"
echo
echo "To run locally with Docker Compose:"
echo "  docker-compose up"
echo
echo "To deploy to Kubernetes:"
echo "  1. Set up Vault integration (see VAULT-SETUP.md)"
echo "  2. Update NFS server and path settings in k8s-cronjob.yaml"
echo "  3. kubectl apply -f k8s-cronjob.yaml"
