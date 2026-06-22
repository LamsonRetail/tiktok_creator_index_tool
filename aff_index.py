"""
Aff Indexs module — kéo số liệu video affiliate TikTok về Lark Base.

Pipeline mỗi KOL:
    Link aff  → parse username/video_id/shop_id/shop_region
             → resolve creator_id từ trang public (nếu trống)
             → mở affiliate.tiktok.com/data/creator-analysis
             → CDP hook bắt response creator_analytics/creator/video/list
             → trích GMV / hoa hồng / CTR / like / comment / sold / view
             → upsert vào bảng "Số liệu Video" theo video_id (overwrite + cập nhật "Ngày cập nhật")
             → backfill các cột thiếu vào "Booking KOL"
"""
import os
import re
import json
import time
import subprocess
import tempfile
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from driver_manager import init_driver, get_login_profile_dir, get_user_dir  # noqa: F401


# =============================================================================
# SHOP CONFIG — shop_id của user (cố định), dùng khi link KOL không có shop_id
# =============================================================================
def _shop_config_path(admin_key: str = "admin_shared_session") -> str:
    return os.path.join(get_user_dir(admin_key), "aff_shop_config.json")


def load_shop_config(admin_key: str = "admin_shared_session") -> Dict[str, str]:
    p = _shop_config_path(admin_key)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
            return {
                "shop_id": str(d.get("shop_id") or "").strip(),
                "shop_region": (str(d.get("shop_region") or "VN").strip() or "VN"),
            }
    except Exception:
        return {"shop_id": "", "shop_region": "VN"}


def save_shop_config(shop_id: str, shop_region: str = "VN",
                     admin_key: str = "admin_shared_session") -> None:
    p = _shop_config_path(admin_key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"shop_id": str(shop_id or "").strip(),
                   "shop_region": (shop_region or "VN").strip() or "VN"}, f)

# =============================================================================
# CDP CAPTURE HOOK — chỉ bắt endpoint số liệu video aff
# =============================================================================
AFF_CAPTURE_HOOK_JS = r"""
(function(){
  if (window.__tt_aff_cap_installed) return true;
  window.__tt_aff_cap_installed = true;
  window.__tt_aff_api_capture = [];

  function tryParse(t){ try { return JSON.parse(t); } catch(e){ return null; } }
  function shouldCapture(url){
    return url
      && url.indexOf('creator_analytics/creator/video/list') !== -1
      && url.indexOf('search/list') === -1;
  }
  function record(url, method, postData, text){
    var j = tryParse(text);
    try { window.__tt_aff_api_capture.push({ url: url, method: method, body: j }); } catch(e) {}
  }

  var origFetch = window.fetch;
  if (origFetch){
    window.fetch = function(){
      var args = arguments, url = args[0];
      try { url = (typeof url === 'string') ? url : (url && url.url) ? url.url : String(url); } catch(e){}
      var method = 'GET';
      try { if (args[1] && args[1].method) method = args[1].method; } catch(e){}
      return origFetch.apply(this, args).then(function(resp){
        try {
          if (shouldCapture(String(url))){
            resp.clone().text().then(function(t){ record(String(url), method, null, t); }).catch(function(){});
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
          try { if (shouldCapture(String(u))) record(String(u), self.__m || 'GET', null, self.responseText); } catch(e){}
        });
      } catch(e){}
      return oSend.apply(this, arguments);
    };
  }
  return true;
})();
"""


def install_aff_capture_hook(driver) -> bool:
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": AFF_CAPTURE_HOOK_JS}
        )
        return True
    except Exception:
        return False


def _clear_aff_capture(driver):
    try:
        driver.execute_script("if(window.__tt_aff_api_capture) window.__tt_aff_api_capture.length=0;")
    except Exception:
        pass


