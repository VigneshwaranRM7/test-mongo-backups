import os
import json
import subprocess
import datetime
from typing import Dict, List, Optional, Tuple
import requests
from flask import Flask, jsonify
from google.cloud import storage

app = Flask(__name__)

port = int(os.environ.get("PORT", 8080))
mongo_list_raw = os.environ.get("MONGO_LIST")
bucket_name = os.environ.get("GCS_BUCKET")


def get_current_timestamp() -> str:
    """Generate a timestamp string in YYYYMMDDHHMMSS format."""
    return datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")


def generate_backup_filename(database_name: str) -> str:
    """Generate a compressed backup filename with timestamp."""
    return f"{database_name}-{get_current_timestamp()}.gz"


def send_slack_notification(message: str) -> None:
    """Send a notification to Slack via webhook if configured."""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return
    
    try:
        response = requests.post(webhook_url, json={"text": message}, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Failed to send Slack notification: {e}")


def parse_mongo_config() -> Tuple[List[Dict], Optional[str]]:
    """Parse and validate MongoDB configuration from environment."""
    
    if not mongo_list_raw:
        return [], "Missing MONGO_LIST env var"

    try:
        mongo_list = json.loads(mongo_list_raw)
        if not isinstance(mongo_list, list):
            return [], "MONGO_LIST must be a JSON array"
        return mongo_list, None
    except json.JSONDecodeError as e:
        return [], f"Invalid MONGO_LIST JSON format: {e}"


def create_mongodb_dump(uri: str, output_path: str) -> bool:
    """Create a MongoDB dump using mongodump command."""
    try:
        subprocess.run(
            ["mongodump", f"--uri={uri}", f"--archive={output_path}", "--gzip"],
            check=True,
            timeout=3600
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"mongodump failed with return code {e.returncode}")
        return False
    except subprocess.TimeoutExpired:
        print("mongodump timed out after 1 hour")
        return False


def upload_to_gcs(bucket, blob_path: str, local_file_path: str) -> bool:
    """Upload a file to Google Cloud Storage."""
    try:
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(local_file_path)
        return True
    except Exception as e:
        print(f"Failed to upload to GCS: {e}")
        return False


def cleanup_temp_file(file_path: str) -> None:
    """Safely remove temporary file if it exists."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError as e:
        print(f"Failed to cleanup temp file {file_path}: {e}")


def process_database_backup(entry: Dict, bucket) -> Dict:
    """Process backup for a single database entry."""
    name = entry.get("name")
    uri = entry.get("uri")

    if not name or not uri:
        error_msg = f"Missing name or URI for database entry: {entry}"
        send_slack_notification(error_msg)
        return {"name": name or "<no-name>", "status": "skipped", "reason": "missing name or uri"}

    filename = generate_backup_filename(name)
    dump_path = f"/tmp/{filename}"
    blob_path = f"backups/{name}/{filename}"

    try:
        # Create MongoDB dump
        if not create_mongodb_dump(uri, dump_path):
            error_msg = f"Failed to create dump for {name}"
            send_slack_notification(error_msg)
            return {"name": name, "status": "error", "reason": error_msg}

        # Upload to GCS
        if not upload_to_gcs(bucket, blob_path, dump_path):
            error_msg = f"Failed to upload {name} to GCS"
            send_slack_notification(error_msg)
            return {"name": name, "status": "error", "reason": error_msg}

        # Success
        gcs_url = f"gs://{bucket_name}/{blob_path}"
        success_msg = f"Backup successful: {name} → {gcs_url}"
        send_slack_notification(success_msg)

        return {"name": name, "status": "ok", "gcs": gcs_url}

    except Exception as e:
        error_msg = f"Unexpected error backing up {name}: {str(e)}"
        send_slack_notification(error_msg)
        return {"name": name, "status": "error", "reason": error_msg}

    finally:
        cleanup_temp_file(dump_path)


@app.route("/", methods=["GET"])
def backup_all_databases():
    """Main endpoint to backup all configured MongoDB databases."""
    mongo_list, parse_error = parse_mongo_config()
    if parse_error:
        send_slack_notification(parse_error)
        return parse_error, 500


    if not bucket_name:
        msg = "Missing GCS_BUCKET env var"
        send_slack_notification(msg)
        return msg, 500

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    results = []
    for entry in mongo_list:
        results.append(process_database_backup(entry, bucket))

    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=port)
