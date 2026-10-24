import asyncio
import json
import time
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

from . import tieba, tieba_parser
from .tieba import TiebaError

DATA_DIR = Path("data/plugin_data/astrbot_plugin_tieba_push")
PUSHED_FILE = DATA_DIR / "pushed.json"
TMP_DIR = DATA_DIR / "tmp"

DEFAULT_FORUMS = ["搬石", "核战避难所", "地狱笑话", "吊图"]


@register(
    "astrbot_plugin_tieba_push",
    "Balji",
    "定时抓取指定贴吧的热门帖(文字+图片)并推送到白名单群聊;管理员可手动触发",
    "1.4.5",
)
class TiebaPushPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = config or {}
        self.forums = [str(x) for x in (cfg.get("forums") or DEFAULT_FORUMS) if str(x).strip()]
        self.per_forum = max(1, int(cfg.get("per_forum", 2)))
        self.min_replies = max(0, int(cfg.get("min_replies", 50)))
        if self.min_replies == 800:
            # 旧版默认800在中小规模吧会过滤掉全部帖子,迁移为新默认50
            logger.info("[tieba_push] 检测到旧默认热度门槛800,已调整为50(可在配置中修改)")
            self.min_replies = 50
        self.push_times = [str(x) for x in (cfg.get("push_times") or ["08:00", "20:00"])]
        self.group_whitelist = [str(x) for x in (cfg.get("group_whitelist") or [])]
        self.platform = str(cfg.get("platform", "aiocqhttp"))
        self.cookie = str(cfg.get("tieba_cookie", "") or "").strip()
        self.timeout = int(cfg.get("timeout", 20))
        self.max_images = max(1, int(cfg.get("max_images_per_post", 9)))
        self.max_video_mb = max(1, int(cfg.get("max_video_mb", 30)))
        self._sched_task = None
        self._pushing = False
        self._pushed = self._load_pushed()
        self._platform_id = ""

    async def initialize(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._clean_tmp()
        if self.push_times and self.group_whitelist:
            self._sched_task = asyncio.create_task(self._scheduler_loop())
            logger.info(f"[tieba_push] 定时推送已启动:每天 {self.push_times} -> 群 {self.group_whitelist}")
        else:
            logger.info("[tieba_push] 未配置推送时间或白名单,定时推送未启动(手动指令仍可用)")

    def _clean_tmp(self):
        """清空临时目录(图片/视频发送后不再需要,保留一轮后清理)。"""
        try:
            if TMP_DIR.is_dir():
                count, size = 0, 0
                for f in TMP_DIR.glob("*"):
                    if f.is_file():
                        size += f.stat().st_size
                        f.unlink(missing_ok=True)
                        count += 1
                if count:
                    logger.info(f"[tieba_push] 已清理临时文件 {count} 个,释放 {size / 1024 / 1024:.1f} MB")
        except Exception as e:
            logger.warning(f"[tieba_push] 清理临时文件失败: {e}")

    async def terminate(self):
        if self._sched_task:
            self._sched_task.cancel()
            self._sched_task = None

    # ---------------- 去重记录 ----------------

    def _load_pushed(self) -> dict:
        try:
            return json.loads(PUSHED_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_pushed(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if len(self._pushed) > 5000:
                cutoff = time.time() - 30 * 86400
                self._pushed = {k: v for k, v in self._pushed.items() if v > cutoff}
            PUSHED_FILE.write_text(json.dumps(self._pushed, ensure_ascii=False),
                                   encoding="utf-8")
        except Exception as e:
            logger.warning(f"[tieba_push] 去重记录保存失败: {e}")

    # ---------------- 定时 ----------------

    async def _scheduler_loop(self):
        while True:
            try:
                today_targets = []
                now = time.time()
                lt = time.localtime(now)
                for hhmm in self.push_times:
                    try:
                        hh, mm = map(int, hhmm.strip().split(":"))
                    except ValueError:
                        continue
                    ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                      hh % 24, mm % 60, 0, 0, 0, -1))
                    if ts > now:
                        today_targets.append(ts)
                wait = (min(today_targets) - now) if today_targets else 3600
                if not today_targets:
                    # 今天的时间点都过了,睡到明天第一个点
                    for hhmm in self.push_times:
                        try:
                            hh, mm = map(int, hhmm.strip().split(":"))
                            ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                              hh % 24, mm % 60, 0, 0, 0, -1)) + 86400
                            today_targets.append(ts)
                        except ValueError:
                            continue
                    wait = (min(today_targets) - now) if today_targets else 3600
                await asyncio.sleep(max(1, wait))
                await self.run_push()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[tieba_push] 调度循环异常,1小时后重试: {e}")
                await asyncio.sleep(3600)

    # ---------------- 抓取 ----------------

    async def _collect_mobile(self, session, kw: str):
        """主通道:贴吧老版移动端页面(免登录)。列表顺序即热门顺序。

        返回 (帖子列表, 热度不足被跳过数, 列表总帖数)。
        """
        from urllib.parse import quote
        html_text = await tieba.fetch_html(
            session, tieba_parser.LIST_URL.format(kw=quote(kw)), self.cookie, self.timeout)
        posts = tieba_parser.parse_list(html_text)
        if not posts:
            raise TiebaError(f"{kw}吧:移动端未解析到帖子(可能被反爬)")
        results = []
        skipped_low = 0
        for p in posts:
            if len(results) >= self.per_forum:
                break
            if p.tid in self._pushed:
                continue
            if p.reply_num < self.min_replies:
                skipped_low += 1
                continue  # 热度门槛:低于设定回复数的帖子不推送
            try:
                d = await tieba.fetch_html(
                    session, tieba_parser.DETAIL_URL.format(tid=p.tid), self.cookie, self.timeout)
                text, imgs, videos = tieba_parser.parse_detail(d)
            except Exception as e:
                logger.debug(f"[tieba_push] 移动端帖子 {p.tid} 解析失败: {e}")
                continue
            if not imgs and not videos:
                continue
            results.append((p.tid, p.title, text,
                            imgs[: self.max_images], videos[: 2]))
        return results, skipped_low, len(posts)

    async def _collect_www(self, session, kw: str) -> list:
        """备用通道:www 端页面(通常需要配置 Cookie,按回复数取热门)。"""
        html_text = await tieba.fetch_html(session, tieba.forum_url(kw),
                                           self.cookie, self.timeout)
        threads = tieba.parse_thread_list(html_text)
        if not threads:
            raise TiebaError(f"{kw}吧:www端未解析到帖子(可能被反爬)")
        results = []
        for brief in tieba.pick_hot(threads, max(self.per_forum * 4, self.per_forum + 3)):
            if len(results) >= self.per_forum:
                break
            if brief.tid in self._pushed:
                continue
            try:
                page = await tieba.fetch_html(
                    session, f"https://tieba.baidu.com/p/{brief.tid}",
                    self.cookie, self.timeout)
                content = tieba.parse_thread_page(page, brief.tid)
            except Exception as e:
                logger.debug(f"[tieba_push] www端帖子 {brief.tid} 解析失败: {e}")
                continue
            if not content.image_urls:
                continue
            results.append((brief.tid, content.title, content.text,
                            content.image_urls[: self.max_images]))
        return results

    async def _collect_posts(self, kw: str) -> list:
        """抓一个吧,凑够 per_forum 条图文/视频帖:移动端优先,www端兜底。

        移动端列表解析正常但没有帖子符合条件(热度门槛/已推过)时,
        不再调用www端(同样过不了门槛),由调用方给出准确提示。
        """
        results = []
        threshold_only = False
        async with aiohttp.ClientSession() as session:
            try:
                results, skipped_low, total = await self._collect_mobile(session, kw)
                if not results:
                    if total > 0:
                        threshold_only = True
                        logger.info(
                            f"[tieba_push] {kw}吧 移动端列表{total}帖无符合条件者"
                            f"({skipped_low}帖热度不足门槛{self.min_replies},其余已推过或无图文/视频)"
                        )
                    else:
                        raise TiebaError(f"{kw}吧:移动端未解析到帖子(可能被反爬)")
            except TiebaError as e:
                logger.warning(f"[tieba_push] {kw}吧 移动端失败({e}),改用www端")
                results = await self._collect_www(session, kw)
            except Exception as e:
                logger.warning(f"[tieba_push] {kw}吧 移动端失败({e}),改用www端")
                results = await self._collect_www(session, kw)
            if threshold_only:
                return []
            # 图片/视频下载落盘(发送用文件路径最稳)
            final = []
            for tid, title, text, img_urls, video_urls in results:
                img_paths, video_paths = [], []
                for u in img_urls:
                    try:
                        data = await tieba.download_image(session, u, self.cookie, self.timeout)
                        TMP_DIR.mkdir(parents=True, exist_ok=True)
                        p = TMP_DIR / f"{tid}_img{len(img_paths)}.jpg"
                        p.write_bytes(data)
                        img_paths.append(str(p))
                    except Exception as e:
                        logger.debug(f"[tieba_push] 图片下载失败 {u}: {e}")
                for u in video_urls:
                    try:
                        data = await tieba.download_video(
                            session, u, self.cookie, 90, self.max_video_mb * 1024 * 1024)
                        TMP_DIR.mkdir(parents=True, exist_ok=True)
                        p = TMP_DIR / f"{tid}_video{len(video_paths)}.mp4"
                        p.write_bytes(data)
                        video_paths.append(str(p))
                    except Exception as e:
                        logger.debug(f"[tieba_push] 视频下载失败 {u}: {e}")
                if img_paths or video_paths:
                    final.append((tid, title, text, img_paths, video_paths))
                    self._pushed[tid] = time.time()
            self._save_pushed()  # 每个吧收集完立即落盘,中断不丢去重记录
        return final

    # ---------------- 推送 ----------------

    def _resolve_platform_id(self) -> str:
        """解析平台实例ID:umo 需要的是实例ID(如 XiaoTao),不是平台类型名(aiocqhttp)。

        优先用配置值;若配置值不是任何实例ID,则自动查找 aiocqhttp 类型实例。
        """
        if self._platform_id:
            return self._platform_id
        resolved = self.platform
        try:
            insts = self.context.platform_manager.platform_insts
            ids = [p.meta().id for p in insts]
            if self.platform in ids:
                resolved = self.platform
            else:
                for p in insts:
                    if p.meta().name == "aiocqhttp":
                        resolved = p.meta().id
                        break
        except Exception as e:
            logger.debug(f"[tieba_push] 平台实例探测失败,使用配置值: {e}")
        self._platform_id = str(resolved)
        if self._platform_id != self.platform:
            logger.info(f"[tieba_push] 平台实例自动识别:{self._platform_id}")
        return self._platform_id

    async def run_push(self, only_forum: str = "") -> str:
        """执行一次推送:每个图文帖一条合并转发(标题+图片,不显示吧名/作者),
        视频帖单独直发(标题+视频同一条)。返回结果摘要。"""
        if self._pushing:
            return "上一轮推送尚未结束,已忽略本次触发"
        self._pushing = True
        chains = []
        forward_count = 0
        direct = []
        errors = []
        try:
            self._clean_tmp()  # 清掉上一轮的临时文件(本轮新文件留到下轮再清)
            forums = [only_forum] if only_forum else self.forums
            for kw in forums:
                try:
                    posts = await self._collect_posts(kw)
                except Exception as e:
                    logger.warning(f"[tieba_push] {kw} 抓取失败: {e}")
                    errors.append(f"{kw}: {e}")
                    continue
                if not posts:
                    errors.append(
                        f"{kw}: 无符合条件的新帖(热度门槛{self.min_replies}回复/已推过/无图文),"
                        f"可调低min_replies")
                    continue
                for tid, title, text, img_paths, video_paths in posts:
                    if img_paths:
                        node = Node(
                            content=[Plain(title)] +
                                    [Image.fromFileSystem(p) for p in img_paths],
                            name="百度贴吧", uin="10000",
                        )
                        chains.append(MessageChain(chain=[Nodes(nodes=[node])]))
                        forward_count += 1
                    for v in video_paths:
                        direct.append((title, v))
            for title, v in direct:
                chains.append(MessageChain(chain=[Plain(title), Video.fromFileSystem(v)]))
            if not chains:
                self._save_pushed()
                summary = "本轮没有可推送的内容" + ("\n问题:" + ";".join(errors) if errors else "")
                return summary
            platform_id = self._resolve_platform_id()
            for gid in self.group_whitelist:
                umo = f"{platform_id}:GroupMessage:{gid}"
                for chain in chains:
                    try:
                        ok = await self.context.send_message(umo, chain)
                        if ok is False:
                            errors.append(f"群{gid}发送失败:平台或会话不存在({umo})")
                            continue
                        await asyncio.sleep(1.5)  # 防风控限速
                    except Exception as e:
                        logger.error(f"[tieba_push] 发送到群 {gid} 失败: {e}")
                        errors.append(f"群{gid}发送失败: {e}")
            self._save_pushed()
        finally:
            self._pushing = False
        summary = f"推送完成:图文合并转发{forward_count}条+视频{len(direct)}条"
        if errors:
            summary += "\n问题:" + ";".join(errors)
        logger.info(f"[tieba_push] {summary}")
        return summary


    @filter.command("贴吧cookie测试")
    async def test_cookie(self, event: AstrMessageEvent):
        """检验当前配置的贴吧Cookie是否有效(仅管理员)。双端点验证减少误判。"""
        if not event.is_admin():
            yield event.plain_result("该指令仅管理员可用")
            return
        if not self.cookie:
            yield event.plain_result("当前未配置Cookie(tieba_cookie为空)。不填也能用:移动端通道+原图直取均可用,仅www兜底通道不可用")
            return
        import aiohttp as _aiohttp
        from . import tieba as _tb
        results = []
        username = ""
        async with _aiohttp.ClientSession() as session:
            for name, url in [
                ("我的贴吧", "https://tieba.baidu.com/f/like/myname"),
                ("百度个人中心", "https://passport.baidu.com/center"),
            ]:
                try:
                    async with session.get(
                        url, headers=_tb._headers(self.cookie), allow_redirects=False,
                        timeout=_aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        if resp.status == 200:
                            results.append(f"{name}:✅")
                            if not username:
                                body = await resp.text(errors="ignore")
                                import re as _re
                                m = _re.search(r'user_name["\s=:]+([^"<>&\s]{1,30})', body)
                                if m:
                                    username = m.group(1)
                        elif resp.status in (301, 302):
                            results.append(f"{name}:❌(重定向)")
                        else:
                            results.append(f"{name}:⚠️ HTTP {resp.status}")
                except Exception as e:
                    results.append(f"{name}:⚠️ {str(e)[:40]}")
        summary = " | ".join(results)
        if "✅" in summary:
            who = f"(账号:{username})" if username else ""
            yield event.plain_result(f"✅ Cookie有效{who}\n{summary}\nwww兜底通道已启用")
        elif "❌" in summary:
            yield event.plain_result(
                f"❌ Cookie无效或已过期\n{summary}\n"
                "请确认:1)用电脑浏览器登录tieba.baidu.com后重新复制;2)BDUSS值需完整(约190字符,Ctrl+A全选复制,结尾易漏);3)不要经过聊天软件中转")
        else:
            yield event.plain_result(f"⚠️ 无法确定(可能网络/风控)\n{summary}\n建议切换网络后重试")

    @filter.command("贴吧推送")
    async def manual_push(self, event: AstrMessageEvent, forum: str = ""):
        """手动触发一次推送(仅管理员)。用法:/贴吧推送 [吧名]"""
        if not event.is_admin():
            yield event.plain_result("该指令仅管理员可用")
            return
        if not self.group_whitelist:
            yield event.plain_result("尚未配置推送群白名单,请先在插件配置中填写 group_whitelist")
            return
        yield event.plain_result(f"开始抓取{'[' + forum + ']' if forum else '全部贴吧'}…")
        summary = await self.run_push(only_forum=forum.strip())
        yield event.plain_result(summary)
