# SpiderFor91

这个仓库用于存放可导入 video-site-91 的爬虫脚本。每个爬虫脚本应放在独立目录中，例如：

```text
91Porn/
  91Porn.py
```

## 爬虫脚本规范

脚本当前只支持 Python 文件，文件后缀必须是 `.py`。

脚本必须在文件顶层声明静态名称：

```python
CRAWLER_NAME = "91Porn"
```

后台导入脚本时会读取 `CRAWLER_NAME` 作为爬虫名称，用户不需要手动填写名称或 ID。

## 运行协议

后台会用以下方式调用脚本：

```bash
python3 /path/to/crawler.py --job /path/to/job.json
```

脚本必须读取 `--job` 指定的 JSON 文件，并按 `crawler.v1` 协议执行抓取。

`job.json` 的关键字段：

```json
{
  "protocol": "crawler.v1",
  "mode": "crawl",
  "run_id": "20260610T120000Z",
  "crawler_id": "crawler-91porn",
  "target_new": 10,
  "seen_source_ids_file": "/path/to/seen.txt",
  "output_dir": "/path/to/output",
  "network": {
    "proxy_url": "http://127.0.0.1:7890"
  }
}
```

字段要求：

- `protocol` 必须支持 `crawler.v1`。
- `mode` 当前只要求支持 `crawl`。
- `target_new` 表示本次最多输出多少个新视频。
- `seen_source_ids_file` 每行是一个已入库的视频唯一标识，脚本应尽量跳过这些视频。
- `output_dir` 可用于保存本次运行的临时结果或归档文件。
- `network.proxy_url` 如果存在，脚本应将其用于 HTTP/HTTPS 请求代理。

## 输出协议

脚本必须向 `stdout` 输出 JSON Lines。每一行是一个完整 JSON 对象。

日志必须写到 `stderr`，不能混入 `stdout`。

发现一个视频时输出：

```json
{
  "type": "item",
  "item": {
    "source_id": "123456",
    "title": "视频标题",
    "media_url": "https://example.com/video.mp4",
    "thumbnail_url": "https://example.com/thumb.jpg",
    "detail_url": "https://example.com/detail",
    "headers": {
      "Referer": "https://example.com/detail"
    }
  }
}
```

必填字段：

- `item.title`：视频名称。
- `item.media_url`：视频下载直链。

推荐字段：

- `item.source_id`：同一个爬虫内稳定唯一的视频标识，用于提高去重效率。
- `item.thumbnail_url`：封面图下载直链；没有时后台会尝试从视频生成封面。
- `item.detail_url`：视频详情页地址。
- `item.headers`：下载视频或封面时需要带上的请求头。

任务完成时可以输出：

```json
{
  "type": "done",
  "stats": {
    "emitted": 10,
    "skipped": 3,
    "failed": 1
  }
}
```

## 当前脚本

- `91Porn/91Porn.py`
