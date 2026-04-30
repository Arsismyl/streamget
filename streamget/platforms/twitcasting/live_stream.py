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
        """
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        anchor_id = path_parts[0] if path_parts and path_parts[0] else url.rstrip("/").split("/")[-1]

        result = {
            "anchor_name": "",
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

        async def get_data() -> tuple[str, str, str]:
            html_str = await async_req(
                url,
                proxy_addr=self.proxy_addr,
                headers=self.mobile_headers,
                timeout=20,
            )

            # 主播名 / 页面标题，兼容不同语言和不同 HTML 结构
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

            is_live = False
            if re.search(r'data-is-onlive=["\']true["\']', html_str, re.I):
                is_live = True
            elif re.search(r'"is_live"\s*:\s*true', html_str, re.I):
                is_live = True
            elif re.search(r'"isOnLive"\s*:\s*true', html_str, re.I):
                is_live = True
            elif movie_id:
                # TwitCasting 页面能拿到当前 movie_id 时，大概率就是直播中
                is_live = True

            anchor_name = f"{anchor_display}-{anchor_screen}-{movie_id or 'unknown'}"
            return anchor_name, "true" if is_live else "false", live_title

        # 只有明确带 ?login=true 时才走账号密码登录
        # 你现在是 Cookie 模式，正常不要在 URL 后面加 login=true
        to_login = self.get_params(url, "login")
        if to_login == "true":
            if not self.username or not self.password:
                raise RuntimeError("TwitCasting login=true requires username and password. If using cookie, remove login=true.")
            new_cookie = await self.login_twitcasting()
            if not new_cookie:
                raise RuntimeError("TwitCasting login failed, please check username/password.")
            self.mobile_headers["cookie"] = new_cookie

        try:
            anchor_name, live_status, live_title = await get_data()
        except Exception as e:
            # 不要在 Cookie 模式解析失败后强行账号密码登录
            # 否则就会出现 cs_session_id / NoneType 之类的二次报错
            if self.username and self.password and not self.cookies:
                new_cookie = await self.login_twitcasting()
                if not new_cookie:
                    raise RuntimeError("TwitCasting login failed, please check username/password.")
                self.mobile_headers["cookie"] = new_cookie
                anchor_name, live_status, live_title = await get_data()
            else:
                result["error"] = f"TwitCasting page parse failed: {e}"
                return result

        result["anchor_name"] = anchor_name

        if live_status == "true":
            url_streamserver = (
                f"https://twitcasting.tv/streamserver.php?"
                f"target={anchor_id}&mode=client&player=pc_web"
            )

            last_error = None
            json_data = None

            for i in range(3):
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
                raise RuntimeError(f"TwitCasting streamserver request failed after retries: {last_error}")

            if not json_data.get("tc-hls") or not json_data["tc-hls"].get("streams"):
                raise RuntimeError("No m3u8_url, please check TwitCasting url or cookie")

            stream_dict = json_data["tc-hls"]["streams"]

            quality_order = {
                "high": 0,
                "medium": 1,
                "low": 2,
            }

            sorted_streams = sorted(
                stream_dict.items(),
                key=lambda item: quality_order.get(item[0], 99),
            )

            play_url_list = [stream_url for quality, stream_url in sorted_streams]

            result |= {
                "title": live_title,
                "is_live": True,
                "play_url_list": play_url_list,
            }

        result["new_cookies"] = new_cookie
        return result

    async def fetch_stream_url(self, json_data: dict, video_quality: str | int | None = None) -> StreamData:
        """
        Fetches the stream URL for a live room and wraps it into a StreamData object.
        """
        data = await self.get_stream_url(json_data, video_quality, spec=False, platform='TwitCasting')
        return wrap_stream(data)