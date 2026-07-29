import os
import io
import ftplib
import requests
from flask import Flask, request, jsonify, render_template, send_from_directory
from PIL import Image, ImageSequence

app = Flask(__name__, static_folder='static', template_folder='.')

# ==========================================
# 환경 설정 (.env 환경변수 활용 권장)
# ==========================================
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'processed_images')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CAFE24_MALL_ID = os.getenv("CAFE24_MALL_ID", "your_mall_id")
CAFE24_ACCESS_TOKEN = os.getenv("CAFE24_ACCESS_TOKEN", "your_access_token")

FTP_HOST = os.getenv("FTP_HOST", f"{CAFE24_MALL_ID}.cafe24.com")
FTP_USER = os.getenv("FTP_USER", "your_ftp_id")
FTP_PASS = os.getenv("FTP_PASS", "your_ftp_password")
FTP_PORT = int(os.getenv("FTP_PORT", 21))
FTP_BASE_URL = f"http://{CAFE24_MALL_ID}.cafe24.com/web/upload/thumbnail/"

# ==========================================
# 1:1 -> 1:1.4 (1000x1400) 가공 로직
# ==========================================
def pad_to_1400_ratio(img, target_w=1000, target_h=1400):
    """1:1 이미지를 1000x1400 Canvas 중앙에 배치 (상하 여백 흰색 채움)"""
    img = img.convert('RGBA')
    orig_w, orig_h = img.size

    # Canvas 생성 (흰색 배경)
    canvas = Image.new('RGBA', (target_w, target_h), (255, 255, 255, 255))

    # 가로 1000 맞춤 리사이즈
    if orig_w != target_w:
        new_h = int(orig_h * (target_w / orig_w))
        img = img.resize((target_w, new_h), Image.Resampling.LANCZOS)

    # 상하 중앙 배치
    offset_y = (target_h - img.height) // 2
    canvas.paste(img, (0, offset_y), mask=img if img.mode == 'RGBA' else None)
    return canvas.convert('RGB')

def process_image_bytes(image_bytes, filename):
    """GIF Multi-frame / JPG / PNG 처리"""
    ext = os.path.splitext(filename)[1].lower()
    img = Image.open(io.BytesIO(image_bytes))
    output_filename = f"processed_{filename}"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)

    if ext == '.gif' and getattr(img, "is_animated", False):
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(img):
            durations.append(frame.info.get('duration', 100))
            frames.append(pad_to_1400_ratio(frame))

        frames[0].save(
            output_path,
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0
        )
    else:
        processed_img = pad_to_1400_ratio(img)
        processed_img.save(output_path, quality=95)

    return output_filename

def upload_to_ftp(local_file_path, remote_filename):
    """Web FTP 업로드 함수"""
    try:
        ftp = ftplib.FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(FTP_USER, FTP_PASS)
        
        try:
            ftp.cwd('/web/upload/thumbnail/')
        except ftplib.error_perm:
            ftp.mkd('/web/upload/thumbnail/')
            ftp.cwd('/web/upload/thumbnail/')

        with open(local_file_path, 'rb') as f:
            ftp.storbinary(f'STOR {remote_filename}', f)
        ftp.quit()
        return f"{FTP_BASE_URL}{remote_filename}"
    except Exception as e:
        raise Exception(f"FTP 업로드 실패: {str(e)}")

# ==========================================
# API Endpoints
# ==========================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/processed_images/<filename>')
def serve_processed_image(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# [1단계 API] 썸네일 변환 (미리보기 생성)
@app.route('/api/convert', methods=['POST'])
def convert_thumbnails():
    data = request.json or {}
    products = data.get('products', [])
    results = []

    for item in products:
        p_no = item.get('product_no')
        img_url = item.get('image_url')
        filename = item.get('filename', f"{p_no}.jpg")

        try:
            resp = requests.get(img_url, timeout=10)
            resp.raise_for_status()

            output_file = process_image_bytes(resp.content, filename)
            preview_url = f"/static/processed_images/{output_file}"

            results.append({
                "product_no": p_no,
                "status": "SUCCESS",
                "preview_url": preview_url,
                "processed_filename": output_file
            })
        except Exception as e:
            results.append({"product_no": p_no, "status": "FAIL", "error": str(e)})

    return jsonify({"success": True, "data": results})

# [2단계 API] FTP 업로드 & 카페24 전송
@app.route('/api/send-cafe24', methods=['POST'])
def send_to_cafe24():
    """
    JSON 구조 예시:
    {
      "mode": "model",  // "product" 또는 "model"
      "items": [
         {
           "product_no": "10",
           "processed_filename": "processed_10.jpg",
           "add_images": ["https://.../add1.jpg"] // 제품컷 변환 시 필요
         }
      ]
    }
    """
    data = request.json or {}
    mode = data.get('mode', 'product')
    items = data.get('items', [])

    success_list = []
    fail_list = []

    headers = {
        "Authorization": f"Bearer {CAFE24_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Cafe24-Api-Version": "2024-03-01"
    }

    for item in items:
        p_no = item.get('product_no')
        filename = item.get('processed_filename')
        local_path = os.path.join(UPLOAD_FOLDER, filename)

        try:
            # 1. 대표 썸네일 FTP 업로드
            ftp_main_url = upload_to_ftp(local_path, filename)

            images_payload = {
                "detail_image": ftp_main_url,
                "big_image": ftp_main_url
            }

            # 2. 추가이미지 제어 조건
            if mode == 'model':
                # [모델컷 모드] 추가이미지 일괄 삭제
                images_payload["add_image"] = []
            elif mode == 'product':
                # [제품컷 모드] 추가이미지도 상하여백 가공 후 업데이트
                raw_add_images = item.get('add_images', [])
                processed_add_urls = []

                for idx, add_url in enumerate(raw_add_images):
                    add_resp = requests.get(add_url, timeout=10)
                    add_resp.raise_for_status()

                    add_filename = f"{p_no}_add_{idx}.jpg"
                    out_add_file = process_image_bytes(add_resp.content, add_filename)
                    out_add_path = os.path.join(UPLOAD_FOLDER, out_add_file)

                    ftp_add_url = upload_to_ftp(out_add_path, out_add_file)
                    processed_add_urls.append(ftp_add_url)

                images_payload["add_image"] = processed_add_urls

            # 3. 카페24 API 호출
            api_url = f"https://{CAFE24_MALL_ID}.cafe24api.com/api/v2/admin/products/{p_no}"
            res = requests.put(api_url, json={"request": {"images": images_payload}}, headers=headers, timeout=15)

            if res.status_code == 200:
                success_list.append({"product_no": p_no, "url": ftp_main_url})
            else:
                err_msg = res.json().get('error', {}).get('message', 'API 호출 실패')
                fail_list.append({"product_no": p_no, "reason": err_msg})

        except Exception as e:
            fail_list.append({"product_no": p_no, "reason": str(e)})

    return jsonify({
        "success": True,
        "summary": {"total": len(items), "success_count": len(success_list), "fail_count": len(fail_list)},
        "failures": fail_list
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)