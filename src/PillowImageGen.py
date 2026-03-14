import logging
import os
import re
import requests
import textwrap
import time
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from hashlib import md5

from apps.channels.models import Channel
from apps.proxy.ts_proxy.server import ProxyServer
from apps.proxy.ts_proxy.channel_status import ChannelStatus

from .TooManyStreamsConfig import TooManyStreamsConfig


DEFAULT_OUT_FILE = "too_many_streams.jpg"
CACHE_DIR = "/tmp/tms_logos"

class PillowImageGen:
    """
    Generates a 1920x1080 JPG image of active streams using Pillow.
    Optimized for low CPU usage with reliable state detection.
    """
    
    _last_active_uuids = None

    def __init__(
        self,
        out_path: str = DEFAULT_OUT_FILE,
    ):
        config = TooManyStreamsConfig.get_config()
        self.title = config.stream_title
        self.description = config.stream_description
        self.html_cols = max(1, int(config.stream_channel_cols))
        self.out_path = out_path
        self.active_streams: list[tuple[str, str, str]] = []
        self._current_uuids = []

        self.logger = logging.getLogger("plugins.too_many_streams.PillowImageGen")
        self.logger.setLevel(config.tms_log_level)
        
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _get_cached_logo(self, url: str) -> Image.Image:
        if not url: return None
        hashed_url = md5(url.encode()).hexdigest()
        cache_path = os.path.join(CACHE_DIR, hashed_url)
        
        if os.path.exists(cache_path) and (time.time() - os.path.getmtime(cache_path) < 3600):
            try:
                return Image.open(cache_path).convert("RGBA")
            except Exception: pass

        try:
            resp = requests.get(url, timeout=3)
            if resp.status_code == 200:
                with open(cache_path, "wb") as f:
                    f.write(resp.content)
                return Image.open(BytesIO(resp.content)).convert("RGBA")
        except Exception: pass
        return None

    def get_active_streams(self) -> bool:
        """
        Fetches active streams and populates self.active_streams.
        Returns: True if the list of streams has changed since last generation.
        """
        try:
            proxy_server = ProxyServer.get_instance()
            channel_pattern = "ts_proxy:channel:*:metadata"
            cursor = 0
            active_uuids = []
            
            while True:
                cursor, keys = proxy_server.redis_client.scan(cursor, match=channel_pattern)
                for key in keys:
                    try:
                        m = re.search(r"ts_proxy:channel:(.*):metadata", key.decode("utf-8"))
                        if m: active_uuids.append(m.group(1))
                    except: continue
                if cursor == 0: break
            
            active_uuids.sort()
            self._current_uuids = active_uuids

            # Always fetch the data to ensure self.active_streams is populated for generate()
            if not active_uuids: 
                self.active_streams = []
            else:
                channels = Channel.objects.filter(uuid__in=active_uuids).only('id', 'name', 'logo', 'uuid')
                active_list = []
                tms_url = TooManyStreamsConfig.get_stream_url()
                
                for ch in channels:
                    channel_info = ChannelStatus.get_basic_channel_info(str(ch.uuid))
                    if channel_info.get("url") == tms_url:
                        continue
                    
                    active_list.append((
                        f"#{ch.id}", 
                        ch.logo.url if ch.logo else "", 
                        ch.name
                    ))
                
                def channel_sort_key(item):
                    num_str = item[0].lstrip("#")
                    return int(num_str) if num_str.isdigit() else 999999
                
                active_list.sort(key=channel_sort_key)
                self.active_streams = active_list[:15] 

            # Detect change
            has_changed = self._current_uuids != PillowImageGen._last_active_uuids
            return has_changed
            
        except Exception as e:
            self.logger.error("Error in get_active_streams", exc_info=True)
            return True # Force generation on error to be safe

    def _hex_to_rgb(self, hex_color: str, default: tuple) -> tuple:
        try:
            hex_color = hex_color.lstrip('#')
            return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        except Exception:
            return default

    def generate(self, force=False) -> bool:
        """Generates the image. Force=True bypasses the change check."""
        if not force and self._current_uuids == PillowImageGen._last_active_uuids and os.path.exists(self.out_path):
            return False

        width, height = 1920, 1080
        config = TooManyStreamsConfig.get_config()
        
        bg_color = self._hex_to_rgb(config.theme_bg_color, (15, 23, 42))
        title_color = self._hex_to_rgb(config.theme_text_color, (248, 250, 252))
        desc_color = (148, 163, 184) # Keep secondary text static or derive? Let's keep it static for now or add config later.
        
        card_bg = self._hex_to_rgb(config.theme_card_bg_color, (30, 41, 59))
        card_border = self._hex_to_rgb(config.theme_card_border_color, (51, 65, 85))
        
        pill_bg_color = self._hex_to_rgb(config.theme_accent_color, (56, 189, 248))
        pill_text_color = self._hex_to_rgb(config.theme_accent_text_color, (15, 23, 42))
        
        name_color = title_color # Use main text color for channel names
        unavailable_color = (239, 68, 68)
        
        try:
            img = Image.new('RGBA', (width, height), color=bg_color + (255,))
            draw = ImageDraw.Draw(img)

            def load_font(size, bold=False):
                fonts = ["arialbd.ttf", "arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
                for f in fonts:
                    try: return ImageFont.truetype(f, size)
                    except: continue
                return ImageFont.load_default()

            title_font, desc_font = load_font(48, True), load_font(20)
            name_font, pill_font = load_font(22, True), load_font(14, True)

            if not self.active_streams:
                unavailable_text = "This Channel is Unavailable"
                bbox = draw.textbbox((0, 0), unavailable_text, font=title_font)
                draw.text(((width - (bbox[2] - bbox[0])) / 2, (height - (bbox[3] - bbox[1])) / 2), 
                          unavailable_text, font=title_font, fill=unavailable_color)
            else:
                bbox = draw.textbbox((0, 0), self.title, font=title_font)
                draw.text(((width - (bbox[2] - bbox[0])) / 2, 100), self.title, font=title_font, fill=title_color)

                content_width = 1440
                grid_margin = (width - content_width) / 2
                wrapper = textwrap.TextWrapper(width=100)
                desc_lines = wrapper.wrap(text=self.description)
                current_y = 180
                for line in desc_lines:
                    bbox = draw.textbbox((0, 0), line, font=desc_font)
                    draw.text(((width - (bbox[2] - bbox[0])) / 2, current_y), line, font=desc_font, fill=desc_color)
                    current_y += 32

                cols, card_spacing = self.html_cols, 24
                card_w = (content_width - (card_spacing * (cols - 1))) / cols
                card_h, grid_y_start = 200, 350

                for i, (channel_num, icon_url, channel_name) in enumerate(self.active_streams):
                    col, row = i % cols, i // cols
                    x = grid_margin + col * (card_w + card_spacing)
                    y = grid_y_start + row * (card_h + card_spacing)
                    
                    # Alternating card background slightly? 
                    # The original code had card_bg_odd/even. 
                    # Let's simplify to just one card_bg for custom themes, or darken one slightly.
                    # We will stick to the single configured card color for consistency.
                    
                    draw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=12, fill=card_bg + (255,), outline=card_border + (255,), width=2)
                    
                    px, py = 24, 24
                    pill_text = f"CH {channel_num.replace('#', '')}"
                    p_bbox = draw.textbbox((0, 0), pill_text, font=pill_font)
                    p_w, p_h = (p_bbox[2] - p_bbox[0]) + 24, (p_bbox[3] - p_bbox[1]) + 12
                    draw.rounded_rectangle([x + px, y + py, x + px + p_w, y + py + p_h], radius=6, fill=pill_bg_color + (255,))
                    draw.text((x + px + 12, y + py + 6), pill_text, font=pill_font, fill=pill_text_color)
                    icon_size = 80
                    icon_x, icon_y = x + px, y + py + p_h + 16
                    icon = self._get_cached_logo(icon_url)
                    if icon:
                        icon.thumbnail((icon_size, icon_size), Image.Resampling.LANCZOS)
                        draw.rounded_rectangle([icon_x, icon_y, icon_x + icon_size, icon_y + icon_size], radius=8, fill=bg_color + (255,), outline=card_border + (255,), width=1)
                        img.paste(icon, (int(icon_x), int(icon_y)), icon)
                    else:
                        draw.rectangle([icon_x, icon_y, icon_x + icon_size, icon_y + icon_size], fill=(bg_color + (255,)))
                    name_x, name_y = icon_x + icon_size + 16, icon_y + 5
                    max_name_w = card_w - (px * 2) - icon_size - 20
                    avg_char_w = draw.textbbox((0, 0), "A", font=name_font)[2]
                    chars_per_line = max(1, int(max_name_w / avg_char_w))
                    name_lines = textwrap.wrap(channel_name, width=chars_per_line)
                    for line_idx, line in enumerate(name_lines[:3]):
                        draw.text((name_x, name_y + (line_idx * 28)), line, font=name_font, fill=name_color)

            final_img = img.convert("RGB")
            os.makedirs(os.path.dirname(os.path.abspath(self.out_path)) or ".", exist_ok=True)
            final_img.save(self.out_path, "JPEG", quality=92)
            PillowImageGen._last_active_uuids = self._current_uuids
            return True
        except Exception as e:
            self.logger.error("Generation failed", exc_info=True)
            return False
