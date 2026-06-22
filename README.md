# TikTok Creator Index — Data Crawler (Direct API)

Công cụ thu thập dữ liệu **creator/KOL** trên TikTok Shop Affiliate (Trung tâm liên kết — `affiliate.tiktok.com`).
Phiên bản này **đọc thẳng API nội bộ của TikTok** thay vì cào DOM, nên nhanh, ổn định và đầy đủ số liệu hơn.

Giao diện thao tác bằng **Streamlit**, chạy đa tiến trình để crawl song song nhiều KOL cùng lúc.

---

## 1. Vì sao dùng "Direct API" thay vì cào DOM?

| | Cào DOM (bản cũ) | Đọc API trực tiếp (bản này) |
|---|---|---|
| Cách lấy số liệu | Đợi trang render rồi đọc text trên HTML | Bắt thẳng JSON mà TikTok trả về |
| Độ ổn định | Dễ vỡ khi TikTok đổi giao diện | Bền hơn (chỉ phụ thuộc cấu trúc JSON) |
| Tốc độ | Chậm (phải chờ render, scroll, đọc nhiều phần tử) | Nhanh (1 response chứa gần như toàn bộ số liệu) |
| Độ đầy đủ | Thiếu field bị lazy-load / ẩn | Lấy được cả field không hiện trên UI |

**Vấn đề mấu chốt đã giải quyết:** nhiều API của trang chi tiết được gọi **ngay khi điều hướng** (trước khi script của ta kịp chạy). Nếu chèn hook sau khi trang load thì sẽ **bỏ lỡ** các call này.

---

## 2. Cơ chế kỹ thuật cốt lõi

### 2.1. Chèn hook *trước* mọi script của trang (CDP)
Sử dụng Chrome DevTools Protocol:

```python
driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": CAPTURE_HOOK_JS})
```

Lệnh này đảm bảo đoạn JS hook được nạp **trước** mọi script của TikTok trên **mỗi lần điều hướng** → bắt được cả những request bắn ra ngay lúc trang vừa mở.

### 2.2. Hook `fetch` và `XMLHttpRequest`
`CAPTURE_HOOK_JS` ghi đè `window.fetch` và `XMLHttpRequest` để sao chép response JSON vào 2 vùng nhớ trên `window`:
- `window.__tt_api_capture` — danh sách response của endpoint hồ sơ creator.
- `window.__tt_sug_map` — map `handle → creator_id (CID)` lấy từ API gợi ý tìm kiếm.

Phía Python chỉ việc poll 2 biến này qua `driver.execute_script(...)`.

### 2.3. Vì sao vẫn phải mở trình duyệt thật?
Mỗi request của TikTok được ký bằng `msToken`, `X-Bogus`, `X-Gnarly` — sinh ra bởi JS của chính TikTok và **không thể giả mạo** từ bên ngoài. Do đó ta để **trình duyệt thật điều hướng & ký request**, còn ta chỉ **chặn và đọc** kết quả.

### 2.4. Các endpoint sử dụng
- **Giải CID từ tên KOL:** `GET /api/v1/insights/affiliate/creator/search/suggestions`
  → đọc `data.sug_contents[].creator.creator_oecuid` để lấy `creator_id`.
- **Lấy toàn bộ số liệu creator:** `POST /api/v1/oec/affiliate/creator/marketplace/profile`
  - body: `{"creator_oec_id": <CID>, "profile_types": [N]}`
  - Trang chi tiết gọi nhiều `profile_types` (số liệu bán hàng, cộng tác, video/LIVE, nhân khẩu học...). Tool **gộp** kết quả từ các response, **ưu tiên** field `is_authorized=true` và có giá trị.

### 2.5. Luồng crawl 1 KOL
```
resolve_cid(name)            # gõ tên vào ô tìm kiếm → poll __tt_sug_map → CID
   ↓
fetch_creator_profile(cid)   # mở trang chi tiết → poll __tt_api_capture → gộp creator_profile
   ↓
map_profile_to_row(cp)       # ánh xạ JSON → các cột trong FIELD_CATALOG
```
Nếu không lấy được CID hoặc không có dữ liệu API → kiểm tra/cảnh báo **captcha** rồi đánh dấu trạng thái dòng đó.

---

## 3. Xử lý & định dạng dữ liệu

### Tiền tệ
TikTok trả số viết tắt (`1M₫+`, `131,7K đ`, `1,5Tr ₫`...). Tool **quy đổi về số đầy đủ + dấu chấm phân cách hàng nghìn**:

