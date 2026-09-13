# -*- coding: utf-8 -*-
import asyncio
import aiohttp
import json
import re
import os
import time
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode, quote

# ==============================================
# 【可调参数区】全部参数集中于此，后续修改只改这里
# ==============================================
# 双黑名单配置
MAX_CONSECUTIVE_FAIL = 3        # 定时任务连续失败多少轮进入临时黑名单
TEMP_BLACKLIST_EXPIRE_DAY = 30  # 临时黑名单过期天数
MAX_TEMP_BLACKLIST_ENTRY = 4    # 累计进入临时黑名单多少次升级为永久黑名单
TEMP_BLACKLIST_PATH = "sources/临时黑名单.txt"
PERM_BLACKLIST_PATH = "sources/永久黑名单.txt"
# 测速配置
VLC_UA = "VLC/3.0.20 LibVLC/3.0.20"
CONCURRENCY_HTTP = 15       # http请求并发
CONCURRENCY_FFPROBE = 8     # ffprobe子进程并发(CPU密集，低于http)
TIMEOUT = 4                 # 单个http请求超时
SUCCESS_CODES = {200, 201, 202, 206}
MAX_SPEED_TEST_RUN_TIME = 5 * 3600 + 30 * 60  # 仅测速阶段最大运行时长5.5小时
HTTP_READ_BYTES = 2048       # http读取流字节数，优化检测准确性
SKIP_AUDIO_ONLY_STREAM = False # 是否跳过仅音频流；True=仅音频视为无效，False允许纯音频源有效
# ==============================================
# 文件夹路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
SOURCES_DIR = os.path.join(BASE_DIR, "sources")
CATEGORY_DIR = os.path.join(BASE_DIR, "category")
LOG_DIR = os.path.join(BASE_DIR, "log")
# 创建必要文件夹
for d in [SOURCES_DIR, CATEGORY_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)
# 全局变量：控制测速强制终止
stop_speed_test = False
start_time = time.time()

# ------------------------------
# 黑名单工具函数
# ------------------------------
def get_run_trigger_type():
    """获取触发类型: schedule=定时, workflow_dispatch=手动触发"""
    return os.environ.get("GITHUB_EVENT_NAME", "")

