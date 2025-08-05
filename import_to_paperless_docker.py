#!/usr/bin/env python3

import os
import sys
import time
import datetime
import requests
import hashlib
import glob
import logging
from logging.handlers import RotatingFileHandler

# ----------------------------
# Container Configuration via Environment Variables
# ----------------------------
def get_container_config():
    """Get configuration from environment variables for containerized deployment"""
    config = {
        "WATCH_DIR": os.getenv("WATCH_DIR", "/mnt/documents"),
        "PAPERLESS_API_URL": os.getenv("PAPERLESS_API_URL"),
        "PAPERLESS_API_TOKEN": os.getenv("PAPERLESS_API_TOKEN"),
        "IGNORED_PATHS": os.getenv("IGNORED_PATHS", "/mnt/").split(","),
        "IGNORED_FOLDERS": os.getenv("IGNORED_FOLDERS", "#recycle,@eaDir").split(","),
        "IGNORED_EXTENSIONS": os.getenv("IGNORED_EXTENSIONS", ".url,.pkpass,.xlsx,.xls,.html,.htm,.ini,.lnk,.exe,.msi,.bat,.cmd,.doc,.docx,.db,.mp4,.zip").split(","),
        "LOG_RETENTION_DAYS": int(os.getenv("LOG_RETENTION_DAYS", "30"))
    }
    
    # Validate required configuration
    required_configs = ["PAPERLESS_API_URL", "PAPERLESS_API_TOKEN"]
    missing_configs = [key for key in required_configs if not config[key]]
    
    if missing_configs:
        raise ValueError(f"Missing required environment variables: {missing_configs}")
    
    return config

# Load configuration
try:
    config = get_container_config()
except ValueError as e:
    print(f"Configuration error: {e}")
    sys.exit(2)

# Set configuration variables
WATCH_DIR = config["WATCH_DIR"]
BASE_API_URL = config["PAPERLESS_API_URL"]
IGNORED_FOLDERS = [folder.strip() for folder in config["IGNORED_FOLDERS"] if folder.strip()]
IGNORED_EXTENSIONS = [ext.strip() for ext in config["IGNORED_EXTENSIONS"] if ext.strip()]
IGNORED_PATHS = [path.strip() for path in config["IGNORED_PATHS"] if path.strip()]

# Global variables
submitted_tasks = {}

# ----------------------------
# Logging Configuration
# ----------------------------
def setup_logging():
    """Setup logging to file with rotation"""
    # Create logs directory if it doesn't exist
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    # Generate log filename with current date
    log_filename = f"paperless_import_{datetime.datetime.now().strftime('%Y%m%d')}.log"
    log_path = os.path.join(log_dir, log_filename)
    
    # Clean up old log files
    cleanup_old_logs(log_dir)
    
    # Configure logging - in containers, also log to stdout
    handlers = [
        logging.FileHandler(log_path, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)  # For Kubernetes logging
    ]
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers
    )
    
    return logging.getLogger(__name__)

def cleanup_old_logs(log_dir):
    """Remove log files older than configured retention days"""
    try:
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=config["LOG_RETENTION_DAYS"])
        log_pattern = os.path.join(log_dir, "paperless_import_*.log")
        
        for log_file in glob.glob(log_pattern):
            try:
                file_time = datetime.datetime.fromtimestamp(os.path.getctime(log_file))
                if file_time < cutoff_date:
                    os.remove(log_file)
                    print(f"Removed old log file: {os.path.basename(log_file)}")
            except Exception as e:
                print(f"Warning: Could not process log file {log_file}: {e}")
    except Exception as e:
        print(f"Warning: Could not clean up old logs: {e}")

# Global variables for error tracking
logger = None
has_errors = False
has_critical_errors = False

def log_message(message, level="INFO"):
    """Print messages with a timestamp and log to file."""
    global has_errors, has_critical_errors
    
    if level == "ERROR":
        has_errors = True
    elif level == "CRITICAL":
        has_critical_errors = True
    
    # Clean message for file logging (remove emojis for container logs)
    clean_message = message
    emojis_to_remove = ["❌", "⚠️", "✅", "📨", "📄", "🔍", "⏳", "📊", "🚫", "⏭️", "📁", "🧹", "ℹ️", "🚀"]
    for emoji in emojis_to_remove:
        clean_message = clean_message.replace(emoji, "").strip()
    
    # Log to file if logger is available (without emojis)
    if logger:
        if level == "ERROR":
            logger.error(clean_message)
        elif level == "CRITICAL":
            logger.critical(clean_message)
        elif level == "WARNING":
            logger.warning(clean_message)
        else:
            logger.info(clean_message)

# ----------------------------
# Initialize API Token and Headers
# ----------------------------
PAPERLESS_API_TOKEN = config["PAPERLESS_API_TOKEN"]

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
        log_message(f"Error calculating checksum for {file_path}: {e}", "WARNING")
        return None

