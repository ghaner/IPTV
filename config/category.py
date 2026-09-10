import json
import os
from typing import List, Set
BASE_CONFIG = os.path.dirname(os.path.abspath(__file__))
# 通用基础分类名称
BASE_CATEGORIES = [
    "CCTV", "CGTN", "CETV", "卫视", "凤凰卫视", "地方台",
    "体育", "财经", "少儿", "电影", "电视剧", "影视解说", "音乐",
    "演唱会", "相声小品", "歌手", "演员", "春晚", "纪录片", "huya", "douyu",
    "bilibili", "yy", "CHC", "TVB", "埋堆堆", "录像", "景区", "未分类"
]
# 特殊url片段匹配影视解说规则
SPECIAL_VIDEO_COMMENT_URLS = {
    "huya/11774959",
    "huya/29982676",
    "douyu/9639225"
}
def _load_json_keys(filename: str) -> List[str]:
    """
    内部工具函数：读取config下json文件的key列表
    文件缺失 / JSON解析错误 → 返回空列表，不抛异常
    """
    file_path = os.path.join(BASE_CONFIG, filename)
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return list(data.keys())
    except (json.JSONDecodeError, IOError, OSError):
        return []
def get_all_category_names() -> List[str]:
    """动态获取全部完整分类集合，替代原来顶层ALL_CATEGORY_NAMES常量"""
    province_list = _load_json_keys("province.json")
    city_list = _load_json_keys("city.json")
    singer_names = _load_json_keys("singer.json")
    actor_names = _load_json_keys("actor.json")
    teleplay_names = _load_json_keys("teleplay.json")
    sceniczone_names = _load_json_keys("sceniczone.json")
    documentary_names = _load_json_keys("documentary.json")
    return (
        BASE_CATEGORIES
        + province_list
        + city_list
        + singer_names
        + actor_names
        + teleplay_names
        + sceniczone_names
        + documentary_names
    )
def get_channel_categories(name: str, link: str) -> List[str]:
    """
    输入频道name、link(链接)，返回该频道归属的所有分类列表
    全部逐条归类，不要遗漏；一个频道可属于多个分类；无任何命中则归入【未分类】
    :param name: 频道名称
    :param link: 直播源链接
    :return: 分类字符串列表
    """
    name_raw = name.strip()
    link_raw = link.strip()
    result_cats: Set[str] = set()
    # 每次调用加载json数据（有容错，损坏文件返回空）
    PROVINCE_LIST = _load_json_keys("province.json")
    CITY_LIST = _load_json_keys("city.json")
    SINGER_NAMES = _load_json_keys("singer.json")
    ACTOR_NAMES = _load_json_keys("actor.json")
    TV_DRAMA_NAMES = _load_json_keys("teleplay.json")
    SCENIC_ZONE_NAMES = _load_json_keys("sceniczone.json")
    DOCUMENTARY_NAMES = _load_json_keys("documentary.json")
    # -------- link链接匹配平台分类 --------
    if "/huya" in link_raw:
        result_cats.add("huya")
    if "/douyu" in link_raw:
        result_cats.add("douyu")
    if "/bilibili" in link_raw:
        result_cats.add("bilibili")
    if "/yy/" in link_raw:
        result_cats.add("yy")
    # 特殊链接片段归入影视解说
    for special_frag in SPECIAL_VIDEO_COMMENT_URLS:
        if special_frag in link_raw:
            result_cats.add("影视解说")
    # -------- name名称匹配基础分类名称（包含相同字符即归类） --------
    for cat in BASE_CATEGORIES:
        if cat in name_raw:
            result_cats.add(cat)
    # name关键词规则：说电影 / 看电影 / 侃电影 / 讲电影 / 撩电影 →电影 + 影视解说
    movie_comment_keywords = ["说电影", "看电影", "侃电影", "讲电影", "撩电影"]
    for kw in movie_comment_keywords:
        if kw in name_raw:
            result_cats.add("电影")
            result_cats.add("影视解说")
    # name中有“DJ” →音乐
    if "DJ" in name_raw:
        result_cats.add("音乐")
    # name中有“风云” →CCTV
    if "风云" in name_raw:
        result_cats.add("CCTV")
    # name中有“足球”、“高尔夫”、“网球” →体育
    for sport_kw in ["足球", "高尔夫", "网球"]:
        if sport_kw in name_raw:
            result_cats.add("体育")
    # name中有“凤凰” →凤凰卫视
    if "凤凰" in name_raw:
        result_cats.add("凤凰卫视")
    # name中有“风景”、“景区”、“泰山” 以及sceniczone.json内景区名称 →景区
    for scenic_kw in ["风景", "景区", "泰山"]:
        if scenic_kw in name_raw:
            result_cats.add("景区")
    for sz_name in SCENIC_ZONE_NAMES:
        if sz_name in name_raw:
            result_cats.add("景区")
    # name中有“相声”、“小品” →相声小品
    if "相声" in name_raw or "小品" in name_raw:
        result_cats.add("相声小品")
    # name中有CHC →CHC
    if "CHC" in name_raw:
        result_cats.add("CHC")
    # name中有TVB、翡翠台 →TVB
    if "TVB" in name_raw or "翡翠台" in name_raw:
        result_cats.add("TVB")
    # name中有财经 →财经
    if "财经" in name_raw:
        result_cats.add("财经")
    # name包含电视剧名称(teleplay.json) →电视剧分类
    for drama_name in TV_DRAMA_NAMES:
        if drama_name in name_raw:
            result_cats.add("电视剧")
    # name包含纪录片名称(documentary.json) →纪录片分类
    for doc_name in DOCUMENTARY_NAMES:
        if doc_name in name_raw:
            result_cats.add("纪录片")
    # name包含歌手姓名(singer.json) →歌手分类
    for singer in SINGER_NAMES:
        if singer in name_raw:
            result_cats.add("歌手")
    # name包含演员姓名(actor.json) →演员分类
    for actor in ACTOR_NAMES:
        if actor in name_raw:
            result_cats.add("演员")
    # -------- 省级、市级行政区划名称匹配 --------
    for prov in PROVINCE_LIST:
        if prov in name_raw:
            result_cats.add(prov)
    for city in CITY_LIST:
        if city in name_raw:
            result_cats.add(city)
    # 全部规则均未命中 →归入未分类
    if not result_cats:
        result_cats.add("未分类")
    return list(result_cats)