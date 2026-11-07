"""Offline display-only site names. Raw hosts remain the aggregation keys."""
from collections import Counter


# Chinese names stay in the HTML legend; ASCII names also work in server PNGs
# without requiring a CJK font on a headless Linux deployment.
KNOWN = {
    "github.com": ("GitHub", "GitHub"),
    "gitlab.com": ("GitLab", "GitLab"),
    "stackoverflow.com": ("Stack Overflow", "Stack Overflow"),
    "notion.so": ("Notion", "Notion"),
    "figma.com": ("Figma", "Figma"),
    "coursera.org": ("Coursera", "Coursera"),
    "wikipedia.org": ("Wikipedia", "Wikipedia"),
    "docs.google.com": ("Google 文档", "Google Docs"),
    "docs.qq.com": ("腾讯文档", "Tencent Docs"),
    "docs.python.org": ("Python 文档", "Python Docs"),
    "21st.dev": ("21st.dev", "21st.dev"),
    "youtube.com": ("YouTube", "YouTube"),
    "bilibili.com": ("哔哩哔哩", "Bilibili"),
    "douyin.com": ("抖音", "Douyin"),
    "tiktok.com": ("TikTok", "TikTok"),
    "netflix.com": ("Netflix", "Netflix"),
    "twitch.tv": ("Twitch", "Twitch"),
    "iqiyi.com": ("爱奇艺", "iQIYI"),
    "youku.com": ("优酷", "Youku"),
    "zhihu.com": ("知乎", "Zhihu"),
    "weibo.com": ("微博", "Weibo"),
    "x.com": ("X", "X"),
    "twitter.com": ("X / Twitter", "Twitter"),
    "facebook.com": ("Facebook", "Facebook"),
    "instagram.com": ("Instagram", "Instagram"),
    "reddit.com": ("Reddit", "Reddit"),
    "threads.net": ("Threads", "Threads"),
    "bbc.com": ("BBC", "BBC"),
    "cnn.com": ("CNN", "CNN"),
    "nytimes.com": ("The New York Times", "NY Times"),
    "reuters.com": ("Reuters", "Reuters"),
    "thepaper.cn": ("澎湃新闻", "The Paper"),
    "news.qq.com": ("腾讯新闻", "Tencent News"),
    "news.google.com": ("Google 新闻", "Google News"),
    "taobao.com": ("淘宝", "Taobao"),
    "tmall.com": ("天猫", "Tmall"),
    "jd.com": ("京东", "JD.com"),
    "amazon.com": ("Amazon", "Amazon"),
    "ebay.com": ("eBay", "eBay"),
    "chatgpt.com": ("ChatGPT", "ChatGPT"),
    "openai.com": ("OpenAI", "OpenAI"),
    "google.com": ("Google", "Google"),
    "mail.google.com": ("Gmail", "Gmail"),
    "mail.proton.me": ("Proton Mail", "Proton Mail"),
    "bing.com": ("Bing", "Bing"),
    "baidu.com": ("百度", "Baidu"),
    "nowcoder.com": ("牛客", "Nowcoder"),
}


def _known(host):
    lookup = str(host).lower().rstrip(".")
    if lookup.startswith("www."):
        lookup = lookup[4:]
    return KNOWN.get(lookup)


def names_for_hosts(hosts, *, chart=False):
    """Only rename known exact hosts (plus www); disambiguate duplicate aliases."""
    unique = list(dict.fromkeys(str(host) for host in hosts))
    index = 1 if chart else 0
    names = {host: (_known(host) or (host, host))[index] for host in unique}
    repeated = Counter(names.values())
    return {host: (host if chart else f"{name} ({host})") if repeated[name] > 1 else name
            for host, name in names.items()}


def names_for_rows(rows, *, chart=False):
    totals = {}
    for row in rows:
        host = str(row["host"])
        totals[host] = totals.get(host, 0.0) + float(row["seconds"])
    return names_for_hosts(sorted(totals, key=lambda host: (-totals[host], host)), chart=chart)