| Đơn vị | Hệ số | Ví dụ |
|---|---|---|
| K | 1.000 | `131,7K đ` → `131.700 ₫` |
| M / Tr | 1.000.000 | `1M₫+` → `1.000.000 ₫+`, `587,6Tr ₫` → `587.600.000 ₫` |
| T / B | 1.000.000.000 | `3,5T ₫` → `3.500.000.000 ₫` |

Với field có sẵn số thô (vd GPM) thì dùng số thô để chính xác tuyệt đối.

### Nhân khẩu học người theo dõi
- **Giới tính / Độ tuổi:** API trả tỉ lệ sẵn (`0.2191` → `21,91%`).
- **Địa điểm hàng đầu:** API chỉ trả **số đếm top địa điểm** → tool tự tính `%` theo **tổng các địa điểm trả về**.
  ⚠️ Vì TikTok tính % trên toàn bộ audience (không có trong payload này), con số có thể **lệch nhẹ** so với tooltip trên giao diện TikTok.

### Các chỉ số phái sinh
- `Tần suất đăng bài` = số video / 30 ngày (suy ra, không có field trực tiếp).
- `Tỷ lệ tương tác` = (like + comment + share) / view × 100 (tự tính).

Một số field bị **TikTok khóa quyền** (`is_authorized=false`) sẽ hiển thị `N/A` với những creator chưa có lịch sử cộng tác.

---

## 4. Cấu trúc dự án

```
.
├── app.py                 # Giao diện Streamlit + điều phối đa tiến trình (UI/luồng)
├── parallel_crawler.py    # Backend: hook API, giải CID, lấy & ánh xạ dữ liệu, worker
├── driver_manager.py      # Khởi tạo Chrome + quản lý thư mục profile
├── discover_api.py        # Script chạy 1 lần để dò/đổ JSON các endpoint (xây parser)
├── heartbeat.py           # Keep-alive phiên đăng nhập (headless), chạy bởi Task Scheduler
├── setup_heartbeat.ps1    # Đăng ký 3 task Windows Task Scheduler (random delay)
├── requirements.txt
├── .streamlit/            # Cấu hình theme
└── .gitignore
```

> **Không** đưa lên git: `chrome_profiles/` (phiên đăng nhập + cookie), `api_dumps/` (chứa token & dữ liệu thật), `creators.xlsx`, các file `RESULT_*.xlsx`.

---

## 5. Cài đặt

Yêu cầu: **Python 3.10+** và **Google Chrome** đã cài trên máy.

```bash
pip install -r requirements.txt
```
`webdriver-manager` sẽ tự tải đúng phiên bản ChromeDriver.

---

## 6. Cách sử dụng

### Bước 1 — Đăng nhập & lưu phiên (chỉ làm 1 lần)
1. Chạy app: `streamlit run app.py`
2. Vào tab **ADMIN SETTINGS**, nhập mật khẩu admin.
3. Bấm **OPEN BROWSER LOGIN** → cửa sổ Chrome mở trang affiliate → **đăng nhập TikTok**.
4. Đăng nhập xong, bấm **SAVE SESSION DATA** → phiên được lưu vào `chrome_profiles/admin_shared_session/tt_auth_state.json`.

### Bước 2 — Crawl
1. Sang tab **EXTRACTION HUB**, nhập **Operator Name**.
2. **INPUT SOURCE** — chọn 1 trong 2:
   - **Upload Excel**: file `.xlsx` có cột `creator_name`.
   - **Dán danh sách**: mỗi dòng 1 KOL (handle/tên).
3. **SELECTION OPTIONS**: chọn các chỉ số muốn lấy (để trống = lấy tất cả).
4. Bấm **INITIATE CRAWL SEQUENCE**.

### Bước 3 — Kết quả
- Hiện **bảng kết quả** ngay trên giao diện (kéo chọn ô để copy, hoặc nút copy ở góc bảng).
- Tải **Excel (.xlsx)** hoặc **CSV (.csv)**.
- File tạm `RESULT_*.xlsx` được **tự xoá** sau khi hoàn tất / dừng để tránh rác máy.

---

## 7. Cấu hình

| Mục | Vị trí | Mặc định |
|---|---|---|
| Mật khẩu admin | biến môi trường `TT_ADMIN_PASSWORD` (xem `app.py`) | có giá trị dự phòng nội bộ |
| Số worker chạy song song | `worker_ids` trong `app.py` | 2 (`["A", "B"]`) |
| Shop region | `SHOP_URL` / `DETAIL_URL_BASE` trong `parallel_crawler.py` | `VN` |

> Tăng số worker = nhanh hơn nhưng **nặng/lag máy** hơn. 2 worker là mức cân bằng cho máy phổ thông.

