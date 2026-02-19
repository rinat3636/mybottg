#!/bin/bash
# ComfyUI Output Cleanup Script for RunPod
# 
# This script should be run on your RunPod instance via cron
# to automatically clean up old generated files.
#
# Installation:
# 1. SSH into your RunPod instance
# 2. Copy this script to /workspace/cleanup.sh
# 3. Make it executable: chmod +x /workspace/cleanup.sh
# 4. Add to crontab: crontab -e
#    0 * * * * /workspace/cleanup.sh >> /workspace/cleanup.log 2>&1
#
# This will run the cleanup every hour.

OUTPUT_DIR="/workspace/ComfyUI/output"
TEMP_DIR="/workspace/ComfyUI/temp"
INPUT_DIR="/workspace/ComfyUI/input"
MAX_AGE_HOURS=24

echo "==================================="
echo "ComfyUI Cleanup - $(date)"
echo "==================================="

# Check if directories exist
if [ ! -d "$OUTPUT_DIR" ]; then
    echo "Warning: Output directory not found: $OUTPUT_DIR"
fi

if [ ! -d "$TEMP_DIR" ]; then
    echo "Warning: Temp directory not found: $TEMP_DIR"
fi

# Disk usage before cleanup
echo "Disk usage before cleanup:"
df -h /workspace

# Clean up output directory
if [ -d "$OUTPUT_DIR" ]; then
    echo "Cleaning output directory: $OUTPUT_DIR"
    DELETED_OUTPUT=$(find "$OUTPUT_DIR" -type f -mmin +$((MAX_AGE_HOURS * 60)) -print -delete | wc -l)
    echo "Deleted $DELETED_OUTPUT file(s) from output directory"
fi

# Clean up temp directory (more aggressive - 1 hour)
if [ -d "$TEMP_DIR" ]; then
    echo "Cleaning temp directory: $TEMP_DIR"
    DELETED_TEMP=$(find "$TEMP_DIR" -type f -mmin +60 -print -delete | wc -l)
    echo "Deleted $DELETED_TEMP file(s) from temp directory"
fi

# Clean up old uploaded images in input directory (7 days)
if [ -d "$INPUT_DIR" ]; then
    echo "Cleaning input directory: $INPUT_DIR"
    DELETED_INPUT=$(find "$INPUT_DIR" -type f -mmin +$((7 * 24 * 60)) -print -delete | wc -l)
    echo "Deleted $DELETED_INPUT file(s) from input directory"
fi

# Clean up empty directories
find "$OUTPUT_DIR" -type d -empty -delete 2>/dev/null
find "$TEMP_DIR" -type d -empty -delete 2>/dev/null

# Disk usage after cleanup
echo "Disk usage after cleanup:"
df -h /workspace

echo "Cleanup completed at $(date)"
echo ""
