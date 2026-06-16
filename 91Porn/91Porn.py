#!/usr/bin/env python3
"""
脚本名称: 91Porn
用途: 爬取 91Porn 列表中的视频标题、视频下载直链、封面图直链和唯一标识，
并按 crawler.v1 协议输出给 video-site-91 后端入库。

已修改: 默认不再固定为热门(category=top)，默认不带 category 查询参数以抓取全部视频。
新增: CLI 参数 --category 可用于指定单个分类（例如 "top"、"new" 等）。
新增: 支持通过 --series-url 抓取 xchina 系列页面（单页模式）。
"""

import argparse
import requests
import re
import time
import random
import json
import os
import socket
import sys
import html
from urllib.parse import urljoin, unquote, urlparse
from datetime import datetime

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("错误: 缺少依赖库 beautifulsoup4", file=sys.stderr)
    print("请运行: pip install beautifulsoup4 lxml", file=sys.stderr)
    sys.exit(1)


def prefer_ipv4_for_plain_socks5_proxy():
    proxy_envs = (
        os.environ.get("HTTPS_PROXY", ""),
        os.environ.get("HTTP_PROXY", ""),
        os.environ.get("https_proxy", ""),
        os.environ.get("http_proxy", ""),
    )
    uses_plain_socks5 = any(v.strip().lower().startswith("socks5://") for v in proxy_envs)
    if not uses_plain_socks5 or getattr(socket, "_spider91_ipv4_first", False):
        return

    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4_first(*args, **kwargs):
        infos = original_getaddrinfo(*args, **kwargs)
        return sorted(infos, key=lambda info: 0 if info[0] == socket.AF_INET else 1)

    socket.getaddrinfo = getaddrinfo_ipv4_first
    socket._spider91_ipv4_first = True

