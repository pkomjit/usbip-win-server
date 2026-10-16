"""The USBShare icon (USB trident on a blue tile), drawn in code so no image files ship."""

from __future__ import annotations

from PIL import Image, ImageDraw


def make_icon(size: int = 256, active: bool = True) -> Image.Image:
    """Blue tile while sharing (active), grey when stopped."""
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, s - 8, s - 8), radius=48,
                        fill=(0, 102, 204, 255) if active else (120, 120, 120, 255))
    w, white, cx = 14, (255, 255, 255, 255), s // 2
    d.line((cx, 214, cx, 58), fill=white, width=w)  # trunk
    d.polygon([(cx, 34), (cx - 22, 70), (cx + 22, 70)], fill=white)  # arrow head
    d.line((cx, 170, cx - 58, 130, cx - 58, 104), fill=white, width=w, joint="curve")
    d.ellipse((cx - 76, 78, cx - 40, 114), fill=white)  # round tip
    d.line((cx, 150, cx + 58, 112, cx + 58, 92), fill=white, width=w, joint="curve")
    d.rectangle((cx + 42, 64, cx + 74, 96), fill=white)  # square tip
    d.ellipse((cx - 24, 196, cx + 24, 244), fill=white)  # base
    return img if size == s else img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":  # regenerate packaging/usbshare.ico
    import sys
    make_icon().save(sys.argv[1], sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