### Dò lại endpoint khi TikTok đổi API
```bash
python discover_api.py "ten_creator"
```
Script mở trang chi tiết, bắt mọi JSON liên quan và đổ vào `api_dumps/` để cập nhật parser.

---

## 8. Giữ phiên đăng nhập tự động (Heartbeat)

Phiên TikTok bị timeout nếu tài khoản **không hoạt động** trong thời gian dài.
Để tránh phải đăng nhập lại tay, dự án có sẵn cơ chế **heartbeat keep-alive**: định kỳ
mở thầm trang affiliate (headless Chrome) ở chế độ nền để TikTok refresh cookie/token.

### Cơ chế
- `heartbeat.py` mở Chrome **headless** với đúng profile `admin_shared_session/LOGIN`
  (nguồn mà mọi worker copy session từ đó).
- Vào `affiliate.tiktok.com`, cuộn nhẹ vài lần, đợi ~30–60s rồi đóng.
- TikTok thấy có "hoạt động" → kéo dài timestamp `last_active` của phiên.
- Vì sửa đúng profile gốc, **mọi worker** mở sau đó đều có session mới.

### Lịch chạy (random)
Script `setup_heartbeat.ps1` đăng ký 3 task trên Windows Task Scheduler, mỗi task
có **RandomDelay** để giờ thực sự kích hoạt là ngẫu nhiên (đỡ giống bot):

| Task | Khung giờ thực tế |
|---|---|
| `TikTokCrawl_Heartbeat_Night` | 01:30 – 04:30 (random) |
| `TikTokCrawl_Heartbeat_Noon` | 12:00 – 13:00 (random) |
| `TikTokCrawl_Heartbeat_Random` | 09:00 – 16:00 (random) |

### Cài đặt
Mở **PowerShell với quyền Administrator** (chuột phải → *Run as administrator*),
rồi chạy 1 lần:
```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\PC\Data Crawl\Tiktok Crawl API\setup_heartbeat.ps1"
```
> Cần admin vì task dùng `LogonType S4U` (chạy được cả khi user chưa đăng nhập GUI).

Gỡ bỏ:
```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\PC\Data Crawl\Tiktok Crawl API\setup_heartbeat.ps1" -Remove
```

### Tắt màn hình / khoá máy có ảnh hưởng không?
- **Không.** Chrome chạy nền không cần display; task đặt `S4U` nên kích hoạt được cả khi user
  chưa đăng nhập GUI và `WakeToRun=true` để kéo máy dậy nếu cần.
- **Cần** máy không sleep/hibernate hoàn toàn (Power Options → đặt "Sleep: Never" hoặc cho phép
  wake timer). Tắt màn hình thuần (display off) không sao.

### Log & debug
- Log mỗi lần chạy ghi tại `heartbeat.log` (đã ignore khỏi git).
- Chạy thủ công với cửa sổ Chrome hiện ra để debug:
  ```bash
  python heartbeat.py --visible
  ```
- Khi heartbeat phát hiện mất phiên (cookie session biến mất / redirect login),
  dòng log sẽ ghi `logged_in=False` → đăng nhập lại tay theo Bước 1 mục 6.

### Giới hạn
- Heartbeat chỉ chống timeout do **idle**, không chống được **hard expiry**
  (TikTok có thể vẫn buộc đăng nhập lại sau 30–60 ngày).
- Nếu TikTok bật captcha (rất hiếm với traffic đăng nhập sẵn), heartbeat sẽ ghi
  `logged_in=False` và bạn cần login tay.

---

## 9. AFF INDEXS — Kéo số liệu video aff vào Lark Base

Module mới `aff_index.py` cho phép tự động kéo số liệu của **từng video aff** (GMV, hoa hồng, CTR, lượt thích/bình luận/xem, số món bán ra) rồi ghi đè vào chính dòng tương ứng trong **Lark Base bất kỳ** mà team đang vận hành.

### 9.1. Tổng quan

Tab **AFF INDEXS** trong `app.py` có 2 mode chạy song song:

| Mode | Đầu vào | Đầu ra |
|---|---|---|
| **TỪ LARK BASE** | URL/token Lark Base + chọn bảng + chọn cột chứa link video | Ghi đè số liệu lên CHÍNH dòng đó + thêm cột `Ngày cập nhật` |
| **DÁN LINK** | Textarea — mỗi dòng 1 link video TikTok | Bảng kết quả + tải Excel/CSV (không ghi Lark) |

### 9.2. Pipeline 1 link

