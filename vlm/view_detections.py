"""
Interactive browser-based viewer for VLM bounding-box detections on panoramic images.

Usage:
    python vlm/view_detections.py \
        --input kinshicho_c/kinshicho_c_route.json \
        --detections output/kinshicho_c \
        --image-dir kinshicho_c

Images are rotated on-the-fly using rotate_panorama from geo_utils.py,
so bounding boxes align with the displayed image.
Frame ordering comes from the input route JSON (sorted by segment then frame_order).

Navigate with left/right arrow keys or the on-screen buttons.
"""

import argparse
import io
import json
import os
import sys
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image

from geo_utils import load_orientation_lookup, rotate_panorama


def load_ordered_frames(input_json):
    """Return a flat ordered list of (frame_id, is_reverse, turn_angle) from the route JSON.

    turn_angle is only set when the previous frame in the segment had
    is_turn_warning=True, otherwise it is 0.
    """
    with open(input_json) as f:
        data = json.load(f)

    frames = []
    for segment_id in sorted(data.keys(), key=lambda s: data[s][0].get("segment_number", 0)):
        segment_frames = sorted(data[segment_id], key=lambda fr: fr["frame_order"])
        for i, fr in enumerate(segment_frames):
            is_reverse = fr.get("direction", "forward") == "reverse"
            prev_warned = (i > 0 and segment_frames[i - 1].get("is_turn_warning", False))
            turn_angle = (fr.get("turn_angle", 0.0) or 0.0) if prev_warned else 0.0
            frames.append((fr["frame_id"], is_reverse, turn_angle))
    return frames


def find_image(image_dir, frame_id):
    """Find the image file for a frame_id. Checks subdir/frame_id.jpg and flat frame_id.jpg."""
    prefix = frame_id.rsplit("_", 1)[0]  # e.g. GS010397

    # Try subdir first: image_dir/GS010397/GS010397_0000.jpg
    candidate = os.path.join(image_dir, prefix, f"{frame_id}.jpg")
    if os.path.exists(candidate):
        return candidate

    # Flat: image_dir/GS010397_0000.jpg
    candidate = os.path.join(image_dir, f"{frame_id}.jpg")
    if os.path.exists(candidate):
        return candidate

    return None


