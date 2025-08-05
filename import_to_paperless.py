import os
import sys
import time
import datetime
import platform
import socket
import hvac
import requests
import hashlib

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
    "Valentin-PC": {
        "WATCH_DIR": "/mnt/z/factures/",
        "VAULT_SCRIPT_DIR": "/mnt/z/tools_enc/Vault",
        "IGNORED_PATHS": ["/mnt/z/"]
    },
    "docker-vm": {
        "WATCH_DIR": "/mnt/factures/",
        "VAULT_SCRIPT_DIR": "/root/tools_enc/Vault",
        "IGNORED_PATHS": ["/mnt/"]
    },
    "default": {  # Fallback paths
        "WATCH_DIR": "/mnt/z/factures/",
        "VAULT_SCRIPT_DIR": "/mnt/z/tools_enc/Vault",
        "IGNORED_PATHS": ["/mnt/z/"]
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
BASE_API_URL = "https://paperless.example.com/api"
IGNORED_FOLDERS = ["#recycle", "@eaDir"]  # Ignore all files inside these folders
IGNORED_EXTENSIONS = [".url", ".pkpass", ".xlsx", ".xls", ".html", ".htm", ".ini", ".lnk", ".exe", ".msi", ".bat", ".cmd", ".doc", ".docx"]  # Unsupported file types
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
        client = hvac.Client(url=vault_addr, verify=True)
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
# Function to Calculate File Checksum
# ----------------------------
def calculate_file_checksum(file_path):
    """Calculate MD5 checksum of a file."""
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        log_message(f"⚠️ Error calculating checksum for {file_path}: {e}")
        return None

# ----------------------------
# Function to Check if Document Already Exists
# ----------------------------
def document_exists(file_path):
    """Check if document already exists in Paperless-ngx by checksum and filename."""
    filename = os.path.basename(file_path)
    checksum = calculate_file_checksum(file_path)
    
    if not checksum:
        log_message(f"⚠️ Could not calculate checksum for {filename}, skipping existence check")
        return False
    
    # Check by checksum first (most reliable) - limit to 1 result for efficiency
    params = {"checksum__iexact": checksum, "page_size": 1}
    try:
        response = requests.get(f"{BASE_API_URL}/documents/", headers=HEADERS, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("count", 0) > 0:
                existing_doc = data["results"][0]
                log_message(f"📄 Document already exists (checksum match): {filename} -> '{existing_doc.get('title', 'Unknown')}'")
                return True
        else:
            log_message(f"⚠️ Error checking document by checksum: HTTP {response.status_code}")
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️ Network error checking document by checksum: {e}")
    
    # Fallback: check by exact filename - limit to 1 result for efficiency  
    params = {"original_filename__iexact": filename, "page_size": 1}
    try:
        response = requests.get(f"{BASE_API_URL}/documents/", headers=HEADERS, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("count", 0) > 0:
                existing_doc = data["results"][0]
                log_message(f"📄 Document already exists (filename match): {filename} -> '{existing_doc.get('title', 'Unknown')}'")
                return True
        else:
            log_message(f"⚠️ Error checking document by filename: HTTP {response.status_code}")
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️ Network error checking document by filename: {e}")
    
    return False

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
        if folder.lower() in [ignored.lower() for ignored in IGNORED_FOLDERS]:
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
    if any(ignored_folder.lower() in file_path.lower() for ignored_folder in IGNORED_FOLDERS):
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
            log_message(f"📨 Document '{os.path.basename(file_path)}' submitted (Task UUID: {task_id})")
        else:
            log_message(f"❌ Error submitting document '{os.path.basename(file_path)}': {response.text}")

# ----------------------------
# Processing Queue and Task Status
# ----------------------------
def get_task_details(task_id):
    """Get detailed information about a specific task"""
    try:
        response = requests.get(f"{BASE_API_URL}/tasks/{task_id}/", headers=HEADERS, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            log_message(f"⚠️ Error getting task details for {task_id}: HTTP {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️ Network error getting task details for {task_id}: {e}")
        return None

def acknowledge_completed_tasks():
    """Acknowledge all completed tasks to clean up the queue"""
    try:
        response = requests.get(f"{BASE_API_URL}/tasks/", headers=HEADERS)
        if response.status_code == 200:
            tasks = response.json()
            completed_task_ids = [
                task.get("id") for task in tasks 
                if task.get("status") in ["SUCCESS", "FAILURE"] and not task.get("acknowledged", False)
            ]
            
            if completed_task_ids:
                log_message(f"🧹 Acknowledging {len(completed_task_ids)} completed tasks...")
                ack_response = requests.post(
                    f"{BASE_API_URL}/tasks/acknowledge/", 
                    headers=HEADERS, 
                    json={"tasks": completed_task_ids}
                )
                if ack_response.status_code == 200:
                    log_message(f"✅ Successfully acknowledged {len(completed_task_ids)} completed tasks")
                else:
                    log_message(f"⚠️ Failed to acknowledge tasks: HTTP {ack_response.status_code}")
            else:
                log_message("ℹ️ No completed tasks to acknowledge")
    except requests.exceptions.RequestException as e:
        log_message(f"⚠️ Error acknowledging tasks: {e}")

def wait_for_queue_to_clear():
    """Wait until the Paperless task queue is empty before checking task statuses"""
    log_message("\n⏳ Waiting for the Paperless queue to clear...")
    
    stuck_task_threshold = 60  # If a task stays the same for 60 seconds, consider it stuck
    task_stuck_timer = {}

    while True:
        response = requests.get(f"{BASE_API_URL}/tasks/", headers=HEADERS)
        if response.status_code == 200:
            tasks = response.json()
            # Based on the API spec, these are the active statuses that should be waited for
            active_statuses = ["PENDING", "RECEIVED", "STARTED", "RETRY"]
            active_tasks = [task for task in tasks if task["status"] in active_statuses]

            log_message(f"⏳ Active tasks in queue: {len(active_tasks)}")
            
            # Debug: Log details of remaining tasks
            if active_tasks:
                log_message("🔍 Remaining active tasks:")
                current_task_ids = []
                current_time = time.time()
                
                for task in active_tasks:
                    task_id = task.get("task_id", "Unknown")
                    status = task.get("status", "Unknown")
                    task_name = task.get("task_name", "Unknown")
                    current_task_ids.append(task_id)
                    
                    # Track how long this task has been stuck
                    if task_id not in task_stuck_timer:
                        task_stuck_timer[task_id] = current_time
                    elif current_time - task_stuck_timer[task_id] > stuck_task_threshold:
                        log_message(f"   - Task {task_id}: {status} ({task_name}) ⚠️ STUCK for {int(current_time - task_stuck_timer[task_id])}s")
                    else:
                        log_message(f"   - Task {task_id}: {status} ({task_name})")
                
                # Clean up timers for tasks that are no longer active
                task_stuck_timer = {tid: timer for tid, timer in task_stuck_timer.items() if tid in current_task_ids}
            else:
                task_stuck_timer.clear()

            if not active_tasks:
                log_message("✅ Task queue is now empty. Proceeding with final status check.")
                break

        time.sleep(5)

# ----------------------------
# Main Execution
# ----------------------------
def main():
    existing_tags = get_existing_tags()
    
    skipped_existing = 0
    skipped_ignored = 0
    skipped_unsupported = 0
    uploaded_count = 0

    all_files = []
    for root, _, files in os.walk(WATCH_DIR):
        for filename in files:
            full_path = os.path.join(root, filename)
            try:
                mod_time = os.path.getmtime(full_path)
                all_files.append((full_path, mod_time))
            except FileNotFoundError:
                log_message(f"⚠️ File not found or inaccessible: {full_path}")

    # Sort by descending modification time
    all_files.sort(key=lambda x: x[1], reverse=True)

    log_message(f"🔍 Found {len(all_files)} files to process")

    for file_path, _ in all_files:
        filename = os.path.basename(file_path)
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Check if file is in ignored folder
        if any(ignored_folder.lower() in file_path.lower() for ignored_folder in IGNORED_FOLDERS):
            skipped_ignored += 1
            continue
            
        # Check if file extension is unsupported
        if file_ext in IGNORED_EXTENSIONS:
            log_message(f"🚫 Skipping unsupported file type: {filename} ({file_ext})")
            skipped_unsupported += 1
            continue
            
        # Check if document already exists
        if document_exists(file_path):
            skipped_existing += 1
            continue
            
        # Upload the document
        upload_document(file_path, existing_tags)
        uploaded_count += 1

    log_message(f"📊 Processing Summary:")
    log_message(f"   📁 Total files found: {len(all_files)}")
    log_message(f"   🚫 Skipped (ignored folders): {skipped_ignored}")
    log_message(f"   🚫 Skipped (unsupported types): {skipped_unsupported}")
    log_message(f"   ⏭️ Skipped (already exist): {skipped_existing}")
    log_message(f"   📨 Submitted for upload: {uploaded_count}")
    
    if uploaded_count > 0:
        # First acknowledge any completed tasks to clean up the queue
        acknowledge_completed_tasks()
        # Then wait for the new tasks to complete
        wait_for_queue_to_clear()
    else:
        log_message("✅ No new documents to upload!")

if __name__ == "__main__":
    main()
