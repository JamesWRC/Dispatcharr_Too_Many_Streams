from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class PluginConfig:
    stream_title: str = "Sorry, this channel is unavailable."
    stream_description: str = "While this channel is not currently available, here are some other channels you can watch."
    stream_channel_cols: int = 5
    tms_image_path: Optional[str] = None
    tms_log_level: str = "INFO"
    
    # Advanced / Performance
    video_encoder: str = "libx264"
    
    # Theme Colors
    theme_bg_color: str = "#0F172A"
    theme_card_bg_color: str = "#1E293B"
    theme_card_border_color: str = "#334155"
    theme_text_color: str = "#F8FAFC"
    theme_accent_color: str = "#38BDF8"
    theme_accent_text_color: str = "#0F172A"

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            stream_title=str(data.get("stream_title", cls.stream_title)),
            stream_description=str(data.get("stream_description", cls.stream_description)),
            stream_channel_cols=int(data.get("stream_channel_cols", cls.stream_channel_cols)),
            tms_image_path=data.get("tms_image_path", cls.tms_image_path),
            tms_log_level=str(data.get("tms_log_level", cls.tms_log_level)).upper(),
            
            video_encoder=str(data.get("video_encoder", cls.video_encoder)),
            
            theme_bg_color=str(data.get("theme_bg_color", cls.theme_bg_color)),
            theme_card_bg_color=str(data.get("theme_card_bg_color", cls.theme_card_bg_color)),
            theme_card_border_color=str(data.get("theme_card_border_color", cls.theme_card_border_color)),
            theme_text_color=str(data.get("theme_text_color", cls.theme_text_color)),
            theme_accent_color=str(data.get("theme_accent_color", cls.theme_accent_color)),
            theme_accent_text_color=str(data.get("theme_accent_text_color", cls.theme_accent_text_color)),
        )

    def dict(self):
        return asdict(self)
