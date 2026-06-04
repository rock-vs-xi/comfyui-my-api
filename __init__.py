import os
import uuid
import time
import json
from datetime import datetime
from io import BytesIO

import boto3
from botocore.config import Config
from aiohttp import web
from PIL import Image

import folder_paths
from server import PromptServer


PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = "/workspace/config/config.json"
CONFIG_PATH = os.environ.get("MY_API_CONFIG_PATH", DEFAULT_CONFIG_PATH)


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        raise FileNotFoundError(
            f"my_api config not found: {CONFIG_PATH}. "
            f"Copy {os.path.join(PLUGIN_DIR, 'config.example.json')} to this path and fill it."
        )

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    required_keys = [
        "r2_account_id",
        "r2_access_key",
        "r2_secret_key",
        "r2_bucket_name",
        "r2_public_base"
    ]
    missing_keys = [key for key in required_keys if not data.get(key)]

    if missing_keys:
        raise ValueError(f"my_api config missing keys: {', '.join(missing_keys)}")

    return data


config = load_config()

R2_ACCOUNT_ID = config["r2_account_id"]
R2_ACCESS_KEY = config["r2_access_key"]
R2_SECRET_KEY = config["r2_secret_key"]
R2_BUCKET_NAME = config["r2_bucket_name"]
R2_PUBLIC_BASE = config["r2_public_base"].rstrip("/")


s3_client = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    config=Config(
        signature_version="s3v4",
        connect_timeout=10,
        read_timeout=300
    )
)

routes = PromptServer.instance.routes


def elapsed_ms(start):
    return round((time.time() - start) * 1000)


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def check_directory(file_type):
    start = time.time()
    directory = folder_paths.get_directory_by_type(file_type)

    if directory is None:
        return {
            "status": "fail",
            "type": file_type,
            "error": "unknown type",
            "costMs": elapsed_ms(start)
        }

    return {
        "status": "ok" if os.path.isdir(directory) else "fail",
        "type": file_type,
        "path": directory,
        "exists": os.path.isdir(directory),
        "readable": os.access(directory, os.R_OK),
        "writable": os.access(directory, os.W_OK),
        "costMs": elapsed_ms(start)
    }


def check_r2_roundtrip():
    total_start = time.time()
    timings = {}
    key = f"health-check/{uuid.uuid4().hex}.txt"
    body = b"ok"
    deleted = False

    try:
        step = time.time()
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            Body=body,
            ContentType="text/plain"
        )
        timings["putObject"] = elapsed_ms(step)

        step = time.time()
        head = s3_client.head_object(
            Bucket=R2_BUCKET_NAME,
            Key=key
        )
        timings["headObject"] = elapsed_ms(step)

        step = time.time()
        s3_client.delete_object(
            Bucket=R2_BUCKET_NAME,
            Key=key
        )
        deleted = True
        timings["deleteObject"] = elapsed_ms(step)
        timings["total"] = elapsed_ms(total_start)

        return {
            "status": "ok",
            "bucket": R2_BUCKET_NAME,
            "publicBase": R2_PUBLIC_BASE,
            "key": key,
            "contentLength": head.get("ContentLength"),
            "deleted": deleted,
            "timings": timings
        }

    except Exception as e:
        if not deleted:
            try:
                s3_client.delete_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=key
                )
                deleted = True
            except Exception:
                pass

        timings["total"] = elapsed_ms(total_start)
        return {
            "status": "fail",
            "bucket": R2_BUCKET_NAME,
            "publicBase": R2_PUBLIC_BASE,
            "key": key,
            "deleted": deleted,
            "error": str(e),
            "timings": timings
        }


def get_file_path(filename, file_type="temp", subfolder=""):
    base_dir = folder_paths.get_directory_by_type(file_type)
    if base_dir is None:
        raise ValueError(f"unknown type: {file_type}")

    safe_name = os.path.basename(filename)

    if subfolder:
        full_dir = os.path.abspath(os.path.join(base_dir, subfolder))
        if os.path.commonpath((full_dir, os.path.abspath(base_dir))) != os.path.abspath(base_dir):
            raise ValueError("invalid subfolder")
    else:
        full_dir = base_dir

    filepath = os.path.join(full_dir, safe_name)

    if not os.path.isfile(filepath):
        raise FileNotFoundError(filepath)

    return filepath


def trim_transparent_area(image_bytes):
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()

    if bbox is None:
        return image_bytes

    cropped = img.crop(bbox)

    out = BytesIO()
    cropped.save(out, format="PNG")
    return out.getvalue()


@routes.get("/my_api/health")
async def health(request):
    temp_check = check_directory("temp")
    output_check = check_directory("output")
    input_check = check_directory("input")
    checks = [temp_check, output_check, input_check]

    return web.json_response({
        "status": "ok" if all(item["status"] == "ok" for item in checks) else "fail",
        "plugin": "my_api",
        "time": now_text(),
        "checks": {
            "temp": temp_check,
            "output": output_check,
            "input": input_check
        }
    })


@routes.get("/my_api/health/r2")
async def health_r2(request):
    r2_check = check_r2_roundtrip()

    return web.json_response({
        "status": r2_check["status"],
        "plugin": "my_api",
        "time": now_text(),
        "r2": r2_check
    }, status=200 if r2_check["status"] == "ok" else 500)


@routes.get("/my_api/health/full")
async def health_full(request):
    temp_check = check_directory("temp")
    output_check = check_directory("output")
    input_check = check_directory("input")
    r2_check = check_r2_roundtrip()
    checks = [temp_check, output_check, input_check, r2_check]

    return web.json_response({
        "status": "ok" if all(item["status"] == "ok" for item in checks) else "fail",
        "plugin": "my_api",
        "time": now_text(),
        "checks": {
            "temp": temp_check,
            "output": output_check,
            "input": input_check,
            "r2": r2_check
        }
    }, status=200 if all(item["status"] == "ok" for item in checks) else 500)


@routes.get("/trim_upload_r2")
async def trim_upload_r2(request):
    total_start = time.time()
    timings = {}

    try:
        filename = request.rel_url.query.get("filename")
        file_type = request.rel_url.query.get("type", "temp")
        subfolder = request.rel_url.query.get("subfolder", "")

        if not filename:
            return web.json_response({"error": "missing filename"}, status=400)

        step = time.time()
        filepath = get_file_path(filename, file_type, subfolder)
        timings["findFile"] = elapsed_ms(step)

        step = time.time()
        with open(filepath, "rb") as f:
            raw_bytes = f.read()
        timings["readImage"] = elapsed_ms(step)

        step = time.time()
        trimmed_bytes = trim_transparent_area(raw_bytes)
        timings["trimTransparentArea"] = elapsed_ms(step)

        step = time.time()
        date_folder = datetime.now().strftime("%Y%m%d")
        uuid_filename = uuid.uuid4().hex + ".png"
        r2_key = f"removed/{date_folder}/{uuid_filename}"
        timings["buildR2Path"] = elapsed_ms(step)

        step = time.time()
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=r2_key,
            Body=trimmed_bytes,
            ContentType="image/png"
        )
        timings["putObjectR2"] = elapsed_ms(step)

        timings["total"] = elapsed_ms(total_start)

        return web.json_response({
            "url": f"{R2_PUBLIC_BASE}/{r2_key}",
            "key": r2_key,
            "filename": uuid_filename,
            "timings": timings
        })

    except Exception as e:
        return web.json_response({
            "error": str(e),
            "timings": timings
        }, status=500)


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
