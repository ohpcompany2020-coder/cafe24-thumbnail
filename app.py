import os
import io
import time
import html
import base64
import logging
import threading
import ftplib
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.exceptions import HTTPException
from PIL import Image, ImageSequence

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=template_dir)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """API 라우트에서 처리하지 못한 예외가 Flask 기본 HTML 에러 페이지로 새어나가지 않도록
    항상 JSON으로 응답한다. (프런트가 res.json()을 그대로 파싱하므로 HTML이 내려가면
    "Unexpected token '<'" 같은 파싱 에러로 이어짐)"""
    if isinstance(e, HTTPException):
        return e
    logger.exception("처리되지 않은 예외로 요청 실패")
    return jsonify({"success": False, "error": f"서버 내부 오류가 발생했습니다: {str(e)}"}), 500

# ==========================================
# 환경 설정 (.env 환경변수 활용)
# ==========================================
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'processed_images')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CAFE24_MALL_ID = os.getenv("CAFE24_MALL_ID", "your_mall_id")
CAFE24_ACCESS_TOKEN = os.getenv("CAFE24_ACCESS_TOKEN", "your_access_token")
CAFE24_REFRESH_TOKEN = os.getenv("CAFE24_REFRESH_TOKEN", "")
CAFE24_CLIENT_ID = os.getenv("CAFE24_CLIENT_ID", "")
CAFE24_CLIENT_SECRET = os.getenv("CAFE24_CLIENT_SECRET", "")
# 카페24 개발자센터 기준 최신 API 버전(YYYY-MM-DD). 카페24가 주기적으로 버전을 갱신하므로
# https://developers.cafe24.com 의 버전 안내를 주기적으로 확인해 CAFE24_API_VERSION로 갱신할 것.
CAFE24_API_VERSION = os.getenv("CAFE24_API_VERSION", "2026-03-01")
CAFE24_API_BASE = f"https://{CAFE24_MALL_ID}.cafe24api.com/api/v2/admin"
CAFE24_TOKEN_URL = f"https://{CAFE24_MALL_ID}.cafe24api.com/api/v2/oauth/token"
CAFE24_AUTHORIZE_URL = f"https://{CAFE24_MALL_ID}.cafe24api.com/api/v2/oauth/authorize"
# OAuth 재인증 시 카페24가 콜백을 보낼 주소 (카페24 개발자센터에 등록된 Redirect URI와 동일해야 함)
CAFE24_REDIRECT_URI = os.getenv("CAFE24_REDIRECT_URI", "")
CAFE24_OAUTH_SCOPE = os.getenv("CAFE24_OAUTH_SCOPE", "mall.read_product,mall.write_product")
# 재인증 URL을 직접 고정하고 싶을 때 사용 (선택, 미설정 시 위 정보로 자동 생성)
CAFE24_REAUTH_URL = os.getenv("CAFE24_REAUTH_URL", "")

FTP_HOST = os.getenv("FTP_HOST", f"{CAFE24_MALL_ID}.cafe24.com")
FTP_USER = os.getenv("FTP_USER", "your_ftp_id")
FTP_PASS = os.getenv("FTP_PASS", "your_ftp_password")
FTP_PORT = int(os.getenv("FTP_PORT", 21))
FTP_BASE_URL = f"http://{CAFE24_MALL_ID}.cafe24.com/web/upload/thumbnail/"

if "your_" in CAFE24_MALL_ID or "your_" in CAFE24_ACCESS_TOKEN:
    logger.warning("CAFE24_MALL_ID / CAFE24_ACCESS_TOKEN이 기본값입니다. .env에 실제 운영 값을 설정해 주세요.")

# Access/Refresh Token은 갱신될 수 있으므로 프로세스 메모리에 별도로 보관 (재시작 시 .env 값으로 초기화됨)
_cafe24_token = {
    "access_token": CAFE24_ACCESS_TOKEN,
    "refresh_token": CAFE24_REFRESH_TOKEN,
}
_cafe24_token_lock = threading.Lock()


class Cafe24ReauthRequired(Exception):
    """Refresh Token까지 만료/무효화되어 카페24 앱 재인증이 필요할 때 발생"""
    pass