def load_perm_blacklist() -> set:
    """加载永久黑名单，返回url集合"""
    data = set()
    if os.path.exists(PERM_BLACKLIST_PATH):
        with open(PERM_BLACKLIST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                url = line.strip()
                if url:
                    data.add(url)
    return data

def save_perm_blacklist(black_set: set):
    with open(PERM_BLACKLIST_PATH, "w", encoding="utf-8") as f:
        for url in sorted(black_set):
            f.write(url + "\n")

def load_temp_blacklist() -> dict:
    """
    加载临时黑名单
    返回 {url: {"enter_time": datetime, "count": int}}
    文件格式：url|iso时间字符串|累计进入次数
    """
    result = {}
    if not os.path.exists(TEMP_BLACKLIST_PATH):
        return result
    with open(TEMP_BLACKLIST_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) != 3:
                continue
            url, time_str, cnt_str = parts
            try:
                enter_dt = datetime.fromisoformat(time_str)
                cnt = int(cnt_str)
                result[url] = {"enter_time": enter_dt, "count": cnt}
            except Exception:
                continue
    return result

def save_temp_blacklist(bl_dict: dict):
    lines = []
    for url, info in bl_dict.items():
        iso_time = info["enter_time"].isoformat()
        count = info["count"]
        lines.append(f"{url}|{iso_time}|{count}")
    with open(TEMP_BLACKLIST_PATH, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")

def clean_expired_temp_blacklist(temp_bl: dict) -> tuple[dict, set]:
    """清理过期临时黑名单；返回(保留未过期字典，被释放的url集合)"""
    now = datetime.now()
    keep_items = {}
    released_urls = set()
    for url, info in temp_bl.items():
        expire_dt = info["enter_time"] + timedelta(days=TEMP_BLACKLIST_EXPIRE_DAY)
        if now >= expire_dt:
            released_urls.add(url)
        else:
            keep_items[url] = info
    return keep_items, released_urls

# ------------------------------
# 工具函数
# ------------------------------
ZERO_WIDTH_PAT = re.compile(r'[\u200b\u200c\u200d\u2060\ufeff]')
LR_SUFFIX_PAT = re.compile(r'(\.m3u8.*?)\$LR•.*$', re.IGNORECASE)
PAREN_CONTENT_PAT = re.compile(r'[(\[].*?[)\]]')
RESOLUTION_TAG_PAT = re.compile(r'(1080p|720p|4K|HD|超清|高清)', re.IGNORECASE)

# IPTV垃圾线路query参数：需要移除的key（小写匹配）
TRASH_QUERY_KEYS = {
    "line", "lr", "route", "r", "sp", "channelno",
    "uid", "userid", "type", "t", "live_type", "srcidx"
}

def sanitize_iptv_url(raw_url: str) -> str:
    """
    IPTV直播源URL标准化清洗，顺序严格不可调换
    1. 删除.m3u8尾部 $LR•开头线路标记
    2. 清除零宽字符、换行、回车、制表符
    3. 删除括号及括号内全部内容，分辨率标记碎片
    4. 首尾空白，清除内部多余空白
    5. 协议头修复：
       - http:// https:// rtsp:// rtmp://完整协议直接放行
       - rtsp:/rtmp: 缺少//则补双斜杠
       - 完全无协议标记兜底补 https://
    6. Query参数处理：剔除垃圾线路参数，保留其余业务参数
    7. 局部编码：仅 path、query 做特殊字符编码；协议、域名、斜杠不编码
    """
    u = raw_url

    # ① 删除 .m3u8 后面以 $LR• 开头尾部字符串
    u = LR_SUFFIX_PAT.sub(r"\1", u)

    # ② 清除零宽字符、换行、回车、制表符
    u = ZERO_WIDTH_PAT.sub("", u)
    u = re.sub(r'[\r\n\t]', '', u)

    # ③ 删除括号内容、分辨率标记碎片
    u = PAREN_CONTENT_PAT.sub("", u)
    u = RESOLUTION_TAG_PAT.sub("", u)

    # ④ 首尾及内部多余空白全部清除
    u = re.sub(r'\s+', '', u.strip())
    if not u:
        return ""

    # ⑤协议头修复
    full_protos = ("http://", "https://", "rtsp://", "rtmp://")
    if u.startswith(full_protos):
        pass
    elif u.startswith("rtsp:"):
        u = "rtsp://" + u[5:]
    elif u.startswith("rtmp:"):
        u = "rtmp://" + u[5:]
    else:
        # 完全没有协议标记，兜底补 https://
        u = "https://" + u

    try:
        p = urlparse(u)

        # ⑥ query参数过滤：移除垃圾线路参数，保留其他参数
        qs_dict = parse_qs(p.query, keep_blank_values=True)
        new_qs = {}
        for k, v_list in qs_dict.items():
            if k.lower() not in TRASH_QUERY_KEYS:
                new_qs[k] = v_list
        new_query = urlencode(new_qs, doseq=True)

        # ⑦ 仅 path 和 query 编码；保护域名、scheme、/ 不被编码
        safe_chars = "/:-,._~"
        new_path = quote(p.path, safe=safe_chars)

        sanitized = urlunparse((
            p.scheme,
            p.netloc,
            new_path,
            p.params,
            new_query,
            p.fragment
        ))
        return sanitized
    except Exception:
        # 解析异常直接返回处理后的原始字符串
        return u

def clean_m3u_display_name(raw_name: str) -> str:
    """
    m3u输出用：清理显示名称，移除线路标记、分辨率、括号内容；
    区别于clean_channel_name：clean_channel_name是EPG匹配专用；本函数用于输出m3u界面展示
    """
    n = raw_name.strip()
    # 删除 $ 开头线路标记
    n = re.sub(r"\$.*", "", n)
    # 删除括号和内部
    n = re.sub(r"\(.*?\)", "", n)
    n = re.sub(r"\[.*?\]", "", n)
    # 删除分辨率高清标记
    n = re.sub(r"(高清|超清|1080p|720p|4K|HD)", "", n, flags=re.IGNORECASE)
    # 零宽字符、多余空格
    n = ZERO_WIDTH_PAT.sub("", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n

def clean_channel_name(raw_name: str) -> str:
    """
    【EPG匹配专用清洗】只用于查询tvg_id_map，输出m3u保持原始频道名不变
    1. 移除括号、方括号分辨率后缀 (1080p) [1080][S] [576][S]
    2. 移除 $LR•IPV4•29『线路xx』线路标记
    3. CCTV01 / CCTV1 → CCTV‑1，把两位/一位数字编号归一化
    4. 去除各类横杠变体、高清后缀、全部空白、零宽字符
    5. 去除首尾空格
    """
    name = raw_name.strip()
    # 移除线路后缀 $LR•IPV4•29『线路xx』
    name = re.sub(r"\$.*", "", name)
    # 移除圆括号内容 (xxx)
    name = re.sub(r"\(.*?\)", "", name)
    # 移除方括号内容 [xxx]
    name = re.sub(r"\[.*?\]", "", name)
    # ========= 增强修复部分 =========
    # 将各种长破折号、特殊横杠统一替换为标准减号
    name = re.sub(r"[‑–—―−]", "-", name)
    # 剔除高清、超清、分辨率标记后缀
    name = re.sub(r"(高清|超清|1080p|720p|4K|HD)", "", name, flags=re.IGNORECASE)
    # 兼容 CCTV1 / CCTV01 → CCTV‑1
    name = re.sub(r"CCTV0?(\d+)", r"CCTV-\1", name)
    # 删除全部空白字符（普通空格、制表、零宽空格）
    name = re.sub(r"\s+", "", name)
    # ==============================
    name = name.strip()
    return name

def load_json(path):
    """加载JSON文件【修复BUG：区分list/dict】
    DOWNLOAD_SOURCE_URLS.json 顶层为list，直接返回列表
    tvg_id_map.json等映射配置顶层为dict，返回字典
    """
    if not os.path.exists(path):
        print(f"[ERROR] JSON文件不存在: {path}")
        return [] if "DOWNLOAD_SOURCE_URLS" in path else dict()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            print(f"[DEBUG] 加载 {os.path.basename(path)}，读取列表长度：{len(data)}")
            return data
        elif isinstance(data, dict):
            print(f"[DEBUG] 加载 {os.path.basename(path)}，读取字典，keys数量:{len(data.keys())}")
            return data
        else:
            print(f"[WARN] {os.path.basename(path)} 不是字典/列表")
            return [] if "DOWNLOAD_SOURCE_URLS" in path else dict()
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON解析失败 {path} : {str(e)}")
        return [] if "DOWNLOAD_SOURCE_URLS" in path else dict()
    except Exception as e:
        print(f"[ERROR] 文件读取异常 {path}: {str(e)}")
        return [] if "DOWNLOAD_SOURCE_URLS" in path else dict()

def clean_text(s):
    """清理空白字符"""
    return s.strip() if s else ""

def url_standardize(url):
    """URL标准化（旧接口保留，实际业务使用sanitize_iptv_url）"""
    return clean_text(url)

def get_domain(url: str) -> str:
    """提取域名用于域名限流"""
    try:
        p = urlparse(url)
        return p.netloc or "unknown"
    except Exception:
        return "unknown"

def parse_m3u(content, source_url):
    """解析M3U/M3U8文件，提取name和url"""
    sources = []
    name_pattern = re.compile(r'tvg-name="([^"]+)"')
    lines = content.splitlines()
    current_name = ""
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF"):
            match = name_pattern.search(line)
            if match:
                current_name = match.group(1)
            else:
                current_name = line.split(",")[-1] if "," in line else "未知频道"
        elif line.startswith("http"):
            if current_name and line:
                sources.append(f"{clean_text(current_name)},{line} #{source_url}")
            current_name = ""
    return sources

def parse_txt(content, source_url):
    """解析TXT直播源文件"""
    sources = []
    lines = content.splitlines()
    for line in lines:
        line = clean_text(line)
        if not line or line.startswith("#"):
            continue
        if "," in line:
            name, url = line.split(",", 1)
            if url.startswith("http"):
                sources.append(f"{clean_text(name)},{clean_text(url)} #{source_url}")
        elif line.startswith("http"):
            sources.append(f"未知频道,{line} #{source_url}")
    return sources

# ===================== 1.下载直播源 =====================
async def download_sources():
    """从配置文件下载所有直播源"""
    url_file = os.path.join(CONFIG_DIR, "DOWNLOAD_SOURCE_URLS.json")
    source_urls = load_json(url_file)
    if not isinstance(source_urls, list) or len(source_urls) == 0:
        print("[FATAL] DOWNLOAD_SOURCE_URLS.json 下载地址为空，终止下载！")
        return {}
    all_sources = []
    source_map = defaultdict(list)
    print(f"[STEP1-DEBUG] 待下载源数量：{len(source_urls)}")
    async with aiohttp.ClientSession() as session:
        for idx, source_url in enumerate(source_urls):
            try:
                print(f"[STEP1] ({idx+1}/{len(source_urls)}) 下载: {source_url}")
                async with session.get(
                    source_url,
                    headers={"User-Agent": VLC_UA},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status not in SUCCESS_CODES:
                        print(f"[WARN] 状态码异常 {source_url} status={resp.status}")
                        continue
                    content = await resp.text(errors="ignore")
                    if source_url.endswith((".m3u", ".m3u8")):
                        items = parse_m3u(content, source_url)
                    else:
                        items = parse_txt(content, source_url)
                    all_sources.extend(items)
                    source_map[source_url] = items
                    print(f"[STEP1-DEBUG] {source_url} 解析得到 {len(items)} 条源")
            except Exception as e:
                print(f"[ERROR] 下载失败 {source_url}: {str(e)}")
    output = os.path.join(SOURCES_DIR, "下载源.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(all_sources))
    print(f"[STEP1-END] 下载完成，下载源.txt总条数：{len(all_sources)}")
    return source_map

# ===================== 2.汇总新旧直播源 =====================
def merge_sources():
    download = os.path.join(SOURCES_DIR, "下载源.txt")
    valid = os.path.join(SOURCES_DIR, "有效直播源.txt")
    merged = []
    cnt_download = 0
    cnt_valid_old = 0
    if os.path.exists(download):
        with open(download, "r", encoding="utf-8") as f:
            dl_list = [clean_text(l) for l in f if clean_text(l)]
            merged.extend(dl_list)
            cnt_download = len(dl_list)
    if os.path.exists(valid):
        with open(valid, "r", encoding="utf-8") as f:
            old_valid = [clean_text(l) for l in f if clean_text(l)]
            merged.extend(old_valid)
            cnt_valid_old = len(old_valid)
    output = os.path.join(SOURCES_DIR, "汇总.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(merged))
    print(f"[STEP2-END] 汇总完成；下载源:{cnt_download}条；旧有效源:{cnt_valid_old}条；汇总.txt合计：{len(merged)}条")

# ===================== 3.汇总直播源初步处理（加载双黑名单过滤） =====================
def process_merged():
    merged_path = os.path.join(SOURCES_DIR, "汇总.txt")
    if not os.path.exists(merged_path):
        print("[WARN] 汇总.txt不存在，跳过初处理")
        return
    # 加载双黑名单
    perm_black = load_perm_blacklist()
    temp_black = load_temp_blacklist()
    temp_black, released_urls = clean_expired_temp_blacklist(temp_black)
    save_temp_blacklist(temp_black)
    count_perm_invalid = 0   # 被永久黑名单过滤掉的数量
    count_temp_invalid = 0   # 被临时黑名单过滤掉的数量
    count_dup_url = 0         # url重复过滤
    count_bad_line = 0        # 坏行（空名字、空url等）
    raw_lines = []
    with open(merged_path, "r", encoding="utf-8") as f:
        raw_lines = [clean_text(l) for l in f if clean_text(l)]
    print(f"[STEP3-DEBUG] 初处理输入原始条数：{len(raw_lines)}")
    lines = []
    url_set = set()
    for line in raw_lines:
        if not line or "," not in line:
            count_bad_line += 1
            continue
        name, url_part = line.split(",", 1)
        raw_url = url_part.split("#")[0].strip()
        comment = "#" + url_part.split("#")[1] if "#" in url_part else ""

        # ✅执行URL标准化清洗
        url = sanitize_iptv_url(raw_url)
        if not url:
            count_bad_line += 1
            continue

        if not name or not url:
            count_bad_line += 1
            continue
        if url in perm_black:
            count_perm_invalid += 1
            continue
        if url in temp_black:
            count_temp_invalid += 1
            continue
        if url in url_set:
            count_dup_url += 1
            continue
        if name.isspace():
            count_bad_line += 1
            continue
        url_set.add(url)
        lines.append(f"{clean_text(name)},{url}{comment}")
    output = os.path.join(SOURCES_DIR, "初处理.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[STEP3-FILTER] 永久黑名单过滤：{count_perm_invalid} 条；临时黑名单过滤：{count_temp_invalid} 条；URL去重过滤：{count_dup_url} 条；坏行丢弃：{count_bad_line} 条")
    input_total = len(raw_lines)
    sum_filtered = count_perm_invalid + count_temp_invalid + count_dup_url + count_bad_line
    output_total = len(lines)
    calc_total = sum_filtered + output_total
    if input_total == calc_total:
        print(f"[STEP3-CHECK] ✅计数校验通过：输入{input_total} = 过滤{sum_filtered} + 输出{output_total}")
    else:
        print(f"[STEP3-CHECK] ⚠️计数校验不匹配！输入:{input_total} 计算合计:{calc_total}，请检查计数逻辑")
    print(f"[STEP3-END] 初处理完成，初处理.txt剩余 {len(lines)} 条")
    return released_urls

# ===================== 4.直播源测速 =====================
async def ffprobe_check(url: str, ffprobe_sem: asyncio.Semaphore) -> Tuple[bool, str, str, str, str]:
    try:
        async with ffprobe_sem:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-timeout", "3000000",
                "-stimeout", "3000000",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name,bit_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.2)
                output = stdout.decode().strip()
                data = output.splitlines() if output else []
                w = data[0] if len(data) > 0 else ""
                h = data[1] if len(data) > 1 else ""
                codec = data[2] if len(data) > 2 else ""
                br = data[3] if len(data) > 3 else "0"
                bitrate = str(int(br) // 1000) if br.isdigit() else "0"
                has_video = bool(w and h and w != "N/A" and h != "N/A")
                if SKIP_AUDIO_ONLY_STREAM and not has_video:
                    return False, "", "", "", ""
                return True, w, h, codec, bitrate
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return False, "", "", "", ""
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
    except Exception:
        return False, "", "", "", ""

async def test_single_source(
    session: aiohttp.ClientSession,
    http_sem: asyncio.Semaphore,
    domain_sem_map: dict[str, asyncio.Semaphore],
    ffprobe_sem: asyncio.Semaphore,
    line: str
) -> Tuple[Optional[dict], Optional[str], str]:
    """
    返回 (result, origin_line, error_reason)
    error_reason: 空字符串代表无异常，正常完成测速；其余为失败描述
    """
    try:
        line = clean_text(line)
        if not line or "," not in line:
            return None, line, "bad_line_format"
        name, url_part = line.split(",", 1)
        url = url_part.split("#")[0].strip()
        source = url_part.split("#")[1] if "#" in url_part else ""
        domain = get_domain(url)
        if domain not in domain_sem_map:
            domain_sem_map[domain] = asyncio.Semaphore(3)
        dom_sem = domain_sem_map[domain]
        async with http_sem, dom_sem:
            start = time.time()
            http_ok = False
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": VLC_UA},
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                ) as resp:
                    await resp.content.read(HTTP_READ_BYTES)
                    http_ok = resp.status in SUCCESS_CODES
            except aiohttp.ClientError:
                return None, line, "http_client_error"
            except asyncio.TimeoutError:
                return None, line, "http_timeout"
            except Exception:
                return None, line, "http_unknown_exception"
            delay = round((time.time() - start) * 1000)
            ff_ok, w, h, codec, br = await ffprobe_check(url, ffprobe_sem)
            valid = http_ok and ff_ok
            result = {
                "name": name, "url": url, "delay": delay,
                "width": w, "height": h, "codec": codec, "bitrate": br,
                "valid": valid, "source": source
            }
            return result, line, ""
    except asyncio.CancelledError:
        raise
    except Exception:
        return None, line, "task_inner_exception"

async def run_speed_test():
    input_path = os.path.join(SOURCES_DIR, "初处理.txt")
    if not os.path.exists(input_path):
        print("[WARN] 初处理.txt不存在，跳过测速")
        return []
    with open(input_path, "r", encoding="utf-8") as f:
        lines = [l for l in f if clean_text(l)]
    print(f"[STEP4-DEBUG] 测速任务待检测总条数：{len(lines)}")
    speed_test_start = time.time()
    http_sem = asyncio.Semaphore(CONCURRENCY_HTTP)
    ffprobe_sem = asyncio.Semaphore(CONCURRENCY_FFPROBE)
    domain_sem_map: dict[str, asyncio.Semaphore] = dict()
    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY_HTTP,
        ttl_dns_cache=300,
        force_close=False
    )
    valid_tmp = os.path.join(SOURCES_DIR, ".valid.tmp")
    fail_tmp = os.path.join(SOURCES_DIR, ".fail.tmp")
    results = []
    async with aiohttp.ClientSession(connector=connector) as session:
        async with asyncio.TaskGroup() as tg:
            task_list = [
                tg.create_task(test_single_source(session, http_sem, domain_sem_map, ffprobe_sem, ln))
                for ln in lines
            ]
            completed_count = 0
            f_valid = open(valid_tmp, "w", encoding="utf-8")
            f_fail = open(fail_tmp, "w", encoding="utf-8")
            try:
                for task in asyncio.as_completed(task_list):
                    if time.time() - speed_test_start > MAX_SPEED_TEST_RUN_TIME:
                        print(f"⚠️ [STEP4] 测速阶段达到最大时长 {MAX_SPEED_TEST_RUN_TIME}s，触发TaskGroup取消全部测速任务")
                        for t in task_list:
                            if not t.done():
                                t.cancel()
                        stop_speed_test = True
                        break
                    res, origin_line, err_reason = await task
                    completed_count += 1
                    if res is not None:
                        results.append(res)
                        if res["valid"]:
                            f_valid.write(origin_line + "\n")
                        else:
                            f_fail.write(origin_line + "\n")
                    if completed_count % 50 == 0:
                        valid_cnt = sum(1 for r in results if r["valid"])
                        fail_cnt = len(results) - valid_cnt
                        print(f"[STEP4-PROGRESS] 已测速 {completed_count}/{len(lines)} 有效:{valid_cnt} 失败:{fail_cnt} last_err:{err_reason}")
            finally:
                f_valid.close()
                f_fail.close()
    valid_out = os.path.join(SOURCES_DIR, "有效直播源.txt")
    fail_out = os.path.join(SOURCES_DIR, "测速失败.txt")
    if os.path.exists(valid_tmp):
        os.replace(valid_tmp, valid_out)
    if os.path.exists(fail_tmp):
        os.replace(fail_tmp, fail_out)
    valid_final = sum(1 for r in results if r["valid"])
    fail_final = len(results) - valid_final
    print(f"[STEP4-END] 测速结束；有效:{valid_final}条；本轮失败:{fail_final}条；总结果集:{len(results)}")
    return results

# ===================== 5.测速结果处理｜双黑名单更新逻辑 =====================
def update_blacklist_logic(results, released_urls):
    """
    测速结束后黑名单更新逻辑
    released_urls：本轮从临时黑名单过期释放出来的url集合
    """
    trigger_type = get_run_trigger_type()
    perm_black = load_perm_blacklist()
    temp_black = load_temp_blacklist()
    # 初始化连续失败计数器
    url_consec_fail = {}
    for ru in released_urls:
        url_consec_fail[ru] = 0
    for item in results:
        url = item["url"]
        if url not in url_consec_fail:
            url_consec_fail[url] = 0
        if item["valid"]:
            url_consec_fail[url] = 0
        else:
            # 只有定时任务才累加连续失败
            if trigger_type == "schedule":
                url_consec_fail[url] += 1
    # 仅定时任务执行黑名单新增、升级
    if trigger_type == "schedule":
        new_add_temp = set()
        for url, fail_cnt in url_consec_fail.items():
            if fail_cnt >= MAX_CONSECUTIVE_FAIL:
                if url not in temp_black:
                    new_add_temp.add(url)
        # 新增进入临时黑名单
        for url in new_add_temp:
            old_cnt = temp_black[url]["count"] if url in temp_black else 0
            new_cnt = old_cnt + 1
            temp_black[url] = {
                "enter_time": datetime.now(),
                "count": new_cnt
            }
        # 判断升级进入永久黑名单
        move_perm = set()
        for url in list(temp_black.keys()):
            info = temp_black[url]
            if info["count"] >= MAX_TEMP_BLACKLIST_ENTRY:
                move_perm.add(url)
                del temp_black[url]
        if move_perm:
            perm_black.update(move_perm)
            save_perm_blacklist(perm_black)
        save_temp_blacklist(temp_black)
        print(f"[BLACKLIST-INFO] 本轮新增临时黑名单:{len(new_add_temp)}；升级永久黑名单:{len(move_perm)}")
    else:
        print("[BLACKLIST-INFO] 当前为手动触发，不新增黑名单记录")

def generate_source_report(source_map, results):
    total = defaultdict(int)
    valid = defaultdict(int)
    for r in results:
        s = r["source"]
        total[s] += 1
        if r["valid"]:
            valid[s] += 1
    report = []
    for url, sources in source_map.items():
        t = total.get(url, 0)
        v = valid.get(url, 0)
        f = t - v
        rate = f / t if t > 0 else 0
        report.append({
            "source_url": url,
            "total": t,
            "valid": v,
            "failed": f,
            "failure_rate": round(rate, 4),
            "available_rate": round(1 - rate, 4)
        })
    report_path = os.path.join(LOG_DIR, "source_quality_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    bad_urls = [r["source_url"] for r in sorted(report, key=lambda x: x["failure_rate"], reverse=True)[:3]]
    bad_out = os.path.join(SOURCES_DIR, "失效源地址.txt")
    with open(bad_out, "w", encoding="utf-8") as f:
        f.write("\n".join(bad_urls))
    print(f"[STEP5-END] 源质量报告已生成，失效率TOP3源：{bad_urls}")

# =====================6.有效直播源处理&分类 =====================
def process_valid_sources():
    path = os.path.join(SOURCES_DIR, "有效直播源.txt")
    if not os.path.exists(path):
        print("[WARN]有效直播源.txt不存在")
        return []
    raw_count = 0
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        raw_list = f.readlines()
        raw_count = len(raw_list)
        for l in raw_list:
            l = clean_text(l)
            if "," in l:
                name, url = l.split(",", 1)
                url = url.split("#")[0].strip()
                lines.append(f"{clean_text(name)},{url}")
    lines = sorted(list(set(lines)), key=lambda x: x.split(",")[0])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    # 总合集m3u
    m3u_out_path = os.path.join(SOURCES_DIR, "有效直播源.m3u")
    epg_map = load_json(os.path.join(CONFIG_DIR, "tvg_id_map.json"))
    print(f"[DEBUG] process_valid_sources tvg_id_map加载key总数:{len(epg_map)}")
    with open(m3u_out_path, "w", encoding="utf-8") as fm:
        fm.write("#EXTM3U\n")
        for line in lines:
            n, u = line.split(",", 1)
            lookup_name = clean_channel_name(n)
            display_name = clean_m3u_display_name(n)
            # =========EPG调试打印，调试完成后可注释/删除=========
            print(f"[EPG-DEBUG] 原始={repr(n)} | lookup={repr(lookup_name)} | in_map={lookup_name in epg_map}")
            tvg_id = epg_map.get(lookup_name, "")
            fm.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{display_name}",{display_name}\n{u}\n')
    print(f"[STEP6-INFO] 有效直播源.m3u已生成，路径:{m3u_out_path}")
    print(f"[STEP6-END] 有效源处理完成：输入{raw_count}，去重排序后输出{len(lines)}条")
    return lines

def generate_categories(sources):
    # 白名单：仅这些分类的m3u写入#EPGURL，已修改央视 → CCTV
    EPG_ALLOW_CATS = {"CCTV", "卫视", "地方台"}
    for f in os.listdir(CATEGORY_DIR):
        os.remove(os.path.join(CATEGORY_DIR, f))
    import sys
    sys.path.insert(0, CONFIG_DIR)
    from category import get_channel_categories, get_all_category_names
    all_defined_cats = get_all_category_names()
    print(f"[STEP6-INFO] 代码预定义全部分类总数：{len(all_defined_cats)}")
    cat_map = defaultdict(list)
    other_count = 0
    match_count = 0
    for line in sources:
        name, url = line.split(",", 1)
        cats = get_channel_categories(name, url)
        if cats:
            match_count += 1
            for c in cats:
                cat_map[c].append(line)
        else:
            cat_map["未分类"].append(line)
            other_count += 1
    epg_map = load_json(os.path.join(CONFIG_DIR, "tvg_id_map.json"))
    print(f"[DEBUG] generate_categories tvg_id_map加载key总数:{len(epg_map)}")
    epg_cfg = load_json(os.path.join(CONFIG_DIR, "epg.json"))
    epg_url = epg_cfg.get("epg_url", "")
    tvg_logo_base = epg_cfg.get("tvg_logo_base", "").rstrip("/")
    generated = 0
    for cat_name, items in cat_map.items():
        if not items:
            continue
        txt_path = os.path.join(CATEGORY_DIR, f"{cat_name}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(items))
        m3u_path = os.path.join(CATEGORY_DIR, f"{cat_name}.m3u")
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            # 只有白名单内分类才写#EPGURL
            if cat_name in EPG_ALLOW_CATS and epg_url:
                f.write(f'#EPGURL={epg_url}\n')
            for line in items:
                n, u = line.split(",", 1)
                lookup_name = clean_channel_name(n)
                display_name = clean_m3u_display_name(n)
                tvg_id = epg_map.get(lookup_name, "")
                # 只有白名单分类才输出tvg‑logo
                if cat_name in EPG_ALLOW_CATS and tvg_id and tvg_logo_base:
                    logo_url = f"{tvg_logo_base}/{tvg_id}.png"
                    f.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{display_name}" tvg-logo="{logo_url}",{display_name}\n{u}\n')
                else:
                    f.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{display_name}",{display_name}\n{u}\n')
        generated += 1
    print(f"[STEP6-END] 分类完成；命中规则:{match_count}条；归入未分类:{other_count}条；生成分类文件数量：{generated}")

# =====================主流程 =====================
async def main():
    print("=" * 60)
    print(f"IPT任务启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    global start_time
    start_time = time.time()
    source_map = await download_sources()
    if not source_map:
        print("[FATAL]下载阶段无数据，任务直接退出")
        return
    merge_sources()
    released_urls = process_merged()
    results = await run_speed_test()
    if len(results) > 0:
        update_blacklist_logic(results, released_urls)
        generate_source_report(source_map, results)
        valid_sources = process_valid_sources()
        if valid_sources:
            generate_categories(valid_sources)
    else:
        print("[WARN]测速结果为空，跳过统计、分类")
    elapsed = round(time.time() - start_time, 2)
    print("=" * 60)
    print(f"✅全部流程结束，总耗时 {elapsed} 秒")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())