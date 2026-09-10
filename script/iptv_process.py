import asyncio
import aiohttp
import json
import re
import os
import time
import csv
from datetime import datetime
from collections import defaultdict

# ===================== 基础配置 =====================
# 文件夹路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
SOURCES_DIR = os.path.join(BASE_DIR, "sources")
CATEGORY_DIR = os.path.join(BASE_DIR, "category")
LOG_DIR = os.path.join(BASE_DIR, "log")

# 创建必要文件夹
for d in [SOURCES_DIR, CATEGORY_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

# 测速配置
VLC_UA = "VLC/3.0.20 LibVLC/3.0.20"
CONCURRENCY = 15  # 并发数
TIMEOUT = 4  # 单个请求超时
SUCCESS_CODES = {200, 201, 202, 206}
MAX_RUN_TIME = 5 * 3600 + 30 * 60  # 5.5 小时强制结束

# 全局变量：控制测速强制终止
stop_speed_test = False
start_time = time.time()

# ===================== 工具函数 =====================
def load_json(path):
    """加载JSON文件"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def clean_text(s):
    """清理空白字符"""
    return s.strip() if s else ""

def url_standardize(url):
    """URL标准化"""
    return clean_text(url)

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

# ===================== 1. 下载直播源 =====================
async def download_sources():
    """从配置文件下载所有直播源"""
    url_file = os.path.join(CONFIG_DIR, "DOWNLOAD_SOURCE_URLS.json")
    source_urls = load_json(url_file)
    all_sources = []
    source_map = defaultdict(list)  # 记录每个源下载的内容

    async with aiohttp.ClientSession() as session:
        for source_url in source_urls:
            try:
                print(f"下载: {source_url}")
                async with session.get(
                    source_url, 
                    headers={"User-Agent": VLC_UA},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status not in SUCCESS_CODES:
                        continue
                    content = await resp.text(errors="ignore")
                    # 解析不同格式
                    if source_url.endswith((".m3u", ".m3u8")):
                        items = parse_m3u(content, source_url)
                    else:
                        items = parse_txt(content, source_url)
                    all_sources.extend(items)
                    source_map[source_url] = items
            except Exception as e:
                print(f"下载失败 {source_url}: {str(e)}")

    # 写入下载源.txt
    output = os.path.join(SOURCES_DIR, "下载源.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(all_sources))
    
    print(f"下载完成，共 {len(all_sources)} 条源")
    return source_map

# ===================== 3. 汇总新旧源 =====================
def merge_sources():
    download = os.path.join(SOURCES_DIR, "下载源.txt")
    valid = os.path.join(SOURCES_DIR, "有效直播源.txt")
    merged = []

    for file in [download, valid]:
        if os.path.exists(file):
            with open(file, "r", encoding="utf-8") as f:
                merged.extend([clean_text(l) for l in f if clean_text(l)])

    output = os.path.join(SOURCES_DIR, "汇总.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(merged))
    print("新旧源合并完成")

# ===================== 4. 初步处理 =====================
def process_merged():
    merged_path = os.path.join(SOURCES_DIR, "汇总.txt")
    invalid_path = os.path.join(SOURCES_DIR, "永久失效.txt")
    
    if not os.path.exists(merged_path):
        return
    
    # 读取永久失效URL
    invalid_urls = set()
    if os.path.exists(invalid_path):
        with open(invalid_path, "r", encoding="utf-8") as f:
            for line in f:
                if "," in line:
                    invalid_urls.add(line.split(",", 1)[1].split("#")[0].strip())

    # 读取并处理
    lines = []
    url_set = set()
    with open(merged_path, "r", encoding="utf-8") as f:
        for line in f:
            line = clean_text(line)
            if not line or "," not in line:
                continue
            name, url_part = line.split(",", 1)
            url = url_part.split("#")[0].strip()
            comment = "#" + url_part.split("#")[1] if "#" in url_part else ""

            # 过滤条件
            if not name or not url:
                continue
            if url in invalid_urls:
                continue
            if url in url_set:
                continue
            if name.isspace():
                continue

            url_set.add(url)
            lines.append(f"{clean_text(name)},{url}{comment}")

    # 输出
    output = os.path.join(SOURCES_DIR, "初处理.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"初处理完成，剩余 {len(lines)} 条")

# ===================== 5. 异步测速核心 =====================
async def ffprobe_check(url):
    """异步ffprobe流检测"""
    try:
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
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.5)
            output = stdout.decode().strip()
            data = output.splitlines() if output else []
            w = data[0] if len(data) > 0 else ""
            h = data[1] if len(data) > 1 else ""
            codec = data[2] if len(data) > 2 else ""
            br = data[3] if len(data) > 3 else "0"
            bitrate = str(int(br) // 1000) if br.isdigit() else ""
            return True, w, h, codec, bitrate
        except:
            proc.kill()
            return False, "", "", "", ""
    except:
        return False, "", "", "", ""

async def test_single_source(session, semaphore, line):
    """单条源测速"""
    global stop_speed_test
    if stop_speed_test:
        return None, None

    if time.time() - start_time > MAX_RUN_TIME:
        stop_speed_test = True
        print("⚠️ 运行超时，终止测速")
        return None, None

    try:
        async with semaphore:
            line = clean_text(line)
            if not line or "," not in line:
                return None, None

            name, url_part = line.split(",", 1)
            url = url_part.split("#")[0].strip()
            source = url_part.split("#")[1] if "#" in url_part else ""

            start = time.time()
            # aiohttp 预检（仅读取前512字节）
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": VLC_UA},
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                ) as resp:
                    await resp.content.read(512)
                    http_ok = resp.status in SUCCESS_CODES
            except:
                http_ok = False

            delay = round((time.time() - start) * 1000)
            ff_ok, w, h, codec, br = await ffprobe_check(url)
            valid = http_ok and ff_ok

            result = {
                "name": name, "url": url, "delay": delay,
                "width": w, "height": h, "codec": codec, "bitrate": br,
                "valid": valid, "source": source
            }
            return result, line
    except:
        return None, None

async def run_speed_test():
    """执行批量测速"""
    input_path = os.path.join(SOURCES_DIR, "初处理.txt")
    if not os.path.exists(input_path):
        return []

    with open(input_path, "r", encoding="utf-8") as f:
        lines = [l for l in f if clean_text(l)]

    sem = asyncio.Semaphore(CONCURRENCY)
    results = []
    valid_list = []
    fail_list = []

    async with aiohttp.ClientSession() as session:
        tasks = [test_single_source(session, sem, l) for l in lines]
        for task in asyncio.as_completed(tasks):
            res, line = await task
            if res:
                results.append(res)
                if res["valid"]:
                    valid_list.append(line)
                else:
                    fail_list.append(line)

    # 保存结果
    valid_out = os.path.join(SOURCES_DIR, "有效直播源.txt")
    fail_out = os.path.join(SOURCES_DIR, "测速失败.txt")
    with open(valid_out, "w", encoding="utf-8") as f:
        f.write("\n".join(valid_list))
    with open(fail_out, "a", encoding="utf-8") as f:
        f.write("\n".join(fail_list) + "\n")

    print(f"测速完成：有效 {len(valid_list)} 条，失败 {len(fail_list)} 条")
    return results

# ===================== 6. 测速结果统计 =====================
def save_speed_csv(results):
    """保存测速详情CSV"""
    path = os.path.join(LOG_DIR, "测速详情.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "name", "url", "delay_ms", "width", "height",
            "codec", "bitrate_kb", "status"
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({
                "name": r["name"], "url": r["url"],
                "delay_ms": r["delay"], "width": r["width"],
                "height": r["height"], "codec": r["codec"],
                "bitrate_kb": r["bitrate"],
                "status": "有效" if r["valid"] else "失败"
            })

def update_permanent_invalid():
    """更新永久失效列表（连续3次失败）"""
    fail_path = os.path.join(SOURCES_DIR, "测速失败.txt")
    perm_path = os.path.join(SOURCES_DIR, "永久失效.txt")
    invalid_urls = set()

    if os.path.exists(fail_path):
        with open(fail_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            count = defaultdict(int)
            for l in lines:
                if "," in l:
                    url = l.split(",", 1)[1].split("#")[0].strip()
                    count[url] += 1
                    if count[url] >= 3:
                        invalid_urls.add(l.strip())

    with open(perm_path, "w", encoding="utf-8") as f:
        f.write("\n".join(invalid_urls))
    print(f"永久失效源：{len(invalid_urls)} 条")

def generate_source_report(source_map, results):
    """生成源质量报告"""
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

    # 保存报告
    with open(os.path.join(LOG_DIR, "source_quality_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 失效源地址
    bad_urls = [r["source_url"] for r in sorted(report, key=lambda x: x["failure_rate"], reverse=True)[:3]]
    with open(os.path.join(SOURCES_DIR, "失效源地址.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(bad_urls))

# ===================== 7. 有效源处理 + 分类 =====================
def process_valid_sources():
    """清理有效源并排序"""
    path = os.path.join(SOURCES_DIR, "有效直播源.txt")
    if not os.path.exists(path):
        return []

    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for l in f:
            l = clean_text(l)
            if "," in l:
                name, url = l.split(",", 1)
                url = url.split("#")[0].strip()
                lines.append(f"{clean_text(name)},{url}")

    # 名称升序
    lines = sorted(list(set(lines)), key=lambda x: x.split(",")[0])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return lines

def generate_categories(sources):
    """分类并生成文件"""
    # 清空分类目录
    for f in os.listdir(CATEGORY_DIR):
        os.remove(os.path.join(CATEGORY_DIR, f))

    # 加载分类逻辑
    import sys
    sys.path.append(CONFIG_DIR)
    from category import classify_source

    # 分类映射
    cat_map = defaultdict(list)
    for line in sources:
        name, url = line.split(",", 1)
        cats = classify_source(name, url)
        for c in cats:
            cat_map[c].append(line)

    # 生成 TXT + M3U
    epg_map = load_json(os.path.join(CONFIG_DIR, "tvg_id_map.json"))
    epg_cfg = load_json(os.path.join(CONFIG_DIR, "epg.json"))
    epg_url = epg_cfg.get("url", "")

    for cat_name, items in cat_map.items():
        if not items:
            continue
        # TXT
        txt_path = os.path.join(CATEGORY_DIR, f"{cat_name}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(items))

        # M3U
        m3u_path = os.path.join(CATEGORY_DIR, f"{cat_name}.m3u")
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            if epg_url:
                f.write(f'#EXT-X-URL: {epg_url}\n')
            for line in items:
                n, u = line.split(",", 1)
                tvg_id = epg_map.get(n, "")
                br = "1000"  # 默认码率
                delay = "200"
                f.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{n}",{n} [{br}kbps {delay}ms]\n{u}\n')

    print(f"分类完成，生成 {len(cat_map)} 个分类")

# ===================== 主流程 =====================
async def main():
    print("=" * 50)
    print("IPTV 直播源自动更新开始")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 1 下载
    source_map = await download_sources()
    # 3 汇总
    merge_sources()
    # 4 初处理
    process_merged()
    # 5 测速
    results = await run_speed_test()
    # 6 统计
    save_speed_csv(results)
    update_permanent_invalid()
    generate_source_report(source_map, results)
    # 7 有效源 + 分类
    valid_sources = process_valid_sources()
    generate_categories(valid_sources)

    print("=" * 50)
    print("✅ 全部任务完成")
    print("=" * 50)

if __name__ == "__main__":
    asyncio.run(main())