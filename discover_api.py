"""
DISCOVERY SCRIPT (chạy 1 lần) — Tìm endpoint API của trang chi tiết creator.

Mục đích: Mở trang chi tiết của 1 creator và GHI LẠI toàn bộ JSON mà
TikTok trả về (GMV, cộng tác, video, live, người theo dõi...). File JSON
dump ra thư mục api_dumps/ để xây dựng parser đọc thẳng API (không scrape DOM).

Cách chạy:
    python discover_api.py                 # dùng creator đầu tiên trong creators.xlsx
    python discover_api.py "tencreator"    # chỉ định handle/tên creator cụ thể

Yêu cầu: đã có file session chrome_profiles/admin_shared_session/tt_auth_state.json
(đăng nhập 1 lần qua tab ADMIN SETTINGS của app, hoặc copy từ tool cũ).
"""
import os
import sys
import json
import time

import pandas as pd
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from driver_manager import init_driver, get_user_dir, get_login_profile_dir

ADMIN_KEY = "admin_shared_session"
SHOP_URL = "https://affiliate.tiktok.com/connection/creator?shop_region=VN"
DETAIL_URL_BASE = "https://affiliate.tiktok.com/connection/creator/detail"
DUMP_DIR = os.path.abspath("api_dumps")

# Hook cài qua CDP -> chạy TRƯỚC mọi script của trang trên MỌI lần điều hướng,
# nên bắt được cả những API gọi ngay khi trang detail vừa load.
CAPTURE_HOOK_JS = r"""
(function(){
  if (window.__tt_cap_installed) return true;
  window.__tt_cap_installed = true;
  window.__tt_api_capture = [];
  window.__tt_sug_map = {};

  function tryParse(t){ try { return JSON.parse(t); } catch(e){ return null; } }
  function shouldCapture(url){
    return url && url.indexOf('/api/') !== -1 && /insights|creator|affiliate/i.test(url);
  }
  function record(url, method, postData, text){
    var j = tryParse(text);
    try {
      window.__tt_api_capture.push({ url: url, method: method, postData: postData || null, body: j });
    } catch(e) {}
    if (j && j.data && j.data.sug_contents){
      for (var i=0;i<j.data.sug_contents.length;i++){
        var item = j.data.sug_contents[i];
        if (!item || item.query_type !== 4 || !item.creator) continue;
        var h = item.creator.handle && item.creator.handle.value;
        var cid = item.creator.creator_oecuid && item.creator.creator_oecuid.value;
        if (h && cid) window.__tt_sug_map[String(h).toLowerCase()] = String(cid);
      }
    }
  }

  var origFetch = window.fetch;
  if (origFetch){
    window.fetch = function(){
      var args = arguments, url = args[0];
      try { url = (typeof url === 'string') ? url : (url && url.url) ? url.url : String(url); } catch(e){}
      var method = 'GET', postData = null;
      try { if (args[1] && args[1].method) method = args[1].method; } catch(e){}
      try { if (args[1] && args[1].body) postData = String(args[1].body); } catch(e){}
      return origFetch.apply(this, args).then(function(resp){
        try {
          if (shouldCapture(String(url))){
            resp.clone().text().then(function(t){ record(String(url), method, postData, t); }).catch(function(){});
          }
        } catch(e){}
        return resp;
      });
    };
  }

  var XHR = window.XMLHttpRequest;
  if (XHR){
    var oOpen = XHR.prototype.open, oSend = XHR.prototype.send;
    XHR.prototype.open = function(m, u){ try { this.__u = u; this.__m = m; } catch(e){} return oOpen.apply(this, arguments); };
    XHR.prototype.send = function(b){
      try {
        var self = this, u = self.__u || '';
        self.addEventListener('load', function(){
          try { if (shouldCapture(String(u))) record(String(u), self.__m || 'GET', b ? String(b) : null, self.responseText); } catch(e){}
        });
      } catch(e){}
      return oSend.apply(this, arguments);
    };
  }
  return true;
})();
"""


def install_hook_cdp(driver):
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": CAPTURE_HOOK_JS})


