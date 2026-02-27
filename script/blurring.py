import cv2
import os
from pathlib import Path
from facenet_pytorch import MTCNN
import torch

# Initialize face detector
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
mtcnn = MTCNN(keep_all=True, device=device, min_face_size=20, thresholds=[0.5, 0.6, 0.6])

def blur_faces(image_path, output_path):
    img = cv2.imread(image_path)
    if img is None:
        print(f"Could not read: {image_path}")
        return False
    
    h, w = img.shape[:2]
    
    # Convert BGR to RGB for MTCNN
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    
    # Detect faces at multiple scales
    all_boxes = []
    scales = [1.0, 0.5, 0.75]
    
    for scale in scales:
        if scale != 1.0:
            scaled = cv2.resize(img_rgb, (int(w * scale), int(h * scale)))
        else:
            scaled = img_rgb
        
        boxes, _ = mtcnn.detect(scaled)
        
        if boxes is not None:
            for box in boxes:
                x1, y1, x2, y2 = box
                if scale != 1.0:
                    x1, y1, x2, y2 = x1/scale, y1/scale, x2/scale, y2/scale
                all_boxes.append((int(x1), int(y1), int(x2), int(y2)))
    
    # Remove duplicates
    unique_boxes = remove_duplicates(all_boxes)
    
    # Blur faces
    for (x1, y1, x2, y2) in unique_boxes:
        # Expand box
        pad_w = int((x2 - x1) * 0.2)
        pad_h = int((y2 - y1) * 0.2)
        x1 = max(0, x1 - pad_w)
        y1 = max(0, y1 - pad_h)
        x2 = min(w, x2 + pad_w)
        y2 = min(h, y2 + pad_h)
        
        face_region = img[y1:y2, x1:x2]
        if face_region.size > 0:
            blur_amount = max(99, (x2 - x1) // 2 * 2 + 1)
            blurred = cv2.GaussianBlur(face_region, (blur_amount, blur_amount), 30)
            img[y1:y2, x1:x2] = blurred
    
    cv2.imwrite(output_path, img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"Processed: {image_path} — {len(unique_boxes)} face(s) blurred")
    return True

def remove_duplicates(boxes, iou_thresh=0.3):
    if not boxes:
        return []
    unique = []
    for box in boxes:
        is_dup = False
        for ubox in unique:
            if iou(box, ubox) > iou_thresh:
                is_dup = True
                break
        if not is_dup:
            unique.append(box)
    return unique

def iou(b1, b2):
    x1 = max(b1[0], b2[0])
    y1 = max(b1[1], b2[1])
    x2 = min(b1[2], b2[2])
    y2 = min(b1[3], b2[3])
    inter = max(0, x2-x1) * max(0, y2-y1)
    a1 = (b1[2]-b1[0]) * (b1[3]-b1[1])
    a2 = (b2[2]-b2[0]) * (b2[3]-b2[1])
    return inter / (a1 + a2 - inter) if (a1 + a2 - inter) > 0 else 0

# Setup
input_folder = "/Users/chuli/Desktop/newly-exported-photos"
output_folder = "/Users/chuli/Desktop/newly-exported-photos/blurred_mtcnn"
os.makedirs(output_folder, exist_ok=True)

extensions = {'.jpg', '.jpeg', '.png', '.webp'}
files = [f for f in Path(input_folder).iterdir() if f.suffix.lower() in extensions and f.is_file()]
total = len(files)

for i, file in enumerate(files, 1):
    print(f"[{i}/{total}] ", end="")
    blur_faces(str(file), os.path.join(output_folder, file.name))

print(f"\nDone!")