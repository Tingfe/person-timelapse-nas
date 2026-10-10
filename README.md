# 人物延时摄影 NAS

从小米摄像头等设备导出的 MP4 中筛选含有人物的时段，并生成延时摄影视频。适合将大量家庭监控录像留在 NAS 上本地处理。

原始录像始终以只读方式挂载：工具不会移动、改名或删除任何源文件。人物识别、缩略图、事件索引和成片都只写入独立的输出目录。

## 功能

- 自动识别常见小米录像文件名及无摄像头编号的历史录像；
- 人物检测、事件缩略图、可播放的延时成片；
- 日期索引在后台建立，适合大量历史 MP4；
- 单并发任务队列，降低低配置 NAS 的负载；
- 支持将多个扫描任务依次排队；
- 支持一键选择起止日期：自动扫描尚未完成的录像，再合成一条多日延时视频；
- 每个源文件完成后立即保存检查点，NAS 与 Mac 的结果不会相互覆盖；
- 已处理录像自动跳过；提高扫描精度时只补扫不满足新设置的文件；
- 内置密码访问、实时进度、暂停/继续队列、取消及失败重试。

## 快速开始

镜像已公开发布至 Docker Hub：[`tingfe/person-timelapse-nas`](https://hub.docker.com/r/tingfe/person-timelapse-nas)。推荐使用 Compose 部署：

```yaml
services:
  person-timelapse:
    image: tingfe/person-timelapse-nas:latest
    container_name: person-timelapse
    environment:
      # 使用强密码；也可留空，首次启动后在 output/.access-password 查看随机密码
      - AUTH_PASSWORD=${AUTH_PASSWORD:-}
    volumes:
      # 改为你的录像目录；务必保留 :ro
      - "/path/to/your/camera-recordings:/input:ro"
      # Compose 项目目录下自动持久化结果和模型
      - "./output:/output"
      - "./models:/models"
    ports:
      - "8790:8790"
    restart: unless-stopped
```

启动后在同一局域网打开：

```text
http://NAS 的局域网 IP:8790
```

首次访问需要输入密码。推荐在极空间项目的环境变量中设置 `AUTH_PASSWORD`；若留空，服务会生成随机密码，并保存到持久化的 `output/.access-password`（也会打印在首次启动日志中）。

首次启动会后台读取录像目录并建立日期索引，索引元数据持久化为 `output/inventory.sqlite3`。有旧索引时会先显示旧索引，并在后台检查变化；没有索引时需等待首次索引发现录像日期后再创建任务。默认每小时检查录像目录，管理页的“检查新增录像 / 刷新索引”可立即触发检查。页面刷新和任务状态查询不会反复遍历整个录像目录。

### 极空间部署

在 Docker 的“创建项目 / Compose 配置”中粘贴上面的配置。录像目录请使用“查询路径”获得的真实容器宿主机路径，例如：

```yaml
- "/tmp/zfsv3/磁盘标识/data/摄像头文件备份:/input:ro"
```

不要加入旧版本示例中的 `working_dir` 或 `command`。更新时点击“重新构建”；也可以固定版本以避免自动更新：

```yaml
image: tingfe/person-timelapse-nas:sha-提交哈希
```

## 使用方式

1. 等待日期索引出现，或手动选择一个已知录像日期。
2. 选择摄像头与性能档位，填写可选的结束日期后点击“一键人物延时”。系统会先把范围内实际有录像、尚未完成的日期按时间顺序加入扫描队列，再自动合成一条成片。
3. 需要精细控制时，仍可分别使用“扫描范围人物”和“制作范围延时”。
4. 在日期详情中查看人物事件、缩略图和导出的视频。

任务队列始终只同时执行一个扫描或导出任务，运行中仍可追加新任务。点击“暂停队列”会停止当前任务并保留已经完成的文件检查点；继续时只重做尚未完成的源文件。NAS 或容器重启后，原任务会自动恢复排队，范围合成的前置依赖也会保留。

合成使用临时文件，校验通过才替换原成片；失败不会覆盖已有视频。坏文件会记录为失败而不是“已处理”。若只有部分素材可读，任务标记为“部分完成”，请查看日志后使用“重试 / 继续”。

### 更新与恢复

1. 更新前先停止项目，备份整个 `output` 目录；录像源目录不需要移动。
2. 拉取新镜像并重新创建容器，保留原来的 `/output`、`/models` 挂载位置和访问密码。仅重启旧容器不会更新镜像。
3. 旧版 `processed.json` 与事件文件会继续读取，新扫描检查点保存在 `output/scan-results/日期/`，旧事件备份在 `output/legacy-events/`。不要只迁移 `processed.json`，也不要删除 `scan-results`。
4. 正在运行的任务会自动续排；失败或阻塞的任务可点击“重试 / 继续”。提高档位会自动补扫，也可勾选“按所选档位重新扫描”明确重做。

默认连续 300 秒没有进度会停止卡住的任务。扫描会记录该文件失败，并尝试继续剩余文件；连续超时次数有上限，避免无休止重启。确实需要更长单文件处理时间时，可在 Compose 的 `environment` 中加入 `TASK_STALL_SECONDS=600`。索引检查间隔可通过 `INDEX_REFRESH_SECONDS` 调整（单位：秒，默认 3600）。

旧镜像不认识新的文件级检查点；如需回退，请使用更新前的整份输出目录备份，避免重复扫描或状态不一致。不要用删除输出目录的方式排障。

## 性能建议

人物识别完全依赖 CPU 时，长时间监控录像的处理会很慢。建议先从“超极速（历史回放）”开始：120 秒抽帧、256px 推理，适合粗筛大量历史录像；只对需要复查的日期使用“极速”“平衡”或“精细”。

| 档位 | 适合场景 | 取舍 |
| --- | --- | --- |
| 超极速 | 大量历史回放 | 速度最快，可能漏掉短暂出现的人物 |
| 极速 | 日常全天回顾 | Z2 等低配置 NAS 的折中选择 |
| 平衡 / 精细 | 短录像复查 | 更容易捕捉短事件，但耗时明显增加 |

如果超极速仍无法接受，最有效的方案是让 NAS 仅保存录像，在带 GPU 的电脑上挂载 NAS 目录执行识别。

### Mac 间歇计算节点（实验性）

Apple Silicon Mac 可用 Metal/MPS 参与计算。将 NAS 的录像目录与 `延时摄影` 输出目录通过 SMB 挂载到 Mac 后，在本仓库运行：

```sh
chmod +x mac-worker.sh
./mac-worker.sh 20260324 5
```

也可以启动仅限本机访问的可视化控制台：

```sh
sh mac-console.sh
```

随后打开 `http://127.0.0.1:8791`，选择日期和本次最多处理的文件数，一键启动或停止；日志会直接显示在页面内。

第二个参数是本次最多处理的未处理 MP4 数量（例如 `5`），可随时按 `Ctrl-C` 停止；已完成文件会立即记录在 NAS 的 `scan-results`，下次运行自动续接。首次运行只会在仓库目录创建 `.mac-worker-venv` 独立虚拟环境，进度写入本地 `.mac-worker-state`，不修改系统 Python、Homebrew 或 Docker。NAS 与 Mac 应使用同一版本代码和相同的输出目录，文件锁会避免同时扫描同一文件。

Mac worker 默认会取得“计算优先权”：运行期间 NAS 不会启动新的扫描任务；Mac 正常结束、停止或离线超过 15 分钟后，NAS 队列自动恢复。已在 NAS 运行中的任务不会被强行中断。

Mac 也可以协助建立 NAS 日期索引，不需要安装 AI 依赖：

```sh
sh mac-index.sh
```

它会将共享的 `inventory.sqlite3` 写入 NAS 输出目录。索引锁会协调 NAS 与 Mac，已有节点建立索引时另一节点会提示稍后重试，不会并发改写数据库。

## 支持的文件名

文件名时间用于归档，不依赖 NAS 的修改时间。支持例如：

```text
00_20000101090000_20000101091000.mp4
video_0000_10_10_20000102090000_20000102093000.mp4
20000103090000_20000103090030.mp4
```

前两类中的 `00`、`10` 会作为不同摄像头；第三类历史文件会归入“历史录像”摄像头。

## 命令行（可选）

Web 管理页已覆盖常用操作。如需在容器终端运行：

```sh
python /opt/person-timelapse/person_timelapse.py scan /input /output --date 20260324
python /opt/person-timelapse/person_timelapse.py export /input /output /output/events-20260324.json --camera 0
```

多日导出可连续传入多个事件文件：

```sh
python /opt/person-timelapse/person_timelapse.py export /input /output \
  /output/events-20260323.json /output/events-20260324.json --camera 0
```

## 隐私与安全

- 不要将 `8790` 映射到公网；请仅在家庭局域网使用。
- 原始录像、缩略图、事件索引、任务状态和模型均不应提交到 Git。
- 发布前请阅读 [开源发布前检查清单](OPEN_SOURCE_CHECKLIST.md)。

## 开发与发布

推送到 `main` 后，GitHub Actions 会先运行可靠性回归测试，通过后才构建 ARM64 镜像并发布 `latest` 与 `sha-提交哈希` 标签。维护者需要在 GitHub Actions Secrets 中配置 `DOCKERHUB_USERNAME` 与 `DOCKERHUB_TOKEN`；使用者不需要任何 Docker Hub 凭据。

本地测试：`python3 -m unittest discover -s tests -v`。真实编解码集成测试需要现有的 OpenCV 和 FFmpeg 环境，并设置 `RUN_MEDIA_INTEGRATION=1`；测试只使用生成的色块画面，不读取私人录像。