def cafe24_headers():
    return {
        "Authorization": f"Bearer {_cafe24_token['access_token']}",
        "Content-Type": "application/json",
        "X-Cafe24-Api-Version": CAFE24_API_VERSION
    }


def refresh_cafe24_access_token():
    """CAFE24_REFRESH_TOKEN으로 Access Token을 재발급받아 메모리/환경변수에 반영한다."""
    with _cafe24_token_lock:
        refresh_token = _cafe24_token.get("refresh_token")
        if not refresh_token or not CAFE24_CLIENT_ID or not CAFE24_CLIENT_SECRET:
            raise Cafe24ReauthRequired("Refresh Token 또는 Client 인증정보가 설정되어 있지 않습니다.")

        basic_credential = base64.b64encode(f"{CAFE24_CLIENT_ID}:{CAFE24_CLIENT_SECRET}".encode()).decode()
        try:
            res = requests.post(
                CAFE24_TOKEN_URL,
                headers={
                    "Authorization": f"Basic {basic_credential}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                timeout=15
            )
        except Exception as e:
            logger.exception("카페24 Access Token 갱신 요청 실패")
            raise Cafe24ReauthRequired(f"토큰 갱신 요청 중 오류가 발생했습니다: {str(e)}")

        if res.status_code != 200:
            logger.error(f"카페24 Access Token 갱신 실패 ({res.status_code}): {res.text}")
            raise Cafe24ReauthRequired("Refresh Token이 만료되었거나 갱신에 실패했습니다.")

        payload = res.json()
        new_access_token = payload.get("access_token")
        new_refresh_token = payload.get("refresh_token", refresh_token)
        if not new_access_token:
            raise Cafe24ReauthRequired("토큰 갱신 응답에 access_token이 없습니다.")

        _cafe24_token["access_token"] = new_access_token
        _cafe24_token["refresh_token"] = new_refresh_token
        # 프로세스 재시작 전까지는 os.environ도 함께 갱신해 다른 코드에서 참조하더라도 최신값을 보게 함
        os.environ["CAFE24_ACCESS_TOKEN"] = new_access_token
        os.environ["CAFE24_REFRESH_TOKEN"] = new_refresh_token
        logger.info("카페24 Access Token 갱신 완료")
        return new_access_token


def build_cafe24_authorize_url(state=None):
    """CAFE24_CLIENT_ID/CAFE24_REDIRECT_URI로 카페24 OAuth 2.0 인증 페이지 URL을 생성한다.
    필수 설정이 없으면 None을 반환한다."""
    if not CAFE24_CLIENT_ID or not CAFE24_REDIRECT_URI:
        return None

    params = {
        "response_type": "code",
        "client_id": CAFE24_CLIENT_ID,
        "redirect_uri": CAFE24_REDIRECT_URI,
        "scope": CAFE24_OAUTH_SCOPE,
    }
    if state:
        params["state"] = state
    return f"{CAFE24_AUTHORIZE_URL}?{urlencode(params)}"


def get_cafe24_reauth_url():
    """운영자에게 안내할 재인증 URL. CAFE24_REAUTH_URL이 설정되어 있으면 그 값을 우선 사용한다."""
    return CAFE24_REAUTH_URL or build_cafe24_authorize_url()


def cafe24_api_request(method, url, **kwargs):
    """카페24 Admin API 호출 공용 래퍼. 401 응답 시 Access Token을 한 번 갱신 후 재시도한다."""
    res = requests.request(method, url, headers=cafe24_headers(), **kwargs)

    if res.status_code == 401:
        logger.warning(f"카페24 API 401 응답 - Access Token 갱신 후 재시도 ({method} {url})")
        refresh_cafe24_access_token()  # 실패 시 Cafe24ReauthRequired 발생, 호출부로 전파
        res = requests.request(method, url, headers=cafe24_headers(), **kwargs)

    return res

# ==========================================
# 1:1 -> 1:1.4 (1000x1400) 가공 로직
# ==========================================
def pad_to_1400_ratio(img, target_w=1000, target_h=1400, bg_color=(255, 255, 255)):
    """[제품컷 모드] 정방향(1:1) 이미지를 비율 유지하며 가로 1000px로 맞추고 상하에 흰색 여백을 채워 1000x1400 생성"""
    img = img.convert('RGBA')
    orig_w, orig_h = img.size
    
    # 가로(1000px)에 맞추어 비율 유지 축소/확대 (1:1 이미지의 경우 1000x1000이 됨)
    scale = target_w / orig_w
    new_w = target_w
    new_h = int(orig_h * scale)
    
    # 만약 세로가 1400보다 크면 세로에 맞춤
    if new_h > target_h:
        scale = target_h / orig_h
        new_h = target_h
        new_w = int(orig_w * scale)
        
    resized_img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    # 1000x1400 흰색 바탕 도화지 만들기
    background = Image.new('RGBA', (target_w, target_h), bg_color + (255,))
    
    # 중앙에 이미지 배치 (상하 여백 채우기)
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    background.paste(resized_img, (paste_x, paste_y), resized_img)

    # 최종 Output 사이즈가 정확히 (target_w, target_h)인지 보정
    if background.size != (target_w, target_h):
        background = background.resize((target_w, target_h), Image.Resampling.LANCZOS)

    return background.convert('RGB')


def crop_to_1400_ratio(img, target_w=1000, target_h=1400):
    """[모델컷 모드] 1000x1400 세로 비율(1:1.4)을 꽉 채우도록 확대 후 여백 없이 중앙 기준 절삭"""
    img = img.convert('RGBA')
    orig_w, orig_h = img.size
    
    # 1000x1400 비율(cover)을 만족하도록 확대 비율 계산
    scale = max(target_w / orig_w, target_h / orig_h)
    new_w = int(orig_w * scale)
    new_h = int(orig_h * scale)
    
    resized_img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    # 중앙 기준으로 target_w x target_h 만큼 절삭(Crop)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    cropped = resized_img.crop((left, top, left + target_w, top + target_h))

    # 최종 Output 사이즈가 정확히 (target_w, target_h)인지 보정
    if cropped.size != (target_w, target_h):
        cropped = cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)

    return cropped.convert('RGB')

