import os
import sys
import time
import datetime
import hvac
import requests

# Add Vault script directory to path
sys.path.append('Z:\\tools_enc\\Vault')
from config_vault import vault_addr, vault_role_id, vault_secret_id

# Paperless-NGX API configuration
BASE_API_URL = "http://10.0.0.1:8010/api"
WATCH_DIR = "Z:\\factures\\Castorama"
IGNORED_PATHS = [
    "Z:\\",
    "/mnt/"
]

# Store submitted tasks for batch checking later
submitted_tasks = {}


# Logging helper function
def log_message(message):
    """Print messages with a timestamp."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


# Vault authentication to get Paperless API token
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


# Retrieve API token
PAPERLESS_API_TOKEN = get_token_from_vault(vault_addr, vault_role_id, vault_secret_id, "scripts-kv/paperless-ngx",
                                           "syno_import_api")

# Ensure we have a valid token
if not PAPERLESS_API_TOKEN:
    sys.exit("❌ Exiting: Unable to retrieve Paperless API token from Vault.")

# Headers for authentication
HEADERS = {
    "Authorization": f"Token {PAPERLESS_API_TOKEN}",
    "Accept": "application/json"
}


# Retrieve existing tags
def get_existing_tags():
    """Retrieve all existing tags and return a dictionary {lowercase name: id}"""
    response = requests.get(f"{BASE_API_URL}/tags/", headers=HEADERS)

    if response.status_code == 200:
        try:
            data = response.json()
            return {tag["name"].lower(): tag["id"] for tag in data.get("results", [])}
        except Exception as e:
            log_message(f"⚠️ Error parsing tags response: {e}")
            return {}

    log_message(f"⚠️ Failed to fetch tags: {response.text}")
    return {}


# Create a new tag
def create_tag(tag_name):
    """Create a tag if it doesn't exist and return its ID"""
    tag_name = tag_name.lower().strip()
    response = requests.post(f"{BASE_API_URL}/tags/", headers=HEADERS, json={"name": tag_name})

    if response.status_code in [200, 201]:  # Created or already exists
        return response.json()["id"]

    log_message(f"⚠️ Failed to create tag '{tag_name}': {response.text}")
    return None


# Extract relevant tags from the file path
def get_tags_from_path(file_path, existing_tags):
    """Extract relevant folder names from the file path and convert them to tag IDs."""
    normalized_path = os.path.normpath(file_path)

    # Remove ignored prefixes
    for ignored in IGNORED_PATHS:
        normalized_ignored = os.path.normpath(ignored)
        if normalized_path.startswith(normalized_ignored):
            normalized_path = normalized_path[len(normalized_ignored):]
            break

    # Extract only the directory path (exclude filename)
    parent_directory = os.path.dirname(normalized_path)
    folder_names = parent_directory.split(os.sep)
    tag_ids = []

    for folder in folder_names:
        folder = folder.strip().lower()
        if folder and folder not in existing_tags:
            new_tag_id = create_tag(folder)
            if new_tag_id:
                existing_tags[folder] = new_tag_id
                tag_ids.append(new_tag_id)
        elif folder:
            tag_ids.append(existing_tags[folder])

    return tag_ids


# Upload a document without waiting for task status
def upload_document(file_path, existing_tags):
    """Upload a file to Paperless and store task ID for later processing"""
    with open(file_path, "rb") as file:
        tag_ids = get_tags_from_path(file_path, existing_tags)

        data = {
            "title": os.path.basename(file_path),
            "tags": tag_ids
        }

        files = {"document": file}

        response = requests.post(f"{BASE_API_URL}/documents/post_document/", headers=HEADERS, data=data, files=files)

        if response.status_code == 200:
            task_id = response.text.strip().replace('"', '')
            submitted_tasks[task_id] = file_path
            log_message(f"📨 Document '{file_path}' submitted (Task UUID: {task_id})")
        else:
            log_message(f"❌ Error submitting document '{file_path}': {response.text}")


# Check all task statuses at the end and generate a report
def check_all_tasks():
    """Check the status of all submitted tasks and generate a final report"""
    log_message("\n🔍 Checking task statuses for all uploaded documents...\n")
    results = {"imported": [], "duplicates": [], "failed": []}

    for task_id, file_path in submitted_tasks.items():
        task_url = f"{BASE_API_URL}/tasks/?task_id={task_id}"

        for _ in range(10):  # Polling up to 10 times with delays
            response = requests.get(task_url, headers=HEADERS)

            if response.status_code == 200:
                try:
                    task_data = response.json()
                    if isinstance(task_data, list) and len(task_data) > 0:
                        task = task_data[0]
                        status = task.get("status")
                        file_name = task.get("task_file_name", file_path)

                        if status == "SUCCESS":
                            doc_id = task.get("related_document")
                            results["imported"].append((file_name, doc_id))
                            break
                        elif status == "FAILURE":
                            result_message = task.get("result", "Unknown error")
                            if "duplicate" in result_message.lower():
                                duplicate_id = task.get("related_document")
                                results["duplicates"].append((file_name, duplicate_id))
                            else:
                                results["failed"].append((file_name, result_message))
                            break
                except Exception as e:
                    log_message(f"⚠️ Error processing task response for {file_path}: {e}")

            time.sleep(5)

    log_message("\n📊 FINAL REPORT 📊")
    log_message(f"✅ Imported: {len(results['imported'])}")
    for file_name, doc_id in results["imported"]:
        log_message(f"   - {file_name} (ID: {doc_id})")

    log_message(f"⚠️ Duplicates: {len(results['duplicates'])}")
    for file_name, duplicate_id in results["duplicates"]:
        log_message(f"   - {file_name} (Existing ID: {duplicate_id})")

    log_message(f"❌ Failed: {len(results['failed'])}")
    for file_name, reason in results["failed"]:
        log_message(f"   - {file_name} (Error: {reason})")


# Main function to process files
def main():
    existing_tags = get_existing_tags()
    for root, _, files in os.walk(WATCH_DIR):
        for filename in files:
            file_path = os.path.join(root, filename)
            upload_document(file_path, existing_tags)

    check_all_tasks()


if __name__ == "__main__":
    main()