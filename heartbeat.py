"""
Heartbeat keep-alive cho phiên đăng nhập TikTok Affiliate.

Chạy độc lập (không cần Streamlit). Mỗi lần chạy:
  1. Mở Chrome (headless) với profile admin_shared_session/LOGIN
  2. Vào affiliate.tiktok.com để TikTok refresh cookie/token
  3. Ghi log + đóng

Mục tiêu: giữ phiên đăng nhập "ấm" → không bị TikTok timeout do idle.
Vì dùng đúng profile LOGIN (nguồn của tất cả WORKER_*), session refresh
ở đây có hiệu lực cho mọi worker copy sau đó.

Tắt màn hình KHÔNG ảnh hưởng — Chrome chạy ở process nền, không cần display.
Chỉ cần máy không sleep/hibernate.
"""

import os
import sys
import time
import random
from datetime import datetime

# Ép cwd về thư mục chứa file để Task Scheduler chạy đúng
_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)
sys.path.insert(0, _HERE)

from driver_manager import init_driver, get_login_profile_dir

ADMIN_KEY = "admin_shared_session"
SHOP_URL = "https://affiliate.tiktok.com/connection/creator?shop_region=VN"
LOG_FILE = os.path.join(_HERE, "heartbeat.log")


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _is_logged_in(driver) -> bool:
    """Phát hiện trạng thái đăng nhập qua URL & marker DOM."""
    try:
        url = driver.current_url or ""
        if "login" in url or "passport" in url:
            return False
        # Cookie sessionid_ss/sid_tt là dấu hiệu đã đăng nhập
        cookies = {c["name"] for c in driver.get_cookies()}
        return any(k in cookies for k in ("sessionid_ss", "sid_tt", "passport_csrf_token"))
    except Exception:
        return False


def run_heartbeat(headless: bool = True, dwell_seconds: int = None) -> int:
    """Trả về 0 nếu phiên còn sống, 1 nếu mất phiên / lỗi."""
    if dwell_seconds is None:
        dwell_seconds = random.randint(25, 55)

    profile = get_login_profile_dir(ADMIN_KEY)
    if not os.path.isdir(profile):
        _log(f"FAIL: profile dir không tồn tại: {profile}")
        return 1

    _log(f"START heartbeat (headless={headless}, dwell={dwell_seconds}s)")
    driver = None
    try:
        driver = init_driver(profile, headless=headless)
        driver.set_page_load_timeout(60)
        driver.get(SHOP_URL)
        time.sleep(random.uniform(4, 7))

        # Cuộn nhẹ để TikTok thấy "có hoạt động"
        for _ in range(random.randint(2, 4)):
            try:
                driver.execute_script(
                    f"window.scrollBy(0, {random.randint(200, 700)});"
                )
            except Exception:
                pass
            time.sleep(random.uniform(1.5, 3.0))

        ok = _is_logged_in(driver)
        url = driver.current_url
        _log(f"URL={url}  logged_in={ok}")

        # Lưu lại auth state mới (cookie có thể đã được refresh)
        time.sleep(dwell_seconds)

        return 0 if ok else 1
    except Exception as e:
        _log(f"ERROR: {e!r}")
        return 1
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        _log("END heartbeat\n")


if __name__ == "__main__":
    # CLI flags:
    #   --visible   chạy với cửa sổ Chrome hiện ra (để debug lần đầu)
    headless = "--visible" not in sys.argv
    code = run_heartbeat(headless=headless)
    sys.exit(code)
