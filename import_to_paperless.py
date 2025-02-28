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
BASE_API_URL = "http://10.40.10.10:8010/api"
WATCH_DIR = "Z:\\factures\\"

IGNORED_PATHS = [
    "Z:\\",
    "/mnt/"
]
IGNORED_FOLDERS = ["#recycle"]  # Ignore all files inside these folders

# Store submitted tasks for batch checking later
submitted_tasks = {}


def log_message(message):
    """Print messages with a timestamp."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


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


PAPERLESS_API_TOKEN = get_token_from_vault(vault_addr, vault_role_id, vault_secret_id, "scripts-kv/paperless-ngx",
                                           "syno_import_api")

if not PAPERLESS_API_TOKEN:
    sys.exit("❌ Exiting: Unable to retrieve Paperless API token from Vault.")

HEADERS = {
    "Authorization": f"Token {PAPERLESS_API_TOKEN}",
    "Accept": "application/json"
}


def get_existing_tags():
    """Retrieve all existing tags from Paperless-NGX, handling pagination."""
    all_tags = {}
    url = f"{BASE_API_URL}/tags/"

    while url:  # Keep requesting while there's a next page
        response = requests.get(url, headers=HEADERS)

        if response.status_code == 200:
            try:
                data = response.json()
                for tag in data.get("results", []):
                    all_tags[tag["name"].lower()] = tag["id"]  # Store tags in lowercase

                url = data.get("next")  # Get the next page URL
            except Exception as e:
                log_message(f"⚠️ Error parsing tags response: {e}")
                break
        else:
            log_message(f"⚠️ Failed to fetch tags: {response.text}")
            break

    log_message(f"✅ Retrieved {len(all_tags)} tags from Paperless-NGX.")
    return all_tags


def get_existing_tag_id(tag_name, existing_tags):
    """Find a tag ID in a case-insensitive way"""
    return next((tid for name, tid in existing_tags.items() if name.lower() == tag_name.lower()), None)


def create_tag(tag_name, existing_tags):
    """Create a tag if it doesn't exist and return its ID"""
    tag_name = tag_name.lower().strip()

    # Check if the tag already exists
    existing_tag_id = get_existing_tag_id(tag_name, existing_tags)
    if existing_tag_id:
        return existing_tag_id  # Use the existing tag

    # Create the tag if it doesn't exist
    response = requests.post(f"{BASE_API_URL}/tags/", headers=HEADERS, json={"name": tag_name})

    if response.status_code in [200, 201]:
        tag_id = response.json()["id"]
        existing_tags[tag_name] = tag_id  # Update local cache
        return tag_id
    elif response.status_code == 400 and "unique constraint" in response.text.lower():
        log_message(f"⚠️ Tag '{tag_name}' already exists but may belong to another owner.")
        return get_existing_tag_id(tag_name, existing_tags)  # Return best match

    log_message(f"⚠️ Failed to create tag '{tag_name}': {response.text}")
    return None


def get_tags_from_path(file_path, existing_tags):
    """Extract relevant folder names from the file path and convert them to tag IDs."""
    normalized_path = os.path.normpath(file_path)

    # Remove ignored prefixes
    for ignored in IGNORED_PATHS:
        if normalized_path.startswith(os.path.normpath(ignored)):
            normalized_path = normalized_path[len(os.path.normpath(ignored)):]
            break

    # Extract only the directory path (exclude filename)
    parent_directory = os.path.dirname(normalized_path)
    folder_names = parent_directory.split(os.sep)
    tag_ids = []

    for folder in folder_names:
        folder = folder.strip()

        # Ignore any folders in the ignore list (like #recycle)
        if folder.lower() in IGNORED_FOLDERS:
            continue

        # Split multi-word folders into separate tags
        sub_tags = folder.split()

        for sub_tag in sub_tags:
            sub_tag = sub_tag.lower()

            # Get existing tag ID or create if necessary
            tag_id = get_existing_tag_id(sub_tag, existing_tags) or create_tag(sub_tag, existing_tags)
            if tag_id:
                tag_ids.append(tag_id)

    return tag_ids


def upload_document(file_path, existing_tags):
    """Upload a file to Paperless and store task ID for later processing"""
    # Skip files inside ignored folders
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


def check_all_tasks():
    """Check the status of all submitted tasks and generate a final report"""
    log_message("\n🔍 Checking task statuses for all uploaded documents...\n")
    results = {"imported": [], "duplicates": [], "failed": []}

    for task_id, file_path in submitted_tasks.items():
        response = requests.get(f"{BASE_API_URL}/tasks/?task_id={task_id}", headers=HEADERS)

        if response.status_code == 200:
            try:
                task_data = response.json()
                if isinstance(task_data, list) and len(task_data) > 0:
                    task = task_data[0]
                    status = task.get("status")
                    file_name = task.get("task_file_name", file_path)

                    if status == "SUCCESS":
                        results["imported"].append((file_name, task.get("related_document")))
                    elif status == "FAILURE":
                        result_message = task.get("result", "Unknown error")
                        if "duplicate" in result_message.lower():
                            results["duplicates"].append((file_name, task.get("related_document")))
                        else:
                            results["failed"].append((file_name, result_message))
            except Exception as e:
                log_message(f"⚠️ Error processing task response for {file_path}: {e}")

    log_message("\n📊 FINAL REPORT 📊")
    log_message(f"✅ Imported: {len(results['imported'])}")
    log_message(f"⚠️ Duplicates: {len(results['duplicates'])}")
    log_message(f"❌ Failed: {len(results['failed'])}")


def main():
    existing_tags = get_existing_tags()
    for root, _, files in os.walk(WATCH_DIR):
        for filename in files:
            upload_document(os.path.join(root, filename), existing_tags)

    log_message(f"📨 Total submitted documents: {len(submitted_tasks)}")
    wait_for_queue_to_clear()
    check_all_tasks()


if __name__ == "__main__":
    main()