IMAGE_DOWNLOAD_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# onehundredpercent.co.kr(대표도메인)과 newohpcompany.cafe24.com(카페24 기본도메인)은
# 둘 다 같은 우리 쇼핑몰 서버를 가리킨다. 이 서버가 스크립트성 요청(Referer 없음/불일치)을
# 차단하는 것으로 보여, 두 도메인을 Referer 후보로 순서대로 시도한다.
SHOP_REFERER_CANDIDATES = [
    os.getenv('SHOP_PRIMARY_DOMAIN', 'https://onehundredpercent.co.kr/'),
    os.getenv('SHOP_CAFE24_DOMAIN', 'https://newohpcompany.cafe24.com/'),
]

# 업로드 직후 자체 검증(진단용) 단계를 끄고 싶을 때 환경변수로 비활성화 가능
VERIFY_UPLOADED_IMAGE = os.getenv('VERIFY_UPLOADED_IMAGE', 'true').lower() == 'true'

def fetch_image(url, timeout=10):
    """상품 이미지 다운로드.

    우리 쇼핑몰 이미지 서버(onehundredpercent.co.kr / newohpcompany.cafe24.com)가
    Referer/User-Agent가 없는 스크립트성 요청을 403으로 차단하는 것으로 확인되어,
    브라우저 요청처럼 보이도록 헤더를 채우고 Referer는 쇼핑몰 대표도메인으로 고정 시도한다.
    403이 나면 다음 후보 도메인(카페24 기본도메인)으로 재시도한다.
    """
    last_error = None
    for referer in SHOP_REFERER_CANDIDATES:
        headers = {
            'User-Agent': IMAGE_DOWNLOAD_USER_AGENT,
            'Referer': referer
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 403:
            logger.warning(f"[이미지 다운로드 403] url={url} referer={referer} - 다음 후보로 재시도")
            last_error = resp
            continue
        resp.raise_for_status()
        return resp

    # 모든 후보 Referer에서 403이 발생한 경우 마지막 응답으로 에러를 표면화
    last_error.raise_for_status()

def verify_image_dimensions(product_no, field_name, image_url, expected_ratio=1.4, tolerance=0.02, retry_delay=3):
    """카페24가 실제로 반영한 이미지 URL을 다시 받아 PIL로 실제 크기/비율을 로그로 남긴다.
    CDN 캐시로 예전 이미지가 그대로 보이는 경우와, 애초에 잘못된 필드/이미지가
    올라간 경우를 구분하기 위한 진단용 로그일 뿐이다.

    이 함수는 어떤 예외도 상위로 전파하지 않는다 - 검증 실패가 카페24 PUT 요청의
    성공 여부(실제 송신 결과)를 뒤집어서는 안 되기 때문이다.
    """
    if not VERIFY_UPLOADED_IMAGE:
        logger.info(f"[크기 검증] product_no={product_no} 검증 비활성화(VERIFY_UPLOADED_IMAGE=false) - 건너뜀")
        return

    for attempt in (1, 2):
        try:
            resp = fetch_image(image_url, timeout=10)
            with Image.open(io.BytesIO(resp.content)) as img:
                w, h = img.size
            ratio = (h / w) if w else 0
            ok = abs(ratio - expected_ratio) <= tolerance
            logger.info(
                f"[크기 검증] product_no={product_no} field={field_name} url={image_url} "
                f"size={w}x{h} ratio={ratio:.3f} (기대값 {expected_ratio}) "
                f"{'OK' if ok else 'MISMATCH - 여전히 구 이미지이거나 CDN 캐시일 가능성'}"
            )
            return
        except Exception as e:
            if attempt == 1:
                logger.warning(
                    f"[크기 검증] product_no={product_no} field={field_name} 1차 시도 실패({e}), "
                    f"CDN 반영 시차를 감안해 {retry_delay}초 후 재시도"
                )
                time.sleep(retry_delay)
            else:
                logger.warning(
                    f"[크기 검증 실패] product_no={product_no} field={field_name} url={image_url}: {e} "
                    f"(이 검증은 진단 목적이며 카페24 PUT 성공/실패 판정에는 영향 없음)"
                )

def process_image_bytes(image_bytes, filename, mode='product'):
    """GIF Multi-frame / JPG / PNG 처리

    mode='product' -> pad_to_1400_ratio (상하 여백 채우기)
    mode='model'   -> crop_to_1400_ratio (확대 후 중앙 가로 크롭)
    """
    process_fn = crop_to_1400_ratio if mode == 'model' else pad_to_1400_ratio

    ext = os.path.splitext(filename)[1].lower()
    img = Image.open(io.BytesIO(image_bytes))
    output_filename = f"processed_{filename}"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)

    if ext == '.gif' and getattr(img, "is_animated", False):
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(img):
            durations.append(frame.info.get('duration', 100))
            frames.append(process_fn(frame))

        frames[0].save(
            output_path,
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0
        )
    else:
        processed_img = process_fn(img)
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

