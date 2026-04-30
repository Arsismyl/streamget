import asyncio
import html
import json
import re
from urllib.parse import urlparse

from ... import utils
from ...data import StreamData, wrap_stream
from ...requests.async_http import async_req
from ..base import BaseLiveStream


class TwitCastingLiveStream(BaseLiveStream):
    """
    A class for fetching and processing TwitCasting live stream information.
    """
    def __init__(self, proxy_addr: str | None = None, cookies: str | None = None, username: str | None = None,
                 password: str | None = None, account_type: str | None = None):
        super().__init__(proxy_addr, cookies)
        self.username = username
        self.password = password
        self.account_type = account_type
        self.mobile_headers = self._get_mobile_headers()

    def _get_mobile_headers(self) -> dict:
        return {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,'
                      '*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'content-type': 'application/x-www-form-urlencoded',
            'referer': 'https://twitcasting.tv/indexcaslogin.php?redir=%2Findexloginwindow.php%3Fnext%3D%252F&keep=1',
            'user-agent': 'ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))',
            'cookie': self.cookies or 'hl=zh; did=377eda93b5320f104357ab1bc98dfe4d; _ga=GA1.1.869052351.1747879503; '
                                      'keep=1; chid=relay_trade_jp;',
        }

    async def login_twitcasting(self) -> str | None:
        if self.account_type == "twitter":
            login_url = 'https://twitcasting.tv/indexpasswordlogin.php'
            login_api = 'https://twitcasting.tv/indexpasswordlogin.php?redir=/indexloginwindow.php?next=%2F&keep=1'
        else:
            login_url = 'https://twitcasting.tv/indexcaslogin.php?redir=%2F&keep=1'
            login_api = 'https://twitcasting.tv/indexcaslogin.php?redir=/indexloginwindow.php?next=%2F&keep=1'

        html_str = await async_req(login_url, proxy_addr=self.proxy_addr, headers=self.mobile_headers)
        
        m = re.search(r'<input[^>]+name=["\']cs_session_id["\'][^>]+value=["\'](.*?)["\']', html_str)
        if not m:
            raise RuntimeError("TwitCasting login page parse failed: cs_session_id not found. Cookie mode is recommended.")
        cs_session_id = m.group(1)
        
        data = {
            'username': self.username,
            'password': self.password,
            'action': 'login',
            'cs_session_id': cs_session_id,
        }
        try:
            cookie_dict = await async_req(
                login_api, proxy_addr=self.proxy_addr, headers=self.mobile_headers,
                data=data, return_cookies=True, timeout=20)
            if 'tc_ss' in cookie_dict:
                self.cookies = utils.dict_to_cookie_str(cookie_dict)
                self.mobile_headers['cookie'] = self.cookies
                return self.cookies
        except Exception as e:
            raise Exception("TwitCasting login error,", e)

    async def fetch_web_stream_data(self, url: str, process_data: bool = True) -> dict:
        """
        Fetches web stream data for a live room.
        修正版：
        1. 不再只靠 movie_id 判断开播，避免未开播时误判为直播中。
        2. 拿到 TwitCasting m3u8 后先验证是否可访问。
        3. 如果 HLS 是 404 / 不可播放，返回 is_live=False，避免 StreamCap 进入录制中后报错。
        """
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        anchor_id = path_parts[0] if path_parts and path_parts[0] else url.rstrip("/").split("/")[-1]
    
        result = {
            "anchor_name": anchor_id,
            "is_live": False,
            "live_url": url,
        }
    
        new_cookie = None
    
        def pick(patterns, text, group=1, flags=re.I | re.S, default=""):
            for pattern in patterns:
                m = re.search(pattern, text, flags)
                if m:
                    return html.unescape(m.group(group).strip())
            return default
    
        def parse_page(html_str: str):
            title_tag_match = re.search(r"<title>(.*?)</title>", html_str, re.I | re.S)
            title_tag = html.unescape(title_tag_match.group(1).strip()) if title_tag_match else ""
    
            anchor_match = re.search(r"<title>(.*?)\s*\(@([^)]+)\)", html_str, re.I | re.S)
            if anchor_match:
                anchor_display = html.unescape(anchor_match.group(1).strip())
                anchor_screen = anchor_match.group(2).strip()
            else:
                anchor_display = anchor_id
                anchor_screen = anchor_id
    
            live_title = pick(
                [
                    r'<meta\s+name=["\']twitter:title["\']\s+content=["\']([^"\']*)["\']',
                    r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']*)["\']',
                    r'"title"\s*:\s*"([^"]+)"',
                ],
                html_str,
                default=title_tag or anchor_id,
            )
    
            movie_id = pick(
                [
                    r'data-movie-id=["\']([^"\']+)["\']',
                    r'"movie_id"\s*:\s*"?(\d+)"?',
                    r'"movieId"\s*:\s*"?(\d+)"?',
                ],
                html_str,
                default="",
            )
    
            # 重点：这里只接受明确的直播标记，不再用 movie_id 单独判断直播中
            is_live = False
            live_patterns = [
                r'data-is-onlive=["\']true["\']',
                r'"is_live"\s*:\s*true',
                r'"isOnLive"\s*:\s*true',
                r'"isLive"\s*:\s*true',
            ]
    
            for p in live_patterns:
                if re.search(p, html_str, re.I | re.S):
                    is_live = True
                    break
                
            anchor_name = f"{anchor_display}-{anchor_screen}-{movie_id or 'unknown'}"
            return anchor_name, is_live, live_title, movie_id
    
        async def check_m3u8_available(play_url: str) -> bool:
            """
            验证 m3u8 是否真的可播放。
            404、403、HTML 错误页、空响应都视为不可录制。
            """
            try:
                headers = dict(self.mobile_headers)
                headers.update({
                    "accept": "*/*",
                    "referer": f"https://twitcasting.tv/{anchor_id}",
                    "origin": "https://twitcasting.tv",
                })
    
                txt = await async_req(
                    play_url,
                    proxy_addr=self.proxy_addr,
                    headers=headers,
                    timeout=15,
                )
    
                if isinstance(txt, bytes):
                    txt = txt.decode("utf-8", "ignore")
    
                head = txt[:300].lstrip()
                return head.startswith("#EXTM3U")
    
            except Exception:
                return False
    
        async def get_page_html() -> str:
            return await async_req(
                url,
                proxy_addr=self.proxy_addr,
                headers=self.mobile_headers,
                timeout=20,
            )
    
        # 只有 URL 明确带 ?login=true 时才走账号密码登录。
        # 你现在主要是 Cookie 模式，正常不要加 login=true。
        to_login = self.get_params(url, "login")
        if to_login == "true":
            if not self.username or not self.password:
                raise RuntimeError(
                    "TwitCasting login=true requires username and password. "
                    "If using cookie, remove login=true."
                )
    
            new_cookie = await self.login_twitcasting()
            if not new_cookie:
                raise RuntimeError("TwitCasting login failed, please check username/password.")
    
            self.mobile_headers["cookie"] = new_cookie
    
        try:
            html_str = await get_page_html()
            anchor_name, is_live, live_title, movie_id = parse_page(html_str)
        except Exception as e:
            # Cookie 模式下不要解析失败后强行账号密码登录，否则容易出现 cs_session_id NoneType 二次报错
            if self.username and self.password and not self.cookies:
                new_cookie = await self.login_twitcasting()
                if not new_cookie:
                    raise RuntimeError("TwitCasting login failed, please check username/password.")
    
                self.mobile_headers["cookie"] = new_cookie
                html_str = await get_page_html()
                anchor_name, is_live, live_title, movie_id = parse_page(html_str)
            else:
                result["error"] = f"TwitCasting page parse failed: {e}"
                return result
    
        result["anchor_name"] = anchor_name
        result["new_cookies"] = new_cookie
    
        # 页面没有明确直播标记，直接返回未开播
        if not is_live:
            result["is_live"] = False
            return result
    
        url_streamserver = (
            f"https://twitcasting.tv/streamserver.php?"
            f"target={anchor_id}&mode=client&player=pc_web"
        )
    
        last_error = None
        json_data = None
    
        for i in range(5):
            try:
                twitcasting_str = await async_req(
                    url_streamserver,
                    proxy_addr=self.proxy_addr,
                    headers=self.mobile_headers,
                    timeout=20,
                )
                json_data = json.loads(twitcasting_str)
                break
            except Exception as e:
                last_error = e
                await asyncio.sleep(2 + i * 2)
    
        if json_data is None:
            result["is_live"] = False
            result["error"] = f"TwitCasting streamserver request failed after retries: {last_error}"
            return result
    
        streams = (
            json_data.get("tc-hls", {}).get("streams")
            if isinstance(json_data, dict)
            else None
        )
    
        if not streams:
            result["is_live"] = False
            result["error"] = "TwitCasting has no tc-hls streams. Maybe offline, private, or cookie has no permission."
            return result
    
        quality_order = {
            "high": 0,
            "source": 0,
            "medium": 1,
            "low": 2,
        }
    
        sorted_streams = sorted(
            [
                (quality, stream_url)
                for quality, stream_url in streams.items()
                if isinstance(stream_url, str) and stream_url.startswith("http")
            ],
            key=lambda item: quality_order.get(item[0], 99),
        )
    
        valid_play_url_list = []
    
        for quality, stream_url in sorted_streams:
            if await check_m3u8_available(stream_url):
                valid_play_url_list.append(stream_url)
    
        # 关键：所有 m3u8 都是 404 / 不可访问时，不要告诉 StreamCap 正在直播
        if not valid_play_url_list:
            result["is_live"] = False
            result["error"] = (
                "TwitCasting page says live, but all m3u8 urls are unavailable. "
                "Maybe offline, FC/member-only without permission, or stale streamserver data."
            )
            return result
    
        result |= {
            "title": live_title,
            "is_live": True,
            "play_url_list": valid_play_url_list,
        }
    
        return result

    async def fetch_stream_url(self, json_data: dict, video_quality: str | int | None = None) -> StreamData:
        """
        Fetches the stream URL for a live room and wraps it into a StreamData object.
        """
        data = await self.get_stream_url(json_data, video_quality, spec=False, platform='TwitCasting')
        return wrap_stream(data)