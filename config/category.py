import json
import os
from typing import List, Set
import functools

BASE_CONFIG = os.path.dirname(os.path.abspath(__file__))


def _load_json_keys(filename: str) -> List[str]:
    """
    内部工具函数：读取config下json文件
    ✅兼容两种格式：
        1. dict对象 { "key1":true, "key2":true } → 返回key列表
        2. list数组 [ "item1", "item2" ] → 直接返回数组
    文件缺失 / JSON解析错误 → 返回空列表，不抛异常
    """
    file_path = os.path.join(BASE_CONFIG, filename)
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return list(data.keys())
        else:
            return []
    except (json.JSONDecodeError, IOError, OSError):
        return []


@functools.lru_cache(maxsize=1)
def load_all_json_data() -> dict:
    """一次性加载所有外部json，全局缓存，只做一次IO，同时预生成小写版本"""
    raw = {
        "base_categories": _load_json_keys("base_categories.json"),
        "province": _load_json_keys("province.json"),
        "city": _load_json_keys("city.json"),
        "singer": _load_json_keys("singer.json"),
        "actor": _load_json_keys("actor.json"),
        "teleplay": _load_json_keys("teleplay.json"),
        "sceniczone": _load_json_keys("sceniczone.json"),
        "documentary": _load_json_keys("documentary.json"),
        "taiwan": _load_json_keys("taiwan.json"),
        "show": _load_json_keys("show.json"),
        "mtcommentary": _load_json_keys("MTcommentary.json"),
    }
    # 预计算小写关键词，用于忽略大小写匹配
    lower = {k: [item.lower() for item in v] for k, v in raw.items()}
    return {"raw": raw, "lower": lower}


def get_all_category_names() -> List[str]:
    """动态获取全部完整分类集合"""
    data = load_all_json_data()
    raw = data["raw"]
    return (
        raw["base_categories"]
        + raw["province"]
        + raw["city"]
        + raw["singer"]
        + raw["actor"]
        + raw["teleplay"]
        + raw["sceniczone"]
        + raw["documentary"]
        + raw["taiwan"]
    )