# [재인증 URL 조회 API] 화면 상단 "카페24 어드민 연동/재인증하기" 버튼이 사용
@app.route('/api/auth/reauth-url', methods=['GET'])
def get_reauth_url():
    reauth_url = get_cafe24_reauth_url()
    if not reauth_url:
        return jsonify({
            "success": False,
            "error": "CAFE24_CLIENT_ID / CAFE24_REDIRECT_URI 설정이 필요합니다."
        }), 400
    return jsonify({"success": True, "reauth_url": reauth_url})

# [OAuth 콜백] 카페24 인증 완료 후 Authorization Code를 Access/Refresh Token으로 교환
@app.route('/api/auth/callback', methods=['GET'])
def cafe24_auth_callback():
    error = request.args.get('error')
    if error:
        logger.error(f"카페24 인증 거부/실패: {error}")
        return f"<h2>카페24 인증에 실패했습니다.</h2><p>{html.escape(error)}</p>", 400

    code = request.args.get('code')
    if not code:
        return "<h2>Authorization Code가 전달되지 않았습니다.</h2>", 400

    if not (CAFE24_CLIENT_ID and CAFE24_CLIENT_SECRET and CAFE24_REDIRECT_URI):
        return "<h2>서버에 CAFE24_CLIENT_ID / CAFE24_CLIENT_SECRET / CAFE24_REDIRECT_URI 설정이 필요합니다.</h2>", 500

    basic_credential = base64.b64encode(f"{CAFE24_CLIENT_ID}:{CAFE24_CLIENT_SECRET}".encode()).decode()
    try:
        res = requests.post(
            CAFE24_TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic_credential}",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CAFE24_REDIRECT_URI
            },
            timeout=15
        )
    except Exception as e:
        logger.exception("카페24 Authorization Code 토큰 교환 요청 실패")
        return f"<h2>토큰 발급 요청 중 오류가 발생했습니다.</h2><p>{html.escape(str(e))}</p>", 502

    if res.status_code != 200:
        logger.error(f"카페24 토큰 발급 실패 ({res.status_code}): {res.text}")
        return f"<h2>토큰 발급에 실패했습니다.</h2><p>{html.escape(res.text)}</p>", 502

    payload = res.json()
    access_token = payload.get('access_token')
    refresh_token = payload.get('refresh_token')
    if not access_token or not refresh_token:
        logger.error(f"카페24 토큰 응답에 access_token/refresh_token 누락: {payload}")
        return "<h2>토큰 응답이 올바르지 않습니다.</h2>", 502

    with _cafe24_token_lock:
        _cafe24_token["access_token"] = access_token
        _cafe24_token["refresh_token"] = refresh_token
        os.environ["CAFE24_ACCESS_TOKEN"] = access_token
        os.environ["CAFE24_REFRESH_TOKEN"] = refresh_token

    logger.info("카페24 신규 인증 완료 - Access/Refresh Token 저장됨")
    return """
        <html><body style="font-family:sans-serif;text-align:center;padding-top:80px;">
            <h2>✅ 카페24 인증이 완료되었습니다.</h2>
            <p>이 창은 자동으로 닫힙니다. 닫히지 않으면 <a href="/">여기</a>를 눌러 돌아가 주세요.</p>
            <script>setTimeout(function() { window.close(); }, 3000);</script>
        </body></html>
    """

