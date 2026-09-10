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
    """加载JSON文件【带调试日志】"""
    if not os.path.exists(path):
        print(f"[ERROR] JSON文件不存在: {path}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            print(f"[DEBUG] 加载 {os.path.basename(path)}，读取列表长度：{len(data)}")
        else:
            print(f"[DEBUG] 加载 {os.path.basename(path)}，读取字典，keys:{list(data.keys())}")
        return data
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON解析失败 {path} : {str(e)}")
        return []
    except Exception as e:
        print(f"[ERROR] 文件读取异常 {path}: {str(e)}")
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

# ===================== 3.汇总新旧直播源 =====================
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
    print(f"[STEP2-DEBUG] 下载源:{cnt_download}条；旧有效源:{cnt_valid_old}条；汇总.txt合计：{len(merged)}条")

# ===================== 4.汇总直播源初步处理 =====================
def process_merged():
    merged_path = os.path.join(SOURCES_DIR, "汇总.txt")
    invalid_path = os.path.join(SOURCES_DIR, "永久失效.txt")
    if not os.path.exists(merged_path):
        print("[WARN] 汇总.txt不存在，跳过初处理")
        return

    invalid_urls = set()
    if os.path.exists(invalid_path):
        with open(invalid_path, "r", encoding="utf-8") as f:
            for line in f:
                if "," in line:
                    u = line.split(",",1)[1].split("#")[0].strip()
                    invalid_urls.add(u)

    raw_lines = []
    with open(merged_path, "r", encoding="utf-8") as f:
        raw_lines = [clean_text(l) for l in f if clean_text(l)]
    print(f"[STEP3-DEBUG] 初处理输入原始条数：{len(raw_lines)}")

    lines = []
    url_set = set()
    for line in raw_lines:
        if not line or "," not in line:
            continue
        name, url_part = line.split(",", 1)
        url = url_part.split("#")[0].strip()
        comment = "#" + url_part.split("#")[1] if "#" in url_part else ""

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

    output = os.path.join(SOURCES_DIR, "初处理.txt")
    with open(output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[STEP3-END] 初处理完成，初处理.txt剩余 {len(lines)} 条")

# ===================== 5.直播源测速 =====================
async def ffprobe_check(url):
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
    global stop_speed_test
    if stop_speed_test:
        return None, None
    if time.time() - start_time > MAX_RUN_TIME:
        stop_speed_test = True
        print("⚠️ [STEP4] 总运行时长达到5h30m，强制终止测速任务")
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
    except Exception as e:
        return None, None

async def run_speed_test():
    input_path = os.path.join(SOURCES_DIR, "初处理.txt")
    if not os.path.exists(input_path):
        print("[WARN] 初处理.txt不存在，跳过测速")
        return []
    with open(input_path, "r", encoding="utf-8") as f:
        lines = [l for l in f if clean_text(l)]
    print(f"[STEP4-DEBUG] 测速任务待检测总条数：{len(lines)}")
    sem = asyncio.Semaphore(CONCURRENCY)
    results = []
    valid_list = []
    fail_list = []
    async with aiohttp.ClientSession() as session:
        tasks = [test_single_source(session, sem, l) for l in lines]
        for idx, task in enumerate(asyncio.as_completed(tasks), 1):
            res, line = await task
            if res:
                results.append(res)
                if res["valid"]:
                    valid_list.append(line)
                else:
                    fail_list.append(line)
            if idx % 50 == 0:
                print(f"[STEP4-PROGRESS] 已测速 {idx}/{len(lines)} 有效:{len(valid_list)} 失败:{len(fail_list)}")

    valid_out = os.path.join(SOURCES_DIR, "有效直播源.txt")
    fail_out = os.path.join(SOURCES_DIR, "测速失败.txt")
    with open(valid_out, "w", encoding="utf-8") as f:
        f.write("\n".join(valid_list))
    # ==========【修改：覆盖模式 w，不再a追加】==========
    with open(fail_out, "w", encoding="utf-8") as f:
        f.write("\n".join(fail_list) + "\n")

    print(f"[STEP4-END] 测速结束；有效:{len(valid_list)}条；本轮失败:{len(fail_list)}条；总结果集:{len(results)}")
    return results

# ===================== 6.测速结果处理 =====================
def save_speed_csv(results):
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
    print(f"[STEP5-DEBUG] 测速详情csv已写入log目录，共{len(results)}条记录")

def update_permanent_invalid():
    """读取本轮测速失败，统计连续3轮失败标记永久失效（注意：你需要持久历史，当前仅读取本轮，如需跨轮需要独立历史文件）"""
    fail_path = os.path.join(SOURCES_DIR, "测速失败.txt")
    perm_path = os.path.join(SOURCES_DIR, "永久失效.txt")
    invalid_urls = set()
    if os.path.exists(fail_path):
        with open(fail_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            count = defaultdict(int)
            for l in lines:
                if "," in l:
                    url = l.split(",",1)[1].split("#")[0].strip()
                    count[url] +=1
                    if count[url] >=3:
                        invalid_urls.add(l.strip())
    with open(perm_path, "w", encoding="utf-8") as f:
        f.write("\n".join(invalid_urls))
    print(f"[STEP5-DEBUG] 永久失效.txt写入，数量:{len(invalid_urls)}")

def generate_source_report(source_map, results):
    total = defaultdict(int)
    valid = defaultdict(int)
    for r in results:
        s = r["source"]
        total[s] +=1
        if r["valid"]:
            valid[s] +=1
    report = []
    for url, sources in source_map.items():
        t = total.get(url,0)
        v = valid.get(url,0)
        f = t - v
        rate = f / t if t>0 else 0
        report.append({
            "source_url": url,
            "total": t,
            "valid": v,
            "failed": f,
            "failure_rate": round(rate,4),
            "available_rate": round(1-rate,4)
        })
    report_path = os.path.join(LOG_DIR, "source_quality_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report,f,ensure_ascii=False,indent=2)
    bad_urls = [r["source_url"] for r in sorted(report, key=lambda x:x["failure_rate"], reverse=True)[:3]]
    bad_out = os.path.join(SOURCES_DIR, "失效源地址.txt")
    with open(bad_out, "w", encoding="utf-8") as f:
        f.write("\n".join(bad_urls))
    print(f"[STEP5-DEBUG] 源质量报告已生成，失效率TOP3源：{bad_urls}")

# =====================7.有效直播源处理&分类 =====================
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
                name, url = l.split(",",1)
                url = url.split("#")[0].strip()
                lines.append(f"{clean_text(name)},{url}")
    lines = sorted(list(set(lines)), key=lambda x:x.split(",")[0])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[STEP6-DEBUG] 有效源处理：输入{raw_count}，去重排序后输出{len(lines)}条")
    return lines

def generate_categories(sources):
    for f in os.listdir(CATEGORY_DIR):
        os.remove(os.path.join(CATEGORY_DIR, f))
    import sys
    sys.path.insert(0, CONFIG_DIR)
    from category import classify_source
    cat_map = defaultdict(list)
    for line in sources:
        name, url = line.split(",",1)
        cats = classify_source(name, url)
        for c in cats:
            cat_map[c].append(line)
    epg_map = load_json(os.path.join(CONFIG_DIR, "tvg_id_map.json"))
    epg_cfg = load_json(os.path.join(CONFIG_DIR, "epg.json"))
    epg_url = epg_cfg.get("url","")
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
            if epg_url:
                f.write(f'#EXT-X-URL: {epg_url}\n')
            for line in items:
                n, u = line.split(",",1)
                tvg_id = epg_map.get(n, "")
                br = "1000"
                delay = "200"
                f.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{n}",{n} [{br}kbps {delay}ms]\n{u}\n')
        generated +=1
    print(f"[STEP6-END] 分类完成，生成分类文件数量：{generated}")

# =====================主流程 =====================
async def main():
    print("="*60)
    print(f"IPT任务启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    global start_time
    start_time = time.time()

    source_map = await download_sources()
    if not source_map:
        print("[FATAL]下载阶段无数据，任务直接退出")
        return

    merge_sources()
    process_merged()
    results = await run_speed_test()

    if len(results) > 0:
        save_speed_csv(results)
        update_permanent_invalid()
        generate_source_report(source_map, results)
        valid_sources = process_valid_sources()
        if valid_sources:
            generate_categories(valid_sources)
    else:
        print("[WARN]测速结果为空，跳过统计、分类")

    elapsed = round(time.time()-start_time,2)
    print("="*60)
    print(f"✅全部流程结束，总耗时 {elapsed} 秒")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(main())