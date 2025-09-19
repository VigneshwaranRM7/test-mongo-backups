
import os
import json
import subprocess
import datetime
from flask import Flask, jsonify
from google.cloud import storage

app = Flask(__name__)

def now_ts():
    return datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")

@app.route("/", methods=["GET"])
def backup_all():
    # GCS bucket (single bucket)
    bucket_name = os.environ.get("GCS_BUCKET")
    if not bucket_name:
        return "Missing GCS_BUCKET env var", 500

   
    mongo_list_raw = os.environ.get("MONGO_LIST")
    if not mongo_list_raw:
        return "Missing MONGO_LIST env var (from Secret Manager)", 500

    try:
        mongo_list = json.loads(mongo_list_raw)
    except Exception as e:
        return f"Invalid MONGO_LIST JSON: {e}", 500

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    timestamp = now_ts()
    results = []

    for entry in mongo_list:
        name = entry.get("name")
        uri = entry.get("uri")
        if not name or not uri:
            results.append({"name": name or "<no-name>", "status": "skipped", "reason": "missing name or uri"})
            continue

        dump_path = f"/tmp/{name}-mongodump-{timestamp}.gz"
        try:
            # run mongodump
            subprocess.run([
                "mongodump",
                f"--uri={uri}",
                f"--archive={dump_path}",
                "--gzip"
            ], check=True, timeout=60*60)  # timeout 1 hour per DB

            # upload
            blob_path = f"backups/{name}/mongodump-{timestamp}.gz"
            blob = bucket.blob(blob_path)
            blob.upload_from_filename(dump_path)
            results.append({"name": name, "status": "ok", "gcs": f"gs://{bucket_name}/{blob_path}"})


        except subprocess.CalledProcessError as cpe:
            results.append({"name": name, "status": "error", "reason": f"mongodump failed: {cpe}"})
        except Exception as ex:
            results.append({"name": name, "status": "error", "reason": str(ex)})
        finally:
            # cleanup tmp file if exists
            try:
                if os.path.exists(dump_path):
                    os.remove(dump_path)
            except Exception:
                pass

    return jsonify(results)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
