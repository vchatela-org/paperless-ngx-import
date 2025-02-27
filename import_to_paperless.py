import os
import sys
import time
import hvac
import requests

# Add Vault script directory to path
sys.path.append('Z:\\tools_enc\\Vault')
from config_vault import vault_addr, vault_role_id, vault_secret_id

# Paperless-NGX API configuration
BASE_API_URL = "http://10.40.10.10:8010/api"
WATCH_DIR = "Z:\\factures\\Amazon"
IGNORED_PATHS = [
    "Z:\\",
    "/mnt/"
]

def get_token_from_vault(vault_addr, role_id, secret_id, secret_path, secret_key):
    """Retrieve API token from HashiCorp Vault"""
    try:
        client = hvac.Client(url=vault_addr, verify=False)
        client.auth.approle.login(role_id=role_id, secret_id=secret_id)

        if not client.is_authenticated():
            print("❌ Vault authentication failed!")
            return None

        mount_point, secret_path = secret_path.split('/', 1)
        read_response = client.secrets.kv.v2.read_secret_version(mount_point=mount_point, path=secret_path)
        return read_response['data']['data'].get(secret_key, None)

    except Exception as e:
        print(f"❌ Error accessing Vault: {e}")
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


def get_existing_tags():
    """Retrieve all existing tags and return a dictionary {lowercase name: id}"""
    response = requests.get(f"{BASE_API_URL}/tags/", headers=HEADERS)

    if response.status_code == 200:
        try:
            data = response.json()  # Parse JSON response
            tag_dict = {tag["name"].lower(): tag["id"] for tag in data.get("results", [])}  # Extract tags correctly
            return tag_dict
        except Exception as e:
            print(f"⚠️ Error parsing tags response: {e}")
            return {}

    print(f"⚠️ Failed to fetch tags: {response.text}")
    return {}


def create_tag(tag_name):
    """Create a tag if it doesn't exist and return its ID"""
    tag_name = tag_name.lower().strip()  # Normalize to lowercase
    response = requests.post(f"{BASE_API_URL}/tags/", headers=HEADERS, json={"name": tag_name})

    if response.status_code == 201:  # Successfully created
        return response.json()["id"]
    elif response.status_code == 200:  # Tag already exists (rare case)
        return response.json()["id"]
    else:
        print(f"⚠️ Failed to create tag '{tag_name}': {response.text}")
        return None

def get_tags_from_path(file_path, existing_tags):
    """Extract relevant folder names from the file path and convert them to tag IDs."""
    # Normalize path and exclude ignored prefixes
    normalized_path = os.path.normpath(file_path)

    # Find the longest ignored path that matches the beginning of this file path
    for ignored in IGNORED_PATHS:
        normalized_ignored = os.path.normpath(ignored)
        if normalized_path.startswith(normalized_ignored):
            # Remove the ignored prefix from the path
            normalized_path = normalized_path[len(normalized_ignored):]
            break  # Stop after the first match

    # Extract only the directory path (exclude filename)
    parent_directory = os.path.dirname(normalized_path)

    # Extract the remaining directories for tagging
    folder_names = parent_directory.split(os.sep)
    tag_ids = []

    for folder in folder_names:
        folder = folder.strip().lower()  # Normalize tag names
        if folder and folder not in existing_tags:
            new_tag_id = create_tag(folder)
            if new_tag_id:
                existing_tags[folder] = new_tag_id  # Store new tag
                tag_ids.append(new_tag_id)
        elif folder:
            tag_ids.append(existing_tags[folder])

    return tag_ids


import datetime


def log_message(message):
    """Helper function to print messages with a timestamp"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def check_task_status(task_id):
    """Poll the Paperless task API to check if the document was processed successfully"""
    task_url = f"{BASE_API_URL}/tasks/?task_id={task_id}"

    for _ in range(10):  # Polling up to 10 times with delays
        response = requests.get(task_url, headers=HEADERS)

        if response.status_code == 200:
            try:
                task_data = response.json()

                if isinstance(task_data, list) and len(task_data) > 0:  # Ensure response is valid
                    task = task_data[0]  # Get the first (and only) task
                    status = task.get("status")
                    file_name = task.get("task_file_name", "Unknown File")

                    if status == "SUCCESS":
                        doc_id = task.get("related_document")
                        log_message(f"✅ Document '{file_name}' imported successfully (ID: {doc_id})")
                        return True

                    elif status == "FAILURE":
                        result_message = task.get("result", "Unknown error")
                        if "duplicate" in result_message.lower():
                            duplicate_id = task.get("related_document")
                            log_message(
                                f"⚠️ Duplicate detected: '{file_name}' is already in Paperless (ID: {duplicate_id}). Skipping import.")
                            return False
                        else:
                            log_message(f"❌ Import failed for '{file_name}': {result_message}")
                            return False

            except Exception as e:
                log_message(f"⚠️ Error processing task response: {e}")

        time.sleep(5)  # Wait 5 seconds before checking again

    log_message(f"⚠️ Timeout while waiting for document processing (UUID: {task_id})")
    return False


def upload_document(file_path, existing_tags):
    """Upload a file to Paperless and track the processing task"""
    with open(file_path, "rb") as file:
        tag_ids = get_tags_from_path(file_path, existing_tags)

        data = {
            "title": os.path.basename(file_path),  # Use filename as title
            "tags": tag_ids
        }

        files = {"document": file}

        response = requests.post(f"{BASE_API_URL}/documents/post_document/", headers=HEADERS, data=data, files=files)

        if response.status_code == 200:
            # Extract Task UUID from plain text response
            task_id = response.text.strip().replace('"', '')

            print(f"📨 Document submitted for processing (Task UUID: {task_id})")
            check_task_status(task_id)  # Poll the task API
        else:
            print(f"❌ Error submitting document: {file_path}, Response: {response.text}")


def main():
    """Scan the directory and import files into Paperless"""
    existing_tags = get_existing_tags()  # Load existing tags

    for root, _, files in os.walk(WATCH_DIR):
        for filename in files:
            file_path = os.path.join(root, filename)
            upload_document(file_path, existing_tags)


if __name__ == "__main__":
    main()
