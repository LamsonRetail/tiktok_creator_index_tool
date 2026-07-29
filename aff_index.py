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

import requests

from driver_manager import init_driver, get_login_profile_dir, get_user_dir  # noqa: F401


# =============================================================================
# LARK OPEN API — gọi trực tiếp bằng app_id/app_secret (thay lark-cli)
# =============================================================================
# Base URL user gửi có domain `larksuite.com` (Lark SG / international)
# → dùng open.larksuite.com. Nếu sau này dùng feishu.cn thì đổi host qua config.
_LARK_HOST_DEFAULT = "https://open.larksuite.com"
_LARK_APP_ID_DEFAULT = "cli_aab8fe2acfb9deea"
_LARK_APP_SECRET_DEFAULT = "v2PuAV6ksbMKK8HKcsC0EbTXud2nrfkh"


def _lark_app_config_path(admin_key: str = "admin_shared_session") -> str:
    return os.path.join(get_user_dir(admin_key), "lark_app.json")


def load_lark_app_config(admin_key: str = "admin_shared_session") -> Dict[str, str]:
    p = _lark_app_config_path(admin_key)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        aid = str(d.get("app_id") or "").strip()
        sec = str(d.get("app_secret") or "").strip()
        host = str(d.get("host") or "").strip() or _LARK_HOST_DEFAULT
        if aid and sec:
            return {"app_id": aid, "app_secret": sec, "host": host}
    except Exception:
        pass
    return {"app_id": _LARK_APP_ID_DEFAULT, "app_secret": _LARK_APP_SECRET_DEFAULT,
            "host": _LARK_HOST_DEFAULT}


def save_lark_app_config(app_id: str, app_secret: str,
                         host: str = _LARK_HOST_DEFAULT,
                         admin_key: str = "admin_shared_session") -> None:
    p = _lark_app_config_path(admin_key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({
            "app_id": (app_id or "").strip(),
            "app_secret": (app_secret or "").strip(),
            "host": (host or _LARK_HOST_DEFAULT).strip(),
        }, f, ensure_ascii=False)


_LARK_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}


def _lark_tenant_token(admin_key: str = "admin_shared_session") -> str:
    """Lấy tenant_access_token, cache theo app_id với TTL của server (thường 2h)."""
    cfg = load_lark_app_config(admin_key)
    key = f"{cfg['host']}|{cfg['app_id']}"
    now = time.time()
    ent = _LARK_TOKEN_CACHE.get(key)
    if ent and ent.get("expire_at", 0) - 60 > now:
        return ent["token"]
    url = f"{cfg['host']}/open-apis/auth/v3/tenant_access_token/internal"
    r = requests.post(url, json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]},
                      timeout=15)
    try:
        d = r.json()
    except Exception:
        raise RuntimeError(f"tenant_access_token HTTP {r.status_code}: {r.text[:200]}")
    if d.get("code") not in (0, None) or not d.get("tenant_access_token"):
        raise RuntimeError(f"tenant_access_token error: {d}")
    tok = d["tenant_access_token"]
    _LARK_TOKEN_CACHE[key] = {"token": tok, "expire_at": now + int(d.get("expire", 7200))}
    return tok


