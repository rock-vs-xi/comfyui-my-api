import os
import uuid
import time
import json
import asyncio
import copy
from datetime import datetime
from io import BytesIO

import boto3
from botocore.config import Config
from aiohttp import web, ClientSession, ClientTimeout, FormData
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
COMFYUI_BASE_URL = config.get("comfyui_base_url", "http://127.0.0.1:8188").rstrip("/")
COMFYUI_API_KEY = config.get("comfyui_api_key", "")
COMFYUI_TIMEOUT_SECONDS = int(config.get("comfyui_timeout_seconds", 180))
COMFYUI_POLL_INTERVAL_SECONDS = float(config.get("comfyui_poll_interval_seconds", 2))
COMFYUI_POLL_TIMEOUT_SECONDS = int(config.get("comfyui_poll_timeout_seconds", 120))
CUTOUT_TASK_TTL_SECONDS = int(config.get("cutout_task_ttl_seconds", 3600))
CUTOUT_TASK_MAX_COUNT = int(config.get("cutout_task_max_count", 1000))

CUTOUT_TASKS = {}

REMOVE_BACKGROUND_WORKFLOW = {
    "client_id": "@client_id@",
    "prompt": {
        "10": {
            "inputs": {
                "image": "@image@"
            },
            "class_type": "LoadImage",
            "_meta": {
                "title": "加载图像"
            }
        },
        "11": {
            "inputs": {
                "model": "ZhengPeng7/BiRefNet",
                "load_local_model": False,
                "background_color_name": "transparency",
                "device": "auto",
                "image": [
                    "10",
                    0
                ]
            },
            "class_type": "BiRefNet_Hugo",
            "_meta": {
                "title": "BiRefNet"
            }
        },
        "13": {
            "inputs": {
                "mask": [
                    "11",
                    1
                ]
            },
            "class_type": "MaskToImage",
            "_meta": {
                "title": "遮罩转换为图像"
            }
        },
        "14": {
            "inputs": {
                "images": [
                    "11",
                    0
                ]
            },
            "class_type": "PreviewImage",
            "_meta": {
                "title": "预览图像"
            }
        }
    }
}


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


def cleanup_cutout_tasks():
    now_ts = time.time()
    expired_ids = []

    for task_id, task in CUTOUT_TASKS.items():
        created_at_ts = task.get("createdAtTs", now_ts)
        if now_ts - created_at_ts > CUTOUT_TASK_TTL_SECONDS:
            expired_ids.append(task_id)

    for task_id in expired_ids:
        CUTOUT_TASKS.pop(task_id, None)

    if len(CUTOUT_TASKS) <= CUTOUT_TASK_MAX_COUNT:
        return

    sorted_items = sorted(
        CUTOUT_TASKS.items(),
        key=lambda item: item[1].get("createdAtTs", now_ts)
    )
    overflow_count = len(CUTOUT_TASKS) - CUTOUT_TASK_MAX_COUNT
    for task_id, _ in sorted_items[:overflow_count]:
        CUTOUT_TASKS.pop(task_id, None)


def comfyui_headers():
    if COMFYUI_API_KEY:
        return {
            "Authorization": f"Bearer {COMFYUI_API_KEY}"
        }
    return {}


def parse_json_response(text, action):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        preview = (text or "").replace("\n", " ")[:300]
        raise RuntimeError(f"{action}返回非JSON响应: {e}. 响应内容: {preview}")


def build_public_url(public_base, key):
    return f"{public_base.rstrip('/')}/{key}"


def put_object_to_r2(bucket_name, key, body, content_type):
    s3_client.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=body,
        ContentType=content_type
    )


async def upload_image_to_comfyui(image_url):
    total_start = time.time()
    timings = {}

    form = FormData()
    form.add_field("image", image_url)
    form.add_field("type", "input")

    timeout = ClientTimeout(total=COMFYUI_TIMEOUT_SECONDS)
    step = time.time()
    async with ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{COMFYUI_BASE_URL}/api/upload/image",
            data=form,
            headers=comfyui_headers(),
            allow_redirects=False
        ) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"上传图片到ComfyUI失败: {response.status}, {text}")

            data = parse_json_response(text, "上传图片到ComfyUI")
            name = data.get("name")
            if not name:
                raise RuntimeError(f"ComfyUI上传图片未返回name: {text}")

    timings["上传图片到ComfyUI耗时"] = elapsed_ms(step)
    timings["uploadImageByUrl总耗时"] = elapsed_ms(total_start)
    return name, image_url, "URL上传", timings


def build_workflow_json(image_name, client_id):
    workflow = copy.deepcopy(REMOVE_BACKGROUND_WORKFLOW)
    workflow["client_id"] = client_id
    workflow["prompt"]["10"]["inputs"]["image"] = image_name
    return workflow


async def send_workflow(workflow):
    timeout = ClientTimeout(total=COMFYUI_TIMEOUT_SECONDS)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{COMFYUI_BASE_URL}/api/prompt",
            json=workflow,
            headers=comfyui_headers(),
            allow_redirects=False
        ) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"提交ComfyUI工作流失败: {response.status}, {text}")

            data = parse_json_response(text, "提交ComfyUI工作流")
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                raise RuntimeError(f"ComfyUI工作流未返回prompt_id: {text}")
            return prompt_id


