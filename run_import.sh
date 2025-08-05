#!/bin/bash

# Paperless-NGX Import Script Runner
# This script ensures the import runs with the correct Python environment

# Set the virtual environment path
VENV_PATH="$HOME/paperless-venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMPORT_SCRIPT="$SCRIPT_DIR/import_to_paperless.py"

# Check if virtual environment exists
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    exit 2
fi

# Check if Python executable exists in venv
if [ ! -f "$VENV_PATH/bin/python" ]; then
    echo "Error: Python executable not found in virtual environment at $VENV_PATH/bin/python"
    exit 2
fi

# Check if import script exists
if [ ! -f "$IMPORT_SCRIPT" ]; then
    echo "Error: Import script not found at $IMPORT_SCRIPT"
    exit 2
fi

# Activate virtual environment and run the import script
echo "Using Python from: $VENV_PATH"
echo "Running import script: $IMPORT_SCRIPT"
echo "----------------------------------------"

# Run the script with the virtual environment's Python
"$VENV_PATH/bin/python" "$IMPORT_SCRIPT"

# Capture and forward the exit code
exit_code=$?
echo "----------------------------------------"
echo "Import script completed with exit code: $exit_code"
exit $exit_code
