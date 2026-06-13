import logging
import os
import re
import requests
import textwrap
import time
import uuid
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from hashlib import md5

from apps.channels.models import Channel, ChannelStream, Stream
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

    @staticmethod
    def _format_channel_number(channel_number) -> str:
        """Format channel numbers so whole floats (e.g. 13.0) render as 13."""
        try:
            num = float(channel_number)
        except (TypeError, ValueError):
            return str(channel_number)
        return str(int(num)) if num.is_integer() else str(num)

    @staticmethod
    def _channel_sort_key(item):
        """Sort grid items by channel number (item[0] looks like '#1015')."""
        num_str = item[0].lstrip("#")
        try:
            return float(num_str)
        except (TypeError, ValueError):
            return 999999

    def _get_directory_fallback(self, tms_url):
        """Channels this plugin manages (those with the TMS stream applied), as a
        (number, logo, name) grid.

        Returns (items, signature). The signature is stored in _current_uuids so
        generate()'s change-detection can tell the directory view apart from the
        active-channel view and from a previous empty render.
        """
        try:
            tms_stream_id = Stream.objects.filter(url=tms_url).values_list('id', flat=True).first()
            if not tms_stream_id:
                return [], ['__tms_directory_empty__']

            ch_ids = list(
                ChannelStream.objects.filter(stream_id=tms_stream_id).values_list('channel_id', flat=True)
            )
            if not ch_ids:
                return [], ['__tms_directory_empty__']

            # Sort + cap in the DB. channel_number is an indexed FloatField, so the
            # ORDER BY matches the old Python float sort exactly while letting the DB
            # return just the 15 rows we render -- instead of materializing every
            # channel that has the placeholder applied (thousands) only to drop all
            # but 15. This path runs every 60s while the placeholder is on screen.
            channels = (
                Channel.objects.filter(id__in=ch_ids)
                .only('channel_number', 'name', 'logo')
                .order_by('channel_number')[:15]
            )
            items = [
                (f"#{self._format_channel_number(ch.channel_number)}",
                 ch.logo.url if ch.logo else "", ch.name)
                for ch in channels
            ]
            signature = ['__tms_directory__'] + [it[0] for it in items]
            return items, signature
        except Exception:
            self.logger.error("Error building TMS directory fallback", exc_info=True)
            return [], ['__tms_directory_error__']

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

            # ts_proxy also creates metadata keys for streams viewed DIRECTLY by
            # their stream_hash (a 64-char hex), not only channels keyed by UUID.
            # The TMS placeholder stream itself does this when previewed/opened,
            # so its own hash shows up here. A single non-UUID value makes
            # Channel.objects.filter(uuid__in=...) raise a ValidationError that
            # aborts the whole lookup and blanks the list (showing "unavailable"
            # even while real channels are active). Keep only valid channel UUIDs.
            valid_uuids = []
            for u in active_uuids:
                try:
                    uuid.UUID(str(u))
                except (ValueError, TypeError, AttributeError):
                    continue
                valid_uuids.append(u)
            active_uuids = valid_uuids

            tms_url = TooManyStreamsConfig.get_stream_url()

            # Build the list of OTHER channels currently streaming real content:
            # active in ts_proxy, excluding any that are themselves showing the TMS
            # placeholder (so the screen never lists itself during a failover).
            active_list = []
            if active_uuids:
                channels = Channel.objects.filter(uuid__in=active_uuids).only('channel_number', 'name', 'logo', 'uuid')
                for ch in channels:
                    channel_info = ChannelStatus.get_basic_channel_info(str(ch.uuid)) or {}
                    if channel_info.get("url") == tms_url:
                        continue
                    display_number = self._format_channel_number(ch.channel_number)
                    active_list.append((f"#{display_number}", ch.logo.url if ch.logo else "", ch.name))
                active_list.sort(key=self._channel_sort_key)
                active_list = active_list[:15]

            if active_list:
                # Something watchable is on elsewhere -> show those channels.
                self.active_streams = active_list
                self._current_uuids = active_uuids
            else:
                # Nothing else is watchable right now (e.g. during a failover every
                # active channel is itself on the placeholder, or nothing is on at
                # all). Fall back to a directory of the channels this plugin manages
                # so the grid is never empty.
                self.active_streams, self._current_uuids = self._get_directory_fallback(tms_url)

            # Detect change. The signature differentiates active vs directory vs empty.
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