async def poll_workflow_result(prompt_id):
    timeout = ClientTimeout(total=COMFYUI_TIMEOUT_SECONDS)
    start = time.time()

    async with ClientSession(timeout=timeout) as session:
        while True:
            async with session.get(
                f"{COMFYUI_BASE_URL}/api/history/{prompt_id}",
                headers=comfyui_headers(),
                allow_redirects=False
            ) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"查询ComfyUI结果失败: {response.status}, {text}")

                if text and len(text.strip()) > 2:
                    return parse_json_response(text, "查询ComfyUI结果")

            if time.time() - start > COMFYUI_POLL_TIMEOUT_SECONDS:
                raise RuntimeError(f"轮询ComfyUI结果超时: promptId={prompt_id}")

            await asyncio.sleep(COMFYUI_POLL_INTERVAL_SECONDS)


def extract_filename(history_json, prompt_id):
    prompt_result = history_json.get(prompt_id)
    if not prompt_result:
        return None

    outputs = prompt_result.get("outputs") or {}
    for node_output in outputs.values():
        images = node_output.get("images") or []
        if images:
            filename = images[0].get("filename")
            if filename:
                return filename
    return None


def trim_upload_to_r2(filename, file_type="temp", subfolder=""):
    total_start = time.time()
    timings = {}

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
    put_object_to_r2(R2_BUCKET_NAME, r2_key, trimmed_bytes, "image/png")
    timings["putObjectR2"] = elapsed_ms(step)

    timings["total"] = elapsed_ms(total_start)

    return {
        "url": build_public_url(R2_PUBLIC_BASE, r2_key),
        "key": r2_key,
        "filename": uuid_filename,
        "timings": timings
    }


async def run_cutout_task(task_id, input_image):
    total_start = time.time()
    timings = {}
    client_id = uuid.uuid4().hex
    prompt_id = None
    filename = None
    upload_mode = None
    upload_input = None

    try:
        CUTOUT_TASKS[task_id].update({
            "status": "running",
            "clientId": client_id,
            "startedAt": now_text()
        })

        step = time.time()
        image_name, upload_input, upload_mode, upload_timings = await upload_image_to_comfyui(input_image)
        timings.update(upload_timings)
        timings["输入图处理总耗时"] = elapsed_ms(step)

        step = time.time()
        workflow = build_workflow_json(image_name, client_id)
        timings["生成工作流JSON耗时"] = elapsed_ms(step)

        step = time.time()
        prompt_id = await send_workflow(workflow)
        timings["提交ComfyUI工作流耗时"] = elapsed_ms(step)

        step = time.time()
        history_json = await poll_workflow_result(prompt_id)
        timings["轮询ComfyUI结果耗时"] = elapsed_ms(step)

        step = time.time()
        filename = extract_filename(history_json, prompt_id)
        if not filename:
            raise RuntimeError(f"ComfyUI结果中未找到图片文件名: promptId={prompt_id}")
        timings["提取结果文件名耗时"] = elapsed_ms(step)

        step = time.time()
        upload_result = await asyncio.to_thread(trim_upload_to_r2, filename, "temp", "")
        timings.update(upload_result.get("timings") or {})
        timings["结果图处理总耗时"] = elapsed_ms(step)
        timings["整套流程总耗时"] = elapsed_ms(total_start)

        CUTOUT_TASKS[task_id].update({
            "status": "success",
            "clientId": client_id,
            "promptId": prompt_id,
            "filename": filename,
            "uploadMode": upload_mode,
            "uploadInput": upload_input,
            "url": upload_result["url"],
            "r2_url": upload_result["url"],
            "key": upload_result["key"],
            "resultFilename": upload_result["filename"],
            "timings": timings,
            "finishedAt": now_text()
        })

    except Exception as e:
        timings["整套流程总耗时"] = elapsed_ms(total_start)
        CUTOUT_TASKS[task_id].update({
            "status": "fail",
            "clientId": client_id,
            "promptId": prompt_id,
            "filename": filename,
            "uploadMode": upload_mode,
            "uploadInput": upload_input,
            "error": str(e),
            "timings": timings,
            "finishedAt": now_text()
        })


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


@routes.get("/my_api/ping")
async def ping(request):
    return web.json_response({
        "status": "ok",
        "plugin": "my_api",
        "time": now_text()
    })


@routes.post("/my_api/cutout/start")
async def cutout_start(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({
            "status": "fail",
            "error": "请求体必须是JSON"
        }, status=400)

    input_image = data.get("inputImage") or data.get("input_image")
    if not input_image or not str(input_image).strip():
        return web.json_response({
            "status": "fail",
            "error": "inputImage不能为空"
        }, status=400)

    cleanup_cutout_tasks()
    task_id = uuid.uuid4().hex
    CUTOUT_TASKS[task_id] = {
        "taskId": task_id,
        "status": "pending",
        "createdAt": now_text(),
        "createdAtTs": time.time()
    }
    asyncio.create_task(run_cutout_task(task_id, str(input_image).strip()))

    return web.json_response({
        "status": "running",
        "taskId": task_id
    })


@routes.get("/my_api/cutout/result")
async def cutout_result(request):
    task_id = request.rel_url.query.get("taskId") or request.rel_url.query.get("task_id")
    if not task_id:
        return web.json_response({
            "status": "fail",
            "error": "taskId不能为空"
        }, status=400)

    task = CUTOUT_TASKS.get(task_id)
    if task is None:
        return web.json_response({
            "status": "not_found",
            "taskId": task_id,
            "error": "任务不存在或ComfyUI已重启"
        }, status=404)

    return web.json_response(task)


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


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