# =============================================================================
# CREATOR HARVEST HOOK — passive scan mọi response từ affiliate.tiktok.com
# tìm cặp (creator_id, unique_id/username/nickname) và tự nhồi cache.
# =============================================================================
AFF_CREATOR_HARVEST_JS = r"""
(function(){
  if (window.__tt_cid_harvest_installed) return true;
  window.__tt_cid_harvest_installed = true;
  window.__tt_cid_collect = {};  // {username_lower: creator_id}

  function tryParse(t){ try { return JSON.parse(t); } catch(e){ return null; } }

  function pickCid(o){
    return o.creator_id || o.creatorId || o.author_id || o.authorId
        || (o.user && (o.user.id || o.user.user_id)) || null;
  }
  function pickUname(o){
    return o.unique_id || o.uniqueId || o.username || o.uniqueName
        || (o.user && (o.user.unique_id || o.user.uniqueId || o.user.username))
        || null;
  }

  function walk(obj, depth){
    if (!obj || depth > 10) return;
    if (Array.isArray(obj)){
      for (var i=0; i<obj.length; i++) walk(obj[i], depth+1);
      return;
    }
    if (typeof obj === 'object'){
      try {
        var cid = pickCid(obj);
        var uname = pickUname(obj);
        if (cid && uname){
          var k = String(uname).toLowerCase().replace(/^@/, '');
          var v = String(cid);
          if (/^\d{6,}$/.test(v)) window.__tt_cid_collect[k] = v;
        }
      } catch(e){}
      for (var k in obj){
        try { walk(obj[k], depth+1); } catch(e){}
      }
    }
  }

  function shouldCapture(url){
    if (!url) return false;
    url = String(url);
    return url.indexOf('affiliate.tiktok.com') !== -1
        || url.indexOf('/aff_api/') !== -1
        || url.indexOf('/aff_creator_api/') !== -1
        || url.indexOf('/aff_oec_api/') !== -1;
  }

  var origFetch = window.fetch;
  if (origFetch){
    window.fetch = function(){
      var args = arguments, url = args[0];
      try { url = (typeof url === 'string') ? url : (url && url.url) ? url.url : String(url); } catch(e){}
      return origFetch.apply(this, args).then(function(resp){
        try {
          if (shouldCapture(url)){
            resp.clone().text().then(function(t){
              var j = tryParse(t);
              if (j) walk(j, 0);
            }).catch(function(){});
          }
        } catch(e){}
        return resp;
      });
    };
  }

  var XHR = window.XMLHttpRequest;
  if (XHR){
    var oOpen = XHR.prototype.open, oSend = XHR.prototype.send;
    XHR.prototype.open = function(m, u){ try { this.__u = u; } catch(e){} return oOpen.apply(this, arguments); };
    XHR.prototype.send = function(b){
      try {
        var self = this;
        self.addEventListener('load', function(){
          try {
            if (shouldCapture(self.__u)){
              var j = tryParse(self.responseText);
              if (j) walk(j, 0);
            }
          } catch(e){}
        });
      } catch(e){}
      return oSend.apply(this, arguments);
    };
  }
  return true;
})();
"""


def install_creator_harvest_hook(driver) -> bool:
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": AFF_CREATOR_HARVEST_JS},
        )
        return True
    except Exception:
        return False


def drain_creator_harvest(driver, admin_key: str = "admin_shared_session") -> int:
    """Đọc bảng harvest từ trang hiện tại, gộp vào cache. Trả về số entry mới."""
    try:
        collected = driver.execute_script("return window.__tt_cid_collect || {};") or {}
    except Exception:
        collected = {}
    if not collected:
        return 0
    cache = load_creator_cache(admin_key)
    added = 0
    for k, v in collected.items():
        k = (k or "").strip().lower().lstrip("@")
        v = str(v or "").strip()
        if k and v and v.isdigit() and cache.get(k) != v:
            cache[k] = v
            added += 1
    if added:
        save_creator_cache(cache, admin_key)
    return added


def harvest_affiliate_creators(
    driver,
    shop_region: str = "VN",
    wait_seconds: float = 12.0,
    extra_urls: Optional[List[str]] = None,
    admin_key: str = "admin_shared_session",
) -> int:
    """Mở trang list KOL collab của shop trên affiliate.tiktok.com, để hook quét
    rồi gộp tất cả (username → creator_id) vào cache.
    Có thể truyền extra_urls để mở thêm trang khác (vd /connection/recommended)."""
    install_creator_harvest_hook(driver)
    urls = [
        f"https://affiliate.tiktok.com/connection/creator?shop_region={shop_region}",
    ]
    if extra_urls:
        urls.extend(extra_urls)
    total_added = 0
    for u in urls:
        try:
            driver.get(u)
        except Exception:
            continue
        # cuộn trang để load thêm chunk nếu có infinite scroll
        end = time.time() + wait_seconds
        last_count = -1
        while time.time() < end:
            time.sleep(1.0)
            try:
                driver.execute_script("window.scrollBy(0, document.body.scrollHeight);")
            except Exception:
                pass
            try:
                cur = driver.execute_script(
                    "return Object.keys(window.__tt_cid_collect||{}).length;"
                )
            except Exception:
                cur = 0
            if cur == last_count and cur > 0:
                # đã ổn định
                break
            last_count = cur
        total_added += drain_creator_harvest(driver, admin_key)
    return total_added


# =============================================================================
# LINK PARSER
# =============================================================================
_LINK_RE = re.compile(r"@([A-Za-z0-9_.]+)/video/(\d+)")


def parse_aff_link(url: str) -> Dict[str, Optional[str]]:
    """
    https://www.tiktok.com/@minhquanmacdep/video/7651181115302055175?shop_id=...&shop_region=VN
        -> {username, video_id, shop_id, shop_region}
    """
    out = {"username": None, "video_id": None, "shop_id": None, "shop_region": None}
    if not url:
        return out
    m = _LINK_RE.search(url)
    if m:
        out["username"] = m.group(1)
        out["video_id"] = m.group(2)
    try:
        q = parse_qs(urlparse(url).query)
        out["shop_id"] = (q.get("shop_id") or [None])[0]
        out["shop_region"] = (q.get("shop_region") or [None])[0]
    except Exception:
        pass
    return out


