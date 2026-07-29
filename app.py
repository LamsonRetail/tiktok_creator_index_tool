import os
import io
import json
import time
import threading
import subprocess
from multiprocessing import Process, Queue
import streamlit as st
import pandas as pd

from driver_manager import (
    init_driver,
    get_login_profile_dir,
    get_worker_profile_dir,
    get_user_dir,
)
from parallel_crawler import export_auth_state, worker_crawl, FIELD_CATALOG, import_auth_state
import aff_index

# ===============================
# HELPER: CHỈ ĐÓNG CHROME CỦA TOOL
# ===============================
def kill_specific_tool_chromes():
    """Chỉ quét sạch các Chrome chạy theo Profile của 4 Worker"""
    try:
        if os.name == 'nt':
            # Danh sách đủ 4 Profile để đảm bảo không bị sót tiến trình chạy ngầm
            for profile in ["Worker_A", "Worker_B", "Worker_C", "Worker_D"]:
                cmd = f'wmic process where "commandline like \'%--user-data-dir=%{profile}%\'" delete'
                subprocess.run(cmd, shell=True, capture_output=True)
            # Đóng chromedriver để giải phóng RAM
            subprocess.run("taskkill /F /IM chromedriver.exe /T", shell=True, capture_output=True)
    except Exception:
        pass

# ===============================
# SINGLE USER LOCK MANAGER
# ===============================
class SystemLock:
    def __init__(self):
        self.locked = False
        self.active_user = None
        self.start_time = None
        self._lock = threading.Lock()

    def try_acquire(self, user_name):
        with self._lock:
            if not self.locked:
                self.locked = True
                self.active_user = user_name
                self.start_time = time.time()
                return True
            return False

    def release(self):
        with self._lock:
            self.locked = False
            self.active_user = None
            self.start_time = None

    def get_status(self):
        return self.locked, self.active_user

    def force_reset(self):
        with self._lock:
            self.locked = False
            self.active_user = None

@st.cache_resource
def get_system_lock():
    return SystemLock()

sys_lock = get_system_lock()

# ===============================
# CONFIG & PATHS
# ===============================
ADMIN_KEY = "admin_shared_session"
# Mật khẩu admin: nên đặt qua biến môi trường TT_ADMIN_PASSWORD; mặc định để chạy nội bộ.
ADMIN_PASSWORD = os.environ.get("TT_ADMIN_PASSWORD", "abc")
auth_state_path = os.path.join(get_user_dir(ADMIN_KEY), "tt_auth_state.json")

