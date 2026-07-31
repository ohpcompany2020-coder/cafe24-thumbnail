import os
import io
import re
import time
import html
import base64
import logging
import threading
import ftplib
import zipfile
import requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file
from werkzeug.exceptions import HTTPException
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from PIL import Image, ImageFile, ImageSequence

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

# 실제 FileZilla 접속 테스트로 확인된 FTP 서버 주소는 "{mall_id}.ftp.cafe24.com"이다.
# 기존 기본값 "{mall_id}.cafe24.com"은 ".ftp." 없이 웹 도메인과 같아서 잘못된 서버로
# 붙었을 가능성이 높다 (계정/코드 로직 문제가 아니라 호스트 자체가 틀렸던 것).
FTP_HOST = os.getenv("FTP_HOST", f"{CAFE24_MALL_ID}.ftp.cafe24.com")
FTP_USER = os.getenv("FTP_USER", "your_ftp_id")
FTP_PASS = os.getenv("FTP_PASS", "your_ftp_password")
FTP_PORT = int(os.getenv("FTP_PORT", 21))
FTP_HOST_URL = f"http://{CAFE24_MALL_ID}.cafe24.com"
# 카페24 고객센터 확인: "/web/product/..."는 카페24 시스템이 자체 관리하는 상품이미지
# 전용 폴더라 일반 FTP 계정에는 쓰기 권한이 없다. 공식 안내는 "web 폴더 바로 하위에
# 원하는 이름의 폴더를 새로 만들어 쓰라"는 것이었고, 이미 성공이 확인된
# "/web/upload/thumbnail/"이 바로 그 원칙에 맞는 폴더이므로 대표 이미지도 이 폴더를 쓴다.

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

def _extract_single_product(res_json):
    """GET /products/{product_no}(단수 리소스) 응답에서 상품 dict를 꺼낸다.

    카페24는 목록 조회(GET /products)는 {"products": [...]} 복수 키로,
    단건 조회(GET /products/{no})는 {"product": {...}} 단수 키로 응답한다.
    단건 응답에서 'products'만 찾으면 항상 빈 값이 나와 list_image가 무조건
    None으로 보이므로(실제 반영 여부와 무관하게) 두 형태를 모두 처리한다."""
    if not isinstance(res_json, dict):
        return None

    product = res_json.get('product')
    if isinstance(product, dict):
        return product

    products = res_json.get('products')
    if isinstance(products, list) and products and isinstance(products[0], dict):
        return products[0]
    if isinstance(products, dict):  # 일부 응답이 단수 dict를 'products'로 주는 경우 대비
        return products

    return None


def extract_cafe24_error_message(res_json, default='API 호출 실패'):
    """카페24 에러 응답은 엔드포인트에 따라 {"error": {...}} 단수 형태와
    {"errors": [{...}, ...]} 복수 형태가 섞여 있어 둘 다 처리한다."""
    error = res_json.get('error')
    if isinstance(error, dict):
        return error.get('message', default)
    errors = res_json.get('errors')
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0].get('message', default)
    return default

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

# 이미지 1건당 비율 확인에 허용할 시간(초). 짧게 잡아 느린 이미지 하나가 페이지 전체를
# 붙잡지 못하게 한다 - 확인 못 한 상품은 '판정 불가'로 두고 목록에는 그대로 남긴다.
IMAGE_PROBE_TIMEOUT = float(os.getenv('IMAGE_PROBE_TIMEOUT', 4))
# 한 페이지의 비율 확인 전체에 허용할 시간(초). 이 시간이 지나면 남은 건 포기하고 응답한다.
# 카페24 API 호출 시간까지 더해도 Render 요청 타임아웃(약 30초) 안에 들어오도록 잡는다.
IMAGE_PROBE_BUDGET = float(os.getenv('IMAGE_PROBE_BUDGET', 12))


def get_remote_image_size(url, timeout=IMAGE_PROBE_TIMEOUT):
    """이미지 전체를 내려받지 않고 헤더 부분만 스트리밍해서 (width, height)를 얻는다.

    검색 결과의 모든 상품 이미지를 원본 그대로 받으면 느리므로, PIL의 증분 파서에
    앞부분 청크만 흘려넣어 크기가 확정되는 즉시 연결을 끊는다.
    실패하면 None을 반환한다 (호출부에서 '판정 불가'로 처리)."""
    if not url:
        return None

    for referer in SHOP_REFERER_CANDIDATES:
        try:
            headers = {'User-Agent': IMAGE_DOWNLOAD_USER_AGENT, 'Referer': referer}
            with requests.get(url, headers=headers, timeout=timeout, stream=True) as resp:
                if resp.status_code == 403:
                    continue
                resp.raise_for_status()

                parser = ImageFile.Parser()
                for chunk in resp.iter_content(chunk_size=4096):
                    if not chunk:
                        continue
                    parser.feed(chunk)
                    if parser.image:
                        return parser.image.size
        except Exception as e:
            logger.debug(f"[이미지 크기 조회 실패] url={url} referer={referer}: {e}")
            continue

    return None


def is_square_image(url, tolerance=0.02, timeout=IMAGE_PROBE_TIMEOUT):
    """이미지가 1:1 비율인지 판정한다. 크기를 확인하지 못하면 None(판정 불가)을 반환한다."""
    size = get_remote_image_size(url, timeout=timeout)
    if not size:
        return None

    width, height = size
    if not width or not height:
        return None
    return abs((height / width) - 1.0) <= tolerance


def probe_square_flags(products, budget_seconds=IMAGE_PROBE_BUDGET, max_workers=16):
    """상품 목록의 1:1 여부를 병렬로 확인한다.

    전체에 시간 상한(budget)을 둔다. 이미지 서버가 느리거나 응답하지 않을 때
    페이지 처리가 무한정 늘어나 요청 자체가 타임아웃(502)되는 것을 막기 위함이다.
    상한 안에 끝내지 못한 상품은 None(판정 불가)으로 두어 목록에 그대로 남긴다.

    executor.shutdown(wait=False)로 남은 작업을 기다리지 않고 즉시 반환한다.
    (with 블록을 쓰면 shutdown이 실행 중인 작업을 모두 기다려 상한이 무의미해진다)"""
    flags = [None] * len(products)
    if not products:
        return flags

    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_to_index = {
            executor.submit(is_square_image, product['image_url']): idx
            for idx, product in enumerate(products)
        }

        done, not_done = futures_wait(future_to_index.keys(), timeout=budget_seconds)

        for future in done:
            idx = future_to_index[future]
            try:
                flags[idx] = future.result()
            except Exception as e:
                logger.warning(f"[비율 확인 예외] product_no={products[idx].get('product_no')}: {e}")
                flags[idx] = None

        for future in not_done:
            future.cancel()
            idx = future_to_index[future]
            logger.warning(
                f"[비율 확인 시간초과] product_no={products[idx].get('product_no')} "
                f"url={products[idx].get('image_url')} - 판정 불가로 두고 목록에는 표시"
            )
    finally:
        executor.shutdown(wait=False)

    elapsed = time.monotonic() - started
    timed_out = sum(1 for f in flags if f is None)
    logger.info(
        f"[비율 확인] {len(products)}건 처리 {elapsed:.1f}초 (판정 불가 {timed_out}건)"
    )
    return flags


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

def _ftp_ensure_dir(ftp, remote_dir):
    """remote_dir(예: '/web/product/big/202607/')이 없으면 한 단계씩 생성하며 이동한다.
    일부 카페24 FTP 계정은 접속 시 특정 폴더에 chroot되어 있어 cwd('/')(루트만 단독
    지정)를 거부한다 (550 /: No such file or directory). 따라서 루트로 먼저 이동하지
    않고, 매 단계마다 remote_dir의 절대경로 전체(예: '/web', '/web/product', ...)를
    그대로 cwd에 전달한다 - 기존에 '/web/upload/thumbnail/' 같은 절대경로 1회 cwd가
    실제로 성공했던 것과 동일한 방식.

    각 단계(CWD 성공/실패, MKD 필요 여부)를 전부 로그로 남겨 실제 어느 폴더까지
    도달했는지 추적 가능하게 한다."""
    path_so_far = ''
    for part in remote_dir.strip('/').split('/'):
        path_so_far += '/' + part
        try:
            ftp.cwd(path_so_far)
            logger.info(f"[FTP CWD] '{path_so_far}' 이동 성공 (기존 폴더)")
        except ftplib.error_perm as e:
            logger.warning(f"[FTP CWD] '{path_so_far}' 이동 실패({e}) - MKD 시도")
            try:
                ftp.mkd(path_so_far)
                logger.info(f"[FTP MKD] '{path_so_far}' 생성 성공")
            except ftplib.error_perm as mkd_err:
                logger.error(f"[FTP MKD] '{path_so_far}' 생성 실패: {mkd_err}")
                raise
            ftp.cwd(path_so_far)
            logger.info(f"[FTP CWD] '{path_so_far}' 생성 후 이동 성공")