```
link KOL gửi
   ↓ parse_aff_link  → {username, video_id, [shop_id], [shop_region]}
   ↓ _resolve_shop_fields → bù shop_id/region từ Shop Config (Admin Settings)
   ↓ resolve_creator_id  → cache → harvest → affiliate API → (fallback) TikTok public
   ↓ fetch_video_metrics → mở affiliate.tiktok.com/data/creator-analysis → CDP hook bắt response
   ↓ extract_metrics     → {GMV, Hoa hồng, CTR%, Like, Comment, Sold, View}
   ↓ lark_update_record_fields  (record-upsert, ghi đè dòng cũ)
```

### 9.3. Tự động tạo cột metric

Trước khi ghi, gọi `lark_ensure_metric_fields()`:
- Đọc field-list của bảng người dùng chọn.
- So với spec 8 cột chuẩn (`GMV video`, `Hoa hồng ước tính`, `CTR (%)`, `Lượt thích`, `Bình luận`, `Số món bán ra`, `Lượt xem`, `Ngày cập nhật`).
- **Chỉ tạo cột nào chưa có** — nếu bảng đã có sẵn thì giữ nguyên, không động vào.

### 9.4. Cấu hình 1 lần ở ADMIN SETTINGS

| Mục | Mục đích |
|---|---|
| **AFF SHOP CONFIG** — Shop ID + Region | Bù `shop_id` cho các link KOL gửi từ trang TikTok cá nhân (không có `?shop_id=...`). Shop ID là cố định cho 1 shop. |
| **CREATOR ID CACHE** | (Fallback) Bảng `username → creator_id` chỉnh tay. Lưu vào `aff_creator_cache.json`. Bình thường không cần đụng tới — auto-harvest tự nhồi. |

---

## 10. Các vấn đề khó khăn & cách bypass

### 10.1. Cookie tách domain → `tiktok.com` bị login wall

**Vấn đề:** Session admin đăng nhập ở `affiliate.tiktok.com`. Khi pipeline cũ mở `https://www.tiktok.com/@user/video/<id>` để đọc `__UNIVERSAL_DATA_FOR_REHYDRATION__` lấy `creator_id` → TikTok bật **login wall**, DOM không có JSON đầy đủ.

**Bypass — Auto-harvest từ chính affiliate.tiktok.com:**
1. Cài CDP hook `AFF_CREATOR_HARVEST_JS` (patch `fetch` + `XMLHttpRequest`) ngay khi driver khởi động.
2. Hook passive-scan MỌI response từ `affiliate.tiktok.com` / `/aff_api/` / `/aff_creator_api/` / `/aff_oec_api/`.
3. Walk recursive JSON tìm cặp `{creator_id | author_id | user.id, unique_id | username | nickname}` → lưu vào `window.__tt_cid_collect`.
4. Trước khi xử lý records, driver tự navigate vào `affiliate.tiktok.com/connection/creator?shop_region=VN` (trang list KOL collab của shop), cuộn cho lazy-load, đợi tới khi số entry ổn định.
5. Drain `window.__tt_cid_collect` → ghi vào `aff_creator_cache.json` (lowercase username).
6. Sau đó `resolve_creator_id(username)` chỉ cần hit cache, **không bao giờ cần đụng `tiktok.com` public**.

**Side-channel harvest:** sau mỗi lần fetch metrics ở `creator-analysis?creator_id=X`, response cũng chứa author info → tự drain thêm vào cache.

**Fallback chain** (chỉ kích hoạt khi auto-harvest không bắt được KOL đó):
1. Cache hit → 2. Profile page `tiktok.com/@username` (ít bị chặn hơn video page) → 3. Video page → 4. Embed page `tiktok.com/embed/v2/<video_id>`.

### 10.2. Link KOL gửi không có `shop_id`

**Vấn đề:** KOL share link từ trang cá nhân `https://www.tiktok.com/@user/video/<id>` — không có `?shop_id=...&shop_region=...`. Pipeline cũ require đủ 3 thông tin nên báo lỗi.

**Insight:** `shop_id` là **shop của bạn**, cố định cho mọi link mọi KOL.

**Bypass:**
- Admin lưu Shop ID 1 lần ở **ADMIN SETTINGS → AFF SHOP CONFIG** (persist vào `aff_shop_config.json` trong `chrome_profiles/admin_shared_session/`).
- `_resolve_shop_fields(parsed, default)`: ưu tiên giá trị parse từ link, fallback default từ config.
- Region mặc định `VN`.

### 10.3. `lark-cli` API shape không nhất quán