# [상품 검색 API] 카페24 Admin API로 상품 목록 조회
@app.route('/api/products/search', methods=['GET'])
def search_products():
    search_type = request.args.get('search_type', 'product_name')  # model_name | product_name | product_code
    keyword = request.args.get('keyword', '').strip()
    limit = min(int(request.args.get('limit', 20)), 100)
    sort = request.args.get('sort', '').strip()
    order = request.args.get('order', '').strip()

    if not keyword:
        return jsonify({"success": False, "error": "검색어를 입력해 주세요."}), 400

    params = {"limit": limit}
    if search_type == 'product_code':
        params['product_code'] = keyword
    elif search_type == 'model_name':
        # 카페24 model_name 파라미터는 완전일치(exact match)이며 등록된 모델명이 모두
        # 대문자이므로, 사용자가 대소문자를 섞어 입력해도 찾을 수 있도록 대문자로 변환한다.
        params['model_name'] = keyword.upper()
    else:
        params['product_name'] = keyword

    if sort:
        params['sort'] = sort
    if order:
        params['order'] = order

    logger.info(f"카페24 상품 검색 요청: search_type={search_type} -> params={params}")

    try:
        res = cafe24_api_request('GET', f"{CAFE24_API_BASE}/products", params=params, timeout=15)
        logger.info(f"카페24 상품 검색 실제 요청 URL: {res.url} (status={res.status_code})")

        if res.status_code >= 400:
            try:
                err_body = res.json()
            except ValueError:
                err_body = {}
            cafe24_error = err_body.get('error') or {}
            detail_msg = (
                (cafe24_error.get('message') if isinstance(cafe24_error, dict) else None)
                or err_body.get('error_description')
                or res.text
                or f"HTTP {res.status_code}"
            )
            logger.error(f"카페24 상품 검색 실패 ({res.status_code}) url={res.url} body={res.text}")
            return jsonify({
                "success": False,
                "error": f"카페24 상품 조회 실패 ({res.status_code}): {detail_msg}",
                "status_code": res.status_code,
                "requested_url": res.url
            }), 502

        raw_products = res.json().get('products', [])
        if not isinstance(raw_products, list):
            raw_products = []

        products = []
        for p in raw_products:
            if not isinstance(p, dict):
                continue

            product_no = p.get('product_no')
            main_image = p.get('detail_image') or p.get('list_image') or ''
            main_image = main_image if isinstance(main_image, str) else ''
            filename = os.path.basename(main_image.split('?')[0]) if main_image else f"{product_no}.jpg"
            ext = os.path.splitext(filename)[1].lstrip('.').upper() or 'JPG'

            # 카페24 API 버전에 따라 추가이미지 필드명이 다를 수 있어 기본값은 빈 배열로 처리
            raw_add_images = p.get('additional_image')
            add_images = [img for img in raw_add_images if img] if isinstance(raw_add_images, list) else []

            products.append({
                "product_no": str(product_no),
                "product_code": p.get('product_code', ''),
                "product_name": p.get('product_name', ''),
                "image_url": main_image,
                "filename": filename,
                "format": ext,
                "add_images": add_images
            })

        return jsonify({"success": True, "data": products, "requested_url": res.url})

    except Cafe24ReauthRequired as e:
        logger.error(f"카페24 재인증 필요: {e}")
        reauth_url = get_cafe24_reauth_url()
        return jsonify({
            "success": False,
            "error": "카페24 토큰 재인증이 필요합니다. 관리자 페이지에서 앱을 재인증해 주세요.",
            "reauth_required": True,
            **({"reauth_url": reauth_url} if reauth_url else {})
        }), 401
    except Exception as e:
        logger.exception("카페24 상품 검색 요청 중 예외 발생")
        return jsonify({"success": False, "error": f"카페24 상품 조회 중 오류가 발생했습니다: {str(e)}"}), 502

