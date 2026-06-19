import os
import time
import json
import re
from typing import Optional, Dict, Any, List
from multiprocessing import Queue

import pandas as pd
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from plyer import notification
except Exception:  # plyer optional
    notification = None

from driver_manager import init_driver

SHOP_URL = "https://affiliate.tiktok.com/connection/creator?shop_region=VN"
DETAIL_URL_BASE = "https://affiliate.tiktok.com/connection/creator/detail"
SUGGESTIONS_ENDPOINT_PATH = "/api/v1/insights/affiliate/creator/search/suggestions"
PROFILE_ENDPOINT_PATH = "/api/v1/oec/affiliate/creator/marketplace/profile"

# =========================
# FIELD CATALOG (for UI selection) — giữ nguyên 100% so với tool cũ
# =========================
FIELD_CATALOG = [
    # BÁN HÀNG
    {"key": "sales_gmv", "label": "GMV", "group": "Bán hàng"},
    {"key": "sales_items_sold", "label": "Số món bán ra", "group": "Bán hàng"},
    {"key": "sales_gpm", "label": "GPM", "group": "Bán hàng"},
    {"key": "sales_gmv_per_user", "label": "GMV mỗi khách hàng", "group": "Bán hàng"},
    {"key": "sales_chart_channel", "label": "GMV theo kênh bán", "group": "Bán hàng"},
    {"key": "sales_chart_category", "label": "GMV theo hạng mục sản phẩm", "group": "Bán hàng"},

    # CỘNG TÁC
    {"key": "collab_freq", "label": "Tần suất đăng bài ước tính", "group": "Cộng tác"},
    {"key": "collab_comm", "label": "Tỷ lệ hoa hồng", "group": "Cộng tác"},
    {"key": "collab_prods", "label": "Số sản phẩm", "group": "Cộng tác"},
    {"key": "collab_brands", "label": "Thương hiệu đã cộng tác", "group": "Cộng tác"},
    {"key": "collab_price", "label": "Giá sản phẩm", "group": "Cộng tác"},

    # VIDEO
    {"key": "video_gpm", "label": "GPM Video", "group": "Video"},
    {"key": "video_count", "label": "Số video", "group": "Video"},
    {"key": "video_view_avg", "label": "Lượt xem video trung bình", "group": "Video"},
    {"key": "video_interact_avg", "label": "Tỷ lệ tương tác video", "group": "Video"},
    {"key": "video_like_avg", "label": "Lượt thích video trung bình", "group": "Video"},
    {"key": "video_comment_avg", "label": "Lượt bình luận video trung bình", "group": "Video"},
    {"key": "video_share_avg", "label": "Lượt chia sẻ video trung bình", "group": "Video"},

    # LIVE
    {"key": "live_gpm", "label": "GPM LIVE", "group": "LIVE"},
    {"key": "live_count", "label": "Số buổi LIVE", "group": "LIVE"},
    {"key": "live_view_avg", "label": "Lượt xem LIVE trung bình", "group": "LIVE"},
    {"key": "live_interact_avg", "label": "Tỷ lệ tương tác LIVE", "group": "LIVE"},
    {"key": "live_like_avg", "label": "Lượt thích LIVE trung bình", "group": "LIVE"},
    {"key": "live_comment_avg", "label": "Lượt bình luận LIVE trung bình", "group": "LIVE"},
    {"key": "live_share_avg", "label": "Lượt chia sẻ LIVE trung bình", "group": "LIVE"},

    # NGƯỜI THEO DÕI
    {"key": "follower_gender", "label": "Giới tính người theo dõi", "group": "Người theo dõi"},
    {"key": "follower_age", "label": "Độ tuổi người theo dõi", "group": "Người theo dõi"},
    {"key": "follower_location", "label": "Địa điểm hàng đầu", "group": "Người theo dõi"},
]

FIELD_LABEL_MAP = {item["key"]: item["label"] for item in FIELD_CATALOG}
FIELD_LABEL_MAP.update({
    "creator_name": "Tên creator",
    "status": "Trạng thái",
})


