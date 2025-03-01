import os
import sys
import time
import datetime
import platform
import socket
import hvac
import requests

# ----------------------------
# Function to Get Host and OS
# ----------------------------
def get_host_info():
    """Retrieve OS, hostname, and other relevant system details."""
    os_type = platform.system()  # 'Windows' or 'Linux'
    hostname = socket.gethostname()  # Machine's hostname
    return os_type, hostname

# ----------------------------
# Define Host-Specific Paths
# ----------------------------
HOST_PATHS = {
    "DESKTOP-4NAMDD3": {
        "WATCH_DIR": "Z:\\factures\\",
        "VAULT_SCRIPT_DIR": "Z:\\tools_enc\\Vault",
        "IGNORED_PATHS": ["Z:\\"]
    },
    "docker-vm": {
        "WATCH_DIR": "/mnt/factures/",
        "VAULT_SCRIPT_DIR": "/root/tools_enc/Vault",
        "IGNORED_PATHS": ["/mnt/"]
    },
    "default": {  # Fallback paths
        "WATCH_DIR": "Z:\\factures\\",
        "VAULT_SCRIPT_DIR": "Z:\\tools_enc\\Vault",
        "IGNORED_PATHS": ["Z:\\"]
    }
}

# ----------------------------
# Determine Paths Based on Host
# ----------------------------
os_type, hostname = get_host_info()
paths = HOST_PATHS.get(hostname, HOST_PATHS["default"])

WATCH_DIR = paths["WATCH_DIR"]
VAULT_SCRIPT_DIR = paths["VAULT_SCRIPT_DIR"]
IGNORED_PATHS = paths["IGNORED_PATHS"]

# Add Vault script directory to Python path
sys.path.append(VAULT_SCRIPT_DIR)

# Import Vault configuration dynamically
from config_vault import vault_addr, vault_role_id, vault_secret_id

# ----------------------------
# Paperless-NGX API configuration
# ----------------------------
BASE_API_URL = "http://10.0.0.1:8010/api"
IGNORED_FOLDERS = ["#recycle", "@eaDir"]  # Ignore all files inside these folders
submitted_tasks = {}

def log_message(message):
    """Print messages with a timestamp."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

# ----------------------------
# Function to Retrieve API Token from Vault
# ----------------------------
def get_token_from_vault(vault_addr, role_id, secret_id, secret_path, secret_key):
    """Retrieve API token from HashiCorp Vault"""
    try:
        client = hvac.Client(url=vault_addr, verify=False)
        client.auth.approle.login(role_id=role_id, secret_id=secret_id)

        if not client.is_authenticated():
            log_message("❌ Vault authentication failed!")
            return None

        mount_point, secret_path = secret_path.split('/', 1)
        read_response = client.secrets.kv.v2.read_secret_version(mount_point=mount_point, path=secret_path)
        return read_response['data']['data'].get(secret_key, None)

    except Exception as e:
        log_message(f"❌ Error accessing Vault: {e}")
        return None

# Retrieve Paperless API token
PAPERLESS_API_TOKEN = get_token_from_vault(vault_addr, vault_role_id, vault_secret_id, "scripts-kv/paperless-ngx",
                                           "syno_import_api")

if not PAPERLESS_API_TOKEN:
    sys.exit("❌ Exiting: Unable to retrieve Paperless API token from Vault.")

HEADERS = {
    "Authorization": f"Token {PAPERLESS_API_TOKEN}",
    "Accept": "application/json"
}

# ----------------------------
# Retrieve Existing Tags from Paperless
# ----------------------------
def get_existing_tags():
    """Retrieve all existing tags from Paperless-NGX, handling pagination."""
    all_tags = {}
    url = f"{BASE_API_URL}/tags/"

    while url:
        response = requests.get(url, headers=HEADERS)

        if response.status_code == 200:
            try:
                data = response.json()
                for tag in data.get("results", []):
                    all_tags[tag["name"].lower()] = tag["id"]

                url = data.get("next")
            except Exception as e:
                log_message(f"⚠️ Error parsing tags response: {e}")
                break
        else:
            log_message(f"⚠️ Failed to fetch tags: {response.text}")
            break

    log_message(f"✅ Retrieved {len(all_tags)} tags from Paperless-NGX.")
    return all_tags

# ----------------------------
# Handle Document Tagging
# ----------------------------
def get_existing_tag_id(tag_name, existing_tags):
    """Find a tag ID in a case-insensitive way"""
    return next((tid for name, tid in existing_tags.items() if name.lower() == tag_name.lower()), None)

def create_tag(tag_name, existing_tags):
    """Create a tag if it doesn't exist and return its ID"""
    tag_name = tag_name.lower().strip()

    existing_tag_id = get_existing_tag_id(tag_name, existing_tags)
    if existing_tag_id:
        return existing_tag_id

    response = requests.post(f"{BASE_API_URL}/tags/", headers=HEADERS, json={"name": tag_name})

    if response.status_code in [200, 201]:
        tag_id = response.json()["id"]
        existing_tags[tag_name] = tag_id
        return tag_id

    log_message(f"⚠️ Failed to create tag '{tag_name}': {response.text}")
    return None

