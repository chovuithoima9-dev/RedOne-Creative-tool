# 📘 HƯỚNG DẪN DI CHUYỂN REPO GITHUB & QUY TRÌNH PHÁT HÀNH (RELEASE)
> **Tài liệu hướng dẫn kỹ thuật dành cho Quản trị viên & Nhà phát triển**  
> *Áp dụng cho hệ thống RedOne Creative Tool & Chrome Extension*

---

## MỤC LỤC
1. [Phân tích Cơ chế Cập nhật Tự động (Auto-Update Architecture)](#1-phân-tích-cơ-chế-cập-nhật-tự-động-auto-update-architecture)
2. [Chiến lược Di chuyển Repo sang Tài khoản GitHub mới](#2-chiến-lược-di-chuyển-repo-sang-tài-khoản-github-mới)
   * [Kịch bản 1: Sử dụng tính năng "Transfer Ownership" của GitHub (Khuyên dùng số 1)](#kịch-bản-1-sử-dụng-tính-năng-transfer-ownership-của-github-khuyên-dùng-số-1)
   * [Kịch bản 2: Tạo Repo mới độc lập (Phương án cầu nối an toàn)](#kịch-bản-2-tạo-repo-mới-độc-lập-phương-án-cầu-nối-an-toàn)
3. [Cấu hình Webhook & GitHub Actions khi sang Repo mới](#3-cấu-hình-webhook--github-actions-khi-sang-repo-mới)
4. [Danh sách các vị trí mã nguồn cần cập nhật khi đổi Repo](#4-danh-sách-các-vị-trí-mã-nguồn-cần-cập-nhật-khi-đổi-repo)
5. [Nội dung phát hành mẫu (Release Notes)](#5-nội-dung-phát-hành-mẫu-release-notes)
6. [Hướng dẫn chi tiết dành cho Người dùng khi Cập nhật](#6-hướng-dẫn-chi-tiết-dành-cho-người-dùng-khi-cập-nhật)

---

## 1. PHÂN TÍCH CƠ CHẾ CẬP NHẬT TỰ ĐỘNG (AUTO-UPDATE ARCHITECTURE)

Hệ thống RedOne Creative có 2 thành phần cập nhật độc lập mà người dùng (User) đang cài đặt cục bộ trên máy:

### 1.1. Cập nhật Tool RedOne (Ứng dụng máy tính .EXE)
* **File quản lý**: `backend/services/updater.py` và `backend/config.py`.
* **Cơ chế**: Khi ứng dụng khởi động (hoặc khi người dùng bấm kiểm tra), backend sẽ gửi một HTTP GET request đến API của GitHub:
  ```http
  GET https://api.github.com/repos/{GITHUB_REPO}/releases/latest
  ```
  *(Hiện tại `{GITHUB_REPO}` đang là `kiennt-bit/RedOne-Creative-tool` và được đóng gói cố định bên trong file `.exe` của người dùng).*
* **Luồng xử lý**: Nếu tag phiên bản trên GitHub lớn hơn phiên bản cục bộ, Tool sẽ tải file nén `.zip` về, giải nén vào thư mục tạm và chạy script batch ngầm để thay thế file chạy sau khi ứng dụng tắt, giữ nguyên 100% dữ liệu người dùng (`data/`).

### 1.2. Cập nhật Chrome Extension (RedOne Auth Helper)
* **File quản lý**: `chrome-ext/update.xml`.
* **Cơ chế**: Trình duyệt Google Chrome tự động định kỳ truy vấn đường dẫn update XML được đăng ký trong Windows Registry:
  ```http
  GET https://raw.githubusercontent.com/{GITHUB_REPO}/main/chrome-ext/update.xml
  ```
  File XML này chứa link tải file tiện ích đóng gói `.crx` và số `version` mới nhất.

---

## 2. CHIẾN LƯỢC DI CHUYỂN REPO SANG TÀI KHOẢN GITHUB MỚI

Khi bạn chuyển dự án từ tài khoản cũ (ví dụ: `kiennt-bit`) sang tài khoản mới (ví dụ: `chovuithoima9-dev`), có 2 kịch bản xử lý:

---

### 🟢 KỊCH BẢN 1: Sử dụng tính năng "Transfer Ownership" của GitHub (Khuyên dùng số 1)
> **Đánh giá: An toàn 100% — Không làm gián đoạn bất kỳ User nào!**

* **Bản chất**: GitHub cung cấp tính năng chuyển giao quyền sở hữu nguyên vẹn một repository từ tài khoản này sang tài khoản khác.
* **Cơ chế bảo vệ của GitHub (Permanent Redirect)**:
  * GitHub tự động chuyển tiếp vĩnh viễn (HTTP 301/307 Redirect) mọi yêu cầu từ URL cũ sang URL mới.
  * Lệnh gọi `https://api.github.com/repos/kiennt-bit/RedOne-Creative-tool/releases/latest` sẽ **tự động chuyển hướng** sang `https://api.github.com/repos/chovuithoima9-dev/RedOne-Creative-tool/releases/latest`.
  * Các đường dẫn file thô `raw.githubusercontent.com/...` cũng được redirect tương ứng.
* **Kết quả**: Tất cả các User đang cài phiên bản cũ trên máy vẫn kiểm tra và tải bản cập nhật mới bình thường mà không cần bất kỳ thao tác cài lại nào!
* **Các bước thực hiện**:
  1. Đăng nhập tài khoản GitHub cũ chứa repo.
  2. Vào **Settings** của repo ➔ Kéo xuống mục **Danger Zone** ở cuối trang.
  3. Bấm nút **Transfer ownership**.
  4. Nhập username của tài khoản GitHub mới và xác nhận chuyển giao.
  5. Đăng nhập tài khoản mới và chấp nhận yêu cầu chuyển giao (Accept transfer).

---

### 🟡 KỊCH BẢN 2: Tạo Repo mới độc lập (Phương án cầu nối an toàn)
> **Áp dụng khi**: Bạn không thể truy cập tài khoản cũ hoặc muốn tách riêng một repo mới hoàn toàn.

* **Cảnh báo rủi ro**: Nếu bạn chỉ tạo repo mới trên tài khoản mới rồi **xóa hoặc chuyển private** repo cũ ngay lập tức, toàn bộ các máy User cũ khi gọi API kiểm tra update sẽ nhận mã lỗi `404 Not Found` và mất hoàn toàn khả năng tự động cập nhật!
* **Quy trình "Bắc cầu" giải quyết triệt để**:
  1. **Bước 1**: Đẩy mã nguồn lên repo mới trên tài khoản mới.
  2. **Bước 2**: Giữ repo cũ ở chế độ Public thêm một khoảng thời gian.
  3. **Bước 3 (Bản phát hành bắc cầu)**:
     * Cập nhật cấu hình trong code: đổi `GITHUB_REPO = "TAI_KHOAN_MOI/RedOne-Creative-tool"`.
     * Build đóng gói bản cập nhật mới (ví dụ v1.6.3).
     * **Phát hành (Publish Release) bản v1.6.3 này trên CẢ 2 REPO (cũ và mới)**.
  4. **Bước 4**: Khi User cũ mở Tool lên, Tool sẽ tự động kéo bản v1.6.3 từ repo cũ về. Sau khi cài xong v1.6.3, code trong máy của User đã được đổi sang repo mới ➔ Từ các lần sau, User sẽ tự động nhận cập nhật từ repo mới!

---

## 3. CẤU HÌNH WEBHOOK & GITHUB ACTIONS KHI SANG REPO MỚI

Trong dự án có sẵn cấu hình tự động thông báo Discord khi có bản Release mới tại `.github/workflows/discord-release.yml`.

### Các bước cài đặt Webhook tại Repo mới:
1. Mở repository trên tài khoản GitHub mới.
2. Vào tab **Settings** ➔ Menu bên trái chọn **Secrets and variables** ➔ **Actions**.
3. Bấm **New repository secret** và thêm 2 khóa bảo mật:
   * **`DISCORD_ID`**: ID của Webhook Discord của bạn (dãy số đầu trong link webhook).
   * **`DISCORD_TOKEN`**: Token của Webhook Discord (chuỗi ký tự phía sau ID).
4. *(Tùy chọn Webhook ngoài)*: Nếu bạn muốn gửi Webhook payload JSON trực tiếp sang hệ thống quản lý riêng:
   * Vào **Settings** ➔ **Webhooks** ➔ Bấm **Add webhook**.
   * Điền **Payload URL**, chọn **Content type** là `application/json`.
   * Chọn sự kiện kích hoạt: `Releases` (hoặc `Pushes` tùy nhu cầu).

---

## 4. DANH SÁCH CÁC VỊ TRÍ MÃ NGUỒN CẦN CẬP NHẬT KHI ĐỔI REPO

Khi chính thức chuyển Remote sang repo mới, bạn hãy thay thế chuỗi tên repo cũ (`kiennt-bit/RedOne-Creative-tool`) thành tên mới:

1. **[`backend/config.py`](file:///d:/RedOne%20Creative%20tool/backend/config.py)**:
   * Dòng 13: `GITHUB_REPO = "TEN_TAI_KHOAN_MOI/RedOne-Creative-tool"`
2. **[`installer/RedOne.iss`](file:///d:/RedOne%20Creative%20tool/installer/RedOne.iss)**:
   * Dòng 17: Cập nhật URL file `update.xml` theo tài khoản mới.
3. **[`chrome-ext/update.xml`](file:///d:/RedOne%20Creative%20tool/chrome-ext/update.xml)**:
   * Dòng 4: Cập nhật đường dẫn file `redone-auth-helper.crx` trỏ về tài khoản mới.
4. **[`frontend/js/app.js`](file:///d:/RedOne%20Creative%20tool/frontend/js/app.js)**:
   * Dòng 444: `const _GH_FALLBACK = 'https://github.com/TEN_TAI_KHOAN_MOI/RedOne-Creative-tool';`
5. **Đổi Git Remote trên máy tính dev**:
   ```bash
   git remote set-url origin https://github.com/TEN_TAI_KHOAN_MOI/RedOne-Creative-tool.git
   git push -u origin main --tags
   ```

---

## 5. NỘI DUNG PHÁT HÀNH MẪU (RELEASE NOTES)
*(Dùng để dán trực tiếp vào phần Description khi tạo Release mới trên GitHub)*

```markdown
# 🚀 RedOne Creative Tool & Extension (v1.6.3)

> **Bản cập nhật lớn:** Tích hợp trọn bộ mô hình Google Flow 2.1 thế hệ mới nhất (`Nano Banana 2.1`), khắc phục triệt để lỗi Model không khả dụng và hỗ trợ tạo media song song đa tài khoản Google Flow.

---

### 🌟 Tính năng & Cải tiến nổi bật:

#### 1. 🍌 Hỗ trợ toàn diện Google Flow 2.1 (Mới nhất)
* **Tích hợp Model `Nano Banana 2.1` (`BELUGA`)**: Cập nhật chính xác tên mã nội bộ mới nhất của Google Flow tháng 10/2026.
* **Tích hợp Model `Nano Banana 2 Lite` (`HARBOR_SEAL`)**: Tối ưu tốc độ tạo ảnh siêu tốc, giữ nguyên chất lượng.
* **Sửa dứt điểm lỗi "Model không khả dụng hoặc Google không còn hỗ trợ"**: Khớp chuẩn xác mã RPC `ogiZ0b` trên toàn bộ hệ thống Storyboard, Tạo ảnh và Photoshop Plugin.

#### 2. 👥 Chạy song song đa tài khoản Google Flow (Multi-Account)
* Hỗ trợ chạy cùng lúc 2 (hoặc nhiều) Profile Chrome với các tài khoản Google Flow khác nhau.
* Bấm nút Gen ở cửa sổ Profile nào, task sẽ tự động được gán và thực thi chính xác bằng tài khoản của Profile đó.
* Gán tag tài khoản (`account_email`) trực quan cho từng task trong danh sách quản lý.
* Đồng bộ hóa thời gian thực (Real-time Sync qua WebSocket) tiến độ task giữa tất cả các cửa sổ Chrome.

#### 3. 👁️ Sửa lỗi xem chi tiết Task chéo Profile
* Cơ chế tự động tìm nạp dữ liệu `tasksStore.getOrFetch` giúp xem chi tiết ảnh/video kết quả mượt mà dù đang đứng ở cửa sổ Profile tài khoản khác.

---

### 📥 Hướng dẫn cập nhật:
* **Với Tool**: Nhấn nút "Cập nhật ngay" khi mở app (hoặc tải file zip bản mới bên dưới và giải nén đè).
* **Với Extension**: Vào `chrome://extensions/` ➔ Bật Developer mode ➔ Bấm nút **Cập nhật** (hoặc nút xoay `↻` trên tiện ích RedOne Auth Helper).
```

---

## 6. HƯỚNG DẪN CHI TIẾT DÀNH CHO NGƯỜI DÙNG KHI CẬP NHẬT

### 6.1. Dành cho Người dùng cập nhật Tool RedOne:
* **Cách 1 (Tự động ngay trong app)**:
  1. Mở phần mềm RedOne Creative lên.
  2. Khi có bản cập nhật mới, ở góc trên bên phải màn hình sẽ xuất hiện thông báo bản cập nhật mới kèm nút **"Cập nhật ngay"**.
  3. Bấm nút **"Cập nhật ngay"** ➔ Tool tự động tải gói cài đặt, giải nén và khởi động lại.
  4. *Toàn bộ tài khoản, lịch sử gen và dữ liệu cá nhân trong thư mục `data/` được giữ nguyên vẹn 100%.*
* **Cách 2 (Cài đặt thủ công)**:
  1. Tải file `RedOne-Creative-v<VERSION>-win64.zip` từ trang GitHub Releases.
  2. Tắt hoàn toàn phần mềm cũ.
  3. Giải nén file zip và chạy file `RedOne Creative.exe`.

### 6.2. Dành cho Người dùng cập nhật Extension (RedOne Auth Helper):
1. Mở Google Chrome, truy cập: `chrome://extensions/`.
2. Đảm bảo công tắc **Developer mode** ở góc trên bên phải đang bật.
3. Bấm vào nút **Cập nhật (Update)** ở thanh công cụ trên cùng (hoặc bấm vào biểu tượng mũi tên xoay tròn **`↻`** trên thẻ tiện ích **RedOne Auth Helper**).
4. Kiểm tra phiên bản hiển thị là phiên bản mới nhất và công tắc màu xanh đang bật.
