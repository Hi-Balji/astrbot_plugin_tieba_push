"""贴吧页面解析(纯函数,可独立测试)。

数据源:贴吧老版移动端页面(免登录可访问)
- 吧列表: https://tieba.baidu.com/mo/q/g---9F9F9F8B/m?kw={kw}&lp=6024
- 帖子详情: https://tieba.baidu.com/mo/q/g---9F9F9F8B/m?kz={tid}&lp=6024
"""

import html as html_lib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

LIST_URL = "https://tieba.baidu.com/mo/q/g---9F9F9F8B/m?kw={kw}&lp=6024"
DETAIL_URL = "https://tieba.baidu.com/mo/q/g---9F9F9F8B/m?kz={tid}&lp=6024"

_LI_RE = re.compile(
    r'<li class="tl_(shadow|top)[^"]*"[^>]*data-tid="(\d+)"[^>]*>(.*?)</li>', re.S
)
_TITLE_RE = re.compile(
    r'class="ti_title"[^>]*>\s*(?:<span class="ti_title_icon[^>]*>\s*[^<]*</span>\s*)?<span>(.*?)</span>', re.S
)
_REPLY_RE = re.compile(r'btn_reply[^>]*>\s*<span class="btn_icon">\s*(\d+)\s*</span>', re.S)
_AUTHOR_RE = re.compile(r'class="ti_author[^"]*"[^>]*>\s*([^<]+?)\s*</span>')

_MAIN_RE = re.compile(
    r'class="content"\s+lz="1">(.*?)(?=class="list_item post_list_item|class="comment_area|</body)', re.S
)
_IMG_RE = re.compile(r'data-url=["\']([^"\']*tiebapic\.baidu\.com/forum/[^"\']*)["\']')
_ORIG_IMG_RE = re.compile(r'src=(http[^\'"&]+?/forum/pic/item/[^\'"&]+?\.jpe?g)', re.I)
_VIDEO_RE = re.compile(
    r'(https?://tb-video\.bdstatic\.com/tieba-smallvideo-transcode-cae/'
    r'[^"\'\s<>]+?\.mp4\?[^"\'\s<>]+)', re.I
)
_STATIC_JUNK = ("bdstatic.com", "bcebos.com/mis", "/editor/images/", "/tb/mobile/", "logo", "icon")


@dataclass
class TiebaPost:
    tid: str
    title: str = ""
    reply_num: int = 0
    author: str = ""
    text: str = ""
    images: list = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://tieba.baidu.com/p/{self.tid}"


def parse_list(html: str) -> list:
    """解析吧列表页,返回非置顶帖子(按页面顺序,即热门顺序),含 tid/标题/回复数/作者。"""
    posts = []
    for kind, tid, body in _LI_RE.findall(html):
        if kind == "top":  # 置顶帖跳过
            continue
        m = _TITLE_RE.search(body)
        title = html_lib.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""
        m = _REPLY_RE.search(body)
        reply = int(m.group(1)) if m else 0
        m = _AUTHOR_RE.search(body)
        author = html_lib.unescape(m.group(1).strip()) if m else ""
        if tid and title:
            posts.append(TiebaPost(tid=tid, title=title, reply_num=reply, author=author))
    # 去重(同一tid可能出现多次)
    seen = set()
    unique = []
    for p in posts:
        if p.tid not in seen:
            seen.add(p.tid)
            unique.append(p)
    return unique


def _upgrade_image_url(url: str) -> str:
    """保留原始带签名代理链接(下载层会优先尝试imgsrc高清原图,失败再回退此链接)。"""
    return url


def _strip_html(fragment: str) -> str:
    frag = re.sub(r"<script[^>]*>.*?</script>", " ", fragment, flags=re.S)
    frag = re.sub(r"<style[^>]*>.*?</style>", " ", fragment, flags=re.S)
    frag = re.sub(r"<br\s*/?>", "\n", frag, flags=re.I)
    frag = re.sub(r"<[^>]+>", " ", frag)
    frag = html_lib.unescape(frag)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in frag.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # 精确清除运营文案(逐个删除,避免把正文一起截掉)
    for junk in ("看高清大图，打开贴吧App", "更多高清视频，打开贴吧App",
                 "更多精彩内容，打开贴吧App", "操作 收藏 回复 举报", "Tail.Tail."):
        text = text.replace(junk, "")
    return text.strip()


def parse_detail(html: str) -> tuple:
    """解析帖子详情页主楼,返回 (正文文字, [图片URL], [视频URL])。

    视频为带签名参数的转码直链,下载时必须携带 Referer: https://tieba.baidu.com/
    """
    m = _MAIN_RE.search(html)
    block = m.group(1) if m else ""
    # 主楼尾部常挂楼层"热评预览"组件(<li tid=...>,通常无闭合标签):
    # 主楼正文与图片均在其前部,直接在第一个 <li 处截断
    li_pos = block.find("<li")
    if li_pos > 0:
        block = block[:li_pos]
    # 图片:pb_img_item 的 data-url(还原原图),按出现顺序去重
    imgs = []
    for u in _IMG_RE.findall(block):
        u = u.replace("&amp;", "&")
        if any(j in u for j in _STATIC_JUNK):
            continue
        u2 = _upgrade_image_url(u)
        if u2 not in imgs:
            imgs.append(u2)
    # 视频:转码直链(含完整签名参数),去重
    videos = []
    for u in _VIDEO_RE.findall(block):
        u = u.replace("&amp;", "&")
        if u not in videos:
            videos.append(u)
    text = _strip_html(block)
    # 残留的楼层计数等纯数字无意义,丢弃
    if text.isdigit() and len(text) <= 4:
        text = ""
    return text, imgs, videos