def get_tags_from_path(file_path, existing_tags):
    """Extract relevant folder names from the file path and convert them to tag IDs."""
    normalized_path = os.path.normpath(file_path)

    for ignored in IGNORED_PATHS:
        if normalized_path.startswith(os.path.normpath(ignored)):
            normalized_path = normalized_path[len(os.path.normpath(ignored)):]
            break

    parent_directory = os.path.dirname(normalized_path)
    folder_names = parent_directory.split(os.sep)
    tag_ids = []

    for folder in folder_names:
        folder = folder.strip()
        if folder.lower() in IGNORED_FOLDERS:
            continue

        sub_tags = folder.split()
        for sub_tag in sub_tags:
            sub_tag = sub_tag.lower()
            tag_id = get_existing_tag_id(sub_tag, existing_tags) or create_tag(sub_tag, existing_tags)
            if tag_id:
                tag_ids.append(tag_id)

    return tag_ids

# ----------------------------
# Upload Documents
# ----------------------------
def upload_document(file_path, existing_tags):
    """Upload a file to Paperless and store task ID for later processing"""
    if any(ignored_folder in file_path.lower() for ignored_folder in IGNORED_FOLDERS):
        log_message(f"🚫 Skipping file in ignored folder: {file_path}")
        return

    with open(file_path, "rb") as file:
        tag_ids = get_tags_from_path(file_path, existing_tags)

        data = {"title": os.path.basename(file_path), "tags": tag_ids}
        files = {"document": file}

        response = requests.post(f"{BASE_API_URL}/documents/post_document/", headers=HEADERS, data=data, files=files)

        if response.status_code == 200:
            task_id = response.text.strip().replace('"', '')
            submitted_tasks[task_id] = file_path
            log_message(f"📨 Document '{file_path}' submitted (Task UUID: {task_id})")
        else:
            log_message(f"❌ Error submitting document '{file_path}': {response.text}")

# ----------------------------
# Processing Queue and Task Status
# ----------------------------
def wait_for_queue_to_clear():
    """Wait until the Paperless task queue is empty before checking task statuses"""
    log_message("\n⏳ Waiting for the Paperless queue to clear...")

    while True:
        response = requests.get(f"{BASE_API_URL}/tasks/", headers=HEADERS)
        if response.status_code == 200:
            tasks = response.json()
            active_tasks = [task for task in tasks if task["status"] not in ["SUCCESS", "FAILURE"]]

            log_message(f"⏳ Active tasks in queue: {len(active_tasks)}")

            if not active_tasks:
                log_message("✅ Task queue is now empty. Proceeding with final status check.")
                break

        time.sleep(5)

# ----------------------------
# Main Execution
# ----------------------------
def main():
    existing_tags = get_existing_tags()
    for root, _, files in os.walk(WATCH_DIR):
        for filename in files:
            upload_document(os.path.join(root, filename), existing_tags)

    log_message(f"📨 Total submitted documents: {len(submitted_tasks)}")
    wait_for_queue_to_clear()

if __name__ == "__main__":
    main()