def upload_to_ftp(local_file_path, remote_filename, remote_dir='/web/upload/thumbnail/'):
    """Web FTP 업로드 함수. remote_dir 하위(없으면 생성)에 파일을 올리고 접근 가능한 URL을 반환한다."""
    try:
        logger.info(
            f"[FTP 접속 시도] host={FTP_HOST} port={FTP_PORT} user={FTP_USER} "
            f"remote_dir={remote_dir} remote_filename={remote_filename}"
        )
        ftp = ftplib.FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        logger.info(f"[FTP 접속] 서버 환영 메시지: {ftp.getwelcome()}")

        ftp.login(FTP_USER, FTP_PASS)

        # 일부 카페24 FTP 계정은 PWD 자체를 550으로 거부하지만, 이는 업로드(STOR)나
        # 폴더 이동(CWD)과는 무관한 별개 명령이라 실패해도 무시하고 계속 진행한다.
        try:
            logger.info(f"[FTP 접속] 로그인 직후 기본 디렉토리(pwd): {ftp.pwd()}")
        except Exception as pwd_err:
            logger.warning(f"[FTP 접속] pwd() 실패(무시하고 계속 진행): {pwd_err}")

        _ftp_ensure_dir(ftp, remote_dir)

        logger.info(f"[FTP STOR 시도] '{remote_dir}' 폴더에서 '{remote_filename}' 업로드 시작")
        with open(local_file_path, 'rb') as f:
            ftp.storbinary(f'STOR {remote_filename}', f)
        logger.info(f"[FTP STOR 성공] '{remote_dir}{remote_filename}'")
        ftp.quit()
        return f"{FTP_HOST_URL}{remote_dir}{remote_filename}"
    except Exception as e:
        logger.error(f"[FTP 업로드 실패] remote_dir={remote_dir} remote_filename={remote_filename}: {e}")
        raise Exception(f"FTP 업로드 실패: {str(e)}")

