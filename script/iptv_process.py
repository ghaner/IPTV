import asyncio
import aiohttp
import json
import re
import os
import time
from datetime import datetime
from collections import defaultdict
from typing import Optional, Tuple
# ===================== 基础配置 =====================
# 文件夹路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
SOURCES_DIR = os.path.join(BASE_DIR, "sources")
CATEGORY_DIR = os.path.join(BASE_DIR, "category")
LOG_DIR = os.path.join(BASE_DIR, "log")
# 持久化失败计数器文件
FAIL_COUNTER_FILE = os.path.join(LOG_DIR, "source_fail_counter.json")
# 创建必要文件夹
for d in [SOURCES_DIR, CATEGORY_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)
# 测速配置
VLC_UA = "VLC/3.0.20 LibVLC/3.0.20"
CONCURRENCY_HTTP = 15       # http请求并发
CONCURRENCY_FFPROBE = 8     # ffprobe子进程并发(CPU密集，低于http)
TIMEOUT = 4                 # 单个http请求超时
SUCCESS_CODES = {200, 201, 202, 206}
MAX_SPEED_TEST_RUN_TIME = 5 * 3600 + 30 * 60  # 仅测速阶段最大运行时长5.5小时
PERMANENT_FAIL_THRESHOLD = 3  # 连续失败N轮标记永久失效
HTTP_READ_BYTES = 2048       # http读取流字节数，优化检测准确性
SKIP_AUDIO_ONLY_STREAM = False # 是否跳过仅音频流；True=仅音频视为无效，False允许纯音频源有效
# 全局变量：控制测速强制终止(TaskGroup接管实际取消，保留兼容旧打印)
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
def get_domain(url: str) -> str:
    """提取域名用于域名限流"""
    try:
        from urllib.parse import urlparse
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
# ---------------- 失败计数器持久化工具 ----------------
def load_fail_counter():
    """加载URL失败轮次计数器 {url: fail_count}"""
    if not os.path.exists(FAIL_COUNTER_FILE):
        return dict()
    try:
        with open(FAIL_COUNTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict()
def save_fail_counter(counter):
    """保存计数器到json"""
    with open(FAIL_COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(counter, f, ensure_ascii=False, indent=2)
def update_fail_counter(results):
    """根据本轮测速结果更新失败计数器
    有效源：重置计数为0；失败源：计数+1
    返回达到阈值的永久失效url集合
    """
    counter = load_fail_counter()
    permanent_invalid_urls = set()
    for item in results:
        url = item["url"]
        is_valid = item["valid"]
        if is_valid:
            counter[url] = 0
        else:
            old = counter.get(url, 0)
            counter[url] = old + 1
            if counter[url] >= PERMANENT_FAIL_THRESHOLD:
                permanent_invalid_urls.add(url)
    save_fail_counter(counter)
    print(f"[COUNTER-DEBUG] 更新失败计数器，达到阈值{PERMANENT_FAIL_THRESHOLD}轮失败URL数量：{len(permanent_invalid_urls)}")
    return permanent_invalid_urls
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
# ===================== 3.汇总直播源初步处理 =====================
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
                    u = line.split(",", 1)[1].split("#")[0].strip()
                    invalid_urls.add(u)
    print(f"[STEP3-DEBUG] 永久失效.txt加载失效URL总数：{len(invalid_urls)}")
    count_perm_invalid = 0   # 被永久失效列表过滤掉的数量
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
        url = url_part.split("#")[0].strip()
        comment = "#" + url_part.split("#")[1] if "#" in url_part else ""
        if not name or not url:
            count_bad_line += 1
            continue
        if url in invalid_urls:
            count_perm_invalid += 1
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
    print(f"[STEP3-FILTER] 永久失效过滤：{count_perm_invalid} 条；URL去重过滤：{count_dup_url} 条；坏行丢弃：{count_bad_line} 条")
    input_total = len(raw_lines)
    sum_filtered = count_perm_invalid + count_dup_url + count_bad_line
    output_total = len(lines)
    calc_total = sum_filtered + output_total
    if input_total == calc_total:
        print(f"[STEP3-CHECK] ✅计数校验通过：输入{input_total} = 过滤{sum_filtered} + 输出{output_total}")
    else:
        print(f"[STEP3-CHECK] ⚠️计数校验不匹配！输入:{input_total} 计算合计:{calc_total}，请检查计数逻辑")
    print(f"[STEP3-END] 初处理完成，初处理.txt剩余 {len(lines)} 条")
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
# ===================== 5.测速结果处理｜【方案A：累积持久黑名单】 =====================
def update_permanent_invalid(permanent_invalid_urls, all_result_lines):
    out_path = os.path.join(SOURCES_DIR, "永久失效.txt")
    old_lines = []
    old_url_set = set()
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            for l in f:
                ll = l.strip()
                if not ll or "," not in ll:
                    continue
                old_lines.append(ll)
                u = ll.split(",", 1)[1].split("#")[0].strip()
                old_url_set.add(u)
    new_collect = []
    for line in all_result_lines:
        if "," not in line:
            continue
        url = line.split(",", 1)[1].split("#")[0].strip()
        if url in permanent_invalid_urls and url not in old_url_set:
            new_collect.append(line.strip())
    total_lines = old_lines + new_collect
    final_map = {}
    for l in total_lines:
        u = l.split(",", 1)[1].split("#")[0].strip()
        final_map[u] = l
    final_lines = list(final_map.values())
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(final_lines))
    print(f"[STEP5-END] 永久失效.txt更新完成；历史载入{len(old_lines)}条；本轮新增{len(new_collect)}条；合并后总黑名单:{len(final_lines)}条")
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

    # 新增：将有效直播源转为m3u格式输出到sources文件夹
    m3u_out_path = os.path.join(SOURCES_DIR, "有效直播源.m3u")
    epg_cfg = load_json(os.path.join(CONFIG_DIR, "epg.json"))
    epg_url = epg_cfg.get("epg_url", "")
    epg_map = load_json(os.path.join(CONFIG_DIR, "tvg_id_map.json"))
    with open(m3u_out_path, "w", encoding="utf-8") as fm:
        fm.write("#EXTM3U\n")
        if epg_url:
            fm.write(f'#EXT-X-URL: {epg_url}\n')
        for line in lines:
            n, u = line.split(",", 1)
            tvg_id = epg_map.get(n, "")
            fm.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{n}",{n}\n{u}\n')
    print(f"[STEP6-INFO] 有效直播源.m3u已生成，路径:{m3u_out_path}")

    print(f"[STEP6-END] 有效源处理完成：输入{raw_count}，去重排序后输出{len(lines)}条")
    return lines
def generate_categories(sources):
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
    epg_cfg = load_json(os.path.join(CONFIG_DIR, "epg.json"))
    epg_url = epg_cfg.get("epg_url", "")
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
                n, u = line.split(",", 1)
                tvg_id = epg_map.get(n, "")
                f.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{n}",{n}\n{u}\n')
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
    process_merged()
    results = await run_speed_test()
    if len(results) > 0:
        perm_invalid_url_set = update_fail_counter(results)
        all_result_lines = []
        fail_file = os.path.join(SOURCES_DIR, "测速失败.txt")
        valid_file = os.path.join(SOURCES_DIR, "有效直播源.txt")
        for fp in [fail_file, valid_file]:
            if os.path.exists(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    all_result_lines.extend([clean_text(l) for l in f if clean_text(l)])
        update_permanent_invalid(perm_invalid_url_set, all_result_lines)
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