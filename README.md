# comfyui-my-api

这是一个 ComfyUI 自定义 API 插件，主要用于把 ComfyUI 生成的透明背景图片裁剪后上传到 Cloudflare R2，并返回公网访问地址。

## 功能

- 提供插件健康检测接口
- 检测 ComfyUI 的 `temp`、`output`、`input` 目录状态
- 检测 R2 是否可以正常写入、校验和删除文件
- 根据 ComfyUI 生成的图片文件名定位本地图片
- 自动裁剪透明区域
- 上传裁剪后的 PNG 到 R2
- 返回图片 URL、R2 Key、文件名和耗时明细

## 安装

把整个 `my_api` 目录放到 ComfyUI 的自定义节点目录：

```bash
ComfyUI/custom_nodes/my_api
```

安装依赖：

```bash
pip install boto3 pillow
```

安装完成后重启 ComfyUI。

## 配置

插件默认读取配置文件：

```bash
/workspace/config/config.json
```

可以参考 `config.example.json` 创建真实配置：

```json
{
  "r2_account_id": "your-r2-account-id",
  "r2_access_key": "your-r2-access-key",
  "r2_secret_key": "your-r2-secret-key",
  "r2_bucket_name": "rembg",
  "r2_public_base": "https://your-public-domain.example.com"
}
```

建议收紧配置文件权限：

```bash
chmod 600 /workspace/config/config.json
```

注意：不要把真实配置文件或密钥提交到 GitHub。

## 接口

### 1. 插件健康检测

```bash
GET /my_api/health
```

用于检测插件是否加载，以及 ComfyUI 的 `temp`、`output`、`input` 目录是否可用。

### 2. R2 健康检测

```bash
GET /my_api/health/r2
```

用于检测 R2 是否可以正常上传、校验和删除文件。

### 3. 完整健康检测

```bash
GET /my_api/health/full
```

包含目录检测和 R2 检测。

### 4. 裁剪透明区域并上传 R2

```bash
GET /trim_upload_r2?filename=ComfyUI_temp_xxx.png&type=temp
```

参数说明：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `filename` | 是 | ComfyUI 生成的图片文件名 |
| `type` | 否 | 文件类型，默认 `temp`，也可以是 `output`、`input` |
| `subfolder` | 否 | ComfyUI 子目录 |

返回示例：

```json
{
  "url": "https://example.com/removed/20260604/xxx.png",
  "key": "removed/20260604/xxx.png",
  "filename": "xxx.png",
  "timings": {
    "findFile": 0,
    "readImage": 1,
    "trimTransparentArea": 800,
    "buildR2Path": 0,
    "putObjectR2": 4000,
    "total": 5000
  }
}
```

## 耗时字段说明

| 字段 | 说明 |
| --- | --- |
| `findFile` | 定位 ComfyUI 本地图片耗时 |
| `readImage` | 读取图片文件耗时 |
| `trimTransparentArea` | 裁剪透明区域耗时 |
| `buildR2Path` | 生成 R2 存储路径耗时 |
| `putObjectR2` | 上传图片到 R2 耗时 |
| `total` | 整个接口总耗时 |

## 调试命令

```bash
curl http://127.0.0.1:7860/my_api/health
curl http://127.0.0.1:7860/my_api/health/r2
curl http://127.0.0.1:7860/my_api/health/full
```

示例：

```bash
curl "http://127.0.0.1:7860/trim_upload_r2?filename=ComfyUI_temp_xxx.png&type=temp"
```