def _ftp_debug_step(steps, name, fn):
    """진단 단계 하나를 실행하고 성공/실패와 결과(또는 에러)를 steps 딕셔너리에 기록한다.
    한 단계가 실패해도 예외를 던지지 않고 다음 단계로 계속 진행할 수 있게 한다."""
    try:
        result = fn()
        steps[name] = {"ok": True, "result": result}
        return True
    except Exception as e:
        steps[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return False

@app.route('/api/debug/ftp-test', methods=['GET'])
def debug_ftp_test():
    """FTP 접속 문제를 실제 업로드/변환 흐름 없이 바로 진단하기 위한 최소 테스트 엔드포인트.
    connect/login까지는 필수 전제 조건으로 실패 시 즉시 중단하고, 그 이후 pwd/cwd/nlst/
    실제 업로드(STOR)는 각각 독립적으로 시도해서 어느 단계가 성공/실패하는지 모두 남긴다
    (PWD만 막히고 나머지는 정상 동작하는 경우를 확인하기 위함)."""
    steps = {}
    ftp = None
    try:
        steps['host'] = FTP_HOST
        steps['port'] = FTP_PORT
        steps['user'] = FTP_USER

        ftp = ftplib.FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        steps['connect'] = {"ok": True}
        steps['welcome'] = ftp.getwelcome()

        ftp.login(FTP_USER, FTP_PASS)
        steps['login'] = {"ok": True}

        _ftp_debug_step(steps, 'pwd', ftp.pwd)
        _ftp_debug_step(steps, 'cwd_web', lambda: ftp.cwd('/web'))
        _ftp_debug_step(steps, 'nlst_web', ftp.nlst)
        _ftp_debug_step(steps, 'cwd_web_product', lambda: ftp.cwd('/web/product'))

        # 실제 업로드(STOR) 테스트: 이미 변환된 이미지가 있으면 그중 하나를 재사용하고,
        # 없으면 작은 더미 텍스트 파일을 만들어 업로드해본다. 어느 폴더에 있든(위 cwd
        # 성공 여부와 무관하게 현재 위치 기준) 시도한다.
        existing_files = [
            f for f in os.listdir(UPLOAD_FOLDER)
            if os.path.isfile(os.path.join(UPLOAD_FOLDER, f))
        ]
        if existing_files:
            test_local_path = os.path.join(UPLOAD_FOLDER, existing_files[0])
            test_remote_name = f"_ftp_test_{existing_files[0]}"
        else:
            test_local_path = os.path.join(UPLOAD_FOLDER, '_ftp_test_dummy.txt')
            with open(test_local_path, 'wb') as f:
                f.write(b"cafe24-thumbnail ftp connectivity test")
            test_remote_name = '_ftp_test_dummy.txt'

        def _do_stor_test():
            with open(test_local_path, 'rb') as f:
                ftp.storbinary(f'STOR {test_remote_name}', f)
            return test_remote_name

        _ftp_debug_step(steps, 'stor_test', _do_stor_test)

        logger.info(f"[FTP 진단] {steps}")
        return jsonify({"success": True, "steps": steps})
    except Exception as e:
        steps['fatal_error'] = f"{type(e).__name__}: {e}"
        logger.error(f"[FTP 진단 실패] {steps}")
        return jsonify({"success": False, "steps": steps}), 500
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                pass

# ==========================================
# [진단] products/images "image" 파라미터 형식 일괄 테스트
# ==========================================
# 대표도메인(자체 도메인). 카페24 기본도메인은 f"{CAFE24_MALL_ID}.cafe24.com"으로 계산한다.
SHOP_PRIMARY_HOST = os.getenv('SHOP_PRIMARY_HOST', 'onehundredpercent.co.kr')
# 카페24 안내상 "외부에서 업로드한 상품 이미지"가 요구될 수 있다는 폴더
CAFE24_EXTRA_IMAGE_DIR = '/web/product/extra/excel/'


def _preview_image_value(value, limit=50):
    """로그/응답에 남길 image 값 미리보기. base64 data URI처럼 긴 값은 앞 50자만 남긴다."""
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...(총 {len(value)}자)"


def _register_image_attempt(product_no, label, description, image_value):
    """POST /admin/products/images 를 image 값 후보 하나로 호출하고 결과를 dict로 반환한다.
    실패해도 예외를 던지지 않는다 (다음 후보를 계속 시도해야 하므로)."""
    api_url = f"{CAFE24_API_BASE}/products/images"
    body = {"shop_no": 1, "requests": [{"product_no": int(product_no), "image": image_value}]}
    log_payload = {
        "shop_no": 1,
        "requests": [{"product_no": int(product_no), "image": _preview_image_value(image_value)}]
    }

    attempt = {
        "candidate": label,
        "description": description,
        "image_preview": _preview_image_value(image_value),
        "image_length": len(image_value) if isinstance(image_value, str) else None,
        "request_payload": log_payload,
    }

    logger.info(f"[이미지등록 진단 요청] #{label} ({description}) url={api_url} payload={log_payload}")

    try:
        res = cafe24_api_request('POST', api_url, json=body, timeout=20)
    except Cafe24ReauthRequired as e:
        attempt.update({"ok": False, "error": f"카페24 재인증 필요: {e}", "reauth_required": True})
        logger.error(f"[이미지등록 진단 응답] #{label} 재인증 필요: {e}")
        return attempt
    except Exception as e:
        attempt.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        logger.error(f"[이미지등록 진단 응답] #{label} 요청 예외: {type(e).__name__}: {e}")
        return attempt

    try:
        res_json = res.json()
    except ValueError:
        res_json = {"raw_text": res.text}

    # requests의 headers는 대소문자 구분이 없으므로 한 번만 조회하면 된다.
    trace_id = res.headers.get('X-Trace-Id')
    ok = res.status_code in (200, 201)

    attempt.update({
        "ok": ok,
        "status": res.status_code,
        "trace_id": trace_id,
        "error_message": None if ok else extract_cafe24_error_message(res_json, f"HTTP {res.status_code}"),
        "response_body": res_json,
    })

    logger.info(
        f"[이미지등록 진단 응답] #{label} status={res.status_code} ok={ok} "
        f"X-Trace-Id={trace_id} body={res_json}"
    )
    if not ok:
        logger.error(f"[이미지등록 진단 실패 헤더] #{label} 전체 헤더={dict(res.headers)}")

    return attempt


@app.route('/api/debug/image-register-test', methods=['GET', 'POST'])
def debug_image_register_test():
    """카페24 "Wrong image path" 에러의 원인을 찾기 위한 진단 엔드포인트.

    이미 FTP에 올라가 있는 파일 하나를 기준으로, image 파라미터에 넣을 수 있는 값의
    후보를 순서대로 전부 시도하고 각 시도의 결과를 한꺼번에 반환한다.

    사용법:
      GET /api/debug/image-register-test?product_no=123&filename=processed_123.jpg
      (filename 생략 시 static/processed_images의 가장 최근 파일 사용)

    옵션:
      skip_extra_ftp=true  -> 후보 7(/web/product/extra/excel/ 업로드)을 건너뜀
      skip_base64=true     -> 후보 8(base64 data URI)을 건너뜀
    """
    payload = request.json if request.method == 'POST' and request.is_json else {}
    payload = payload or {}

    def _param(name, default=None):
        value = request.args.get(name)
        if value is None:
            value = payload.get(name)
        return default if value is None else value

    product_no = _param('product_no')
    if not product_no:
        return jsonify({
            "success": False,
            "error": "product_no 파라미터가 필요합니다. 예: /api/debug/image-register-test?product_no=123"
        }), 400
    try:
        product_no = int(product_no)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": f"product_no는 숫자여야 합니다: {product_no}"}), 400

    filename = _param('filename')
    if not filename:
        local_files = [
            f for f in os.listdir(UPLOAD_FOLDER)
            if os.path.isfile(os.path.join(UPLOAD_FOLDER, f)) and not f.startswith('_ftp_test')
        ]
        if not local_files:
            return jsonify({
                "success": False,
                "error": "filename 파라미터가 없고 static/processed_images에도 파일이 없습니다. "
                         "먼저 변환(/api/convert)을 실행하거나 filename을 직접 지정해 주세요."
            }), 400
        filename = max(local_files, key=lambda f: os.path.getmtime(os.path.join(UPLOAD_FOLDER, f)))

    filename = os.path.basename(filename)  # 경로 주입 방지
    local_path = os.path.join(UPLOAD_FOLDER, filename)
    local_exists = os.path.isfile(local_path)

    skip_extra_ftp = str(_param('skip_extra_ftp', 'false')).lower() == 'true'
    skip_base64 = str(_param('skip_base64', 'false')).lower() == 'true'

    cafe24_host = f"{CAFE24_MALL_ID}.cafe24.com"
    thumb_dir = '/web/upload/thumbnail/'

    logger.info(
        f"[이미지등록 진단 시작] product_no={product_no} filename={filename} "
        f"local_exists={local_exists} skip_extra_ftp={skip_extra_ftp} skip_base64={skip_base64}"
    )

    # 후보 1~6: 이미 /web/upload/thumbnail/ 에 올라가 있는 파일 기준
    candidates = [
        ("1", "대표도메인 www 포함 절대URL",
         f"https://www.{SHOP_PRIMARY_HOST}{thumb_dir}{filename}"),
        ("2", "대표도메인 www 없이 절대URL",
         f"https://{SHOP_PRIMARY_HOST}{thumb_dir}{filename}"),
        ("3", "카페24 기본도메인 절대URL (/web 제외)",
         f"https://{cafe24_host}/upload/thumbnail/{filename}"),
        ("4", "상대경로 (/web 제외, 앞 슬래시 있음)",
         f"/upload/thumbnail/{filename}"),
        ("5", "상대경로 (앞 슬래시 없음)",
         f"upload/thumbnail/{filename}"),
        ("6", "파일명만", filename),
    ]

    attempts = []
    skipped = []

    for label, description, image_value in candidates:
        attempts.append(_register_image_attempt(product_no, label, description, image_value))
        time.sleep(0.4)  # 카페24 API 호출 제한(초당 요청 수)에 걸리지 않도록 간격을 둔다

    # 후보 7: /web/product/extra/excel/ 에 FTP 업로드 후 절대URL/상대경로 두 형태로 시도
    extra_ftp = {"attempted": False}
    if skip_extra_ftp:
        skipped.append({"candidate": "7", "reason": "skip_extra_ftp=true"})
    elif not local_exists:
        skipped.append({
            "candidate": "7",
            "reason": f"로컬 파일이 없어 FTP 업로드 불가: {local_path}"
        })
    else:
        extra_ftp["attempted"] = True
        # 업로드 성공 여부와 API 호출 결과를 섞지 않도록 try는 FTP 업로드까지만 감싼다.
        try:
            uploaded_url = upload_to_ftp(local_path, filename, remote_dir=CAFE24_EXTRA_IMAGE_DIR)
            extra_ftp.update({"ok": True, "remote_dir": CAFE24_EXTRA_IMAGE_DIR, "ftp_url": uploaded_url})
            logger.info(f"[이미지등록 진단] 후보7 FTP 업로드 성공: {uploaded_url}")
        except Exception as e:
            extra_ftp.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
            skipped.append({
                "candidate": "7",
                "reason": f"{CAFE24_EXTRA_IMAGE_DIR} FTP 업로드 실패: {type(e).__name__}: {e}"
            })
            logger.error(f"[이미지등록 진단] 후보7 FTP 업로드 실패: {type(e).__name__}: {e}")

        if extra_ftp.get("ok"):
            attempts.append(_register_image_attempt(
                product_no, "7-a", f"{CAFE24_EXTRA_IMAGE_DIR} 업로드 후 절대URL",
                f"https://{cafe24_host}{CAFE24_EXTRA_IMAGE_DIR}{filename}"
            ))
            time.sleep(0.4)
            attempts.append(_register_image_attempt(
                product_no, "7-b", f"{CAFE24_EXTRA_IMAGE_DIR} 업로드 후 상대경로",
                f"{CAFE24_EXTRA_IMAGE_DIR}{filename}"
            ))
            time.sleep(0.4)

    # 후보 8: base64 data URI를 image 값으로 직접 전달
    if skip_base64:
        skipped.append({"candidate": "8", "reason": "skip_base64=true"})
    elif not local_exists:
        skipped.append({"candidate": "8", "reason": f"로컬 파일이 없어 base64 인코딩 불가: {local_path}"})
    else:
        try:
            with open(local_path, 'rb') as f:
                encoded = base64.b64encode(f.read()).decode()
            ext = os.path.splitext(filename)[1].lower()
            mime = {'.png': 'image/png', '.gif': 'image/gif'}.get(ext, 'image/jpeg')
            attempts.append(_register_image_attempt(
                product_no, "8", f"base64 data URI ({mime})",
                f"data:{mime};base64,{encoded}"
            ))
        except Exception as e:
            skipped.append({"candidate": "8", "reason": f"base64 인코딩 실패: {type(e).__name__}: {e}"})
            logger.error(f"[이미지등록 진단] 후보8 base64 인코딩 실패: {type(e).__name__}: {e}")

    successes = [a for a in attempts if a.get("ok")]
    logger.info(
        f"[이미지등록 진단 종료] product_no={product_no} filename={filename} "
        f"시도={len(attempts)}건 성공={[a['candidate'] for a in successes]} 건너뜀={skipped}"
    )

    return jsonify({
        "success": True,
        "product_no": product_no,
        "filename": filename,
        "local_file_exists": local_exists,
        "summary": {
            "total_attempts": len(attempts),
            "success_count": len(successes),
            "successful_candidates": [
                {"candidate": a["candidate"], "description": a["description"], "image": a["image_preview"]}
                for a in successes
            ],
            "any_success": bool(successes),
        },
        "extra_folder_ftp_upload": extra_ftp,
        "skipped": skipped,
        "attempts": attempts,
    })


# 카페24 상품의 이미지 필드 4종. 대표이미지 반영 여부는 이 중 하나라도 새 경로로
# 바뀌었는지로 판정한다 (list_image 하나만 보면 detail_image만 바뀌는 경우를 놓친다).
PRODUCT_IMAGE_FIELDS = ('detail_image', 'list_image', 'small_image', 'tiny_image')

# PUT /products/{no}의 image_upload_type. 카페24 문서 기준 A=대표이미지등록,
# B=개별이미지등록, C=웹FTP 등록. 우리는 웹FTP로 올린 파일 경로를 넘기므로 "C"를 쓴다.
# (진단에서 F=C+detail_image, J=B+4종이 각각 반영 확인됨. 형식만 바꿔볼 수 있도록 상수로 둔다)
CAFE24_IMAGE_UPLOAD_TYPE = os.getenv('CAFE24_IMAGE_UPLOAD_TYPE', 'C')

# PUT /products/{no}로 추가이미지를 보낼 때 쓸 필드명. 조회 응답의 키는 'additionalimages'
# (언더스코어 없는 복수형)로 확인됐지만, 쓰기 필드명이 조회 키와 같다는 보장은 없다.
# /api/debug/additional-image-put-test로 확정한 뒤 이 값을 맞추면 된다.
CAFE24_ADDITIONAL_IMAGE_FIELD = os.getenv('CAFE24_ADDITIONAL_IMAGE_FIELD', 'additional_image')


def _changed_image_fields(before, after):
    """before 대비 after에서 새 값으로 바뀐 이미지 필드명 목록을 돌려준다."""
    before = before or {}
    after = after or {}
    changed = []
    for field in PRODUCT_IMAGE_FIELDS:
        new_value = after.get(field)
        if new_value and new_value != before.get(field):
            changed.append(field)
    return changed


def _get_product_image_fields(product_no):
    """상품의 현재 이미지 필드를 단건 조회해서 dict로 반환한다 (진단/검증용)."""
    res = cafe24_api_request(
        'GET', f"{CAFE24_API_BASE}/products/{product_no}",
        params={"fields": "product_no,list_image,detail_image,tiny_image,small_image"}, timeout=10
    )
    res_json = res.json()
    product = _extract_single_product(res_json) or {}
    return {
        "status": res.status_code,
        "list_image": product.get('list_image'),
        "detail_image": product.get('detail_image'),
        "tiny_image": product.get('tiny_image'),
        "small_image": product.get('small_image'),
        "raw_keys": list(res_json.keys()) if isinstance(res_json, dict) else None,
    }


def _try_link_candidate(product_no, label, description, put_request, settle_seconds=2):
    """PUT /products/{no} 후보 하나를 보내고, 실제 반영 여부까지 판정해서 dict로 반환한다.

    카페24는 모르는 필드를 200과 함께 조용히 무시하므로 PUT 응답만으로는 반영 여부를
    알 수 없다. 또 직전 후보가 값을 바꿔놨을 수 있으므로 기준값(before)은 반드시
    "매 시도 직전"에 새로 캡처한다 - 업로드 전 한 번만 찍으면 false negative가 난다.
    """
    api_url = f"{CAFE24_API_BASE}/products/{product_no}"
    body = {"shop_no": 1, "request": put_request}

    before = _get_product_image_fields(product_no)
    attempt = {
        "candidate": label,
        "description": description,
        "request_payload": body,
        "before": before,
    }
    logger.info(f"[연결 진단 요청] #{label} ({description}) payload={body} before={before}")

    try:
        res = cafe24_api_request('PUT', api_url, json=body, timeout=20)
    except Cafe24ReauthRequired as e:
        attempt.update({
            "ok": False, "actually_linked": False,
            "error": f"카페24 재인증 필요: {e}", "reauth_required": True
        })
        logger.error(f"[연결 진단 응답] #{label} 재인증 필요: {e}")
        return attempt
    except Exception as e:
        attempt.update({"ok": False, "actually_linked": False, "error": f"{type(e).__name__}: {e}"})
        logger.error(f"[연결 진단 응답] #{label} 요청 예외: {type(e).__name__}: {e}")
        return attempt

    try:
        res_json = res.json()
    except ValueError:
        res_json = {"raw_text": res.text}

    attempt.update({
        "status": res.status_code,
        "trace_id": res.headers.get('X-Trace-Id'),
        "response_body": res_json,
        "error_message": None if res.status_code == 200 else extract_cafe24_error_message(
            res_json, f"HTTP {res.status_code}"),
    })
    logger.info(f"[연결 진단 응답] #{label} status={res.status_code} body={res_json}")

    # 반영 여부는 두 곳에서 각각 판정한다.
    #  (1) PUT 응답 본문의 product - 카페24가 방금 반영한 값을 그대로 돌려주므로 가장 확실하다.
    #  (2) 재조회 - 반영 시차/캐시로 옛 값이 잠시 더 보일 수 있어 (1)보다 늦게 바뀔 수 있다.
    # list_image 하나만 비교하면 detail_image만 바뀌는 경우(F 후보)를 놓치므로 4종을 모두 본다.
    responded_product = _extract_single_product(res_json) or {}
    changed_in_response = _changed_image_fields(before, responded_product)

    time.sleep(settle_seconds)  # 카페24 반영 시차
    after = _get_product_image_fields(product_no)
    changed_in_requery = _changed_image_fields(before, after)

    changed_fields = changed_in_response or changed_in_requery
    linked = bool(changed_fields)

    attempt.update({
        "after": after,
        "response_product": {f: responded_product.get(f) for f in PRODUCT_IMAGE_FIELDS},
        "changed_fields_in_response": changed_in_response,
        "changed_fields_in_requery": changed_in_requery,
        "changed_fields": changed_fields,
        "actually_linked": linked,
        "ok": res.status_code == 200 and linked,
    })
    logger.info(
        f"[연결 진단 검증] #{label} 반영여부={linked} 바뀐필드={changed_fields} "
        f"(응답기준={changed_in_response} 재조회기준={changed_in_requery})"
    )
    return attempt


@app.route('/api/debug/product-link-test', methods=['GET', 'POST'])
def debug_product_link_test():
    """웹FTP로 올린 이미지를 상품 대표이미지로 연결하는 PUT 형식을 찾는 진단 엔드포인트.

    지금까지 확인된 사실:
      - POST /products/images는 base64를 201로 받아주지만 파일을 /web/upload/NNEditor/
        (상세페이지 에디터용 저장소)에 올릴 뿐 상품에는 연결하지 않는다 (후보 E 실패).
      - 상품의 실제 썸네일은 /web/product/big|medium|small|tiny/ 에 있으므로, PUT에
        NNEditor 경로를 넣으면 "Wrong image path"가 난다 (후보 A/B/D 실패).
      - image_upload_type 없이 detail_image만 보내면 "[Image type] The field is
        required." - 즉 PUT은 detail_image를 제대로 읽고 있고 image_upload_type만
        채우면 된다 (후보 C).
      - image_upload_type: A=대표이미지등록, B=개별이미지등록, C=웹FTP 등록.
        FTP로 올린 파일에 맞는 값은 C인데 아직 한 번도 시도하지 않았다.

    그래서 이 엔드포인트는 변환된 이미지를 먼저 FTP(/web/upload/thumbnail/, 이미 성공이
    검증된 경로)에 올린 뒤, 그 파일을 가리키는 경로 표기 4종을 image_upload_type="C"로
    시도하고(F~I), 마지막으로 type="B" + 4종 개별 지정(J)을 시도한다.

    사용법:
      GET /api/debug/product-link-test?product_no=676&filename=processed_676.jpg
      (filename 생략 시 static/processed_images의 가장 최근 파일 사용)
    """
    payload = request.json if request.method == 'POST' and request.is_json else {}
    payload = payload or {}

    def _param(name, default=None):
        value = request.args.get(name)
        if value is None:
            value = payload.get(name)
        return default if value is None else value

    product_no = _param('product_no')
    if not product_no:
        return jsonify({"success": False, "error": "product_no 파라미터가 필요합니다."}), 400
    try:
        product_no = int(product_no)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": f"product_no는 숫자여야 합니다: {product_no}"}), 400

    filename = _param('filename')
    if not filename:
        local_files = [
            f for f in os.listdir(UPLOAD_FOLDER)
            if os.path.isfile(os.path.join(UPLOAD_FOLDER, f)) and not f.startswith('_ftp_test')
        ]
        if not local_files:
            return jsonify({
                "success": False,
                "error": "filename 파라미터가 없고 변환된 이미지 파일도 없습니다. "
                         "먼저 변환(/api/convert)을 실행해 주세요."
            }), 400
        filename = max(local_files, key=lambda f: os.path.getmtime(os.path.join(UPLOAD_FOLDER, f)))

    filename = os.path.basename(filename)
    local_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.isfile(local_path):
        return jsonify({"success": False, "error": f"파일을 찾을 수 없습니다: {local_path}"}), 400

    # 1단계: 변환 이미지를 웹FTP로 업로드 (image_upload_type="C"의 전제 조건)
    thumb_dir = '/web/upload/thumbnail/'
    try:
        ftp_url = upload_to_ftp(local_path, filename, remote_dir=thumb_dir)
        ftp_step = {"ok": True, "remote_dir": thumb_dir, "filename": filename, "ftp_url": ftp_url}
        logger.info(f"[연결 진단] FTP 업로드 성공: {ftp_url}")
    except Exception as e:
        logger.error(f"[연결 진단] FTP 업로드 실패: {type(e).__name__}: {e}")
        return jsonify({
            "success": False,
            "error": f"FTP 업로드에 실패해 연결 테스트를 진행할 수 없습니다: {type(e).__name__}: {e}",
            "ftp_step": {"ok": False, "error": f"{type(e).__name__}: {e}"}
        }), 502

    ftp_abs_path = f"{thumb_dir}{filename}"                                   # /web/upload/thumbnail/x.jpg
    ftp_https_url = f"https://{SHOP_PRIMARY_HOST}{thumb_dir}{filename}"       # 대표도메인 절대URL
    ftp_no_web_path = f"/upload/thumbnail/{filename}"                         # /web 제외

    # 2단계: 연결용 PUT 후보. image_upload_type="C"(웹FTP 등록)에 경로 표기만 바꿔가며 시도한다.
    candidates = [
        ("F", 'image_upload_type="C" + detail_image=웹FTP 절대경로(/web 포함)',
         {"image_upload_type": "C", "detail_image": ftp_abs_path}),
        ("G", 'image_upload_type="C" + detail_image=대표도메인 https 절대URL',
         {"image_upload_type": "C", "detail_image": ftp_https_url}),
        ("H", 'image_upload_type="C" + detail_image=파일명만',
         {"image_upload_type": "C", "detail_image": filename}),
        ("I", 'image_upload_type="C" + detail_image=/web 제외 상대경로',
         {"image_upload_type": "C", "detail_image": ftp_no_web_path}),
        ("J", 'image_upload_type="B"(개별이미지등록) + 4종 모두 웹FTP 절대경로',
         {
             "image_upload_type": "B",
             "detail_image": ftp_abs_path,
             "list_image": ftp_abs_path,
             "tiny_image": ftp_abs_path,
             "small_image": ftp_abs_path,
         }),
    ]

    attempts = []
    for label, description, put_request in candidates:
        attempt = _try_link_candidate(product_no, label, description, put_request)
        attempts.append(attempt)

        if attempt.get("reauth_required"):
            logger.error("[연결 진단] 재인증이 필요해 남은 후보를 중단한다")
            break
        if attempt.get("actually_linked"):
            logger.info(f"[연결 진단] #{label} 형식이 실제로 반영됨 - 이후 후보는 시도하지 않음")
            break
        time.sleep(0.4)

    winners = [a for a in attempts if a.get("actually_linked")]

    return jsonify({
        "success": True,
        "product_no": product_no,
        "filename": filename,
        "ftp_step": ftp_step,
        "tested_paths": {
            "absolute_path": ftp_abs_path,
            "https_url": ftp_https_url,
            "filename_only": filename,
            "no_web_path": ftp_no_web_path,
        },
        "summary": {
            "working_candidate": winners[0]["candidate"] if winners else None,
            "working_description": winners[0]["description"] if winners else None,
            "working_payload": winners[0]["request_payload"] if winners else None,
            "any_linked": bool(winners),
        },
        "attempts": attempts,
    })


@app.route('/api/debug/additional-images-test', methods=['GET'])
def debug_additional_images_test():
    """추가이미지를 어느 경로로 조회해야 하는지, 응답의 정확한 필드명이 무엇인지 확인한다.

    상품 목록 응답에는 추가이미지가 기본 포함되지 않으므로, 아래 4가지를 모두 호출해
    각각의 원문과 추출 결과를 함께 돌려준다.

    사용법: GET /api/debug/additional-images-test?product_no=676
    """
    product_no = request.args.get('product_no')
    if not product_no:
        return jsonify({"success": False, "error": "product_no 파라미터가 필요합니다."}), 400

    attempts = []

    def _try(label, method, url, **kwargs):
        attempt = {"label": label, "url": url, "params": kwargs.get('params')}
        try:
            res = cafe24_api_request(method, url, timeout=15, **kwargs)
            attempt["status"] = res.status_code
            try:
                payload = res.json()
            except ValueError:
                payload = {"raw_text": res.text[:2000]}

            urls, found = _extract_additional_images(payload)
            attempt.update({
                "additional_image_key_present": found,
                "extracted_count": len(urls),
                "extracted_urls": urls,
                "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else None,
                # 필드명을 눈으로 확인할 수 있게 응답 원문을 그대로(길면 잘라서) 남긴다
                "raw_body": str(payload)[:3000],
            })
            logger.info(f"[추가이미지 진단] {label} status={res.status_code} 추출={len(urls)}장 body={str(payload)[:1000]}")
        except Exception as e:
            attempt["error"] = f"{type(e).__name__}: {e}"
            logger.error(f"[추가이미지 진단] {label} 예외: {e}")
        attempts.append(attempt)

    base = CAFE24_API_BASE
    _try("A. 전용 리소스 (additionalimages)", 'GET', f"{base}/products/{product_no}/additionalimages")
    _try("B. 단건 조회 + embed", 'GET', f"{base}/products/{product_no}",
         params={"embed": "additionalimages"})
    _try("C. 목록 조회 + embed (현재 검색이 쓰는 방식)", 'GET', f"{base}/products",
         params={"product_no": product_no, "embed": "additionalimages"})
    _try("D. 목록 조회 (embed 없음 - 기존 방식)", 'GET', f"{base}/products",
         params={"product_no": product_no})

    working = [a for a in attempts if a.get("extracted_count")]
    return jsonify({
        "success": True,
        "product_no": product_no,
        "summary": {
            "working_method": working[0]["label"] if working else None,
            "additional_image_count": working[0]["extracted_count"] if working else 0,
            "any_found": bool(working),
        },
        "attempts": attempts,
    })


@app.route('/api/debug/additional-image-put-test', methods=['GET'])
def debug_additional_image_put_test():
    """추가이미지를 "다시 보낼 때"의 정확한 요청 형식을 찾는 진단 엔드포인트.

    조회 키는 'additionalimages'(객체 배열)로 확인됐지만, 쓰기 필드명과 값 형식이
    조회와 같다는 보장이 없다. 상품 PUT의 필드명 2가지와 전용 리소스 PUT 2가지를
    순서대로 시도하고, 매번 추가이미지를 다시 읽어 실제로 반영됐는지 확인한다.

    ※ 이 테스트는 대상 상품의 추가이미지를 실제로 교체한다. 반드시 테스트용 상품으로만
       실행할 것. 실행 전 현재 추가이미지 목록을 baseline으로 남기므로, 필요하면
       그 목록을 보고 수동 복구할 수 있다.

    사용법:
      GET /api/debug/additional-image-put-test?product_no=676&filename=processed_676_add_0.jpg&confirm=yes
    """
    product_no = request.args.get('product_no')
    filename = request.args.get('filename')
    if not product_no:
        return jsonify({"success": False, "error": "product_no 파라미터가 필요합니다."}), 400

    if request.args.get('confirm') != 'yes':
        return jsonify({
            "success": False,
            "error": "이 테스트는 대상 상품의 추가이미지를 실제로 교체합니다. "
                     "테스트용 상품인지 확인 후 confirm=yes 를 붙여 다시 호출해 주세요.",
            "current_additional_images": fetch_additional_images(product_no),
        }), 400

    # 변환 파일이 지정되지 않으면 가장 최근 변환 파일을 쓴다
    if not filename:
        local_files = [
            f for f in os.listdir(UPLOAD_FOLDER)
            if os.path.isfile(os.path.join(UPLOAD_FOLDER, f)) and not f.startswith('_ftp_test')
        ]
        if not local_files:
            return jsonify({"success": False, "error": "변환된 이미지가 없습니다. 먼저 변환을 실행해 주세요."}), 400
        filename = max(local_files, key=lambda f: os.path.getmtime(os.path.join(UPLOAD_FOLDER, f)))

    filename = os.path.basename(filename)
    local_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.isfile(local_path):
        return jsonify({"success": False, "error": f"파일을 찾을 수 없습니다: {local_path}"}), 400

    baseline = fetch_additional_images(product_no)
    logger.warning(f"[추가이미지 PUT 진단] product_no={product_no} 변경 전 목록(복구용)={baseline}")

    remote_dir = '/web/upload/thumbnail/'
    try:
        upload_to_ftp(local_path, filename, remote_dir=remote_dir)
    except Exception as e:
        return jsonify({"success": False, "error": f"FTP 업로드 실패: {type(e).__name__}: {e}"}), 502

    ftp_path = f"{remote_dir}{filename}"
    product_url = f"{CAFE24_API_BASE}/products/{product_no}"
    resource_url = f"{CAFE24_API_BASE}/products/{product_no}/additionalimages"

    candidates = [
        ("A. 상품 PUT + additional_image (문자열 배열)", product_url,
         {"shop_no": 1, "request": {"image_upload_type": CAFE24_IMAGE_UPLOAD_TYPE,
                                    "additional_image": [ftp_path]}}),
        ("B. 상품 PUT + additionalimages (문자열 배열)", product_url,
         {"shop_no": 1, "request": {"image_upload_type": CAFE24_IMAGE_UPLOAD_TYPE,
                                    "additionalimages": [ftp_path]}}),
        ("C. 전용 리소스 PUT + additional_image", resource_url,
         {"shop_no": 1, "request": {"additional_image": [ftp_path]}}),
        ("D. 전용 리소스 PUT + additionalimages", resource_url,
         {"shop_no": 1, "request": {"additionalimages": [ftp_path]}}),
    ]

    attempts = []
    for label, url, body in candidates:
        attempt = {"label": label, "url": url, "request_payload": body}
        logger.info(f"[추가이미지 PUT 진단] {label} url={url} payload={body}")

        try:
            res = cafe24_api_request('PUT', url, json=body, timeout=20)
            try:
                res_json = res.json()
            except ValueError:
                res_json = {"raw_text": res.text[:2000]}

            attempt.update({
                "status": res.status_code,
                "response_body": str(res_json)[:2000],
                "error_message": None if res.status_code == 200 else extract_cafe24_error_message(
                    res_json, f"HTTP {res.status_code}"),
            })

            time.sleep(1.5)  # 반영 시차
            after = fetch_additional_images(product_no)
            attempt.update({
                "after": after,
                "changed": after != baseline,
                "ok": res.status_code == 200 and after != baseline,
            })
            logger.info(f"[추가이미지 PUT 진단] {label} status={res.status_code} 반영={after != baseline} after={after}")
        except Exception as e:
            attempt.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
            logger.error(f"[추가이미지 PUT 진단] {label} 예외: {e}")

        attempts.append(attempt)
        if attempt.get("ok"):
            logger.info(f"[추가이미지 PUT 진단] {label} 형식이 실제로 반영됨 - 이후 후보 중단")
            break
        time.sleep(0.4)

    winners = [a for a in attempts if a.get("ok")]
    return jsonify({
        "success": True,
        "product_no": product_no,
        "sent_path": ftp_path,
        "baseline_additional_images": baseline,
        "restore_hint": "원래 상태로 되돌리려면 baseline_additional_images의 이미지를 "
                        "카페24 관리자에서 다시 등록하세요.",
        "summary": {
            "working_method": winners[0]["label"] if winners else None,
            "working_payload": winners[0]["request_payload"] if winners else None,
            "any_worked": bool(winners),
        },
        "attempts": attempts,
    })


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

# 카페24 Admin API의 상품 목록 조회 limit 상한
CAFE24_PRODUCTS_MAX_LIMIT = 100
# 한 요청에서 처리할 기본 건수. 상품마다 이미지를 열어 비율을 확인해야 하므로 100건을
# 한 번에 처리하면 Render의 요청 타임아웃(약 30초)에 걸려 502가 난다. 페이지를 잘게
# 나눠 한 요청의 처리 시간을 짧게 유지한다.
CAFE24_PRODUCTS_DEFAULT_LIMIT = int(os.getenv('SEARCH_PAGE_SIZE', 30))


def build_product_search_params(search_type, keyword):
    """검색 조건(상품명/모델명/상품코드)을 카페24 API 파라미터로 변환한다."""
    params = {}
    if search_type == 'product_code':
        params['product_code'] = keyword
    elif search_type == 'model_name':
        # 카페24 model_name 파라미터는 완전일치(exact match)이며 등록된 모델명이 모두
        # 대문자이므로, 사용자가 대소문자를 섞어 입력해도 찾을 수 있도록 대문자로 변환한다.
        params['model_name'] = keyword.upper()
    else:
        params['product_name'] = keyword
    return params


def fetch_product_total_count(search_params):
    """검색 조건에 맞는 전체 상품 수를 조회한다 (GET /products/count).

    페이지네이션 종료 시점을 판단하는 용도. 실패하면 None을 반환하고, 호출부는
    "빈 페이지가 나올 때까지 계속 조회"하는 방식으로 대체한다."""
    try:
        res = cafe24_api_request('GET', f"{CAFE24_API_BASE}/products/count", params=search_params, timeout=15)
        if res.status_code >= 400:
            logger.warning(f"[상품 수 조회 실패] status={res.status_code} body={res.text}")
            return None

        payload = res.json()
        count = payload.get('count')
        if isinstance(count, dict):  # 일부 응답이 {"count": {"count": N}} 형태일 수 있음
            count = count.get('count')
        return int(count) if count is not None else None
    except Exception as e:
        logger.warning(f"[상품 수 조회 예외] {e}")
        return None


# 카페24 응답에서 추가이미지가 담겨 오는 키 후보. API 버전에 따라 이름이 다를 수 있어
# 알려진 형태를 모두 확인한다.
ADDITIONAL_IMAGE_KEYS = (
    # 진단으로 확인된 실제 키(언더스코어 없는 복수형). 나머지는 방어용 후보.
    'additionalimages', 'additionalimage', 'additional_images', 'additional_image',
)
# 추가이미지 1건은 {"big": url, "medium": url, "small": url} 객체로 온다.
# 변환 소스로는 가장 고화질인 "big"을 쓴다.
ADDITIONAL_IMAGE_URL_KEYS = ('big', 'medium', 'small', 'image', 'path', 'url')


def _normalize_additional_image(entry):
    """추가이미지 1건(문자열 또는 dict)에서 이미지 URL을 뽑는다."""
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        for key in ADDITIONAL_IMAGE_URL_KEYS:
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_additional_images(payload):
    """응답에서 추가이미지 URL 목록을 뽑는다.

    반환값은 (urls, found)이다. found는 "추가이미지 키가 응답에 존재했는지"로,
    빈 목록(추가이미지가 정말 없음)과 키 자체가 없음(조회 방식이 틀림)을 구분하기 위해
    필요하다 - 이 둘을 섞으면 전 상품에 불필요한 재조회를 돌게 된다."""
    if not isinstance(payload, dict):
        return [], False

    container = None
    found = False

    for key in ADDITIONAL_IMAGE_KEYS:
        if key in payload:
            container = payload[key]
            found = True
            break

    # {"product": {...}} / {"products": [...]} 로 한 겹 감싸인 응답도 처리
    if not found:
        product = _extract_single_product(payload)
        if isinstance(product, dict):
            for key in ADDITIONAL_IMAGE_KEYS:
                if key in product:
                    container = product[key]
                    found = True
                    break

    if not found:
        return [], False

    if isinstance(container, dict):
        container = [container]
    if not isinstance(container, list):
        return [], True

    urls = [url for url in (_normalize_additional_image(e) for e in container) if url]
    return urls, True


def fetch_additional_images(product_no):
    """상품 1건의 추가이미지를 전용 리소스로 조회한다.

    카페24는 상품 목록(GET /products) 응답에 추가이미지를 기본 포함하지 않으므로,
    embed로도 받지 못했을 때 이 엔드포인트로 상품마다 따로 조회한다."""
    try:
        res = cafe24_api_request(
            'GET', f"{CAFE24_API_BASE}/products/{product_no}/additionalimages", timeout=10
        )
        if res.status_code >= 400:
            logger.warning(
                f"[추가이미지 조회 실패] product_no={product_no} "
                f"status={res.status_code} body={res.text[:300]}"
            )
            return []

        payload = res.json()
        urls, _ = _extract_additional_images(payload)
        if not urls:
            logger.info(f"[추가이미지 조회] product_no={product_no} 0장 (응답={str(payload)[:300]})")
        return urls
    except Exception as e:
        logger.warning(f"[추가이미지 조회 예외] product_no={product_no}: {e}")
        return []


def attach_additional_images(products, budget_seconds=IMAGE_PROBE_BUDGET, max_workers=12):
    """추가이미지를 아직 못 받은 상품들에 대해 전용 리소스로 병렬 조회해 채운다.
    상품 수만큼 API 호출이 필요하므로 전체 시간 상한을 둔다."""
    targets = [p for p in products if not p.get('add_images_loaded')]
    if not targets:
        return

    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_to_product = {
            executor.submit(fetch_additional_images, p['product_no']): p for p in targets
        }
        done, not_done = futures_wait(future_to_product.keys(), timeout=budget_seconds)

        for future in done:
            product = future_to_product[future]
            try:
                product['add_images'] = future.result()
                product['add_images_loaded'] = True
            except Exception as e:
                logger.warning(f"[추가이미지 조회 예외] product_no={product['product_no']}: {e}")

        for future in not_done:
            future.cancel()
            logger.warning(
                f"[추가이미지 조회 시간초과] product_no={future_to_product[future]['product_no']}"
            )
    finally:
        executor.shutdown(wait=False)

    total = sum(len(p.get('add_images') or []) for p in targets)
    logger.info(
        f"[추가이미지 보완 조회] {len(targets)}개 상품 {time.monotonic() - started:.1f}초, "
        f"총 {total}장 확인"
    )


def is_product_available(product):
    """진열함 + 판매함 + 품절아님 상태인지 판정한다.

    값이 응답에 없으면(API 버전에 따라 필드가 빠질 수 있음) 임의로 제외하지 않고
    통과시킨다 - 조회되지 않은 필드 때문에 멀쩡한 상품이 사라지는 편이 더 나쁘다.
    제외 사유 목록을 함께 돌려준다."""
    reasons = []
    if str(product.get('display', '')).upper() == 'F':
        reasons.append('진열안함')
    if str(product.get('selling', '')).upper() == 'F':
        reasons.append('판매안함')
    if str(product.get('sold_out', '')).upper() == 'T':
        reasons.append('품절')
    return (not reasons), reasons


# [상품 검색 API] 카페24 Admin API로 상품 목록 조회 (offset 기반 페이지 단위)
@app.route('/api/products/search', methods=['GET'])
def search_products():
    """검색 조건에 맞는 상품을 offset/limit 한 페이지씩 조회해 필터링 결과를 돌려준다.

    카페24 API는 한 번에 최대 100건까지만 주므로 전체(예: 303건)를 보려면 offset을
    옮겨가며 반복 호출해야 한다. 한 번에 전부 처리하지 않고 페이지 단위로 나눠
    응답하는 이유는, 상품마다 원본 이미지를 열어 1:1 비율을 확인해야 해서 전체를
    한 요청에 담으면 응답이 너무 오래 걸리고(게이트웨이 타임아웃 위험) 진행 상황도
    보여줄 수 없기 때문이다. 프런트가 total_count를 보고 offset을 이어서 호출한다.
    """
    # 파라미터 파싱까지 포함해 라우트 전체를 감싼다. 잘못된 쿼리스트링(int 변환 실패
    # 등)으로 예외가 나도 HTML 에러 페이지가 아니라 항상 JSON을 돌려주기 위함이다.
    page_started = time.monotonic()
    offset = 0
    try:
        search_type = request.args.get('search_type', 'product_name')  # model_name | product_name | product_code
        keyword = request.args.get('keyword', '').strip()
        try:
            limit = min(int(request.args.get('limit', CAFE24_PRODUCTS_DEFAULT_LIMIT)), CAFE24_PRODUCTS_MAX_LIMIT)
            offset = max(int(request.args.get('offset', 0)), 0)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "limit/offset은 숫자여야 합니다."}), 400

        sort = request.args.get('sort', '').strip()
        order = request.args.get('order', '').strip()
        # 원본 대표이미지가 1:1인 상품만 표시 (변환 대상이 정방형 이미지이므로 기본 활성화)
        only_square = request.args.get('only_square', 'true').lower() != 'false'
        # 진열함 + 판매함 + 품절아님 상품만 표시
        only_available = request.args.get('only_available', 'true').lower() != 'false'

        if not keyword:
            return jsonify({"success": False, "error": "검색어를 입력해 주세요."}), 400

        search_params = build_product_search_params(search_type, keyword)
        # embed로 추가이미지를 함께 받아 상품마다 따로 호출하는 것을 피한다.
        # (지원되지 않으면 무시되거나 키가 안 오고, 그때는 전용 리소스로 보완 조회한다)
        params = {**search_params, "limit": limit, "offset": offset, "embed": "additionalimages"}
        if sort:
            params['sort'] = sort
        if order:
            params['order'] = order

        logger.info(f"카페24 상품 검색 요청: search_type={search_type} -> params={params}")

        # 전체 건수는 첫 페이지에서만 조회한다 (이후 페이지는 프런트가 이미 알고 있음)
        total_count = fetch_product_total_count(search_params) if offset == 0 else None
        if offset == 0:
            logger.info(f"[상품 전체 건수] 검색조건={search_params} -> {total_count}")

        res = cafe24_api_request('GET', f"{CAFE24_API_BASE}/products", params=params, timeout=20)
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

        fetched_count = len(raw_products)
        excluded = []
        products = []

        for p in raw_products:
            if not isinstance(p, dict):
                continue

            product_no = p.get('product_no')
            product_code = p.get('product_code', '')

            # [1차 필터] 진열/판매/품절 상태. 이미지 확인보다 먼저 걸러야 불필요한
            # 이미지 다운로드를 크게 줄일 수 있다.
            if only_available:
                available, reasons = is_product_available(p)
                if not available:
                    excluded.append({
                        "product_no": str(product_no),
                        "product_code": product_code,
                        "reason": " / ".join(reasons),
                    })
                    continue

            main_image = p.get('detail_image') or p.get('list_image') or ''
            main_image = main_image if isinstance(main_image, str) else ''
            filename = os.path.basename(main_image.split('?')[0]) if main_image else f"{product_no}.jpg"
            ext = os.path.splitext(filename)[1].lstrip('.').upper() or 'JPG'

            # 추가이미지는 embed로 함께 받아온 경우에만 여기서 채워진다. 키 자체가 없으면
            # (embed 미지원 등) 아래에서 전용 리소스로 다시 조회한다.
            add_images, add_images_found = _extract_additional_images(p)

            products.append({
                "product_no": str(product_no),
                "product_code": product_code,
                "product_name": p.get('product_name', ''),
                "image_url": main_image,
                "filename": filename,
                "format": ext,
                "add_images": add_images,
                "add_images_loaded": add_images_found,
                "display": p.get('display'),
                "selling": p.get('selling'),
                "sold_out": p.get('sold_out'),
            })

        excluded_status_count = len(excluded)

        # [2차 필터] 원본 대표이미지를 실제로 열어 1:1인 상품만 남긴다. 상품 수만큼
        # 요청이 필요하므로 병렬로 처리하고, 크기를 확인하지 못한 상품은 임의로 지우지
        # 않고 그대로 남긴다 (조회 실패를 "비정방형"으로 오판하지 않기 위함).
        if only_square and products:
            square_flags = probe_square_flags(products)

            kept = []
            for product, is_square in zip(products, square_flags):
                if is_square is False:
                    excluded.append({
                        "product_no": product['product_no'],
                        "product_code": product['product_code'],
                        "reason": "원본 대표이미지가 1:1 비율이 아님",
                    })
                    continue
                if is_square is None:
                    logger.warning(
                        f"[비율 확인 실패] product_no={product['product_no']} "
                        f"url={product['image_url']} - 목록에는 그대로 표시"
                    )
                    product['ratio_unknown'] = True
                kept.append(product)

            products = kept

        # 추가이미지 보완 조회: 목록 응답에 추가이미지 키가 아예 없었던 상품만 대상으로
        # 전용 리소스를 호출한다. 필터를 모두 통과한 상품에 대해서만 하므로 호출 수가 적다.
        if products:
            attach_additional_images(products)
        for product in products:
            product.pop('add_images_loaded', None)

        excluded_ratio_count = len(excluded) - excluded_status_count
        elapsed = time.monotonic() - page_started
        total_add_images = sum(len(p.get('add_images') or []) for p in products)
        logger.info(
            f"[페이지 처리 완료] offset={offset} 소요시간 {elapsed:.1f}초 - "
            f"조회 {fetched_count}건 중 {len(products)}건 표시 "
            f"(상태 제외 {excluded_status_count}건 / 비1:1 제외 {excluded_ratio_count}건 / "
            f"추가이미지 합계 {total_add_images}장)"
        )

        return jsonify({
            "success": True,
            "data": products,
            "offset": offset,
            "limit": limit,
            "elapsed_seconds": round(elapsed, 1),
            "fetched_count": fetched_count,        # 이번 페이지에서 카페24가 준 원본 건수
            "total_count": total_count,            # 검색 조건 전체 건수 (첫 페이지에서만 채워짐)
            "has_more": fetched_count == limit,    # 다음 페이지가 더 있을 가능성
            "excluded_count": len(excluded),
            "excluded_status_count": excluded_status_count,
            "excluded_ratio_count": excluded_ratio_count,
            "excluded": excluded,
            "requested_url": res.url
        })

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
        elapsed = time.monotonic() - page_started
        logger.exception(f"카페24 상품 검색 요청 중 예외 발생 (offset={offset} 소요시간 {elapsed:.1f}초)")
        return jsonify({
            "success": False,
            "error": f"카페24 상품 조회 중 오류가 발생했습니다: {str(e)}",
            "offset": offset,
            "elapsed_seconds": round(elapsed, 1),
        }), 502