# ----------------------------
# Function to Check if Document Already Exists
# ----------------------------
def document_exists(file_path):
    """Check if document already exists in Paperless-ngx by checksum and filename."""
    filename = os.path.basename(file_path)
    checksum = calculate_file_checksum(file_path)
    
    if not checksum:
        log_message(f"Could not calculate checksum for {filename}, skipping existence check", "WARNING")
        return False
    
    # Check by checksum first (most reliable) - limit to 1 result for efficiency
    params = {"checksum__iexact": checksum, "page_size": 1}
    try:
        response = requests.get(f"{BASE_API_URL}/documents/", headers=HEADERS, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("count", 0) > 0:
                existing_doc = data["results"][0]
                log_message(f"Document already exists (checksum match): {filename} -> '{existing_doc.get('title', 'Unknown')}'")
                return True
        else:
            log_message(f"Error checking document by checksum: HTTP {response.status_code}", "WARNING")
    except requests.exceptions.RequestException as e:
        log_message(f"Network error checking document by checksum: {e}", "WARNING")
    
    # Fallback: check by exact filename - limit to 1 result for efficiency  
    params = {"original_filename__iexact": filename, "page_size": 1}
    try:
        response = requests.get(f"{BASE_API_URL}/documents/", headers=HEADERS, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("count", 0) > 0:
                existing_doc = data["results"][0]
                log_message(f"Document already exists (filename match): {filename} -> '{existing_doc.get('title', 'Unknown')}'")
                return True
        else:
            log_message(f"Error checking document by filename: HTTP {response.status_code}", "WARNING")
    except requests.exceptions.RequestException as e:
        log_message(f"Network error checking document by filename: {e}", "WARNING")
    
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
                log_message(f"Error parsing tags response: {e}")
                break
        else:
            log_message(f"Failed to fetch tags: {response.text}", "ERROR")
            break

    log_message(f"Retrieved {len(all_tags)} tags from Paperless-NGX.")
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

    log_message(f"Failed to create tag '{tag_name}': {response.text}", "WARNING")
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
        log_message(f"Skipping file in ignored folder: {file_path}")
        return None  # Not counted as success or failure

    try:
        with open(file_path, "rb") as file:
            tag_ids = get_tags_from_path(file_path, existing_tags)

            data = {"title": os.path.basename(file_path), "tags": tag_ids}
            files = {"document": file}

            response = requests.post(f"{BASE_API_URL}/documents/post_document/", headers=HEADERS, data=data, files=files)

            if response.status_code == 200:
                task_id = response.text.strip().replace('"', '')
                submitted_tasks[task_id] = file_path
                log_message(f"Document '{os.path.basename(file_path)}' submitted (Task UUID: {task_id})")
                return True
            else:
                error_text = response.text.lower()
                # Check if this is an expected issue (unsupported file type or empty file)
                if "not supported" in error_text or "empty" in error_text:
                    log_message(f"Skipping document '{os.path.basename(file_path)}': {response.text}", "WARNING")
                    return None  # Not counted as success or failure
                else:
                    log_message(f"Error submitting document '{os.path.basename(file_path)}': {response.text}", "ERROR")
                    return False
    except Exception as e:
        log_message(f"Exception while uploading '{os.path.basename(file_path)}': {e}", "ERROR")
        return False

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
            log_message(f"Error getting task details for {task_id}: HTTP {response.status_code}", "WARNING")
            return None
    except requests.exceptions.RequestException as e:
        log_message(f"Network error getting task details for {task_id}: {e}", "WARNING")
        return None

def delete_task(task_id):
    """Delete a specific task by its ID"""
    try:
        response = requests.delete(f"{BASE_API_URL}/tasks/{task_id}/", headers=HEADERS, timeout=10)
        if response.status_code in [200, 204]:
            log_message(f"Successfully deleted task: {task_id}")
            return True
        else:
            log_message(f"Failed to delete task {task_id}: HTTP {response.status_code}", "WARNING")
            log_message(f"Response: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        log_message(f"Network error deleting task {task_id}: {e}", "WARNING")
        return False

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
                log_message(f"Acknowledging {len(completed_task_ids)} completed tasks...")
                ack_response = requests.post(
                    f"{BASE_API_URL}/tasks/acknowledge/", 
                    headers=HEADERS, 
                    json={"tasks": completed_task_ids}
                )
                if ack_response.status_code == 200:
                    log_message(f"Successfully acknowledged {len(completed_task_ids)} completed tasks")
                else:
                    log_message(f"Failed to acknowledge tasks: HTTP {ack_response.status_code}", "WARNING")
            else:
                log_message("No completed tasks to acknowledge")
    except requests.exceptions.RequestException as e:
        log_message(f"Error acknowledging tasks: {e}", "WARNING")

def wait_for_queue_to_clear():
    """Wait until the Paperless task queue is empty before checking task statuses"""
    log_message("Waiting for the Paperless queue to clear...")
    
    stuck_task_threshold = 60  # If a task stays the same for 60 seconds, consider it stuck
    task_stuck_timer = {}

    while True:
        response = requests.get(f"{BASE_API_URL}/tasks/", headers=HEADERS)
        if response.status_code == 200:
            tasks = response.json()
            # Based on the API spec, these are the active statuses that should be waited for
            active_statuses = ["PENDING", "RECEIVED", "STARTED", "RETRY"]
            active_tasks = [task for task in tasks if task["status"] in active_statuses]

            log_message(f"Active tasks in queue: {len(active_tasks)}")
            
            # Debug: Log details of remaining tasks
            if active_tasks:
                log_message("Remaining active tasks:")
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
                        log_message(f"   - Task {task_id}: {status} ({task_name}) STUCK for {int(current_time - task_stuck_timer[task_id])}s")
                    else:
                        log_message(f"   - Task {task_id}: {status} ({task_name})")
                
                # Clean up timers for tasks that are no longer active
                task_stuck_timer = {tid: timer for tid, timer in task_stuck_timer.items() if tid in current_task_ids}
            else:
                task_stuck_timer.clear()

            if not active_tasks:
                log_message("Task queue is now empty. Proceeding with final status check.")
                break

        time.sleep(5)

# ----------------------------
# Main Execution
# ----------------------------
def main():
    global logger, has_errors, has_critical_errors
    
    # Initialize logging
    logger = setup_logging()
    log_message("Starting Paperless-NGX Import Process")
    log_message(f"Watch directory: {WATCH_DIR}")
    log_message(f"Paperless API URL: {BASE_API_URL}")
    
    # Check if watch directory exists
    if not os.path.exists(WATCH_DIR):
        log_message(f"Watch directory does not exist: {WATCH_DIR}", "CRITICAL")
        return 2
    
    try:
        existing_tags = get_existing_tags()
        
        # Check if we had critical errors early (like Vault or API issues)
        if has_critical_errors:
            log_message("Critical errors encountered during initialization", "CRITICAL")
            return 2
        
        skipped_existing = 0
        skipped_ignored = 0
        skipped_unsupported = 0
        skipped_api_issues = 0  # For unsupported file types and empty files at API level
        uploaded_count = 0
        failed_uploads = 0

        all_files = []
        for root, _, files in os.walk(WATCH_DIR):
            for filename in files:
                full_path = os.path.join(root, filename)
                try:
                    mod_time = os.path.getmtime(full_path)
                    all_files.append((full_path, mod_time))
                except FileNotFoundError:
                    log_message(f"File not found or inaccessible: {full_path}", "WARNING")

        # Sort by descending modification time
        all_files.sort(key=lambda x: x[1], reverse=True)

        log_message(f"Found {len(all_files)} files to process")

        for file_path, _ in all_files:
            filename = os.path.basename(file_path)
            file_ext = os.path.splitext(filename)[1].lower()
            
            # Check if file is in ignored folder
            if any(ignored_folder.lower() in file_path.lower() for ignored_folder in IGNORED_FOLDERS):
                skipped_ignored += 1
                continue
                
            # Check if file extension is unsupported
            if file_ext in IGNORED_EXTENSIONS:
                log_message(f"Skipping unsupported file type: {filename} ({file_ext})")
                skipped_unsupported += 1
                continue
                
            # Check if document already exists
            if document_exists(file_path):
                skipped_existing += 1
                continue
                
            # Track upload attempts
            upload_success = upload_document(file_path, existing_tags)
            if upload_success is False:  # Explicit False means upload failed
                failed_uploads += 1
            elif upload_success is None:  # None means skipped (ignored folder or API-level issue)
                skipped_api_issues += 1
            else:  # True means successful upload
                uploaded_count += 1

        log_message(f"Processing Summary:")
        log_message(f"   Total files found: {len(all_files)}")
        log_message(f"   Skipped (ignored folders): {skipped_ignored}")
        log_message(f"   Skipped (unsupported types): {skipped_unsupported}")
        log_message(f"   Skipped (already exist): {skipped_existing}")
        log_message(f"   Skipped (API issues - unsupported/empty): {skipped_api_issues}")
        log_message(f"   Submitted for upload: {uploaded_count}")
        if failed_uploads > 0:
            log_message(f"   Failed uploads: {failed_uploads}", "ERROR")
        
        if uploaded_count > 0:
            # First acknowledge any completed tasks to clean up the queue
            acknowledge_completed_tasks()
            # Then wait for the new tasks to complete
            wait_for_queue_to_clear()
        else:
            log_message("No new documents to upload!")

        # Determine exit code based on what happened
        if has_critical_errors:
            log_message("Process completed with critical errors", "CRITICAL")
            return 2
        elif has_errors or failed_uploads > 0:
            log_message("Process completed with some errors", "ERROR")
            return 1
        else:
            log_message("Process completed successfully")
            return 0
            
    except Exception as e:
        log_message(f"Unexpected error in main process: {e}", "CRITICAL")
        return 2

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
