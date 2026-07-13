# 小米录像人物延时（Z2 Pro 试运行）

这个工具只读取既有 MP4 录像，按文件名中的时间范围检测 `person`，把结果写入输出目录。它不会移动、改名或删除原始录像。

## 先跑一小段

在极空间 Docker 中新建项目，上传本目录的 `compose.yaml` 与 `app/person_timelapse.py`。编辑 `compose.yaml` 的两个宿主机路径：

- `/你的/小米录像目录`：现有 MP4 的父目录，挂载为只读；
- `/你的/人物延时输出目录`：新建的空目录，保存索引、缩略图和成片。

启动容器后，在容器终端运行：

```sh
python /app/person_timelapse.py scan /input /output --date 20260324 --sample-seconds 2
```

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
python /app/person_timelapse.py export /input /output/events-20260324.json /output \
  --camera 00 --fps 25 --frame-seconds 1
```

输出为 `/output/people-timelapse-00-20260324.mp4`。
成片会缩放至 1280px 宽，以避免高分辨率原始监控画面导致延时视频占用过大空间。

## 录像文件约定

文件名末尾必须类似（前面可保留 NAS 自动添加的前缀）：

```text
00_20260324193154_20260324195042.mp4
video_0000_10_20260324195528_20260324200632.mp4
```

其中 `00` / `10` 被视为不同摄像头（或不同录像流）。拍摄时间以文件名为准，不使用 NAS 的修改时间。

## 资源建议

- Z2 Pro 是 ARM CPU 平台：首次试跑用 `--sample-seconds 2`，只处理一天。
- 扫描运行期间不要同时跑下载、转码等重任务。
- 若人物长期出现在画面内，导出视频仍可能较长；可把 `--frame-seconds` 调大到 `2` 或 `3`。

## Web 管理页

启动 Compose 项目后，在同一局域网浏览器打开：

```text
http://极空间的局域网IP:8790
```

管理页支持按日期查看人物事件、播放已导出的延时视频、创建人物扫描任务，以及为已扫描日期和指定摄像头创建延时导出任务。为了保护低配置 NAS，任务队列一次只运行一个任务；刷新页面或重启容器不会丢失已完成文件的去重台账。

请只在家庭局域网内访问该端口，不要为 `8790` 设置公网端口映射。

## 开源发布与隐私

仓库默认通过 `.gitignore` 和 `.dockerignore` 排除原始录像、人物缩略图、事件索引、任务日志和模型权重。发布到 GitHub 或 Docker Hub 前，请逐项完成 [开源发布前检查清单](OPEN_SOURCE_CHECKLIST.md)。