# [1단계 API] 썸네일 변환 (미리보기 생성)
@app.route('/api/convert', methods=['POST'])
def convert_thumbnails():
    """선택된 이미지(대표/추가)를 모두 같은 가공모드로 1000x1400 변환한다.

    화면에서 체크 해제한 이미지는 아예 넘어오지 않으므로 변환하지 않는다.

    JSON 구조 예시:
    {
      "mode": "product",
      "products": [
        {
          "product_no": "676",
          "images": [
            {"key": "main",  "label": "대표",  "url": "https://.../a.jpg"},
            {"key": "add_0", "label": "추가1", "url": "https://.../b.jpg"}
          ]
        }
      ]
    }
    """
    data = request.json or {}
    mode = data.get('mode', 'product')  # 'product' -> 상하 여백 채우기 / 'model' -> 확대 후 중앙 가로 크롭
    products = data.get('products', [])
    results = []

    for item in products:
        p_no = item.get('product_no')
        images = item.get('images', []) or []
        image_results = []

        # 이미지 한 장이 실패해도 나머지 결과까지 버리지 않고 끝까지 진행한다.
        for img in images:
            key = str(img.get('key') or 'main')
            label = img.get('label') or key
            url = img.get('url')

            try:
                resp = fetch_image(url, timeout=10)

                source_name = os.path.basename((url or '').split('?')[0])
                ext = os.path.splitext(source_name)[1].lower() or '.jpg'
                # 상품/이미지별로 파일명을 고정해 재변환 시 같은 파일을 덮어쓰게 한다
                safe_key = re.sub(r'[^0-9A-Za-z_-]', '_', key)
                out_file = process_image_bytes(resp.content, f"{p_no}_{safe_key}{ext}", mode)

                image_results.append({
                    "key": key,
                    "label": label,
                    "status": "SUCCESS",
                    "source_url": url,
                    "processed_filename": out_file,
                    "preview_url": f"/static/processed_images/{out_file}",
                })
            except Exception as e:
                logger.exception(f"이미지 변환 실패 (product_no={p_no} key={key} url={url})")
                image_results.append({
                    "key": key,
                    "label": label,
                    "status": "FAIL",
                    "source_url": url,
                    "error": str(e),
                })

        success_count = sum(1 for r in image_results if r["status"] == "SUCCESS")
        logger.info(
            f"[변환] product_no={p_no} mode={mode} 요청 {len(images)}장 중 {success_count}장 성공"
        )

        results.append({
            "product_no": p_no,
            "status": "SUCCESS" if success_count else "FAIL",
            "images": image_results,
            "success_count": success_count,
            "fail_count": len(image_results) - success_count,
        })

    return jsonify({"success": True, "data": results})