# =============================================================================
# CREATOR ID CACHE — username → creator_id (persistent, tránh resolve lại)
# =============================================================================
def _creator_cache_path(admin_key: str = "admin_shared_session") -> str:
    return os.path.join(get_user_dir(admin_key), "aff_creator_cache.json")


def load_creator_cache(admin_key: str = "admin_shared_session") -> Dict[str, str]:
    try:
        with open(_creator_cache_path(admin_key), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_creator_cache(cache: Dict[str, str],
                       admin_key: str = "admin_shared_session") -> None:
    p = _creator_cache_path(admin_key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def cache_set_creator_id(username: str, creator_id: str,
                         admin_key: str = "admin_shared_session") -> None:
    if not (username and creator_id):
        return
    c = load_creator_cache(admin_key)
    c[username.strip().lower()] = str(creator_id)
    save_creator_cache(c, admin_key)


def cache_get_creator_id(username: str,
                         admin_key: str = "admin_shared_session") -> Optional[str]:
    if not username:
        return None
    return load_creator_cache(admin_key).get(username.strip().lower())


# =============================================================================
# RESOLVE creator_id — nhiều fallback vì public TikTok hay bật login wall
# =============================================================================
_RESOLVE_JS_FROM_VIDEO = r"""
try {
  var el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
  if (!el) return null;
  var data = JSON.parse(el.textContent);
  var scope = (data && data.__DEFAULT_SCOPE__) || {};
  var detail = scope['webapp.video-detail'];
  var id = (detail && detail.itemInfo && detail.itemInfo.itemStruct
            && detail.itemInfo.itemStruct.author
            && detail.itemInfo.itemStruct.author.id) || null;
  if (id) return id;
  // fallback: user-detail
  var ud = scope['webapp.user-detail'];
  return (ud && ud.userInfo && ud.userInfo.user && ud.userInfo.user.id) || null;
} catch(e){ return null; }
"""

_RESOLVE_JS_FROM_PROFILE = r"""
try {
  var el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
  if (!el) return null;
  var data = JSON.parse(el.textContent);
  var scope = (data && data.__DEFAULT_SCOPE__) || {};
  var ud = scope['webapp.user-detail'];
  return (ud && ud.userInfo && ud.userInfo.user && ud.userInfo.user.id) || null;
} catch(e){ return null; }
"""

_RESOLVE_JS_FROM_EMBED = r"""
try {
  // embed bundle hay nhúng JSON trong <script id="SIGI_STATE"> hoặc inline
  var scripts = document.querySelectorAll('script');
  for (var i=0; i<scripts.length; i++){
    var t = scripts[i].textContent || '';
    var m = t.match(/"authorId"\s*:\s*"(\d+)"/);
    if (m) return m[1];
    m = t.match(/"author"\s*:\s*\{[^}]*"id"\s*:\s*"(\d+)"/);
    if (m) return m[1];
  }
  return null;
} catch(e){ return null; }
"""


def _try_resolve(driver, url: str, js: str, wait: float = 2.0) -> Optional[str]:
    try:
        driver.get(url)
        time.sleep(wait)
        cid = driver.execute_script(js)
        return str(cid) if cid else None
    except Exception:
        return None


def resolve_creator_id(driver, username: str, video_id: str,
                       admin_key: str = "admin_shared_session") -> Optional[str]:
    """Tìm creator_id theo thứ tự: cache → profile page → video page → embed page.
    Lưu cache sau khi resolve thành công."""
    if not username:
        return None

    # 1) cache hit
    cached = cache_get_creator_id(username, admin_key)
    if cached:
        return cached

    # 2) profile page — ít bị chặn modal hơn video page
    cid = _try_resolve(
        driver, f"https://www.tiktok.com/@{username}",
        _RESOLVE_JS_FROM_PROFILE, wait=2.5,
    )
    if cid:
        cache_set_creator_id(username, cid, admin_key)
        return cid

    # 3) video page (cách cũ)
    if video_id:
        cid = _try_resolve(
            driver, f"https://www.tiktok.com/@{username}/video/{video_id}",
            _RESOLVE_JS_FROM_VIDEO, wait=2.5,
        )
        if cid:
            cache_set_creator_id(username, cid, admin_key)
            return cid

        # 4) embed page — bypass login wall vì là iframe public
        cid = _try_resolve(
            driver, f"https://www.tiktok.com/embed/v2/{video_id}",
            _RESOLVE_JS_FROM_EMBED, wait=2.0,
        )
        if cid:
            cache_set_creator_id(username, cid, admin_key)
            return cid

    return None


# =============================================================================
# FETCH METRICS qua creator-analysis page + CDP capture
# =============================================================================
def fetch_video_metrics(
    driver,
    creator_id: str,
    shop_id: str,
    shop_region: str,
    video_id: str,
    timeout: float = 30.0,
) -> Optional[Dict[str, Any]]:
    _clear_aff_capture(driver)
    url = (
        f"https://affiliate.tiktok.com/data/creator-analysis"
        f"?creator_id={creator_id}&shop_region={shop_region}&shop_id={shop_id}"
    )
    driver.get(url)

    end = time.time() + timeout
    while time.time() < end:
        caps = driver.execute_script("return window.__tt_aff_api_capture || [];") or []
        for c in caps:
            body = c.get("body") or {}
            try:
                stats = body["data"]["segments"][0]["timed_lists"][0].get("stats") or []
            except Exception:
                stats = []
            for item in stats:
                meta = (item or {}).get("video_meta") or {}
                if str(meta.get("item_id") or "") == str(video_id):
                    return extract_metrics(item)
        time.sleep(0.3)
    return None


def _amount(o):
    if isinstance(o, dict):
        v = o.get("amount")
        try:
            return float(v) if v is not None and v != "" else None
        except Exception:
            return None
    return None


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


def extract_metrics(stat: dict) -> Dict[str, Any]:
    ctr_raw = _num(stat.get("product_ctr"))
    return {
        "Tên video": (stat.get("video_meta") or {}).get("name") or "",
        "video_id": (stat.get("video_meta") or {}).get("item_id") or "",
        "GMV video": _amount(stat.get("gmv")),
        "Hoa hồng ước tính": _amount(stat.get("est_commission")),
        "CTR (%)": round(ctr_raw * 100, 4) if ctr_raw is not None else None,
        "Lượt thích": _num(stat.get("video_like_cnt")),
        "Bình luận": _num(stat.get("video_comment_cnt")),
        "Số món bán ra": _num(stat.get("item_sold_cnt")),
        "Lượt xem": _num(stat.get("video_view_cnt")),
    }


# =============================================================================
# LARK CLI WRAPPER
# =============================================================================
def _run_lark(args: List[str], cwd: Optional[str] = None) -> dict:
    res = subprocess.run(
        ["lark-cli"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=True,
        cwd=cwd,
    )
    out = (res.stdout or "").strip()
    i = out.find("{")
    if i < 0:
        raise RuntimeError(f"lark-cli no JSON output. stderr={res.stderr[:500]}")
    try:
        j = json.loads(out[i:])
    except Exception as e:
        raise RuntimeError(f"lark-cli JSON parse error: {e}; output[:400]={out[:400]}")
    if j.get("ok") is False:
        raise RuntimeError(f"lark-cli error: {j.get('error')}")
    return j


def _records_to_rows(json_data: dict) -> List[dict]:
    d = json_data["data"]
    fields = d.get("fields") or []
    rows = d.get("data") or []
    ids = d.get("record_id_list") or []
    out = []
    for i, row in enumerate(rows):
        by_name = {fields[j]: row[j] for j in range(len(fields))}
        out.append({"record_id": ids[i] if i < len(ids) else None, "fields": by_name})
    return out


def lark_list_bookings(cfg: dict) -> List[dict]:
    j = _run_lark([
        "base", "+record-list",
        "--base-token", cfg["base_token"],
        "--table-id", cfg["booking_table_id"],
        "--as", cfg.get("identity", "user"),
        "--format", "json",
    ])
    return _records_to_rows(j)


def lark_list_metric_records(cfg: dict) -> List[dict]:
    j = _run_lark([
        "base", "+record-list",
        "--base-token", cfg["base_token"],
        "--table-id", cfg["metric_table_id"],
        "--as", cfg.get("identity", "user"),
        "--format", "json",
    ])
    return _records_to_rows(j)


def _find_metric_rid_by_video_id(metrics_cache: List[dict], video_id: str) -> Optional[str]:
    for r in metrics_cache:
        if str(r["fields"].get("video_id") or "") == str(video_id):
            return r["record_id"]
    return None


def _write_json_tmp(payload: dict) -> str:
    """Lark CLI yêu cầu --json @<relative-path>. Ghi vào ./.tmp/ rồi trả về relative path."""
    proj_dir = os.path.dirname(os.path.abspath(__file__))
    tmp_dir = os.path.join(proj_dir, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    fname = f"aff_{int(time.time()*1000)}_{os.getpid()}.json"
    fpath = os.path.join(tmp_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return f"./.tmp/{fname}", fpath, proj_dir


def lark_upsert_metric(
    cfg: dict,
    booking_record_id: Optional[str],
    values: Dict[str, Any],
    metrics_cache: Optional[List[dict]] = None,
) -> str:
    """Upsert 1 dòng/video theo video_id. Trả về 'created' hoặc 'updated'."""
    video_id = str(values.get("video_id") or "")
    if not video_id:
        raise RuntimeError("missing video_id for upsert")

    if metrics_cache is None:
        metrics_cache = lark_list_metric_records(cfg)
    existing_rid = _find_metric_rid_by_video_id(metrics_cache, video_id)

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    full = {
        "Tên video": values.get("Tên video") or video_id,
        "video_id": video_id,
        "Booking": [{"id": booking_record_id}] if booking_record_id else None,
        "Ngày cập nhật": now,
        "GMV video": values.get("GMV video"),
        "Hoa hồng ước tính": values.get("Hoa hồng ước tính"),
        "CTR (%)": values.get("CTR (%)"),
        "Lượt thích": values.get("Lượt thích"),
        "Bình luận": values.get("Bình luận"),
        "Số món bán ra": values.get("Số món bán ra"),
        "Lượt xem": values.get("Lượt xem"),
    }
    full = {k: v for k, v in full.items() if v is not None}

    if existing_rid:
        rel, abs_path, cwd = _write_json_tmp({
            "records": [{"record_id": existing_rid, "fields": full}]
        })
        try:
            _run_lark([
                "base", "+record-batch-update",
                "--base-token", cfg["base_token"],
                "--table-id", cfg["metric_table_id"],
                "--json", f"@{rel}",
                "--as", cfg.get("identity", "user"),
            ], cwd=cwd)
        finally:
            try: os.unlink(abs_path)
            except Exception: pass
        return "updated"
    else:
        cols = list(full.keys())
        rows = [[full[c] for c in cols]]
        rel, abs_path, cwd = _write_json_tmp({"fields": cols, "rows": rows})
        try:
            _run_lark([
                "base", "+record-batch-create",
                "--base-token", cfg["base_token"],
                "--table-id", cfg["metric_table_id"],
                "--json", f"@{rel}",
                "--as", cfg.get("identity", "user"),
            ], cwd=cwd)
        finally:
            try: os.unlink(abs_path)
            except Exception: pass
        return "created"


def lark_update_booking(cfg: dict, record_id: str, fields: Dict[str, Any]):
    """Backfill các cột thiếu (username, video_id, shop_id, shop_region, creator_id) vào Booking KOL."""
    fields = {k: v for k, v in fields.items() if v not in (None, "", [])}
    if not fields:
        return
    rel, abs_path, cwd = _write_json_tmp({
        "records": [{"record_id": record_id, "fields": fields}]
    })
    try:
        _run_lark([
            "base", "+record-batch-update",
            "--base-token", cfg["base_token"],
            "--table-id", cfg["booking_table_id"],
            "--json", f"@{rel}",
            "--as", cfg.get("identity", "user"),
        ], cwd=cwd)
    finally:
        try: os.unlink(abs_path)
        except Exception: pass


# =============================================================================
# TOP-LEVEL: process_bookings — gọi từ Streamlit UI
# =============================================================================
def process_bookings(
    driver,
    cfg: dict,
    bookings: List[dict],
    progress_cb=None,
) -> List[dict]:
    """
    bookings: [{'record_id': 'rec...', 'fields': {...}}, ...]
    progress_cb(i, kol_name, message)
    """
    install_aff_capture_hook(driver)
    # Cache 1 lần snapshot bảng metric, sau upsert thì cập nhật cache để tránh list lại liên tục.
    metrics_cache = lark_list_metric_records(cfg)
    results = []

    for i, b in enumerate(bookings):
        f = b.get("fields") or {}
        kol_name = f.get("Tên KOL") or "?"
        link = (f.get("Link Aff Video") or "").strip()
        try:
            if not link:
                raise RuntimeError("thiếu Link Aff Video")

            parsed = parse_aff_link(link)
            username = f.get("Username") or parsed["username"]
            video_id = f.get("video_id") or parsed["video_id"]
            shop_id = f.get("shop_id") or parsed["shop_id"]
            shop_region = f.get("shop_region") or parsed["shop_region"] or "VN"
            creator_id = f.get("creator_id")

            if not (username and video_id and shop_id):
                raise RuntimeError("link không parse được đủ username/video_id/shop_id")

            if not creator_id:
                if progress_cb: progress_cb(i, kol_name, "resolve creator_id...")
                creator_id = resolve_creator_id(driver, username, video_id)
                if not creator_id:
                    raise RuntimeError("không lấy được creator_id từ trang public")

            # Backfill các cột thiếu vào Booking KOL (cả creator_id vừa resolve)
            backfill = {}
            if not f.get("Username"):    backfill["Username"] = username
            if not f.get("video_id"):    backfill["video_id"] = video_id
            if not f.get("shop_id"):     backfill["shop_id"] = shop_id
            if not f.get("shop_region"): backfill["shop_region"] = shop_region
            if not f.get("creator_id"):  backfill["creator_id"] = str(creator_id)
            if backfill:
                if progress_cb: progress_cb(i, kol_name, "backfill Booking KOL...")
                lark_update_booking(cfg, b["record_id"], backfill)

            if progress_cb: progress_cb(i, kol_name, "kéo số liệu...")
            metrics = fetch_video_metrics(
                driver, str(creator_id), str(shop_id), str(shop_region), str(video_id)
            )
            if not metrics:
                raise RuntimeError("không bắt được response metrics (timeout)")

            if progress_cb: progress_cb(i, kol_name, "ghi Lark Base...")
            action = lark_upsert_metric(cfg, b["record_id"], metrics, metrics_cache=metrics_cache)
            # cập nhật cache để các iteration sau không phải list lại
            if action == "created":
                metrics_cache.append({"record_id": None, "fields": {"video_id": metrics["video_id"]}})

            results.append({"kol": kol_name, "ok": True, "action": action, "metrics": metrics})
            if progress_cb:
                progress_cb(i, kol_name, f"OK ({action}) · GMV={metrics.get('GMV video')}")
        except Exception as e:
            results.append({"kol": kol_name, "ok": False, "error": str(e)})
            if progress_cb: progress_cb(i, kol_name, f"LỖI: {e}")
    return results


# =============================================================================
# BASE / TABLE / FIELD HELPERS — cho mode "chọn base bất kỳ"
# =============================================================================
_BASE_URL_RE = re.compile(r"/base/([A-Za-z0-9]+)")
_TABLE_PARAM_RE = re.compile(r"[?&]table=(tbl[A-Za-z0-9]+)")


def extract_base_token(s: str) -> str:
    """Nhận URL Base hoặc token thô, trả về base_token."""
    s = (s or "").strip()
    if not s:
        return ""
    if not s.startswith("http"):
        return s
    m = _BASE_URL_RE.search(s)
    return m.group(1) if m else s


def extract_table_id_hint(s: str) -> Optional[str]:
    """Lấy ?table=tbl... từ URL nếu có, để auto-preselect bảng."""
    if not s:
        return None
    m = _TABLE_PARAM_RE.search(s)
    return m.group(1) if m else None


def lark_base_get(token: str, identity: str = "user") -> dict:
    j = _run_lark([
        "base", "+base-get",
        "--base-token", token,
        "--as", identity,
    ])
    d = j.get("data") or {}
    return d.get("base") or d


def lark_list_tables(token: str, identity: str = "user") -> List[dict]:
    j = _run_lark([
        "base", "+table-list",
        "--base-token", token,
        "--as", identity,
    ])
    d = j.get("data") or {}
    items = d.get("tables") or d.get("items") or []
    out = []
    for t in items or []:
        tid = t.get("id") or t.get("table_id")
        name = t.get("name") or t.get("table_name")
        if tid:
            out.append({"table_id": tid, "name": name or tid})
    return out


def lark_list_fields(token: str, table_id: str, identity: str = "user") -> List[dict]:
    j = _run_lark([
        "base", "+field-list",
        "--base-token", token,
        "--table-id", table_id,
        "--as", identity,
    ])
    d = j.get("data") or {}
    items = d.get("fields") or d.get("items") or []
    out = []
    for f in items or []:
        out.append({
            "field_id": f.get("id") or f.get("field_id"),
            "name": f.get("name") or f.get("field_name"),
            "type": f.get("type") or f.get("ui_type"),
        })
    return out


def lark_list_all_records(token: str, table_id: str, identity: str = "user",
                          page_size: int = 500, max_pages: int = 20) -> List[dict]:
    """Lấy toàn bộ records, auto-paginate qua page_token."""
    all_rows: List[dict] = []
    page_token = None
    for _ in range(max_pages):
        args = [
            "base", "+record-list",
            "--base-token", token,
            "--table-id", table_id,
            "--limit", str(page_size),
            "--as", identity,
            "--format", "json",
        ]
        if page_token:
            args.extend(["--page-token", page_token])
        j = _run_lark(args)
        d = j.get("data") or {}
        all_rows.extend(_records_to_rows(j))
        page_token = d.get("page_token") or d.get("next_page_token")
        if not page_token or not d.get("has_more"):
            break
    return all_rows


# Lark field types are strings: "text", "number", "datetime", ...
METRIC_FIELD_SPECS = [
    ("GMV video", "number"),
    ("Hoa hồng ước tính", "number"),
    ("CTR (%)", "number"),
    ("Lượt thích", "number"),
    ("Bình luận", "number"),
    ("Số món bán ra", "number"),
    ("Lượt xem", "number"),
    ("Ngày cập nhật", "datetime"),
]


def lark_ensure_metric_fields(token: str, table_id: str, identity: str = "user") -> Dict[str, str]:
    """Đảm bảo bảng có 8 cột metric (tạo nếu chưa có). Trả về {name: 'existed'|'created'|'error:...'}."""
    try:
        existing = lark_list_fields(token, table_id, identity)
        existing_names = {f["name"] for f in existing}
    except Exception:
        existing_names = set()
    out: Dict[str, str] = {}
    for name, ftype in METRIC_FIELD_SPECS:
        if name in existing_names:
            out[name] = "existed"
            continue
        payload = {"name": name, "type": ftype}
        rel, abs_path, cwd = _write_json_tmp(payload)
        try:
            _run_lark([
                "base", "+field-create",
                "--base-token", token,
                "--table-id", table_id,
                "--json", f"@{rel}",
                "--as", identity,
            ], cwd=cwd)
            out[name] = "created"
        except Exception as e:
            out[name] = f"error: {str(e)[:120]}"
        finally:
            try: os.unlink(abs_path)
            except Exception: pass
    return out


def lark_update_record_fields(token: str, table_id: str, record_id: str,
                              fields: Dict[str, Any], identity: str = "user"):
    """Cập nhật 1 record với các field khác nhau — dùng +record-upsert."""
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    # record-upsert nhận --json '{field: value}' (CellValue map), không bọc trong "records"
    rel, abs_path, cwd = _write_json_tmp(fields)
    try:
        _run_lark([
            "base", "+record-upsert",
            "--base-token", token,
            "--table-id", table_id,
            "--record-id", record_id,
            "--json", f"@{rel}",
            "--as", identity,
        ], cwd=cwd)
    finally:
        try: os.unlink(abs_path)
        except Exception: pass


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_URL_RE = re.compile(r"https?://\S+")


def _strip_md_link(s: str) -> str:
    """Lark text-cell hay trả `[url](url)` (markdown). Trả URL thuần (ưu tiên URL trong ngoặc tròn)."""
    if not s:
        return ""
    s = s.strip()
    m = _MD_LINK_RE.search(s)
    if m:
        # ưu tiên URL trong dấu (), nếu không phải URL thì lấy text trong []
        target = m.group(2).strip()
        if target.startswith("http"):
            return target
        return m.group(1).strip()
    m2 = _URL_RE.search(s)
    if m2:
        return m2.group(0).rstrip(")]>,'\"")
    return s


def extract_link_value(v) -> str:
    """Trích chuỗi URL từ giá trị cell Lark — chấp nhận text, dict {link/url/text/value}, list."""
    if v is None:
        return ""
    if isinstance(v, str):
        return _strip_md_link(v)
    if isinstance(v, dict):
        for k in ("link", "url", "text", "value"):
            x = v.get(k)
            if x:
                return _strip_md_link(str(x))
        return ""
    if isinstance(v, list) and v:
        return extract_link_value(v[0])
    return _strip_md_link(str(v))


# =============================================================================
# NEW PROCESS FLOWS
# =============================================================================
def _resolve_shop_fields(parsed: dict, default_shop_id: Optional[str],
                         default_shop_region: Optional[str]) -> tuple:
    """Lấy shop_id/region từ link; nếu thiếu, dùng default từ shop config."""
    sid = (parsed.get("shop_id") or "").strip() or (default_shop_id or "").strip()
    sreg = (parsed.get("shop_region") or "").strip() or (default_shop_region or "VN").strip() or "VN"
    return sid, sreg


def fetch_metrics_for_link(driver, link: str,
                           default_shop_id: Optional[str] = None,
                           default_shop_region: Optional[str] = None) -> dict:
    """Mode 'dán link': parse → resolve creator_id → fetch metrics, không ghi Lark.
    Nếu link KOL gửi không có shop_id (vd dán trực tiếp từ trang TikTok cá nhân),
    sẽ dùng default_shop_id (đã cấu hình ở Admin Settings)."""
    parsed = parse_aff_link(link)
    if not (parsed["username"] and parsed["video_id"]):
        return {"link": link, "ok": False, "error": "link không parse được username/video_id"}
    shop_id, shop_region = _resolve_shop_fields(parsed, default_shop_id, default_shop_region)
    if not shop_id:
        return {"link": link, "ok": False,
                "error": "thiếu shop_id (link không có, và Shop ID mặc định ở Admin Settings cũng trống)"}
    cid = resolve_creator_id(driver, parsed["username"], parsed["video_id"])
    if not cid:
        return {"link": link, "ok": False,
                "error": "không lấy được creator_id (TikTok bật login wall). "
                         "Vào ADMIN SETTINGS → CREATOR ID CACHE để nhập tay username→creator_id."}
    metrics = fetch_video_metrics(driver, cid, shop_id, shop_region, parsed["video_id"])
    if not metrics:
        return {"link": link, "ok": False, "error": "timeout khi bắt response metrics"}
    out = {"link": link, "ok": True, "creator_id": cid}
    out.update(parsed)
    out["shop_id"] = shop_id
    out["shop_region"] = shop_region
    for k, v in metrics.items():
        out[k] = v
    return out


def process_links_paste(driver, links: List[str], progress_cb=None,
                        default_shop_id: Optional[str] = None,
                        default_shop_region: Optional[str] = None,
                        do_harvest: bool = True,
                        admin_key: str = "admin_shared_session") -> List[dict]:
    install_aff_capture_hook(driver)
    install_creator_harvest_hook(driver)
    if do_harvest:
        if progress_cb: progress_cb(-1, "harvest",
            "Mở affiliate.tiktok.com/connection/creator để nhồi cache creator_id...")
        try:
            added = harvest_affiliate_creators(
                driver, shop_region=(default_shop_region or "VN"),
                admin_key=admin_key,
            )
            if progress_cb: progress_cb(-1, "harvest", f"Đã nhồi {added} creator_id vào cache.")
        except Exception as e:
            if progress_cb: progress_cb(-1, "harvest", f"Harvest lỗi (bỏ qua): {e}")
    out = []
    for i, link in enumerate(links):
        label = (link[:50] + "...") if len(link) > 53 else link
        try:
            if progress_cb: progress_cb(i, label, "xử lý...")
            r = fetch_metrics_for_link(driver, link, default_shop_id, default_shop_region)
            if progress_cb:
                msg = f"OK · GMV={r.get('GMV video')}" if r.get("ok") else f"LỖI: {r.get('error')}"
                progress_cb(i, label, msg)
        except Exception as e:
            r = {"link": link, "ok": False, "error": str(e)}
            if progress_cb: progress_cb(i, label, f"LỖI: {e}")
        out.append(r)
    return out


def process_records_in_table(
    driver,
    base_cfg: dict,                 # {'base_token', 'table_id', 'identity'}
    records: List[dict],
    link_field_name: str,
    progress_cb=None,
    default_shop_id: Optional[str] = None,
    default_shop_region: Optional[str] = None,
    creator_id_field_name: Optional[str] = None,   # nếu bảng đã có cột creator_id
    do_harvest: bool = True,
    admin_key: str = "admin_shared_session",
) -> List[dict]:
    """Ghi metrics vào CHÍNH dòng record của bảng người dùng đã chọn.
    Nếu link không có shop_id (KOL gửi link gốc TikTok), sẽ dùng default_shop_id.
    Nếu record đã có sẵn cột creator_id (do người dùng điền tay), ưu tiên dùng — bỏ qua resolve.
    do_harvest=True: trước khi xử lý, mở trang list KOL collab trên affiliate.tiktok.com để
    auto-nhồi cache (username→creator_id), không cần đụng tiktok.com public."""
    install_aff_capture_hook(driver)
    install_creator_harvest_hook(driver)
    if do_harvest:
        if progress_cb: progress_cb(-1, "harvest",
            "Mở affiliate.tiktok.com/connection/creator để nhồi cache creator_id...")
        try:
            added = harvest_affiliate_creators(
                driver, shop_region=(default_shop_region or "VN"),
                admin_key=admin_key,
            )
            if progress_cb: progress_cb(-1, "harvest", f"Đã nhồi {added} creator_id vào cache.")
        except Exception as e:
            if progress_cb: progress_cb(-1, "harvest", f"Harvest lỗi (bỏ qua): {e}")
    results = []
    for i, r in enumerate(records):
        f = r.get("fields") or {}
        link = extract_link_value(f.get(link_field_name))
        label = link[-40:] if link else (r.get("record_id") or "?")
        try:
            if not link:
                raise RuntimeError(f"cột '{link_field_name}' trống")
            parsed = parse_aff_link(link)
            if not (parsed["username"] and parsed["video_id"]):
                raise RuntimeError("link không parse được username/video_id")
            shop_id, shop_region = _resolve_shop_fields(parsed, default_shop_id, default_shop_region)
            if not shop_id:
                raise RuntimeError("thiếu shop_id (link không có và Shop ID mặc định trống)")

            cid = None
            # 1) ưu tiên creator_id đã điền trong bảng (nếu user chỉ định)
            if creator_id_field_name:
                cid_raw = f.get(creator_id_field_name)
                if isinstance(cid_raw, (dict, list)):
                    cid_raw = extract_link_value(cid_raw)
                cid_raw = (str(cid_raw or "")).strip()
                if cid_raw and cid_raw.isdigit():
                    cid = cid_raw
                    # nhân tiện cache lại để Mode B dùng được
                    cache_set_creator_id(parsed["username"], cid)
            if not cid:
                if progress_cb: progress_cb(i, label, "resolve creator_id...")
                cid = resolve_creator_id(driver, parsed["username"], parsed["video_id"])
            if not cid:
                raise RuntimeError(
                    "không lấy được creator_id (TikTok bật login wall). "
                    "Thử: vào ADMIN SETTINGS → CREATOR ID CACHE để nhập tay username→creator_id, "
                    "hoặc thêm cột 'creator_id' trong bảng và điền sẵn."
                )
            if progress_cb: progress_cb(i, label, "kéo số liệu...")
            metrics = fetch_video_metrics(driver, cid, shop_id, shop_region, parsed["video_id"])
            # nhân tiện drain harvest từ trang creator-analysis (response chứa video_meta.author)
            try: drain_creator_harvest(driver, admin_key)
            except Exception: pass
            if not metrics:
                raise RuntimeError("timeout bắt response metrics")
            ts_ms = int(time.time() * 1000)
            payload = {
                "GMV video": metrics.get("GMV video"),
                "Hoa hồng ước tính": metrics.get("Hoa hồng ước tính"),
                "CTR (%)": metrics.get("CTR (%)"),
                "Lượt thích": metrics.get("Lượt thích"),
                "Bình luận": metrics.get("Bình luận"),
                "Số món bán ra": metrics.get("Số món bán ra"),
                "Lượt xem": metrics.get("Lượt xem"),
                "Ngày cập nhật": ts_ms,
            }
            if progress_cb: progress_cb(i, label, "ghi Lark Base...")
            lark_update_record_fields(
                base_cfg["base_token"], base_cfg["table_id"], r["record_id"],
                payload, identity=base_cfg.get("identity", "user"),
            )
            results.append({"label": label, "ok": True, "metrics": metrics})
            if progress_cb: progress_cb(i, label, f"OK · GMV={metrics.get('GMV video')}")
        except Exception as e:
            results.append({"label": label, "ok": False, "error": str(e)})
            if progress_cb: progress_cb(i, label, f"LỖI: {e}")
    return results
