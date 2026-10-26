"""Offline, host-only website categories. Unknown hosts stay in 其他."""

CATEGORIES = (
    ("社交媒体", "#A9C6E8"),
    ("视频娱乐", "#A7D9C3"),
    ("工作学习", "#F1C49B"),
    ("新闻资讯", "#E9ADB9"),
    ("购物", "#C9B8E6"),
    ("工具类", "#A8D2D8"),
    ("其他", "#CAD2DE"),
)

_RULES = (
    ("社交媒体", ("x.com", "twitter.com", "facebook.com", "instagram.com", "reddit.com",
                  "weibo.com", "zhihu.com", "threads.net")),
    ("视频娱乐", ("youtube.com", "youtu.be", "bilibili.com", "douyin.com", "tiktok.com",
                  "netflix.com", "twitch.tv", "iqiyi.com", "youku.com")),
    ("工作学习", ("github.com", "gitlab.com", "stackoverflow.com", "notion.so",
                  "figma.com", "coursera.org", "wikipedia.org", "docs.google.com")),
    ("新闻资讯", ("bbc.com", "cnn.com", "nytimes.com", "reuters.com", "thepaper.cn",
                  "news.google.com", "news.qq.com")),
    ("购物", ("taobao.com", "tmall.com", "jd.com", "amazon.com", "ebay.com")),
    ("工具类", ("chatgpt.com", "openai.com", "google.com", "bing.com", "baidu.com",
                "offershow.cn", "nowcoder.com")),
)


def classify(host):
    host = str(host or "").lower().rstrip(".").split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    for name, suffixes in _RULES:
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return name
    return "其他"


def totals(rows, seconds_key="total_seconds"):
    result = {name: 0.0 for name, _ in CATEGORIES}
    for row in rows:
        seconds = max(0.0, float(row.get(seconds_key, 0)))
        result[classify(row.get("host"))] += seconds
    return {name: seconds for name, seconds in result.items() if seconds > 0}