BASE_URL = "https://www.91porn.com/v.php"
# LIST_PARAMS 不再固定为热门，保留配置位置以供扩展
LIST_PARAMS = {
    # "category": "top",
    "viewtype": "basic"
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;"
        "q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

MIN_PAGE_DELAY = 3.0
MAX_PAGE_DELAY = 6.0
MIN_DETAIL_DELAY = 2.0
MAX_DETAIL_DELAY = 5.0

MAX_RETRIES = 3
RETRY_DELAY = 5.0

OUTPUT_FILE = "91porn_videos.json"
MAX_PAGES = None
RESUME = True
MAX_EMPTY_PAGES = 2
CRAWLER_NAME = "91Porn"
CRAWLER_PROTOCOL = "crawler.v1"


def crawler_source_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return safe[:160]


def write_jsonl(event: dict):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def positive_int(*values, default: int) -> int:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return default


class Porn91Spider:
    def __init__(
        self,
        output_file: str = None,
        start_page: int = 1,
        max_pages: int = None,
        resume: bool = None,
        max_empty_pages: int = None,
        quiet: bool = False,
        target_new: int = None,
        seen_viewkeys: list = None,
        stream_output: bool = False,
        stream_protocol: str = "legacy",
        category: str = None,
        series_url: str = None,
    ):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.cookies.set("mode", "d")

        self.output_file = output_file if output_file is not None else OUTPUT_FILE
        self.start_page = max(1, int(start_page or 1))
        self.max_pages = max_pages if max_pages is None or max_pages > 0 else None
        self.resume = RESUME if resume is None else bool(resume)
        self.max_empty_pages = (
            MAX_EMPTY_PAGES if max_empty_pages is None else int(max_empty_pages)
        )
        self.target_new = target_new if target_new and target_new > 0 else None
        self.quiet = bool(quiet)
        self.stream_output = bool(stream_output)
        self.stream_protocol = stream_protocol or "legacy"
        # 新增: 支持按分类抓取，None 或空字符串表示不带 category（抓取全部）
        self.category = category.strip() if isinstance(category, str) and category.strip() else None
        # 新增: 支持抓取单个外部系列页（例如 xchina 系列页）
        self.series_url = series_url.strip() if isinstance(series_url, str) and series_url.strip() else None

        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry_strategy = Retry(
                total=MAX_RETRIES,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)
        except ImportError:
            pass

        self.results = []
        self.pages_crawled = 0
        self.processed_videos = 0
        self.skipped_videos = 0
        self.failed_videos = 0
        self.skip_viewkeys = set()

        if seen_viewkeys:
            for vk in seen_viewkeys:
                if not vk:
                    continue
                vk = vk.strip()
                if vk:
                    self.skip_viewkeys.add(vk)

        if self.resume and os.path.exists(self.output_file):
            try:
                with open(self.output_file, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
                existing_videos = existing_data.get('videos', [])
                self.results = existing_videos
                for v in existing_videos:
                    vk = v.get('viewkey', '')
                    if vk:
                        self.skip_viewkeys.add(vk)
                self.processed_videos = existing_data.get('successful', 0)
                self.failed_videos = existing_data.get('failed', 0)
                self.log(f"加载已有数据: {len(self.results)} 个视频, 将跳过已处理项")
            except Exception:
                pass

    def log(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        if self.stream_output:
            print(line, file=sys.stderr, flush=True)
        else:
            print(line)

    def emit_stream_video(self, video: dict):
        if not self.stream_output:
            return
        try:
            if self.stream_protocol == "crawler.v1":
                source_id = crawler_source_id(video.get("source_id") or video.get("viewkey") or "")
                item = {
                    "title": video.get("title") or "",
                    "detail_url": video.get("detail_url") or "",
                    "author": "91porn",
                    "tags": ["91porn"],
                    "media_url": video.get("video_url") or "",
                    "thumbnail_url": video.get("thumb_url") or "",
                    "headers": {
                        "Referer": video.get("detail_url") or BASE_URL,
                    },
                }
                if source_id:
                    item["source_id"] = source_id
                event = {
                    "type": "item",
                    "item": item,
                }
                write_jsonl(event)
            else:
                print(json.dumps(video, ensure_ascii=False), flush=True)
        except Exception as e:
            print(f"[stream] emit failed: {e}", file=sys.stderr, flush=True)

    def random_sleep(self, min_sec: float, max_sec: float):
        delay = random.uniform(min_sec, max_sec)
        if not self.quiet:
            self.log(f"  随机延时 {delay:.2f} 秒...")
        time.sleep(delay)

    def fetch_page(self, url: str, description: str = "", referer: str = "") -> str:
        headers_extra = {}
        if referer:
            headers_extra["Referer"] = referer

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.log(f"正在请求: {description or url} (尝试 {attempt}/{MAX_RETRIES})")
                response = self.session.get(url, timeout=30, headers=headers_extra)

                if response.status_code == 403:
                    self.log("警告: 收到 403 Forbidden，可能被拦截")
                    if attempt < MAX_RETRIES:
                        self.random_sleep(RETRY_DELAY, RETRY_DELAY + 3)
                        continue
                    return ""

                response.raise_for_status()

                try:
                    html_content = response.content.decode('utf-8', errors='replace')
                except Exception:
                    html_content = response.text

                is_cf_challenge = (
                    "Just a moment" in html_content and
                    len(html_content) < 8000
                )
                if is_cf_challenge:
                    self.log("警告: 页面被Cloudflare挑战拦截，需要浏览器环境或正确cookie")
                    if attempt < MAX_RETRIES:
                        self.random_sleep(RETRY_DELAY, RETRY_DELAY + 5)
                        continue
                    return ""

                return html_content
            except requests.exceptions.HTTPError as e:
                self.log(f"HTTP错误: {e}")
                if attempt < MAX_RETRIES:
                    self.random_sleep(RETRY_DELAY, RETRY_DELAY + 3)
                else:
                    return ""
            except requests.exceptions.RequestException as e:
                self.log(f"请求失败: {e}")
                if attempt < MAX_RETRIES:
                    self.random_sleep(RETRY_DELAY, RETRY_DELAY + 3)
                else:
                    self.log(f"达到最大重试次数，放弃: {url}")
                    return ""
        return ""

    def parse_list_page(self, html: str) -> list:
        videos = []
        soup = BeautifulSoup(html, 'lxml')

        video_cards = soup.select('div.col-xs-12.col-sm-4.col-md-3.col-lg-3')

        seen_cards = set()

        for card in video_cards:
            link = card.find('a', href=re.compile(r'view_video\.php\?viewkey='))
            if not link:
                continue
            href = link.get('href', '')
            if not href:
                continue

            match = re.search(r'viewkey=([^&]+)', href)
            if not match:
                continue
            viewkey = match.group(1)

            detail_url = urljoin(BASE_URL, href)

            title = self._extract_title(link)

            thumb_url = ""
            source_id = ""
            overlay = link.find(id=re.compile(r'^playvthumb_\d+$'))
            if overlay:
                source_id = overlay.get('id', '').rsplit('_', 1)[-1]
            img = link.find('img', class_=re.compile(r'img-responsive'))
            if img:
                thumb_url = img.get('src', '') or img.get('data-original', '')
                if thumb_url:
                    thumb_url = urljoin(BASE_URL, thumb_url)
            if not source_id and thumb_url:
                source_id = self._extract_thumb_source_id(thumb_url)

            card_key = source_id or detail_url
            if card_key in seen_cards:
                continue
            seen_cards.add(card_key)

            videos.append({
                "title": title,
                "detail_url": detail_url,
                "thumb_url": thumb_url,
                "viewkey": viewkey,
                "source_id": source_id
            })

        return videos

    def _extract_title(self, link) -> str:
        title_el = link.find('span', class_=re.compile(r'video-title'))
        if title_el:
            title = title_el.get_text(strip=True)
            if title:
                return html.unescape(title)

        title = link.get('title', '').strip()
        if title:
            return html.unescape(title)

        text = link.get_text(separator=' ', strip=True)
        text = re.sub(r'^(HD\s+|91\s+)?\d{2}:\d{2}:\d{2}\s*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return html.unescape(text)[:120]

    def parse_xchina_series_page(self, html: str) -> list:
        """解析 xchina-like 的系列页面，提取出单页内的所有视频条目（单页模式）。

        目标页面示例: https://xchina.co/videos/series-63824a975d8ae.html
        解析策略:
          - 查找 href 包含 "/videos/" 的链接作为视频详情页
          - 过滤掉包含 "series-" 的链接（避免再次匹配系列页自身）
          - 尽量从链接或子元素中提取标题和缩略图
        """
        videos = []
        if not html:
            return videos
        base = self.series_url or BASE_URL
        soup = BeautifulSoup(html, 'lxml')
        seen = set()

        for a in soup.select('a[href]'):
            href = a.get('href') or ''
            if '/videos/' not in href:
                continue
            if 'series-' in href:
                # 跳过系列页自身
                continue
            detail_url = urljoin(base, href)
            # 去重
            if detail_url in seen:
                continue
            seen.add(detail_url)

            title = a.get('title') or a.get_text(separator=' ', strip=True)
            title = html.unescape(title or '')[:160]

            thumb_url = ''
            img = a.find('img')
            if img:
                src = img.get('src') or img.get('data-src') or img.get('data-original') or ''
                if src:
                    thumb_url = urljoin(base, src)

            # 生成一个 viewkey-like 标识（取路径最后一段无扩展名）
            parsed = urlparse(detail_url)
            name = os.path.basename(parsed.path)
            vk = os.path.splitext(name)[0] or detail_url

            videos.append({
                'title': title,
                'detail_url': detail_url,
                'thumb_url': thumb_url,
                'viewkey': vk,
                'source_id': ''
            })

        return videos

    def parse_detail_page(self, html: str) -> dict:
        result = {}

        if not html:
            return result

        title = self._extract_detail_title(html)
        if title:
            result["title"] = title

        strencode_match = re.search(r'strencode2\(["\']([^"\']+)["\']\)', html)
        if strencode_match:
            encoded = strencode_match.group(1)
            try:
                decoded = unquote(encoded)

                src_match = re.search(r"src=['\"]([^'\"]+)['\"]", decoded)
                if src_match:
                    video_url = src_match.group(1)
                    video_url = re.sub(r'(https?://[^/]+)//+', r'\1/', video_url)
                    result["video_url"] = video_url
                    result["source_id"] = self._extract_source_id(video_url)
                    return result
            except Exception as e:
                self.log(f"  解码 strencode2 失败: {e}")

        mp4_match = re.search(
            r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*',
            html
        )
        if mp4_match:
            url = mp4_match.group(0)
            if 'kwai' not in url and 'ad-' not in url.lower():
                result["video_url"] = url
                result["source_id"] = self._extract_source_id(url)
                return result

        return result

    def _extract_detail_title(self, html_text: str) -> str:
        soup = BeautifulSoup(html_text, 'lxml')
        title_el = soup.find('title')
        if not title_el:
            return ""
        title = title_el.get_text(" ", strip=True)
        title = re.sub(r'\s*-\s*91porn.*$', '', title, flags=re.IGNORECASE).strip()
        return html.unescape(title)[:160]

    def _extract_source_id(self, video_url: str) -> str:
        path = urlparse(video_url or "").path
        name = os.path.basename(path)
        stem, ext = os.path.splitext(name)
        if ext.lower() not in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}:
            return ""
        source_id = re.sub(r'[^0-9]+', '', stem)
        if not source_id or source_id != stem:
            return ""
        return source_id

    def _extract_thumb_source_id(self, thumb_url: str) -> str:
        path = urlparse(thumb_url or "").path
        match = re.search(r'/thumb/(\d+)\.[A-Za-z0-9]+$', path)
        return match.group(1) if match else ""

    def _thumb_url_for_source(self, thumb_url: str, source_id: str) -> str:
        if not thumb_url or not source_id:
            return thumb_url
        parsed = urlparse(thumb_url)
        match = re.search(r'/thumb/([^/?#]+)\.[A-Za-z0-9]+$', parsed.path)
        if not match:
            return thumb_url
        current = match.group(1)
        if current == source_id:
            return thumb_url
        path = re.sub(
            r'/thumb/[^/?#]+\.[A-Za-z0-9]+$',
            f'/thumb/{source_id}.jpg',
            parsed.path,
        )
        return parsed._replace(path=path, query="", fragment="").geturl()

    def crawl(self):
        self.log("=" * 60)
        self.log("91porn 视频爬虫启动")
        self.log("=" * 60)
        self.log(f"配置: 列表页延时 {MIN_PAGE_DELAY}-{MAX_PAGE_DELAY}s, 详情页延时 {MIN_DETAIL_DELAY}-{MAX_DETAIL_DELAY}s")
        self.log(f"配置: 最大重试 {MAX_RETRIES} 次, 连续空页上限 {self.max_empty_pages}")
        self.log(f"配置: 起始页 {self.start_page}, 最大爬取页数 {self.max_pages if self.max_pages else '不限'}")
        if self.target_new:
            self.log(f"配置: 目标新增视频数 {self.target_new}")
        self.log(f"配置: 输出文件 {os.path.abspath(self.output_file)}")
        if self.skip_viewkeys:
            self.log(f"配置: 已跳过 {len(self.skip_viewkeys)} 个已知 viewkey")
        if self.category:
            self.log(f"配置: 指定分类 category={self.category}")
        else:
            self.log("配置: 未指定 category，默认抓取全部视频")
        if self.series_url:
            self.log(f"配置: 指定系列页 series_url={self.series_url} (单页模式)")
        self.log("")

        page_num = self.start_page
        consecutive_empty = 0
        crawled_in_session = 0

        while True:
            if self.max_pages is not None and crawled_in_session >= self.max_pages:
                self.log(f"达到配置的页数上限 {self.max_pages}，停止")
                break
            if consecutive_empty >= self.max_empty_pages:
                self.log(f"连续 {self.max_empty_pages} 页无结果，已达到末尾")
                break
            if self.target_new is not None and self.processed_videos >= self.target_new:
                self.log(f"已累计 {self.processed_videos} 个新视频，达到目标 {self.target_new}，停止")
                break

            # 构建基础列表页 URL：如果指定了 series_url 则抓取该页面（单页），否则按原有列表逻辑
            if self.series_url:
                page_url = self.series_url
            else:
                if self.category:
                    base_url = f"{BASE_URL}?category={self.category}&viewtype=basic"
                else:
                    base_url = f"{BASE_URL}?viewtype=basic"

                if page_num == 1:
                    page_url = base_url
                else:
                    page_url = f"{base_url}&page={page_num}"

            if crawled_in_session > 0:
                self.log("")
                self.random_sleep(MIN_PAGE_DELAY, MAX_PAGE_DELAY)

            self.log(f"[页 {page_num}] 请求: {page_url}")
            page_html = self.fetch_page(page_url, f"列表页 第{page_num}页")

            if not page_html:
                self.log(f"[页 {page_num}] 获取失败，跳过")
                consecutive_empty += 1
                page_num += 1
                crawled_in_session += 1
                # 如果是 series 单页模式，失败后直接结束
                if self.series_url:
                    break
                continue

            # 解析页面：系列页使用专用解析，否则使用原有解析
            if self.series_url:
                page_videos = self.parse_xchina_series_page(page_html)
            else:
                page_videos = self.parse_list_page(page_html)

            if not page_videos:
                self.log(f"[页 {page_num}] 页面无视频，可能已到末尾")
                consecutive_empty += 1
                page_num += 1
                crawled_in_session += 1
                # 系列单页模式遇到空结果也结束
                if self.series_url:
                    break
                continue

            consecutive_empty = 0

            new_videos = [v for v in page_videos if v['viewkey'] not in self.skip_viewkeys]
            skipped_on_page = len(page_videos) - len(new_videos)

            if skipped_on_page > 0:
                self.log(f"[页 {page_num}] 发现 {len(page_videos)} 个链接, 其中 {skipped_on_page} 个已处理, {len(new_videos)} 个新视频")
            else:
                self.log(f"[页 {page_num}] 发现 {len(new_videos)} 个视频")

            if new_videos:
                self._process_video_list(new_videos, referer=page_url)
            self.pages_crawled += 1
            page_num += 1
            crawled_in_session += 1

            # 如果是 series 单页模式，抓取完后立即结束
            if self.series_url:
                break

        self._save_results()
        self._print_summary()

    def _process_video_list(self, videos: list, referer: str = ""):
        for idx, video in enumerate(videos, 1):
            if self.target_new is not None and self.processed_videos >= self.target_new:
                return
            if video['viewkey'] in self.skip_viewkeys:
                self.log(f"  [SKIP] 已处理过: {video['viewkey']}")
                self.skipped_videos += 1
                continue

            self.log(f"  处理视频 {idx}/{len(videos)}: {video['title'][:40]}...")

            if idx > 1:
                self.random_sleep(MIN_DETAIL_DELAY, MAX_DETAIL_DELAY)

            detail_html = self.fetch_page(video['detail_url'], f"详情页 viewkey={video['viewkey']}", referer=referer)

            if not detail_html:
                self.log(f"  [FAIL] 详情页获取失败: {video['viewkey']}")
                video["video_url"] = ""
                self.results.append(video)
                self.skip_viewkeys.add(video['viewkey'])
                self.failed_videos += 1
                continue

            detail_info = self.parse_detail_page(detail_html)

            if detail_info.get("video_url"):
                video["video_url"] = detail_info["video_url"]
                if detail_info.get("title"):
                    video["title"] = detail_info["title"]
                list_source_id = video.get("source_id", "")
                detail_source_id = detail_info.get("source_id", "")
                if list_source_id and detail_source_id and list_source_id != detail_source_id:
                    self.log(
                        f"  [FAIL] 详情页视频源不匹配: list_source_id={list_source_id} "
                        f"detail_source_id={detail_source_id} viewkey={video['viewkey']}"
                    )
                    self.failed_videos += 1
                    self.skip_viewkeys.add(video['viewkey'])
                    continue
                if not list_source_id and detail_source_id:
                    video["source_id"] = detail_source_id
                if video.get("source_id"):
                    video["thumb_url"] = self._thumb_url_for_source(
                        video.get("thumb_url", ""),
                        video["source_id"],
                    )
                    if video["source_id"] in self.skip_viewkeys:
                        self.log(f"  [SKIP] 已处理过 source_id: {video['source_id']}")
                        self.skipped_videos += 1
                        continue
                self.results.append(video)
                self.skip_viewkeys.add(video['viewkey'])
                if video.get("source_id"):
                    self.skip_viewkeys.add(video["source_id"])
                self.processed_videos += 1
                self.log(f"  [OK] 成功提取视频直链")
                self.emit_stream_video(video)
            else:
                self.log(f"  [FAIL] 未找到视频直链: {video['viewkey']}")
                video["video_url"] = ""
                self.results.append(video)
                self.skip_viewkeys.add(video['viewkey'])
                self.failed_videos += 1

    def _save_results(self):
        output_data = {
            "crawl_time": datetime.now().isoformat(),
            "source_url": BASE_URL,
            "pages_crawled": self.pages_crawled,
            "total_videos": len(self.results),
            "successful": self.processed_videos,
            "skipped": self.skipped_videos,
            "failed": self.failed_videos,
            "videos": self.results
        }

        try:
            out_path = self.output_file
            parent = os.path.dirname(os.path.abspath(out_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp_path = out_path + ".part"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, out_path)
            self.log(f"结果已保存到: {os.path.abspath(out_path)}")
        except Exception as e:
            self.log(f"保存文件失败: {e}")
            backup_out = sys.stderr if self.stream_output else sys.stdout
            print("\n--- 备份输出 ---", file=backup_out, flush=True)
            print(json.dumps(output_data, ensure_ascii=False, indent=2), file=backup_out, flush=True)

    def _print_summary(self):
        self.log("")
        self.log("=" * 60)
        self.log("爬取完成!")
        self.log("=" * 60)
        self.log(f"爬取页数: {self.pages_crawled}")
        self.log(f"总视频数: {len(self.results)}")
        self.log(f"成功提取直链: {self.processed_videos}")
        self.log(f"跳过(已处理): {self.skipped_videos}")
        self.log(f"失败/缺失直链: {self.failed_videos}")
        self.log(f"输出文件: {os.path.abspath(self.output_file)}")
        self.log("=" * 60)


def print_help():
    print("""
================================================
    91porn 视频爬虫 v1.0
================================================

本脚本将爬取 91porn 列表下的所有视频信息：
  - 视频名称
  - 封面图直链
  - 视频直链 (MP4)

依赖安装:
    pip install requests beautifulsoup4 lxml PySocks

使用方法:
    python spider_91porn.py

配置说明 (编辑脚本内 "配置区域"):
    MIN_PAGE_DELAY / MAX_PAGE_DELAY : 列表页请求间隔 (默认 3-6 秒)
    MIN_DETAIL_DELAY / MAX_DETAIL_DELAY : 详情页请求间隔 (默认 2-5 秒)
    MAX_PAGES : 限制最大爬取页数 (None=不限, 如 5=只爬前5页)
    OUTPUT_FILE : 输出文件名 (默认 91porn_videos.json)

按 Ctrl+C 可随时中断并保存已爬取的数据

注意:
    1. 视频直链包含时效性token，会过期，需定期重新爬取
    2. 脚本已内置随机延时，请勿移除，避免对服务器造成压力
    3. 如遇到Cloudflare拦截，需要先通过浏览器获取Cookie
    4. 本脚本仅供学习交流，请遵守当地法律法规
================================================
""")


def run_job(job_path: str):
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)

    if job.get("protocol") != CRAWLER_PROTOCOL:
        raise ValueError(f"unsupported crawler protocol: {job.get('protocol')!r}")
    if job.get("mode") not in ("", None, "crawl"):
        raise ValueError(f"unsupported crawler mode: {job.get('mode')!r}")

    candidate_budget = positive_int(
        job.get("candidate_budget"),
        job.get("target_new"),
        default=15,
    )
    unique_target = positive_int(job.get("unique_target"), default=0)
    print(
        f"[job] unique_target={unique_target or 'unknown'} candidate_budget={candidate_budget}",
        file=sys.stderr,
        flush=True,
    )
    seen_file = job.get("seen_source_ids_file") or ""
    output_dir = job.get("output_dir") or os.getcwd()
    run_id = job.get("run_id") or datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"spider91-{run_id}.json")

    network = job.get("network") if isinstance(job.get("network"), dict) else {}
    proxy_url = str(network.get("proxy_url") or "").strip()
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url
        os.environ["NO_PROXY"] = ""
        os.environ["no_proxy"] = ""

    seen_viewkeys = []
    if seen_file:
        try:
            with open(seen_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        seen_viewkeys.append(line)
        except FileNotFoundError:
            print(f"警告: seen_source_ids_file 不存在: {seen_file}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"警告: 读取 seen_source_ids_file 失败: {e}", file=sys.stderr, flush=True)

    prefer_ipv4_for_plain_socks5_proxy()
    spider = Porn91Spider(
        output_file=output_file,
        start_page=1,
        max_pages=None,
        resume=False,
        quiet=True,
        target_new=candidate_budget,
        seen_viewkeys=seen_viewkeys,
        stream_output=True,
        stream_protocol="crawler.v1",
    )
    try:
        spider.crawl()
        done = {
            "type": "done",
            "stats": {
                "emitted": spider.processed_videos,
                "failed": spider.failed_videos,
                "skipped": spider.skipped_videos,
            },
        }
        write_jsonl(done)
    except KeyboardInterrupt:
        spider.log("\n用户中断，正在保存已爬取的数据...")
        spider._save_results()
        raise


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help', 'help'):
        print_help()
        return

    parser = argparse.ArgumentParser(
        prog="spider_91porn.py",
        description="91porn 视频元数据爬虫",
        add_help=False,
    )
    parser.add_argument("--page", type=int, default=None,
                        help="只爬指定页（单页模式，配合 --output 用于定时任务）")
    parser.add_argument("--output", type=str, default=None,
                        help="输出 JSON 路径，覆盖默认 OUTPUT_FILE")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="单页模式下，从 --page 起最多再爬几页（默认 1）")
    parser.add_argument("--no-resume", action="store_true",
                        help="禁用断点续爬（单页模式默认禁用）")
    parser.add_argument("--quiet", action="store_true",
                        help="压缩日志，每条视频只输出关键事件")
    parser.add_argument("--target-new", type=int, default=None,
                        help="目标新增模式：从 page 1 起翻页直到累计处理这么多新源视频后停止（backend 凌晨任务用）")
    parser.add_argument("--seen-viewkeys-file", type=str, default=None,
                        help="文件路径，每行一个已处理过的 viewkey 或 mp4 源 ID；脚本会跳过这些视频")
    parser.add_argument("--stream-output", action="store_true",
                        help="流式模式：每解析一条视频直链就立即把它作为一行 JSON 写到 stdout 并 flush；"
                             "日志改走 stderr。配合 backend 边读边下载使用。")
    parser.add_argument("--job", type=str, default=None,
                        help="crawler.v1 job JSON 路径；作为通用脚本爬虫运行。")
    # 新增: 支持指定 category（默认为 None，表示不带 category，抓取全部）
    parser.add_argument("--category", type=str, default=None,
                        help="分类，默认空表示抓取全部视频；例如 --category top")
    # 新增: 支持指定外部系列页（单页模式），例如 xchina 系列页
    parser.add_argument("--series-url", type=str, default=None,
                        help="系列页面 URL（单页模式），例如 --series-url https://xchina.co/videos/series-63824a975d8ae.html")

    args, _ = parser.parse_known_args()
    if args.job:
        run_job(args.job)
        return

    cli_out = sys.stderr if args.stream_output else sys.stdout
    prefer_ipv4_for_plain_socks5_proxy()

    print("""
================================================
    91porn 视频爬虫启动中...
================================================
按 Ctrl+C 可随时中断并保存进度
""", file=cli_out)

    seen_viewkeys = []
    if args.seen_viewkeys_file:
        try:
            with open(args.seen_viewkeys_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        seen_viewkeys.append(line)
        except FileNotFoundError:
            print(f"警告: --seen-viewkeys-file 不存在: {args.seen_viewkeys_file}", file=cli_out)
        except Exception as e:
            print(f"警告: 读取 --seen-viewkeys-file 失败: {e}", file=cli_out)

    if args.target_new is not None:
        spider = Porn91Spider(
            output_file=args.output,
            start_page=1,
            max_pages=None,
            resume=False,
            quiet=args.quiet,
            target_new=args.target_new,
            seen_viewkeys=seen_viewkeys,
            stream_output=args.stream_output,
            category=args.category,
            series_url=args.series_url,
        )
    elif args.page is not None:
        start_page = max(1, args.page)
        max_pages = args.max_pages if args.max_pages and args.max_pages > 0 else 1
        spider = Porn91Spider(
            output_file=args.output,
            start_page=start_page,
            max_pages=max_pages,
            resume=False,
            quiet=args.quiet,
            seen_viewkeys=seen_viewkeys,
            stream_output=args.stream_output,
            category=args.category,
            series_url=args.series_url,
        )
    else:
        spider = Porn91Spider(
            output_file=args.output,
            resume=False if args.no_resume else None,
            quiet=args.quiet,
            seen_viewkeys=seen_viewkeys,
            stream_output=args.stream_output,
            category=args.category,
            series_url=args.series_url,
        )

    try:
        spider.crawl()
    except KeyboardInterrupt:
        spider.log("\n用户中断，正在保存已爬取的数据...")
        spider._save_results()
        spider._print_summary()
        sys.exit(0)
    except Exception as e:
        spider.log(f"发生未预料的错误: {e}")
        import traceback
        traceback.print_exc(file=sys.stderr)
        spider._save_results()
        raise


if __name__ == "__main__":
    main()
