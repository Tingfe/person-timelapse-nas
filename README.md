# 小米录像人物延时（Z2 Pro 试运行）

这个工具只读取既有 MP4 录像，按文件名中的时间范围检测 `person`，把结果写入输出目录。它不会移动、改名或删除原始录像。

## 先跑一小段

在极空间 Docker 中新建项目，上传本目录的 `compose.yaml`。它默认拉取 ARM64 镜像 `tingfe/person-timelapse-nas:latest`；只需编辑一处宿主机路径：

- `/你的/小米录像目录`：现有 MP4 的父目录，挂载为只读；

项目启动时会自动在项目目录下创建并挂载 `output/`（索引、缩略图、成片）与 `models/`（首次下载的模型）。因此更新镜像无需重新选择这两个目录。

启动容器后，在容器终端运行：

```sh
python /opt/person-timelapse/person_timelapse.py scan /input /output --date 20260324 --sample-seconds 2
```

更新时执行 `docker compose pull && docker compose up -d`，或在极空间界面中拉取最新镜像后重新创建项目。若希望固定在某次发布版本，可把 `image:` 改为 `tingfe/person-timelapse-nas:sha-提交哈希`。

若在极空间中选择“从镜像创建容器”，镜像会自动启动管理页；仍需设置 `8790:8790` 端口映射，并手动选择录像目录挂载到 `/input`（只读）。推荐使用 Compose 项目部署，以便自动持久化 `output/` 和 `models/` 目录。

首次验证单个录像文件时可附加 `--limit 1 --sample-seconds 5`，确认缩略图正确后再移除 `--limit`。
每次成功扫描的文件都会登记在 `/output/processed.json`；下次运行会自动跳过相同的录像。若需主动重扫，附加 `--force`。

低配置 NAS 推荐加入 `--motion-threshold 3 --keepalive-seconds 60`：先用低分辨率画面变化过滤静止帧，再对有变化的画面和人物刚出现后的短时间运行 YOLO。

对于包含许多日期的 TF 卡录像，可运行 `run_mj_batch.sh`。它按日期顺序扫描并使用同一台账，可中断后再次运行。

首次仅选择一天。完成后会生成：

```text
/output/events-20260324.json
/output/thumbnails/00/*.jpg
/output/thumbnails/10/*.jpg
```

确认事件和缩略图正确后，导出某一路的延时视频：

```sh
python /opt/person-timelapse/person_timelapse.py export /input /output/events-20260324.json /output \
  --camera 00 --fps 25 --frame-seconds 1
```

输出为 `/output/people-timelapse-00-20260324.mp4`。
成片会缩放至 1280px 宽，以避免高分辨率原始监控画面导致延时视频占用过大空间。

## 录像文件约定

文件名末尾必须类似（前面可保留 NAS 自动添加的前缀）：

```text
00_20260324193154_20260324195042.mp4
video_0000_10_20260324195528_20260324200632.mp4
20231127165410_20231127165439.mp4
```

其中 `00` / `10` 被视为不同摄像头（或不同录像流）。没有摄像头编号的历史文件会归入“历史录像”流。拍摄时间以文件名为准，不使用 NAS 的修改时间。

## 资源建议

- Z2 Pro 是 ARM CPU 平台：优先使用管理页的“极速（Z2 推荐）”（30 秒抽帧、320px YOLO）。精细档仅适合短录像复核。
- 扫描运行期间不要同时跑下载、转码等重任务。
- 若人物长期出现在画面内，导出视频仍可能较长；可把 `--frame-seconds` 调大到 `2` 或 `3`。

## Web 管理页

启动 Compose 项目后，在同一局域网浏览器打开：

```text
http://极空间的局域网IP:8790
```

管理页支持按日期查看人物事件、播放已导出的延时视频、创建人物扫描任务，以及为已扫描日期和指定摄像头创建延时导出任务。

运行中的任务每 5 秒自动更新一次，显示当前文件、文件数量、抽帧数、YOLO 推理次数、已运行时间和预计剩余时间；也可以在队列中取消任务。扫描可选择三档性能：`节能`（每 10 秒抽帧）、`平衡`（每 5 秒，默认）与 `精细`（每 2 秒）。为保护低配置 NAS，任务队列一次只运行一个任务。若管理页或 NAS 重启，正在运行的任务会标为“已中断”，已完成的文件与去重台账仍会保留。

请只在家庭局域网内访问该端口，不要为 `8790` 设置公网端口映射。

## 自动发布 Docker Hub

仓库已包含 ARM64 的 GitHub Actions 工作流。首次启用时，在 GitHub 仓库 **Settings → Secrets and variables → Actions** 中添加：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名；
- `DOCKERHUB_TOKEN`：Docker Hub 的 Access Token（不要填写账户密码）。

之后每次推送 `main` 且修改 Dockerfile 或 `app/`，Actions 会发布 `latest` 和 `sha-提交哈希` 两个标签。工作流不含任何账户凭据；未配置这两个 Secret 时会跳过登录和推送步骤。

## 开源发布与隐私

仓库默认通过 `.gitignore` 和 `.dockerignore` 排除原始录像、人物缩略图、事件索引、任务日志和模型权重。发布到 GitHub 或 Docker Hub 前，请逐项完成 [开源发布前检查清单](OPEN_SOURCE_CHECKLIST.md)。