def get_channel_categories(name: str, link: str) -> List[str]:
    """
    输入频道name、link(链接)，返回该频道归属的所有分类列表
    全部逐条归类，不要遗漏；一个频道可属于多个分类；无任何命中则归入【未分类】
    ✅匹配逻辑忽略大小写，输出分类名称保持原始大小写
    :param name: 频道名称
    :param link: 直播源链接
    :return: 分类字符串列表
    """
    name_raw = name.strip()
    link_raw = link.strip()
    name_lower = name_raw.lower()
    link_lower = link_raw.lower()
    result_cats: Set[str] = set()
    cache_data = load_all_json_data()
    raw_data = cache_data["raw"]
    lower_data = cache_data["lower"]

    #定义局部变量
    base_categories = raw_data["base_categories"]
    PROVINCE_LIST = raw_data["province"]
    PROVINCE_LOWER = lower_data["province"]
    CITY_LIST = raw_data["city"]
    CITY_LOWER = lower_data["city"]
    SINGER_NAMES = raw_data["singer"]
    SINGER_LOWER = lower_data["singer"]
    ACTOR_NAMES = raw_data["actor"]
    ACTOR_LOWER = lower_data["actor"]
    TV_DRAMA_NAMES = raw_data["teleplay"]
    TV_DRAMA_LOWER = lower_data["teleplay"]
    TV_DRAMA_LINKS = raw_data["teleplay"]
    TV_DRAMA_LOWER = lower_data["teleplay"]
    SCENIC_ZONE_NAMES = raw_data["sceniczone"]
    SCENIC_ZONE_LOWER = lower_data["sceniczone"]
    DOCUMENTARY_NAMES = raw_data["documentary"]
    DOCUMENTARY_LOWER = lower_data["documentary"]
    DOCUMENTARY_LINKS = raw_data["documentary"]
    DOCUMENTARY_LOWER = lower_data["documentary"]
    TAIWAN_NAMES = raw_data["taiwan"]
    TAIWAN_LOWER = lower_data["taiwan"]
    SHOW_LINKS = raw_data["show"]
    SHOW_LOWER = lower_data["show"]

    MTCOMMENTARY_LINKS = raw_data["mtcommentary"]
    MTCOMMENTARY_LOWER = lower_data["mtcommentary"]
    # -------- link链接匹配平台分类（忽略大小写） --------
    if "/huya" in link_lower:
        result_cats.add("huya")
    if "/douyu" in link_lower:
        result_cats.add("douyu")
    if "/bilibili" in link_lower:
        result_cats.add("bilibili")
    if "/yy/" in link_lower:
        result_cats.add("yy")
    # 特殊链接片段归入影视解说
    for show_raw, show_low in zip(SHOW_LINKS, SHOW_LOWER):
        if show_low in link_lower:
            result_cats.add("综艺")
    for mtcommentary_raw, mtcommentary_low in zip(MTCOMMENTARY_LINKS, MTCOMMENTARY_LOWER):
        if mtcommentary_low in link_lower:
            result_cats.add("影视解说")
    for DOCUMENTARY_raw, DOCUMENTARY_low in zip(DOCUMENTARY_LINKS, DOCUMENTARY_LOWER):
        if DOCUMENTARY_low in link_lower:
            result_cats.add("纪录片")            
    for TV_DRAMA_raw, TV_DRAMA_low in zip(TV_DRAMA_LINKS, TV_DRAMA_LOWER):
        if TV_DRAMA_low in link_lower:
            result_cats.add("电视剧")            
    # -------- name名称匹配基础分类名称（忽略大小写） --------
    for cat in base_categories:
        if cat.lower() in name_lower:
            result_cats.add(cat)
    # name关键词规则：说电影 / 看电影 / 侃电影 / 讲电影 / 撩电影 →电影 + 影视解说
    movie_comment_keywords = ["说电影", "看电影", "侃电影", "讲电影", "撩电影"]
    for kw in movie_comment_keywords:
        if kw in name_lower:
            result_cats.add("电影")
            result_cats.add("影视解说")
    # name中有“DJ” →音乐
    if "dj" in name_lower:
        result_cats.add("音乐")
    # name中有“风云” →CCTV
    if "风云" in name_lower:
        result_cats.add("CCTV")
    # name中有“足球”、“高尔夫”、“网球” →体育
    for sport_kw in ["足球", "高尔夫", "网球"]:
        if sport_kw in name_lower:
            result_cats.add("体育")
    # name中有“凤凰” →凤凰卫视
    if "凤凰" in name_lower:
        result_cats.add("凤凰卫视")
    # name中有“风景”、“景区”、“泰山” →景区
    for scenic_kw in ["风景", "景区", "泰山"]:
        if scenic_kw in name_lower:
            result_cats.add("景区")
    for sz_raw, sz_low in zip(SCENIC_ZONE_NAMES, SCENIC_ZONE_LOWER):
        if sz_low in name_lower:
            result_cats.add("景区")
    # name中有“相声”、“小品” →相声小品
    if "相声" in name_lower or "小品" in name_lower:
        result_cats.add("相声小品")
    # name中有CHC →CHC
    if "chc" in name_lower:
        result_cats.add("CHC")
    # name中有TVB、翡翠台 →TVB
    if "tvb" in name_lower or "翡翠台" in name_lower:
        result_cats.add("TVB")
    # name中有财经 →财经
    if "财经" in name_lower:
        result_cats.add("财经")
    # name包含电视剧名称(teleplay.json) →电视剧分类
    for drama_raw, drama_low in zip(TV_DRAMA_NAMES, TV_DRAMA_LOWER):
        if drama_low in name_lower:
            result_cats.add("电视剧")
    # name包含纪录片名称(documentary.json) →纪录片分类
    for doc_raw, doc_low in zip(DOCUMENTARY_NAMES, DOCUMENTARY_LOWER):
        if doc_low in name_lower:
            result_cats.add("纪录片")
    for tw_raw, tw_low in zip(TAIWAN_NAMES, TAIWAN_LOWER):
        if tw_low in name_lower:
            result_cats.add("台湾")
    # name包含歌手姓名(singer.json) →歌手分类
    for singer_raw, singer_low in zip(SINGER_NAMES, SINGER_LOWER):
        if singer_low in name_lower:
            result_cats.add("歌手")
    # name包含演员姓名(actor.json) →演员分类
    for actor_raw, actor_low in zip(ACTOR_NAMES, ACTOR_LOWER):
        if actor_low in name_lower:
            result_cats.add("演员")
    # -------- 省级、市级行政区划名称匹配 --------
    for prov_raw, prov_low in zip(PROVINCE_LIST, PROVINCE_LOWER):
        if prov_low in name_lower:
            result_cats.add(prov_raw)
    for city_raw, city_low in zip(CITY_LIST, CITY_LOWER):
        if city_low in name_lower:
            result_cats.add(city_raw)
    # 全部规则均未命中 →归入未分类
    if not result_cats:
        result_cats.add("未分类")
    return list(result_cats)

# 如果运行期间修改了json文件，调用此函数清空缓存重新加载
# usage: load_all_json_data.cache_clear()