# [1단계 API] 썸네일 변환 (미리보기 생성)
@app.route('/api/convert', methods=['POST'])
def convert_thumbnails():
    data = request.json or {}
    mode = data.get('mode', 'product')  # 'product' -> 상하 여백 채우기 / 'model' -> 확대 후 중앙 가로 크롭
    products = data.get('products', [])
    results = []

    for item in products:
        p_no = item.get('product_no')
        img_url = item.get('image_url')
        filename = item.get('filename', f"{p_no}.jpg")

        try:
            resp = fetch_image(img_url, timeout=10)

            output_file = process_image_bytes(resp.content, filename, mode)
            preview_url = f"/static/processed_images/{output_file}"

            results.append({
                "product_no": p_no,
                "status": "SUCCESS",
                "preview_url": preview_url,
                "processed_filename": output_file
            })
        except Exception as e:
            logger.exception(f"상품 변환 실패 (product_no={p_no})")
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

    for item in items:
        p_no = item.get('product_no')
        filename = item.get('processed_filename')
        local_path = os.path.join(UPLOAD_FOLDER, filename)

        try:
            # 1. 대표 썸네일 FTP 업로드
            ftp_main_url = upload_to_ftp(local_path, filename)
            logger.info(f"[업로드 URL] product_no={p_no} FTP 업로드 실제 URL: {ftp_main_url}")

            # 2. 대표 이미지 갱신: detail_image/list_image/tiny_image 등은 상품 PUT의
            # "쓰기 가능한" 파라미터가 아니라 읽기 전용 응답 속성이다 (카페24가 200을
            # 반환해도 실제로는 무시함 - list_image가 안 바뀌던 원인). 카페24는 원본
            # 이미지 1장을 등록하면 쇼핑몰의 "이미지 사이즈 설정"에 따라 tiny/small/
            # list/detail/big을 서버에서 직접 재생성하는 전용 리소스
            # (POST /products/images, "Products images")를 제공하므로 이걸 사용한다.
            image_upload_url = f"{CAFE24_API_BASE}/products/images"
            image_upload_body = {
                "shop_no": 1,
                "requests": [
                    {"product_no": int(p_no), "request_url": ftp_main_url}
                ]
            }
            logger.info(f"[카페24 이미지 등록 요청] product_no={p_no} url={image_upload_url} payload={image_upload_body}")

            img_res = cafe24_api_request('POST', image_upload_url, json=image_upload_body, timeout=15)
            img_res_json = img_res.json()
            logger.info(f"[카페24 이미지 등록 응답] product_no={p_no} status={img_res.status_code} body={img_res_json}")

            if img_res.status_code not in (200, 201):
                err_msg = img_res_json.get('error', {}).get('message', '이미지 등록 API 호출 실패')
                logger.error(f"카페24 이미지 등록 실패 (product_no={p_no}): {err_msg}")
                fail_list.append({"product_no": p_no, "reason": err_msg})
                continue

            # 3. 추가이미지 제어 조건 (기존 상품 PUT 그대로 유지 - add_image는 별도 필드)
            images_payload = {}
            if mode == 'model':
                # [모델컷 모드] 추가이미지 일괄 삭제
                images_payload["add_image"] = []
            elif mode == 'product':
                # [제품컷 모드] 추가이미지도 상하여백 가공 후 업데이트
                raw_add_images = item.get('add_images', [])
                processed_add_urls = []

                for idx, add_url in enumerate(raw_add_images):
                    add_resp = fetch_image(add_url, timeout=10)

                    add_filename = f"{p_no}_add_{idx}.jpg"
                    out_add_file = process_image_bytes(add_resp.content, add_filename, 'product')
                    out_add_path = os.path.join(UPLOAD_FOLDER, out_add_file)

                    ftp_add_url = upload_to_ftp(out_add_path, out_add_file)
                    processed_add_urls.append(ftp_add_url)

                images_payload["add_image"] = processed_add_urls

            api_url = f"{CAFE24_API_BASE}/products/{p_no}"
            request_body = {"request": {"images": images_payload}}
            logger.info(f"[카페24 PUT 요청] product_no={p_no} url={api_url} payload={request_body}")

            res = cafe24_api_request('PUT', api_url, json=request_body, timeout=15)
            res_json = res.json()
            logger.info(f"[카페24 응답] product_no={p_no} status={res.status_code} body={res_json}")

            if res.status_code == 200:
                success_list.append({"product_no": p_no, "url": ftp_main_url})

                # 실제로 반영된 list_image를 카페24에 직접 재조회해서 검증한다.
                # (이미지 등록 API 응답의 필드명을 확신할 수 없으므로, 항상 정확한
                # 상품 리소스 GET을 기준으로 확인 - 검증 실패는 송신 성공/실패에 영향 없음)
                try:
                    verify_get = cafe24_api_request(
                        'GET', f"{CAFE24_API_BASE}/products/{p_no}",
                        params={"fields": "list_image,detail_image"}, timeout=10
                    )
                    verify_products = verify_get.json().get('products', [])
                    current_list_image = verify_products[0].get('list_image') if verify_products else None
                    logger.info(f"[카페24 현재 list_image 재조회] product_no={p_no} list_image={current_list_image}")
                    if current_list_image:
                        verify_image_dimensions(p_no, 'list_image', current_list_image)
                except Exception as verify_err:
                    logger.warning(f"[list_image 재조회 실패] product_no={p_no}: {verify_err}")
            else:
                err_msg = res_json.get('error', {}).get('message', 'API 호출 실패')
                logger.error(f"카페24 송신 실패 (product_no={p_no}): {err_msg}")
                fail_list.append({"product_no": p_no, "reason": err_msg})

        except Cafe24ReauthRequired as e:
            logger.error(f"카페24 재인증 필요, 송신 중단 (product_no={p_no}): {e}")
            fail_list.append({"product_no": p_no, "reason": "카페24 토큰 재인증이 필요합니다."})
            reauth_url = get_cafe24_reauth_url()
            return jsonify({
                "success": False,
                "error": "카페24 토큰 재인증이 필요합니다. 관리자 페이지에서 앱을 재인증해 주세요.",
                "reauth_required": True,
                **({"reauth_url": reauth_url} if reauth_url else {}),
                "summary": {"total": len(items), "success_count": len(success_list), "fail_count": len(fail_list)},
                "failures": fail_list
            }), 401
        except Exception as e:
            logger.exception(f"카페24 송신 중 예외 발생 (product_no={p_no})")
            fail_list.append({"product_no": p_no, "reason": str(e)})

    return jsonify({
        "success": True,
        "summary": {"total": len(items), "success_count": len(success_list), "fail_count": len(fail_list)},
        "failures": fail_list
    })

if __name__ == '__main__':
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
