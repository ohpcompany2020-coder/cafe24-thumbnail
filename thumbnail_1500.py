"""1500x1800 다운로드 전용 변환.

1000x1400 변환/카페24 송신 로직(app.py)과 완전히 분리된 모듈이다. 결과 파일은
카페24로 송신되면 안 되므로 파일명 접두사(OUTPUT_PREFIX)로 구분한다.

지원하는 원본 비율(세로/가로, 오차 RATIO_TOLERANCE 이내):
  - 1:1.4 (5:7, 예: 1000x1400) - 기본. 위/아래를 각 1/14(1000x1400이면 100px)씩 잘라
    5:6(1000x1200)을 만든 뒤 1500x1800으로 확대한다. 제품컷/모델컷 결과가 같다.
  - 1:1 - 제품컷은 1500x1500 확대 후 위/아래 150px 흰 여백, 모델컷은 1800x1800 확대 후
    가로 중앙 1500px 크롭.
그 외 비율은 임의로 늘이거나 자르지 않고 UnsupportedRatioError로 거부한다.
"""
import io
import os

from PIL import Image

TARGET_W = 1500
TARGET_H = 1800
RATIO_TOLERANCE = 0.02
OUTPUT_PREFIX = 'processed1500_'
JPEG_QUALITY = 95


class UnsupportedRatioError(ValueError):
    """원본이 1:1 또는 1:1.4(5:7)가 아니어서 규칙대로 변환할 수 없을 때."""


def is_1500_output(filename):
    return os.path.basename(str(filename or '')).startswith(OUTPUT_PREFIX)


def _flatten_on_white(img):
    """투명/팔레트 이미지를 흰 배경 위에 합성한다 (기존 1000x1400 변환과 같은 방식)."""
    rgba = img.convert('RGBA')
    background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
    background.paste(rgba, (0, 0), rgba)
    return background.convert('RGB')


def _trim_5x7_to_1500x1800(img):
    """[5:7 원본] 위/아래 각 1/14을 잘라 5:6을 만들고 1500x1800으로 확대한다.
    (1000x1400 -> 위 100px, 아래 100px 제거 -> 1000x1200 -> 1.5배 확대)"""
    flat = _flatten_on_white(img)
    width, height = flat.size
    trim = round(height / 14)
    return flat.crop((0, trim, width, height - trim)).resize((TARGET_W, TARGET_H), Image.Resampling.LANCZOS)


def _pad_square_to_1500x1800(img):
    """[1:1 제품컷] 1500x1500으로 확대하고 위/아래에 흰색 150px씩 여백을 준다."""
    square = _flatten_on_white(img).resize((TARGET_W, TARGET_W), Image.Resampling.LANCZOS)
    canvas = Image.new('RGB', (TARGET_W, TARGET_H), (255, 255, 255))
    canvas.paste(square, (0, (TARGET_H - TARGET_W) // 2))
    return canvas


def _crop_square_to_1500x1800(img):
    """[1:1 모델컷] 1800x1800으로 확대하고 가로 중앙 1500px만 남긴다."""
    square = _flatten_on_white(img).resize((TARGET_H, TARGET_H), Image.Resampling.LANCZOS)
    left = (TARGET_H - TARGET_W) // 2
    return square.crop((left, 0, left + TARGET_W, TARGET_H))


def process_image_bytes_1500(image_bytes, filename, mode, output_dir):
    """원본을 1500x1800 JPEG로 변환해 output_dir에 저장하고 파일명을 반환한다.

    애니메이션 GIF는 기존 변환과 동일하게 마지막 프레임만 쓴다."""
    img = Image.open(io.BytesIO(image_bytes))
    if getattr(img, 'is_animated', False):
        img.seek(img.n_frames - 1)

    width, height = img.size
    ratio = (height / width) if width else 0

    if abs(ratio - 1.4) <= RATIO_TOLERANCE:
        result = _trim_5x7_to_1500x1800(img)
    elif abs(ratio - 1.0) <= RATIO_TOLERANCE:
        result = _crop_square_to_1500x1800(img) if mode == 'model' else _pad_square_to_1500x1800(img)
    else:
        raise UnsupportedRatioError(f"지원하지 않는 원본 비율 ({width}x{height})")

    assert result.size == (TARGET_W, TARGET_H), result.size

    base_name = os.path.splitext(filename)[0]
    output_filename = f"{OUTPUT_PREFIX}{base_name}.jpg"
    result.save(os.path.join(output_dir, output_filename), 'JPEG', quality=JPEG_QUALITY)
    return output_filename