def get_field_catalog() -> List[Dict[str, str]]:
    return FIELD_CATALOG


# =========================
# AUTH: EXPORT / IMPORT (giữ nguyên)
# =========================

def _dump_local_storage(driver) -> Dict[str, str]:
    return driver.execute_script(
        """
        const out = {};
        for (let i=0; i<localStorage.length; i++){
          const k = localStorage.key(i);
          out[k] = localStorage.getItem(k);
        }
        return out;
        """
    )


def _restore_local_storage(driver, data: Dict[str, str]):
    driver.execute_script("window.localStorage.clear();")
    driver.execute_script(
        """
        const data = arguments[0] || {};
        for (const k of Object.keys(data)){
          try { localStorage.setItem(k, data[k]); } catch(e) {}
        }
        """,
        data or {},
    )


def export_auth_state(driver, auth_state_path: str) -> Dict[str, Any]:
    driver.get("https://affiliate.tiktok.com/")
    time.sleep(2.0)
    state = {"cookies": [], "localStorage": {}}
    try:
        state["cookies"] = driver.get_cookies()
    except Exception:
        state["cookies"] = []
    try:
        state["localStorage"] = _dump_local_storage(driver)
    except Exception:
        state["localStorage"] = {}
    os.makedirs(os.path.dirname(auth_state_path), exist_ok=True)
    with open(auth_state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state


def import_auth_state(driver, auth_state_path: str):
    if not os.path.exists(auth_state_path):
        raise FileNotFoundError(f"Missing auth_state: {auth_state_path}")
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
        _restore_local_storage(driver, state.get("localStorage") or {})
    except Exception:
        pass
    driver.refresh()
    time.sleep(2.2)


# =========================
# API INTERCEPTION HOOK (cài qua CDP -> chạy trước script của trang)
# Bắt cả suggestions (lấy CID) và marketplace/profile (toàn bộ số liệu).
# =========================
CAPTURE_HOOK_JS = r"""
(function(){
  if (window.__tt_cap_installed) return true;
  window.__tt_cap_installed = true;
  window.__tt_api_capture = [];
  window.__tt_sug_map = {};

  function tryParse(t){ try { return JSON.parse(t); } catch(e){ return null; } }
  function shouldCapture(url){
    return url && url.indexOf('/api/') !== -1 &&
           (url.indexOf('creator/marketplace/profile') !== -1 ||
            url.indexOf('creator/search/suggestions') !== -1);
  }
  function record(url, method, postData, text){
    var j = tryParse(text);
    try { window.__tt_api_capture.push({ url: url, method: method, postData: postData || null, body: j }); } catch(e) {}
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


def install_capture_hook(driver):
    """Cài hook qua CDP để nó chạy trên MỌI document mới (trước script của TikTok)."""
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": CAPTURE_HOOK_JS}
        )
        return True
    except Exception:
        return False


def _clear_capture(driver):
    try:
        driver.execute_script(
            "if(window.__tt_api_capture) window.__tt_api_capture.length=0; window.__tt_sug_map={};"
        )
    except Exception:
        pass


# =========================
# CAPTCHA (giữ phát hiện + thông báo + chờ giải tay)
# =========================

def handle_tiktok_captcha(driver, wait, worker_name="?", creator_name="?", progress_q=None):
    try:
        if "Server error" in driver.page_source:
            driver.refresh()
            time.sleep(3)
    except Exception:
        pass

    try:
        if not driver.find_elements(By.CLASS_NAME, "captcha_verify_container"):
            return False
    except Exception:
        return False

    if notification:
        try:
            notification.notify(
                title=f"⚠️ TIKTOK CAPTCHA - MÁY {worker_name}",
                message=f"Phát hiện xác thực khi crawl: {creator_name}. Vui lòng giải tay để tiếp tục!",
                app_name="TikTok Crawler Elite",
                timeout=10,
            )
        except Exception:
            pass

    if progress_q:
        progress_q.put(("captcha_alert", worker_name, creator_name))
    print(f"\n[!] MÁY {worker_name} ĐANG ĐỢI GIẢI CAPTCHA cho {creator_name}...")
    input(f">>> [MÁY {worker_name}] Nhấn ENTER sau khi đã giải xong trên trình duyệt...")
    if progress_q:
        progress_q.put(("captcha_resolved", worker_name))
    return True


# =========================
# CID (qua suggestions hook — như tool cũ)
# =========================

def get_cid_from_hook(driver, creator_name: str, timeout=6.5) -> Optional[str]:
    creator_lc = creator_name.strip().lower()
    end = time.time() + timeout
    while time.time() < end:
        cid = driver.execute_script("return (window.__tt_sug_map||{})[arguments[0]] || null;", creator_lc)
        if cid:
            return str(cid)
        time.sleep(0.08)
    return None


def resolve_cid(driver, wait, creator_name: str) -> Optional[str]:
    driver.get(SHOP_URL)
    time.sleep(2.0)
    handle_tiktok_captcha(driver, wait, creator_name=creator_name)
    _clear_capture(driver)

    try:
        box = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[placeholder*='Tìm'], input[placeholder*='Search'], input")))
        box.click()
        box.send_keys(Keys.CONTROL + "a")
        box.send_keys(Keys.DELETE)
        box.send_keys(creator_name)
        time.sleep(0.22)
    except Exception:
        return None

    cid = get_cid_from_hook(driver, creator_name, timeout=6.5)
    if not cid:
        try:
            box.send_keys(Keys.END)
            box.send_keys(" ")
            time.sleep(0.12)
            box.send_keys(Keys.BACKSPACE)
        except Exception:
            pass
        cid = get_cid_from_hook(driver, creator_name, timeout=5.0)
    return cid


# =========================
# FETCH PROFILE: mở trang detail, bắt JSON marketplace/profile, gộp creator_profile
# =========================

def fetch_creator_profile(driver, cid: str, timeout=18.0) -> Dict[str, Any]:
    _clear_capture(driver)
    detail_url = f"{DETAIL_URL_BASE}?cid={cid}&shop_region=VN"
    driver.get(detail_url)

    merged: Dict[str, Any] = {}
    got_types = set()
    end = time.time() + timeout
    scrolled = False
    while time.time() < end:
        caps = driver.execute_script("return window.__tt_api_capture || [];") or []
        for c in caps:
            url = c.get("url") or ""
            if "creator/marketplace/profile" not in url:
                continue
            body = c.get("body") or {}
            cp = (body or {}).get("creator_profile") or {}
            try:
                pt = json.loads(c.get("postData") or "{}").get("profile_types")
            except Exception:
                pt = None
            for k, v in cp.items():
                cur = merged.get(k)
                # ưu tiên field đã authorized + có value
                better = isinstance(v, dict) and v.get("is_authorized") and v.get("value") not in (None, "")
                cur_ok = isinstance(cur, dict) and cur.get("is_authorized") and cur.get("value") not in (None, "")
                if k not in merged or (better and not cur_ok):
                    merged[k] = v
            if pt:
                got_types.update(pt if isinstance(pt, list) else [pt])

        if {2, 3}.issubset(got_types):
            break
        # cuộn để kích hoạt block lazy-load (người theo dõi)
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except Exception:
            pass
        scrolled = True
        time.sleep(0.7)

    return merged


# =========================
# PARSE HELPERS
# =========================
_CHANNEL_LABELS = {"live_gmv": "LIVE", "video_gmv": "Video",
                   "showcase_gmv": "Thẻ sản phẩm", "product_card_gmv": "Thẻ sản phẩm"}
_GENDER_LABELS = {"male": "Nam", "female": "Nữ", "unknown": "Khác"}


def _get(cp: Dict[str, Any], *keys):
    """Trả về value đầu tiên có is_authorized=True và value khác rỗng."""
    for k in keys:
        o = cp.get(k)
        if isinstance(o, dict) and o.get("is_authorized") and o.get("value") not in (None, ""):
            return o.get("value")
    return None


def _fmt_metric(v) -> str:
    if v is None:
        return "N/A"
    if isinstance(v, dict):
        if v.get("format"):
            return str(v["format"])
        if "minimal_format" in v or "maximum_format" in v:
            mn, mx = v.get("minimal_format"), v.get("maximum_format")
            if mn and mx and mn != mx:
                return f"{mn} - {mx}"
            return str(mn or mx or "N/A")
        if "value" in v:
            return str(v["value"])
        return "N/A"
    return str(v)


def _to_num(v):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return None


def _pct(v) -> Optional[str]:
    n = _to_num(v)
    if n is None:
        return None
    s = f"{n * 100:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",") + "%"


# Hệ số quy đổi đơn vị viết tắt của TikTok sang số đầy đủ
#   K = nghìn (1.000), M / Tr = triệu (1.000.000), T / B = tỷ (1.000.000.000)
_UNIT_MUL = {
    "k": 1_000,
    "m": 1_000_000, "tr": 1_000_000, "tră": 1_000_000,
    "t": 1_000_000_000, "b": 1_000_000_000, "ty": 1_000_000_000, "tỷ": 1_000_000_000,
}
# Khớp: số (dấu phẩy thập phân kiểu VN) + đơn vị (tùy chọn) + ký hiệu tiền (₫ hoặc đ)
_MONEY_TOKEN = re.compile(r"([0-9][0-9.\s]*(?:,[0-9]+)?)\s*(Tr|tr|TR|K|k|M|m|T|t|B|b)?\s*[₫đ]")


def _vn_int(n) -> str:
    """Số nguyên với dấu chấm phân cách hàng nghìn kiểu Việt Nam."""
    return f"{int(round(float(n))):,}".replace(",", ".")


def _expand_money_str(s: str) -> str:
    """Quy đổi chuỗi tiền viết tắt ('1M₫+', '131,7K đ-263,4K đ') -> số đầy đủ có phân cách."""
    def repl(m):
        num_raw, unit = m.group(1), (m.group(2) or "")
        num = num_raw.strip().replace(".", "").replace(" ", "").replace(",", ".")
        try:
            val = float(num)
        except Exception:
            return m.group(0)
        mul = _UNIT_MUL.get(unit.lower(), 1) if unit else 1
        return _vn_int(val * mul) + " ₫"
    return _MONEY_TOKEN.sub(repl, str(s))


def _fmt_money(v) -> str:
    """Định dạng tiền: ưu tiên số thô; nếu chỉ có chuỗi viết tắt thì quy đổi ra số đầy đủ."""
    if v is None:
        return "N/A"
    if isinstance(v, dict):
        # value là số thô trực tiếp
        val = v.get("value")
        if val is not None and not isinstance(val, (list, dict)):
            n = _to_num(val)
            if n is not None:
                return _vn_int(n) + " ₫"
        # value lồng dict (vd gpm: value={format, value})
        if isinstance(val, dict):
            return _fmt_money(val)
        # min/max số thô
        if v.get("minimal") is not None or v.get("maximum") is not None:
            mn, mx = _to_num(v.get("minimal")), _to_num(v.get("maximum"))
            if mn is not None and mx is not None:
                if abs(mn - mx) < 1:
                    return _vn_int(mn) + " ₫"
                return f"{_vn_int(mn)} ₫ - {_vn_int(mx)} ₫"
            one = mn if mn is not None else mx
            if one is not None:
                return _vn_int(one) + " ₫"
        # fallback: chuỗi định dạng viết tắt
        for fk in ("format", "minimal_format", "maximum_format"):
            if v.get(fk):
                return _expand_money_str(v[fk])
        return "N/A"
    return _expand_money_str(v)


def _fmt_locations(lst, sep=" - ") -> str:
    """Địa điểm hàng đầu: API trả số đếm thô -> quy ra % theo tổng các địa điểm trả về."""
    if not isinstance(lst, list) or not lst:
        return "N/A"
    pairs, total = [], 0.0
    for it in lst:
        if not isinstance(it, dict):
            continue
        cnt = _to_num(it.get("value"))
        name = it.get("key") or it.get("name")
        if cnt is None or name is None:
            continue
        pairs.append((name, cnt))
        total += cnt
    if not pairs or total <= 0:
        return "N/A"
    parts = []
    for name, cnt in pairs:
        p = f"{cnt / total * 100:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        parts.append(f"{name}: {p}%")
    return sep.join(parts)


def _fmt_groups(lst, label_map=None, sep=" - ", use_name=False) -> str:
    if not isinstance(lst, list) or not lst:
        return "N/A"
    parts = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        if use_name:
            label = it.get("name") or it.get("key")
        else:
            key = it.get("key")
            label = (label_map or {}).get(key, key)
        pct = _pct(it.get("value"))
        if label is not None and pct is not None:
            parts.append(f"{label}: {pct}")
    return sep.join(parts) if parts else "N/A"


def _interact_rate(cp, like_k, comment_k, share_k, view_k) -> str:
    like = _to_num(_get(cp, like_k)) or 0
    comment = _to_num(_get(cp, comment_k)) or 0
    share = _to_num(_get(cp, share_k)) or 0
    view = _to_num(_get(cp, view_k))
    if not view:
        return "N/A"
    rate = (like + comment + share) / view * 100
    return f"{rate:.2f}".rstrip("0").rstrip(".").replace(".", ",") + "%"


def map_profile_to_row(cp: Dict[str, Any], selected: Optional[set]) -> Dict[str, Any]:
    row: Dict[str, Any] = {}

    def put(key, value):
        if selected is None or key in selected:
            row[key] = value

    # SALES
    put("sales_gmv", _fmt_money(_get(cp, "med_gmv_revenue", "med_gmv_revenue_range")))
    put("sales_items_sold", _fmt_metric(_get(cp, "units_sold", "units_sold_range")))
    put("sales_gpm", _fmt_money(_get(cp, "gpm", "gpm_range")))
    put("sales_gmv_per_user", _fmt_money(_get(cp, "avg_revenue_per_buyer", "avg_revenue_per_buyer_range")))
    put("sales_chart_channel", _fmt_groups(_get(cp, "content_groups"), label_map=_CHANNEL_LABELS))
    put("sales_chart_category", _fmt_groups(_get(cp, "industry_groups"), use_name=True))

    # COLLAB
    freq = _get(cp, "ec_video_publish_cnt_30d", "video_publish_cnt_30d")
    put("collab_freq", f"{freq} video/30 ngày" if freq not in (None, "") else "N/A")
    put("collab_comm", _fmt_metric(_get(cp, "med_commission_rate", "med_commission_rate_range")))
    put("collab_prods", _fmt_metric(_get(cp, "promoted_product_num", "product_cnt")))
    put("collab_brands", _fmt_metric(_get(cp, "collaborated_brands_num")))
    put("collab_price", _fmt_money(_get(cp, "product_price_range")))

    # VIDEO (ưu tiên chỉ số EC, fallback chỉ số thường)
    put("video_gpm", _fmt_money(_get(cp, "ec_video_gpm")))
    put("video_count", _fmt_metric(_get(cp, "ec_video_publish_cnt_30d", "video_publish_cnt_30d")))
    put("video_view_avg", _fmt_metric(_get(cp, "ec_video_med_view_cnt", "video_med_view_cnt")))
    put("video_interact_avg", _interact_rate(cp, "ec_video_med_like_cnt", "ec_video_med_comment_cnt",
                                             "ec_video_med_share_cnt", "ec_video_med_view_cnt"))
    put("video_like_avg", _fmt_metric(_get(cp, "ec_video_med_like_cnt", "video_med_like_cnt")))
    put("video_comment_avg", _fmt_metric(_get(cp, "ec_video_med_comment_cnt", "video_med_comment_cnt")))
    put("video_share_avg", _fmt_metric(_get(cp, "ec_video_med_share_cnt", "video_med_share_cnt")))

    # LIVE
    put("live_gpm", _fmt_money(_get(cp, "ec_live_gpm")))
    put("live_count", _fmt_metric(_get(cp, "ec_live_streaming_cnt_30d", "live_streaming_cnt_30d")))
    put("live_view_avg", _fmt_metric(_get(cp, "ec_live_med_view_cnt", "live_med_view_cnt")))
    put("live_interact_avg", _interact_rate(cp, "ec_live_med_like_cnt", "ec_live_med_comment_cnt",
                                            "ec_live_med_share_cnt", "ec_live_med_view_cnt"))
    put("live_like_avg", _fmt_metric(_get(cp, "ec_live_med_like_cnt", "live_med_like_cnt")))
    put("live_comment_avg", _fmt_metric(_get(cp, "ec_live_med_comment_cnt", "live_med_comment_cnt")))
    put("live_share_avg", _fmt_metric(_get(cp, "ec_live_med_share_cnt", "live_med_share_cnt")))

    # FOLLOWERS
    put("follower_gender", _fmt_groups(_get(cp, "follower_genders_v2"), label_map=_GENDER_LABELS))
    put("follower_age", _fmt_groups(_get(cp, "follower_ages_v2"), sep=" / "))
    put("follower_location", _fmt_locations(_get(cp, "follower_state_location")))

    return row


# =========================
# CRAWL 1 CREATOR (qua API, không scrape DOM)
# =========================

def crawl_one_creator(driver, wait, name: str, selected_fields: Optional[set] = None,
                      worker_name="?", progress_q=None) -> Dict[str, Any]:
    row: Dict[str, Any] = {"creator_name": name, "status": "Pending"}

    cid = resolve_cid(driver, wait, name)
    if not cid:
        handle_tiktok_captcha(driver, wait, worker_name, name, progress_q)
        row["status"] = "Click Failed"
        return row

    try:
        cp = fetch_creator_profile(driver, cid)
        if not cp:
            handle_tiktok_captcha(driver, wait, worker_name, name, progress_q)
            row["status"] = "No API Data"
            return row
        row.update(map_profile_to_row(cp, selected_fields))
        row["status"] = "Success"
        return row
    except Exception as e:
        row["status"] = f"Error Crawl: {e}"
        return row


# =========================
# WORKER (Process target) — chữ ký giữ nguyên để app.py không đổi
# =========================

def worker_crawl(
    worker_name: str,
    creators: List[str],
    profile_dir: str,
    auth_state_path: str,
    out_xlsx: str,
    progress_q: Optional[Queue] = None,
    headless: bool = False,
    selected_fields: Optional[List[str]] = None,
):
    driver = init_driver(profile_dir, headless=headless)
    install_capture_hook(driver)
    wait = WebDriverWait(driver, 15)

    try:
        import_auth_state(driver, auth_state_path)
    except Exception as e:
        if progress_q:
            progress_q.put(("fatal", worker_name, f"Import session FAILED: {e}"))
        driver.quit()
        return

    results = []
    total = len(creators)
    selected_set = set(selected_fields) if selected_fields else None

    def _order_df(df: pd.DataFrame) -> pd.DataFrame:
        if selected_fields:
            base_cols = [c for c in ["creator_name", "status"] if c in df.columns]
            other_cols = [c for c in selected_fields if c in df.columns and c not in base_cols]
            remaining = [c for c in df.columns if c not in base_cols + other_cols]
            return df[base_cols + other_cols + remaining]
        return df

    def _rename_df_headers(df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns=lambda c: FIELD_LABEL_MAP.get(c, c))

    for i, name in enumerate(creators, start=1):
        t0 = time.time()
        try:
            row = crawl_one_creator(driver, wait, name, selected_fields=selected_set,
                                    worker_name=worker_name, progress_q=progress_q)
        except Exception as e:
            row = {"creator_name": name, "status": f"Error: {e}"}

        results.append(row)

        if progress_q:
            progress_q.put(("done", worker_name, name, row.get("status", ""), i, total, round(time.time() - t0, 2)))

        try:
            _rename_df_headers(_order_df(pd.DataFrame(results))).to_excel(out_xlsx, index=False)
        except Exception:
            pass

    _rename_df_headers(_order_df(pd.DataFrame(results))).to_excel(out_xlsx, index=False)
    driver.quit()

    if progress_q:
        progress_q.put(("finish", worker_name, out_xlsx))
