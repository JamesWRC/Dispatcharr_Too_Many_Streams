import logging
import io, base64, mimetypes, os, math, re, tempfile, subprocess, shutil
from pathlib import Path
from typing import List, Tuple

from apps.channels.models import Channel
from apps.proxy.ts_proxy.server import ProxyServer
from apps.proxy.ts_proxy.channel_status import ChannelStatus

from .TooManyStreamsConfig import TooManyStreamsConfig

class ActiveStreamImgGen:
    """
    Generates a 1920x1080 JPG image of active streams using wkhtmltoimage.
    No Playwright/Chromium required.
    """

    DEFAULT_TITLE = "Sorry, this channel is unavailable."
    DEFAULT_DESCRIPTION = "While this channel is not currently available, here are some other channels you can watch."
    DEFAULT_HTML_COLS = 4
    DEFAULT_OUT_FILE = "too_many_streams.jpg"

    def __init__(
        self,
        title: str = DEFAULT_TITLE,
        description: str = DEFAULT_DESCRIPTION,
        out_path: str = DEFAULT_OUT_FILE,
        html_cols: int = DEFAULT_HTML_COLS,
    ):
        self.title = title
        self.description = description
        self.out_path = out_path
        self.html_cols = int(html_cols)
        self.active_streams: List[Tuple[str, str, str]] = []

        self.logger = logging.getLogger("plugins.too_many_streams.ActiveStreamImgGen")
        self.logger.setLevel(os.environ.get("TMS_LOG_LEVEL", os.environ.get("DISPATCHARR_LOG_LEVEL", "INFO")).upper())

    def get_active_streams(self) -> List[Tuple[str, str, str]]:
        """
        Placeholder method to fetch active streams.
        Returns: list of (channel_number, icon_url, channel_name)
        """

        self.active_streams = []
        # Below code is from Dispatcharr\apps\proxy\ts_proxy\views.py channel_status()
        proxy_server = ProxyServer.get_instance()
        channel_pattern = "ts_proxy:channel:*:metadata"
        active_channels = []
        cursor = 0
        while True:
            cursor, keys = proxy_server.redis_client.scan(cursor, match=channel_pattern)
            for key in keys:
                m = re.search(r"ts_proxy:channel:(.*):metadata", key.decode("utf-8"))
                if m:
                    ch_id = m.group(1)
                    channel_info = ChannelStatus.get_basic_channel_info(ch_id)

                    # Skip our own TMS stream
                    if channel_info.get("url", "") == TooManyStreamsConfig.get_stream_url():  
                        continue
                    
                    self.logger.debug(f"Channel ID DATA: {ch_id}, Info: {channel_info}")
                    channel_data = Channel.objects.get(uuid=ch_id)
                    channel_num = channel_data.id
                    channel_img = channel_data.logo.url
                    channel_name = channel_data.name
                    self.logger.debug(f"Channel DATA: {channel_data}")
                    self.logger.debug(f"Channel NUM: {channel_num}, IMG: {channel_img}, NAME: {channel_name}")
                    active_channels.append((f"#{channel_num}", channel_img, channel_name))
            if cursor == 0:
                break
        
        self.logger.info(f"Found {len(active_channels)} active channels in Redis.")
        self.active_streams = active_channels

        # Order by channel number (assuming numeric)
        def channel_sort_key(item):
            num_str = item[0].lstrip("#")
            try:
                return int(num_str)
            except ValueError:
                return float('inf')  # Non-numeric channels go to the end
        self.active_streams.sort(key=channel_sort_key)
        # limit to first 12 active channels
        self.active_streams = self.active_streams[:12]

        return self.active_streams

    def file_to_data_uri(self, path: str) -> str:
        """
        Convert a local file to data URI for embedding in HTML.
        """
        p = Path(path).expanduser().resolve()
        if not p.exists():
            # tiny 1x1 gray PNG fallback
            png = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
                b"\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            b64 = base64.b64encode(png).decode()
            return f"data:image/png;base64,{b64}"
        mime, _ = mimetypes.guess_type(p.as_posix())
        mime = mime or "image/png"
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"data:{mime};base64,{b64}"

    def html_doc(self) -> str:
        """
        Build the HTML document for the channel grid.
        """
        rows_count = len(self.active_streams)
      
        REPLACE_WITH_PERCENT = round(100 / self.html_cols, 3)
        style = f"""
        <style>
          html, body {{
            width: 1920px; height: 1080px; margin: 0; background: #fff; color: #111;
            font-family: Arial, "Segoe UI", Roboto, sans-serif; /* simple stack */
          }}

          /* Center whole block */
          body {{
            text-align: center; /* minimal, avoids flex */
          }}
          .wrap {{
            width: 92%;
            max-width: 1680px;
            margin: 0 auto;
            display: block;
          }}

          h1 {{
            font-size: 48px;  /* fixed size; avoid clamp() */
            margin: 0 0 12px;
          }}
          .desc {{
            width: 82%;
            margin: 0 auto 20px;
            font-size: 20px;    /* fixed */
            line-height: 1.45;
            color: #333;
          }}

          /* ------- Grid replacement (row/column without CSS Grid) ------- */
          /* Set columns in your template (C = self.html_cols) */
          /* Each card becomes an inline-block with percentage width */
          .grid {{ font-size: 0; /* remove gaps between inline-blocks */ }}
          .card {{
            display: inline-block;
            vertical-align: top;
            width: {REPLACE_WITH_PERCENT}%;  /* = 100 / C, e.g., 50% for 2 cols, 33.333% for 3 */
            box-sizing: border-box;
            padding: 16px 22px;
            margin: 7px 9px;               /* simulate gap */
            background: #ffffff;
            border: 1px solid #e6e9ef;
            border-radius: 16px;
            box-shadow: 0 1px 2px rgba(16,24,40,0.04);
            font-size: 16px; /* restore text size */
            text-align: left;
          }}

          /* darker stripe for “even” rows: add class server-side */
          .card_even {{
            display: inline-block;
            vertical-align: top;
            width: {REPLACE_WITH_PERCENT}%;  /* = 100 / C, e.g., 50% for 2 cols, 33.333% for 3 */
            box-sizing: border-box;
            padding: 16px 22px;
            margin: 7px 9px;               /* simulate gap */
            background: #e2e2e2;
            border: 1px solid #e6e9ef;
            border-radius: 16px;
            box-shadow: 0 1px 2px rgba(16,24,40,0.04);
            font-size: 16px; /* restore text size */
            text-align: left;
          }}

          /* channel number pill */
          .chan {{
            display: inline-block;
            font-weight: 600;
            font-size: 18px;
            padding: 6px 10px;
            border-radius: 999px;
            background: #e1e1e1;
            color: #7294f2;
            border: 1px solid #dbe5ff;
            white-space: nowrap;
            margin-right: 12px;
          }}

          /* logo box (avoid object-fit) */
          .icon {{
            display: inline-block;
            width: 120px; height: 120px;   /* smaller for wkhtml; adjust as needed */
            overflow: hidden;
            border-radius: 12px;
            border: 1px solid #e6e9ef;
            vertical-align: middle;
            margin-right: 14px;
            background: transparent;
          }}
          .icon img {{
            max-width: 100%;
            max-height: 100%;
            display: block;
            background: transparent;
          }}

          .name {{
            display: inline-block;
            vertical-align: middle;
            max-width: calc(100% - 150px); /* crude but works in wkhtml */
            font-size: 24px;
            line-height: 1.35;
            font-weight: 600;
            letter-spacing: 0.01em;
            color: #0b1220;
            white-space: normal;
            word-break: break-word;
            /* avoid text-wrap/hyphens for compatibility */
          }}
        </style>
        """

        # Build cards (embed local files as data: URIs to avoid path issues)
        cards = []
        for index, channel_data in enumerate(self.active_streams):
            num, icon, name = channel_data
            src = icon if icon.startswith(("http://","https://","data:")) else self.file_to_data_uri(icon)
            card_class_name = "card"
            if index % 2 == 0:
                card_class_name = "card_even"
            cards.append(
                f"""<div class="{card_class_name}">
                      <div class="chan">{num}</div>
                      <div class="icon"><img src="{src}" alt="icon"></div>
                      <div class="name">{name}</div>
                    </div>"""
            )
        self.logger.debug(f"Generated {len(cards)} channel cards for HTML.")
        self.logger.debug(f"Active streams: {cards}")
        return f"""<!doctype html><html><head><meta charset="utf-8">{style}</head>
        <body>
          <div class="wrap">
            <h1>{self.title}</h1>
            <div class="desc">{self.description}</div>

            <div class="grid">
              {''.join(cards)}
            </div>
          </div>
        </body></html>"""

    @staticmethod
    def _find_wkhtmltoimage() -> str:
        exe = shutil.which("wkhtmltoimage")
        if not exe:
            raise ImportError(
                "wkhtmltoimage not found. Install via:\n"
                "  sudo apt-get install -y wkhtmltopdf   # provides wkhtmltoimage\n"
                "or use the upstream .deb from wkhtmltopdf.org."
            )
        return exe

    def generate(self) -> None:
        """
        Render the HTML to a 1920x1080 JPG using wkhtmltoimage.
        """
        wkhtml = self._find_wkhtmltoimage()

        html = self.html_doc()
        os.makedirs(os.path.dirname(os.path.abspath(self.out_path)) or ".", exist_ok=True)

        with tempfile.TemporaryDirectory() as td:
            html_file = Path(td, "in.html")
            tmp_out = Path(td, "out.jpg")

            html_file.write_text(html, encoding="utf-8")

            # Arguments tuned for fixed 1920x1080 and robust remote image loading.
            cmd = [
                wkhtml,
                "--quiet",
                "--format", "jpg",
                "--quality", "92",
                "--width", "1920",
                "--height", "1080",
                "--disable-smart-width",            # respect width
                "--enable-local-file-access",       # allow local data/file refs if any
                "--load-error-handling", "ignore",  # don't fail on missing assets
                "--javascript-delay", "1200",       # small wait for images/CDNs
                str(html_file),
                str(tmp_out),
            ]

            subprocess.run(cmd, check=True)

            # Move result into place
            Path(self.out_path).write_bytes(tmp_out.read_bytes())
            self.logger.info("Wrote %s", self.out_path)
            self.logger.info(f"Wrote {self.out_path}")


if __name__ == "__main__":
    # Example usage
    gen = ActiveStreamImgGen(
        title="Sorry, this channel is unavailable.",
        description="While this channel is not currently active, here are some other channels you can watch.",
        out_path="too_many_streams.jpg",
        html_cols=3,
    )
    gen.get_active_streams()
    gen.generate()