**Vấn đề 1:** Lệnh `+record-batch-update` báo `code: 800010701 "Remove unsupported fields"` khi truyền `{"records":[{record_id, fields}]}`.

**Nguyên nhân:** `+record-batch-update` chỉ dùng cho **same-patch batch** với shape `{"record_id_list": [...], "patch": {...}}` — không phải per-record different-fields.

**Bypass:** Chuyển sang `+record-upsert --record-id <rid> --json '{field: value}'` cho update per-record với fields khác nhau.

**Vấn đề 2:** Một số lệnh (`base-get`, `table-list`, `field-list`) không support `--format json` (chỉ `record-list` support).

**Bypass:** Gọi không có `--format`, parse JSON từ stdout output mặc định (đã là JSON). Helper `_run_lark()` tìm `{` đầu tiên trong stdout rồi `json.loads`.

**Vấn đề 3:** Field-create payload không phải `{"field_name":"X","type":2}` mà là `{"name":"X","type":"text"}` — type là **string**, không phải int enum.

**Bypass:** `METRIC_FIELD_SPECS = [("GMV video", "number"), ..., ("Ngày cập nhật", "datetime")]` — type string.

**Vấn đề 4:** Response shape:
- `base-get`: `data.base.{name,url,base_token,...}` (không phải `data` trực tiếp)
- `table-list`: `data.tables[].{id, name}` (không phải `table_id`/`table_name`)
- `field-list`: `data.fields[].{id, name, type, style}`

### 10.4. Cell value Lark trả markdown link

**Vấn đề:** Cột text chứa URL trả về dạng markdown `[url](url)` → `urlparse` ăn cả `[` `]` `)`.

**Bypass:** `_strip_md_link()` regex `\[([^\]]+)\]\(([^)]+)\)` → ưu tiên URL trong `()`, fallback `[]` nếu là URL.

### 10.5. Cross-page checkbox selection trong Streamlit

**Vấn đề:** `st.data_editor` chỉ giữ trạng thái của trang đang render. Khi pagination, chuyển trang là mất lựa chọn ở các trang trước.

**Bypass:** Selection được lưu ở `st.session_state.aff_selected_rids: set[str]`. Mỗi lần render trang, sync 2 chiều:
- Khởi tạo `df["Chọn"] = (record_id in sel_set)` cho dòng của trang hiện tại.
- Sau khi user edit, iterate `edited.iterrows()` → `sel_set.add/discard(rid)`.
Chuyển trang chỉ thay đổi page index, `sel_set` được giữ nguyên.

### 10.6. Hook fetch trước script của TikTok (CDP)

**Vấn đề:** Nhiều API của TikTok bắn ra **ngay khi điều hướng** (trước khi `window.onload`). Inject hook sau khi trang load thì bỏ lỡ.

**Bypass:** Dùng `Page.addScriptToEvaluateOnNewDocument` của Chrome DevTools Protocol:
```python
driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_JS})
```
Đoạn JS này được nạp **trước** mọi script của trang, **trên mỗi lần điều hướng**. Đây cũng là kỹ thuật dùng cho cả `AFF_CAPTURE_HOOK_JS` (bắt metric) và `AFF_CREATOR_HARVEST_JS` (harvest creator_id).

### 10.7. msToken, X-Bogus, X-Gnarly — không thể giả mạo

**Vấn đề:** Mỗi request TikTok được ký bằng các header sinh ra bởi JS của chính TikTok, đổi mỗi request, dùng 1 lần.

**Bypass:** Không cố ký request, mà để **trình duyệt thật điều hướng & ký**, ta chỉ **chặn và đọc** kết quả qua hook trên `window.fetch` / `XMLHttpRequest`. Đây là lý do vẫn cần Selenium dù đã đọc thẳng API.

---

## 11. Lưu ý & giới hạn
- **Phiên hết hạn / captcha:** nếu TikTok chặn, tool đánh dấu dòng lỗi và cảnh báo — cần đăng nhập lại (Bước 1).
- **Quyền truy cập field:** field `is_authorized=false` sẽ là `N/A`.
- **% địa điểm** tính theo top trả về, lệch nhẹ so với UI TikTok (xem mục 3).
- Tôn trọng điều khoản dịch vụ của TikTok; chỉ dùng cho dữ liệu mà tài khoản của bạn được phép truy cập.

## 10. Bảo mật
- **Không commit** session/cookie (`tt_auth_state.json`), `api_dumps/`, hay danh sách KOL — đã chặn sẵn trong `.gitignore`.
- Đổi mật khẩu admin qua `TT_ADMIN_PASSWORD` thay vì để giá trị mặc định trong code.