def rotate_and_encode(pano_path, frame_id, orientation_lookup, is_reverse,
                      turn_angle=0.0):
    """Load a panorama, rotate so walking direction is centered, return JPEG bytes."""
    img = np.array(Image.open(pano_path).convert("RGB"))
    img = rotate_panorama(img, frame_id, orientation_lookup, is_reverse)
    if turn_angle:
        shift_x = -round(turn_angle / 360.0 * img.shape[1])
        img = np.roll(img, shift_x, axis=1)
    pil_img = Image.fromarray(img.astype(np.uint8))
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def build_viewer_html(frame_ids, detections, image_dir, frame_info_map=None):
    """Build the HTML/JS for the viewer. Image data is served via /image?id=frame_id."""
    frame_data = []
    for fid in frame_ids:
        dets = detections.get(fid, [])
        has_image = find_image(image_dir, fid) is not None
        turn_angle = 0.0
        if frame_info_map and fid in frame_info_map:
            turn_angle = frame_info_map[fid][1]
        frame_data.append({
            "frame_id": fid,
            "detections": dets,
            "has_image": has_image,
            "turn_angle": turn_angle,
        })

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>VLM Detection Viewer</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #1a1a1a; color: #eee; font-family: system-ui, sans-serif; }}
  #top-bar {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 20px; background: #222; border-bottom: 1px solid #444;
  }}
  #top-bar h2 {{ font-size: 16px; font-weight: 500; }}
  #nav {{ display: flex; gap: 10px; align-items: center; }}
  #nav button {{
    background: #444; color: #eee; border: none; padding: 6px 16px;
    border-radius: 4px; cursor: pointer; font-size: 14px;
  }}
  #nav button:hover {{ background: #555; }}
  #nav button:disabled {{ opacity: 0.3; cursor: default; }}
  #counter {{ font-size: 14px; color: #aaa; min-width: 100px; text-align: center; }}
  #viewer {{
    display: flex; flex-direction: column; align-items: center;
    padding: 20px; gap: 16px;
  }}
  #canvas-wrap {{
    position: relative; display: inline-block;
    max-width: 95vw; max-height: 75vh;
  }}
  canvas {{ max-width: 95vw; max-height: 75vh; display: block; }}
  #info {{
    background: #222; border-radius: 6px; padding: 12px 16px;
    max-width: 900px; width: 100%; font-size: 13px; line-height: 1.5;
  }}
  #info .frame-id {{ color: #8cf; font-weight: 600; margin-bottom: 6px; }}
  .det {{ margin: 6px 0; padding: 6px 10px; background: #2a2a2a; border-radius: 4px; }}
  .det-desc {{ color: #ccc; }}
  .det-score {{ color: #fc6; font-weight: 600; }}
  .det-bbox {{ color: #888; font-size: 12px; }}
  .no-det {{ color: #888; font-style: italic; }}
  #tooltip {{
    display: none; position: fixed; background: rgba(0,0,0,0.85); color: #eee;
    padding: 8px 12px; border-radius: 4px; font-size: 12px; max-width: 350px;
    pointer-events: none; z-index: 100;
  }}
</style>
</head>
<body>
<div id="top-bar">
  <h2>VLM Detection Viewer</h2>
  <div id="nav">
    <button id="prev-btn" onclick="go(-1)">&larr; Prev</button>
    <span id="counter"></span>
    <button id="next-btn" onclick="go(1)">Next &rarr;</button>
  </div>
</div>
<div id="viewer">
  <div id="canvas-wrap">
    <canvas id="canvas"></canvas>
  </div>
  <div id="info"></div>
</div>
<div id="tooltip"></div>

<script>
const FRAMES = {json.dumps(frame_data)};
let idx = 0;
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
const tooltip = document.getElementById('tooltip');

// Color palette for bounding boxes
const COLORS = ['#ff4444', '#44aaff', '#44ff44', '#ffaa00', '#ff44ff', '#00ffcc'];

function go(delta) {{
  idx = Math.max(0, Math.min(FRAMES.length - 1, idx + delta));
  render();
}}

function render() {{
  const frame = FRAMES[idx];
  document.getElementById('counter').textContent = (idx + 1) + ' / ' + FRAMES.length;
  document.getElementById('prev-btn').disabled = idx === 0;
  document.getElementById('next-btn').disabled = idx === FRAMES.length - 1;

  // Info panel
  let html = '<div class="frame-id">' + frame.frame_id + '</div>';
  if (frame.detections.length === 0) {{
    html += '<div class="no-det">No detections</div>';
  }} else {{
    frame.detections.forEach((d, i) => {{
      const color = COLORS[i % COLORS.length];
      const cat = d.category_label || ('cat ' + d.category);
      let validTag = '';
      if (d.dedup_removed === true) {{
        validTag = '<span style="color:#f80;font-weight:600"> &#10007; dedup removed</span>';
      }} else if (d.is_valid === true) {{
        validTag = '<span style="color:#4f4;font-weight:600"> &#10003; valid</span>';
      }} else if (d.is_valid === false) {{
        validTag = '<span style="color:#f44;font-weight:600"> &#10007; invalid</span>';
      }}
      let reasonLine = '';
      if (d.reason_is_valid) {{
        reasonLine = '<div class="det-bbox" style="color:#aaa;font-style:italic">' + d.reason_is_valid + '</div>';
      }}
      html += '<div class="det">' +
        '<span class="det-score" style="color:' + color + '">[' + (i+1) + '] ' + cat + '</span>' + validTag + ' &mdash; ' +
        '<span class="det-desc">' + d.description + '</span>' +
        reasonLine +
        '<div class="det-bbox">color: ' + (d.color_prominence || 0).toFixed(2) +
        ' &middot; shape: ' + (d.shape_prominence || 0).toFixed(2) +
        ' &middot; familiarity: ' + (d.semantic_familiarity || 0).toFixed(2) +
        ' &middot; bbox: [' + d.bbox.join(', ') + ']</div>' +
        '</div>';
    }});
  }}
  document.getElementById('info').innerHTML = html;

  // Load image
  if (!frame.has_image) {{
    canvas.width = 800; canvas.height = 400;
    ctx.fillStyle = '#333';
    ctx.fillRect(0, 0, 800, 400);
    ctx.fillStyle = '#888';
    ctx.font = '20px system-ui';
    ctx.textAlign = 'center';
    ctx.fillText('Image not found: ' + frame.frame_id, 400, 200);
    return;
  }}

  const img = new Image();
  img.onload = () => {{
    canvas.width = img.width;
    canvas.height = img.height;
    ctx.drawImage(img, 0, 0);
    drawBoxes(frame.detections, img.width, img.height);
  }};
  img.src = '/image?id=' + encodeURIComponent(frame.frame_id);
}}

function drawBoxes(dets, w, h) {{
  dets.forEach((d, i) => {{
    const color = COLORS[i % COLORS.length];
    // bbox is [y_min, x_min, y_max, x_max] in 0-1000 coords (yaw-only space)
    const [yMin, xMin, yMax, xMax] = d.bbox;
    const x1 = (xMin / 1000) * w;
    const y1 = (yMin / 1000) * h;
    const x2 = (xMax / 1000) * w;
    const y2 = (yMax / 1000) * h;

    ctx.strokeStyle = color;
    ctx.lineWidth = 20;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

    // Diagonal strikethrough if marked invalid or dedup-removed
    if (d.is_valid === false || d.dedup_removed === true) {{
      ctx.strokeStyle = d.dedup_removed ? '#ff8800' : '#ff0000';
      ctx.lineWidth = 6;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(x2, y1);
      ctx.lineTo(x1, y2);
      ctx.stroke();
    }}

    // Label
    const label = '[' + (i+1) + '] ' + (d.category_label || 'cat ' + d.category);
    ctx.font = 'bold 50px system-ui';
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = color;
    ctx.fillRect(x1, y1 - 20, tw + 8, 20);
    ctx.fillStyle = '#000';
    ctx.fillText(label, x1 + 4, y1 - 5);
  }});
}}

// Hover tooltip on canvas
canvas.addEventListener('mousemove', (e) => {{
  const frame = FRAMES[idx];
  if (!frame.has_image || frame.detections.length === 0) {{
    tooltip.style.display = 'none';
    return;
  }}
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const mx = (e.clientX - rect.left) * scaleX;
  const my = (e.clientY - rect.top) * scaleY;

  let hit = null;
  frame.detections.forEach((d, i) => {{
    const [yMin, xMin, yMax, xMax] = d.bbox;
    const x1 = (xMin / 1000) * canvas.width;
    const y1 = (yMin / 1000) * canvas.height;
    const x2 = (xMax / 1000) * canvas.width;
    const y2 = (yMax / 1000) * canvas.height;
    if (mx >= x1 && mx <= x2 && my >= y1 && my <= y2) hit = d;
  }});

  if (hit) {{
    tooltip.style.display = 'block';
    tooltip.style.left = (e.clientX + 12) + 'px';
    tooltip.style.top = (e.clientY + 12) + 'px';
    tooltip.textContent = hit.description + ' (color: ' + (hit.color_prominence || 0).toFixed(2) + ', shape: ' + (hit.shape_prominence || 0).toFixed(2) + ', familiarity: ' + (hit.semantic_familiarity || 0).toFixed(2) + ')';
  }} else {{
    tooltip.style.display = 'none';
  }}
}});
canvas.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});

// Keyboard nav
document.addEventListener('keydown', (e) => {{
  if (e.key === 'ArrowLeft') go(-1);
  else if (e.key === 'ArrowRight') go(1);
}});

render();
</script>
</body>
</html>"""


def make_handler(html_content, image_dir, frame_info_map, orientation_lookup):
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)

            if parsed.path == "/" or parsed.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_content.encode())
                return

            if parsed.path == "/image":
                params = parse_qs(parsed.query)
                frame_id = params.get("id", [None])[0]
                if frame_id:
                    img_path = find_image(image_dir, frame_id)
                    if img_path:
                        is_reverse, turn_angle = frame_info_map.get(frame_id, (False, 0.0))
                        jpeg_bytes = rotate_and_encode(
                            img_path, frame_id, orientation_lookup, is_reverse,
                            turn_angle=turn_angle,
                        )
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.end_headers()
                        self.wfile.write(jpeg_bytes)
                        return

                self.send_error(404)
                return

            self.send_error(404)

        def log_message(self, format, *args):
            pass  # Suppress request logs

    return Handler


def main():
    parser = argparse.ArgumentParser(description="View VLM detections on panoramic images")
    parser.add_argument("--input", required=True, help="Route JSON (for frame ordering)")
    parser.add_argument("--detections", required=True, help="Detections output JSON")
    parser.add_argument("--image-dir", required=True, help="Directory with panoramic images")
    parser.add_argument("--port", type=int, default=8765, help="Port for local server")
    args = parser.parse_args()

    # Load orientation data for rotation
    orientation_lookup = load_orientation_lookup()
    if not orientation_lookup:
        print("Warning: no orientation data found, images will not be rotated")

    frames = load_ordered_frames(args.input)  # list of (frame_id, is_reverse, turn_angle)
    frame_ids = [fid for fid, _, _ in frames]
    frame_info_map = {fid: (is_rev, ta) for fid, is_rev, ta in frames}

    with open(args.detections) as f:
        raw = json.load(f)

    detections = {}
    for batch in raw["batches"].values():
        if batch.get("status") == "completed":
            detections.update(batch.get("results", {}))

    # Filter to only frames that have detections
    det_frame_ids = set(detections.keys())
    frame_ids = [f for f in frame_ids if f in det_frame_ids]

    if not frame_ids:
        print("No frames with detections found. Check that input and detections match.")
        sys.exit(1)

    print(f"Loaded {len(frame_ids)} frames with detections")

    image_dir = os.path.abspath(args.image_dir)
    html = build_viewer_html(frame_ids, detections, image_dir, frame_info_map)
    handler_class = make_handler(html, image_dir, frame_info_map, orientation_lookup)

    server = HTTPServer(("localhost", args.port), handler_class)
    url = f"http://localhost:{args.port}"
    print(f"Serving at {url}")
    print("Press Ctrl+C to stop")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