st.set_page_config(
    page_title="TikTok Creator Crawler | Elite Edition", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# ===============================
# UI RUNTIME STATE
# ===============================
if "job_running" not in st.session_state:
    st.session_state.job_running = False
if "p1" not in st.session_state:
    st.session_state.p1 = None
if "p2" not in st.session_state:
    st.session_state.p2 = None

# ===============================
# ELITE UI CSS (ENHANCED V2)
# ===============================
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=Space+Grotesk:wght@700&display=swap');
    
    html, body, [data-testid="stAppViewContainer"] { font-family: 'Inter', sans-serif; }

    /* Logo TikTok Adaptive - Giúp logo nổi bật trên mọi nền */
    [data-testid="stSidebar"] img {
        filter: drop-shadow(0px 0px 8px rgba(255, 255, 255, 0.4));
        transition: all 0.3s ease;
    }
    @media (prefers-color-scheme: dark) {
        [data-testid="stSidebar"] img {
            filter: brightness(0) invert(1) drop-shadow(0px 0px 10px rgba(0, 242, 234, 0.3));
        }
    }

    .main-header { 
        font-family: 'Space Grotesk', sans-serif;
        font-weight: 900; font-size: 3.8rem !important; 
        background: linear-gradient(90deg, #FF0050 0%, #00F2EA 100%);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        letter-spacing: -2px; line-height: 1.1;
    }
    
    /* Multiselect Tags - Làm đẹp tag chọn trường dữ liệu */
    div[data-baseweb="tag"] {
        background: linear-gradient(135deg, rgba(255, 0, 80, 0.1) 0%, rgba(0, 242, 234, 0.1) 100%) !important;
        border: 1px solid rgba(255, 0, 80, 0.2) !important;
        border-radius: 6px !important;
        padding: 4px 10px !important;
    }
    div[data-baseweb="tag"] span {
        color: var(--text-color) !important;
        font-weight: 500 !important;
        font-size: 0.8rem !important;
    }
    div[data-baseweb="tag"] svg { fill: #FF0050 !important; }

    /* Button Styling */
    .stButton>button {
        background: linear-gradient(90deg, #FF0050 0%, #ad1457 100%) !important;
        color: white !important; border: none !important; border-radius: 10px !important;
        font-weight: 600 !important; height: 3.2rem !important;
        text-transform: uppercase; letter-spacing: 0.8px; transition: all 0.2s;
    }
    .stButton>button:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 15px rgba(255, 0, 80, 0.3);
    }

    /* Cancel Button - Elite Dark Style */
    div.stButton > button[kind="secondary"] {
        background: #1a1b21 !important;
        border: 1px solid rgba(255, 255, 255, 0.15) !important;
        color: #efefef !important;
    }
    div.stButton > button[kind="secondary"]:hover {
        border-color: #00F2EA !important;
        color: #00F2EA !important;
        background: #25262e !important;
    }

    .status-badge {
        font-family: 'Inter', sans-serif; font-weight: 600;
        padding: 12px 15px; border-radius: 8px; border-left: 4px solid;
        background-color: rgba(128, 128, 128, 0.05); font-size: 0.85rem;
    }
    .status-free { border-color: #00F2EA; }
    .status-busy { border-color: #FF0050; }
    </style>
    """, unsafe_allow_html=True)

# ===============================
# SIDEBAR
# ===============================
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/en/a/a9/TikTok_logo.svg", width=140)
    st.markdown("<br>", unsafe_allow_html=True)
    is_locked, active_user = sys_lock.get_status()
    st.markdown("### SYSTEM STATUS")
    if not is_locked:
        st.markdown('<div class="status-badge status-free">SYSTEM AVAILABLE<br><small style="opacity:0.6">Ready for command</small></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="status-badge status-busy">SYSTEM BUSY<br><small style="opacity:0.6">Active: {active_user}</small></div>', unsafe_allow_html=True)
    st.divider()
    st.caption("ENGINE V.2025-ELITE")

# ===============================
# MAIN UI
# ===============================
st.markdown('<p class="main-header">TIKTOK CRAWLER CREATOR INDEX</p>', unsafe_allow_html=True)
st.markdown('<p style="opacity:0.6; text-transform:uppercase; letter-spacing:1px; font-size:0.8rem; margin-top:-15px; margin-bottom:40px;">Advanced Intelligence Extraction System</p>', unsafe_allow_html=True)

tab_extract, tab_aff, tab_live, tab_control = st.tabs(["CREATOR INDEX", "VIDEO AFF", "LIVESTREAM INDEXS", "ADMIN SETTINGS"])

with tab_extract:
    if os.path.exists(auth_state_path):
        c_left, c_right = st.columns([1.8, 1])
        with c_left:
            st.markdown("#### 1. IDENTIFICATION")
            user_name = st.text_input("Operator Name:", placeholder="Thien Quy Digital Trans", key="user_identity")
            
            st.markdown("#### 2. INPUT SOURCE")
            input_mode = st.radio(
                "Nguồn KOL:",
                ["Upload Excel", "Dán danh sách (mỗi KOL 1 dòng)"],
                horizontal=True, key="input_mode",
            )
            uploaded_file = None
            pasted_text = ""
            if input_mode == "Upload Excel":
                uploaded_file = st.file_uploader("Upload Excel File:", type=["xlsx"])
            else:
                pasted_text = st.text_area(
                    "Dán danh sách KOL (mỗi dòng 1 KOL):", height=180,
                    key="pasted_kols", placeholder="kol_1\nkol_2\nkol_3\n...",
                )

            def _build_creators():
                if input_mode == "Upload Excel":
                    if not uploaded_file:
                        return []
                    try:
                        _dfx = pd.read_excel(uploaded_file)
                        return _dfx["creator_name"].dropna().astype(str).str.strip().tolist()
                    except Exception:
                        return []
                return [ln.strip() for ln in (pasted_text or "").splitlines() if ln.strip()]

            creators_list = _build_creators()
            has_input = len(creators_list) > 0

            if has_input and user_name.strip():
                st.markdown("#### 3. SELECTION OPTIONS")
                _meta = {f["key"]: f for f in FIELD_CATALOG}
                _keys = [f["key"] for f in FIELD_CATALOG]
                def _fmt(k): return f'{_meta.get(k, {}).get("label", k)}'
                selected_fields = st.multiselect("Select Metrics to Extract:", options=_keys, default=[], format_func=_fmt)
                fields_for_export = selected_fields or _keys

                st.markdown("#### 4. EXECUTION")
                is_busy, current_runner = sys_lock.get_status()
                btn_placeholder = st.empty()
                
                # --- PHẦN ĐIỀU KHIỂN NÚT (ẨN/HIỆN/CANCEL) ---
                if st.session_state.job_running:
                    if btn_placeholder.button(" STOP OPERATION & DISCONNECT", use_container_width=True, type="secondary"):
                        # 1. Dừng các tiến trình ngay lập tức
                        if "active_workers" in st.session_state:
                            for p in st.session_state.active_workers:
                                if p.is_alive(): p.terminate()
                        
                        # 2. Đóng Chrome
                        kill_specific_tool_chromes() 
                        
                        # 3. Thu thập dữ liệu đã crawl được cho đến lúc bấm dừng, rồi xoá file tạm
                        interrupted_frames = []
                        for wid in ["A", "B", "C", "D"]:
                            p_out = os.path.join(get_user_dir(ADMIN_KEY), f"RESULT_{wid}.xlsx")
                            if os.path.exists(p_out):
                                interrupted_frames.append(pd.read_excel(p_out))
                                try:
                                    os.remove(p_out)
                                except Exception:
                                    pass

                        if interrupted_frames:
                            st.session_state.final_df = pd.concat(interrupted_frames, ignore_index=True)

                        sys_lock.release()
                        st.session_state.job_running = False
                        st.rerun()
                else:
                    if is_busy:
                        btn_placeholder.button(f"SYSTEM OCCUPIED BY {current_runner.upper()}", disabled=True, use_container_width=True)
                    else:
                        if btn_placeholder.button("INITIATE CRAWL SEQUENCE", use_container_width=True, type="primary"):
                            if sys_lock.try_acquire(user_name):
                                st.session_state.job_running = True
                                st.rerun()

                # --- PHẦN LOGIC CHẠY (GIỮ NGUYÊN 100%) ---
                if st.session_state.job_running:
                    st.markdown("<div style='margin-top:15px; padding:15px; border-radius:10px; background:rgba(0,242,234,0.05); border:1px solid rgba(0,242,234,0.2); color:#00F2EA; font-weight:600;'> Don't refresh or F5 this page. We are processing your request...</div>", unsafe_allow_html=True)
                    try:
                        # 1. Đọc dữ liệu đầu vào và chuẩn bị Queue tiến độ
                        creators = creators_list
                        q = Queue()
                        
                        # 2. Khởi tạo danh sách Worker và dọn dẹp file kết quả cũ
                        worker_ids = ["A", "B"]
                        n_workers = len(worker_ids)
                        out_paths = []
                        st.session_state.active_workers = [] # Lưu danh sách tiến trình để quản lý

                        for wid in worker_ids:
                            p_out = os.path.join(get_user_dir(ADMIN_KEY), f"RESULT_{wid}.xlsx")
                            if os.path.exists(p_out): 
                                os.remove(p_out)
                            out_paths.append(p_out)

                        # 3. Kích hoạt các luồng xử lý với cơ chế khởi động lệch giờ
                        for i, wid in enumerate(worker_ids):
                            # Chia nhỏ danh sách creator cho các máy xử lý độc lập
                            p = Process(
                                target=worker_crawl,
                                args=(wid, creators[i::n_workers], get_worker_profile_dir(ADMIN_KEY, wid),
                                      auth_state_path, out_paths[i], q, False, fields_for_export)
                            )
                            p.start()
                            st.session_state.active_workers.append(p)
                            
                            st.caption(f"⚙️ Launching Worker {wid} (Profile: Worker_{wid})...")
                            time.sleep(3) # Khoảng nghỉ để Windows ổn định trình điều khiển Chrome

                        # 4. Theo dõi và hiển thị tiến độ thu thập dữ liệu
                        prog_bar = st.progress(0)
                        log_win = st.expander(f"SYSTEM GENERAL ({n_workers} WORKERS ACTIVE)", expanded=True)
                        done, total = 0, len(creators)
                        
                        # Chờ cho đến khi tất cả các luồng hoàn thành và dữ liệu trong Queue được xử lý hết
                        while any(p.is_alive() for p in st.session_state.active_workers) or not q.empty():
                            while not q.empty():
                                msg = q.get()
                                # Nhận tín hiệu báo captcha từ worker
                                if msg[0] == "captcha_alert":
                                    st.error(f"🛑 **TikTok đã phát hiện crawl tại Máy {msg[1]} ({msg[2]}). Hãy thông báo cho admin để xử lý!**")
                                # Bạn có thể thêm lệnh phát tiếng kêu bíp tại đây nếu muốn
                                # os.system('echo \a')
                                elif msg[0] == "done":
                                    done += 1
                                    prog_bar.progress(min(done/total, 1.0))
                                    log_win.text(f"[{msg[1]}] Success: {msg[2]} ({msg[6]}s)")
                            time.sleep(0.1)
                        
                        # Đảm bảo các tiến trình được đóng sạch sẽ sau khi xong
                        for p in st.session_state.active_workers: 
                            p.join()
                        
                        # 5. Gộp kết quả tổng hợp từ 4 file Excel thành phần
                        frames = []
                        for f in out_paths:
                            if os.path.exists(f):
                                try:
                                    frames.append(pd.read_excel(f))
                                except Exception:
                                    pass

                        if frames:
                            st.session_state.final_df = pd.concat(frames, ignore_index=True)
                            st.session_state.final_summary = f"Hệ thống đã thu thập thành công dữ liệu của {done}/{total} creators."

                    except Exception as e:
                        st.error(f"ERROR: {e}")
                        # Dù lỗi vẫn cố gắng lấy dữ liệu đã có
                        interrupted_frames = []
                        for wid in ["A", "B", "C", "D"]:
                            p_out = os.path.join(get_user_dir(ADMIN_KEY), f"RESULT_{wid}.xlsx")
                            if os.path.exists(p_out):
                                interrupted_frames.append(pd.read_excel(p_out))
                        if interrupted_frames:
                            st.session_state.final_df = pd.concat(interrupted_frames, ignore_index=True)
                    finally:
                        # Dữ liệu đã nằm trong session (final_df) -> xoá file tạm tránh rác máy
                        for wid in ["A", "B", "C", "D"]:
                            p_tmp = os.path.join(get_user_dir(ADMIN_KEY), f"RESULT_{wid}.xlsx")
                            try:
                                if os.path.exists(p_tmp):
                                    os.remove(p_tmp)
                            except Exception:
                                pass
                        st.session_state.job_running = False
                        sys_lock.release() # Giải phóng quyền truy cập hệ thống
            elif not user_name.strip():
                st.info("Identity required to access extraction engine.")
            else:
                st.info("Cung cấp danh sách KOL (upload Excel hoặc dán danh sách) để bắt đầu.")
        with c_right:
            st.markdown("#### HARDWARE INFO")
            st.markdown(f"- **GATEWAY:** `{os.environ.get('COMPUTERNAME', 'SERVER-01')}`\n- **STATUS:** `{'BUSY' if is_locked else 'AVAILABLE'}`")
            if is_locked: st.info(f"Operator: {active_user}")

        # Hiển thị kết quả thu hoạch được (dành cho cả trường hợp chạy xong or bị ngắt)
        if "final_df" in st.session_state and not st.session_state.job_running:
            df_res = st.session_state.final_df
            st.divider()
            st.success(st.session_state.get("final_summary") or f"Đã thu thập dữ liệu của {len(df_res)} creators.")

            c1, c2 = st.columns(2)
            with c1:
                buf = io.BytesIO()
                df_res.to_excel(buf, index=False, engine='openpyxl')
                st.download_button("TẢI FILE EXCEL (.xlsx)", data=buf.getvalue(), file_name="TIKTOK_DATA_FULL.xlsx", use_container_width=True)
            with c2:
                st.download_button("TẢI FILE CSV (.csv)", data=df_res.to_csv(index=False).encode('utf-8-sig'), file_name="TIKTOK_DATA_FULL.csv", use_container_width=True)

            # Bảng kết quả ngay trên giao diện để copy trực tiếp (đổi tên cột sang nhãn tiếng Việt)
            _labels = {f["key"]: f["label"] for f in FIELD_CATALOG}
            _labels.update({"creator_name": "Tên creator", "status": "Trạng thái"})
            st.caption("Bảng kết quả (bấm vào ô / kéo chọn để copy, hoặc dùng nút Copy ở góc bảng):")
            st.dataframe(df_res.rename(columns=_labels), use_container_width=True, hide_index=True)

with tab_aff:
    # ===============================
    # AFF INDEXS — chọn base bất kỳ + paste link
    # ===============================
    if not os.path.exists(auth_state_path):
        st.warning("Chưa có phiên TikTok. Vào tab ADMIN SETTINGS đăng nhập và SAVE SESSION DATA trước.")
    else:
        try:
            _identity = (dict(st.secrets["lark"]).get("identity") if "lark" in st.secrets else "user") or "user"
        except Exception:
            _identity = "user"

        # Cảnh báo nếu chưa cấu hình Shop ID (KOL gửi link gốc tiktok.com sẽ thiếu shop_id)
        _shop_cfg_top = aff_index.load_shop_config(ADMIN_KEY)
        if not _shop_cfg_top.get("shop_id"):
            st.warning(
                "Chưa cấu hình **Shop ID** trong ADMIN SETTINGS. "
                "Các link KOL gửi từ trang TikTok cá nhân (không có `?shop_id=...`) sẽ báo lỗi. "
                "Vào tab ADMIN SETTINGS → mục **AFF SHOP CONFIG** để lưu Shop ID 1 lần."
            )
        else:
            _cache_size = len(aff_index.load_creator_cache(ADMIN_KEY))
            st.info(
                f"Khi bấm EXECUTE, hệ thống sẽ tự mở `affiliate.tiktok.com/connection/creator` "
                f"và quét toàn bộ KOL collab của shop để nạp creator_id vào cache "
                f"(hiện đang có **{_cache_size}** username trong cache). "
                f"Không cần đụng vào trang TikTok public hay nhập thủ công."
            )

        mode_a, mode_b = st.tabs(["TỪ LARK BASE", "DÁN LINK"])

        # =============================================================
        # MODE A: chọn base bất kỳ, chọn cột link, paginate 40/page,
        #         execute → auto-tạo cột metric + ghi đè cùng dòng.
        # =============================================================
        with mode_a:
            ca_left, ca_right = st.columns([1.8, 1])
            with ca_left:
                st.markdown("#### 1. CONNECTION")
                base_input = st.text_input(
                    "Lark Base URL hoặc base_token",
                    placeholder="https://xxx.larksuite.com/base/XXXXXXXXXXXXX  hoặc  XXXXXXXXXXXXX",
                    key="aff_base_input",
                )

                def _connect_base(raw_input: str):
                    token = aff_index.extract_base_token(raw_input)
                    if not token:
                        st.error("Vui lòng dán URL Lark Base hoặc base_token.")
                        return
                    try:
                        with st.spinner("Kết nối Lark, đọc bảng & records..."):
                            info = aff_index.lark_base_get(token, identity=_identity)
                            tables = aff_index.lark_list_tables(token, identity=_identity)
                            if not tables:
                                st.error("Base không có bảng nào.")
                                return
                            # nếu URL có ?table=tbl... thì ưu tiên dùng làm bảng mặc định
                            hint_tid = aff_index.extract_table_id_hint(raw_input)
                            first_tid = hint_tid if any(t["table_id"] == hint_tid for t in tables) else tables[0]["table_id"]
                            fields = aff_index.lark_list_fields(token, first_tid, identity=_identity)
                            records = aff_index.lark_list_all_records(token, first_tid, identity=_identity)
                        st.session_state.aff_base_token = token
                        st.session_state.aff_base_info = info
                        st.session_state.aff_tables = tables
                        st.session_state.aff_table_id = first_tid
                        st.session_state.aff_fields = fields
                        st.session_state.aff_records = records
                        st.session_state.aff_link_field = None
                        st.session_state.aff_page = 1
                        st.session_state.aff_selected_rids = set()
                        st.session_state.pop("aff_last_results", None)
                    except Exception as e:
                        st.error(f"Không kết nối được: {e}")

                if st.button("CONNECT", key="aff_connect_btn", use_container_width=True):
                    _connect_base(base_input)

                if st.session_state.get("aff_base_token"):
                    info = st.session_state.get("aff_base_info") or {}
                    tables = st.session_state.get("aff_tables") or []
                    st.success(f"{info.get('name') or st.session_state.aff_base_token}  ·  {len(tables)} bảng")

                    st.markdown("#### 2. CHỌN BẢNG")
                    tbl_ids = [t["table_id"] for t in tables]
                    cur_tid = st.session_state.get("aff_table_id") or tbl_ids[0]
                    tbl_index_default = tbl_ids.index(cur_tid) if cur_tid in tbl_ids else 0
                    sel_tbl_idx = st.selectbox(
                        "Bảng dữ liệu:",
                        options=list(range(len(tables))),
                        index=tbl_index_default,
                        format_func=lambda i: tables[i]["name"],
                        key="aff_tbl_select",
                    )
                    chosen_tid = tables[sel_tbl_idx]["table_id"]
                    if chosen_tid != st.session_state.get("aff_table_id"):
                        try:
                            with st.spinner("Đang tải records..."):
                                fields = aff_index.lark_list_fields(
                                    st.session_state.aff_base_token, chosen_tid, identity=_identity,
                                )
                                records = aff_index.lark_list_all_records(
                                    st.session_state.aff_base_token, chosen_tid, identity=_identity,
                                )
                            st.session_state.aff_table_id = chosen_tid
                            st.session_state.aff_fields = fields
                            st.session_state.aff_records = records
                            st.session_state.aff_link_field = None
                            st.session_state.aff_page = 1
                            st.session_state.aff_selected_rids = set()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Đọc bảng lỗi: {e}")

                    fields = st.session_state.get("aff_fields") or []
                    field_names = [f["name"] for f in fields if f.get("name")]
                    if not field_names:
                        st.info("Bảng này chưa có cột nào.")
                    else:
                        # Cột link: ưu tiên "Link video/id live" (cột dùng chung với LIVE),
                        # fallback "Link video", cuối cùng tìm cột đầu tiên có "link"
                        link_field = (
                            "Link video/id live" if "Link video/id live" in field_names else
                            "Link video" if "Link video" in field_names else
                            next((n for n in field_names if "link" in n.lower()), None)
                        )
                        # Cột creator_id: tự phát hiện (không có cũng OK — sẽ resolve/harvest)
                        cid_field = next(
                            (n for n in field_names if "creator_id" in n.lower() or "creator id" in n.lower()),
                            None,
                        )
                        st.session_state.aff_link_field = link_field
                        st.session_state.aff_cid_field = cid_field
                        if not link_field:
                            st.error("Bảng chưa có cột **Link video/id live**. Thêm cột này rồi thử lại.")
                            st.stop()
                        st.caption(
                            f"Cột link: **{link_field}**"
                            + (f"  ·  Cột creator_id: **{cid_field}**" if cid_field else "")
                        )

                        # Nhận mọi dòng có dữ liệu (link video hoặc live_id đều được).
                        # User tick chọn dòng nào thì tool xử lý dòng đó — tab VIDEO AFF
                        # ưu tiên link video, dòng chỉ có live_id sẽ báo lỗi khi EXECUTE.
                        records = st.session_state.get("aff_records") or []
                        eligible = []
                        for _r in records:
                            _v = aff_index.extract_link_value((_r.get("fields") or {}).get(link_field))
                            if _v and str(_v).strip():
                                eligible.append(_r)

                        st.markdown(f"#### 3. RECORDS  ·  {len(eligible)} dòng có dữ liệu / {len(records)} tổng")

                        if not eligible:
                            st.info(f"Không có dòng nào có dữ liệu ở cột `{link_field}`.")
                        else:
                            PAGE_SIZE = 40
                            total_pages = max(1, (len(eligible) + PAGE_SIZE - 1) // PAGE_SIZE)
                            page = int(st.session_state.get("aff_page", 1))
                            page = max(1, min(page, total_pages))

                            if not isinstance(st.session_state.get("aff_selected_rids"), set):
                                st.session_state.aff_selected_rids = set()
                            sel_set: set = st.session_state.aff_selected_rids

                            # thanh chọn / bỏ chọn trang + refresh
                            cs1, cs2, cs3, cs4 = st.columns([1, 1, 1, 2])
                            with cs1:
                                if st.button("Chọn cả trang", key="aff_sel_page", use_container_width=True):
                                    for r in eligible[(page-1)*PAGE_SIZE: page*PAGE_SIZE]:
                                        sel_set.add(r["record_id"])
                                    st.rerun()
                            with cs2:
                                if st.button("Bỏ chọn trang", key="aff_unsel_page", use_container_width=True):
                                    for r in eligible[(page-1)*PAGE_SIZE: page*PAGE_SIZE]:
                                        sel_set.discard(r["record_id"])
                                    st.rerun()
                            with cs3:
                                if st.button("Xoá lựa chọn", key="aff_sel_clear", use_container_width=True):
                                    sel_set.clear()
                                    st.rerun()
                            with cs4:
                                if st.button("REFRESH RECORDS", key="aff_refresh_records",
                                             use_container_width=True, type="secondary"):
                                    try:
                                        with st.spinner("Đang tải lại records từ Lark..."):
                                            st.session_state.aff_records = aff_index.lark_list_all_records(
                                                st.session_state.aff_base_token,
                                                st.session_state.aff_table_id,
                                                identity=_identity,
                                            )
                                        st.session_state.aff_page = 1
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Refresh lỗi: {e}")

                            page_records = eligible[(page-1)*PAGE_SIZE: page*PAGE_SIZE]
                            # Preview cột cố định theo yêu cầu (chỉ hiện cột nào thực sự có trong bảng)
                            _PREVIEW_VIDEO = ["creator_name", "Mã Yêu cầu", "Tệp KOC",
                                              "KOL_Trạng thái liên hệ", "Loại hình"]
                            preview_field_names = [n for n in _PREVIEW_VIDEO
                                                   if n in field_names and n != link_field]
                            rows = []
                            for r in page_records:
                                f = r.get("fields") or {}
                                row = {
                                    "Chọn": r["record_id"] in sel_set,
                                    link_field: aff_index.extract_link_value(f.get(link_field)),
                                }
                                for n in preview_field_names:
                                    v = f.get(n)
                                    if isinstance(v, (dict, list)):
                                        try: v = json.dumps(v, ensure_ascii=False)
                                        except Exception: v = str(v)
                                    row[n] = "" if v is None else str(v)
                                row["_rid"] = r["record_id"]
                                rows.append(row)
                            df_page = pd.DataFrame(rows)

                            edited = st.data_editor(
                                df_page,
                                hide_index=True,
                                use_container_width=True,
                                column_config={
                                    "Chọn": st.column_config.CheckboxColumn(width="small"),
                                    link_field: st.column_config.LinkColumn(),
                                    "_rid": None,
                                },
                                disabled=[link_field] + preview_field_names,
                                key=f"aff_editor_page_{page}",
                            )
                            for _, row in edited.iterrows():
                                rid = row["_rid"]
                                if row["Chọn"]:
                                    sel_set.add(rid)
                                else:
                                    sel_set.discard(rid)

                            # ----- PAGINATION đơn giản: < 1 2 3 ... > -----
                            def _render_pagination(cur: int, total: int):
                                # tối đa 7 nút số trang quanh trang hiện tại
                                window = 5
                                if total <= window + 2:
                                    pages = list(range(1, total + 1))
                                else:
                                    half = window // 2
                                    start = max(1, cur - half)
                                    end = min(total, start + window - 1)
                                    start = max(1, end - window + 1)
                                    pages = list(range(start, end + 1))
                                    if pages[0] > 1:
                                        pages = [1, "…"] + pages
                                    if pages[-1] < total:
                                        pages = pages + ["…", total]

                                btns = ["<"] + pages + [">"]
                                cols = st.columns(len(btns))
                                for idx, b in enumerate(btns):
                                    with cols[idx]:
                                        if b == "<":
                                            if st.button("<", key=f"aff_pg_prev_{cur}",
                                                         disabled=(cur <= 1), use_container_width=True):
                                                st.session_state.aff_page = cur - 1
                                                st.rerun()
                                        elif b == ">":
                                            if st.button(">", key=f"aff_pg_next_{cur}",
                                                         disabled=(cur >= total), use_container_width=True):
                                                st.session_state.aff_page = cur + 1
                                                st.rerun()
                                        elif b == "…":
                                            st.markdown("<div style='text-align:center;opacity:0.5;padding-top:10px'>…</div>",
                                                        unsafe_allow_html=True)
                                        else:
                                            label = str(b)
                                            if b == cur:
                                                st.markdown(
                                                    f"<div style='text-align:center;padding:8px 0;"
                                                    f"background:linear-gradient(90deg,#FF0050,#ad1457);"
                                                    f"color:white;border-radius:8px;font-weight:600;'>{label}</div>",
                                                    unsafe_allow_html=True,
                                                )
                                            else:
                                                if st.button(label, key=f"aff_pg_{b}_{cur}", use_container_width=True):
                                                    st.session_state.aff_page = int(b)
                                                    st.rerun()

                            st.caption(f"Trang {page}/{total_pages}  ·  đã chọn {len(sel_set)} dòng")
                            _render_pagination(page, total_pages)

                            st.markdown("#### 4. EXECUTION")
                            is_busy_a, runner_a = sys_lock.get_status()
                            if st.session_state.get("aff_running"):
                                st.info("Đang xử lý — đừng F5 / chuyển tab cho đến khi xong.")
                            elif is_busy_a:
                                st.button(f"SYSTEM OCCUPIED BY {runner_a.upper()}",
                                          disabled=True, use_container_width=True)
                            elif st.button(
                                f"EXECUTE · LẤY SỐ LIỆU & GHI VÀO BASE  ({len(sel_set)} dòng)",
                                disabled=(len(sel_set) == 0),
                                use_container_width=True, type="primary",
                                key="aff_run_btn",
                            ):
                                if sys_lock.try_acquire("aff_index"):
                                    st.session_state.aff_running = True
                                    st.session_state.aff_selected_snapshot = list(sel_set)
                                    st.rerun()

                            if st.session_state.get("aff_running"):
                                sel_rids = st.session_state.get("aff_selected_snapshot", [])
                                rid_set = set(sel_rids)
                                selected = [r for r in eligible if r["record_id"] in rid_set]
                                prog = st.progress(0.0)
                                status_box = st.empty()
                                results = []
                                driver = None
                                try:
                                    status_box.write("Khởi tạo trình duyệt & phiên TikTok...")
                                    driver = init_driver(get_login_profile_dir(ADMIN_KEY))
                                    import_auth_state(driver, auth_state_path)

                                    def _cb(i, label, msg):
                                        try:
                                            prog.progress(min((i + 1) / max(len(selected), 1), 1.0))
                                            status_box.write(f"[{i+1}/{len(selected)}] {label}: {msg}")
                                        except Exception:
                                            pass

                                    _shop_def = aff_index.load_shop_config(ADMIN_KEY)
                                    results = aff_index.process_records_in_table(
                                        driver,
                                        {
                                            "base_token": st.session_state.aff_base_token,
                                            "table_id": st.session_state.aff_table_id,
                                            "identity": _identity,
                                        },
                                        selected, st.session_state.aff_link_field,
                                        progress_cb=_cb,
                                        default_shop_id=_shop_def.get("shop_id") or None,
                                        default_shop_region=_shop_def.get("shop_region") or "VN",
                                        creator_id_field_name=st.session_state.get("aff_cid_field"),
                                        admin_key=ADMIN_KEY,
                                    )
                                except Exception as e:
                                    st.error(f"Lỗi vận hành: {e}")
                                finally:
                                    try:
                                        if driver: driver.quit()
                                    except Exception:
                                        pass
                                    sys_lock.release()
                                    st.session_state.aff_running = False
                                    st.session_state.aff_last_results = results
                                    # reload records từ Lark để thấy số mới
                                    try:
                                        st.session_state.aff_records = aff_index.lark_list_all_records(
                                            st.session_state.aff_base_token,
                                            st.session_state.aff_table_id,
                                            identity=_identity,
                                        )
                                    except Exception:
                                        pass
                                    st.session_state.aff_selected_rids = set()
                                    st.rerun()

                            if "aff_last_results" in st.session_state and not st.session_state.get("aff_running"):
                                rs = st.session_state.aff_last_results
                                ok = sum(1 for r in rs if r.get("ok"))
                                err = len(rs) - ok
                                st.success(f"Hoàn thành: {ok} OK · {err} lỗi")
                                res_rows = []
                                for r in rs:
                                    if r.get("ok"):
                                        m = r.get("metrics") or {}
                                        res_rows.append({
                                            "Link": r.get("label"), "Trạng thái": "OK",
                                            "GMV": m.get("GMV video"), "Hoa hồng": m.get("Hoa hồng ước tính"),
                                            "CTR (%)": m.get("CTR (%)"), "Lượt xem": m.get("Lượt xem"),
                                            "Lượt thích": m.get("Lượt thích"), "Bình luận": m.get("Bình luận"),
                                            "Sold": m.get("Số món bán ra"),
                                        })
                                    else:
                                        res_rows.append({"Link": r.get("label"), "Trạng thái": f"LỖI: {r.get('error','')}",
                                                         "GMV": None, "Hoa hồng": None, "CTR (%)": None,
                                                         "Lượt xem": None, "Lượt thích": None,
                                                         "Bình luận": None, "Sold": None})
                                if res_rows:
                                    st.dataframe(pd.DataFrame(res_rows), use_container_width=True, hide_index=True)

            with ca_right:
                st.markdown("#### HARDWARE INFO")
                _busy_a, _ = sys_lock.get_status()
                st.markdown(
                    f"- **GATEWAY:** `{os.environ.get('COMPUTERNAME', 'SERVER-01')}`\n"
                    f"- **STATUS:** `{'BUSY' if _busy_a else 'AVAILABLE'}`"
                )
                st.caption("Mặc định 40 dòng / trang. Tích vào ô Chọn ở các trang khác nhau đều được ghi nhận.")
                st.caption("Số liệu lấy là **cửa sổ 7 ngày gần nhất**.")
                st.caption("EXECUTE chỉ ghi 7 cột: **GMV · Hoa hồng ước tính · CTR (%) · video_view_avg · video_interact_avg · Lượt xem · Lượt thích** (bạn đã tạo sẵn trong base).")

        # =============================================================
        # MODE B: dán link → trả bảng + cho tải Excel/CSV (không ghi Lark)
        # =============================================================
        with mode_b:
            cb_left, cb_right = st.columns([1.8, 1])
            with cb_left:
                st.markdown("#### 1. DÁN LINK")
                paste_links = st.text_area(
                    "Mỗi dòng một link video TikTok:",
                    height=200, key="aff_paste_text",
                    placeholder="https://www.tiktok.com/@user1/video/1234567890\nhttps://www.tiktok.com/@user2/video/0987654321",
                )
                link_list = [ln.strip() for ln in (paste_links or "").splitlines() if ln.strip()]
                st.caption(f"Đã nhận **{len(link_list)}** link.")

                st.markdown("#### 2. EXECUTION")
                is_busy_b, runner_b = sys_lock.get_status()
                if st.session_state.get("aff_paste_running"):
                    st.info("Đang xử lý — đừng F5 / chuyển tab cho đến khi xong.")
                elif is_busy_b:
                    st.button(f"SYSTEM OCCUPIED BY {runner_b.upper()}",
                              disabled=True, use_container_width=True, key="aff_paste_busy")
                elif st.button(
                    f"EXECUTE · LẤY SỐ LIỆU  ({len(link_list)} link)",
                    disabled=(len(link_list) == 0),
                    use_container_width=True, type="primary",
                    key="aff_paste_run_btn",
                ):
                    if sys_lock.try_acquire("aff_paste"):
                        st.session_state.aff_paste_running = True
                        st.session_state.aff_paste_links = link_list
                        st.rerun()

                if st.session_state.get("aff_paste_running"):
                    links = st.session_state.get("aff_paste_links", [])
                    prog = st.progress(0.0)
                    status_box = st.empty()
                    results = []
                    driver = None
                    try:
                        driver = init_driver(get_login_profile_dir(ADMIN_KEY))
                        import_auth_state(driver, auth_state_path)

                        def _cb_b(i, label, msg):
                            try:
                                prog.progress(min((i + 1) / max(len(links), 1), 1.0))
                                status_box.write(f"[{i+1}/{len(links)}] {label}: {msg}")
                            except Exception:
                                pass

                        _shop_def_b = aff_index.load_shop_config(ADMIN_KEY)
                        results = aff_index.process_links_paste(
                            driver, links, progress_cb=_cb_b,
                            default_shop_id=_shop_def_b.get("shop_id") or None,
                            default_shop_region=_shop_def_b.get("shop_region") or "VN",
                            admin_key=ADMIN_KEY,
                        )
                    except Exception as e:
                        st.error(f"Lỗi vận hành: {e}")
                    finally:
                        try:
                            if driver: driver.quit()
                        except Exception:
                            pass
                        sys_lock.release()
                        st.session_state.aff_paste_running = False
                        st.session_state.aff_paste_results = results
                        st.rerun()

                if "aff_paste_results" in st.session_state and not st.session_state.get("aff_paste_running"):
                    rs = st.session_state.aff_paste_results
                    ok = sum(1 for r in rs if r.get("ok"))
                    err = len(rs) - ok
                    st.success(f"Hoàn thành: {ok} OK · {err} lỗi")
                    res_rows = []
                    for r in rs:
                        if r.get("ok"):
                            res_rows.append({
                                "Link": r.get("link"), "username": r.get("username"),
                                "video_id": r.get("video_id"), "creator_id": r.get("creator_id"),
                                "shop_id": r.get("shop_id"), "shop_region": r.get("shop_region"),
                                "Tên video": r.get("Tên video"),
                                "GMV video": r.get("GMV video"),
                                "Hoa hồng ước tính": r.get("Hoa hồng ước tính"),
                                "CTR (%)": r.get("CTR (%)"),
                                "Lượt xem": r.get("Lượt xem"),
                                "Lượt thích": r.get("Lượt thích"),
                                "Bình luận": r.get("Bình luận"),
                                "Số món bán ra": r.get("Số món bán ra"),
                                "Trạng thái": "OK",
                            })
                        else:
                            res_rows.append({
                                "Link": r.get("link"), "username": None, "video_id": None,
                                "creator_id": None, "shop_id": None, "shop_region": None,
                                "Tên video": None,
                                "GMV video": None, "Hoa hồng ước tính": None, "CTR (%)": None,
                                "Lượt xem": None, "Lượt thích": None, "Bình luận": None,
                                "Số món bán ra": None,
                                "Trạng thái": f"LỖI: {r.get('error','')}",
                            })
                    if res_rows:
                        df_res = pd.DataFrame(res_rows)
                        st.dataframe(df_res, use_container_width=True, hide_index=True)
                        cdl1, cdl2 = st.columns(2)
                        with cdl1:
                            _buf = io.BytesIO()
                            df_res.to_excel(_buf, index=False, engine="openpyxl")
                            st.download_button(
                                "TẢI EXCEL (.xlsx)", data=_buf.getvalue(),
                                file_name="AFF_METRICS.xlsx", use_container_width=True,
                                key="aff_paste_dl_xlsx",
                            )
                        with cdl2:
                            st.download_button(
                                "TẢI CSV (.csv)",
                                data=df_res.to_csv(index=False).encode("utf-8-sig"),
                                file_name="AFF_METRICS.csv", use_container_width=True,
                                key="aff_paste_dl_csv",
                            )

            with cb_right:
                st.markdown("#### HARDWARE INFO")
                _busy_b2, _ = sys_lock.get_status()
                st.markdown(
                    f"- **GATEWAY:** `{os.environ.get('COMPUTERNAME', 'SERVER-01')}`\n"
                    f"- **STATUS:** `{'BUSY' if _busy_b2 else 'AVAILABLE'}`"
                )
                st.caption("Mode này **không ghi** Lark. Kết quả chỉ hiển thị + tải file.")
                st.caption("Chấp nhận link gốc `https://www.tiktok.com/@user/video/<id>` — shop_id sẽ tự lấy từ ADMIN SETTINGS.")

with tab_live:
    # ===============================
    # LIVESTREAM INDEXS — booking chốt live_id, tool crawl GMV LIVE
    # Giao diện giống VIDEO AFF: từ Lark Base hoặc dán live_id.
    # ===============================
    if not os.path.exists(auth_state_path):
        st.warning("Chưa có phiên TikTok. Vào tab ADMIN SETTINGS đăng nhập và SAVE SESSION DATA trước.")
    else:
        try:
            _identity_l = (dict(st.secrets["lark"]).get("identity") if "lark" in st.secrets else "user") or "user"
        except Exception:
            _identity_l = "user"

        _shop_cfg_top_l = aff_index.load_shop_config(ADMIN_KEY)
        if not _shop_cfg_top_l.get("shop_id"):
            st.warning(
                "Chưa cấu hình **Shop ID** trong ADMIN SETTINGS. "
                "Không có shop_id thì không thể query analytics LIVE. "
                "Vào tab ADMIN SETTINGS → mục **AFF SHOP CONFIG** để lưu Shop ID 1 lần."
            )
        else:
            _cache_size_l = len(aff_index.load_creator_cache(ADMIN_KEY))
            st.info(
                f"Booking cần gửi **username** + **live_id** (id của buổi live). "
                f"Khi bấm EXECUTE, hệ thống sẽ mở `affiliate.tiktok.com/connection/creator` để nạp creator_id "
                f"(hiện đang có **{_cache_size_l}** username trong cache), rồi vào trang `creator-analysis` → tab LIVE "
                f"để lấy số liệu buổi live theo `live_id`."
            )

        mode_a_live, mode_b_live = st.tabs(["TỪ LARK BASE", "DÁN LIVE ID"])

        # =============================================================
        # MODE A: chọn base, chọn 2 cột (username + live_id), execute
        #         → auto-tạo cột metric LIVE + ghi đè cùng dòng.
        # =============================================================
        with mode_a_live:
            la_left, la_right = st.columns([1.8, 1])
            with la_left:
                st.markdown("#### 1. CONNECTION")
                base_input_l = st.text_input(
                    "Lark Base URL hoặc base_token",
                    placeholder="https://xxx.larksuite.com/base/XXXXXXXXXXXXX  hoặc  XXXXXXXXXXXXX",
                    key="live_base_input",
                )

                def _connect_base_live(raw_input: str):
                    token = aff_index.extract_base_token(raw_input)
                    if not token:
                        st.error("Vui lòng dán URL Lark Base hoặc base_token.")
                        return
                    try:
                        with st.spinner("Kết nối Lark, đọc bảng & records..."):
                            info = aff_index.lark_base_get(token, identity=_identity_l)
                            tables = aff_index.lark_list_tables(token, identity=_identity_l)
                            if not tables:
                                st.error("Base không có bảng nào.")
                                return
                            hint_tid = aff_index.extract_table_id_hint(raw_input)
                            first_tid = hint_tid if any(t["table_id"] == hint_tid for t in tables) else tables[0]["table_id"]
                            fields = aff_index.lark_list_fields(token, first_tid, identity=_identity_l)
                            records = aff_index.lark_list_all_records(token, first_tid, identity=_identity_l)
                        st.session_state.live_base_token = token
                        st.session_state.live_base_info = info
                        st.session_state.live_tables = tables
                        st.session_state.live_table_id = first_tid
                        st.session_state.live_fields = fields
                        st.session_state.live_records = records
                        st.session_state.live_uname_field = None
                        st.session_state.live_lid_field = None
                        st.session_state.live_page = 1
                        st.session_state.live_selected_rids = set()
                        st.session_state.pop("live_last_results", None)
                    except Exception as e:
                        st.error(f"Không kết nối được: {e}")

                if st.button("CONNECT", key="live_connect_btn", use_container_width=True):
                    _connect_base_live(base_input_l)

                if st.session_state.get("live_base_token"):
                    info = st.session_state.get("live_base_info") or {}
                    tables = st.session_state.get("live_tables") or []
                    st.success(f"{info.get('name') or st.session_state.live_base_token}  ·  {len(tables)} bảng")

                    st.markdown("#### 2. CHỌN BẢNG")
                    tbl_ids = [t["table_id"] for t in tables]
                    cur_tid = st.session_state.get("live_table_id") or tbl_ids[0]
                    tbl_index_default = tbl_ids.index(cur_tid) if cur_tid in tbl_ids else 0
                    sel_tbl_idx = st.selectbox(
                        "Bảng dữ liệu:",
                        options=list(range(len(tables))),
                        index=tbl_index_default,
                        format_func=lambda i: tables[i]["name"],
                        key="live_tbl_select",
                    )
                    chosen_tid = tables[sel_tbl_idx]["table_id"]
                    if chosen_tid != st.session_state.get("live_table_id"):
                        try:
                            with st.spinner("Đang tải records..."):
                                fields = aff_index.lark_list_fields(
                                    st.session_state.live_base_token, chosen_tid, identity=_identity_l,
                                )
                                records = aff_index.lark_list_all_records(
                                    st.session_state.live_base_token, chosen_tid, identity=_identity_l,
                                )
                            st.session_state.live_table_id = chosen_tid
                            st.session_state.live_fields = fields
                            st.session_state.live_records = records
                            st.session_state.live_uname_field = None
                            st.session_state.live_lid_field = None
                            st.session_state.live_page = 1
                            st.session_state.live_selected_rids = set()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Đọc bảng lỗi: {e}")

                    fields = st.session_state.get("live_fields") or []
                    field_names = [f["name"] for f in fields if f.get("name")]
                    if not field_names:
                        st.info("Bảng này chưa có cột nào.")
                    else:
                        # Cột username: cố định "creator_name", fallback tìm "user"/"kol"/"creator"
                        uname_field = ("creator_name" if "creator_name" in field_names else
                                       next((n for n in field_names
                                             if "user" in n.lower() or "kol" in n.lower() or "creator" in n.lower()),
                                            None))
                        # Cột id live: ưu tiên "Link video/id live" (dùng chung với luồng video),
                        # fallback các biến thể "id live" / "live id" / "live_id"
                        _lower_map = {n.lower(): n for n in field_names}
                        lid_field = (_lower_map.get("link video/id live")
                                     or _lower_map.get("id live")
                                     or _lower_map.get("live id") or _lower_map.get("live_id")
                                     or next((n for n in field_names
                                              if "live" in n.lower() and ("id" in n.lower() or "room" in n.lower())),
                                             None))
                        # Cột creator_id: tự phát hiện (tuỳ chọn)
                        cid_field = next(
                            (n for n in field_names if "creator_id" in n.lower() or "creator id" in n.lower()),
                            None,
                        )
                        st.session_state.live_uname_field = uname_field
                        st.session_state.live_lid_field = lid_field
                        st.session_state.live_cid_field = cid_field
                        if not uname_field:
                            st.error("Bảng chưa có cột **creator_name**. Thêm cột này rồi thử lại.")
                            st.stop()
                        if not lid_field:
                            st.error("Bảng chưa có cột **Link video/id live**. Thêm cột này rồi thử lại.")
                            st.stop()
                        st.caption(
                            f"Cột username: **{uname_field}**  ·  Cột id live: **{lid_field}**"
                            + (f"  ·  Cột creator_id: **{cid_field}**" if cid_field else "")
                        )

                        # Chỉ nhận dòng có live_id thuần (loại các dòng chứa URL video)
                        import re as _re_live
                        _LID_RE = _re_live.compile(r"\d{15,25}")
                        records = st.session_state.get("live_records") or []
                        def _has_live(r):
                            f = r.get("fields") or {}
                            u = f.get(uname_field); l = f.get(lid_field)
                            if isinstance(u, (dict, list)):
                                u = aff_index.extract_link_value(u)
                            if isinstance(l, (dict, list)):
                                l = aff_index.extract_link_value(l)
                            if not (str(u or "")).strip():
                                return False
                            s = str(l or "")
                            if "/video/" in s or "tiktok.com" in s.lower():
                                return False
                            return bool(_LID_RE.search(s))
                        eligible = [r for r in records if _has_live(r)]

                        st.markdown(f"#### 3. RECORDS  ·  {len(eligible)} dòng đủ 2 cột / {len(records)} tổng")

                        if not eligible:
                            st.info(f"Không có dòng nào có đủ cả cột `{uname_field}` và `{lid_field}`.")
                        else:
                            PAGE_SIZE = 40
                            total_pages = max(1, (len(eligible) + PAGE_SIZE - 1) // PAGE_SIZE)
                            page = int(st.session_state.get("live_page", 1))
                            page = max(1, min(page, total_pages))

                            if not isinstance(st.session_state.get("live_selected_rids"), set):
                                st.session_state.live_selected_rids = set()
                            sel_set: set = st.session_state.live_selected_rids

                            cs1, cs2, cs3, cs4 = st.columns([1, 1, 1, 2])
                            with cs1:
                                if st.button("Chọn cả trang", key="live_sel_page", use_container_width=True):
                                    for r in eligible[(page-1)*PAGE_SIZE: page*PAGE_SIZE]:
                                        sel_set.add(r["record_id"])
                                    st.rerun()
                            with cs2:
                                if st.button("Bỏ chọn trang", key="live_unsel_page", use_container_width=True):
                                    for r in eligible[(page-1)*PAGE_SIZE: page*PAGE_SIZE]:
                                        sel_set.discard(r["record_id"])
                                    st.rerun()
                            with cs3:
                                if st.button("Xoá lựa chọn", key="live_sel_clear", use_container_width=True):
                                    sel_set.clear()
                                    st.rerun()
                            with cs4:
                                if st.button("REFRESH RECORDS", key="live_refresh_records",
                                             use_container_width=True, type="secondary"):
                                    try:
                                        with st.spinner("Đang tải lại records từ Lark..."):
                                            st.session_state.live_records = aff_index.lark_list_all_records(
                                                st.session_state.live_base_token,
                                                st.session_state.live_table_id,
                                                identity=_identity_l,
                                            )
                                        st.session_state.live_page = 1
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Refresh lỗi: {e}")

                            page_records = eligible[(page-1)*PAGE_SIZE: page*PAGE_SIZE]
                            # Preview cột cố định theo yêu cầu
                            _PREVIEW_LIVE = ["Mã Yêu cầu", "Tệp KOC",
                                             "KOL_Trạng thái liên hệ", "Loại hình"]
                            preview_field_names = [n for n in _PREVIEW_LIVE
                                                   if n in field_names and n not in (uname_field, lid_field)]
                            rows = []
                            for r in page_records:
                                f = r.get("fields") or {}
                                u_val = f.get(uname_field)
                                if isinstance(u_val, (dict, list)):
                                    u_val = aff_index.extract_link_value(u_val)
                                l_val = f.get(lid_field)
                                if isinstance(l_val, (dict, list)):
                                    l_val = aff_index.extract_link_value(l_val)
                                row = {
                                    "Chọn": r["record_id"] in sel_set,
                                    uname_field: "" if u_val is None else str(u_val),
                                    lid_field: "" if l_val is None else str(l_val),
                                }
                                for n in preview_field_names:
                                    v = f.get(n)
                                    if isinstance(v, (dict, list)):
                                        try: v = json.dumps(v, ensure_ascii=False)
                                        except Exception: v = str(v)
                                    row[n] = "" if v is None else str(v)
                                row["_rid"] = r["record_id"]
                                rows.append(row)
                            df_page = pd.DataFrame(rows)

                            edited = st.data_editor(
                                df_page,
                                hide_index=True,
                                use_container_width=True,
                                column_config={
                                    "Chọn": st.column_config.CheckboxColumn(width="small"),
                                    "_rid": None,
                                },
                                disabled=[uname_field, lid_field] + preview_field_names,
                                key=f"live_editor_page_{page}",
                            )
                            for _, row in edited.iterrows():
                                rid = row["_rid"]
                                if row["Chọn"]:
                                    sel_set.add(rid)
                                else:
                                    sel_set.discard(rid)

                            def _render_pagination_live(cur: int, total: int):
                                window = 5
                                if total <= window + 2:
                                    pages = list(range(1, total + 1))
                                else:
                                    half = window // 2
                                    start = max(1, cur - half)
                                    end = min(total, start + window - 1)
                                    start = max(1, end - window + 1)
                                    pages = list(range(start, end + 1))
                                    if pages[0] > 1:
                                        pages = [1, "…"] + pages
                                    if pages[-1] < total:
                                        pages = pages + ["…", total]
                                btns = ["<"] + pages + [">"]
                                cols = st.columns(len(btns))
                                for idx, b in enumerate(btns):
                                    with cols[idx]:
                                        if b == "<":
                                            if st.button("<", key=f"live_pg_prev_{cur}",
                                                         disabled=(cur <= 1), use_container_width=True):
                                                st.session_state.live_page = cur - 1
                                                st.rerun()
                                        elif b == ">":
                                            if st.button(">", key=f"live_pg_next_{cur}",
                                                         disabled=(cur >= total), use_container_width=True):
                                                st.session_state.live_page = cur + 1
                                                st.rerun()
                                        elif b == "…":
                                            st.markdown("<div style='text-align:center;opacity:0.5;padding-top:10px'>…</div>",
                                                        unsafe_allow_html=True)
                                        else:
                                            label = str(b)
                                            if b == cur:
                                                st.markdown(
                                                    f"<div style='text-align:center;padding:8px 0;"
                                                    f"background:linear-gradient(90deg,#FF0050,#ad1457);"
                                                    f"color:white;border-radius:8px;font-weight:600;'>{label}</div>",
                                                    unsafe_allow_html=True,
                                                )
                                            else:
                                                if st.button(label, key=f"live_pg_{b}_{cur}", use_container_width=True):
                                                    st.session_state.live_page = int(b)
                                                    st.rerun()

                            st.caption(f"Trang {page}/{total_pages}  ·  đã chọn {len(sel_set)} dòng")
                            _render_pagination_live(page, total_pages)

                            st.markdown("#### 4. EXECUTION")
                            is_busy_l, runner_l = sys_lock.get_status()
                            if st.session_state.get("live_running"):
                                st.info("Đang xử lý — đừng F5 / chuyển tab cho đến khi xong.")
                            elif is_busy_l:
                                st.button(f"SYSTEM OCCUPIED BY {runner_l.upper()}",
                                          disabled=True, use_container_width=True, key="live_busy_btn")
                            elif st.button(
                                f"EXECUTE · LẤY SỐ LIỆU LIVE & GHI VÀO BASE  ({len(sel_set)} dòng)",
                                disabled=(len(sel_set) == 0),
                                use_container_width=True, type="primary",
                                key="live_run_btn",
                            ):
                                if sys_lock.try_acquire("live_index"):
                                    st.session_state.live_running = True
                                    st.session_state.live_selected_snapshot = list(sel_set)
                                    st.rerun()

                            if st.session_state.get("live_running"):
                                sel_rids = st.session_state.get("live_selected_snapshot", [])
                                rid_set = set(sel_rids)
                                selected = [r for r in eligible if r["record_id"] in rid_set]
                                prog = st.progress(0.0)
                                status_box = st.empty()
                                results = []
                                driver = None
                                try:
                                    status_box.write("Khởi tạo trình duyệt & phiên TikTok...")
                                    driver = init_driver(get_login_profile_dir(ADMIN_KEY))
                                    import_auth_state(driver, auth_state_path)

                                    def _cb_l(i, label, msg):
                                        try:
                                            prog.progress(min((i + 1) / max(len(selected), 1), 1.0))
                                            status_box.write(f"[{i+1}/{len(selected)}] {label}: {msg}")
                                        except Exception:
                                            pass

                                    _shop_def_l = aff_index.load_shop_config(ADMIN_KEY)
                                    results = aff_index.process_live_records_in_table(
                                        driver,
                                        {
                                            "base_token": st.session_state.live_base_token,
                                            "table_id": st.session_state.live_table_id,
                                            "identity": _identity_l,
                                        },
                                        selected,
                                        st.session_state.live_uname_field,
                                        st.session_state.live_lid_field,
                                        progress_cb=_cb_l,
                                        default_shop_id=_shop_def_l.get("shop_id") or None,
                                        default_shop_region=_shop_def_l.get("shop_region") or "VN",
                                        creator_id_field_name=st.session_state.get("live_cid_field"),
                                        admin_key=ADMIN_KEY,
                                    )
                                except Exception as e:
                                    st.error(f"Lỗi vận hành: {e}")
                                finally:
                                    try:
                                        if driver: driver.quit()
                                    except Exception:
                                        pass
                                    sys_lock.release()
                                    st.session_state.live_running = False
                                    st.session_state.live_last_results = results
                                    try:
                                        st.session_state.live_records = aff_index.lark_list_all_records(
                                            st.session_state.live_base_token,
                                            st.session_state.live_table_id,
                                            identity=_identity_l,
                                        )
                                    except Exception:
                                        pass
                                    st.session_state.live_selected_rids = set()
                                    st.rerun()

                            if "live_last_results" in st.session_state and not st.session_state.get("live_running"):
                                rs = st.session_state.live_last_results
                                ok = sum(1 for r in rs if r.get("ok"))
                                err = len(rs) - ok
                                st.success(f"Hoàn thành: {ok} OK · {err} lỗi")
                                res_rows = []
                                for r in rs:
                                    if r.get("ok"):
                                        m = r.get("metrics") or {}
                                        res_rows.append({
                                            "KOL · Live": r.get("label"), "Trạng thái": "OK",
                                            "GMV LIVE": m.get("GMV LIVE"),
                                            "GMV hoàn trả": m.get("GMV hoàn trả"),
                                            "Hoa hồng ước tính": m.get("Hoa hồng ước tính"),
                                            "Số món bán ra": m.get("Số món bán ra"),
                                            "Số món hoàn trả": m.get("Số món hoàn trả"),
                                            "GPM": m.get("GPM"),
                                            "Khách hàng liên kết TB": m.get("Khách hàng liên kết TB"),
                                        })
                                    else:
                                        res_rows.append({"KOL · Live": r.get("label"),
                                                         "Trạng thái": f"LỖI: {r.get('error','')}",
                                                         "GMV LIVE": None, "GMV hoàn trả": None,
                                                         "Hoa hồng ước tính": None, "Số món bán ra": None,
                                                         "Số món hoàn trả": None, "GPM": None,
                                                         "Khách hàng liên kết TB": None})
                                if res_rows:
                                    st.dataframe(pd.DataFrame(res_rows), use_container_width=True, hide_index=True)

            with la_right:
                st.markdown("#### HARDWARE INFO")
                _busy_l2, _ = sys_lock.get_status()
                st.markdown(
                    f"- **GATEWAY:** `{os.environ.get('COMPUTERNAME', 'SERVER-01')}`\n"
                    f"- **STATUS:** `{'BUSY' if _busy_l2 else 'AVAILABLE'}`"
                )
                st.caption("Mặc định 40 dòng / trang. Tích vào ô Chọn ở các trang khác nhau đều được ghi nhận.")
                st.caption("Số liệu lấy trong **cửa sổ 28 ngày gần nhất** của tab LIVE trên affiliate portal.")
                st.caption("EXECUTE chỉ ghi 7 cột: **GMV · Hoa hồng ước tính · CTR (%) · video_view_avg · video_interact_avg · Lượt xem · Lượt thích** (bạn đã tạo sẵn trong base).")

        # =============================================================
        # MODE B: dán '@username live_id' → bảng + tải Excel/CSV (không ghi Lark)
        # =============================================================
        with mode_b_live:
            lb_left, lb_right = st.columns([1.8, 1])
            with lb_left:
                st.markdown("#### 1. DÁN LIVE ID")
                paste_lives = st.text_area(
                    "Mỗi dòng một cặp `@username live_id` (hoặc `username live_id`):",
                    height=200, key="live_paste_text",
                    placeholder="@kiendacheck 7501234567890123456\n@calie_official 7519876543210987654",
                )
                line_list = [ln.strip() for ln in (paste_lives or "").splitlines() if ln.strip()]
                st.caption(f"Đã nhận **{len(line_list)}** dòng.")

                st.markdown("#### 2. EXECUTION")
                is_busy_lb, runner_lb = sys_lock.get_status()
                if st.session_state.get("live_paste_running"):
                    st.info("Đang xử lý — đừng F5 / chuyển tab cho đến khi xong.")
                elif is_busy_lb:
                    st.button(f"SYSTEM OCCUPIED BY {runner_lb.upper()}",
                              disabled=True, use_container_width=True, key="live_paste_busy")
                elif st.button(
                    f"EXECUTE · LẤY SỐ LIỆU LIVE  ({len(line_list)} dòng)",
                    disabled=(len(line_list) == 0),
                    use_container_width=True, type="primary",
                    key="live_paste_run_btn",
                ):
                    if sys_lock.try_acquire("live_paste"):
                        st.session_state.live_paste_running = True
                        st.session_state.live_paste_lines = line_list
                        st.rerun()

                if st.session_state.get("live_paste_running"):
                    lines = st.session_state.get("live_paste_lines", [])
                    prog = st.progress(0.0)
                    status_box = st.empty()
                    results = []
                    driver = None
                    try:
                        driver = init_driver(get_login_profile_dir(ADMIN_KEY))
                        import_auth_state(driver, auth_state_path)

                        def _cb_lb(i, label, msg):
                            try:
                                prog.progress(min((i + 1) / max(len(lines), 1), 1.0))
                                status_box.write(f"[{i+1}/{len(lines)}] {label}: {msg}")
                            except Exception:
                                pass

                        _shop_def_lb = aff_index.load_shop_config(ADMIN_KEY)
                        results = aff_index.process_live_links_paste(
                            driver, lines, progress_cb=_cb_lb,
                            default_shop_id=_shop_def_lb.get("shop_id") or None,
                            default_shop_region=_shop_def_lb.get("shop_region") or "VN",
                            admin_key=ADMIN_KEY,
                        )
                    except Exception as e:
                        st.error(f"Lỗi vận hành: {e}")
                    finally:
                        try:
                            if driver: driver.quit()
                        except Exception:
                            pass
                        sys_lock.release()
                        st.session_state.live_paste_running = False
                        st.session_state.live_paste_results = results
                        st.rerun()

                if "live_paste_results" in st.session_state and not st.session_state.get("live_paste_running"):
                    rs = st.session_state.live_paste_results
                    ok = sum(1 for r in rs if r.get("ok"))
                    err = len(rs) - ok
                    st.success(f"Hoàn thành: {ok} OK · {err} lỗi")
                    res_rows = []
                    for r in rs:
                        if r.get("ok"):
                            res_rows.append({
                                "Dòng": r.get("line"), "username": r.get("username"),
                                "live_id": r.get("live_id"), "creator_id": r.get("creator_id"),
                                "shop_id": r.get("shop_id"), "shop_region": r.get("shop_region"),
                                "Tên LIVE": r.get("Tên LIVE"),
                                "GMV LIVE": r.get("GMV LIVE"),
                                "GMV hoàn trả": r.get("GMV hoàn trả"),
                                "Hoa hồng ước tính": r.get("Hoa hồng ước tính"),
                                "Số món bán ra": r.get("Số món bán ra"),
                                "Số món hoàn trả": r.get("Số món hoàn trả"),
                                "GPM": r.get("GPM"),
                                "CTR (%)": r.get("CTR (%)"),
                                "Khách hàng liên kết TB": r.get("Khách hàng liên kết TB"),
                                "Đơn hàng": r.get("Đơn hàng"),
                                "AOV": r.get("AOV"),
                                "Live PV": r.get("Live PV"),
                                "Lượt thích": r.get("Lượt thích"),
                                "Comments": r.get("Comments"),
                                "Trạng thái": "OK",
                            })
                        else:
                            res_rows.append({
                                "Dòng": r.get("line"), "username": None, "live_id": None,
                                "creator_id": None, "shop_id": None, "shop_region": None,
                                "Tên LIVE": None,
                                "GMV LIVE": None, "GMV hoàn trả": None, "Hoa hồng ước tính": None,
                                "Số món bán ra": None, "Số món hoàn trả": None, "GPM": None,
                                "CTR (%)": None, "Khách hàng liên kết TB": None,
                                "Đơn hàng": None, "AOV": None, "Live PV": None,
                                "Lượt thích": None, "Comments": None,
                                "Trạng thái": f"LỖI: {r.get('error','')}",
                            })
                    if res_rows:
                        df_res = pd.DataFrame(res_rows)
                        st.dataframe(df_res, use_container_width=True, hide_index=True)
                        cdl1, cdl2 = st.columns(2)
                        with cdl1:
                            _buf = io.BytesIO()
                            df_res.to_excel(_buf, index=False, engine="openpyxl")
                            st.download_button(
                                "TẢI EXCEL (.xlsx)", data=_buf.getvalue(),
                                file_name="LIVE_METRICS.xlsx", use_container_width=True,
                                key="live_paste_dl_xlsx",
                            )
                        with cdl2:
                            st.download_button(
                                "TẢI CSV (.csv)",
                                data=df_res.to_csv(index=False).encode("utf-8-sig"),
                                file_name="LIVE_METRICS.csv", use_container_width=True,
                                key="live_paste_dl_csv",
                            )

            with lb_right:
                st.markdown("#### HARDWARE INFO")
                _busy_lb2, _ = sys_lock.get_status()
                st.markdown(
                    f"- **GATEWAY:** `{os.environ.get('COMPUTERNAME', 'SERVER-01')}`\n"
                    f"- **STATUS:** `{'BUSY' if _busy_lb2 else 'AVAILABLE'}`"
                )
                st.caption("Mode này **không ghi** Lark. Kết quả chỉ hiển thị + tải file.")
                st.caption("Cần cấu hình **Shop ID** ở ADMIN SETTINGS trước.")

with tab_control:
    st.markdown("#### ADMIN GATEWAY")
    pass_input = st.text_input("Credential:", type="password")
    if pass_input == ADMIN_PASSWORD:
        st.success("ACCESS GRANTED")
        ca, cb = st.columns(2)
        with ca:
            if st.button("OPEN BROWSER LOGIN", use_container_width=True):
                st.session_state.login_driver = init_driver(get_login_profile_dir(ADMIN_KEY))
                st.session_state.login_driver.get("https://affiliate.tiktok.com/connection/creator?shop_region=VN")
            if "login_driver" in st.session_state and st.session_state.login_driver:
                if st.button("SAVE SESSION DATA", use_container_width=True):
                    export_auth_state(st.session_state.login_driver, auth_state_path)
                    st.session_state.login_driver.quit(); st.session_state.login_driver = None
                    st.rerun()
        with cb:
            if st.button("SYSTEM RESET (FORCE KILL)", type="secondary", use_container_width=True):
                if st.session_state.p1: st.session_state.p1.terminate()
                if st.session_state.p2: st.session_state.p2.terminate()
                kill_specific_tool_chromes() # Chỉ đóng chrome của tool
                sys_lock.force_reset()
                st.session_state.job_running = False
                st.rerun()

        st.divider()
        st.markdown("#### AFF SHOP CONFIG")
        st.caption(
            "KOL thường gửi link gốc dạng `https://www.tiktok.com/@user/video/<id>` (không có shop_id). "
            "Lưu `Shop ID` của bạn 1 lần ở đây, các link thiếu shop_id sẽ tự động dùng giá trị này."
        )
        _shop_cfg = aff_index.load_shop_config(ADMIN_KEY)
        sc1, sc2, sc3 = st.columns([3, 1, 1])
        with sc1:
            shop_id_input = st.text_input(
                "Shop ID (TikTok Shop Affiliate)",
                value=_shop_cfg.get("shop_id", ""),
                placeholder="vd 7496137102397442781",
                key="aff_shop_id_input",
            )
        with sc2:
            shop_region_input = st.text_input(
                "Shop Region",
                value=_shop_cfg.get("shop_region", "VN"),
                key="aff_shop_region_input",
            )
        with sc3:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if st.button("SAVE", key="aff_save_shop_cfg", use_container_width=True):
                try:
                    aff_index.save_shop_config(shop_id_input, shop_region_input or "VN", ADMIN_KEY)
                    st.success(f"Đã lưu: shop_id={shop_id_input}, region={shop_region_input or 'VN'}")
                except Exception as e:
                    st.error(f"Lưu lỗi: {e}")
        st.caption(
            "Lấy Shop ID: vào `affiliate.tiktok.com/data/creator-analysis` → mở 1 video bất kỳ → "
            "copy giá trị `shop_id` trên URL."
        )

        st.divider()
        st.markdown("#### CREATOR ID CACHE")
        st.caption(
            "TikTok thường bật login wall ở trang public → không tự lấy được `creator_id` từ link KOL gửi. "
            "Lưu sẵn `username → creator_id` ở đây để bypass. "
            "Lấy creator_id: vào `affiliate.tiktok.com/data/creator-analysis`, click vào 1 video của KOL đó, "
            "copy giá trị `creator_id` trên URL."
        )
        _ccache = aff_index.load_creator_cache(ADMIN_KEY)
        _cache_rows = [{"username": u, "creator_id": cid} for u, cid in _ccache.items()] or [
            {"username": "", "creator_id": ""}
        ]
        edited_cache = st.data_editor(
            pd.DataFrame(_cache_rows),
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "username": st.column_config.TextColumn(
                    "Username", help="phần sau @ trong link (vd: kiendacheck)"),
                "creator_id": st.column_config.TextColumn(
                    "Creator ID", help="số dài, lấy từ URL creator-analysis"),
            },
            key="aff_creator_cache_editor",
        )
        if st.button("SAVE CACHE", key="aff_save_creator_cache", use_container_width=True):
            new_cache = {}
            for _, row in edited_cache.iterrows():
                u = (str(row.get("username") or "")).strip().lower()
                cid = (str(row.get("creator_id") or "")).strip()
                if u and cid and cid.isdigit():
                    new_cache[u] = cid
            aff_index.save_creator_cache(new_cache, ADMIN_KEY)
            st.success(f"Đã lưu {len(new_cache)} mục.")
            st.rerun()

st.markdown("<div style='text-align:center; margin-top:100px; opacity:0.5; font-size:0.8rem;'>Thien Quy Digital Trans | Elite Analytics 2025</div>", unsafe_allow_html=True)