# [다운로드 API] 변환된 이미지를 상품코드별 폴더로 묶어 ZIP으로 내려준다
@app.route('/api/download', methods=['POST'])
def download_processed_images():
    """선택된 상품의 변환 결과(대표이미지 + 추가이미지)를 ZIP으로 묶어 반환한다.

    체크 해제된 이미지는 items[].images에 담기지 않으므로 ZIP에도 포함되지 않는다.

    JSON 구조 예시:
    {
      "items": [
        {
          "product_code": "P0000ABC",
          "images": [
            {"label": "대표",  "filename": "processed_676_main.jpg"},
            {"label": "추가1", "filename": "processed_676_add_0.jpg"}
          ]
        }
      ]
    }
    """
    data = request.json or {}
    items = data.get('items', [])

    if not items:
        return jsonify({"success": False, "error": "다운로드할 상품이 없습니다."}), 400

    missing = []
    added_count = 0
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            # 상품코드를 폴더명으로 쓰므로 경로 구분자 등 위험한 문자는 걸러낸다
            raw_code = str(item.get('product_code') or item.get('product_no') or 'unknown')
            folder = re.sub(r'[^0-9A-Za-z가-힣._-]', '_', raw_code) or 'unknown'

            entries = [
                (img.get('label') or f'이미지{idx}', img.get('filename'))
                for idx, img in enumerate(item.get('images', []) or [], start=1)
                if img.get('filename')
            ]

            for label, stored_name in entries:
                label = re.sub(r'[^0-9A-Za-z가-힣._-]', '_', str(label)) or '이미지'
                stored_name = os.path.basename(stored_name)
                source_path = os.path.join(UPLOAD_FOLDER, stored_name)
                if not os.path.isfile(source_path):
                    missing.append({"product_code": raw_code, "filename": stored_name})
                    logger.warning(f"[다운로드] 파일 없음: {source_path}")
                    continue

                ext = os.path.splitext(stored_name)[1] or '.jpg'
                zf.write(source_path, f"{folder}/{folder}_{label}{ext}")
                added_count += 1

    if added_count == 0:
        return jsonify({
            "success": False,
            "error": "다운로드할 변환 파일을 찾지 못했습니다. 먼저 [1단계: 변환실행]을 진행해 주세요.",
            "missing": missing
        }), 404

    buffer.seek(0)
    if len(items) == 1:
        raw_code = str(items[0].get('product_code') or items[0].get('product_no') or 'thumbnails')
        base_name = re.sub(r'[^0-9A-Za-z가-힣._-]', '_', raw_code) or 'thumbnails'
    else:
        base_name = f"thumbnails_{time.strftime('%Y%m%d_%H%M%S')}"

    logger.info(f"[다운로드] {len(items)}개 상품 / {added_count}개 파일 ZIP 생성 (누락 {len(missing)}건)")
    return send_file(
        buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{base_name}.zip"
    )


