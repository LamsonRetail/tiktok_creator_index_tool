import os
import io
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
from parallel_crawler import export_auth_state, worker_crawl, FIELD_CATALOG

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
ADMIN_PASSWORD = os.environ.get("TT_ADMIN_PASSWORD", "thienquyabc")
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

tab_extract, tab_control = st.tabs(["EXTRACTION HUB", "ADMIN SETTINGS"])

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

st.markdown("<div style='text-align:center; margin-top:100px; opacity:0.5; font-size:0.8rem;'>Thien Quy Digital Trans | Elite Analytics 2025</div>", unsafe_allow_html=True)