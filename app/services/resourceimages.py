"""On-demand forum images with a referer, bounded memory and no disk cache."""
import ipaddress
import socket
import threading
from io import BytesIO
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image

from app.core.config import get_settings
from app.modules.ladysite.sources import get_source
from app.services.mihomo import proxy_lease

_slots = threading.BoundedSemaphore(4)
MAX_BYTES = 8 * 1024 * 1024


def _public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('图片地址无效')
    if parsed.port not in (None, 80, 443):
        raise ValueError('图片地址端口无效')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('不读取内网图片地址')


def fetch(url, referer, source):
    if not _slots.acquire(timeout=2):
        raise TimeoutError('图片请求繁忙，请稍后重试')
    try:
        headers = {'Referer': referer, 'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*'}
        explicit = (get_source(source) or {}).get('proxy') or None
        with proxy_lease(get_settings().proxy or None, explicit, source, url) as proxy, httpx.Client(
            proxy=proxy, timeout=httpx.Timeout(20, connect=8), follow_redirects=False, trust_env=False
        ) as client:
            for _ in range(4):
                _public_url(url)
                with client.stream('GET', url, headers=headers) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get('location', ''))
                        continue
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError('图片超过 8 MiB')
                with Image.open(BytesIO(data)) as image:
                    media_type = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp', 'GIF': 'image/gif', 'AVIF': 'image/avif'}.get(image.format)
                    if not media_type or image.width * image.height > 40_000_000:
                        raise ValueError('图片格式或尺寸不支持')
                    image.verify()
                return bytes(data), media_type
            raise ValueError('图片重定向次数过多')
    finally:
        _slots.release()