# [2단계 API] FTP 업로드 & 카페24 전송
@app.route('/api/send-cafe24', methods=['POST'])
def send_to_cafe24():
    """체크된 이미지만 카페24에 반영한다.

    - 대표이미지가 없으면(체크 해제) detail/list/small/tiny 4종을 요청에서 아예 빼서
      기존 값이 그대로 유지되게 한다. 대표이미지는 "삭제" 개념이 없다.
    - 추가이미지는 보낸 배열로 통째로 교체되므로, 체크 해제된 것은 자연히 삭제된다.
    - 단, 애초에 추가이미지가 없는 상품(has_additional_images=false)에는 필드 자체를
      넣지 않는다. 빈 배열을 보내면 기존 이미지가 지워지기 때문이다.

    JSON 구조 예시:
    {
      "items": [
         {
           "product_no": "10",
           "main_filename": "processed_10_main.jpg",       // 대표 체크 해제 시 null
           "add_filenames": ["processed_10_add_0.jpg"],    // 체크된 추가이미지만
           "has_additional_images": true                    // 상품에 추가이미지가 존재하는지
         }
      ]
    }
    """
    data = request.json or {}
    items = data.get('items', [])

    success_list = []
    fail_list = []

    for item in items:
        p_no = item.get('product_no')
        main_filename = item.get('main_filename')
        add_filenames = item.get('add_filenames', []) or []
        has_additional_images = bool(item.get('has_additional_images'))

        try:
            # [반영 방식 - /api/debug/product-link-test 진단으로 확정]
            #   - POST /products/images(base64)는 201로 성공하지만 파일을 상세페이지
            #     에디터 저장소(/web/upload/NNEditor/)에 올릴 뿐 상품에 연결하지 않는다.
            #   - 상품 썸네일은 /web/product/big|medium|small|tiny/ 에 있어야 하므로
            #     NNEditor 경로를 PUT에 넣으면 "Wrong image path"가 난다.
            #   - 웹FTP로 /web/upload/thumbnail/에 올린 뒤 그 경로를 PUT에 넘기면
            #     카페24가 /web/product/... 로 옮겨 담아 실제로 반영된다.
            if not main_filename and not has_additional_images:
                logger.info(f"[송신 건너뜀] product_no={p_no} 반영할 이미지가 없음")
                fail_list.append({"product_no": p_no, "reason": "선택된 이미지가 없습니다."})
                continue

            # 반영 여부를 판정하려면 "변경 전" 값이 필요하다.
            baseline_images = _get_product_image_fields(p_no)
            logger.info(
                f"[송신 전 이미지 상태] product_no={p_no} {baseline_images} "
                f"대표={'포함' if main_filename else '유지(체크 해제)'} "
                f"추가이미지={len(add_filenames)}장 (상품 보유여부={has_additional_images})"
            )

            remote_dir = '/web/upload/thumbnail/'
            put_request = {"image_upload_type": CAFE24_IMAGE_UPLOAD_TYPE}

            # 1. 대표이미지: 체크됐을 때만 FTP 업로드 후 4종 필드에 지정한다.
            #    체크 해제 시에는 필드를 아예 넣지 않아 기존 값이 유지된다.
            if main_filename:
                main_filename = os.path.basename(main_filename)
                local_path = os.path.join(UPLOAD_FOLDER, main_filename)
                if not os.path.isfile(local_path):
                    raise Exception(f"변환된 대표이미지 파일을 찾을 수 없습니다: {local_path}")

                ftp_url = upload_to_ftp(local_path, main_filename, remote_dir=remote_dir)
                ftp_path = f"{remote_dir}{main_filename}"
                logger.info(f"[FTP 업로드 완료-대표] product_no={p_no} url={ftp_url} PUT 전달 경로={ftp_path}")

                # 4종을 모두 방금 올린 경로로 지정 (카페24가 크기별로 리사이징한다)
                put_request.update({
                    "detail_image": ftp_path,
                    "list_image": ftp_path,
                    "small_image": ftp_path,
                    "tiny_image": ftp_path,
                })
            else:
                logger.info(f"[대표이미지 유지] product_no={p_no} 체크 해제되어 4종 필드를 보내지 않음")

            # 2. 추가이미지: 조회 응답에서 읽을 때 쓰는 필드명이 'additional_image'이므로
            #    (위 search_products 참고) 쓰기도 같은 이름을 쓴다.
            #    이 필드는 "보낸 목록으로 통째로 교체"되므로 체크 해제된 것은 삭제된다.
            processed_add_urls = []
            for add_filename in add_filenames:
                add_filename = os.path.basename(add_filename)
                add_local_path = os.path.join(UPLOAD_FOLDER, add_filename)
                if not os.path.isfile(add_local_path):
                    logger.warning(f"[추가이미지 건너뜀] product_no={p_no} 파일 없음: {add_local_path}")
                    continue

                upload_to_ftp(add_local_path, add_filename, remote_dir=remote_dir)
                # 대표이미지와 동일하게 "http:// 절대URL"이 아니라 웹FTP 경로를 넘긴다
                # (절대URL 형태는 대표이미지에서 Wrong image path로 거부됐다).
                processed_add_urls.append(f"{remote_dir}{add_filename}")

            if has_additional_images:
                # 상품이 추가이미지를 가진 경우에만 필드를 보낸다. 빈 배열이면 전체 삭제가
                # 되지만, 이는 사용자가 모든 추가이미지를 체크 해제한 의도적 결과다.
                put_request[CAFE24_ADDITIONAL_IMAGE_FIELD] = processed_add_urls
                logger.info(
                    f"[추가이미지 반영] product_no={p_no} {len(processed_add_urls)}장 전송 "
                    f"{processed_add_urls}"
                )
            else:
                # 추가이미지가 없는 상품에는 필드 자체를 넣지 않는다 (실수로 지우지 않도록).
                logger.info(f"[추가이미지 필드 생략] product_no={p_no} 상품에 추가이미지가 없음")

            api_url = f"{CAFE24_API_BASE}/products/{p_no}"
            request_body = {"shop_no": 1, "request": put_request}
            logger.info(f"[카페24 PUT 요청] product_no={p_no} url={api_url} payload={request_body}")

            res = cafe24_api_request('PUT', api_url, json=request_body, timeout=20)
            res_json = res.json()
            logger.info(f"[카페24 응답] product_no={p_no} status={res.status_code} body={res_json}")

            if res.status_code != 200:
                err_msg = extract_cafe24_error_message(res_json)
                logger.error(f"카페24 송신 실패 (product_no={p_no}): {err_msg}")
                fail_list.append({"product_no": p_no, "reason": err_msg})
                continue

            # 3. 반영 판정: 카페24는 모르는 필드를 200과 함께 조용히 무시하므로 200만으로는
            # 부족하다. PUT 응답 본문의 product가 방금 반영된 값을 그대로 돌려주므로 이를
            # 우선 보고, 아직 안 바뀌었으면 재조회로 한 번 더 확인한다.
            responded_product = _extract_single_product(res_json) or {}
            changed_fields = _changed_image_fields(baseline_images, responded_product)
            current_images = responded_product

            if not changed_fields:
                time.sleep(2)  # 카페24 반영 시차
                after_images = _get_product_image_fields(p_no)
                changed_fields = _changed_image_fields(baseline_images, after_images)
                current_images = after_images

            logger.info(
                f"[반영 확인] product_no={p_no} 바뀐필드={changed_fields} "
                f"before={ {f: baseline_images.get(f) for f in PRODUCT_IMAGE_FIELDS} } "
                f"after={ {f: current_images.get(f) for f in PRODUCT_IMAGE_FIELDS} }"
            )

            # 대표이미지를 보내지 않은 경우에는 4종 필드가 그대로인 것이 정상이므로
            # 변화 여부로 성공을 판정할 수 없다. 이때는 PUT 200을 성공으로 본다.
            if main_filename and not changed_fields:
                reason = (
                    "PUT은 200이지만 이미지 4종(detail/list/small/tiny) 중 어느 것도 "
                    "새 경로로 바뀌지 않았습니다."
                )
                logger.error(f"카페24 송신 실패 (product_no={p_no}): {reason} 응답={res_json}")
                fail_list.append({"product_no": p_no, "reason": reason})
                continue

            success_list.append({
                "product_no": p_no,
                "main_updated": bool(main_filename),
                "additional_sent": len(processed_add_urls) if has_additional_images else None,
                "changed_fields": changed_fields,
                "list_image": current_images.get('list_image'),
            })

            if main_filename:
                # 반영된 이미지를 실제로 내려받아 1:1.4 비율인지까지 확인한다
                # (진단 목적이며 송신 성공/실패 판정에는 영향 없음).
                verify_target = current_images.get('list_image') or current_images.get('detail_image')
                if verify_target:
                    verify_image_dimensions(p_no, 'list_image', verify_target)

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
