# Paperless-NGX Import Script

This script automatically imports documents from a watch directory to Paperless-NGX, handling deduplication, tagging, and queue monitoring.

## Prerequisites

- Python virtual environment with required packages
- Access to HashiCorp Vault for API token retrieval
- Paperless-NGX instance running and accessible

## Setup

### 1. Virtual Environment

The script requires a Python virtual environment with the necessary dependencies. Use the virtual environment located at `~/paperless-venv`.

### 2. Dependencies

Install the required packages:
```bash
~/paperless-venv/bin/pip install -r requirements.txt
```

Required packages:
- `hvac` - HashiCorp Vault client
- `requests` - HTTP library

## Usage

### Running the Script

```bash
cd "/mnt/z/tools/Docker Apps/Paperless-ngx"
~/paperless-venv/bin/python import_to_paperless.py
```

### What the Script Does

1. **Authentication**: Retrieves API token from HashiCorp Vault
2. **File Discovery**: Scans the watch directory for files
3. **Deduplication**: Checks if documents already exist (by checksum and filename)
4. **Tagging**: Creates tags based on folder structure
5. **Upload**: Submits new documents to Paperless-NGX
6. **Queue Monitoring**: Waits for all tasks to complete

### Task Status Monitoring

The script monitors the Paperless-NGX task queue and considers these statuses as "active":
- `PENDING` - Task is waiting to be processed
- `RECEIVED` - Task has been received by the worker
- `STARTED` - Task is currently being processed
- `RETRY` - Task is being retried after a failure

Completed statuses:
- `SUCCESS` - Task completed successfully
- `FAILURE` - Task failed permanently
- `REVOKED` - Task was cancelled

#### Task Queue Management
The script automatically:
1. Acknowledges completed tasks before waiting (cleans up the queue)
2. Monitors only active tasks that need to complete
3. Detects tasks that may be stuck (same state for >60 seconds)

If tasks get stuck, you can use the provided utility scripts:
```bash
# Check current tasks
~/paperless-venv/bin/python check_tasks.py

# Acknowledge completed tasks manually
~/paperless-venv/bin/python -c "
from import_to_paperless import acknowledge_completed_tasks
acknowledge_completed_tasks()
"
```

### Configuration

The script automatically detects the host and uses appropriate paths:

#### Host-Specific Paths
- **Valentin-PC**: `/mnt/z/factures/` (watch directory)
- **docker-vm**: `/mnt/factures/` (watch directory)
- **default**: Fallback to Valentin-PC paths

#### Ignored Items
- **Folders**: `#recycle`, `@eaDir`
- **Extensions**: `.url`, `.pkpass`, `.xlsx`, `.xls`, `.html`, `.htm`, `.ini`, `.lnk`, `.exe`, `.msi`, `.bat`, `.cmd`

## Troubleshooting

### Common Issues

1. **Module not found errors**: Ensure you're using the correct virtual environment
2. **Vault authentication failures**: Check vault credentials and network connectivity
3. **API connection issues**: Verify Paperless-NGX is accessible and API token is valid
4. **Tasks stuck in queue**: The script now provides detailed task information for debugging

### Debug Information

The script provides detailed logging including:
- Task IDs and statuses
- File processing summary
- Error messages with timestamps
- Detailed task information for stuck tasks

### Example Output

```
[2025-08-05 13:03:46] ⏳ Active tasks in queue: 1
[2025-08-05 13:03:46] 🔍 Remaining active tasks:
[2025-08-05 13:03:46]    - Task da9d01a3-4420-4820-9537-a6bc1365959c: STARTED (consume_file)
[2025-08-05 13:03:46]      Details: document.pdf | Progress: 50% | Date: 2025-08-05T13:00:00Z
```
