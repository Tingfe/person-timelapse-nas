# 人物延时摄影 NAS

从小米摄像头等设备导出的 MP4 中筛选含有人物的时段，并生成延时摄影视频。适合将大量家庭监控录像留在 NAS 上本地处理。

原始录像始终以只读方式挂载：工具不会移动、改名或删除任何源文件。人物识别、缩略图、事件索引和成片都只写入独立的输出目录。

## 功能

- 自动识别常见小米录像文件名及无摄像头编号的历史录像；
- 人物检测、事件缩略图、可播放的延时成片；
- 日期索引在后台建立，适合大量历史 MP4；
- 单并发任务队列，降低低配置 NAS 的负载；
- 支持将多个扫描任务依次排队；
- 支持选择起止日期，将已扫描的人物事件合成一个多日延时视频；
- 已处理录像会登记去重，重复运行不会重复分析相同文件；
- 内置局域网 Web 管理页、实时进度、预计剩余时间和取消任务。

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

首次启动会后台读取录像目录并建立日期索引，索引元数据持久化为 `output/inventory.sqlite3`。后续刷新会复用未变化文件的日期与摄像头信息，仅重新解析新增或变更的录像。索引较大时仍可直接输入已知日期，将扫描任务加入队列。

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
2. 选择摄像头与性能档位，点击“扫描人物”。多个日期可以连续提交，会自动排队执行。
3. 扫描完成后选择同一摄像头、填写起止日期，点击“制作延时”。区间内每天都必须已扫描。
4. 在日期详情中查看人物事件、缩略图和导出的视频。

任务队列始终只同时执行一个扫描或导出任务，避免低配置 NAS 因并发推理而更慢。NAS 或容器重启时，正在运行的任务会标为中断；尚未开始的队列任务会继续执行。

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
./mac-worker.sh 20260324 2
```

也可以启动仅限本机访问的可视化控制台：

```sh
sh mac-console.sh
```

随后打开 `http://127.0.0.1:8791`，选择日期和批次数，一键启动或停止；日志会直接显示在页面内。

每批最多处理 5 个未处理 MP4；可随时按 `Ctrl-C` 停止，已完成文件会记录在 NAS 的 `processed.json`，下次运行自动续接。首次运行只会在仓库目录创建 `.mac-worker-venv` 独立虚拟环境，不修改系统 Python、Homebrew 或 Docker。运行 Mac worker 时，请不要让 NAS 对同一日期执行扫描任务。

Mac 也可以协助建立 NAS 日期索引，不需要安装 AI 依赖：

```sh
sh mac-index.sh
```

它会将共享的 `inventory.sqlite3` 写入 NAS 输出目录。执行期间不要同时让 NAS 管理页建立索引。

## 支持的文件名

文件名时间用于归档，不依赖 NAS 的修改时间。支持例如：

```text
00_20260324193154_20260324195042.mp4
video_0000_10_10_20260303202252_20260303204922.mp4
20231127165410_20231127165439.mp4
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

推送到 `main` 后，GitHub Actions 会构建 ARM64 镜像并发布 `latest` 与 `sha-提交哈希` 标签。维护者需要在 GitHub Actions Secrets 中配置 `DOCKERHUB_USERNAME` 与 `DOCKERHUB_TOKEN`；使用者不需要任何 Docker Hub 凭据。
