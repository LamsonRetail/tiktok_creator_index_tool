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

## 8. Lưu ý & giới hạn
- **Phiên hết hạn / captcha:** nếu TikTok chặn, tool đánh dấu dòng lỗi và cảnh báo — cần đăng nhập lại (Bước 1).
- **Quyền truy cập field:** field `is_authorized=false` sẽ là `N/A`.
- **% địa điểm** tính theo top trả về, lệch nhẹ so với UI TikTok (xem mục 3).
- Tôn trọng điều khoản dịch vụ của TikTok; chỉ dùng cho dữ liệu mà tài khoản của bạn được phép truy cập.

## 9. Bảo mật
- **Không commit** session/cookie (`tt_auth_state.json`), `api_dumps/`, hay danh sách KOL — đã chặn sẵn trong `.gitignore`.
- Đổi mật khẩu admin qua `TT_ADMIN_PASSWORD` thay vì để giá trị mặc định trong code.
