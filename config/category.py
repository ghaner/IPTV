import os
import json

# 加载JSON分类数据
BASE = os.path.dirname(os.path.abspath(__file__))

province = set(json.load(open(os.path.join(BASE, "province.json"), encoding="utf-8")))
city = set(json.load(open(os.path.join(BASE, "city.json"), encoding="utf-8")))
singer = set(json.load(open(os.path.join(BASE, "singer.json"), encoding="utf-8")))
actor = set(json.load(open(os.path.join(BASE, "actor.json"), encoding="utf-8")))
teleplay = set(json.load(open(os.path.join(BASE, "teleplay.json"), encoding="utf-8")))
scenic = set(json.load(open(os.path.join(BASE, "sceniczone.json"), encoding="utf-8")))
documentary = set(json.load(open(os.path.join(BASE, "documentary.json"), encoding="utf-8")))

# 特殊排除项
EXCLUDE = {"我的体育老师"}

def classify_source(name, url):
    """分类主函数"""
    name = name.lower()
    url = url.lower()
    categories = set()

    # 排除
    if name in EXCLUDE:
        categories.add("未分类")
        return list(categories)

    # ---------------- 基础分类 ----------------
    if "cctv" in name or "风云" in name:
        categories.add("CCTV")
    if "cgtn" in name:
        categories.add("CGTN")
    if "cetv" in name:
        categories.add("CETV")
    if "卫视" in name:
        categories.add("卫视")
    if "凤凰" in name:
        categories.add("凤凰卫视")
    if "chc" in name:
        categories.add("CHC")
    if "tvb" in name or "翡翠台" in name:
        categories.add("TVB")
    if "埋堆堆" in name:
        categories.add("埋堆堆")

    # ---------------- 内容分类 ----------------
    if "体育" in name and "我的体育老师" not in name:
        categories.add("体育")
    if any(k in name for k in ["足球", "高尔夫", "网球"]):
        categories.add("体育")
    if "财经" in name:
        categories.add("财经")
    if "少儿" in name:
        categories.add("少儿")
    if "电影" in name:
        categories.add("电影")
    if "电视剧" in name:
        categories.add("电视剧")
    if "dj" in name:
        categories.add("音乐")
    if "演唱会" in name:
        categories.add("演唱会")
    if "相声" in name or "小品" in name:
        categories.add("相声小品")
    if "春晚" in name:
        categories.add("春晚")
    if "录像" in name:
        categories.add("录像")

    # ---------------- 解说 ----------------
    if any(k in name for k in ["说电影", "看电影", "侃电影", "讲电影", "撩电影"]):
        categories.add("电影")
        categories.add("影视解说")

    # ---------------- 景区 ----------------
    if any(k in name for k in scenic) or "风景" in name or "景区" in name or "泰山" in name:
        categories.add("景区")

    # ---------------- 平台链接 ----------------
    if "bilibili" in url:
        categories.add("bilibili")
    if "douyu" in url:
        categories.add("douyu")
    if "huya" in url:
        categories.add("huya")
    if "yy" in url:
        categories.add("yy")

    # 特殊直播间 = 影视解说
    special = ["huya/11774959", "huya/29982676", "douyu/9639225"]
    if any(s in url for s in special):
        categories.add("影视解说")

    # ---------------- JSON 精准匹配 ----------------
    if any(p in name for p in province):
        categories.add("地方台")
    if any(c in name for c in city):
        categories.add("地方台")
    if any(s in name for s in singer):
        categories.add("歌手")
    if any(a in name for a in actor):
        categories.add("演员")
    if any(t in name for t in teleplay):
        categories.add("电视剧")
    if any(d in name for d in documentary):
        categories.add("纪录片")

    # ---------------- 未分类 ----------------
    if not categories:
        categories.add("未分类")

    return list(categories)