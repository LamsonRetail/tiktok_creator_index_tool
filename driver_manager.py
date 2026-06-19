import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


# CHROMEDRIVER_PATH = "chromedriver.exe"
BASE_PROFILE_DIR = os.path.abspath("chrome_profiles")

def get_user_dir(user_key: str) -> str:
    user_key = (user_key or "").strip() or "default_user"
    return os.path.join(BASE_PROFILE_DIR, user_key)

def get_login_profile_dir(user_key: str) -> str:
    return os.path.join(get_user_dir(user_key), "LOGIN")

def get_worker_profile_dir(user_key: str, worker_name: str) -> str:
    return os.path.join(get_user_dir(user_key), f"WORKER_{worker_name}")

def build_options(profile_dir: str, headless=False) -> Options:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")

    # stable flags (bạn đang dùng cũng cùng kiểu) :contentReference[oaicite:3]{index=3}
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-features=OptimizationGuideModelDownloading")
    opts.add_argument("--disable-features=UserAgentClientHint")
    opts.add_argument("--disable-features=MediaRouter")
    opts.add_argument("--window-size=1920,1080")

    os.makedirs(profile_dir, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--disable-features=UseChromeProfile")

    # anti automation
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    return opts

def init_driver(profile_dir: str, implicit_wait=5, headless=False):
    options = build_options(profile_dir, headless=headless)

    service = Service(
        ChromeDriverManager().install()
    )

    driver = webdriver.Chrome(
        service=service,
        options=options
    )
    driver.implicitly_wait(implicit_wait)
    return driver