def import_auth_state(driver, auth_state_path):
    if not os.path.exists(auth_state_path):
        raise FileNotFoundError(f"Thiếu file session: {auth_state_path}")
    with open(auth_state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    driver.get("https://affiliate.tiktok.com/")
    time.sleep(1.5)
    for c in state.get("cookies") or []:
        try:
            c.pop("sameSite", None)
            driver.add_cookie(c)
        except Exception:
            pass
    try:
        driver.execute_script(
            """
            window.localStorage.clear();
            var data = arguments[0] || {};
            for (var k in data){ try { localStorage.setItem(k, data[k]); } catch(e){} }
            """,
            state.get("localStorage") or {},
        )
    except Exception:
        pass
    driver.refresh()
    time.sleep(2.2)


def get_cid(driver, wait, creator_name, timeout=8.0):
    driver.get(SHOP_URL)
    time.sleep(2.5)
    try:
        box = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[placeholder*='Tìm'], input[placeholder*='Search'], input")))
        box.click()
        box.send_keys(Keys.CONTROL + "a")
        box.send_keys(Keys.DELETE)
        box.send_keys(creator_name)
    except Exception as e:
        print(f"[WARN] Không gõ được vào ô tìm kiếm: {e}")
        return None

    creator_lc = creator_name.strip().lower()
    end = time.time() + timeout
    while time.time() < end:
        cid = driver.execute_script("return (window.__tt_sug_map||{})[arguments[0]] || null;", creator_lc)
        if cid:
            return str(cid)
        time.sleep(0.1)
    # fallback: lấy bất kỳ CID nào bắt được (nếu handle không khớp tuyệt đối)
    any_cid = driver.execute_script(
        "var m=window.__tt_sug_map||{};var k=Object.keys(m);return k.length?m[k[0]]:null;")
    return str(any_cid) if any_cid else None


def main():
    creator = sys.argv[1] if len(sys.argv) > 1 else None
    if not creator:
        df = pd.read_excel("creators.xlsx")
        creator = df["creator_name"].dropna().tolist()[0]
    print(f">>> Creator dùng để dò API: {creator!r}")

    os.makedirs(DUMP_DIR, exist_ok=True)
    auth_state_path = os.path.join(get_user_dir(ADMIN_KEY), "tt_auth_state.json")

    driver = init_driver(get_login_profile_dir(ADMIN_KEY))
    install_hook_cdp(driver)
    wait = WebDriverWait(driver, 20)

    try:
        print(">>> Khôi phục session đăng nhập...")
        import_auth_state(driver, auth_state_path)

        print(">>> Đang tìm CID qua API suggestions...")
        cid = get_cid(driver, wait, creator)
        if not cid:
            print("[LỖI] Không lấy được CID. Có thể session hết hạn hoặc gặp captcha.")
            print(">>> Hãy đăng nhập lại qua tab ADMIN SETTINGS rồi SAVE SESSION DATA.")
            input(">>> Nếu muốn tự đăng nhập trong cửa sổ này rồi thử lại, đăng nhập xong nhấn ENTER...")
            cid = get_cid(driver, wait, creator)
            if not cid:
                return
        print(f">>> CID: {cid}")

        detail_url = f"{DETAIL_URL_BASE}?cid={cid}&shop_region=VN"
        print(f">>> Mở trang chi tiết: {detail_url}")
        # reset buffer ngay trước khi điều hướng để chỉ giữ API của trang detail
        driver.execute_script("if(window.__tt_api_capture) window.__tt_api_capture.length=0;")
        driver.get(detail_url)
        time.sleep(6)

        # Scroll để kích hoạt các block lazy-load (người theo dõi, xu hướng...)
        for _ in range(4):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(3)

        captured = driver.execute_script("return window.__tt_api_capture || [];")
        print(f">>> Đã bắt được {len(captured)} response API.")

        index = []
        for i, c in enumerate(captured):
            url = (c.get("url") or "")
            path = url.split("?")[0]
            tag = "_".join(path.split("/")[-4:]) or f"resp{i}"
            tag = "".join(ch if ch.isalnum() or ch == "_" else "" for ch in tag)
            fname = f"detail_{i:02d}_{tag}.json"
            with open(os.path.join(DUMP_DIR, fname), "w", encoding="utf-8") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
            index.append({"file": fname, "method": c.get("method"), "url": url,
                          "postData": c.get("postData")})
            print(f"   [{i:02d}] {c.get('method')} {path}")

        with open(os.path.join(DUMP_DIR, "_index.json"), "w", encoding="utf-8") as f:
            json.dump({"creator": creator, "cid": cid, "responses": index}, f,
                      ensure_ascii=False, indent=2)

        print(f"\n>>> XONG. Mở thư mục: {DUMP_DIR}")
        print(">>> Gửi lại nội dung thư mục api_dumps/ (hoặc file _index.json) để mình viết parser.")
    finally:
        input(">>> Nhấn ENTER để đóng trình duyệt...")
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
