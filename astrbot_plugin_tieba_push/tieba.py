"""贴吧抓取与解析。

吧首页 HTML -> 帖子列表(tid/作者/回复数,过滤置顶)
帖子详情 HTML -> 标题/首楼文字/首楼图片URL列表
解析与抓取解耦,便于离线测试。
"""

import html as html_lib
import json
import re
from dataclasses import dataclass, field

UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Mobile Safari/537.36")


class TiebaError(RuntimeError):
    pass


@dataclass
class ThreadBrief:
    tid: str
    author: str = ""
    reply_num: int = 0


@dataclass
class ThreadContent:
    tid: str
    title: str
    text: str
    image_urls: list = field(default_factory=list)


def _headers(cookie: str = "") -> dict:
    h = {
        "User-Agent": UA,
        "Referer": "https://tieba.baidu.com",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    c = (cookie or "").strip()
    if c:
        # 允许只填 BDUSS 的值(不含"="时自动包装),也可以填整段Cookie
        if "=" not in c and len(c) > 20:
            c = f"BDUSS={c}"
        h["Cookie"] = c
    return h


# ---------------- 吧首页:帖子列表 ----------------

def parse_thread_list(html_text: str) -> list:
    """从吧首页 HTML 提取非置顶帖子列表(按页面顺序)。"""
    threads = []
    for m in re.finditer(r"<li([^>]*j_thread_list[^>]*)>", html_text):
        tag = m.group(1)
        if "thread_top" in tag:  # 置顶帖跳过
            continue
        fm = re.search(r"data-field='([^']+)'", tag)
        if not fm:
            continue
        try:
            info = json.loads(html_lib.unescape(fm.group(1)))
        except ValueError:
            continue
        tid = str(info.get("id") or "").strip()
        if not tid:
            continue
        try:
            reply_num = int(info.get("reply_num") or 0)
        except (TypeError, ValueError):
            reply_num = 0
        threads.append(ThreadBrief(tid=tid, author=info.get("author_name") or "",
                                    reply_num=reply_num))
    return threads


def pick_hot(threads: list, count: int) -> list:
    """取热门帖:按回复数降序取前 count 个。"""
    ranked = sorted(threads, key=lambda t: t.reply_num, reverse=True)
    return ranked[:count]


# ---------------- 帖子详情 ----------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


def _clean_text(raw: str) -> str:
    text = html_lib.unescape(_TAG_RE.sub("", raw))
    lines = [ln.strip() for ln in text.splitlines()]
    return _WS_RE.sub("\n\n", "\n".join(lines)).strip()


def parse_thread_page(html_text: str, tid: str) -> ThreadContent:
    """解析帖子详情页:标题 + 首楼文字 + 首楼图片。"""
    title = ""
    m = re.search(r'class="core_title_txt[^"]*"[^>]*title="([^"]+)"', html_text)
    if m:
        title = html_lib.unescape(m.group(1))
    if not title:
        m = re.search(r"<title>(.*?)(?:_百度贴吧)?</title>", html_text, re.S)
        if m:
            title = _clean_text(m.group(1))

    posts = re.findall(r'id="post_content_\d+"[^>]*>(.*?)</div>', html_text, re.S)
    first_floor = posts[0] if posts else ""
    text = _clean_text(first_floor)

    image_urls = []
    for im in re.finditer(r"<img([^>]*BDE_Image[^>]*)>", first_floor):
        sm = re.search(r'src="(https?://[^"]+)"', im.group(1))
        if sm:
            image_urls.append(sm.group(1))

    if not title and not text and not image_urls:
        raise TiebaError("帖子解析失败(页面结构变化或被反爬拦截)")
    return ThreadContent(tid=tid, title=title or "(无标题)", text=text,
                         image_urls=image_urls)


# ---------------- 抓取(aiohttp) ----------------

def forum_url(kw: str) -> str:
    from urllib.parse import quote
    return f"https://tieba.baidu.com/f?kw={quote(kw)}&ie=utf-8"


async def fetch_html(session, url: str, cookie: str = "", timeout: int = 20) -> str:
    import aiohttp
    async with session.get(url, headers=_headers(cookie),
                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            raise TiebaError(f"页面请求失败 HTTP {resp.status}: {url}")
        return await resp.text(errors="ignore")


def _img_ok(data: bytes) -> bool:
    """校验下载内容是正常图片:能解码,且不是已知占位图尺寸。"""
    import io
    from PIL import Image
    try:
        w, h = Image.open(io.BytesIO(data)).size
    except Exception:
        return False
    return (w, h) not in ((200, 200), (238, 238))


async def download_image(session, url: str, cookie: str = "",
                         timeout: int = 20) -> bytes:
    """下载图片:首选 imgsrc 原图接口(免登录,用完整图片哈希,多数图能拿到1000px+原图),
    失败/占位回退原始带签名链接(250px档)。

    注意:签名链接不可改任何参数(改尺寸/scheme必返回占位图);
    哈希必须取完整串(新版为40位,旧的32位正则会截断导致原图接口回占位图)。
    """
    import aiohttp
    h = _headers(cookie)
    h["Referer"] = "https://tieba.baidu.com/"
    candidates = []
    m = re.search(r"([0-9a-f]{30,42}\.jpe?g)", url)
    if m and "imgsrc.baidu.com" not in url:
        candidates.append(f"https://imgsrc.baidu.com/forum/pic/item/{m.group(1)}")
    if url not in candidates:
        candidates.append(url)
    best = b""
    last_err: Exception = TiebaError("图片下载失败")
    for u in candidates:
        try:
            async with session.get(u, headers=h,
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    raise TiebaError(f"图片下载失败 HTTP {resp.status}")
                data = await resp.read()
            if len(data) >= 3000 and _img_ok(data):
                return data  # 正常图(原图或签名缩略图)
            if len(data) > len(best):
                best = data  # 占位图留作最后兜底
        except Exception as e:
            last_err = e
    if best and _img_ok(best):
        return best
    raise last_err


async def download_video(session, url: str, cookie: str = "", timeout: int = 60,
                         max_bytes: int = 30 * 1024 * 1024) -> bytes:
    """下载视频(转码直链,必须携带贴吧 Referer,保留原始scheme)。"""
    import aiohttp
    h = _headers(cookie)
    h["Referer"] = "https://tieba.baidu.com/"
    async with session.get(url, headers=h,
                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            raise TiebaError(f"视频下载失败 HTTP {resp.status}")
        data = await resp.read()
    if len(data) < 50 * 1024:
        raise TiebaError("视频内容异常(过小)")
    if len(data) > max_bytes:
        raise TiebaError(f"视频过大({len(data) // 1024 // 1024}MB),跳过")
    return data