def _lark_api(method: str, path: str, *, params: dict = None, body: Any = None,
              admin_key: str = "admin_shared_session", timeout: float = 30.0,
              retries: int = 1) -> dict:
    """HTTP wrapper cho Lark Open API. Trả về `data` field (đã tách khỏi code/msg)."""
    cfg = load_lark_app_config(admin_key)
    last_err = None
    for attempt in range(retries + 1):
        tok = _lark_tenant_token(admin_key)
        url = f"{cfg['host']}{path}"
        headers = {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            r = requests.request(method, url, headers=headers,
                                 params=params or None,
                                 json=body if body is not None else None,
                                 timeout=timeout)
        except Exception as e:
            last_err = e
            time.sleep(0.6)
            continue
        try:
            d = r.json()
        except Exception:
            raise RuntimeError(f"{method} {path} → HTTP {r.status_code}, body: {r.text[:300]}")
        code = d.get("code")
        # 99991663/99991661 = token invalid/expired → clear cache & retry lần nữa
        if code in (99991663, 99991661, 99991664) and attempt < retries:
            _LARK_TOKEN_CACHE.pop(f"{cfg['host']}|{cfg['app_id']}", None)
            continue
        if code not in (0, None):
            raise RuntimeError(f"{method} {path} → code={code} msg={d.get('msg')}")
        return d.get("data") or {}
    if last_err:
        raise last_err
    raise RuntimeError(f"{method} {path} failed after {retries+1} attempts")


# Field type strings → Lark Bitable numeric type
_LARK_FIELD_TYPE_MAP = {
    "text": 1, "number": 2, "single_select": 3, "multi_select": 4,
    "datetime": 5, "checkbox": 7, "user": 11, "phone": 13, "url": 15,
    "attachment": 17, "link": 18, "formula": 20, "lookup": 21,
}


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
  // Mặc định mở rộng cửa sổ ngày cho request creator/live/list & creator/video/list.
  // TikTok Affiliate portal mặc định 7 ngày → tự nới lên 90 ngày để cover đủ.
  window.__tt_aff_days = 90;

  function tryParse(t){ try { return JSON.parse(t); } catch(e){ return null; } }

  function shouldCapture(url){
    if (!url) return false;
    var s = String(url);
    if (s.indexOf('search/list') !== -1) return false;
    return s.indexOf('creator_analytics/creator/video/list') !== -1
        || s.indexOf('creator_analytics/creator/live/list') !== -1;
  }
  function record(url, method, postData, text){
    var j = tryParse(text);
    try { window.__tt_aff_api_capture.push({ url: url, method: method, postData: postData, body: j }); } catch(e) {}
  }

  // ---- Rewrite date-range để nới cửa sổ query lên N ngày ----
  var DATE_START_KEYS = ['start_date','start_ts','startTime','start_time','date_from','begin_date','beginTime','begin_ts','from_date','fromDate','st'];
  var DATE_END_KEYS   = ['end_date','end_ts','endTime','end_time','date_to','finish_date','endDate','to_date','toDate','et'];

  function pad2(n){ n = String(n); return n.length<2 ? '0'+n : n; }
  function fmtByPattern(orig, targetDate){
    var s = String(orig || '');
    // Số nguyên: đoán đơn vị theo độ lớn
    if (/^\d+$/.test(s)){
      var v = Number(s);
      if (v > 1e12) return String(targetDate.getTime());               // ms
      if (v > 1e9)  return String(Math.floor(targetDate.getTime()/1000)); // s
      if (v >= 20000000 && v <= 99999999)                                  // YYYYMMDD
        return '' + targetDate.getFullYear() + pad2(targetDate.getMonth()+1) + pad2(targetDate.getDate());
      // fallback: ms epoch
      return String(targetDate.getTime());
    }
    // YYYY-MM-DD hoặc YYYY/MM/DD ...
    if (/^\d{4}[-\/]\d{2}[-\/]\d{2}/.test(s)){
      var sep = s.indexOf('-') !== -1 ? '-' : '/';
      var head = targetDate.getFullYear() + sep + pad2(targetDate.getMonth()+1) + sep + pad2(targetDate.getDate());
      if (s.length > 10) return head + s.substring(10);
      return head;
    }
    // Chuỗi ISO: 2026-07-07T00:00:00Z
    if (/T/.test(s) && /^\d{4}-\d{2}-\d{2}T/.test(s)){
      return targetDate.toISOString().replace(/\.\d+Z$/, 'Z');
    }
    return s; // không rõ format → giữ nguyên
  }

  function widenDates(startVal, endVal){
    var days = Number(window.__tt_aff_days || 90);
    var end = new Date();
    var start = new Date(end.getTime() - days*86400*1000);
    return {
      start: fmtByPattern(startVal, start),
      end:   fmtByPattern(endVal,   end),
    };
  }

  function rewriteQuery(qs){
    // qs = 'a=1&b=2'  → trả về qs mới nếu có sửa; null nếu không đổi
    if (!qs) return null;
    var parts = qs.split('&');
    var idxS = -1, idxE = -1, oldS = null, oldE = null;
    for (var i=0; i<parts.length; i++){
      var kv = parts[i].split('=');
      var k = decodeURIComponent(kv[0]||''); var v = decodeURIComponent(kv[1]||'');
      if (DATE_START_KEYS.indexOf(k) !== -1){ idxS = i; oldS = v; }
      if (DATE_END_KEYS.indexOf(k)   !== -1){ idxE = i; oldE = v; }
    }
    if (idxS === -1 && idxE === -1) return null;
    var wd = widenDates(oldS, oldE);
    if (idxS !== -1){
      var kv2 = parts[idxS].split('=');
      parts[idxS] = kv2[0] + '=' + encodeURIComponent(wd.start);
    }
    if (idxE !== -1){
      var kv3 = parts[idxE].split('=');
      parts[idxE] = kv3[0] + '=' + encodeURIComponent(wd.end);
    }
    return parts.join('&');
  }

  function rewriteUrl(url){
    if (!shouldCapture(url)) return url;
    var i = url.indexOf('?');
    if (i === -1) return url;
    var qs = url.substring(i+1);
    var q2 = rewriteQuery(qs);
    if (!q2) return url;
    return url.substring(0, i+1) + q2;
  }

  function rewriteBody(bodyStr){
    if (bodyStr == null) return bodyStr;
    // JSON body?
    var j = tryParse(bodyStr);
    if (j && typeof j === 'object'){
      var touched = false;
      function walk(o){
        if (!o || typeof o !== 'object') return;
        var startK = null, endK = null;
        for (var k in o){
          if (DATE_START_KEYS.indexOf(k) !== -1) startK = k;
          if (DATE_END_KEYS.indexOf(k)   !== -1) endK   = k;
        }
        if (startK || endK){
          var wd = widenDates(startK ? o[startK] : null, endK ? o[endK] : null);
          if (startK){ o[startK] = (typeof o[startK] === 'number') ? Number(wd.start) : wd.start; touched = true; }
          if (endK  ){ o[endK]   = (typeof o[endK]   === 'number') ? Number(wd.end)   : wd.end;   touched = true; }
        }
        for (var k2 in o) if (o[k2] && typeof o[k2] === 'object') walk(o[k2]);
      }
      walk(j);
      if (touched){ try { return JSON.stringify(j); } catch(e){ return bodyStr; } }
      return bodyStr;
    }
    // Form-urlencoded body?
    if (/=/.test(bodyStr) && /&/.test(bodyStr)){
      var q2 = rewriteQuery(bodyStr);
      if (q2) return q2;
    }
    return bodyStr;
  }

  var origFetch = window.fetch;
  if (origFetch){
    window.fetch = function(){
      var args = Array.prototype.slice.call(arguments);
      var url = args[0];
      var urlStr = (typeof url === 'string') ? url : (url && url.url) ? url.url : String(url);
      var method = 'GET';
      try { if (args[1] && args[1].method) method = args[1].method; } catch(e){}
      var postData = null;
      try { if (args[1] && args[1].body != null) postData = (typeof args[1].body === 'string') ? args[1].body : null; } catch(e){}

      if (shouldCapture(urlStr)){
        var newUrl = rewriteUrl(urlStr);
        if (newUrl !== urlStr){
          if (typeof args[0] === 'string') args[0] = newUrl;
          else {
            try { args[0] = new Request(newUrl, args[0]); } catch(e){ args[0] = newUrl; }
          }
          urlStr = newUrl;
        }
        if (postData != null){
          var nb = rewriteBody(postData);
          if (nb !== postData){
            args[1] = Object.assign({}, args[1] || {}, { body: nb });
            postData = nb;
          }
        }
      }

      return origFetch.apply(this, args).then(function(resp){
        try {
          if (shouldCapture(String(urlStr))){
            resp.clone().text().then(function(t){ record(String(urlStr), method, postData, t); }).catch(function(){});
          }
        } catch(e){}
        return resp;
      });
    };
  }

  var XHR = window.XMLHttpRequest;
  if (XHR){
    var oOpen = XHR.prototype.open, oSend = XHR.prototype.send;
    XHR.prototype.open = function(m, u){
      try {
        this.__m = m;
        var uStr = String(u);
        if (shouldCapture(uStr)){
          var nu = rewriteUrl(uStr);
          if (nu !== uStr){ u = nu; arguments[1] = nu; }
          this.__u = String(u);
        } else {
          this.__u = uStr;
        }
      } catch(e){}
      return oOpen.apply(this, arguments);
    };
    XHR.prototype.send = function(b){
      try {
        var self = this, u = self.__u || '';
        var postData = (typeof b === 'string') ? b : null;
        if (shouldCapture(u) && postData != null){
          var nb = rewriteBody(postData);
          if (nb !== postData){ b = nb; arguments[0] = nb; postData = nb; }
        }
        self.__post = postData;
        self.addEventListener('load', function(){
          try { if (shouldCapture(String(u))) record(String(u), self.__m || 'GET', self.__post, self.responseText); } catch(e){}
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
    likes = _num(stat.get("video_like_cnt"))
    comments = _num(stat.get("video_comment_cnt"))
    views = _num(stat.get("video_view_cnt"))
    # video_view_avg / video_interact_avg: thử nhiều tên field khả dĩ trong response TikTok
    view_avg = (
        _num(stat.get("video_view_avg"))
        or _num(stat.get("avg_video_view_cnt"))
        or _num(stat.get("avg_view_cnt"))
        or _num(stat.get("avg_pv_per_video"))
    )
    interact_avg = (
        _num(stat.get("video_interact_avg"))
        or _num(stat.get("avg_video_interact_cnt"))
        or _num(stat.get("avg_interact_cnt"))
    )
    if interact_avg is None and (likes is not None or comments is not None):
        # fallback: tổng like + comment của video hiện tại (không phải "avg" thật)
        interact_avg = (likes or 0) + (comments or 0)
    return {
        "Tên video": (stat.get("video_meta") or {}).get("name") or "",
        "video_id": (stat.get("video_meta") or {}).get("item_id") or "",
        "GMV video": _amount(stat.get("gmv")),
        "Hoa hồng ước tính": _amount(stat.get("est_commission")),
        "CTR (%)": round(ctr_raw * 100, 4) if ctr_raw is not None else None,
        "Lượt thích": likes,
        "Bình luận": comments,
        "Số món bán ra": _num(stat.get("item_sold_cnt")),
        "Lượt xem": views,
        "video_view_avg": view_avg,
        "video_interact_avg": interact_avg,
    }


# =============================================================================
# LIVESTREAM INDEX — song song luồng video, endpoint `creator/live/list`.
# Booking chốt live_id (=room_id 19 chữ số). Cần username để resolve creator_id
# (dùng chung cache/harvest của luồng video).
# =============================================================================
_LIVE_ID_RE = re.compile(r"(\d{15,25})")


def parse_live_paste_line(s: str) -> Dict[str, Optional[str]]:
    """Chấp nhận các format:
      '@username live_id' | 'username live_id' | 'username:live_id'
      'username,live_id'  | 'username|live_id'
      hoặc bất kỳ chuỗi nào chứa @username + số 15-25 chữ số.
    """
    text = (s or "").strip()
    if not text:
        return {"username": None, "live_id": None}
    m_lid = _LIVE_ID_RE.search(text)
    live_id = m_lid.group(1) if m_lid else None
    uname = None
    m_u = re.search(r"@([A-Za-z0-9_.]+)", text)
    if m_u:
        uname = m_u.group(1)
    else:
        for tok in re.split(r"[\s,;:/|]+", text):
            tok = tok.strip().lstrip("@")
            if tok and tok != live_id and re.fullmatch(r"[A-Za-z0-9_.]+", tok):
                uname = tok
                break
    return {"username": uname, "live_id": live_id}


def extract_live_metrics(stat: dict) -> Dict[str, Any]:
    """Map từ 1 phần tử stats của response creator/live/list sang các cột UI.
    Field name lấy từ probe thực tế trên trang affiliate.tiktok.com."""
    ctr_raw = _num(stat.get("product_ctr"))
    enter_raw = _num(stat.get("enter_rate"))
    return {
        "Tên LIVE": stat.get("room_name") or "",
        "live_id": str(stat.get("room_id") or ""),
        "GMV LIVE": _amount(stat.get("revenue")),
        "GMV hoàn trả": _amount(stat.get("refund_gmv")),
        "Hoa hồng ước tính": _amount(stat.get("est_commission")),
        "Số món bán ra": _num(stat.get("item_sold_cnt")),
        "Số món hoàn trả": _num(stat.get("refund_item_sold")),
        "GPM": _amount(stat.get("product_gpm")),
        "CTR (%)": round(ctr_raw * 100, 4) if ctr_raw is not None else None,
        "Khách hàng liên kết TB": _num(stat.get("buyers_daily_avg")),
        "Đơn hàng": _num(stat.get("order_cnt")),
        "AOV": _amount(stat.get("aov")),
        "Live PV": _num(stat.get("live_pv")),
        "Buyers": _num(stat.get("buyers")),
        "Comments": _num(stat.get("comments")),
        "Lượt thích": _num(stat.get("likes")),
        "Direct GMV": _amount(stat.get("direct_gmv")),
        "Product view": _num(stat.get("product_view")),
        "Product clicks": _num(stat.get("product_clicks")),
        "Product view UCNT": _num(stat.get("product_view_ucnt")),
        "Enter rate (%)": round(enter_raw * 100, 4) if enter_raw is not None else None,
        "Thời lượng (s)": _num(stat.get("room_duration")),
        "Bắt đầu (ts)": _num(stat.get("room_release_timestamp")),
        "Kết thúc (ts)": _num(stat.get("room_end_timestamp")),
    }


_LIVE_TAB_CLICK_JS = r"""
(function(){
  var tabs = document.querySelectorAll('[role="tab"], .arco-tabs-tab, .tab, button, div');
  for (var i=0; i<tabs.length; i++){
    var t = (tabs[i].textContent || '').trim();
    if (t === 'LIVE' || t === 'Live'){
      try { tabs[i].click(); return true; } catch(e){}
    }
  }
  return false;
})();
"""


# Re-fire request creator/live/list với date range đã được hook widen sẵn (window.__tt_aff_days).
# Dùng khi lần fire mặc định của TikTok (7 ngày) không cover live_id target.
_LIVE_REFETCH_JS = r"""
(function(days){
  try {
    window.__tt_aff_days = days || 90;
    // tìm capture creator/live/list gần nhất để lấy URL + method + postData template
    var caps = window.__tt_aff_api_capture || [];
    var last = null;
    for (var i = caps.length - 1; i >= 0; i--){
      var u = String((caps[i] || {}).url || '');
      if (u.indexOf('creator/live/list') !== -1 && u.indexOf('search/list') === -1){
        last = caps[i]; break;
      }
    }
    if (!last) return { ok: false, reason: 'no_prior_capture' };
    var method = (last.method || 'GET').toUpperCase();
    var url = String(last.url || '');
    // clear buffer để chờ response mới
    window.__tt_aff_api_capture.length = 0;
    var opts = { method: method, credentials: 'include', headers: { 'Content-Type': 'application/json' } };
    if (method !== 'GET' && last.postData){ opts.body = last.postData; }
    // Fire — fetch hook sẽ tự nới rộng date param theo __tt_aff_days
    fetch(url, opts).catch(function(){});
    return { ok: true, url: url, method: method, days: days };
  } catch(e){ return { ok: false, reason: String(e) }; }
})(arguments[0]);
"""


def fetch_live_metrics(
    driver,
    creator_id: str,
    shop_id: str,
    shop_region: str,
    live_id: str,
    timeout: float = 30.0,
    widen_days: int = 90,
) -> Optional[Dict[str, Any]]:
    """Mở creator-analysis, click tab LIVE, chờ hook bắt response creator/live/list,
    match theo room_id == live_id.

    Nếu lần fire mặc định (7 ngày) không có live_id target, tự động re-fire cùng
    URL/method/postData nhưng nới cửa sổ ngày lên `widen_days` (mặc định 90)
    — hook JS sẽ tự viết lại date param trong URL/body.
    """
    _clear_aff_capture(driver)

    # Set trước cửa sổ ngày cho hook (áp dụng ngay cả lần fire đầu tiên của trang).
    try:
        driver.execute_script(f"window.__tt_aff_days = {int(widen_days)};")
    except Exception:
        pass

    url = (
        f"https://affiliate.tiktok.com/data/creator-analysis"
        f"?creator_id={creator_id}&shop_region={shop_region}&shop_id={shop_id}"
    )
    driver.get(url)

    # Chờ tab LIVE render + click. Retry vài lần vì DOM render bất đồng bộ.
    clicked = False
    click_end = time.time() + 8.0
    while time.time() < click_end and not clicked:
        try:
            # đảm bảo __tt_aff_days được set trên context mới (sau navigate)
            driver.execute_script(f"window.__tt_aff_days = {int(widen_days)};")
        except Exception:
            pass
        try:
            clicked = bool(driver.execute_script(_LIVE_TAB_CLICK_JS))
        except Exception:
            pass
        if not clicked:
            time.sleep(0.4)

    def _scan_captures():
        try:
            caps = driver.execute_script("return window.__tt_aff_api_capture || [];") or []
        except Exception:
            caps = []
        for c in caps:
            u = str(c.get("url") or "")
            if "creator/live/list" not in u or "search/list" in u:
                continue
            body = c.get("body") or {}
            try:
                stats = body["data"]["segments"][0]["timed_lists"][0].get("stats") or []
            except Exception:
                stats = []
            for item in stats:
                if str(item.get("room_id") or "") == str(live_id):
                    return extract_live_metrics(item)
        return None

    # Vòng 1: chờ default capture (đã bị hook widen).
    end = time.time() + timeout
    saw_any_live_response = False
    while time.time() < end:
        m = _scan_captures()
        if m:
            return m
        # nếu đã có ít nhất 1 capture cho creator/live/list mà chưa match → có thể break sớm để refire
        try:
            caps = driver.execute_script("return window.__tt_aff_api_capture || [];") or []
        except Exception:
            caps = []
        for c in caps:
            u = str(c.get("url") or "")
            if "creator/live/list" in u and "search/list" not in u:
                saw_any_live_response = True
                break
        if saw_any_live_response:
            break
        time.sleep(0.3)

    if not saw_any_live_response:
        # trang không bao giờ fire creator/live/list → có thể chưa click được tab
        return None

    # Vòng 2: re-fire với date range mở rộng (đề phòng widen vòng 1 không đủ hoặc bị TikTok
    # cache theo default 7 ngày do date state đã lưu trong redux). Fire lại explicit 90 ngày.
    try:
        driver.execute_script(_LIVE_REFETCH_JS, int(widen_days))
    except Exception:
        pass

    end2 = time.time() + max(10.0, timeout * 0.6)
    while time.time() < end2:
        m = _scan_captures()
        if m:
            return m
        time.sleep(0.3)
    return None


def fetch_metrics_for_live_line(
    driver, line: str,
    default_shop_id: Optional[str] = None,
    default_shop_region: Optional[str] = None,
) -> dict:
    """Mode 'DÁN LIVE': parse `@username live_id` → resolve creator_id → fetch."""
    parsed = parse_live_paste_line(line)
    if not (parsed["username"] and parsed["live_id"]):
        return {"line": line, "ok": False,
                "error": "không parse được username và/hoặc live_id (dùng format '@username 19_chữ_số')"}
    sid = (default_shop_id or "").strip()
    sreg = (default_shop_region or "VN").strip() or "VN"
    if not sid:
        return {"line": line, "ok": False,
                "error": "thiếu Shop ID mặc định trong ADMIN SETTINGS"}
    # dummy video_id (resolve_creator_id nhận cả username+video_id, nhưng cache là theo username)
    cid = resolve_creator_id(driver, parsed["username"], parsed["live_id"])
    if not cid:
        return {"line": line, "ok": False,
                "error": "không lấy được creator_id — vào ADMIN SETTINGS → CREATOR ID CACHE nhập tay username→creator_id"}
    metrics = fetch_live_metrics(driver, cid, sid, sreg, parsed["live_id"])
    if not metrics:
        return {"line": line, "ok": False,
                "error": "timeout / không thấy buổi LIVE này trong dữ liệu creator (live_id sai hoặc ngoài 90 ngày gần đây)"}
    out = {"line": line, "ok": True,
           "username": parsed["username"], "live_id": parsed["live_id"],
           "creator_id": cid, "shop_id": sid, "shop_region": sreg}
    out.update(metrics)
    return out


def process_live_links_paste(
    driver, lines: List[str], progress_cb=None,
    default_shop_id: Optional[str] = None,
    default_shop_region: Optional[str] = None,
    do_harvest: bool = True,
    admin_key: str = "admin_shared_session",
) -> List[dict]:
    """Mode DÁN LIVE: mỗi dòng = '@username live_id'."""
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
    for i, line in enumerate(lines):
        label = (line[:60] + "...") if len(line) > 63 else line
        try:
            if progress_cb: progress_cb(i, label, "xử lý...")
            r = fetch_metrics_for_live_line(driver, line, default_shop_id, default_shop_region)
            if progress_cb:
                msg = f"OK · GMV LIVE={r.get('GMV LIVE')}" if r.get("ok") else f"LỖI: {r.get('error')}"
                progress_cb(i, label, msg)
        except Exception as e:
            r = {"line": line, "ok": False, "error": str(e)}
            if progress_cb: progress_cb(i, label, f"LỖI: {e}")
        out.append(r)
    return out


def process_live_records_in_table(
    driver,
    base_cfg: dict,                 # {'base_token', 'table_id', 'identity'}
    records: List[dict],
    username_field_name: str,
    live_id_field_name: str,
    progress_cb=None,
    default_shop_id: Optional[str] = None,
    default_shop_region: Optional[str] = None,
    creator_id_field_name: Optional[str] = None,
    do_harvest: bool = True,
    admin_key: str = "admin_shared_session",
) -> List[dict]:
    """Mode LARK BASE cho LIVE: đọc 2 cột username + live_id, ghi metrics vào chính dòng."""
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

    sid = (default_shop_id or "").strip()
    sreg = (default_shop_region or "VN").strip() or "VN"

    # Đọc type cột đích 1 lần → coerce đúng kiểu khi ghi
    try:
        _fields_info = lark_list_fields(base_cfg["base_token"], base_cfg["table_id"],
                                        base_cfg.get("identity", "user"))
        field_types = {f["name"]: f.get("type") for f in _fields_info}
    except Exception:
        field_types = {}

    results = []
    for i, r in enumerate(records):
        f = r.get("fields") or {}
        u_raw = f.get(username_field_name)
        l_raw = f.get(live_id_field_name)
        # cho phép ô là dict/list (link field / lookup) → normalize về str
        if isinstance(u_raw, (dict, list)):
            u_raw = extract_link_value(u_raw)
        if isinstance(l_raw, (dict, list)):
            l_raw = extract_link_value(l_raw)
        uname = (str(u_raw or "")).strip().lstrip("@")
        lid_m = _LIVE_ID_RE.search(str(l_raw or ""))
        lid = lid_m.group(1) if lid_m else None
        label = f"{uname or '?'} · {lid or '?'}"
        try:
            if not uname:
                raise RuntimeError(f"cột '{username_field_name}' trống")
            if not lid:
                raise RuntimeError(f"cột '{live_id_field_name}' không chứa live_id hợp lệ (15-25 chữ số)")
            if not sid:
                raise RuntimeError("thiếu Shop ID mặc định trong ADMIN SETTINGS")

            cid = None
            if creator_id_field_name:
                cid_raw = f.get(creator_id_field_name)
                if isinstance(cid_raw, (dict, list)):
                    cid_raw = extract_link_value(cid_raw)
                cid_raw = (str(cid_raw or "")).strip()
                if cid_raw and cid_raw.isdigit():
                    cid = cid_raw
                    cache_set_creator_id(uname, cid)
            if not cid:
                if progress_cb: progress_cb(i, label, "resolve creator_id...")
                cid = resolve_creator_id(driver, uname, lid)
            if not cid:
                raise RuntimeError("không lấy được creator_id — nhập tay ở CREATOR ID CACHE hoặc thêm cột 'creator_id'")

            if progress_cb: progress_cb(i, label, "kéo số liệu LIVE...")
            metrics = fetch_live_metrics(driver, cid, sid, sreg, lid)
            try: drain_creator_harvest(driver, admin_key)
            except Exception: pass
            if not metrics:
                raise RuntimeError("không thấy buổi LIVE trong dữ liệu (sai live_id hoặc ngoài 90 ngày gần đây)")

            # Chỉ ghi đúng 7 cột user chỉ định (đã tạo sẵn trong base).
            # Map LIVE metrics → cột chung với luồng video:
            #   GMV                ← GMV LIVE (revenue của buổi live)
            #   video_view_avg     ← Khách hàng liên kết TB (buyers_daily_avg)
            #   video_interact_avg ← Comments + Lượt thích (interactions của buổi live)
            #   Lượt xem           ← Live PV
            _comments = metrics.get("Comments")
            _likes = metrics.get("Lượt thích")
            _interact = None
            if _comments is not None or _likes is not None:
                _interact = (_comments or 0) + (_likes or 0)
            payload = {
                "GMV": metrics.get("GMV LIVE"),
                "Hoa hồng ước tính": metrics.get("Hoa hồng ước tính"),
                "CTR (%)": metrics.get("CTR (%)"),
                "video_view_avg": metrics.get("Khách hàng liên kết TB"),
                "video_interact_avg": _interact,
                "Lượt xem": metrics.get("Live PV"),
                "Lượt thích": _likes,
            }
            if progress_cb: progress_cb(i, label, "ghi Lark Base...")
            lark_update_record_fields(
                base_cfg["base_token"], base_cfg["table_id"], r["record_id"],
                payload, identity=base_cfg.get("identity", "user"),
                field_types=field_types,
            )
            results.append({"label": label, "ok": True, "metrics": metrics})
            if progress_cb: progress_cb(i, label, f"OK · GMV LIVE={metrics.get('GMV LIVE')}")
        except Exception as e:
            results.append({"label": label, "ok": False, "error": str(e)})
            if progress_cb: progress_cb(i, label, f"LỖI: {e}")
    return results


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
    """GET /bitable/v1/apps/{app_token} — dùng trực tiếp Lark Open API."""
    d = _lark_api("GET", f"/open-apis/bitable/v1/apps/{token}")
    return d.get("app") or d


def lark_list_tables(token: str, identity: str = "user") -> List[dict]:
    """GET /bitable/v1/apps/{app_token}/tables — paginate qua page_token."""
    out: List[dict] = []
    page_token = None
    for _ in range(20):
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        d = _lark_api("GET", f"/open-apis/bitable/v1/apps/{token}/tables", params=params)
        for t in d.get("items") or []:
            tid = t.get("table_id") or t.get("id")
            name = t.get("name") or t.get("table_name")
            if tid:
                out.append({"table_id": tid, "name": name or tid})
        page_token = d.get("page_token")
        if not d.get("has_more") or not page_token:
            break
    return out


def lark_list_fields(token: str, table_id: str, identity: str = "user") -> List[dict]:
    """GET /bitable/v1/apps/{app_token}/tables/{table_id}/fields."""
    out: List[dict] = []
    page_token = None
    for _ in range(20):
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        d = _lark_api("GET",
                      f"/open-apis/bitable/v1/apps/{token}/tables/{table_id}/fields",
                      params=params)
        for f in d.get("items") or []:
            out.append({
                "field_id": f.get("field_id") or f.get("id"),
                "name": f.get("field_name") or f.get("name"),
                "type": f.get("type"),
                "ui_type": f.get("ui_type"),
            })
        page_token = d.get("page_token")
        if not d.get("has_more") or not page_token:
            break
    return out


def lark_list_all_records(token: str, table_id: str, identity: str = "user",
                          page_size: int = 500, max_pages: int = 40,
                          view_id: str = None) -> List[dict]:
    """GET /bitable/v1/apps/{app_token}/tables/{table_id}/records — auto-paginate."""
    all_rows: List[dict] = []
    page_token = None
    page_size = min(max(page_size, 1), 500)
    for _ in range(max_pages):
        params = {"page_size": page_size}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        d = _lark_api("GET",
                      f"/open-apis/bitable/v1/apps/{token}/tables/{table_id}/records",
                      params=params)
        for r in d.get("items") or []:
            all_rows.append({
                "record_id": r.get("record_id"),
                "fields": r.get("fields") or {},
            })
        page_token = d.get("page_token")
        if not d.get("has_more") or not page_token:
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

# Các cột metric ghi vào Lark cho LIVE (mode LARK BASE của tab LIVESTREAM INDEXS).
LIVE_METRIC_FIELD_SPECS = [
    ("GMV LIVE", "number"),
    ("GMV hoàn trả", "number"),
    ("Hoa hồng ước tính", "number"),
    ("Số món bán ra", "number"),
    ("Số món hoàn trả", "number"),
    ("GPM", "number"),
    ("CTR (%)", "number"),
    ("Khách hàng liên kết TB", "number"),
    ("Đơn hàng", "number"),
    ("AOV", "number"),
    ("Live PV", "number"),
    ("Lượt thích", "number"),
    ("Comments", "number"),
    ("Tên LIVE", "text"),
    ("Ngày cập nhật", "datetime"),
]


def _lark_create_field(token: str, table_id: str, name: str, ftype: str) -> None:
    """POST /bitable/v1/apps/{token}/tables/{table_id}/fields."""
    type_id = _LARK_FIELD_TYPE_MAP.get(ftype, 1)
    body: Dict[str, Any] = {"field_name": name, "type": type_id}
    # datetime cần property để hiển thị chuẩn (không có cũng chạy được, nhưng set cho gọn)
    if ftype == "datetime":
        body["property"] = {"date_formatter": "yyyy-MM-dd HH:mm", "auto_fill": False}
    _lark_api("POST",
              f"/open-apis/bitable/v1/apps/{token}/tables/{table_id}/fields",
              body=body)


def lark_ensure_metric_fields(token: str, table_id: str, identity: str = "user") -> Dict[str, str]:
    """Đảm bảo bảng có 8 cột metric video. Trả về {name: 'existed'|'created'|'error:...'}."""
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
        try:
            _lark_create_field(token, table_id, name, ftype)
            out[name] = "created"
        except Exception as e:
            out[name] = f"error: {str(e)[:160]}"
    return out


def lark_ensure_live_metric_fields(token: str, table_id: str, identity: str = "user") -> Dict[str, str]:
    """Đảm bảo bảng có đầy đủ cột metric LIVE. Trả về {name: 'existed'|'created'|'error:...'}."""
    try:
        existing = lark_list_fields(token, table_id, identity)
        existing_names = {f["name"] for f in existing}
    except Exception:
        existing_names = set()
    out: Dict[str, str] = {}
    for name, ftype in LIVE_METRIC_FIELD_SPECS:
        if name in existing_names:
            out[name] = "existed"
            continue
        try:
            _lark_create_field(token, table_id, name, ftype)
            out[name] = "created"
        except Exception as e:
            out[name] = f"error: {str(e)[:160]}"
    return out


def _coerce_field_value(v: Any, ftype: Optional[int]) -> Any:
    """Ép kiểu value cho phù hợp với type của cột Lark.
      1  = Text     → string
      2  = Number   → float/int
      5  = DateTime → int (ms)
      còn lại → giữ nguyên."""
    if v is None:
        return None
    if ftype == 1:
        if isinstance(v, (int, float)):
            # tránh 0.0/123.0 → "0.0"/"123.0"; nếu là số nguyên hoá thì bỏ ".0"
            if isinstance(v, float) and v.is_integer():
                return str(int(v))
            return str(v)
        return str(v)
    if ftype == 2:
        if isinstance(v, (int, float)):
            return v
        try:
            return float(str(v).replace(",", ""))
        except Exception:
            return None
    if ftype == 5:
        try:
            return int(v)
        except Exception:
            return None
    return v


def lark_update_record_fields(token: str, table_id: str, record_id: str,
                              fields: Dict[str, Any], identity: str = "user",
                              field_types: Optional[Dict[str, int]] = None):
    """PUT /bitable/v1/apps/{token}/tables/{table_id}/records/{record_id}.
    Nếu truyền field_types (map name→numeric type từ lark_list_fields), sẽ coerce value
    cho khớp kiểu cột (Text/Number/DateTime). Nếu không truyền, gửi nguyên value."""
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    if field_types:
        fields = {k: _coerce_field_value(v, field_types.get(k)) for k, v in fields.items()}
        fields = {k: v for k, v in fields.items() if v is not None}
    _lark_api("PUT",
              f"/open-apis/bitable/v1/apps/{token}/tables/{table_id}/records/{record_id}",
              body={"fields": fields})


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

    # Đọc type của các cột đích 1 lần → coerce đúng kiểu khi ghi (Text/Number)
    try:
        _fields_info = lark_list_fields(base_cfg["base_token"], base_cfg["table_id"],
                                        base_cfg.get("identity", "user"))
        field_types = {f["name"]: f.get("type") for f in _fields_info}
    except Exception:
        field_types = {}

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
            # Chỉ ghi đúng 7 cột user chỉ định (đã tạo sẵn trong base)
            payload = {
                "GMV": metrics.get("GMV video"),
                "Hoa hồng ước tính": metrics.get("Hoa hồng ước tính"),
                "CTR (%)": metrics.get("CTR (%)"),
                "video_view_avg": metrics.get("video_view_avg"),
                "video_interact_avg": metrics.get("video_interact_avg"),
                "Lượt xem": metrics.get("Lượt xem"),
                "Lượt thích": metrics.get("Lượt thích"),
            }
            if progress_cb: progress_cb(i, label, "ghi Lark Base...")
            lark_update_record_fields(
                base_cfg["base_token"], base_cfg["table_id"], r["record_id"],
                payload, identity=base_cfg.get("identity", "user"),
                field_types=field_types,
            )
            results.append({"label": label, "ok": True, "metrics": metrics})
            if progress_cb: progress_cb(i, label, f"OK · GMV={metrics.get('GMV video')}")
        except Exception as e:
            results.append({"label": label, "ok": False, "error": str(e)})
            if progress_cb: progress_cb(i, label, f"LỖI: {e}")
    return results
