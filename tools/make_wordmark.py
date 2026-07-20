"""Compose the pixel logo with a 'Hindsight' wordmark into a single
transparent PNG for use with st.logo (top-left of app + sidebar)."""

from PIL import Image, ImageDraw, ImageFont

LOGO = "assets/logo.png"
OUT = "assets/wordmark.png"

# Match the logo's brown outline color.
TEXT_COLOR = (139, 62, 15, 255)  # deep brown from the sprite

logo = Image.open(LOGO).convert("RGBA")

# Scale the logo down to a tidy icon height.
target_h = 96
scale = target_h / logo.height
logo = logo.resize((round(logo.width * scale), target_h), Image.Resampling.NEAREST)

font = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", 72, index=1)  # bold

text = "Hindsight"
tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
bbox = tmp.textbbox((0, 0), text, font=font)
text_w = bbox[2] - bbox[0]
text_h = bbox[3] - bbox[1]

gap = 20
pad = 8
canvas_w = int(logo.width + gap + text_w + pad * 2)
canvas_h = int(max(logo.height, text_h) + pad * 2)

canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
canvas.alpha_composite(logo, (pad, (canvas_h - logo.height) // 2))

draw = ImageDraw.Draw(canvas)
text_y = (canvas_h - text_h) // 2 - bbox[1]
draw.text((pad + logo.width + gap, text_y), text, font=font, fill=TEXT_COLOR)

canvas.save(OUT)
print(f"wrote {OUT} ({canvas_w}x{canvas_h})")
