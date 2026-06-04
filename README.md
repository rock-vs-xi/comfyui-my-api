# comfyui-my-api

这是一个 ComfyUI 自定义 API 插件，用来在 ComfyUI 侧完成完整抠图流程，减少 Java 中转服务器和 ComfyUI 之间的多次请求。

## 功能

- Java 只需要发起任务和查询任务结果
- 插件内部完成输入图下载、本地保存、工作流提交、结果轮询、透明区域裁剪、结果图上传 R2
- 支持抠图任务和图生图任务
- 提供轻量存活检测接口，方便监控
- 提供目录和 R2 健康检测接口

## 安装

把整个目录放到 ComfyUI 的自定义节点目录：

```bash
ComfyUI/custom_nodes/my_api
```

安装依赖：

```bash
pip install boto3 pillow
```

重启 ComfyUI 后生效。

## 配置

插件默认读取：

```bash
/workspace/config/config.json
```

配置示例：

```json
{
  "r2_account_id": "your-r2-account-id",
  "r2_access_key": "your-r2-access-key",
  "r2_secret_key": "your-r2-secret-key",
  "r2_bucket_name": "rembg",
  "r2_public_base": "https://your-result-public-domain.example.com",
  "comfyui_base_url": "http://127.0.0.1:8188",
  "comfyui_api_key": "",
  "comfyui_timeout_seconds": 180,
  "comfyui_poll_interval_seconds": 2,
  "comfyui_poll_timeout_seconds": 120,
  "cutout_task_ttl_seconds": 3600,
  "cutout_task_max_count": 1000,
  "birefnet_model": "BiRefNet",
  "birefnet_load_local_model": true
}
```

建议权限：

```bash
chmod 600 /workspace/config/config.json
```

不要把真实配置和密钥提交到 GitHub。

## 接口

### 存活检测

```bash
GET /my_api/ping
```

### 发起抠图任务

```bash
POST /my_api/cutout/start
```

请求体：

```json
{
  "inputImage": "https://example.com/input.jpg"
}
```

返回：

```json
{
  "status": "running",
  "taskId": "xxx"
}
```

### 查询抠图结果

```bash
GET /my_api/cutout/result?taskId=xxx
```

处理中：

```json
{
  "taskId": "xxx",
  "status": "running"
}
```

成功：

```json
{
  "taskId": "xxx",
  "status": "success",
  "r2_url": "https://example.com/removed/20260604/xxx.png",
  "url": "https://example.com/removed/20260604/xxx.png",
  "timings": {}
}
```

失败：

```json
{
  "taskId": "xxx",
  "status": "fail",
  "error": "错误信息"
}
```

### 发起图生图任务

```bash
POST /my_api/img2img/start
```

请求体：

```json
{
  "inputImage": "https://example.com/input.jpg",
  "type": "1"
}
```

`type` 对应 Java 里原来的四套图生图风格参数：`1`、`2`、`3`、默认 `4`。

返回：

```json
{
  "status": "running",
  "taskId": "xxx"
}
```

### 查询图生图结果

```bash
GET /my_api/img2img/result?taskId=xxx
```

成功返回：

```json
{
  "taskId": "xxx",
  "status": "success",
  "imageUrl": "https://example.com/removed/20260604/xxx.png",
  "url": "https://example.com/removed/20260604/xxx.png",
  "timings": {}
}
```

### 健康检测

```bash
GET /my_api/health
GET /my_api/health/r2
GET /my_api/health/full
```

## 调试

```bash
curl http://127.0.0.1:8188/my_api/ping

curl -X POST "http://127.0.0.1:8188/my_api/cutout/start" \
  -H "Content-Type: application/json" \
  -d '{"inputImage":"https://example.com/input.jpg"}'

curl "http://127.0.0.1:8188/my_api/cutout/result?taskId=xxx"

curl -X POST "http://127.0.0.1:8188/my_api/img2img/start" \
  -H "Content-Type: application/json" \
  -d '{"inputImage":"https://example.com/input.jpg","type":"1"}'

curl "http://127.0.0.1:8188/my_api/img2img/result?taskId=xxx"
```
